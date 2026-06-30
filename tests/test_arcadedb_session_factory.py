"""Tests for session factory (Phase 4).

Links:
  Phase 4: hermes_state.py (create_session_db factory)
  Phase 4 spec: docs/arcadedb-migration/phase-4-consumers.md
"""

import pytest


class TestSessionFactory:
    def test_sqlite_session_db_works(self, sqlite_session):
        """Existing SQLite SessionDB works without regressions."""
        sqlite_session.create_session("fact-s1", source="cli")
        s = sqlite_session.get_session("fact-s1")
        assert s is not None
        assert s["id"] == "fact-s1"

    def test_sqlite_append_message(self, sqlite_session):
        """SQLite append_message returns int ID."""
        sqlite_session.create_session("fact-s2", source="cli")
        msg_id = sqlite_session.append_message(
            "fact-s2", role="user", content="test message"
        )
        assert isinstance(msg_id, int)
        assert msg_id > 0

    def test_sqlite_search(self, sqlite_session):
        """SQLite search_messages returns results."""
        sqlite_session.create_session("fact-s3", source="cli")
        sqlite_session.append_message(
            "fact-s3", role="user", content="searchable unique text here"
        )
        results = sqlite_session.search_messages("unique text")
        assert len(results) >= 1

    def test_sqlite_get_messages_as_conversation(self, sqlite_session):
        """SQLite get_messages_as_conversation returns OpenAI format."""
        sqlite_session.create_session("fact-s4", source="cli")
        sqlite_session.append_message("fact-s4", role="user", content="hi")
        sqlite_session.append_message("fact-s4", role="assistant", content="hello")
        msgs = sqlite_session.get_messages_as_conversation("fact-s4")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"
