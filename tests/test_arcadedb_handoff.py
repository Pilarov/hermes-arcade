"""Tests for handoff state machine over ArcadeDB (Phase 3).

Handoff lifecycle: request → claim → complete/fail.
Uses Session handoff_state/handoff_platform/handoff_error fields.

Links:
  Phase 3: hermes_cli/arcadedb_session.py (handoff methods)
  Fixtures: tests/fixtures/arcadedb_fixtures.py
"""

import time
import uuid

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
class TestHandoffRequest:
    """HO-01: request_handoff sets state to pending."""

    def test_request_handoff(self, arcadedb_session):
        sid = f"ho-req-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        result = arcadedb_session.request_handoff(sid, "telegram")
        assert result is True
        time.sleep(0.2)
        state = arcadedb_session.get_handoff_state(sid)
        assert state["handoff_state"] == "pending"
        assert state["handoff_platform"] == "telegram"


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestHandoffClaim:
    """HO-02: claim_handoff atomically transitions pending → running."""

    def test_claim_handoff(self, arcadedb_session):
        sid = f"ho-claim-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.request_handoff(sid, "telegram")
        result = arcadedb_session.claim_handoff(sid)
        assert result is True
        time.sleep(0.2)
        state = arcadedb_session.get_handoff_state(sid)
        assert state["handoff_state"] == "running"

    def test_claim_already_claimed(self, arcadedb_session):
        """HO-02b: Cannot claim an already-claimed handoff."""
        sid = f"ho-reclaim-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.request_handoff(sid, "telegram")
        time.sleep(0.2)
        assert arcadedb_session.claim_handoff(sid) is True
        # Second claim must fail
        assert arcadedb_session.claim_handoff(sid) is False

    def test_claim_without_request(self, arcadedb_session):
        """HO-02c: Cannot claim a session that never requested handoff."""
        sid = f"ho-noreq-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        assert arcadedb_session.claim_handoff(sid) is False


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestHandoffComplete:
    """HO-03: complete_handoff transitions to completed."""

    def test_complete_handoff(self, arcadedb_session):
        sid = f"ho-done-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.request_handoff(sid, "telegram")
        arcadedb_session.claim_handoff(sid)
        arcadedb_session.complete_handoff(sid)
        time.sleep(0.2)
        state = arcadedb_session.get_handoff_state(sid)
        assert state["handoff_state"] == "completed"


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestHandoffFail:
    """HO-04: fail_handoff stores error and sets state to failed."""

    def test_fail_handoff(self, arcadedb_session):
        sid = f"ho-fail-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.request_handoff(sid, "telegram")
        arcadedb_session.claim_handoff(sid)
        arcadedb_session.fail_handoff(sid, "timeout error")
        time.sleep(0.2)
        state = arcadedb_session.get_handoff_state(sid)
        assert state["handoff_state"] == "failed"
        assert "timeout" in str(state.get("handoff_error", ""))


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestPendingHandoffs:
    """HO-05: list_pending_handoffs returns sessions with state='pending'."""

    def test_list_pending(self, arcadedb_session):
        sid = f"ho-pend-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.request_handoff(sid, "telegram")
        time.sleep(0.2)
        pending = arcadedb_session.list_pending_handoffs()
        assert any(s["id"] == sid for s in pending)

    def test_claimed_not_pending(self, arcadedb_session):
        """HO-05b: Claimed handoffs are not in pending list."""
        sid = f"ho-cl-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.request_handoff(sid, "telegram")
        arcadedb_session.claim_handoff(sid)
        time.sleep(0.2)
        pending = arcadedb_session.list_pending_handoffs()
        assert not any(s["id"] == sid for s in pending)
