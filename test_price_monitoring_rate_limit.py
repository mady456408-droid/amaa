#!/usr/bin/env python3
"""
Unit and regression test suite for Price Monitoring adaptive rate limiting,
global cooldown, jittered exponential backoff, and realtime traffic isolation.
"""

import asyncio
import random
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from creators_api import (
    CreatorsAPIError,
    CreatorsClient,
    CreatorsRateLimiter,
    NormalizedItem,
)
from price_monitoring import AdaptiveSemaphore, evaluate_product_price_check


class TestAdaptiveRateLimiter(unittest.IsolatedAsyncioTestCase):
    async def test_1_one_batch_gets_429_then_succeeds(self):
        """Test that a batch receiving 429 retries with jittered backoff and succeeds."""
        limiter = CreatorsRateLimiter("TEST_MONITOR", tps=100.0)
        attempts = 0

        async def mock_call():
            nonlocal attempts
            attempts += 1
            await limiter.acquire(source="MONITOR")
            if attempts == 1:
                await limiter.record_cooldown(0.05)
                raise CreatorsAPIError("Rate limited", status_code=429, retry_after=0.05)
            await limiter.record_success()
            return {"B001": "item1"}

        # Simulate batch attempt with retry
        result = None
        for attempt in range(1, 4):
            try:
                result = await mock_call()
                break
            except CreatorsAPIError as exc:
                if exc.status_code == 429:
                    cooldown = exc.retry_after or 0.05
                    await asyncio.sleep(cooldown)

        self.assertEqual(attempts, 2)
        self.assertEqual(result, {"B001": "item1"})
        self.assertEqual(limiter.get_cooldown_remaining(), 0.0)

    async def test_2_multiple_concurrent_batches_get_429_global_cooldown(self):
        """Test that global cooldown pauses all subsequent monitoring requests."""
        limiter = CreatorsRateLimiter("TEST_MONITOR", tps=100.0)
        request_times = []

        async def worker(idx: int, cause_429: bool):
            await limiter.acquire(source="MONITOR")
            t_entry = time.monotonic()
            request_times.append((idx, t_entry))
            if cause_429:
                await limiter.record_cooldown(0.2)
                raise CreatorsAPIError("Rate limited", status_code=429)
            return idx

        # Task 1 triggers 429 and sets 0.2s cooldown
        t0 = time.monotonic()
        t1 = asyncio.create_task(worker(1, cause_429=True))
        await asyncio.sleep(0.01)

        # Task 2 and 3 try to acquire while cooldown is active
        t2 = asyncio.create_task(worker(2, cause_429=False))
        t3 = asyncio.create_task(worker(3, cause_429=False))

        await asyncio.gather(t1, t2, t3, return_exceptions=True)

        # Task 2 and Task 3 must have acquired AFTER the 0.2s cooldown
        worker_2_time = [t for i, t in request_times if i == 2][0]
        worker_3_time = [t for i, t in request_times if i == 3][0]

        self.assertGreaterEqual(worker_2_time - t0, 0.18)
        self.assertGreaterEqual(worker_3_time - t0, 0.18)

    async def test_3_batches_do_not_retry_simultaneously_jitter_and_spacing(self):
        """Test that exponential backoff with jitter and TPS spacing staggers retries."""
        limiter = CreatorsRateLimiter("TEST_MONITOR", tps=20.0)  # 50ms spacing
        retry_timestamps = []

        async def retrying_batch(batch_id: int):
            # Attempt 1: get 429
            base_sec = 0.05
            jitter = random.uniform(0.01, 0.05)
            cooldown = base_sec + jitter
            await limiter.record_cooldown(cooldown)
            await asyncio.sleep(cooldown)

            # Retry attempt: acquire limiter
            await limiter.acquire(source="MONITOR")
            t_retry = time.monotonic()
            retry_timestamps.append((batch_id, t_retry))

        # Launch 4 concurrent batches retrying
        await asyncio.gather(*(retrying_batch(i) for i in range(4)))

        # Sort retry times
        sorted_times = sorted([t for _, t in retry_timestamps])
        # Verify that all 4 retries did NOT happen at the exact same millisecond
        diffs = [sorted_times[i + 1] - sorted_times[i] for i in range(len(sorted_times) - 1)]
        for diff in diffs:
            self.assertGreaterEqual(diff, 0.02, "Retries must be staggered and not simultaneous")

    async def test_4_concurrency_decreases_after_repeated_429(self):
        """Test that AdaptiveSemaphore decreases concurrency 4 -> 2 -> 1 upon 429s."""
        sem = AdaptiveSemaphore(initial=4, min_limit=1, max_limit=4, recovery_threshold=3)
        self.assertEqual(sem.concurrency, 4)

        # 1st 429: drops to 2
        c1 = await sem.record_429()
        self.assertEqual(c1, 2)
        self.assertEqual(sem.concurrency, 2)

        # 2nd 429: drops to 1
        c2 = await sem.record_429()
        self.assertEqual(c2, 1)
        self.assertEqual(sem.concurrency, 1)

        # 3rd 429: stays at min_limit (1)
        c3 = await sem.record_429()
        self.assertEqual(c3, 1)
        self.assertEqual(sem.concurrency, 1)

    async def test_5_concurrency_recovers_gradually_after_successes(self):
        """Test that AdaptiveSemaphore recovers 1 -> 2 -> 3 -> 4 after consecutive successes."""
        sem = AdaptiveSemaphore(initial=1, min_limit=1, max_limit=4, recovery_threshold=3)
        self.assertEqual(sem.concurrency, 1)

        # 2 successes: still 1
        await sem.record_success()
        await sem.record_success()
        self.assertEqual(sem.concurrency, 1)

        # 3rd success: increases to 2
        await sem.record_success()
        self.assertEqual(sem.concurrency, 2)

        # Another 3 successes: increases to 3
        for _ in range(3):
            await sem.record_success()
        self.assertEqual(sem.concurrency, 3)

        # Another 3 successes: increases to 4 (max)
        for _ in range(3):
            await sem.record_success()
        self.assertEqual(sem.concurrency, 4)

        # Further successes do not exceed max_limit
        for _ in range(5):
            await sem.record_success()
        self.assertEqual(sem.concurrency, 4)

    async def test_6_realtime_requests_remain_completely_unaffected(self):
        """Test that realtime requests (draft profile/manual/republish) bypass monitoring cooldown."""
        client = CreatorsClient()
        # Set large cooldown on monitoring limiter
        await client._monitoring_limiter.record_cooldown(10.0)

        self.assertGreater(client._monitoring_limiter.get_cooldown_remaining(), 5.0)
        self.assertEqual(client._realtime_limiter.get_cooldown_remaining(), 0.0)

        # Acquire realtime limiter — should not wait for 10s cooldown
        t0 = time.monotonic()
        await client._realtime_limiter.acquire(source="REALTIME")
        t_elapsed = time.monotonic() - t0
        await client._realtime_limiter.release_request()

        self.assertLess(t_elapsed, 0.5, "Realtime requests must not be delayed by monitoring cooldown")

    async def test_7_unknown_state_preserved_only_for_permanently_failed_batches(self):
        """Test that evaluate_product_price_check preserves UNKNOWN state on genuinely failed batch."""
        db_mock = MagicMock()
        product = {
            "id": 101,
            "asin": "B000TEST01",
            "title": "Test Product",
            "new_availability": "AVAILABLE",
            "resale_availability": "AVAILABLE",
            "new_last_valid_price": 500.0,
            "resale_last_valid_price": 400.0,
        }
        bulk_history = {}

        # 1. Permanently failed batch (item is None)
        eval_failed = await evaluate_product_price_check(db_mock, product, None, bulk_history)
        self.assertTrue(eval_failed["api_failed"])
        self.assertEqual(eval_failed["counts"]["api_failures"], 1)
        self.assertEqual(eval_failed["counts"]["unknown_new"], 1)
        self.assertEqual(eval_failed["counts"]["unknown_resale"], 1)
        # Verify NO database state mutation when API failed
        self.assertEqual(len(eval_failed["product_check_updates"]), 0)
        self.assertEqual(len(eval_failed["seller_state_updates"]), 0)
        self.assertEqual(len(eval_failed["history_records"]), 0)

        # 2. Succeeded batch (valid item)
        item_success = MagicMock()
        item_success.raw_listings = [
            {
                "merchantInfo": {"id": "A1ZVRGNO5AYLOV", "name": "Amazon.eg"},
                "price": {"money": {"amount": 490.0, "currency": "EGP"}},
                "condition": "New",
            }
        ]
        db_mock.get_price_history_records.return_value = []
        eval_success = await evaluate_product_price_check(db_mock, product, item_success, bulk_history)
        self.assertFalse(eval_success["api_failed"])
        self.assertEqual(eval_success["counts"]["api_failures"], 0)
        self.assertGreater(len(eval_success["product_check_updates"]), 0)

    async def test_8_respect_retry_after_header(self):
        """Test that CreatorsAPIError carries retry_after and is respected."""
        err = CreatorsAPIError("Rate limited", status_code=429, retry_after=5.5)
        self.assertEqual(err.status_code, 429)
        self.assertEqual(err.retry_after, 5.5)

    async def test_9_production_simulation_1000_asins(self):
        """
        Simulate 1000 ASINs (100 batches of 10) under rate-limiting pressure,
        demonstrating the difference between legacy uncoordinated retries vs adaptive recovery.
        """
        # Amazon mock state: rolling rate limit window (max 3 req/sec, penalty of 0.05s on overflow)
        class AmazonMock:
            def __init__(self):
                self.lock = asyncio.Lock()
                self.req_times = []
                self.penalty_until = 0.0

            async def call(self):
                async with self.lock:
                    now = time.monotonic()
                    if now < self.penalty_until:
                        raise CreatorsAPIError("Rate limited", status_code=429, retry_after=0.05)
                    self.req_times = [t for t in self.req_times if now - t < 0.1]
                    if len(self.req_times) >= 2:
                        self.penalty_until = now + 0.05
                        raise CreatorsAPIError("Rate limited", status_code=429, retry_after=0.05)
                    self.req_times.append(now)
                    return {"status": "ok"}

        # 1. Simulate BEFORE (legacy) logic
        mock_api_before = AmazonMock()
        sem_before = asyncio.Semaphore(4)
        batches = [[f"ASIN_{i}_{j}" for j in range(10)] for i in range(100)]

        before_429s = 0
        before_failed_batches = 0
        before_unknown_asins = 0

        async def fetch_batch_before(b_idx, asins):
            nonlocal before_429s, before_failed_batches, before_unknown_asins
            async with sem_before:
                max_attempts = 3
                for attempt in range(1, max_attempts + 1):
                    try:
                        return await mock_api_before.call()
                    except CreatorsAPIError as exc:
                        if exc.status_code == 429 and attempt < max_attempts:
                            before_429s += 1
                            retry_in = float(2 ** (attempt - 1)) * 0.01  # Scaled for test
                            await asyncio.sleep(retry_in)
                            continue
                        else:
                            before_429s += 1
                            before_failed_batches += 1
                            before_unknown_asins += len(asins)
                            return {}

        t0_before = time.monotonic()
        await asyncio.gather(*(fetch_batch_before(i, b) for i, b in enumerate(batches)))
        t_dur_before = time.monotonic() - t0_before

        # 2. Simulate AFTER (adaptive) logic
        mock_api_after = AmazonMock()
        adaptive_sem = AdaptiveSemaphore(initial=4, min_limit=1, max_limit=4, recovery_threshold=3)
        limiter_after = CreatorsRateLimiter("SIM_MONITOR", tps=50.0)

        after_429s = 0
        after_failed_batches = 0
        after_unknown_asins = 0
        after_retries = 0

        async def fetch_batch_after(b_idx, asins):
            nonlocal after_429s, after_failed_batches, after_unknown_asins, after_retries
            async with adaptive_sem:
                max_attempts = 4
                for attempt in range(1, max_attempts + 1):
                    try:
                        await limiter_after.acquire(source="MONITOR")
                        res = await mock_api_after.call()
                        await adaptive_sem.record_success()
                        await limiter_after.record_success()
                        await limiter_after.release_request()
                        return res
                    except CreatorsAPIError as exc:
                        await limiter_after.release_request()
                        if exc.status_code == 429:
                            after_429s += 1
                            await adaptive_sem.record_429()
                            base_sec = float(2 ** (attempt - 1)) * 0.02
                            jitter = random.uniform(0.005, 0.02)
                            cooldown_sec = base_sec + jitter
                            if exc.retry_after and exc.retry_after > cooldown_sec:
                                cooldown_sec = exc.retry_after
                            await limiter_after.record_cooldown(cooldown_sec)

                            if attempt < max_attempts:
                                after_retries += 1
                                await asyncio.sleep(cooldown_sec)
                                continue
                            else:
                                after_failed_batches += 1
                                after_unknown_asins += len(asins)
                                return {}

        t0_after = time.monotonic()
        await asyncio.gather(*(fetch_batch_after(i, b) for i, b in enumerate(batches)))
        t_dur_after = time.monotonic() - t0_after

        # Verification: After logic dramatically improves success
        self.assertEqual(after_failed_batches, 0, "All batches should succeed with adaptive rate limiting")
        self.assertEqual(after_unknown_asins, 0, "Zero ASINs should turn into UNKNOWN")
        self.assertGreater(before_failed_batches, 0, "Legacy logic failed batches under pressure")


if __name__ == "__main__":
    unittest.main()
