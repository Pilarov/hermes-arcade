"""Redis-based distributed lock manager for compression locks.

Replaces DB-based CAS (which requires REPEATABLE_READ isolation)
with Redis SET NX EX — atomic, deterministic, crash-safe.

ArcadeDB Issue #1000 (READ_COMMITTED only) makes DB-based CAS
non-deterministic. Redis provides atomic compare-and-set via SET NX.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Lua script: release lock ONLY if we hold it (atomic check-and-delete)
_RELEASE_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""

_KEY_PREFIX = "hermes:lock:compression"


class RedisLockManager:
    """Distributed lock manager backed by Redis.

    Usage:
        lock = RedisLockManager(redis_client)
        if lock.try_acquire("session-123", "worker-1", ttl=300):
            try:
                do_compression()
                lock.refresh("session-123", "worker-1", ttl=300)
            finally:
                lock.release("session-123", "worker-1")
    """

    def __init__(self, redis_client):
        self._redis = redis_client
        self._release_script = self._redis.register_script(_RELEASE_SCRIPT)

    def _key(self, session_id: str) -> str:
        return f"{_KEY_PREFIX}:{session_id}"

    def try_acquire(
        self, session_id: str, holder: str, ttl_seconds: float = 300,
    ) -> bool:
        """Try to acquire lock. Returns True if acquired, False if held.

        ttl_seconds <= 0 means "already expired" — always succeeds
        (the lock is dead on arrival). Redis can't SET with non-positive EX.
        """
        if not session_id:
            return False
        if ttl_seconds <= 0:
            return True
        try:
            result = self._redis.set(
                self._key(session_id), holder,
                nx=True, ex=int(ttl_seconds),
            )
            return bool(result)
        except Exception:
            logger.debug("Redis try_acquire failed", exc_info=True)
            return False

    def release(self, session_id: str, holder: str) -> None:
        """Release lock if we hold it (atomic check-and-delete)."""
        if not session_id:
            return
        try:
            self._release_script(
                keys=[self._key(session_id)], args=[holder],
            )
        except Exception:
            logger.debug("Redis release failed", exc_info=True)

    def refresh(
        self, session_id: str, holder: str, ttl_seconds: float = 300,
    ) -> bool:
        """Extend lock TTL. Returns True if lock still held by us."""
        if not session_id:
            return False
        try:
            result = self._redis.set(
                self._key(session_id), holder,
                xx=True, ex=int(ttl_seconds),
            )
            return bool(result)
        except Exception:
            logger.debug("Redis refresh failed", exc_info=True)
            return False

    def get_holder(self, session_id: str) -> Optional[str]:
        """Return current lock holder, or None if lock is free."""
        if not session_id:
            return None
        try:
            return self._redis.get(self._key(session_id))
        except Exception:
            logger.debug("Redis get_holder failed", exc_info=True)
            return None
