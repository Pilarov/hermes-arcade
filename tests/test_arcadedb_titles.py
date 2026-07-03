"""Tests for session title methods over ArcadeDB (Phase 3).

Session title lifecycle: set → get → resolve → next_in_lineage → archive.

Links:
  Phase 3: hermes_cli/arcadedb_session.py (title methods)
  Fixtures: tests/fixtures/arcadedb_fixtures.py
"""

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
class TestSessionTitle:
    """TL-01: set_session_title + get_session_title."""

    def test_set_and_get(self, arcadedb_session):
        sid = f"tl-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        assert arcadedb_session.set_session_title(sid, "My Chat")
        assert arcadedb_session.get_session_title(sid) == "My Chat"

    def test_sanitize_title(self, arcadedb_session):
        """TL-01b: Title sanitation rejects empty/whitespace."""
        sid = f"tl-san-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        assert arcadedb_session.set_session_title(sid, "   ") is False
        assert arcadedb_session.get_session_title(sid) is None

    def test_overwrite_title(self, arcadedb_session):
        """TL-01c: Setting twice overwrites."""
        sid = f"tl-ow-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.set_session_title(sid, "First")
        arcadedb_session.set_session_title(sid, "Second")
        assert arcadedb_session.get_session_title(sid) == "Second"


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestSessionByTitle:
    """TL-02: get_session_by_title + resolve_session_by_title."""

    def test_get_by_title(self, arcadedb_session):
        sid = f"tl-gbt-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.set_session_title(sid, "Unique Title")
        s = arcadedb_session.get_session_by_title("Unique Title")
        assert s is not None and s["id"] == sid

    def test_resolve_by_title(self, arcadedb_session):
        """TL-02b: resolve returns newest session with given title."""
        import time
        sid1 = f"tl-rt1-{_uid()}"
        sid2 = f"tl-rt2-{_uid()}"
        arcadedb_session.create_session(sid1, source="test")
        time.sleep(0.1)
        arcadedb_session.create_session(sid2, source="test")
        time.sleep(0.1)
        arcadedb_session.set_session_title(sid1, "Shared Title")
        arcadedb_session.set_session_title(sid2, "Shared Title")
        resolved = arcadedb_session.resolve_session_by_title("Shared Title")
        assert resolved == sid2  # newest first


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestNextTitleInLineage:
    """TL-03: get_next_title_in_lineage deduplicates titles."""

    def test_first_title(self, arcadedb_session):
        """TL-03a: First use returns base title."""
        next_title = arcadedb_session.get_next_title_in_lineage("Fresh Chat")
        assert next_title == "Fresh Chat"

    def test_second_title(self, arcadedb_session):
        """TL-03b: Second use returns base_title #2."""
        sid = f"tl-nl-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.set_session_title(sid, "My Chat")
        next_title = arcadedb_session.get_next_title_in_lineage("My Chat")
        assert next_title == "My Chat #2"


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestSessionArchived:
    """TL-04: set_session_archived."""

    def test_archive(self, arcadedb_session):
        sid = f"tl-arc-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        assert arcadedb_session.set_session_archived(sid, True)
        s = arcadedb_session.get_session(sid)
        assert s["archived"] == 1

    def test_unarchive(self, arcadedb_session):
        """TL-04b: Un-archiving sets archived=0."""
        sid = f"tl-unarc-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.set_session_archived(sid, True)
        arcadedb_session.set_session_archived(sid, False)
        s = arcadedb_session.get_session(sid)
        assert s["archived"] == 0
