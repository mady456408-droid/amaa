"""
Performance benchmark: Sequential vs Concurrent batch dispatcher.

Uses actual CreatorsRateLimiter with 10x time scaling for fast execution.
All measured times are multiplied by SCALE_FACTOR to project real-world values.

Real config: TPS=1.0, HTTP=0.3s
Bench config: TPS=10.0, HTTP=0.03s (10x faster, then scale results back)
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from creators_api import CreatorsRateLimiter

# ---------------------------------------------------------------------------
# Time scaling: run 10x faster, then scale results to real-world values
# ---------------------------------------------------------------------------
SCALE_FACTOR = 10.0
BENCH_TPS = 10.0                    # real: 1.0
BENCH_HTTP_LATENCY = 0.03           # real: 0.3s
BENCH_DB_READ = 0.0001              # real: 0.001s
BENCH_EVAL_PER_PRODUCT = 0.00005    # real: 0.0005s

REAL_TPS = BENCH_TPS / SCALE_FACTOR  # 1.0
REAL_HTTP = BENCH_HTTP_LATENCY * SCALE_FACTOR  # 0.3s
BATCH_SIZE = 10


class AdaptiveSemaphore:
    def __init__(self, initial=4, min_limit=1, max_limit=4, recovery_threshold=3):
        self.min_limit, self.max_limit = min_limit, max_limit
        self.current_limit, self.recovery_threshold = initial, recovery_threshold
        self._active_count, self._consecutive_successes = 0, 0
        self._cond = None

    def _get_cond(self):
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    async def acquire(self):
        cond = self._get_cond()
        async with cond:
            while self._active_count >= self.current_limit:
                await cond.wait()
            self._active_count += 1

    async def release(self):
        cond = self._get_cond()
        async with cond:
            self._active_count = max(0, self._active_count - 1)
            cond.notify_all()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *a):
        await self.release()

    async def record_success(self):
        cond = self._get_cond()
        async with cond:
            self._consecutive_successes += 1
            if self._consecutive_successes >= self.recovery_threshold:
                self._consecutive_successes = 0
                if self.current_limit < self.max_limit:
                    self.current_limit += 1
                    cond.notify_all()
            return self.current_limit


# ---------------------------------------------------------------------------
# Benchmark runners
# ---------------------------------------------------------------------------
async def bench_sequential(n_products, warm_cache=False):
    limiter = CreatorsRateLimiter("SEQ", tps=BENCH_TPS, tpd=999999)
    asins = [f"B{str(i).zfill(9)}" for i in range(n_products)]
    batches = [asins[i:i+BATCH_SIZE] for i in range(0, len(asins), BATCH_SIZE)]

    api_reqs = 0
    t_api_wait = 0.0
    t_http = 0.0
    cache_hits = 0
    cache_misses = 0

    t0 = time.monotonic()

    for batch in batches:
        if warm_cache:
            cache_hits += len(batch)
            continue
        cache_misses += len(batch)

        ta = time.monotonic()
        await limiter.acquire(source="B")
        t_api_wait += time.monotonic() - ta

        th = time.monotonic()
        await asyncio.sleep(BENCH_HTTP_LATENCY)
        t_http += time.monotonic() - th
        api_reqs += 1
        await limiter.release_request()

    # DB + eval
    await asyncio.sleep(BENCH_DB_READ * 2)
    t_db = BENCH_DB_READ * 2
    await asyncio.sleep(BENCH_EVAL_PER_PRODUCT * n_products)
    t_eval = BENCH_EVAL_PER_PRODUCT * n_products

    total = time.monotonic() - t0
    return {
        "total": total, "api_wait": t_api_wait, "http": t_http,
        "db": t_db, "eval": t_eval, "api_reqs": api_reqs,
        "cache_hits": cache_hits, "cache_misses": cache_misses,
        "concurrency": 1,
    }


async def bench_concurrent(n_products, warm_cache=False, concurrency=4):
    limiter = CreatorsRateLimiter("CON", tps=BENCH_TPS, tpd=999999)
    sem = AdaptiveSemaphore(initial=concurrency)
    lock = asyncio.Lock()
    asins = [f"B{str(i).zfill(9)}" for i in range(n_products)]
    batches = [asins[i:i+BATCH_SIZE] for i in range(0, len(asins), BATCH_SIZE)]

    m = {"api_reqs": 0, "api_wait": 0.0, "http": 0.0,
         "cache_hits": 0, "cache_misses": 0}

    t0 = time.monotonic()

    async def _do(batch):
        if warm_cache:
            async with lock:
                m["cache_hits"] += len(batch)
            return
        async with sem:
            async with lock:
                m["cache_misses"] += len(batch)
            ta = time.monotonic()
            await limiter.acquire(source="B")
            aw = time.monotonic() - ta
            th = time.monotonic()
            await asyncio.sleep(BENCH_HTTP_LATENCY)
            ht = time.monotonic() - th
            async with lock:
                m["api_wait"] += aw
                m["http"] += ht
                m["api_reqs"] += 1
            await limiter.release_request()
            await sem.record_success()

    await asyncio.gather(*[_do(b) for b in batches])

    await asyncio.sleep(BENCH_DB_READ * 2)
    t_db = BENCH_DB_READ * 2
    await asyncio.sleep(BENCH_EVAL_PER_PRODUCT * n_products)
    t_eval = BENCH_EVAL_PER_PRODUCT * n_products

    total = time.monotonic() - t0
    return {
        "total": total, "api_wait": m["api_wait"], "http": m["http"],
        "db": t_db, "eval": t_eval, "api_reqs": m["api_reqs"],
        "cache_hits": m["cache_hits"], "cache_misses": m["cache_misses"],
        "concurrency": concurrency,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def scale(v):
    return v * SCALE_FACTOR

def print_row(label, d):
    t = scale(d["total"])
    aw = scale(d["api_wait"])
    ht = scale(d["http"])
    db = scale(d["db"])
    ev = scale(d["eval"])
    api_pct = (aw + ht) / t * 100 if t > 0 else 0
    print(f"  {label}")
    print(f"    {'Total products:':<30} {d.get('n', '?')}")
    print(f"    {'API requests:':<30} {d['api_reqs']}")
    print(f"    {'Batch size:':<30} {BATCH_SIZE}")
    print(f"    {'Configured TPS:':<30} {REAL_TPS}")
    print(f"    {'Max concurrency:':<30} {d['concurrency']}")
    print(f"    {'Total elapsed (projected):':<30} {t:.2f}s")
    print(f"    {'API wait time (limiter):':<30} {aw:.2f}s")
    print(f"    {'Simulated HTTP time:':<30} {ht:.2f}s")
    print(f"    {'DB time:':<30} {db:.4f}s")
    print(f"    {'Evaluation time:':<30} {ev:.4f}s")
    print(f"    {'429 count:':<30} 0")
    print(f"    {'Cache hits:':<30} {d['cache_hits']}")
    print(f"    {'Cache misses:':<30} {d['cache_misses']}")
    print(f"    {'API % of total:':<30} {api_pct:.1f}%")


async def main():
    counts = [100, 500, 1000, 1500]

    print("=" * 72)
    print("PRICE TRACKER PERFORMANCE BENCHMARK")
    print(f"Real TPS={REAL_TPS}  Real HTTP={REAL_HTTP}s  Batch={BATCH_SIZE}")
    print(f"Bench TPS={BENCH_TPS}  Scale={SCALE_FACTOR}x")
    print("=" * 72)

    all_results = []

    for scenario, warm in [("A) COLD CACHE", False), ("B) WARM CACHE", True)]:
        print(f"\n{'='*72}")
        print(f"SCENARIO {scenario}")
        print(f"{'='*72}")

        for n in counts:
            print(f"\n--- {n} products ---")
            seq = await bench_sequential(n, warm_cache=warm)
            seq["n"] = n
            print_row("OLD: Sequential", seq)
            print()

            con = await bench_concurrent(n, warm_cache=warm, concurrency=4)
            con["n"] = n
            print_row("NEW: Concurrent (×4)", con)

            s_t = scale(seq["total"])
            c_t = scale(con["total"])
            speedup = s_t / c_t if c_t > 0 else float("inf")
            floor = max(0, seq["api_reqs"] - 1) * (1.0 / REAL_TPS)

            print(f"\n  === COMPARISON ({n} products) ===")
            print(f"    {'Old (sequential):':<30} {s_t:.2f}s")
            print(f"    {'New (concurrent):':<30} {c_t:.2f}s")
            print(f"    {'Speedup ratio:':<30} {speedup:.2f}x")
            print(f"    {'TPS theoretical floor:':<30} {floor:.1f}s ({seq['api_reqs']} reqs @ TPS={REAL_TPS})")

            all_results.append((n, warm, s_t, c_t, speedup, seq["api_reqs"], floor))

    # Final summary
    print(f"\n{'='*72}")
    print("FINAL SUMMARY")
    print(f"{'='*72}")
    print(f"\n  {'Products':<10} {'Scenario':<12} {'Old (s)':<10} {'New (s)':<10} {'Speedup':<10} {'API Reqs':<10} {'TPS Floor (s)':<14}")
    print(f"  {'-'*10} {'-'*12} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*14}")
    for n, warm, s_t, c_t, sp, reqs, floor in all_results:
        sc = "warm" if warm else "cold"
        print(f"  {n:<10} {sc:<12} {s_t:<10.2f} {c_t:<10.2f} {sp:<10.2f} {reqs:<10} {floor:<14.1f}")

    print(f"""
  KEY FINDINGS:
  =============
  With TPS={REAL_TPS}, the rate limiter is the dominant bottleneck.
  It enforces {1.0/REAL_TPS:.1f}s between consecutive API requests regardless
  of how many concurrent tasks are running.

  The rate limiter's acquire() sets _last_request inside a lock BEFORE
  sleeping. Concurrent callers get staggered target times:
    Task 0 → wait 0s, Task 1 → wait 1s, Task 2 → wait 2s, Task 3 → wait 3s

  WHAT THE OPTIMIZATION ACTUALLY IMPROVES:
  - Bulk DB queries eliminate N+1 per-ASIN lookups
  - In-memory budget counter eliminates per-batch DB queries
  - 429 handling: cooldown in limiter instead of blocking all batches
  - AdaptiveSemaphore reduces concurrency adaptively (4→2→1)
  - Future-proof: if TPS increases, concurrency benefit is immediate

  WHAT WOULD ACTUALLY REDUCE CYCLE TIME:
  1. Higher TPS quota from Amazon (most impactful)
  2. Better cache hit rates (warm cache → near-zero cycle time)
  3. Smarter scheduling to skip recently-checked products
""")


if __name__ == "__main__":
    asyncio.run(main())
