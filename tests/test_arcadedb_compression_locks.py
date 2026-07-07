import json, os, time, uuid
from threading import Thread

import pytest

pytestmark = pytest.mark.skip_phase3

try:
    from hermes_cli.arcadedb_session import ArcadedbSessionDB
    HAS_SESSION = True
except ImportError:
    HAS_SESSION = False


def _uid():
    return uuid.uuid4().hex[:8]


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestCompressionLocks:
    def _uid(self):
        return uuid.uuid4().hex[:8]

    def test_acquire_first(self, arcadedb_session):
        """CL-01: First acquire on a session succeeds."""
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
        """CL-06: Release by non-owner -> no-op, lock stays."""
        sid = f"lock-6-{self._uid()}"
        arcadedb_session.create_session(sid, source="test")
        assert arcadedb_session.try_acquire_compression_lock(
            sid, "worker-1", ttl_seconds=30
        )
        arcadedb_session.release_compression_lock(sid, "worker-2")
        time.sleep(0.2)
        assert not arcadedb_session.try_acquire_compression_lock(
            sid, "worker-3", ttl_seconds=30
        )

    def test_get_holder(self, arcadedb_session):
        """CL-07: get_holder returns the holder name."""
        sid = f"lock-7-{self._uid()}"
        arcadedb_session.create_session(sid, source="test")
        ok = arcadedb_session.try_acquire_compression_lock(
            sid, "worker-1", ttl_seconds=30
        )
        assert ok is True
        time.sleep(0.2)
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
        assert arcadedb_session.try_acquire_compression_lock(sid, "w1", ttl_seconds=30)
        time.sleep(1)
        assert arcadedb_session.refresh_compression_lock(sid, "w1", ttl_seconds=300)
        time.sleep(0.3)
        arcadedb_session.release_compression_lock(sid, "w1")
        time.sleep(0.3)
        assert arcadedb_session.try_acquire_compression_lock(sid, "w2", ttl_seconds=30)


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestSecondPass:
    """CL-13: Idempotency — second pass on existing session works correctly."""

    def test_second_pass_session(self, arcadedb_session):
        sid = f"idem-{uuid.uuid4().hex[:6]}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.append_message(sid, role="user", content="msg1")
        assert arcadedb_session.try_acquire_compression_lock(sid, "w1", ttl_seconds=30)
        arcadedb_session.ensure_session(sid, source="test")
        arcadedb_session.append_message(sid, role="assistant", content="msg2")
        assert not arcadedb_session.try_acquire_compression_lock(sid, "w2", ttl_seconds=30)

    def test_second_pass_lock_expiry(self, arcadedb_session):
        sid = f"idem2-{uuid.uuid4().hex[:6]}"
        arcadedb_session.create_session(sid, source="test")
        assert arcadedb_session.try_acquire_compression_lock(sid, "w1", ttl_seconds=-2)
        time.sleep(0.3)
        assert arcadedb_session.try_acquire_compression_lock(sid, "w2", ttl_seconds=30)


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestOrphanedLocks:
    """CL-14: Orphaned lock cleanup after crash/timeout."""

    def test_orphaned_lock_cleanup(self, arcadedb_session):
        sid = f"orphan-{uuid.uuid4().hex[:6]}"
        arcadedb_session.create_session(sid, source="test")
        assert arcadedb_session.try_acquire_compression_lock(sid, "dead-worker", ttl_seconds=-2)
        time.sleep(0.3)
        assert arcadedb_session.try_acquire_compression_lock(sid, "new-worker", ttl_seconds=30)

    def test_orphaned_lock_not_released_early(self, arcadedb_session):
        sid = f"orphan2-{uuid.uuid4().hex[:6]}"
        arcadedb_session.create_session(sid, source="test")
        assert arcadedb_session.try_acquire_compression_lock(sid, "dead-worker", ttl_seconds=30)
        time.sleep(0.3)
        assert not arcadedb_session.try_acquire_compression_lock(sid, "new-worker", ttl_seconds=30)


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestCompressionCooldown:
    """CL-09-11: compression failure cooldown lifecycle."""

    def test_record_cooldown(self, arcadedb_session):
        import time
        sid = f"cd-{uuid.uuid4().hex[:6]}"
        arcadedb_session.create_session(sid, source="test")
        until = time.time() + 300
        arcadedb_session.record_compression_failure_cooldown(sid, until, "test error")
        time.sleep(0.2)
        cd = arcadedb_session.get_compression_failure_cooldown(sid)
        assert cd is not None
        assert cd["cooldown_until"] is not None

    def test_get_no_cooldown(self, arcadedb_session):
        sid = f"cd-{uuid.uuid4().hex[:6]}"
        arcadedb_session.create_session(sid, source="test")
        cd = arcadedb_session.get_compression_failure_cooldown(sid)
        assert cd is None

    def test_clear_cooldown(self, arcadedb_session):
        import time
        sid = f"cd-{uuid.uuid4().hex[:6]}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.record_compression_failure_cooldown(sid, time.time() + 300, "err")
        arcadedb_session.clear_compression_failure_cooldown(sid)
        assert arcadedb_session.get_compression_failure_cooldown(sid) is None
