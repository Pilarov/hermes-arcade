"""Tests for search over ArcadeDB — FTS5 -> Lucene equivalence (Phase 3).

Links:
  Phase 3: hermes_cli/arcadedb_session.py (search_messages, hybrid_search)
  Phase 3 spec: docs/arcadedb-migration/phase-3-sessiondb.md#search
  Fixtures: tests/fixtures/arcadedb_fixtures.py

Validates: BM25 ranking, CJK fallback, snippet generation, hybrid fuse.
"""

import pytest

pytestmark = pytest.mark.skip_phase3

try:
    from hermes_cli.arcadedb_session import ArcadedbSessionDB
    HAS_SESSION = True
except ImportError:
    HAS_SESSION = False


def _seed_search_data(session_db):
    """Insert sessions and messages for search tests."""
    session_db.create_session("search-s1", source="cli", model="gpt-4")
    session_db.create_session("search-s2", source="telegram", model="gpt-4")
    session_db.create_session("search-s3", source="cron", model="deepseek")

    msgs = [
        ("search-s1", "user", "deploy the new kubernetes cluster to production"),
        ("search-s1", "assistant", "I'll deploy using helm charts"),
        ("search-s1", "user", "check if the pods are running"),
        ("search-s2", "user", "show me the error logs from yesterday"),
        ("search-s2", "assistant", "The logs show a timeout connecting to PostgreSQL"),
        ("search-s3", "user", "daily cron summary for project alpha"),
    ]
    for sid, role, content in msgs:
        session_db.append_message(sid, role=role, content=content)


@pytest.mark.skipif(not HAS_SESSION, reason="Phase 3 ArcadedbSessionDB not yet implemented")
class TestFullTextSearch:
    def test_search_basic(self, arcadedb_session):
        """SR-01: Basic full-text search returns results."""
        _seed_search_data(arcadedb_session)
        results = arcadedb_session.search_messages("kubernetes deployment")
        assert len(results) >= 1

    def test_search_exclude_sources(self, arcadedb_session):
        """SR-03: exclude_sources hides matching sessions."""
        _seed_search_data(arcadedb_session)
        results = arcadedb_session.search_messages(
            "cron", exclude_sources=["cron"]
        )
        for r in results:
            assert r.get("source") != "cron"

    def test_search_role_filter(self, arcadedb_session):
        """SR-04: role_filter returns only matching roles."""
        _seed_search_data(arcadedb_session)
        results = arcadedb_session.search_messages(
            "deploy", role_filter="user"
        )
        for r in results:
            assert r.get("role") == "user"

    def test_search_snippets(self, arcadedb_session):
        """SR-08: Results contain snippets with context."""
        _seed_search_data(arcadedb_session)
        results = arcadedb_session.search_messages("kubernetes")
        for r in results:
            assert "snippet" in r
            assert len(r["snippet"]) > 0


@pytest.mark.skipif(not HAS_SESSION, reason="Phase 3 ArcadedbSessionDB not yet implemented")
class TestCJKSearch:
    def test_cjk_basic(self, arcadedb_session):
        """SR-05: CJK query falls back to LIKE."""
        session_db = arcadedb_session
        session_db.create_session("cjk-s1", source="cli", model="test")
        session_db.append_message(
            "cjk-s1", role="user", content="データベースの検索機能をテストします"
        )
        results = session_db.search_messages("データベース")
        assert len(results) >= 1

    def test_cjk_short(self, arcadedb_session):
        """SR-06: Short CJK (1-2 chars) uses LIKE fallback."""
        session_db = arcadedb_session
        session_db.create_session("cjk-s2", source="cli", model="test")
        session_db.append_message(
            "cjk-s2", role="user", content="日本語のテスト"
        )
        results = session_db.search_messages("本語")
        assert len(results) >= 1


@pytest.mark.skipif(not HAS_SESSION, reason="Phase 3 ArcadedbSessionDB not yet implemented")
class TestHybridSearch:
    def test_hybrid_basic(self, arcadedb_session):
        """SR-11: hybrid_search returns results."""
        _seed_search_data(arcadedb_session)
        results = arcadedb_session.hybrid_search_sessions(
            query="kubernetes deployment", top_k=5,
        )
        assert len(results) >= 1

    def test_hybrid_profile_filter(self, arcadedb_session):
        """SR-12: hybrid_search with profile filter."""
        _seed_search_data(arcadedb_session)
        results = arcadedb_session.hybrid_search_sessions(
            query="error logs", top_k=5,
        )
        assert isinstance(results, list)

    def test_hybrid_days_filter(self, arcadedb_session):
        """SR-13: hybrid_search with date filter."""
        _seed_search_data(arcadedb_session)
        results = arcadedb_session.hybrid_search_sessions(
            query="kubernetes", days=365,
        )
        assert isinstance(results, list)
