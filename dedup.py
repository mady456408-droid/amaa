import logging
import time
from collections import OrderedDict

logger = logging.getLogger(__name__)


class TTLCache:
    """Simple TTL cache for message IDs and ASINs."""

    def __init__(self, ttl_seconds: int = 3600, max_size: int = 2000):
        self.ttl_seconds = ttl_seconds
        self.max_size = max_size
        self._store: OrderedDict[str, float] = OrderedDict()

    def _purge_expired(self) -> None:
        now = time.time()
        expired = [key for key, ts in self._store.items() if now - ts > self.ttl_seconds]
        for key in expired:
            del self._store[key]

    def _evict_oldest(self) -> None:
        while len(self._store) > self.max_size:
            self._store.popitem(last=False)

    def add(self, key: str) -> bool:
        """
        Add key if not present. Returns True if key was new, False if duplicate.
        """
        self._purge_expired()
        if key in self._store:
            logger.info("Dedup hit: %s", key)
            return False
        self._store[key] = time.time()
        self._evict_oldest()
        return True

    def contains(self, key: str) -> bool:
        self._purge_expired()
        return key in self._store
