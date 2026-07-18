"""
Gemini API Key Pool with automatic failover and cooldown.

Provides thread-safe management of multiple Gemini API keys with:
- Automatic failover on rate limits (429 errors)
- Cooldown period management
- Request tracking and statistics
- Backward compatibility with single GEMINI_API_KEY
"""

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class GeminiKey:
    """Represents a single Gemini API key with tracking metadata."""
    
    api_key: str
    index: int
    cooldown_until: Optional[datetime] = None
    last_error: Optional[str] = None
    total_requests: int = 0
    total_success: int = 0
    total_failures: int = 0
    
    @property
    def is_healthy(self) -> bool:
        """Check if key is healthy (not in cooldown)."""
        if self.cooldown_until is None:
            return True
        return datetime.now() >= self.cooldown_until
    
    @property
    def cooldown_remaining_seconds(self) -> float:
        """Get remaining cooldown time in seconds."""
        if self.cooldown_until is None:
            return 0.0
        remaining = self.cooldown_until - datetime.now()
        return max(0.0, remaining.total_seconds())


class GeminiKeyPool:
    """
    Thread-safe pool of Gemini API keys with automatic failover.
    
    Supports multiple keys via environment variables:
    - GEMINI_API_KEY_1, GEMINI_API_KEY_2, etc.
    - Falls back to single GEMINI_API_KEY for backward compatibility
    """
    
    _instance: Optional['GeminiKeyPool'] = None
    _lock: threading.Lock = threading.Lock()
    
    def __new__(cls) -> 'GeminiKeyPool':
        """Singleton pattern to ensure only one pool instance exists."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """Initialize the key pool (only once due to singleton)."""
        if self._initialized:
            return
        
        self._keys: list[GeminiKey] = []
        self._pool_lock = threading.Lock()
        self._load_keys()
        self._initialized = True
        
        logger.info(f"GEMINI KEY POOL INITIALIZED: {len(self._keys)} keys loaded")
    
    def _load_keys(self) -> None:
        """Load API keys from environment variables."""
        keys = []
        
        # Try numbered keys first (GEMINI_API_KEY_1, GEMINI_API_KEY_2, etc.)
        index = 1
        while True:
            key = os.getenv(f"GEMINI_API_KEY_{index}")
            if key:
                keys.append(GeminiKey(api_key=key, index=index))
                index += 1
            else:
                break
        
        # Fall back to single GEMINI_API_KEY for backward compatibility
        if not keys:
            single_key = os.getenv("GEMINI_API_KEY")
            if single_key:
                keys.append(GeminiKey(api_key=single_key, index=1))
                logger.info("GEMINI KEY POOL: Using single GEMINI_API_KEY (backward compatibility)")
        
        self._keys = keys
    
    def get_next_key(self) -> Optional[GeminiKey]:
        """
        Get the next healthy key from the pool.
        
        Returns the first healthy key (not in cooldown).
        Returns None if no healthy keys are available.
        """
        with self._pool_lock:
            for key in self._keys:
                if key.is_healthy:
                    key.total_requests += 1
                    logger.info(f"GEMINI KEY #{key.index} SELECTED")
                    return key
            
            logger.warning("ALL GEMINI KEYS ARE IN COOLDOWN")
            return None
    
    def report_success(self, key: GeminiKey) -> None:
        """
        Report a successful request for a key.
        
        Args:
            key: The GeminiKey that was used successfully
        """
        with self._pool_lock:
            key.total_success += 1
            key.last_error = None
            logger.info(f"GEMINI KEY #{key.index} SUCCESS (total_success={key.total_success})")
    
    def report_failure(self, key: GeminiKey, error: str) -> None:
        """
        Report a failed request for a key.
        
        Args:
            key: The GeminiKey that failed
            error: Error message describing the failure
        """
        with self._pool_lock:
            key.total_failures += 1
            key.last_error = error
            logger.warning(f"GEMINI KEY #{key.index} FAILURE: {error} (total_failures={key.total_failures})")
    
    def put_on_cooldown(self, key: GeminiKey, seconds: float) -> None:
        """
        Put a key on cooldown for the specified duration.
        
        Args:
            key: The GeminiKey to put on cooldown
            seconds: Cooldown duration in seconds
        """
        with self._pool_lock:
            key.cooldown_until = datetime.now() + timedelta(seconds=seconds)
            logger.info(f"GEMINI KEY #{key.index} COOLDOWN {seconds}s")
    
    def get_retry_delay_from_error(self, error: Exception) -> Optional[float]:
        """
        Extract retry delay from a 429 error response.
        
        Args:
            error: The exception from the API call
            
        Returns:
            Retry delay in seconds, or None if not available
        """
        error_str = str(error)
        
        # Try to extract retry_delay from error message
        # Example format: "retry_delay { seconds: 25 }"
        if "retry_delay" in error_str.lower():
            try:
                # Look for pattern like "seconds: 25" or "seconds:25"
                import re
                match = re.search(r'seconds[:\s]+(\d+(?:\.\d+)?)', error_str)
                if match:
                    return float(match.group(1))
            except (ValueError, AttributeError):
                pass
        
        return None
    
    def get_stats(self) -> dict:
        """
        Get statistics for all keys in the pool.
        
        Returns:
            Dictionary with key statistics
        """
        with self._pool_lock:
            stats = {
                "total_keys": len(self._keys),
                "healthy_keys": sum(1 for k in self._keys if k.is_healthy),
                "keys": []
            }
            
            for key in self._keys:
                stats["keys"].append({
                    "index": key.index,
                    "is_healthy": key.is_healthy,
                    "cooldown_remaining_seconds": key.cooldown_remaining_seconds,
                    "total_requests": key.total_requests,
                    "total_success": key.total_success,
                    "total_failures": key.total_failures,
                    "last_error": key.last_error
                })
            
            return stats


# Global singleton instance
_key_pool: Optional[GeminiKeyPool] = None
_pool_lock = threading.Lock()


def get_key_pool() -> GeminiKeyPool:
    """
    Get the global GeminiKeyPool singleton instance.
    
    Returns:
        The singleton GeminiKeyPool instance
    """
    global _key_pool
    
    if _key_pool is None:
        with _pool_lock:
            if _key_pool is None:
                _key_pool = GeminiKeyPool()
    
    return _key_pool
