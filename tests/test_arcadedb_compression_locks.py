"""Tests for compression locks over ArcadeDB (Phase 3 subset).

Links:
  Phase 3: hermes_cli/arcadedb_session.py (compression lock methods)
  Phase 3 spec: docs/arcadedb-migration/phase-3-sessiondb.md#compression-locks
  Fixtures: tests/fixtures/arcadedb_fixtures.py

These tests define the atomic CAS lock protocol.
They will FAIL/SKIP until Phase 3 implements ArcadedbSessionDB.
"""

import time
import uuid
from threading import Thread

import pytest

pytestmark = pytest.mark.skip_phase3

try:
    from hermes_cli.arcadedb_session import ArcadedbSessionDB
    HAS_SESSION = True
except ImportError:
    HAS_SESSION = False


@pytest.mark.skipif(not HAS_SESSION, reason="Phase 3 ArcadedbSessionDB not yet implemented")
class TestCompressionLocks:

    @staticmethod
    def _uid():
        return uuid.uuid4().hex[:8]

    def test_acquire_first(self, arcadedb_session):
        """CL-01: First acquire -> True."""
        sid = f"lock-1-{self._uid()}"
        arcadedb_session.create_session(sid, source="test")
        result = arcadedb_session.try_acquire_compression_lock(
            sid, "worker-1", ttl_seconds=30
        )
        assert result is True

    def test_acquire_conflict(self, arcadedb_session):
        """CL-02: Two acquires on same session -> second fails."""
        sid = f"lock-2-{self._uid()}"
        arcadedb_session.create_session(sid, source="test")
        assert arcadedb_session.try_acquire_compression_lock(
            sid, "worker-1", ttl_seconds=30
        )
        assert not arcadedb_session.try_acquire_compression_lock(
            sid, "worker-2", ttl_seconds=30
        )

    def test_acquire_expired(self, arcadedb_session):
        """CL-03: Expired lock can be re-acquired."""
        sid = f"lock-3-{self._uid()}"
        arcadedb_session.create_session(sid, source="test")
        assert arcadedb_session.try_acquire_compression_lock(
            sid, "worker-1", ttl_seconds=-2
        )
        time.sleep(0.5)
        assert arcadedb_session.try_acquire_compression_lock(
            sid, "worker-2", ttl_seconds=30
        )
        time.sleep(0.5)

    def test_refresh_extends(self, arcadedb_session):
        """CL-04: refresh() extends TTL."""
        sid = f"lock-4-{self._uid()}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.try_acquire_compression_lock(
            sid, "worker-1", ttl_seconds=30
        )
        assert arcadedb_session.refresh_compression_lock(
            sid, "worker-1", ttl_seconds=300
        )

    def test_release(self, arcadedb_session):
        """CL-05: After release, lock can be re-acquired."""
        sid = f"lock-5-{self._uid()}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.try_acquire_compression_lock(
            sid, "worker-1", ttl_seconds=30
        )
        arcadedb_session.release_compression_lock(sid, "worker-1")
        assert arcadedb_session.try_acquire_compression_lock(
            sid, "worker-2", ttl_seconds=30
        )

    def test_release_non_owner(self, arcadedb_session):
        """CL-06: Release by non-owner -> no-op, lock stays (verified via CAS)."""
        sid = f"lock-6-{self._uid()}"
        arcadedb_session.create_session(sid, source="test")
        assert arcadedb_session.try_acquire_compression_lock(
            sid, "worker-1", ttl_seconds=30
        )
        # Non-owner release should NOT free the lock
        arcadedb_session.release_compression_lock(sid, "worker-2")
        time.sleep(0.2)
        # Worker-3 cannot acquire — worker-1 still holds it
        assert not arcadedb_session.try_acquire_compression_lock(
            sid, "worker-3", ttl_seconds=30
        )

    def test_get_holder(self, arcadedb_session):
        """CL-07: try_acquire returns holder on success (verified via CAS)."""
        sid = f"lock-7-{self._uid()}"
        arcadedb_session.create_session(sid, source="test")
        ok = arcadedb_session.try_acquire_compression_lock(
            sid, "worker-1", ttl_seconds=30
        )
        assert ok is True  # Lock acquired
        time.sleep(0.2)
        # CAS: worker-2 cannot acquire → proves worker-1 still holds
        assert not arcadedb_session.try_acquire_compression_lock(
            sid, "worker-2", ttl_seconds=30
        )

    def test_concurrent_compressors(self, arcadedb_session):
        """CL-08: 10 concurrent acquirers -> exactly 1 wins."""
        sid = f"lock-8-{self._uid()}"
        arcadedb_session.create_session(sid, source="test")
        time.sleep(0.2)

        results = []

        def try_acquire(worker_id):
            ok = arcadedb_session.try_acquire_compression_lock(
                sid, f"worker-{worker_id}", ttl_seconds=30
            )
            results.append(ok)

        threads = [Thread(target=try_acquire, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(results) == 1


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestRealFlow:
    """CL-12: Production flow — acquire, work, refresh, release, re-acquire."""

    def test_real_flow(self, arcadedb_session):
        sid = f"real-{uuid.uuid4().hex[:6]}"
        arcadedb_session.create_session(sid, source="test")

        # Acquire lock
        assert arcadedb_session.try_acquire_compression_lock(
            sid, "w1", ttl_seconds=30
        )
        # Simulate compression work (2 seconds)
        time.sleep(2)
        # Refresh extends TTL during work
        assert arcadedb_session.refresh_compression_lock(
            sid, "w1", ttl_seconds=300
        )
        time.sleep(0.5)
        # Release
        arcadedb_session.release_compression_lock(sid, "w1")
        time.sleep(0.3)
        # Another worker can acquire
        assert arcadedb_session.try_acquire_compression_lock(
            sid, "w2", ttl_seconds=30
        )


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestSecondPass:
    """CL-13: Idempotency — second pass on existing session works correctly."""

    def test_second_pass_session(self, arcadedb_session):
        sid = f"idem-{uuid.uuid4().hex[:6]}"
        # First pass: create session, add messages, acquire lock
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.append_message(sid, role="user", content="msg1")
        assert arcadedb_session.try_acquire_compression_lock(
            sid, "w1", ttl_seconds=30
        )

        # Second pass: idempotent operations
        arcadedb_session.ensure_session(sid, source="test")
        arcadedb_session.append_message(sid, role="assistant", content="msg2")
        # Lock still held by w1 from first pass
        assert not arcadedb_session.try_acquire_compression_lock(
            sid, "w2", ttl_seconds=30
        )

    def test_second_pass_lock_expiry(self, arcadedb_session):
        sid = f"idem2-{uuid.uuid4().hex[:6]}"
        arcadedb_session.create_session(sid, source="test")
        # Acquire with short TTL
        assert arcadedb_session.try_acquire_compression_lock(
            sid, "w1", ttl_seconds=1
        )
        # Wait for expiry
        time.sleep(1.5)
        # Second pass: new worker can acquire
        assert arcadedb_session.try_acquire_compression_lock(
            sid, "w2", ttl_seconds=30
        )


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestOrphanedLocks:
    """CL-14: Orphaned lock cleanup after crash/timeout."""

    def test_orphaned_lock_cleanup(self, arcadedb_session):
        sid = f"orphan-{uuid.uuid4().hex[:6]}"
        arcadedb_session.create_session(sid, source="test")
        # Acquire lock — "worker crashes"
        assert arcadedb_session.try_acquire_compression_lock(
            sid, "dead-worker", ttl_seconds=1
        )
        # Lock expires (worker didn't release)
        time.sleep(1.5)
        # New worker can acquire the orphaned lock
        assert arcadedb_session.try_acquire_compression_lock(
            sid, "new-worker", ttl_seconds=30
        )

    def test_orphaned_lock_not_released_early(self, arcadedb_session):
        sid = f"orphan2-{uuid.uuid4().hex[:6]}"
        arcadedb_session.create_session(sid, source="test")
        assert arcadedb_session.try_acquire_compression_lock(
            sid, "dead-worker", ttl_seconds=5
        )
        # Immediately: cannot acquire (lock not expired yet)
        assert not arcadedb_session.try_acquire_compression_lock(
            sid, "new-worker", ttl_seconds=30
        )


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestCompressionCooldown:
    """CL-09-11: compression failure cooldown lifecycle."""

    def test_record_cooldown(self, arcadedb_session):
        """CL-09: record_compression_failure_cooldown sets fields."""
        import time
        sid = f"cd-{uuid.uuid4().hex[:6]}"
        arcadedb_session.create_session(sid, source="test")
        until = time.time() + 300
        arcadedb_session.record_compression_failure_cooldown(sid, until, "test error")
        time.sleep(0.2)
        info = arcadedb_session.get_compression_failure_cooldown(sid)
        assert info is not None
        assert "test error" in str(info.get("error", ""))

    def test_get_no_cooldown(self, arcadedb_session):
        """CL-10: get_compression_failure_cooldown returns None when not set."""
        sid = f"cd-{uuid.uuid4().hex[:6]}"
        arcadedb_session.create_session(sid, source="test")
        info = arcadedb_session.get_compression_failure_cooldown(sid)
        assert info is None

    def test_clear_cooldown(self, arcadedb_session):
        """CL-11: clear_compression_failure_cooldown nulls fields."""
        import time
        sid = f"cd-{uuid.uuid4().hex[:6]}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.record_compression_failure_cooldown(sid, time.time() + 300, "err")
        arcadedb_session.clear_compression_failure_cooldown(sid)
        info = arcadedb_session.get_compression_failure_cooldown(sid)
        assert info is None
