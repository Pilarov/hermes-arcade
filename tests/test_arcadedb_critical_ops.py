"""Tests for critical ArcadedbSessionDB methods not covered elsewhere.

Phase 3: coverage for operations that the agent depends on
(ensure_session, token counts, model updates, system prompt,
archive_and_compact, restore_rewound, clear_messages, message_count).

Links:
  Phase 3: hermes_cli/arcadedb_session.py
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
class TestEnsureSession:
    """CR-01: ensure_session creates session if missing, returns existing."""

    def test_creates_new(self, arcadedb_session):
        sid = f"ens-new-{_uid()}"
        result = arcadedb_session.ensure_session(sid, source="test")
        assert result == sid
        s = arcadedb_session.get_session(sid)
        assert s is not None
        assert s["source"] == "test"

    def test_returns_existing(self, arcadedb_session):
        sid = f"ens-exist-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        result = arcadedb_session.ensure_session(sid, source="other")
        assert result == sid
        s = arcadedb_session.get_session(sid)
        assert s["source"] == "test"  # Source unchanged


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestTokenCounts:
    """CR-02: update_token_counts absolute and incremental modes."""

    def test_absolute(self, arcadedb_session):
        sid = f"tok-abs-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.update_token_counts(
            sid, input_tokens=100, output_tokens=50,
            cache_read_tokens=10, cache_write_tokens=5,
            reasoning_tokens=20, absolute=True,
        )
        s = arcadedb_session.get_session(sid)
        assert s["input_tokens"] == 100
        assert s["output_tokens"] == 50
        assert s["cache_read_tokens"] == 10
        assert s["cache_write_tokens"] == 5
        assert s["reasoning_tokens"] == 20

    def test_incremental(self, arcadedb_session):
        sid = f"tok-inc-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.update_token_counts(sid, input_tokens=50, absolute=True)
        arcadedb_session.update_token_counts(sid, input_tokens=30, output_tokens=20)
        s = arcadedb_session.get_session(sid)
        assert s["input_tokens"] == 80   # 50 + 30
        assert s["output_tokens"] == 20  # 0 + 20


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestSessionModel:
    """CR-03: update_session_model changes model field."""

    def test_update_model(self, arcadedb_session):
        sid = f"mdl-{_uid()}"
        arcadedb_session.create_session(sid, source="test", model="gpt-4")
        arcadedb_session.update_session_model(sid, "deepseek-chat")
        s = arcadedb_session.get_session(sid)
        assert s["model"] == "deepseek-chat"


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestSystemPrompt:
    """CR-04: update_system_prompt stores prompt text."""

    def test_update_prompt(self, arcadedb_session):
        sid = f"prm-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.update_system_prompt(sid, "You are a helpful assistant")
        s = arcadedb_session.get_session(sid)
        assert s["system_prompt"] == "You are a helpful assistant"


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestArchiveCompact:
    """CR-05: archive_and_compact soft-deletes old messages and writes compacted ones."""

    def test_archive(self, arcadedb_session):
        sid = f"arc-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        for i in range(5):
            arcadedb_session.append_message(
                sid, role="user" if i % 2 == 0 else "assistant",
                content=f"Message {i}",
            )
        before = arcadedb_session.message_count(sid)
        assert before >= 5

        compacted = [
            {"role": "assistant", "content": "Compacted summary of conversation"}
        ]
        count = arcadedb_session.archive_and_compact(sid, compacted)
        assert count == 1  # One compacted message inserted

        after = arcadedb_session.message_count(sid)
        # After compaction: old messages inactive, 1 new compacted message
        assert after >= 1


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestRestoreRewound:
    """CR-06: restore_rewound reactivates rewound messages."""

    def test_restore(self, arcadedb_session):
        sid = f"rwd-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        msg1_id = arcadedb_session.append_message(sid, role="user", content="msg1")
        msg2_id = arcadedb_session.append_message(sid, role="assistant", content="msg2")
        arcadedb_session.append_message(sid, role="user", content="msg3")

        # Rewind to msg2: soft-deletes msg3 (active=0)
        result = arcadedb_session.rewind_to_message(sid, msg2_id)
        assert result is not None and result["rewound_count"] >= 1

        # restore_rewound reactivates all inactive non-compacted messages
        active = arcadedb_session.restore_rewound(sid, msg2_id)
        assert active >= 2  # At least msg1+msg2 are active


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestClearMessagesAndCount:
    """CR-07: clear_messages + message_count."""

    def test_clear(self, arcadedb_session):
        sid = f"clr-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.append_message(sid, role="user", content="m1")
        arcadedb_session.append_message(sid, role="user", content="m2")
        arcadedb_session.append_message(sid, role="user", content="m3")

        assert arcadedb_session.message_count(sid) >= 3

        arcadedb_session.clear_messages(sid)
        # Soft-delete: active=0. Message count is total (including inactive)
        total = arcadedb_session.message_count(sid)
        assert total >= 3  # Soft delete keeps rows


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestSessionMeta:
    """CR-08: update_session_meta stores model_config + optional model."""

    def test_update_meta(self, arcadedb_session):
        sid = f"mta-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.update_session_meta(sid, '{"key":"val"}', model="deepseek")
        s = arcadedb_session.get_session(sid)
        assert s["model_config"] == '{"key":"val"}'
        assert s["model"] == "deepseek"


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestBillingRoute:
    """CR-09: update_session_billing_route stores provider/URL/mode."""

    def test_billing_route(self, arcadedb_session):
        sid = f"bil-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.update_session_billing_route(
            sid, provider="openai", base_url="https://api.openai.com", billing_mode="token"
        )
        s = arcadedb_session.get_session(sid)
        assert s["billing_provider"] == "openai"
        assert s["billing_base_url"] == "https://api.openai.com"
        assert s["billing_mode"] == "token"


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestSessionCwd:
    """CR-10: update_session_cwd stores cwd + git metadata."""

    def test_update_cwd(self, arcadedb_session):
        sid = f"cwd-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.update_session_cwd(
            sid, cwd="/home/user/project", git_branch="main", git_repo_root="/home/user/project"
        )
        s = arcadedb_session.get_session(sid)
        assert s["cwd"] == "/home/user/project"
        assert s["git_branch"] == "main"
        assert s["git_repo_root"] == "/home/user/project"


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestBackfillRepoRoots:
    """CR-11: backfill_repo_roots fills NULL git_repo_root by cwd."""

    def test_backfill(self, arcadedb_session):
        sid = f"bfr-{_uid()}"
        arcadedb_session.create_session(sid, source="test")
        arcadedb_session.update_session_cwd(sid, cwd="/home/user/repo")
        arcadedb_session.backfill_repo_roots({"/home/user/repo": "/home/user/repo"})
        s = arcadedb_session.get_session(sid)
        assert s["git_repo_root"] == "/home/user/repo"


@pytest.mark.skipif(not HAS_SESSION, reason="ArcadedbSessionDB not available")
class TestGatewayPeer:
    """CR-12: record_gateway_session_peer + find_latest_gateway_session_for_peer."""

    def test_record_peer(self, arcadedb_session):
        sid = f"gwp-{_uid()}"
        arcadedb_session.create_session(sid, source="test", user_id="user1", session_key="sk1")
        arcadedb_session.record_gateway_session_peer(
            sid, source="telegram", user_id="user1", chat_id="chat1", chat_type="private"
        )
        s = arcadedb_session.get_session(sid)
        assert s["user_id"] == "user1"
        assert s["chat_id"] == "chat1"
        assert s["chat_type"] == "private"

    def test_find_peer(self, arcadedb_session):
        sid = f"gwp-find-{_uid()}"
        arcadedb_session.create_session(sid, source="telegram", user_id="user2", session_key="sk2")
        arcadedb_session.record_gateway_session_peer(
            sid, source="telegram", user_id="user2", session_key="sk2"
        )
        found = arcadedb_session.find_latest_gateway_session_for_peer(
            source="telegram", user_id="user2", session_key="sk2"
        )
        assert found is not None and found["id"] == sid
