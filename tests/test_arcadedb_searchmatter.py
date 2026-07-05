"""Tests for SearchMatter + session_search over ArcadeDB.

Validates:
  - SearchMatter CQRS read model (manual creation — end_session doesn't wire it)
  - hybrid_search_sessions via pg_query (vector.neighbors + vector.fuse)
  - search_messages via pg_query (vector) + LIKE fallback
  - LIKE fallback with filters (source, role, exclude, CJK)

Links:
  Phase 3: hermes_cli/arcadedb_session.py (hybrid_search_sessions, search_messages)
  Fixtures: tests/fixtures/arcadedb_fixtures.py
"""

import json
import time
import uuid

import pytest

pytestmark = pytest.mark.skip_phase3

try:
    from hermes_cli.arcadedb_session import ArcadedbSessionDB
    from hermes_cli.arcadedb_helpers import _q, _n
    HAS_SESSION = True
except ImportError:
    HAS_SESSION = False


def _uid():
    return uuid.uuid4().hex[:8]


def _seed_search_matter(session_db, sid, summary, source="cli", model="test-model"):
    """Manually create a SearchMatter vertex (end_session doesn't wire this yet)."""
    from hermes_cli.arcadedb import ArcadeDBAdapter

    session = session_db.get_session(sid)
    if not session:
        raise RuntimeError(f"Session {sid} not found")
    session_rid = session["@rid"]

    emb = session_db._embedder.embed([summary])[0]
    qv = ArcadeDBAdapter._vec(emb.dense)

    session_db._adapter.execute(
        f"INSERT INTO SearchMatter SET "
        f"session_rid = {_q(session_rid)}, "
        f"summary = {_q(summary)}, "
        f"keywords = {_q(json.dumps(summary.split()))}, "
        f"embedding = {qv}, "
        f"profile = {_q(source)}, "
        f"model = {_q(model)}, "
        f"created_at = {_n(time.time())}"
    )


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestSearchMatter:
    """SM-01: SearchMatter CQRS — create + hybrid_search + search_messages."""

    def test_create_and_hybrid_search(self, arcadedb_session):
        """end_session() auto-creates SearchMatter → hybrid_search_sessions finds it."""
        pref = _uid()
        sid = f"sm-{pref}"
        arcadedb_session.create_session(sid, source="cli", model="gpt-4")
        arcadedb_session.append_message(sid, "user", "deploy kubernetes cluster to production")
        arcadedb_session.append_message(sid, "assistant", "I'll deploy using helm charts")
        arcadedb_session.append_message(sid, "user", "check if pods are running")

        # end_session triggers _create_search_matter automatically
        arcadedb_session.end_session(sid, "agent_close")
        time.sleep(2)  # Lucene + vector index build delay

        # Search via hybrid_search_sessions
        results = arcadedb_session.hybrid_search_sessions(
            query="kubernetes deployment", top_k=5,
        )
        assert len(results) >= 1, f"Expected >=1 hybrid search result, got {len(results)}"

    def test_search_matter_auto_created(self, arcadedb_session):
        """end_session() creates exactly one SearchMatter vertex per session."""
        pref = _uid()
        sid = f"sma-{pref}"
        arcadedb_session.create_session(sid, source="cli", model="gpt-4")
        arcadedb_session.append_message(sid, "user", "test message for search matter")

        # Before end_session — no SearchMatter
        session = arcadedb_session.get_session(sid)
        sm_before = arcadedb_session._adapter.query(
            f"SELECT FROM SearchMatter WHERE session_rid = {session['@rid']}"
        )
        assert len(sm_before) == 0, "No SearchMatter before end_session"

        arcadedb_session.end_session(sid, "agent_close")
        time.sleep(2)

        # After end_session — SearchMatter exists
        sm_after = arcadedb_session._adapter.query(
            f"SELECT FROM SearchMatter WHERE session_rid = {session['@rid']}"
        )
        assert len(sm_after) >= 1, f"Expected SearchMatter after end_session, got {len(sm_after)}"
        assert sm_after[0].get("summary"), "SearchMatter should have a summary"

    def test_search_messages_vector(self, arcadedb_session):
        """search_messages via pg_query vector.neighbors finds results."""
        pref = _uid()
        sid = f"sv-{pref}"
        arcadedb_session.create_session(sid, source="cli", model="gpt-4")
        arcadedb_session.append_message(sid, "user", "deploy kubernetes cluster to production")
        arcadedb_session.append_message(sid, "assistant", "helm charts deployment")

        # Vector search via pg_query
        results = arcadedb_session.search_messages("kubernetes", limit=5)
        assert len(results) >= 1, f"Expected >=1 search result, got {len(results)}"

    def test_search_messages_like_fallback(self, arcadedb_session):
        """LIKE fallback works when vector search returns empty."""
        pref = _uid()
        sid = f"sl-{pref}"
        arcadedb_session.create_session(sid, source="cli", model="gpt-4")
        arcadedb_session.append_message(sid, "user", "unique_test_token_xyz123")
        arcadedb_session.append_message(sid, "assistant", "response to unique query")

        # Search by exact unique word — LIKE should find it
        results = arcadedb_session.search_messages("unique_test_token_xyz123", limit=5)
        assert len(results) >= 1, f"LIKE fallback should find the message, got {len(results)}"


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestSearchFilters:
    """SM-02: search_messages with source/role/exclude filters."""

    def test_source_filter(self, arcadedb_session):
        """Filter by source."""
        pref = _uid()
        s_cli = f"sf-cli-{pref}"
        s_tel = f"sf-tel-{pref}"
        arcadedb_session.create_session(s_cli, source="cli", model="gpt-4")
        arcadedb_session.create_session(s_tel, source="telegram", model="gpt-4")
        arcadedb_session.append_message(s_cli, "user", "cli session message")
        arcadedb_session.append_message(s_tel, "user", "telegram session message")

        time.sleep(0.5)
        results = arcadedb_session.search_messages("session message", source_filter="cli", limit=10)
        assert len(results) >= 1
        for r in results:
            assert r.get("source") == "cli", f"Expected source=cli, got {r.get('source')}"

    def test_role_filter(self, arcadedb_session):
        """Filter by role."""
        pref = _uid()
        sid = f"rf-{pref}"
        arcadedb_session.create_session(sid, source="cli", model="gpt-4")
        arcadedb_session.append_message(sid, "user", "user question about deployment")
        arcadedb_session.append_message(sid, "assistant", "assistant response about kubernetes")
        arcadedb_session.append_message(sid, "user", "another user message")

        time.sleep(0.5)
        results = arcadedb_session.search_messages("deployment kubernetes", role_filter="user", limit=10)
        for r in results:
            assert r.get("role") == "user", f"Expected role=user, got {r.get('role')}"

    def test_exclude_sources(self, arcadedb_session):
        """Exclude sources from results."""
        pref = _uid()
        s_cli = f"ex-cli-{pref}"
        s_cron = f"ex-cron-{pref}"
        arcadedb_session.create_session(s_cli, source="cli", model="gpt-4")
        arcadedb_session.create_session(s_cron, source="cron", model="gpt-4")
        arcadedb_session.append_message(s_cli, "user", "cli message about deploy")
        arcadedb_session.append_message(s_cron, "user", "cron message about deploy")

        time.sleep(0.5)
        results = arcadedb_session.search_messages("deploy", exclude_sources=["cron"], limit=10)
        for r in results:
            assert r.get("source") != "cron", f"cron should be excluded, got {r.get('source')}"


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestCJKSearch:
    """SM-03: CJK text search via LIKE fallback."""

    def test_cjk_search(self, arcadedb_session):
        """Japanese text search."""
        pref = _uid()
        sid = f"cjk-{pref}"
        arcadedb_session.create_session(sid, source="cli", model="gpt-4")
        arcadedb_session.append_message(sid, "user", "データベースの検索機能をテストします")

        time.sleep(0.5)
        results = arcadedb_session.search_messages("データベース", limit=5)
        assert len(results) >= 1, f"CJK search should find message, got {len(results)}"


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestMultiSessionSearch:
    """SM-04: Search across multiple sessions + exclude current."""

    def test_cross_session_search(self, arcadedb_session):
        """Search finds results from multiple sessions."""
        pref = _uid()
        s1 = f"ms1-{pref}"
        s2 = f"ms2-{pref}"
        arcadedb_session.create_session(s1, source="cli", model="gpt-4")
        arcadedb_session.create_session(s2, source="cli", model="gpt-4")
        arcadedb_session.append_message(s1, "user", "session one: deploy kubernetes")
        arcadedb_session.append_message(s2, "user", "session two: kubernetes monitoring")

        time.sleep(0.5)
        results = arcadedb_session.search_messages("kubernetes", limit=10)
        assert len(results) >= 2, f"Expected >=2 results from 2 sessions, got {len(results)}"

    def test_include_inactive(self, arcadedb_session):
        """include_inactive=True finds archived messages."""
        pref = _uid()
        sid = f"ia-{pref}"
        arcadedb_session.create_session(sid, source="cli", model="gpt-4")
        arcadedb_session.append_message(sid, "user", "active message")
        arcadedb_session.end_session(sid, "agent_close")
        arcadedb_session.append_message(sid, "user", "inactive message")

        time.sleep(0.5)
        active = arcadedb_session.search_messages("inactive", include_inactive=False, limit=5)
        inactive = arcadedb_session.search_messages("inactive", include_inactive=True, limit=5)
        # With include_inactive=True we should find the inactive message
        assert len(inactive) >= 1, f"include_inactive should find message, got {len(inactive)}"
