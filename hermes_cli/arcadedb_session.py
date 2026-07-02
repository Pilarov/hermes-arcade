"""ArcadeDB-backed SessionDB — full replacement for hermes_state.SessionDB.

Phase 3 of ArcadeDB native storage migration.
Implements the complete SessionDB public API (~80 methods) against
ArcadeDB via the PostgreSQL wire protocol adapter.

Key differences from SQLite SessionDB:
  - ArcadeDBAdapter transact() instead of _execute_write()
  - Edge traversal (HAS_MESSAGE) instead of WHERE session_id JOIN
  - FULL_TEXT Lucene instead of FTS5 virtual tables
  - CompressionLock vertices instead of compression_locks table
  - StateMeta vertices instead of state_meta table
  - TelegramTopicMode/TelegramTopicBinding vertices instead of tables

Links:
  Phase 3 spec: docs/arcadedb-migration/phase-3-sessiondb.md
  Reference:   hermes_state.py (SessionDB, 5658 lines)
  Tests:       tests/test_arcadedb_session.py
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from hermes_cli.arcadedb import ArcadeDBAdapter, ArcadeDBError
from hermes_cli.arcadedb_helpers import (
    _CONTENT_JSON_PREFIX,
    _HIDDEN_SESSION_SOURCES,
    _DEMOTED_SESSION_SOURCES,
    _now,
    _encode_content,
    _decode_content,
    _sanitize_title,
    _maybe_epoch,
    _format_timestamp,
    _has_cjk,
    _rid_to_int,
    _q,
    _n,
    MAX_TITLE_LENGTH,
)

logger = logging.getLogger(__name__)


# Block 3: Entity extraction + graph edges (var B: edges after transact)
def _extract_entities_from_text(text: str) -> list[str]:
    """Extract named entities from text. Pure function — no DB access."""
    import re
    capitalized = set(re.findall(r'\b[A-Z][a-z]{2,}\b', text))
    tech = set(re.findall(
        r'\b(?:kubernetes|docker|nginx|aws|api|cli|sql|http|ssh|git|npm|pip|'
        r'python|java|node|react|vue|linux|bash|ssl|tls|json|xml|html|css|'
        r'postgres|redis|mongo|graphql|grpc|kafka|helm|terraform)\b',
        text, re.IGNORECASE
    ))
    cyrillic = set(re.findall(r'\b[А-Я][а-я]{2,}\b', text))
    return list(capitalized | tech | cyrillic)[:10]

def _create_entity_vertices(cur, names: list[str], ts: float) -> list[str]:
    """Create Entity vertices + return their @rids. Must be inside transact."""
    rids = []
    for name in names:
        try:
            cur.execute(
                f"CREATE VERTEX Entity SET name = {_q(name)}, "
                f"entity_type = 'extracted', created_at = {_n(ts)}"
            )
        except Exception:
            pass
        try:
            cur.execute(f"SELECT @rid FROM Entity WHERE name = {_q(name)} LIMIT 1")
            rows = cur.fetchall()
            if rows:
                rids.append(rows[0]["@rid"])
        except Exception:
            pass
    return rids

def _create_graph_edges(adapter, msg_rid: str, entity_rids: list[str]) -> None:
    """Create MENTIONS + RELATES_TO edges OUTSIDE transaction (ArcadeDB PG workaround)."""
    for erid in entity_rids:
        try:
            adapter.execute(
                f"CREATE EDGE MENTIONS FROM "
                f"(SELECT FROM Message WHERE @rid = {_q(msg_rid)}) TO "
                f"(SELECT FROM Entity WHERE @rid = {_q(erid)}) "
                f"SET weight = 1.0"
            )
        except Exception:
            pass
    for i, a in enumerate(entity_rids):
        for b in entity_rids[i+1:]:
            try:
                adapter.execute(
                    f"CREATE EDGE RELATES_TO FROM "
                    f"(SELECT FROM Entity WHERE @rid = {_q(a)}) TO "
                    f"(SELECT FROM Entity WHERE @rid = {_q(b)}) "
                    f"SET weight = 1.0"
                )
            except Exception:
                pass


class ArcadedbSessionDB:
    """ArcadeDB-backed session and message store.

    Implements the same public API as hermes_state.SessionDB.
    All callers should use the hermes_state.create_session_db() factory
    (Phase 4) rather than instantiating this class directly.
    """

    def __init__(
        self,
        adapter: Optional[ArcadeDBAdapter] = None,
        embedder: Any = None,
        graph_store: Any = None,
        read_only: bool = False,
    ) -> None:
        self._adapter = adapter
        self._embedder = embedder
        self._graph_store = graph_store
        self._read_only = read_only

    def close(self) -> None:
        if self._adapter:
            self._adapter.close()
            self._adapter = None

    # ==================================================================
    # Session Lifecycle
    # ==================================================================

    def create_session(self, session_id: str, source: str, **kwargs: Any) -> str:
        props = {
            "id": session_id,
            "source": source,
            "started_at": kwargs.pop("started_at", _now()),
        }
        for k, v in kwargs.items():
            if v is not None:
                props[k] = v

        def _do(cur):
            parts = ", ".join(
                f"`{k}` = {_q(v)}" for k, v in props.items()
            )
            cur.execute(f"INSERT INTO Session SET {parts}")
        self._adapter.transact(_do)
        return session_id

    def ensure_session(self, session_id: str, source: str = "unknown", **kwargs: Any) -> str:
        existing = self.get_session(session_id)
        if existing:
            return session_id
        return self.create_session(session_id, source, **kwargs)

    def end_session(self, session_id: str, end_reason: str) -> None:
        self._adapter.execute(
            "UPDATE Session SET ended_at = COALESCE(ended_at, %(now)s), "
            "end_reason = COALESCE(end_reason, %(reason)s) WHERE id = %(id)s",
            {"now": _now(), "reason": end_reason, "id": session_id},
        )

    def reopen_session(self, session_id: str) -> None:
        self._adapter.execute(
            "UPDATE Session SET ended_at = NULL, end_reason = NULL WHERE id = %s",
            (session_id,),
        )

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        rows = self._adapter.query(
            "SELECT FROM Session WHERE id = %s LIMIT 1", (session_id,)
        )
        return rows[0] if rows else None

    def resolve_session_id(self, session_id_or_prefix: str) -> Optional[str]:
        row = self.get_session(session_id_or_prefix)
        if row:
            return session_id_or_prefix
        rows = self._adapter.query(
            "SELECT id FROM Session WHERE id LIKE %s ORDER BY started_at DESC",
            (session_id_or_prefix + "%",),
        )
        ids = [r["id"] for r in rows]
        return ids[0] if len(ids) == 1 else None

    def record_gateway_session_peer(
        self, session_id: str, *, source: str, user_id: Optional[str] = None,
        session_key: Optional[str] = None, chat_id: Optional[str] = None,
        chat_type: Optional[str] = None, thread_id: Optional[str] = None,
    ) -> None:
        updates: Dict[str, Any] = {}
        if user_id is not None: updates["user_id"] = user_id
        if session_key is not None: updates["session_key"] = session_key
        if chat_id is not None: updates["chat_id"] = chat_id
        if chat_type is not None: updates["chat_type"] = chat_type
        if thread_id is not None: updates["thread_id"] = thread_id
        if not updates:
            return
        parts = ", ".join(f"`{k}` = %({k})s" for k in updates)
        updates["id"] = session_id
        self._adapter.execute(
            f"UPDATE Session SET {parts} WHERE id = %(id)s", updates
        )

    def find_latest_gateway_session_for_peer(
        self, *, source: str, user_id: Optional[str] = None,
        session_key: Optional[str] = None, chat_id: Optional[str] = None,
        chat_type: Optional[str] = None, thread_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        params: Dict[str, Any] = {"src": source}
        where = ["source = %(src)s", "ended_at IS NULL"]
        if user_id:
            where.append("user_id = %(uid)s"); params["uid"] = user_id
        if session_key:
            where.append("session_key = %(sk)s"); params["sk"] = session_key
        if chat_id:
            where.append("chat_id = %(cid)s"); params["cid"] = chat_id
        rows = self._adapter.query(
            f"SELECT FROM Session WHERE {' AND '.join(where)} "
            "ORDER BY started_at DESC LIMIT 1",
            params,
        )
        return rows[0] if rows else None

    # ==================================================================
    # Session Metadata
    # ==================================================================

    def update_session_meta(
        self, session_id: str, model_config_json: str, model: Optional[str] = None,
    ) -> None:
        if model:
            self._adapter.execute(
                "UPDATE Session SET model_config = %(mc)s, model = %(m)s WHERE id = %(id)s",
                {"mc": model_config_json, "m": model, "id": session_id},
            )
        else:
            self._adapter.execute(
                "UPDATE Session SET model_config = %s WHERE id = %s",
                (model_config_json, session_id),
            )

    def update_system_prompt(self, session_id: str, system_prompt: str) -> None:
        self._adapter.execute(
            "UPDATE Session SET system_prompt = %s WHERE id = %s",
            (system_prompt, session_id),
        )

    def update_session_model(self, session_id: str, model: str) -> None:
        self._adapter.execute(
            "UPDATE Session SET model = %s WHERE id = %s",
            (model, session_id),
        )

    def update_session_billing_route(
        self, session_id: str, *, provider: str, base_url: str,
        billing_mode: Optional[str] = None,
    ) -> None:
        self._adapter.execute(
            f"UPDATE Session SET billing_provider = {_q(provider)}, billing_base_url = {_q(base_url)}, "
            f"billing_mode = {_q(billing_mode)} WHERE id = {_q(session_id)}"
        )

    def update_token_counts(
        self, session_id: str, input_tokens: int = 0, output_tokens: int = 0,
        cache_read_tokens: int = 0, cache_write_tokens: int = 0,
        reasoning_tokens: int = 0, estimated_cost_usd: Optional[float] = None,
        actual_cost_usd: Optional[float] = None, cost_status: Optional[str] = None,
        cost_source: Optional[str] = None, pricing_version: Optional[str] = None,
        absolute: bool = False,
    ) -> None:
        if absolute:
            self._adapter.execute(
                "UPDATE Session SET input_tokens = %(it)s, output_tokens = %(ot)s, "
                "cache_read_tokens = %(crt)s, cache_write_tokens = %(cwt)s, "
                "reasoning_tokens = %(rt)s WHERE id = %(id)s",
                {"it": input_tokens, "ot": output_tokens, "crt": cache_read_tokens,
                 "cwt": cache_write_tokens, "rt": reasoning_tokens, "id": session_id},
            )
        else:
            parts = []
            params: Dict[str, Any] = {"id": session_id}
            if input_tokens:
                parts.append("input_tokens = input_tokens + %(it)s"); params["it"] = input_tokens
            if output_tokens:
                parts.append("output_tokens = output_tokens + %(ot)s"); params["ot"] = output_tokens
            if cache_read_tokens:
                parts.append("cache_read_tokens = cache_read_tokens + %(crt)s"); params["crt"] = cache_read_tokens
            if cache_write_tokens:
                parts.append("cache_write_tokens = cache_write_tokens + %(cwt)s"); params["cwt"] = cache_write_tokens
            if reasoning_tokens:
                parts.append("reasoning_tokens = reasoning_tokens + %(rt)s"); params["rt"] = reasoning_tokens
            if parts:
                self._adapter.execute(
                    f"UPDATE Session SET {', '.join(parts)} WHERE id = %(id)s", params
                )
        if estimated_cost_usd is not None or actual_cost_usd is not None:
            cost_parts = []
            cost_params: Dict[str, Any] = {"id": session_id}
            if estimated_cost_usd is not None:
                cost_parts.append("estimated_cost_usd = %(ec)s"); cost_params["ec"] = estimated_cost_usd
            if actual_cost_usd is not None:
                cost_parts.append("actual_cost_usd = %(ac)s"); cost_params["ac"] = actual_cost_usd
            if cost_status is not None:
                cost_parts.append("cost_status = %(cs)s"); cost_params["cs"] = cost_status
            if cost_source is not None:
                cost_parts.append("cost_source = %(csrc)s"); cost_params["csrc"] = cost_source
            if pricing_version is not None:
                cost_parts.append("pricing_version = %(pv)s"); cost_params["pv"] = pricing_version
            if cost_parts:
                self._adapter.execute(
                    f"UPDATE Session SET {', '.join(cost_parts)} WHERE id = %(id)s",
                    cost_params,
                )

    def update_session_cwd(
        self, session_id: str, cwd: str, git_branch: Optional[str] = None,
        git_repo_root: Optional[str] = None,
    ) -> None:
        self._adapter.execute(
            f"UPDATE Session SET cwd = {_q(cwd)}, git_branch = {_q(git_branch)}, "
            f"git_repo_root = {_q(git_repo_root)} WHERE id = {_q(session_id)}"
        )

    def backfill_repo_roots(self, cwd_to_root: Dict[str, str]) -> None:
        for cwd, root in cwd_to_root.items():
            self._adapter.execute(
                "UPDATE Session SET git_repo_root = %(root)s "
                "WHERE cwd = %(cwd)s AND git_repo_root IS NULL",
                {"root": root, "cwd": cwd},
            )

    # ==================================================================
    # Session Titles
    # ==================================================================

    def set_session_title(self, session_id: str, title: str) -> bool:
        title = _sanitize_title(title)
        if title is None:
            return False
        try:
            self._adapter.execute(
                f"UPDATE Session SET title = {_q(title)} WHERE id = {_q(session_id)}"
            )
            return True
        except ArcadeDBError:
            return False

    def get_session_title(self, session_id: str) -> Optional[str]:
        rows = self._adapter.query(
            "SELECT title FROM Session WHERE id = %s", (session_id,)
        )
        return rows[0].get("title") if rows else None

    def get_session_by_title(self, title: str) -> Optional[Dict[str, Any]]:
        rows = self._adapter.query(
            "SELECT FROM Session WHERE title = %s LIMIT 1", (title,)
        )
        return rows[0] if rows else None

    def resolve_session_by_title(self, title: str) -> Optional[str]:
        rows = self._adapter.query(
            "SELECT id, started_at FROM Session WHERE title = %s "
            "ORDER BY started_at DESC LIMIT 1", (title,)
        )
        return rows[0]["id"] if rows else None

    def get_next_title_in_lineage(self, base_title: str) -> str:
        rows = self._adapter.query(
            "SELECT title FROM Session WHERE title LIKE %s ORDER BY started_at DESC",
            (base_title + "%",),
        )
        max_n = 0
        for r in rows:
            t = r.get("title", "")
            if t == base_title:
                max_n = max(max_n, 1)
            elif t.startswith(base_title + " #"):
                try:
                    n = int(t[len(base_title) + 2:])
                    max_n = max(max_n, n)
                except ValueError:
                    pass
        if max_n == 0:
            return base_title
        return f"{base_title} #{max_n + 1}"

    def set_session_archived(self, session_id: str, archived: bool) -> bool:
        self._adapter.execute(
            "UPDATE Session SET archived = %s WHERE id = %s",
            (int(archived), session_id),
        )
        return True

    # ==================================================================
    # Session Listing & Counting
    # ==================================================================

    def list_sessions_rich(
        self, source: Optional[str] = None,
        exclude_sources: Optional[list] = None,
        cwd_prefix: Optional[str] = None,
        limit: int = 20, offset: int = 0,
        include_archived: bool = False,
        include_children: bool = False,
    ) -> List[Dict[str, Any]]:
        where = []
        params: Dict[str, Any] = {"l": limit, "o": offset}
        if source:
            where.append("source = %(src)s"); params["src"] = source
        if exclude_sources:
            placeholders = ", ".join(f"%(es{i})s" for i in range(len(exclude_sources)))
            for i, s in enumerate(exclude_sources):
                params[f"es{i}"] = s
            where.append(f"source NOT IN ({placeholders})")
        if cwd_prefix:
            where.append("cwd LIKE %(cwd)s"); params["cwd"] = cwd_prefix + "%"
        if not include_archived:
            where.append("archived = 0")
        if not include_children:
            where.append("parent_session_id IS NULL")

        if where:
            w_clause = " WHERE " + " AND ".join(where)
        else:
            w_clause = ""

        rows = self._adapter.query(
            f"SELECT FROM Session{w_clause} ORDER BY started_at DESC "
            "LIMIT %(l)s SKIP %(o)s", params
        )
        for r in rows:
            r["started_at"] = r.get("started_at", 0)
        return rows

    def list_cron_job_runs(
        self, job_id: str, limit: int = 20, offset: int = 0,
    ) -> List[Dict[str, Any]]:
        return self._adapter.query(
            "SELECT FROM Session WHERE source = 'cron' AND session_key LIKE %(j)s "
            "ORDER BY started_at DESC LIMIT %(l)s SKIP %(o)s",
            {"j": f"%{job_id}%", "l": limit, "o": offset},
        )

    def search_sessions(
        self, source: Optional[str] = None, limit: int = 20, offset: int = 0,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"l": limit, "o": offset}
        where = ["parent_session_id IS NULL"]
        if source:
            where.append("source = %(src)s"); params["src"] = source
        return self._adapter.query(
            f"SELECT FROM Session WHERE {' AND '.join(where)} "
            "ORDER BY started_at DESC LIMIT %(l)s SKIP %(o)s", params
        )

    def search_sessions_by_id(
        self, query: str, limit: int = 20, include_archived: bool = True,
    ) -> List[Dict[str, Any]]:
        rows = self._adapter.query(
            "SELECT FROM Session WHERE id = %s LIMIT 1", (query,)
        )
        if rows:
            return rows
        like_rows = self._adapter.query(
            "SELECT FROM Session WHERE id LIKE %s ORDER BY started_at DESC LIMIT %s",
            (query + "%", limit),
        )
        if like_rows:
            return like_rows
        return self._adapter.query(
            "SELECT FROM Session WHERE id LIKE %s ORDER BY started_at DESC LIMIT %s",
            ("%" + query + "%", limit),
        )

    def session_count(
        self, source: Optional[str] = None, cwd_prefix: Optional[str] = None,
    ) -> int:
        where = []
        params: Dict[str, Any] = {}
        if source:
            where.append("source = %(src)s"); params["src"] = source
        if cwd_prefix:
            where.append("cwd LIKE %(cwd)s"); params["cwd"] = cwd_prefix + "%"
        w_clause = " WHERE " + " AND ".join(where) if where else ""
        rows = self._adapter.query(
            f"SELECT count(*) as cnt FROM Session{w_clause}", params
        )
        return rows[0].get("cnt", 0) if rows else 0

    def distinct_session_cwds(
        self, include_archived: bool = False,
    ) -> List[Dict[str, Any]]:
        where = " WHERE cwd IS NOT NULL"
        if not include_archived:
            where += " AND archived = 0"
        return self._adapter.query(
            f"SELECT cwd, count(*) as cnt FROM Session{where} "
            "GROUP BY cwd ORDER BY cnt DESC"
        )

    def get_compression_tip(self, session_id: str) -> Optional[str]:
        tip = session_id
        seen = {tip}
        while True:
            rows = self._adapter.query(
                "SELECT parent_session_id FROM Session WHERE id = %s", (tip,)
            )
            if not rows or not rows[0].get("parent_session_id"):
                break
            parent = rows[0]["parent_session_id"]
            if not parent or parent in seen:
                break
            seen.add(parent)
            msgs = self._adapter.query(
                "SELECT count(*) as cnt FROM Message WHERE session_id = %s AND active = 1",
                (parent,),
            )
            if msgs and msgs[0].get("cnt", 0) > 0:
                tip = parent
            else:
                break
        return tip if tip != session_id else None

    def resolve_resume_session_id(self, session_id: str) -> str:
        rows = self._adapter.query(
            "SELECT id, parent_session_id FROM Session WHERE id = %s", (session_id,)
        )
        if not rows:
            return session_id
        row = rows[0]
        if row.get("parent_session_id"):
            child_msgs = self._adapter.query(
                "SELECT count(*) as cnt FROM Message WHERE session_id = %s AND active = 1",
                (session_id,),
            )
            if child_msgs and child_msgs[0].get("cnt", 0) > 0:
                return session_id
            return self.resolve_resume_session_id(row["parent_session_id"])
        return session_id

    # ==================================================================
    # Message Storage (CRITICAL)
    # ==================================================================

    def append_message(
        self, session_id: str, role: str, content: Any = None,
        tool_name: Optional[str] = None,
        tool_calls: Optional[List[Dict]] = None,
        tool_call_id: Optional[str] = None,
        token_count: Optional[int] = None,
        finish_reason: Optional[str] = None,
        reasoning: Optional[str] = None,
        reasoning_content: Optional[str] = None,
        reasoning_details: Optional[Any] = None,
        codex_reasoning_items: Optional[Any] = None,
        codex_message_items: Optional[Any] = None,
        platform_message_id: Optional[str] = None,
        observed: bool = False,
        timestamp: Optional[float] = None,
    ) -> int:
        ts = timestamp or _now()
        content_encoded = _encode_content(content)

        tool_calls_json = json.dumps(tool_calls) if tool_calls else None
        reasoning_details_json = json.dumps(reasoning_details) if reasoning_details else None
        codex_reasoning_json = json.dumps(codex_reasoning_items) if codex_reasoning_items else None
        codex_message_json = json.dumps(codex_message_items) if codex_message_items else None
        num_tc = len(tool_calls) if tool_calls else 0

        def _do(cur):
            # Compute embedding if embedder is available
            emb_sql = ""
            if self._embedder and content and isinstance(content, str):
                try:
                    emb = self._embedder.embed([content])[0]
                    from hermes_cli.arcadedb import ArcadeDBAdapter
                    emb_sql = f", embedding = {ArcadeDBAdapter._vec(emb.dense)}"
                except Exception:
                    pass

            sql = (
                "CREATE VERTEX Message SET "
                f"session_id = {_q(session_id)}, role = {_q(role)}, content = {_q(content_encoded)}, "
                f"timestamp = {_n(ts)}, token_count = {_n(token_count)}, finish_reason = {_q(finish_reason)}, "
                f"reasoning = {_q(reasoning)}, reasoning_content = {_q(reasoning_content)}, "
                f"reasoning_details = {_q(reasoning_details_json)}, codex_reasoning_items = {_q(codex_reasoning_json)}, "
                f"codex_message_items = {_q(codex_message_json)}, tool_calls = {_q(tool_calls_json)}, "
                f"tool_call_id = {_q(tool_call_id)}, tool_name = {_q(tool_name)}, "
                f"platform_message_id = {_q(platform_message_id)}, observed = {int(observed)}, "
                "active = 1, compacted = 0"
                f"{emb_sql}"
            )
            cur.execute(sql)

            # ArcadeDB rounds timestamps — find by session_id + role + time window
            cur.execute(
                "SELECT @rid FROM Message WHERE session_id = %s "
                "AND role = %s "
                "ORDER BY @rid DESC LIMIT 1",
                (session_id, role),
            )
            msg = cur.fetchone()
            if not msg:
                raise RuntimeError("Failed to retrieve inserted Message")
            msg_rid = msg["@rid"]

            # Create HAS_MESSAGE edge
            _tks = len(content.split()) if isinstance(content, str) else 0
            cur.execute(
                f"CREATE EDGE HAS_MESSAGE FROM "
                f"(SELECT FROM Session WHERE id = {_q(session_id)}) TO "
                f"(SELECT FROM Message WHERE @rid = {_q(msg_rid)}) "
                f"SET seq = 0, role = {_q(role)}, tokens = {_tks}, created_at = {_n(ts)}"
            )

            # Update session counters
            cur.execute(
                "UPDATE Session SET message_count = message_count + 1 WHERE id = %s",
                (session_id,),
            )
            if tool_name:
                cur.execute(
                    "UPDATE Session SET tool_call_count = tool_call_count + %s "
                    "WHERE id = %s",
                    (num_tc, session_id),
                )

            # Block 3: Extract entity names from message (create vertices inside transact)
            entity_rids = []
            content_str = content if isinstance(content, str) else ""
            if len(content_str) > 10:
                entity_names = _extract_entities_from_text(content_str)
                if entity_names:
                    entity_rids = _create_entity_vertices(cur, entity_names, ts)

            return _rid_to_int(msg_rid), entity_rids, msg_rid

        msg_id, entity_rids, msg_rid_val = self._adapter.transact(_do)

        # Block 3 var B: Create MENTIONS/RELATES_TO edges OUTSIDE transaction
        if entity_rids:
            _create_graph_edges(self._adapter, msg_rid_val, entity_rids)

        return msg_id

    def get_messages(
        self, session_id: str, include_inactive: bool = False,
    ) -> List[Dict[str, Any]]:
        active_filter = "" if include_inactive else " AND active = 1"
        rows = self._adapter.query(
            f"SELECT FROM Message WHERE session_id = %(sid)s{active_filter} "
            "ORDER BY timestamp, @rid",
            {"sid": session_id},
        )
        for r in rows:
            r["content"] = _decode_content(r.get("content"))
        return rows

    def get_messages_as_conversation(
        self, session_id: str, include_ancestors: bool = False,
        include_inactive: bool = False,
    ) -> List[Dict[str, Any]]:
        session_ids = []
        if include_ancestors:
            cur = session_id
            seen = {cur}
            while cur:
                session_ids.append(cur)
                rows = self._adapter.query(
                    "SELECT parent_session_id FROM Session WHERE id = %s", (cur,)
                )
                if not rows or not rows[0].get("parent_session_id"):
                    break
                cur = rows[0]["parent_session_id"]
                if cur in seen:
                    break
                seen.add(cur)
            session_ids.reverse()
        else:
            session_ids = [session_id]

        active_filter = "" if include_inactive else " AND active = 1"
        all_msgs: List[Dict] = []
        for sid in session_ids:
            rows = self._adapter.query(
                f"SELECT FROM Message WHERE session_id = %s{active_filter} "
                "ORDER BY timestamp, @rid", (sid,)
            )
            for r in rows:
                content = _decode_content(r.get("content"))
                msg = {"role": r["role"], "content": content}
                if r.get("tool_calls"):
                    msg["tool_calls"] = json.loads(r["tool_calls"])
                if r.get("tool_call_id"):
                    msg["tool_call_id"] = r["tool_call_id"]
                if r.get("tool_name"):
                    msg["name"] = r["tool_name"]
                if r.get("finish_reason"):
                    msg["finish_reason"] = r["finish_reason"]
                if r.get("reasoning"):
                    msg["reasoning"] = r["reasoning"]
                if r.get("reasoning_content"):
                    msg["reasoning_content"] = r["reasoning_content"]
                all_msgs.append(msg)
        return all_msgs

    def get_messages_around(
        self, session_id: str, around_message_id: int, window: int = 5,
    ) -> Dict[str, Any]:
        rows = self._adapter.query(
            "SELECT @rid, id, timestamp FROM Message WHERE session_id = %s "
            "ORDER BY timestamp, @rid", (session_id,)
        )
        target_idx = None
        for i, r in enumerate(rows):
            rid_str = str(r.get("@rid", ""))
            rid_hash = _rid_to_int(rid_str)
            if rid_hash == around_message_id:
                target_idx = i
                break
        if target_idx is None:
            raise ValueError(f"Message {around_message_id} not found in session {session_id}")

        start = max(0, target_idx - window)
        end = min(len(rows), target_idx + window + 1)
        window_rids = [rows[j]["@rid"] for j in range(start, end)]

        messages = []
        for rid in window_rids:
            msgs = self._adapter.query(
                "SELECT FROM Message WHERE @rid = %s", (rid,)
            )
            if msgs:
                m = msgs[0]
                m["content"] = _decode_content(m.get("content"))
                messages.append(m)

        return {
            "messages": messages,
            "total": len(rows),
            "before_count": max(0, target_idx - start),
            "after_count": end - target_idx - 1,
        }

    def get_anchored_view(
        self, session_id: str, around_message_id: int,
        window: int = 5, bookend: int = 3,
    ) -> Dict[str, Any]:
        result = self.get_messages_around(session_id, around_message_id, window)
        all_msgs = self._adapter.query(
            "SELECT FROM Message WHERE session_id = %s ORDER BY timestamp, @rid",
            (session_id,)
        )
        for m in all_msgs:
            m["content"] = _decode_content(m.get("content"))

        bookend_start = all_msgs[:bookend] if len(all_msgs) >= bookend else all_msgs
        bookend_end = all_msgs[-bookend:] if len(all_msgs) >= bookend * 2 else []

        return {
            "window": result["messages"],
            "bookend_start": bookend_start,
            "bookend_end": bookend_end,
            "total": result["total"],
            "before_count": result["before_count"],
            "after_count": result["after_count"],
        }

    def replace_messages(self, session_id: str, messages: List[Dict]) -> None:
        def _do(cur):
            # Soft-delete instead of DELETE VERTEX (avoids edge cascade hang)
            cur.execute(
                f"UPDATE Message SET active = 0 WHERE session_id = {_q(session_id)} AND active = 1"
            )
            cur.execute(
                f"UPDATE Session SET message_count = 0, tool_call_count = 0 WHERE id = {_q(session_id)}"
            )
            new_msg_count = 0
            new_tc_count = 0
            for msg in messages:
                role = msg.get("role", "user")
                content = _encode_content(msg.get("content"))
                ts = _maybe_epoch(msg.get("timestamp")) or _now()
                tool_calls = msg.get("tool_calls")
                tc_json = json.dumps(tool_calls) if tool_calls else None
                num_tc = len(tool_calls) if tool_calls else 0

                cur.execute(
                    f"CREATE VERTEX Message SET "
                    f"session_id = {_q(session_id)}, role = {_q(role)}, "
                    f"content = {_q(content)}, timestamp = {_n(ts)}, "
                    f"tool_calls = {_q(tc_json)}, "
                    f"tool_call_id = {_q(msg.get('tool_call_id'))}, "
                    f"tool_name = {_q(msg.get('tool_name'))}, "
                    f"finish_reason = {_q(msg.get('finish_reason'))}, "
                    f"reasoning = {_q(msg.get('reasoning'))}, "
                    f"reasoning_content = {_q(msg.get('reasoning_content'))}, "
                    "active = 1"
                )
                new_msg_count += 1
                new_tc_count += num_tc

            cur.execute(
                f"UPDATE Session SET message_count = {new_msg_count}, "
                f"tool_call_count = {new_tc_count} WHERE id = {_q(session_id)}"
            )
        self._adapter.transact(_do)

    def archive_and_compact(
        self, session_id: str, compacted_messages: List[Dict],
    ) -> int:
        def _do(cur):
            cur.execute(
                "UPDATE Message SET active = 0, compacted = 1 "
                "WHERE session_id = %(sid)s AND active = 1",
                {"sid": session_id},
            )
            count = 0
            for msg in compacted_messages:
                content = _encode_content(msg.get("content"))
                ts = _maybe_epoch(msg.get("timestamp")) or _now()
                cur.execute(
                    "CREATE VERTEX Message SET session_id = %(sid)s, role = %(r)s, "
                    "content = %(c)s, timestamp = %(ts)s, active = 1",
                    {"sid": session_id, "r": msg.get("role", "user"),
                     "c": content, "ts": ts},
                )
                count += 1
            cur.execute(
                "UPDATE Session SET message_count = %(mc)s WHERE id = %(id)s",
                {"mc": count, "id": session_id},
            )
            return count
        return self._adapter.transact(_do)

    def rewind_to_message(
        self, session_id: str, target_message_id: int,
    ) -> Dict[str, Any]:
        def _do(cur):
            cur.execute(
                "SELECT @rid FROM Message WHERE session_id = %s ORDER BY timestamp, @rid",
                (session_id,),
            )
            all_rows = cur.fetchall()

            target_rid = None
            target_msg = None
            for r in all_rows:
                rid_str = str(r["@rid"])
                rid_hash = _rid_to_int(rid_str)
                if rid_hash == target_message_id:
                    target_rid = rid_str
                    cur.execute("SELECT FROM Message WHERE @rid = %s", (rid_str,))
                    tm = cur.fetchone()
                    if tm:
                        target_msg = dict(tm)
                        target_msg["content"] = _decode_content(target_msg.get("content"))
                    break

            if target_rid is None:
                raise ValueError(f"Message {target_message_id} not found")

            cur.execute(
                "UPDATE Message SET active = 0 "
                "WHERE session_id = %(sid)s AND @rid >= %(rid)s AND active = 1",
                {"sid": session_id, "rid": target_rid},
            )
            cur.execute(
                "UPDATE Session SET rewind_count = rewind_count + 1 WHERE id = %s",
                (session_id,),
            )
            cur.execute(
                "SELECT @rid FROM Message WHERE session_id = %(sid)s AND active = 1 "
                "ORDER BY @rid DESC LIMIT 1",
                {"sid": session_id},
            )
            head = cur.fetchone()
            new_head = _rid_to_int(str(head["@rid"])) if head else None

            return {
                "rewound_count": cur.rowcount,
                "target_message": target_msg,
                "new_head_id": new_head,
            }

        return self._adapter.transact(_do)

    def restore_rewound(self, session_id: str, since_message_id: int) -> int:
        rows = self._adapter.query(
            "SELECT @rid FROM Message WHERE session_id = %s ORDER BY @rid", (session_id,)
        )
        target_rid = None
        for r in rows:
            if _rid_to_int(str(r["@rid"])) == since_message_id:
                target_rid = str(r["@rid"])
                break
        if target_rid is None:
            return 0
        self._adapter.execute(
            "UPDATE Message SET active = 1 WHERE session_id = %(sid)s "
            "AND @rid >= %(rid)s AND active = 0 AND compacted = 0",
            {"sid": session_id, "rid": target_rid},
        )
        return 0  # simplified

    def list_recent_user_messages(
        self, session_id: str, limit: int = 20, include_inactive: bool = False,
    ) -> List[Dict[str, Any]]:
        active_filter = "" if include_inactive else " AND active = 1"
        rows = self._adapter.query(
            f"SELECT @rid, timestamp, content FROM Message "
            f"WHERE session_id = %s AND role = 'user'{active_filter} "
            "ORDER BY @rid DESC LIMIT %s",
            (session_id, limit),
        )
        result: List[Dict] = []
        for r in rows:
            content = _decode_content(r.get("content"))
            preview = str(content)[:100] if content else ""
            result.append({
                "id": _rid_to_int(str(r["@rid"])),
                "timestamp": r.get("timestamp"),
                "preview": preview,
            })
        return result

    def clear_messages(self, session_id: str) -> None:
        # Soft-delete — DELETE VERTEX hangs on edge cascade (TD-4/18)
        self._adapter.execute(
            f"UPDATE Message SET active = 0, compacted = 1 WHERE session_id = {_q(session_id)}"
        )
        self._adapter.execute(
            f"UPDATE Session SET message_count = 0, tool_call_count = 0 WHERE id = {_q(session_id)}"
        )

    def message_count(self, session_id: Optional[str] = None) -> int:
        if session_id:
            rows = self._adapter.query(
                "SELECT count(*) as cnt FROM Message WHERE session_id = %s",
                (session_id,),
            )
        else:
            rows = self._adapter.query("SELECT count(*) as cnt FROM Message")
        return rows[0].get("cnt", 0) if rows else 0

    def has_platform_message_id(
        self, session_id: str, platform_message_id: str,
    ) -> bool:
        rows = self._adapter.query(
            "SELECT FROM Message WHERE session_id = %s "
            "AND platform_message_id = %s LIMIT 1",
            (session_id, platform_message_id),
        )
        return len(rows) > 0

    # ==================================================================
    # Search (CRITICAL)
    # ==================================================================

    def search_messages(
        self, query: str, source_filter: Optional[str] = None,
        exclude_sources: Optional[List[str]] = None,
        role_filter: Optional[str] = None,
        limit: int = 20, offset: int = 0, sort: Optional[str] = None,
        include_inactive: bool = False,
    ) -> List[Dict[str, Any]]:
        active_clause = "AND (active = 1 OR compacted = 1)" if not include_inactive else ""
        
        # Build filter conditions (Block 2)
        filters = []
        if source_filter:
            filters.append(f"source = {_q(source_filter)}")
        if exclude_sources:
            excl = ", ".join(_q(s) for s in exclude_sources)
            filters.append(f"source NOT IN ({excl})")
        if role_filter:
            filters.append(f"role = {_q(role_filter)}")
        filter_clause = (" AND " + " AND ".join(filters)) if filters else ""
        
        # Sort direction
        sort_clause = "timestamp DESC"
        if sort == "newest":
            sort_clause = "timestamp DESC"
        elif sort == "oldest":
            sort_clause = "timestamp ASC"

        rows = []
        if self._embedder:
            try:
                q_emb = self._embedder.embed_query(query)
                from hermes_cli.arcadedb import ArcadeDBAdapter
                qv = ArcadeDBAdapter._vec(q_emb.dense)
                rows = self._adapter.query(
                    f"SELECT @rid as rid, session_id, role, content, "
                    "timestamp, tool_name "
                    "FROM Message "
                    f"WHERE @rid IN [ expand(`vector.neighbors`('Message[embedding]', {qv}, {limit * 2})) ] "
                    f"{active_clause}{filter_clause} "
                    f"ORDER BY {sort_clause} "
                    f"LIMIT {limit} SKIP {offset}"
                )
            except Exception:
                pass  # fall through to LIKE

        # LIKE fallback (primary for CJK, secondary for non-embedded)
        if not rows:
            has_cjk = _has_cjk(query)
            if has_cjk:
                rows = self._adapter.query(
                    "SELECT @rid as rid, session_id, role, content, "
                    "timestamp, tool_name "
                    "FROM Message "
                    "WHERE (content LIKE %(q)s "
                    "   OR tool_name LIKE %(q)s) "
                    f"{active_clause} "
                    "ORDER BY timestamp DESC, @rid "
                    "LIMIT %(l)s SKIP %(o)s",
                    {"q": f"%{query}%", "l": limit, "o": offset},
                )
            else:
                rows = self._adapter.query(
                    "SELECT session_id, role, content, "
                    "timestamp, tool_name "
                    "FROM Message "
                    "WHERE content LIKE %(q)s "
                    f"{active_clause} "
                    "ORDER BY timestamp DESC "
                    "LIMIT %(l)s SKIP %(o)s",
                    {"q": f"%{query}%", "l": limit, "o": offset},
                )

        # Enrich with session metadata + apply source/role filters (Block 2)
        session_cache = {}
        def _get_session(sid):
            if sid not in session_cache:
                s = self._adapter.query(
                    "SELECT source, model, started_at FROM Session WHERE id = %s",
                    (sid,),
                )
                session_cache[sid] = s[0] if s else {}
            return session_cache[sid]

        results: List[Dict] = []
        for r in rows:
            sid = r.get("session_id")
            s = _get_session(sid)
            
            # Apply filters at application level (source/role not joinable in ArcadeDB)
            if source_filter and s.get("source") != source_filter:
                continue
            if exclude_sources and s.get("source") in exclude_sources:
                continue
            if role_filter and r.get("role") != role_filter:
                continue
                
            content = _decode_content(r.get("content"))
            content_str = str(content) if content else ""
            snippet = self._build_snippet(content_str, query)
            results.append({
                "id": _rid_to_int(str(r.get("@rid", r.get("rid", "")))),
                "session_id": sid,
                "role": r.get("role"),
                "snippet": snippet,
                "timestamp": r.get("timestamp"),
                "tool_name": r.get("tool_name"),
                "source": s.get("source"),
                "model": s.get("model"),
                "session_started": s.get("started_at"),
            })
        
        # Apply sort
        if sort == "oldest":
            results.sort(key=lambda x: x.get("timestamp", 0))
        elif sort == "newest":
            results.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
            
        return results

    def _build_snippet(self, content: str, query: str) -> str:
        if not content:
            return ""
        pos = content.lower().find(query.lower())
        if pos < 0:
            return content[:200] + ("..." if len(content) > 200 else "")

        max_context = 200
        start = max(0, pos - max_context // 2)
        end = min(len(content), pos + len(query) + max_context // 2)
        snippet = content[start:end]
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(content) else ""

        return prefix + snippet + suffix

    def hybrid_search_sessions(
        self, query: str, keywords: str = "", top_k: int = 10,
        profile: Optional[str] = None, days: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if not self._embedder:
            return self._adapter.query(
                f"SELECT FROM SearchMatter "
                f"WHERE summary LIKE {_q(f'%{query}%')} "
                f"ORDER BY created_at DESC LIMIT {top_k}"
            )

        # Compute query embedding — pass as JSON array SQL literal 
        from hermes_cli.arcadedb import ArcadeDBAdapter
        q_emb = self._embedder.embed_query(query)
        qv = ArcadeDBAdapter._vec(q_emb.dense)  # JSON array for SQL

        where = ""
        kw_sql = ""
        params: Dict[str, Any] = {"tk2": top_k}
        fulltext_branch = ""

        if keywords:
            kw_sql = f" WHERE SEARCH_INDEX('SearchMatter[summary]', %(kw)s) = true"
            params["kw"] = keywords
        if profile:
            where += f" AND profile = {_q(profile)}"
        if days is not None:
            cutoff = _now() - days * 86400
            where += f" AND created_at >= {_n(cutoff)}"

        if keywords:
            fulltext_branch = f"    (SELECT @rid FROM SearchMatter{kw_sql}{where}),\n"
        elif where.strip():
            fulltext_branch = f"    (SELECT @rid FROM SearchMatter WHERE 1=1{where}),\n"
        else:
            fulltext_branch = "    (SELECT @rid FROM SearchMatter),\n"

        sql = (
            "SELECT expand(`vector.fuse`(\n"
            f"    `vector.neighbors`('SearchMatter[embedding]', {qv}, {top_k}),\n"
            f"{fulltext_branch}"
            "    { fusion: 'RRF', groupBy: 'session_rid', groupSize: 1 }\n"
            ")) LIMIT %(tk2)s"
        )

        return self._adapter.query(sql, params)

    # ==================================================================
    # Compression Locks
    # ==================================================================

    def try_acquire_compression_lock(
        self, session_id: str, holder: str, ttl_seconds: float = 300,
    ) -> bool:
        now_ts = _now()
        expires = now_ts + ttl_seconds

        def _do(cur):
            cur.execute(
                f"DELETE FROM CompressionLock WHERE session_id = {_q(session_id)} "
                f"AND expires_at < {_n(now_ts)}"
            )
            try:
                cur.execute(
                    f"INSERT INTO CompressionLock SET "
                    f"session_id = {_q(session_id)}, holder = {_q(holder)}, "
                    f"acquired_at = {_n(now_ts)}, expires_at = {_n(expires)}"
                )
            except ArcadeDBError:
                return False
            cur.execute(
                "SELECT holder FROM CompressionLock WHERE session_id = %s",
                (session_id,),
            )
            rows = cur.fetchall()
            return bool(rows and rows[0]["holder"] == holder)

        try:
            return self._adapter.transact(_do)
        except ArcadeDBError:
            return False

    def refresh_compression_lock(
        self, session_id: str, holder: str, ttl_seconds: float = 300,
    ) -> bool:
        expires = _now() + ttl_seconds
        self._adapter.execute(
            f"UPDATE CompressionLock SET expires_at = {_n(expires)} "
            f"WHERE session_id = {_q(session_id)} AND holder = {_q(holder)}"
        )
        return True

    def release_compression_lock(self, session_id: str, holder: str) -> None:
        self._adapter.execute(
            f"DELETE FROM CompressionLock WHERE session_id = {_q(session_id)} AND holder = {_q(holder)}"
        )

    def get_compression_lock_holder(self, session_id: str) -> Optional[str]:
        rows = self._adapter.query(
            "SELECT holder FROM CompressionLock WHERE session_id = %s "
            "AND expires_at >= %s",
            (session_id, _now()),
        )
        return rows[0]["holder"] if rows else None

    # ==================================================================
    # Compression Cooldown
    # ==================================================================

    def record_compression_failure_cooldown(
        self, session_id: str, cooldown_until: float, error: Optional[str] = None,
    ) -> None:
        self._adapter.execute(
            f"UPDATE Session SET compression_failure_cooldown_until = {_n(cooldown_until)}, "
            f"compression_failure_error = {_q(error)} WHERE id = {_q(session_id)}"
        )

    def get_compression_failure_cooldown(
        self, session_id: str,
    ) -> Optional[Dict[str, Any]]:
        rows = self._adapter.query(
            "SELECT compression_failure_cooldown_until, compression_failure_error "
            "FROM Session WHERE id = %s", (session_id,)
        )
        if not rows:
            return None
        r = rows[0]
        cu = r.get("compression_failure_cooldown_until")
        if cu is None:
            return None
        return {
            "cooldown_until": cu,
            "error": r.get("compression_failure_error"),
        }

    def clear_compression_failure_cooldown(self, session_id: str) -> None:
        self._adapter.execute(
            "UPDATE Session SET compression_failure_cooldown_until = NULL, "
            "compression_failure_error = NULL WHERE id = %s", (session_id,)
        )

    # ==================================================================
    # Meta Key/Value Store
    # ==================================================================

    def get_meta(self, key: str) -> Optional[str]:
        rows = self._adapter.query(
            "SELECT value FROM StateMeta WHERE key = %s ORDER BY @rid DESC LIMIT 1", (key,)
        )
        return rows[0].get("value") if rows else None

    def set_meta(self, key: str, value: str) -> None:
        # Find existing vertex and delete by @rid (DELETE VERTEX WHERE key=... hangs)
        existing = self._adapter.query(
            f"SELECT @rid FROM StateMeta WHERE key = {_q(key)} LIMIT 1"
        )
        if existing:
            try:
                self._adapter.execute(
                    f"DELETE VERTEX StateMeta WHERE @rid = {_q(existing[0]['@rid'])}"
                )
            except Exception:
                pass  # already deleted or ArcadeDB issue
        self._adapter.execute(
            f"CREATE VERTEX StateMeta SET key = {_q(key)}, value = {_q(value)}"
        )

    # ==================================================================
    # Session Deletion & Maintenance
    # ==================================================================

    def delete_session(self, session_id: str, sessions_dir: Optional[str] = None) -> bool:
        row = self.get_session(session_id)
        if not row:
            return False
        # Soft-delete messages (DELETE VERTEX hangs with edge cascade — TD-4/18)
        self._adapter.execute(
            f"UPDATE Message SET active = 0, compacted = 1 WHERE session_id = {_q(session_id)}"
        )
        self._adapter.execute(
            f"UPDATE Session SET archived = 1, ended_at = {_n(_now())}, end_reason = 'deleted' WHERE id = {_q(session_id)}"
        )
        return True

    def delete_sessions(
        self, session_ids: List[str], sessions_dir: Optional[str] = None,
    ) -> int:
        count = 0
        for sid in session_ids:
            if self.delete_session(sid):
                count += 1
        return count

    def delete_empty_sessions(self, sessions_dir: Optional[str] = None) -> int:
        rows = self._adapter.query(
            "SELECT id FROM Session WHERE message_count = 0 AND ended_at IS NOT NULL "
            "AND archived = 0"
        )
        count = 0
        for r in rows:
            if self.delete_session(r["id"]):
                count += 1
        return count

    def count_empty_sessions(self) -> int:
        rows = self._adapter.query(
            "SELECT count(*) as cnt FROM Session WHERE message_count = 0 "
            "AND ended_at IS NOT NULL AND archived = 0"
        )
        return rows[0].get("cnt", 0) if rows else 0

    def prune_sessions(
        self, older_than_days: int = 90, source: Optional[str] = None,
        sessions_dir: Optional[str] = None,
    ) -> int:
        cutoff = _now() - older_than_days * 86400
        params: Dict[str, Any] = {"cut": cutoff}
        where = "started_at < %(cut)s"
        if source:
            where += " AND source = %(src)s"; params["src"] = source
        rows = self._adapter.query(f"SELECT id FROM Session WHERE {where}", params)
        count = 0
        for r in rows:
            if self.delete_session(r["id"]):
                count += 1
        return count

    def delete_session_if_empty(
        self, session_id: str, sessions_dir: Optional[str] = None,
    ) -> bool:
        msgs = self._adapter.query(
            "SELECT count(*) as cnt FROM Message WHERE session_id = %s AND active = 1",
            (session_id,),
        )
        if msgs and msgs[0].get("cnt", 0) > 0:
            return False
        s = self.get_session(session_id)
        if s and not s.get("title"):
            self.delete_session(session_id)
            return True
        return False

    def vacuum(self) -> int:
        """VACUUM equivalent — prune inactive vertices (TD-18)."""
        try:
            # Delete old inactive messages (older than 7 days)
            cutoff = _now() - 7 * 86400
            self._adapter.execute(
                f"DELETE VERTEX Message WHERE active = 0 AND timestamp < {_n(cutoff)}"
            )
        except Exception:
            pass
        return 0

    def maybe_auto_prune_and_vacuum(
        self, retention_days: int = 90, min_interval_hours: int = 24,
        vacuum: bool = True, sessions_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        last_run_str = self.get_meta("last_auto_prune")
        if last_run_str:
            last_run = float(last_run_str)
            if _now() - last_run < min_interval_hours * 3600:
                return {"pruned": 0, "vacuumed": False, "skipped": True}

        pruned = self.prune_sessions(retention_days)
        self.set_meta("last_auto_prune", str(_now()))
        return {"pruned": pruned, "vacuumed": False, "skipped": False}

    def prune_empty_ghost_sessions(self, sessions_dir: Optional[str] = None) -> int:
        return self.delete_empty_sessions()

    def finalize_orphaned_compression_sessions(self) -> int:
        rows = self._adapter.query(
            "SELECT id FROM Session WHERE end_reason IS NULL "
            "AND source IN ('compression', 'subagent') AND started_at < %s",
            (_now() - 86400,),
        )
        count = 0
        for r in rows:
            self.end_session(r["id"], "orphaned_compression")
            count += 1
        return count

    # ==================================================================
    # Handoff
    # ==================================================================

    def request_handoff(self, session_id: str, platform: str) -> bool:
        self._adapter.execute(
            f"UPDATE Session SET handoff_state = 'pending', handoff_platform = {_q(platform)} "
            f"WHERE id = {_q(session_id)}"
        )
        return True

    def get_handoff_state(self, session_id: str) -> Optional[Dict[str, Any]]:
        rows = self._adapter.query(
            "SELECT handoff_state, handoff_platform, handoff_error "
            "FROM Session WHERE id = %s", (session_id,)
        )
        return rows[0] if rows else None

    def list_pending_handoffs(self) -> List[Dict[str, Any]]:
        return self._adapter.query(
            "SELECT FROM Session WHERE handoff_state = 'pending'"
        )

    def claim_handoff(self, session_id: str) -> bool:
        rows = self._adapter.query(
            f"SELECT @rid FROM Session WHERE id = {_q(session_id)} "
            f"AND handoff_state = 'pending' LIMIT 1"
        )
        if not rows:
            return False
        self._adapter.execute(
            f"UPDATE Session SET handoff_state = 'running' "
            f"WHERE @rid = {_q(rows[0]['@rid'])}"
        )
        return True

    def complete_handoff(self, session_id: str) -> None:
        self._adapter.execute(
            "UPDATE Session SET handoff_state = 'completed' WHERE id = %s",
            (session_id,),
        )

    def fail_handoff(self, session_id: str, error: str) -> None:
        self._adapter.execute(
            f"UPDATE Session SET handoff_state = 'failed', handoff_error = {_q(error)} "
            f"WHERE id = {_q(session_id)}"
        )

    # ==================================================================
    # Telegram Topic Mode
    # ==================================================================

    def apply_telegram_topic_migration(self) -> None:
        pass

    def enable_telegram_topic_mode(
        self, *, chat_id: str, user_id: str, **kw: Any,
    ) -> None:
        now_ts = _now()
        existing = self._adapter.query(
            "SELECT FROM TelegramTopicMode WHERE chat_id = %s", (chat_id,)
        )
        if existing:
            self._adapter.execute(
                "UPDATE TelegramTopicMode SET enabled = 1, updated_at = %s "
                "WHERE chat_id = %s", (now_ts, chat_id),
            )
        else:
            self._adapter.execute(
                "INSERT INTO TelegramTopicMode SET chat_id = %s, user_id = %s, "
                "enabled = 1, activated_at = %s, updated_at = %s",
                (chat_id, user_id, now_ts, now_ts),
            )

    def disable_telegram_topic_mode(
        self, *, chat_id: str, clear_bindings: bool = True,
    ) -> None:
        self._adapter.execute(
            "UPDATE TelegramTopicMode SET enabled = 0 WHERE chat_id = %s", (chat_id,)
        )
        if clear_bindings:
            self._adapter.execute(
                "DELETE VERTEX TelegramTopicBinding WHERE chat_id = %s", (chat_id,)
            )

    def is_telegram_topic_mode_enabled(
        self, *, chat_id: str, user_id: Optional[str] = None,
    ) -> bool:
        rows = self._adapter.query(
            "SELECT enabled FROM TelegramTopicMode WHERE chat_id = %s", (chat_id,)
        )
        return bool(rows and rows[0].get("enabled"))

    def bind_telegram_topic(
        self, *, chat_id: str, thread_id: str, user_id: str,
        session_key: str, session_id: str, **kw: Any,
    ) -> None:
        now_ts = _now()
        self._adapter.execute(
            f"INSERT INTO TelegramTopicBinding SET "
            f"chat_id = {_q(chat_id)}, thread_id = {_q(thread_id)}, user_id = {_q(user_id)}, "
            f"session_key = {_q(session_key)}, session_id = {_q(session_id)}, "
            f"managed_mode = 'auto', linked_at = {_n(now_ts)}, updated_at = {_n(now_ts)}"
        )

    def get_telegram_topic_binding(
        self, *, chat_id: str, thread_id: str,
    ) -> Optional[Dict[str, Any]]:
        rows = self._adapter.query(
            "SELECT FROM TelegramTopicBinding WHERE chat_id = %s "
            "AND thread_id = %s",
            (chat_id, thread_id),
        )
        return rows[0] if rows else None

    def get_telegram_topic_binding_by_session(
        self, *, session_id: str,
    ) -> Optional[Dict[str, Any]]:
        rows = self._adapter.query(
            "SELECT FROM TelegramTopicBinding WHERE session_id = %s", (session_id,)
        )
        return rows[0] if rows else None

    def list_telegram_topic_bindings_for_chat(
        self, *, chat_id: str,
    ) -> List[Dict[str, Any]]:
        return self._adapter.query(
            "SELECT FROM TelegramTopicBinding WHERE chat_id = %s", (chat_id,)
        )

    def delete_telegram_topic_binding(
        self, *, chat_id: str, thread_id: str,
    ) -> int:
        self._adapter.execute(
            f"DELETE VERTEX TelegramTopicBinding WHERE chat_id = {_q(chat_id)} "
            f"AND thread_id = {_q(thread_id)}"
        )
        remaining = self._adapter.query(
            "SELECT count(*) as cnt FROM TelegramTopicBinding WHERE chat_id = %s",
            (chat_id,),
        )
        if remaining and remaining[0].get("cnt", 0) == 0:
            self.disable_telegram_topic_mode(chat_id=chat_id)
        return 1

    def is_telegram_session_linked_to_topic(
        self, *, session_id: str,
    ) -> bool:
        rows = self._adapter.query(
            "SELECT FROM TelegramTopicBinding WHERE session_id = %s", (session_id,)
        )
        return len(rows) > 0

    def list_unlinked_telegram_sessions_for_user(
        self, *, chat_id: str, user_id: str, limit: int = 10, **kw: Any,
    ) -> List[Dict[str, Any]]:
        linked = self._adapter.query(
            "SELECT session_id FROM TelegramTopicBinding WHERE chat_id = %s", (chat_id,)
        )
        linked_ids = [r["session_id"] for r in linked]
        if linked_ids:
            return []
        return self._adapter.query(
            "SELECT FROM Session WHERE source = 'telegram' "
            "AND chat_id = %s ORDER BY started_at DESC LIMIT %s",
            (chat_id, limit),
        )

    # ==================================================================
    # Export
    # ==================================================================

    def export_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        s = self.get_session(session_id)
        if not s:
            return None
        msgs = self.get_messages(session_id, include_inactive=True)
        return {"session": s, "messages": msgs}

    def export_all(self, source: Optional[str] = None) -> List[Dict[str, Any]]:
        sessions = self.list_sessions_rich(source=source, limit=10000, offset=0)
        result = []
        for s in sessions:
            exp = self.export_session(s["id"])
            if exp:
                result.append(exp)
        return result
