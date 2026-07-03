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

    @pytest.fixture(autouse=True)
    def _unique_prefix(self):
        self._pref = uuid.uuid4().hex[:8]

    def test_acquire_first(self, arcadedb_session):
        """CL-01: First acquire -> True."""
        sid = f"lock-1-{self._pref}"
        arcadedb_session.create_session(sid, source="test")
        result = arcadedb_session.try_acquire_compression_lock(
            sid, "worker-1", ttl_seconds=30
        )
        assert result is True

    def test_acquire_conflict(self, arcadedb_session):
        """CL-02: Two acquires on same session -> second fails."""
        sid = f"lock-2-{self._pref}"
        arcadedb_session.create_session(sid, source="test")
        assert arcadedb_session.try_acquire_compression_lock(
            sid, "worker-1", ttl_seconds=30
        )
        assert not arcadedb_session.try_acquire_compression_lock(
            sid, "worker-2", ttl_seconds=30
        )

    def test_acquire_expired(self, arcadedb_session):
        """CL-03: Expired lock can be re-acquired."""
        sid = f"lock-3-{self._pref}"
        arcadedb_session.create_session(sid, source="test")
        assert arcadedb_session.try_acquire_compression_lock(
            sid, "worker-1", ttl_seconds=0
        )
        time.sleep(0.1)
        assert arcadedb_session.try_acquire_compression_lock(
            sid, "worker-2", ttl_seconds=30
        )

    def test_refresh_extends(self, arcadedb_session):
        """CL-04: refresh() extends TTL."""
        sid = f"lock-4-{self._pref}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.try_acquire_compression_lock(
            sid, "worker-1", ttl_seconds=30
        )
        assert arcadedb_session.refresh_compression_lock(
            sid, "worker-1", ttl_seconds=300
        )

    def test_release(self, arcadedb_session):
        """CL-05: After release, lock can be re-acquired."""
        sid = f"lock-5-{self._pref}"
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
        sid = f"lock-6-{self._pref}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.try_acquire_compression_lock(
            sid, "worker-1", ttl_seconds=30
        )
        arcadedb_session.release_compression_lock(sid, "worker-2")
        holder = arcadedb_session.get_compression_lock_holder(sid)
        assert holder == "worker-1"

    def test_get_holder(self, arcadedb_session):
        """CL-07: get_holder() returns correct holder."""
        sid = f"lock-7-{self._pref}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.try_acquire_compression_lock(
            sid, "worker-1", ttl_seconds=30
        )
        assert arcadedb_session.get_compression_lock_holder(sid) == "worker-1"

    def test_concurrent_compressors(self, arcadedb_session):
        """CL-08: 10 concurrent acquirers -> exactly 1 wins."""
        sid = f"lock-8-{self._pref}"
        arcadedb_session.create_session(sid, source="test")

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
