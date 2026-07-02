"""ArcadeDB graph schema DDL for Hermes Agent.

Maps SQLite tables (state.db, kanban.db, memory_store.db)
into a property-graph model (vertices + edges + indexes).

Usage::

    from hermes_cli.arcadedb import ArcadeDBAdapter
    from hermes_cli.arcadedb_schema import SchemaManager

    db = ArcadeDBAdapter(config)
    db.connect()
    mgr = SchemaManager(db)
    mgr.create_all()            # create all types + indexes
    mgr.drop_all()              # drop everything (careful!)
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from hermes_cli.arcadedb import ArcadeDBAdapter

# ---------------------------------------------------------------------------
# DDL helpers
# ---------------------------------------------------------------------------

_VECTOR_DIM = 1024


def _drop(name: str, kind: str = "TYPE") -> str:
    return f"DROP {kind} `{name}` IF EXISTS"


def _create_type(name: str, kind: str = "VERTEX") -> str:
    return f"CREATE {kind} TYPE `{name}`"


def _create_property(
    type_name: str, prop: str, ptype: str, *,
    external: bool = False,
    notnull: bool = False,
    default: Optional[str] = None,
) -> str:
    parts = [f"CREATE PROPERTY `{type_name}`.{prop} {ptype}"]
    if external:
        parts.append("(EXTERNAL true)")
    opts = []
    if notnull:
        opts.append("NOTNULL")
    if default is not None:
        opts.append(f"DEFAULT {default}")
    if opts:
        parts.append(f"({' '.join(opts)})")
    return " ".join(parts)


def _create_index(
    type_name: str, prop: str, kind: str = "NOTUNIQUE",
    metadata: Optional[str] = None,
    name: Optional[str] = None,
) -> str:
    idx_name = name or f"{type_name}_{prop}_{kind.replace(' ', '_')}"
    if metadata:
        return (
            f"CREATE INDEX `{idx_name}` ON `{type_name}` ({prop}) {kind} "
            f"METADATA {{{metadata}}}"
        )
    return f"CREATE INDEX `{idx_name}` ON `{type_name}` ({prop}) {kind}"


# ---------------------------------------------------------------------------
# Vertex DDL
# ---------------------------------------------------------------------------

VERTICES: Dict[str, Dict[str, Any]] = {
    "Session": {
        "props": [
            ("id", "STRING"),
            ("source", "STRING"),
            ("user_id", "STRING"),
            ("session_key", "STRING"),
            ("chat_id", "STRING"),
            ("chat_type", "STRING"),
            ("thread_id", "STRING"),
            ("model", "STRING"),
            ("model_config", "STRING"),
            ("system_prompt", "STRING"),
            ("started_at", "DOUBLE"),
            ("ended_at", "DOUBLE"),
            ("end_reason", "STRING"),
            ("message_count", "INTEGER"),
            ("tool_call_count", "INTEGER"),
            ("input_tokens", "INTEGER"),
            ("output_tokens", "INTEGER"),
            ("cache_read_tokens", "INTEGER"),
            ("cache_write_tokens", "INTEGER"),
            ("reasoning_tokens", "INTEGER"),
            ("cwd", "STRING"),
            ("git_branch", "STRING"),
            ("git_repo_root", "STRING"),
            ("estimated_cost_usd", "DOUBLE"),
            ("actual_cost_usd", "DOUBLE"),
            ("cost_status", "STRING"),
            ("title", "STRING"),
            ("api_call_count", "INTEGER", "DEFAULT 0"),
            ("rewind_count", "INTEGER", "DEFAULT 0"),
            ("archived", "INTEGER", "DEFAULT 0"),
            # Phase 3: compression / billing / handoff fields (mirrors state.db)
            ("compression_failure_cooldown_until", "DOUBLE"),
            ("compression_failure_error", "STRING"),
            ("handoff_state", "STRING"),
            ("handoff_platform", "STRING"),
            ("handoff_error", "STRING"),
            ("parent_session_id", "STRING"),
            ("billing_provider", "STRING"),
            ("billing_base_url", "STRING"),
            ("billing_mode", "STRING"),
        ],
        "indexes": [
            ("source", "NOTUNIQUE"),
            ("source", "id", "NOTUNIQUE"),
            ("started_at", "NOTUNIQUE"),
            ("session_key", "NOTUNIQUE"),
        ],
    },
    "Message": {
        "props": [
            ("content", "STRING"),
            ("role", "STRING"),
            ("timestamp", "DOUBLE"),
            ("token_count", "INTEGER"),
            ("finish_reason", "STRING"),
            ("reasoning", "STRING"),
            ("reasoning_content", "STRING"),
            ("tool_call_id", "STRING"),
            ("tool_calls", "STRING"),
            ("tool_name", "STRING"),
            ("platform_message_id", "STRING"),
            ("observed", "INTEGER", "DEFAULT 0"),
            ("active", "INTEGER", "DEFAULT 1"),
            ("compacted", "INTEGER", "DEFAULT 0"),
            ("embedding", f"LIST(EXTERNAL true)"),
            ("entity_names", "LIST"),
            # Phase 3: session_id for direct lookup (enables index scans
            # without traversing HAS_MESSAGE edges).
            ("session_id", "STRING"),
            # Phase 3: structured codex/reasoning fields (mirrors state.db columns)
            ("reasoning_details", "STRING"),
            ("codex_reasoning_items", "STRING"),
            ("codex_message_items", "STRING"),
        ],
        "indexes": [
            ("role", "NOTUNIQUE"),
            ("timestamp", "NOTUNIQUE"),
            ("embedding", "LSM_VECTOR",
             f"dimensions:{_VECTOR_DIM},similarity:'COSINE',quantization:'INT8'"),
            # Phase 3: direct lookup by session_id + timestamp (replaces idx_messages_session)
            (("session_id", "timestamp"), "NOTUNIQUE"),
            (("session_id", "active", "timestamp"), "NOTUNIQUE"),
            # Phase 3: full-text search (replaces FTS5 virtual table)
            ("content", "FULL_TEXT"),
            # Phase 3: platform message id dedup
            (("session_id", "platform_message_id"), "NOTUNIQUE",
             "METADATA { ignoreNullValues: true }"),
        ],
    },
    "Task": {
        "props": [
            ("title", "STRING"),
            ("body", "STRING"),
            ("assignee", "STRING"),
            ("status", "STRING"),
            ("priority", "INTEGER", "DEFAULT 0"),
            ("created_by", "STRING"),
            ("created_at", "DOUBLE"),
            ("started_at", "DOUBLE"),
            ("completed_at", "DOUBLE"),
            ("workspace_kind", "STRING", "DEFAULT 'scratch'"),
            ("workspace_path", "STRING"),
            ("branch_name", "STRING"),
            ("project_id", "STRING"),
            ("claim_lock", "STRING"),
            ("claim_expires", "DOUBLE"),
            ("tenant", "STRING"),
            ("result", "STRING"),
            ("consecutive_failures", "INTEGER", "DEFAULT 0"),
            ("max_runtime_seconds", "INTEGER"),
            ("session_id", "STRING"),
            ("block_kind", "STRING"),
            ("block_recurrences", "INTEGER", "DEFAULT 0"),
            ("goal_mode", "INTEGER", "DEFAULT 0"),
        ],
        "indexes": [
            ("assignee", "status", "NOTUNIQUE"),
            ("status", "NOTUNIQUE"),
            ("created_at", "NOTUNIQUE"),
            ("tenant", "NOTUNIQUE"),
            ("session_id", "NOTUNIQUE"),
        ],
    },
    "TaskRun": {
        "props": [
            ("profile", "STRING"),
            ("status", "STRING"),
            ("started_at", "DOUBLE"),
            ("ended_at", "DOUBLE"),
            ("outcome", "STRING"),
            ("summary", "STRING"),
            ("metadata", "STRING"),
            ("error", "STRING"),
            ("max_runtime_seconds", "INTEGER"),
        ],
        "indexes": [
            ("status", "NOTUNIQUE"),
            ("started_at", "NOTUNIQUE"),
        ],
    },
    "TaskComment": {
        "props": [
            ("author", "STRING"),
            ("body", "STRING"),
            ("created_at", "DOUBLE"),
        ],
    },
    "TaskEvent": {
        "props": [
            ("kind", "STRING"),
            ("payload", "STRING"),
            ("created_at", "DOUBLE"),
        ],
    },
    "TaskAttachment": {
        "props": [
            ("filename", "STRING"),
            ("stored_path", "STRING"),
            ("content_type", "STRING"),
            ("size", "INTEGER", "DEFAULT 0"),
            ("uploaded_by", "STRING"),
            ("created_at", "DOUBLE"),
        ],
    },
    "Entity": {
        "props": [
            ("name", "STRING"),
            ("entity_type", "STRING", "DEFAULT 'unknown'"),
            ("aliases", "LIST"),
            ("created_at", "DOUBLE"),
        ],
        "indexes": [
            ("name", "NOTUNIQUE"),
        ],
    },
    "Fact": {
        "props": [
            ("content", "STRING"),
            ("category", "STRING", "DEFAULT 'general'"),
            ("tags", "LIST"),
            ("trust_score", "DOUBLE", "DEFAULT 0.5"),
            ("retrieval_count", "INTEGER", "DEFAULT 0"),
            ("helpful_count", "INTEGER", "DEFAULT 0"),
            ("created_at", "DOUBLE"),
            ("updated_at", "DOUBLE"),
            ("embedding", f"LIST(EXTERNAL true)"),
        ],
        "indexes": [
            ("category", "NOTUNIQUE"),
            ("trust_score", "NOTUNIQUE"),
            ("embedding", "LSM_VECTOR",
             f"dimensions:{_VECTOR_DIM},similarity:'COSINE',quantization:'INT8'"),
            # Phase 3: full-text search (replaces FTS5 facts_fts virtual table)
            ("content", "FULL_TEXT"),
        ],
    },
    "Profile": {
        "props": [
            ("name", "STRING"),
            ("display_name", "STRING"),
            ("created_at", "DOUBLE"),
        ],
        "indexes": [
            ("name", "UNIQUE"),
        ],
    },
    "Project": {
        "props": [
            ("name", "STRING"),
            ("path", "STRING"),
            ("description", "STRING"),
            ("created_at", "DOUBLE"),
        ],
        "indexes": [
            ("name", "UNIQUE"),
        ],
    },
    "KanbanBoard": {
        "props": [
            ("slug", "STRING"),
            ("title", "STRING"),
            ("created_at", "DOUBLE"),
        ],
        "indexes": [
            ("slug", "UNIQUE"),
        ],
    },
    "SearchMatter": {
        "props": [
            ("session_rid", "LINK"),
            ("summary", "STRING"),
            ("keywords", "LIST"),
            ("entity_names", "LIST"),
            ("embedding", f"LIST(EXTERNAL true)"),
            ("created_at", "DOUBLE"),
            ("profile", "STRING"),
            ("model", "STRING"),
        ],
        "indexes": [
            ("summary", "FULL_TEXT"),
            ("embedding", "LSM_VECTOR",
             f"dimensions:{_VECTOR_DIM},similarity:'COSINE',quantization:'INT8'"),
            ("created_at", "NOTUNIQUE"),
            ("profile", "NOTUNIQUE"),
        ],
    },
    # Phase 3: ArcadedbSessionDB additional vertex types
    "CompressionLock": {
        "props": [
            ("session_id", "STRING"),
            ("holder", "STRING"),
            ("acquired_at", "DOUBLE"),
            ("expires_at", "DOUBLE"),
        ],
        "indexes": [
            (("session_id",), "UNIQUE"),
            (("expires_at",), "NOTUNIQUE"),
        ],
    },
    "StateMeta": {
        "props": [
            ("key", "STRING"),
            ("value", "STRING"),
        ],
        "indexes": [
            (("key",), "UNIQUE"),
        ],
    },
    "TelegramTopicMode": {
        "props": [
            ("chat_id", "STRING"),
            ("user_id", "STRING"),
            ("enabled", "INTEGER", "DEFAULT 1"),
            ("activated_at", "DOUBLE"),
            ("updated_at", "DOUBLE"),
            ("has_topics_enabled", "INTEGER"),
            ("allows_users_to_create_topics", "INTEGER"),
            ("capability_checked_at", "DOUBLE"),
            ("intro_message_id", "STRING"),
            ("pinned_message_id", "STRING"),
        ],
        "indexes": [
            (("chat_id",), "UNIQUE"),
        ],
    },
    "TelegramTopicBinding": {
        "props": [
            ("chat_id", "STRING"),
            ("thread_id", "STRING"),
            ("user_id", "STRING"),
            ("session_key", "STRING"),
            ("session_id", "STRING"),
            ("managed_mode", "STRING", "DEFAULT 'auto'"),
            ("linked_at", "DOUBLE"),
            ("updated_at", "DOUBLE"),
        ],
        "indexes": [
            (("chat_id", "thread_id"), "UNIQUE"),
            (("session_id",), "NOTUNIQUE"),
        ],
    },
}

# ---------------------------------------------------------------------------
# Edge DDL (no explicit indexes — ArcadeDB auto-indexes @out / @in)
# ---------------------------------------------------------------------------

EDGES: Dict[str, Dict[str, Any]] = {
    "HAS_MESSAGE": {
        "props": [
            ("seq", "INTEGER"),
            ("role", "STRING"),
            ("tokens", "INTEGER"),
            ("created_at", "DOUBLE"),
        ],
    },
    "HAS_CHILD_SESSION": {
        "props": [
            ("end_reason", "STRING"),
            ("created_at", "DOUBLE"),
        ],
    },
    "ASSIGNED_TO": {
        "props": [
            ("at", "DOUBLE"),
        ],
    },
    "DEPENDS_ON": {
        "props": [],
    },
    "BLOCKED_BY": {
        "props": [
            ("kind", "STRING"),
            ("created_at", "DOUBLE"),
        ],
    },
    "HAS_RUN": {
        "props": [
            ("status", "STRING"),
            ("started_at", "DOUBLE"),
        ],
    },
    "HAS_COMMENT": {
        "props": [
            ("created_at", "DOUBLE"),
        ],
    },
    "HAS_EVENT": {
        "props": [
            ("type", "STRING"),
            ("created_at", "DOUBLE"),
        ],
    },
    "HAS_ATTACHMENT": {
        "props": [
            ("created_at", "DOUBLE"),
        ],
    },
    "MENTIONS": {
        "props": [
            ("weight", "DOUBLE", "DEFAULT 1.0"),
        ],
    },
    "RELATED_TO": {
        "props": [
            ("weight", "DOUBLE", "DEFAULT 0.5"),
        ],
    },
    "HAS_FACT": {
        "props": [],
    },
    "LINKED_TO": {
        "props": [],
    },
    "BELONGS_TO_BOARD": {
        "props": [],
    },
    "BELONGS_TO": {
        "props": [],
    },
}

# ---------------------------------------------------------------------------
# Schema manager
# ---------------------------------------------------------------------------


class SchemaManager:
    """Create / drop / inspect ArcadeDB graph schema from DDL definitions.

    NOTE: ArcadeDB v26.7.1-SNAPSHOT does not expose ``V``, ``E``,
    ``arc_schema_types``, or ``arc_schema`` as queryable types.
    Schema inspection is done via HTTP ``/api/v1/database/{name}``
    (when available) or by tracking created types locally.

    Properties cannot use ``IF NOT EXISTS`` — we catch "already exists"
    errors and skip them silently.
    """

    _ALREADY_EXISTS_MARKERS = (
        "already exists",
        "already defined",
    )

    def __init__(self, db: ArcadeDBAdapter) -> None:
        self._db = db
        # Track types we create (for existence checks — no queryable sys tables)
        self._created_types: set[str] = set()

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_all(self) -> None:
        """Create all vertex types, edge types, and indexes idempotently."""
        self._create_all_types()
        self._create_all_indexes()

    def _create_all_types(self) -> None:
        for name, cfg in VERTICES.items():
            self._create_vertex_type(name, cfg["props"])
        for name, cfg in EDGES.items():
            self._create_edge_type(name, cfg["props"])

    def _create_vertex_type(self, name: str, props: List[tuple]) -> None:
        sqls = [f"CREATE VERTEX TYPE `{name}` IF NOT EXISTS"]
        for p in props:
            pname = p[0]
            ptype = p[1]
            external = "EXTERNAL true" if "EXTERNAL" in ptype else ""
            pdef = ptype.replace("(EXTERNAL true)", "").replace("(", "").replace(")", "").strip()
            default_val = p[2] if len(p) > 2 else None
            opts = []
            if external:
                opts.append(f"({external})")
            if default_val is not None:
                val = default_val.replace("DEFAULT ", "", 1)
                opts.append(f"(DEFAULT {val})")
            opts_str = " ".join(opts)
            sql = f"CREATE PROPERTY `{name}`.{pname} {pdef}"
            if opts_str:
                sql += f" {opts_str}"
            sqls.append(sql)
        self._execute_batch(sqls)
        self._created_types.add(name)

    def _create_edge_type(self, name: str, props: List[tuple]) -> None:
        sqls = [f"CREATE EDGE TYPE `{name}` IF NOT EXISTS"]
        for p in props:
            pname = p[0]
            ptype = p[1]
            default_val = p[2] if len(p) > 2 else None
            val = default_val.replace("DEFAULT ", "", 1) if default_val is not None else None
            opts = f"(DEFAULT {val})" if val is not None else ""
            sqls.append(f"CREATE PROPERTY `{name}`.{pname} {ptype} {opts}".strip())
        self._execute_batch(sqls)
        self._created_types.add(name)

    def _create_all_indexes(self) -> None:
        for type_name, cfg in VERTICES.items():
            for idx_def in cfg.get("indexes", []):
                self._create_index(type_name, idx_def)
        for type_name, cfg in EDGES.items():
            for idx_def in cfg.get("indexes", []):
                self._create_index(type_name, idx_def)

    _INDEX_KINDS = {"UNIQUE", "NOTUNIQUE", "FULL_TEXT", "LSM_VECTOR", "LSM_SPARSE_VECTOR"}

    def _create_index(self, type_name: str, idx_def: tuple) -> None:
        """Create a single index.

        Supported ``idx_def`` formats:

        * 2-tuple ``(prop, kind)`` — e.g. ``("status", "NOTUNIQUE")``
        * 3-tuple ``(prop1, prop2, kind)`` — composite, e.g. ``("source", "id", "NOTUNIQUE")``
        * 3-tuple ``(prop, kind, metadata)`` — typed + metadata, e.g. ``("embedding", "LSM_VECTOR", "dimensions:1024,...")``
        """
        props_str: str
        kind: str
        metadata: Optional[str] = None

        if len(idx_def) == 2:
            props_str = idx_def[0]
            # Handle composite index: ((prop1, prop2), kind)
            if isinstance(props_str, tuple):
                props_str = ", ".join(props_str)
            kind = idx_def[1]
        elif len(idx_def) == 3:
            # If third element looks like an index kind → composite index
            if idx_def[2] in self._INDEX_KINDS:
                props_str = f"{idx_def[0]}, {idx_def[1]}"
                kind = idx_def[2]
            else:
                props_str = idx_def[0]
                kind = idx_def[1]
                metadata = idx_def[2]
        else:
            return

        sql = f"CREATE INDEX IF NOT EXISTS ON `{type_name}` ({props_str}) {kind}"
        if metadata:
            sql += f" METADATA {{{metadata}}}"
        try:
            self._db.execute(sql)
        except Exception as exc:
            if "already exists" in str(exc):
                return
            raise

    # ------------------------------------------------------------------
    # Drop
    # ------------------------------------------------------------------

    def drop_all(self) -> None:
        """Drop all custom types (edges first, then vertices) in dependency order."""
        edge_names = list(reversed(list(EDGES.keys())))
        vertex_names = list(reversed(list(VERTICES.keys())))

        for name in edge_names:
            self._safe_execute(f"DROP TYPE `{name}` IF EXISTS")
        for name in vertex_names:
            self._safe_execute(f"DROP TYPE `{name}` IF EXISTS")
        self._created_types.clear()

    def _safe_execute(self, sql: str) -> None:
        try:
            self._db.execute(sql)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("schema drop skipped: %s — %s", sql, exc)

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def type_exists(self, type_name: str) -> bool:
        """Check if a type exists (uses locally tracked set).

        ArcadeDB v26.7.1 has no queryable ``arc_schema_types`` table,
        so we track creation locally. For runtime verification we
        try a ``SELECT`` — if the type exists the query returns empty
        list (no rows), if it doesn't it errors.
        """
        if type_name in self._created_types:
            return True
        try:
            self._db.query(f"SELECT FROM `{type_name}` LIMIT 0")
            self._created_types.add(type_name)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _execute_batch(self, sqls: List[str]) -> None:
        for sql in sqls:
            try:
                self._db.execute(sql)
            except Exception as exc:
                err_msg = str(exc)
                if any(m in err_msg for m in self._ALREADY_EXISTS_MARKERS):
                    continue
                raise


# ---------------------------------------------------------------------------
# SQL → Graph conversion helpers
# ---------------------------------------------------------------------------

def session_to_graph(row: Dict[str, Any]) -> Dict[str, Any]:
    """Map a SQLite ``sessions`` row to ArcadeDB INSERT props (minus @rid)."""
    return {
        k: v for k, v in row.items()
        if k in {c[0] for c in VERTICES["Session"]["props"]}
    }


def message_to_graph(row: Dict[str, Any]) -> Dict[str, Any]:
    """Map a SQLite ``messages`` row to ArcadeDB INSERT props."""
    return {
        k: v for k, v in row.items()
        if k in {c[0] for c in VERTICES["Message"]["props"]}
    }


def task_to_graph(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        k: v for k, v in row.items()
        if k in {c[0] for c in VERTICES["Task"]["props"]}
    }


def fact_to_graph(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        k: v for k, v in row.items()
        if k in {c[0] for c in VERTICES["Fact"]["props"]}
    }
