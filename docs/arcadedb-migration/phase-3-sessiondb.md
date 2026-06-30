# Phase 3: ArcadedbSessionDB — Full SessionDB Replacement

| Поле | Значение |
|------|----------|
| **Номер** | Phase 3 |
| **Название** | ArcadedbSessionDB |
| **Новых строк** | ~3,500 (ArcadedbSessionDB) + ~200 (schema additions) |
| **Сложность** | **CRITICAL** |
| **Зависит от** | Phase 1 (Tests), Phase 2 (Adapter v2) |
| **Разблокирует** | Phase 4 (Consumers), Phase 5 (Migration), Phase 6 (KanbanDB), Phase 7 (Memory Store), Phase 8 (Other DBs) |

---

## Overview

Полная реализация `ArcadedbSessionDB` — замена `SessionDB` (5658 строк) из
`hermes_state.py`. Реализует **все 80+ публичных методов** с идентичным API.

### Ключевые отличия от SQLite

| SQLite | ArcadeDB | Примечание |
|--------|----------|------------|
| `sqlite3.connect(path)` | `ArcadeDBAdapter(config)` | Connection pool, не файл |
| `_execute_write(fn)` | `adapter.transact(fn)` | Атомарные транзакции |
| `AUTOINCREMENT` (messages) | Нет автоинкремента | ArcadeDB генерирует `@rid` |
| `BEGIN IMMEDIATE` | `adapter.begin()` | WAL lock не нужен |
| `PRAGMA wal_checkpoint` | Не нужно | ArcadeDB server-side |
| `RETURNING` для message_id | `@rid` как идентификатор | Возвращаем `@rid` вместо integer |
| FTS5 virtual table | `FULL_TEXT` Lucene index | Snippet logic в Python |
| `FOREIGN KEY CASCADE` | `DELETE VERTEX` + cascade in code | ArcadeDB edge traversal |
| `active`/`compacted` flags | Те же INTEGER поля | Уже в схеме |

---

## Files

### Новые файлы

| Файл | Строки | Назначение |
|------|--------|------------|
| `hermes_cli/arcadedb_session.py` | ~3,500 | Основной класс `ArcadedbSessionDB` |
| `hermes_cli/arcadedb_helpers.py` | ~150 | Shared utilities (content encoding, timestamps) |

### Модифицируемые файлы

| Файл | Изменение | Связь |
|------|-----------|-------|
| `hermes_cli/arcadedb_schema.py` | Добавить индексы и новые vertex types | [см. arcadedb_schema.py](../../hermes_cli/arcadedb_schema.py) |
| `tools/session_search_tool.py` | Адаптировать `_init_graph_store()` под новый конфиг | [см. session_search_tool.py](../../tools/session_search_tool.py) |
| `hermes_constants.py` | `display_hermes_home()` — уже используется | [см. hermes_constants.py](../../hermes_constants.py) |

---

## Schema Additions (в `hermes_cli/arcadedb_schema.py`)

### Новые индексы для Message (CRITICAL)

```sql
-- Direct session_id lookup (без edge traversal)
CREATE PROPERTY Message.session_id IF NOT EXISTS STRING
CREATE INDEX IF NOT EXISTS ON Message (session_id, timestamp) NOTUNIQUE
CREATE INDEX IF NOT EXISTS ON Message (session_id, active, timestamp) NOTUNIQUE

-- Full-text search на содержимом сообщений
CREATE INDEX IF NOT EXISTS ON Message (content) FULL_TEXT
  METADATA { analyzer: 'StandardAnalyzer', similarity: 'BM25' }

-- Platform message ID dedup
CREATE INDEX IF NOT EXISTS ON Message (session_id, platform_message_id)
  NOTUNIQUE
  METADATA { ignoreNullValues: true }
```

### Новые vertex types

```sql
-- Key-value store (замена state_meta table)
CREATE VERTEX TYPE StateMeta IF NOT EXISTS
CREATE PROPERTY StateMeta.key IF NOT EXISTS STRING (MANDATORY, NOTNULL)
CREATE PROPERTY StateMeta.value IF NOT EXISTS STRING
CREATE UNIQUE INDEX IF NOT EXISTS ON StateMeta (key)

-- Compression locks
CREATE VERTEX TYPE CompressionLock IF NOT EXISTS
CREATE PROPERTY CompressionLock.session_id IF NOT EXISTS STRING (MANDATORY, NOTNULL)
CREATE PROPERTY CompressionLock.holder IF NOT EXISTS STRING (MANDATORY, NOTNULL)
CREATE PROPERTY CompressionLock.acquired_at IF NOT EXISTS DOUBLE (MANDATORY, NOTNULL)
CREATE PROPERTY CompressionLock.expires_at IF NOT EXISTS DOUBLE (MANDATORY, NOTNULL)
CREATE UNIQUE INDEX IF NOT EXISTS ON CompressionLock (session_id)
CREATE INDEX IF NOT EXISTS ON CompressionLock (expires_at) NOTUNIQUE

-- Telegram DM Topic Mode
CREATE VERTEX TYPE TelegramTopicMode IF NOT EXISTS
CREATE PROPERTY TelegramTopicMode.chat_id IF NOT EXISTS STRING (MANDATORY, NOTNULL)
CREATE PROPERTY TelegramTopicMode.user_id IF NOT EXISTS STRING
CREATE PROPERTY TelegramTopicMode.enabled IF NOT EXISTS INTEGER (DEFAULT 1)
CREATE PROPERTY TelegramTopicMode.activated_at IF NOT EXISTS DOUBLE
CREATE PROPERTY TelegramTopicMode.updated_at IF NOT EXISTS DOUBLE
CREATE PROPERTY TelegramTopicMode.has_topics_enabled IF NOT EXISTS INTEGER
CREATE PROPERTY TelegramTopicMode.allows_users_to_create_topics IF NOT EXISTS INTEGER
CREATE PROPERTY TelegramTopicMode.capability_checked_at IF NOT EXISTS DOUBLE
CREATE PROPERTY TelegramTopicMode.intro_message_id IF NOT EXISTS STRING
CREATE PROPERTY TelegramTopicMode.pinned_message_id IF NOT EXISTS STRING
CREATE UNIQUE INDEX IF NOT EXISTS ON TelegramTopicMode (chat_id)

-- Telegram DM Topic Bindings
CREATE VERTEX TYPE TelegramTopicBinding IF NOT EXISTS
CREATE PROPERTY TelegramTopicBinding.chat_id IF NOT EXISTS STRING (MANDATORY, NOTNULL)
CREATE PROPERTY TelegramTopicBinding.thread_id IF NOT EXISTS STRING (MANDATORY, NOTNULL)
CREATE PROPERTY TelegramTopicBinding.user_id IF NOT EXISTS STRING (MANDATORY, NOTNULL)
CREATE PROPERTY TelegramTopicBinding.session_key IF NOT EXISTS STRING (MANDATORY, NOTNULL)
CREATE PROPERTY TelegramTopicBinding.session_id IF NOT EXISTS STRING (MANDATORY, NOTNULL)
CREATE PROPERTY TelegramTopicBinding.managed_mode IF NOT EXISTS STRING (DEFAULT 'auto')
CREATE PROPERTY TelegramTopicBinding.linked_at IF NOT EXISTS DOUBLE
CREATE PROPERTY TelegramTopicBinding.updated_at IF NOT EXISTS DOUBLE
CREATE UNIQUE INDEX IF NOT EXISTS ON TelegramTopicBinding (chat_id, thread_id)
CREATE INDEX IF NOT EXISTS ON TelegramTopicBinding (session_id)

-- Schema version tracking
CREATE PROPERTY StateMeta.value IF NOT EXISTS STRING
-- Для отслеживания версии: key='schema_version'
```

### Модификации `hermes_cli/arcadedb_schema.py`

- **Строки ~30-50:** добавить `_VECTOR_DIM = 1024`
- **Строки ~100-150:** добавить новые vertex types в `VERTICES` dict
- **Строки ~400-450:** добавить новые индексы в индексы dict
- **Строки ~500-550:** добавить `_create_index()` поддержку для FULL_TEXT индексов

**Связь:** [см. arcadedb_schema.py:VERTICES](../../hermes_cli/arcadedb_schema.py)

---

## API Specification

```python
# hermes_cli/arcadedb_session.py (~3,500 строк)

class ArcadedbSessionDB:
    """
    Полная замена SessionDB из hermes_state.py.
    Идентичный API, ArcadeDB как storage backend.
    """

    # ---- Constructor ----
    def __init__(
        self,
        adapter: ArcadeDBAdapter = None,
        embedder: EmbedderProvider = None,
        graph_store: GraphStore = None,
        read_only: bool = False,
    ):
        """
        Args:
            adapter: ArcadeDBAdapter (из Phase 2).
            embedder: EmbedderProvider для поиска.
            graph_store: GraphStore для гибридного поиска.
            read_only: True для read-only режима.
        """

    def close(self) -> None: ...

    # ---- Session Lifecycle ----
    def create_session(self, session_id, source, **kwargs) -> str: ...
    def ensure_session(self, session_id, source="unknown", **kwargs) -> str: ...
    def end_session(self, session_id, end_reason) -> None: ...
    def reopen_session(self, session_id) -> None: ...
    def get_session(self, session_id) -> dict | None: ...
    def resolve_session_id(self, session_id_or_prefix) -> str | None: ...

    # ---- Gateway Peer ----
    def record_gateway_session_peer(self, session_id, *, source, user_id, ...) -> None: ...
    def find_latest_gateway_session_for_peer(self, *, source, user_id, ...) -> dict | None: ...

    # ---- Session Metadata ----
    def update_session_meta(self, session_id, model_config_json, model=None) -> None: ...
    def update_system_prompt(self, session_id, system_prompt) -> None: ...
    def update_session_model(self, session_id, model) -> None: ...
    def update_session_billing_route(self, session_id, *, provider, base_url, ...) -> None: ...
    def update_token_counts(self, session_id, input_tokens=0, output_tokens=0, ...) -> None: ...
    def update_session_cwd(self, session_id, cwd, git_branch=None, git_repo_root=None) -> None: ...
    def backfill_repo_roots(self, cwd_to_root: dict[str, str]) -> None: ...

    # ---- Session Titles ----
    def set_session_title(self, session_id, title) -> bool: ...
    def get_session_title(self, session_id) -> str | None: ...
    def get_session_by_title(self, title) -> dict | None: ...
    def resolve_session_by_title(self, title) -> str | None: ...
    def get_next_title_in_lineage(self, base_title) -> str: ...
    def set_session_archived(self, session_id, archived: bool) -> bool: ...

    # ---- Session Listing & Counting ----
    def list_sessions_rich(self, source=None, exclude_sources=None, ...) -> list[dict]: ...
    def list_cron_job_runs(self, job_id, limit=20, offset=0) -> list[dict]: ...
    def search_sessions(self, source=None, limit=20, offset=0) -> list[dict]: ...
    def search_sessions_by_id(self, query, limit=20, include_archived=True) -> list[dict]: ...
    def session_count(self, source=None, cwd_prefix=None, ...) -> int: ...
    def distinct_session_cwds(self, include_archived=False) -> list[dict]: ...
    def get_compression_tip(self, session_id) -> str | None: ...
    def resolve_resume_session_id(self, session_id) -> str: ...

    # ---- Message Storage (CRITICAL) ----
    def append_message(self, session_id, role, content=None, tool_name=None,
                       tool_calls=None, tool_call_id=None, token_count=None,
                       finish_reason=None, reasoning=None, reasoning_content=None,
                       reasoning_details=None, codex_reasoning_items=None,
                       codex_message_items=None, platform_message_id=None,
                       observed=False, timestamp=None) -> int: ...
    def get_messages(self, session_id, include_inactive=False) -> list[dict]: ...
    def get_messages_as_conversation(self, session_id, include_ancestors=False,
                                     include_inactive=False) -> list[dict]: ...
    def get_messages_around(self, session_id, around_message_id,
                            window=5) -> dict: ...
    def get_anchored_view(self, session_id, around_message_id, window=5,
                          bookend=3, ...) -> dict: ...
    def replace_messages(self, session_id, messages) -> None: ...
    def archive_and_compact(self, session_id, compacted_messages) -> int: ...
    def rewind_to_message(self, session_id, target_message_id) -> dict: ...
    def restore_rewound(self, session_id, since_message_id) -> int: ...
    def list_recent_user_messages(self, session_id, limit=20,
                                  include_inactive=False) -> list[dict]: ...
    def clear_messages(self, session_id) -> None: ...
    def message_count(self, session_id=None) -> int: ...
    def has_platform_message_id(self, session_id,
                                platform_message_id) -> bool: ...

    # ---- Search (CRITICAL) ----
    def search_messages(self, query, source_filter=None, exclude_sources=None,
                        role_filter=None, limit=20, offset=0, sort=None,
                        include_inactive=False) -> list[dict]: ...
    # ---- Унаследован от GraphStore ----
    def hybrid_search_sessions(self, query, keywords="", top_k=10,
                               profile=None, days=None) -> list[dict]: ...

    # ---- Compression Locks (CRITICAL) ----
    def try_acquire_compression_lock(self, session_id, holder,
                                     ttl_seconds=300) -> bool: ...
    def refresh_compression_lock(self, session_id, holder,
                                 ttl_seconds=300) -> bool: ...
    def release_compression_lock(self, session_id, holder) -> None: ...
    def get_compression_lock_holder(self, session_id) -> str | None: ...

    # ---- Session Deletion & Maintenance ----
    def delete_session(self, session_id, sessions_dir=None) -> bool: ...
    def delete_sessions(self, session_ids, sessions_dir=None) -> int: ...
    def delete_empty_sessions(self, sessions_dir=None) -> int: ...
    def count_empty_sessions(self) -> int: ...
    def prune_sessions(self, older_than_days=90, source=None,
                       sessions_dir=None) -> int: ...
    def delete_session_if_empty(self, session_id, sessions_dir=None) -> bool: ...
    def vacuum(self) -> int: ...
    def maybe_auto_prune_and_vacuum(self, retention_days=90, ...) -> dict: ...
    def prune_empty_ghost_sessions(self, sessions_dir=None) -> int: ...
    def finalize_orphaned_compression_sessions(self) -> int: ...

    # ---- Compression Cooldown ----
    def record_compression_failure_cooldown(self, session_id, cooldown_until,
                                            error=None) -> None: ...
    def get_compression_failure_cooldown(self, session_id) -> dict | None: ...
    def clear_compression_failure_cooldown(self, session_id) -> None: ...

    # ---- Meta Key/Value Store ----
    def get_meta(self, key) -> str | None: ...
    def set_meta(self, key, value) -> None: ...

    # ---- Handoff ----
    def request_handoff(self, session_id, platform) -> bool: ...
    def get_handoff_state(self, session_id) -> dict | None: ...
    def list_pending_handoffs(self) -> list[dict]: ...
    def claim_handoff(self, session_id) -> bool: ...
    def complete_handoff(self, session_id) -> None: ...
    def fail_handoff(self, session_id, error) -> None: ...

    # ---- Telegram Topic Mode ----
    def apply_telegram_topic_migration(self) -> None: ...
    def enable_telegram_topic_mode(self, *, chat_id, user_id, ...) -> None: ...
    def disable_telegram_topic_mode(self, *, chat_id, clear_bindings=True) -> None: ...
    def is_telegram_topic_mode_enabled(self, *, chat_id, user_id) -> bool: ...
    def bind_telegram_topic(self, *, chat_id, thread_id, user_id, ...) -> None: ...
    def get_telegram_topic_binding(self, *, chat_id, thread_id) -> dict | None: ...
    def get_telegram_topic_binding_by_session(self, *, session_id) -> dict | None: ...
    def list_telegram_topic_bindings_for_chat(self, *, chat_id) -> list[dict]: ...
    def delete_telegram_topic_binding(self, *, chat_id, thread_id) -> int: ...
    def is_telegram_session_linked_to_topic(self, *, session_id) -> bool: ...
    def list_unlinked_telegram_sessions_for_user(self, *, chat_id, user_id, ...) -> list[dict]: ...

    # ---- Export ----
    def export_session(self, session_id) -> dict | None: ...
    def export_all(self, source=None) -> list[dict]: ...

    # ---- Internal Helpers ----
    @staticmethod
    def _encode_content(content) -> str: ...
    @staticmethod
    def _decode_content(content) -> any: ...
    @staticmethod
    def _now() -> float: ...
```

---

## Файл `hermes_cli/arcadedb_session.py` (~3,500 строк)

### Структура (по секциям)

```
hermes_cli/arcadedb_session.py
│
├── [1-40]    Module docstring + imports
│             from hermes_cli.arcadedb import ArcadeDBAdapter, ArcadeDBError
│             from hermes_cli.embedder import EmbedderProvider
│             from hermes_cli.graph_store import GraphStore
│             from hermes_cli.arcadedb_helpers import _encode_content, _decode_content, _now
│
├── [42-70]   Constants (_CONTENT_JSON_PREFIX, _VECTOR_DIM, _HIDDEN_SESSION_SOURCES, ...)
│
├── [72-200]  ArcadedbSessionDB.__init__() + close()
│
├── [202-400] Session Lifecycle Methods (8 методов)
│             create_session, ensure_session, end_session, reopen_session,
│             get_session, resolve_session_id, record_gateway_session_peer,
│             find_latest_gateway_session_for_peer
│
├── [402-550] Session Metadata Methods (7 методов)
│             update_session_meta, update_system_prompt, update_session_model,
│             update_session_billing_route, update_token_counts,
│             update_session_cwd, backfill_repo_roots
│
├── [552-700] Session Titles (5 методов)
│             set_session_title, get_session_title, get_session_by_title,
│             resolve_session_by_title, get_next_title_in_lineage
│
├── [702-950] Session Listing & Counting (8 методов)
│             list_sessions_rich, list_cron_job_runs, search_sessions,
│             search_sessions_by_id, session_count, distinct_session_cwds,
│             get_compression_tip, resolve_resume_session_id
│
├── [952-1500] Message Storage (15 методов) ← CRITICAL
│             append_message, get_messages, get_messages_as_conversation,
│             get_messages_around, get_anchored_view, replace_messages,
│             archive_and_compact, rewind_to_message, restore_rewound,
│             list_recent_user_messages, clear_messages, message_count,
│             has_platform_message_id, ...
│
├── [1502-1900] Search (3 метода) ← CRITICAL
│             search_messages, hybrid_search_sessions, _build_snippets
│
├── [1902-2100] Compression Locks (5 методов) ← CRITICAL
│             try_acquire_compression_lock, refresh_compression_lock,
│             release_compression_lock, get_compression_lock_holder,
│             _cleanup_expired_locks
│
├── [2102-2400] Session Deletion & Maintenance (10 методов)
│             delete_session, delete_sessions, delete_empty_sessions,
│             count_empty_sessions, prune_sessions, delete_session_if_empty,
│             vacuum, maybe_auto_prune_and_vacuum,
│             prune_empty_ghost_sessions, finalize_orphaned_compression_sessions
│
├── [2402-2500] Compression Cooldown (3 метода)
│             record_compression_failure_cooldown,
│             get_compression_failure_cooldown,
│             clear_compression_failure_cooldown
│
├── [2502-2600] Meta Store (2 метода)
│             get_meta, set_meta
│
├── [2602-2800] Handoff (6 методов)
│             request_handoff, get_handoff_state, list_pending_handoffs,
│             claim_handoff, complete_handoff, fail_handoff
│
├── [2802-3200] Telegram Topic Mode (11 методов)
│             apply_telegram_topic_migration,
│             enable_telegram_topic_mode, disable_telegram_topic_mode,
│             is_telegram_topic_mode_enabled,
│             bind_telegram_topic, get_telegram_topic_binding,
│             get_telegram_topic_binding_by_session,
│             list_telegram_topic_bindings_for_chat,
│             delete_telegram_topic_binding,
│             is_telegram_session_linked_to_topic,
│             list_unlinked_telegram_sessions_for_user
│
├── [3202-3400] Export (2 метода)
│             export_session, export_all
│
└── [3402-3500] Private Helpers
              _encode_content, _decode_content, _now,
              _sanitize_title, _timestamp_to_dict,
              _session_to_dict, _message_to_dict
```

---

## Файл `hermes_cli/arcadedb_helpers.py` (~150 строк)

Shared утилиты между ArcadedbSessionDB и ArcadedbKanbanDB.

```python
# hermes_cli/arcadedb_helpers.py

import json
import time
from typing import Any

_CONTENT_JSON_PREFIX = "\x00json:"
MAX_TITLE_LENGTH = 100

def _now() -> float:
    """Current epoch as float."""
    return time.time()

def _encode_content(content: Any) -> str:
    """
    Encodes content for storage. Mirrors SessionDB._encode_content().

    Multimodal content (list/dict) → "\x00json:" + json.dumps(content)
    String/None pass through unchanged.
    """
    if isinstance(content, (list, dict)):
        return _CONTENT_JSON_PREFIX + json.dumps(content, ensure_ascii=False)
    if content is None:
        return None
    return str(content)

def _decode_content(content: str) -> Any:
    """
    Decodes content from storage. Mirrors SessionDB._decode_content().

    "\x00json:..." → json.loads() → list/dict (multimodal parts)
    Other strings → return as-is.
    """
    if content is None:
        return None
    if isinstance(content, str) and content.startswith(_CONTENT_JSON_PREFIX):
        return json.loads(content[len(_CONTENT_JSON_PREFIX):])
    return content

def _sanitize_title(title: str) -> str | None:
    """Validates and sanitizes session title."""
    if not title or not isinstance(title, str):
        return None
    title = title.strip()
    if not title:
        return None
    # Strip control characters
    import re
    title = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', title)
    return title[:MAX_TITLE_LENGTH]

def _maybe_epoch(val: Any) -> float:
    """Convert ISO datetime string to epoch float. Pass through for numbers."""
    import re
    import calendar, datetime
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", val)
        if match:
            try:
                dt = datetime.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                return calendar.timegm(dt.timetuple()) + dt.microsecond / 1_000_000
            except ValueError:
                pass
    return val

def _format_timestamp(ts: float | int | str | None) -> str:
    """Human-readable date from epoch. Mirrors session_search_tool._format_timestamp()."""
    if ts is None:
        return "unknown"
    try:
        from datetime import datetime
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(ts)
            return dt.strftime("%B %d, %Y at %I:%M %p")
        return str(ts)
    except (ValueError, OSError, OverflowError):
        return str(ts)
```

### Связи
- **[`hermes_state.py:_encode_content`](../../hermes_state.py)** — reference implementation
- **[`tools/session_search_tool.py:_format_timestamp`](../../tools/session_search_tool.py)** — reference
- Используется в: Phase 3 (ArcadedbSessionDB), Phase 5 (Migration), Phase 6 (KanbanDB)

---

## Критические методы — Implementation Details

### 1. `append_message()` — самый горячий write path

```python
def append_message(self, session_id, role, content=None, ...) -> int:
    """
    Appends a message. Returns message @rid as int-compatible.

    SQLite: returns AUTOINCREMENT id (int)
    ArcadeDB: returns @rid (string like "#12:3") → hash to int
    """
    ts = timestamp or _now()
    content_encoded = _encode_content(content)

    # JSON serialise structured fields
    tool_calls_json = json.dumps(tool_calls) if tool_calls else None
    reasoning_details_json = json.dumps(reasoning_details) if reasoning_details else None
    codex_reasoning_json = json.dumps(codex_reasoning_items) if codex_reasoning_items else None
    codex_message_json = json.dumps(codex_message_items) if codex_message_items else None

    def _do(cur):
        # Insert Message vertex
        cur.execute("""
            CREATE VERTEX Message SET
                session_id = %s, content = %s, role = %s,
                timestamp = %s, token_count = %s, finish_reason = %s,
                reasoning = %s, reasoning_content = %s,
                reasoning_details = %s, codex_reasoning_items = %s,
                codex_message_items = %s, tool_calls = %s,
                tool_call_id = %s, tool_name = %s,
                platform_message_id = %s, observed = %s,
                active = 1, compacted = 0
        """, (
            session_id, content_encoded, role,
            ts, token_count, finish_reason,
            reasoning, reasoning_content,
            reasoning_details_json, codex_reasoning_json,
            codex_message_json, tool_calls_json,
            tool_call_id, tool_name,
            platform_message_id, int(observed),
        ))

        # Get the created @rid
        cur.execute("""
            SELECT @rid, id FROM Message
            WHERE session_id = %s AND timestamp = %s AND role = %s
            ORDER BY @rid DESC LIMIT 1
        """, (session_id, ts, role))
        msg = cur.fetchone()
        msg_rid = msg["@rid"]

        # Create HAS_MESSAGE edge
        cur.execute("""
            CREATE EDGE HAS_MESSAGE FROM
            (SELECT FROM Session WHERE id = %s) TO
            (SELECT FROM Message WHERE @rid = %s)
            SET seq = 0, role = %s, tokens = %s, created_at = %s
        """, (session_id, msg_rid, role, len(content.split()) if content else 0, ts))

        # Update session counters
        num_tc = len(tool_calls) if tool_calls else 0
        cur.execute(
            "UPDATE Session SET message_count = message_count + 1 WHERE id = %s",
            (session_id,)
        )
        if tool_name:
            cur.execute(
                "UPDATE Session SET tool_call_count = tool_call_count + %s WHERE id = %s",
                (num_tc, session_id)
            )

        # Return int-compatible ID (hash @rid for API compatibility)
        return hash(msg_rid) & 0x7FFFFFFF  # positive 32-bit int

    return self._adapter.transact(_do)
```

**Проблема:** `@rid` — это строка (`#12:3`), а `SessionDB.append_message()` возвращает `int` (AUTOINCREMENT).

**Решение:** хешируем `@rid` в положительный 32-bit int для обратной совместимости.

**Альтернатива:** добавить `id` property (INTEGER AUTOINCREMENT) на Message vertex. ArcadeDB не поддерживает AUTOINCREMENT на vertex types, но можно использовать:
```sql
CREATE SEQUENCE message_seq
-- затем:
id = message_seq.next()
```
**Рекомендация:** использовать хеш `@rid` для минимальных изменений в consumers.

### 2. `replace_messages()` — атомарная замена

```python
def replace_messages(self, session_id, messages: list[dict]) -> None:
    """
    Atomically replaces all messages in a session.

    SQLite: DELETE + INSERT in one transaction.
    ArcadeDB: DELETE VERTEX (cascade edges) + re-INSERT.

    Это деструктивная операция — старые сообщения удаляются полностью.
    """
    def _do(cur):
        # 1. Удалить все сообщения сессии (и их HAS_MESSAGE edges cascade)
        cur.execute(
            "DELETE VERTEX Message WHERE session_id = %s",
            (session_id,)
        )
        # HAS_MESSAGE edges удаляются автоматически при удалении vertex

        # 2. Сбросить counters
        cur.execute(
            "UPDATE Session SET message_count = 0, tool_call_count = 0 WHERE id = %s",
            (session_id,)
        )

        # 3. Перевставить replacement messages
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
                "CREATE VERTEX Message SET session_id = %s, role = %s, "
                "content = %s, timestamp = %s, tool_calls = %s, active = 1",
                (session_id, role, content, ts, tc_json)
            )
            new_msg_count += 1
            new_tc_count += num_tc

        # 4. Обновить counters
        cur.execute(
            "UPDATE Session SET message_count = %s, tool_call_count = %s WHERE id = %s",
            (new_msg_count, new_tc_count, session_id)
        )

    self._adapter.transact(_do)
```

### 3. `search_messages()` — FTS5→Lucene адаптация

```python
def search_messages(self, query, source_filter=None, exclude_sources=None,
                    role_filter=None, limit=20, offset=0, sort=None,
                    include_inactive=False) -> list[dict]:
    """
    Full-text search over messages.

    Path 1: FULL_TEXT (Lucene) для не-CJK запросов
    Path 2: LIKE fallback для CJK или коротких запросов
    Path 3 (future): hybrid (dense vector + fulltext)

    Возвращает list[dict] с полями:
        id, session_id, role, snippet, content (popped),
        timestamp, tool_name, source, model, session_started
    """
    # Detect CJK
    has_cjk = any('\u4e00' <= c <= '\u9fff' or '\u3040' <= c <= '\u309f'
                   or '\uac00' <= c <= '\ud7af' for c in query)

    # Путь 1: Lucene FULL_TEXT
    if not has_cjk:
        # SEARCH_INDEX возвращает @rid с BM25 rank
        # Строим Lucene-compatible запрос
        lucene_query = _to_lucene_query(query)  # преобразует FTS5→Lucene синтаксис
        sql = """
            SELECT session_id, role,
                   content, timestamp, tool_name,
                   s.source, s.model, s.started_at AS session_started
            FROM Message
            LET s = (SELECT FROM Session WHERE id = $parent.current.session_id)
            WHERE SEARCH_INDEX('Message[content]', %s) = true
              AND (active = 1 OR compacted = 1)
            ORDER BY $score DESC
            LIMIT %s OFFSET %s
        """
        rows = self._adapter.query(sql, {"q": lucene_query, "l": limit, "o": offset})
    else:
        # Путь 2: LIKE fallback для CJK
        like_query = f"%{query}%"
        sql = """
            SELECT session_id, role,
                   content, timestamp, tool_name,
                   s.source, s.model, s.started_at AS session_started
            FROM Message
            LET s = (SELECT FROM Session WHERE id = $parent.current.session_id)
            WHERE (content LIKE %s ESCAPE '\\' OR tool_name LIKE %s ESCAPE '\\')
              AND (active = 1 OR compacted = 1)
            ORDER BY timestamp DESC
            LIMIT %s OFFSET %s
        """
        rows = self._adapter.query(sql, {"lq": like_query, "l": limit, "o": offset})

    # Post-processing: snippets + context window
    results = []
    for row in rows:
        snippet = self._build_snippet(row["content"], query)
        context = self._build_context_window(row["session_id"], row["timestamp"])
        results.append({
            "id": hash(str(row.get("@rid", ""))) & 0x7FFFFFFF,
            "session_id": row["session_id"],
            "role": row["role"],
            "snippet": snippet,
            "timestamp": row["timestamp"],
            "tool_name": row.get("tool_name"),
            "source": row.get("source"),
            "model": row.get("model"),
            "session_started": row.get("session_started"),
            "context_before": context.get("before"),
            "context_after": context.get("after"),
        })

    return results

def _build_snippet(self, content: str, query: str, max_tokens: int = 40) -> str:
    """
    Python-side snippet generation.

    Ищет первое вхождение query в content, возвращает окно ±20 токенов
    с маркерами >>> и <<< (совместимо с FTS5 snippet()).

    ArcadeDB Lucene не предоставляет snippet() через SQL.
    """
    if not content or not query:
        return (content or "")[:max_tokens * 6]

    # Найти позицию query (case-insensitive)
    pos = content.lower().find(query.lower())
    if pos < 0:
        return content[:max_tokens * 6] + "..."

    # Окно вокруг найденной позиции (~max_tokens токенов)
    words = content.split()
    query_words = query.split()
    total_words = len(words)

    # Найти примерный word index
    prefix = content[:pos]
    word_idx = len(prefix.split())

    start = max(0, word_idx - max_tokens // 2)
    end = min(total_words, word_idx + len(query_words) + max_tokens // 2)

    snippet_words = words[start:end]
    snippet = " ".join(snippet_words)

    prefix_str = "..." if start > 0 else ""
    suffix_str = "..." if end < total_words else ""

    # Обернуть query в маркеры
    import re
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    snippet = pattern.sub(f">>>{query}<<<", snippet, count=1)

    return prefix_str + snippet + suffix_str

def _to_lucene_query(self, fts5_query: str) -> str:
    """
    Преобразует FTS5 query syntax в Lucene query syntax.

    FTS5:  "exact phrase" deploy* AND OR NOT
    Lucene: "exact phrase" deploy*  AND OR NOT  (совместимо)
    """
    # FTS5 и Lucene используют совместимый синтаксис для базовых операций
    # Проходим as-is для простых запросов
    return fts5_query.strip()
```

### 4. `try_acquire_compression_lock()` — CAS протокол

```python
def try_acquire_compression_lock(self, session_id, holder, ttl_seconds=300) -> bool:
    """
    Пытается атомарно захватить lock на сессию для compression.

    Протокол (один atomic transaction):
      1. DELETE expired locks
      2. INSERT lock (или fail если уже есть)
      3. SELECT → verify holder == self

    Returns True если lock захвачен.
    """
    now_ts = _now()
    expires = now_ts + ttl_seconds

    def _do(cur):
        # 1. Удалить истёкшие locks
        cur.execute(
            "DELETE FROM CompressionLock WHERE session_id = %s AND expires_at < %s",
            (session_id, now_ts)
        )

        # 2. Попробовать вставить lock
        cur.execute(
            "INSERT INTO CompressionLock SET session_id = %s, holder = %s, "
            "acquired_at = %s, expires_at = %s",
            (session_id, holder, now_ts, expires)
        )
        # Если уже существует — ArcadeDB выбросит duplicate key error
        # Ловим exception → значит lock уже занят

        # 3. Проверить что мы владелец
        cur.execute(
            "SELECT holder FROM CompressionLock WHERE session_id = %s",
            (session_id,)
        )
        rows = cur.fetchall()
        if rows:
            return rows[0]["holder"] == holder
        return False

    try:
        return self._adapter.transact(_do)
    except ArcadeDBError as e:
        if "duplicate" in str(e).lower() or "already exists" in str(e).lower():
            return False
        raise

# Альтернативная реализация через UPSERT (если ArcadeDB поддерживает ON CONFLICT):
def try_acquire_compression_lock_v2(self, session_id, holder, ttl_seconds=300) -> bool:
    """Через UPSERT + RETURNING — чище."""
    now_ts = _now()
    expires = now_ts + ttl_seconds

    def _do(cur):
        cur.execute(
            "DELETE FROM CompressionLock WHERE session_id = %s AND expires_at < %s",
            (session_id, now_ts)
        )
        # UPSERT: создать если нет, или обновить если истёк
        cur.execute("""
            UPDATE CompressionLock
            SET holder = %s, acquired_at = %s, expires_at = %s
            WHERE session_id = %s AND (holder = %s OR expires_at < %s)
            RETURNING holder
        """, (holder, now_ts, expires, session_id, holder, now_ts))

        rows = cur.fetchall()
        if rows:
            return rows[0]["holder"] == holder

        # Не получилось — возможно lock существует и не истёк
        cur.execute(
            "SELECT holder, expires_at FROM CompressionLock WHERE session_id = %s",
            (session_id,)
        )
        rows = cur.fetchall()
        if rows:
            return rows[0]["expires_at"] < now_ts  # истёк? → можно было бы
        return False

    return self._adapter.transact(_do)
```

---

## `tools/session_search_tool.py` адаптация

**Изменения в `_init_graph_store()`:**

```python
# Было: env vars
_ARCADE_HOST = os.environ.get("ARCADE_HOST", "localhost")
_ARCADE_PORT = int(os.environ.get("ARCADE_PORT", "2480"))

# Стало: config.yaml
def _init_graph_store():
    global _GRAPH_STORE_CACHE

    if _GRAPH_STORE_CACHE is not None:
        return _GRAPH_STORE_CACHE if _GRAPH_STORE_CACHE else None

    try:
        from hermes_cli.config import load_config
        config = load_config()
        arcadedb_cfg = config.get("database", {}).get("arcadedb", {})

        if not arcadedb_cfg.get("enabled", False):
            _GRAPH_STORE_CACHE = False
            return None

        from hermes_cli.arcadedb import ArcadeDBConfig, ArcadeDBAdapter
        from hermes_cli.graph_store import GraphStore
        from hermes_cli.embedder import create_embedder

        db_config = ArcadeDBConfig(
            host=arcadedb_cfg["host"],
            port=arcadedb_cfg["port"],
            database=arcadedb_cfg["database"],
            user=arcadedb_cfg["user"],
            password=arcadedb_cfg["password"],
        )
        adapter = ArcadeDBAdapter(db_config)
        adapter.connect()

        embedder = create_embedder(config.get("auxiliary", {}).get("embedding", {}))
        embedder.initialize()

        store = GraphStore(adapter, embedder)
        _GRAPH_STORE_CACHE = store
        return store

    except Exception as e:
        logging.debug("GraphStore not available (falling back to FTS5): %s", e)
        _GRAPH_STORE_CACHE = False
        return None
```

### Связи
- **[`tools/session_search_tool.py:_init_graph_store`](../../tools/session_search_tool.py)**
- **[`hermes_cli/config.py:load_config`](../../hermes_cli/config.py)**
- **[→ Phase 0: config block](phase-0-lifecycle.md#config-block)**

---

## Тест-кейсы

**Тестовый файл:** `tests/test_arcadedb_session.py` → [см. Phase 1: session tests](phase-1-testing.md#файл-4-teststest_arcadedb_sessionpy)

### Сводная карта тестов (72 тест-кейса)

| Группа | Кол-во тестов | ID range | Ключевые проверки |
|--------|-------------|----------|-------------------|
| Session Lifecycle | 8 | S3-01 – S3-08 | CRUD, resolve, reopen |
| Message CRUD | 12 | S3-09 – S3-20 | append, get, replace, JSON content, atomicity |
| Compaction & Undo | 6 | S3-21 – S3-26 | soft-delete, rewind, restore |
| Search | 8 | S3-27 – S3-34 | BM25, snippets, CJK, filters, sort |
| Hybrid Search | 4 | S3-35 – S3-38 | dense, fulltext, fuse, filters |
| Compression Locks | 5 | S3-39 – S3-43 | acquire, conflict, refresh, release |
| Session Meta | 6 | S3-44 – S3-49 | model_config, tokens, cwd |
| Session Listing | 5 | S3-50 – S3-54 | rich listing, search, count |
| Session Titles | 5 | S3-55 – S3-59 | set, duplicate, lineage |
| Session Deletion | 4 | S3-60 – S3-63 | delete, cascade, prune |
| Handoff | 4 | S3-64 – S3-67 | request, claim, complete, fail |
| Archival | 3 | S3-68 – S3-70 | archive, vacuum, auto-prune |
| Export | 2 | S3-71 – S3-72 | export session, export all |

---

## Acceptance Criteria

- [ ] `ArcadedbSessionDB` реализует все 80+ методов с идентичным API `SessionDB`
- [ ] `append_message()` возвращает int (хеш @rid)
- [ ] `replace_messages()` атомарно заменяет все сообщения
- [ ] `search_messages()` возвращает результаты с snippets и контекстом
- [ ] CJK поиск работает через LIKE fallback
- [ ] Compression locks: atomic CAS (MVCC)
- [ ] Telegram topic tables: полный цикл enable→bind→unbind→disable
- [ ] Все 72 теста из `test_arcadedb_session.py` проходят
- [ ] `session_search_tool.py` читает конфиг из config.yaml (не env vars)
- [ ] Schema additions добавлены в `arcadedb_schema.py`

---

## Cross-References

### Предшествующие фазы
- **[← Phase 1: Testing](phase-1-testing.md)** — 72 теста определяют API контракт
- **[← Phase 2: Adapter v2](phase-2-adapter-v2.md)** — `ArcadeDBAdapter` используется во всех методах

### Последующие фазы
- **[→ Phase 4: Consumer Migration](phase-4-consumers.md)** — все consumers переключаются на `ArcadedbSessionDB`
- **[→ Phase 5: Migration Tool](phase-5-migration-tool.md)** — использует `ArcadedbSessionDB` для write path
- **[→ Phase 6: KanbanDB](phase-6-kanbandb.md)** — использует тот же `ArcadeDBAdapter`
- **[→ Phase 7: Memory Store](phase-7-memory-store.md)** — используют общие helpers
- **[→ Phase 8: Other DBs](phase-8-other-dbs.md)** — используют общие helpers

### Связи с существующими файлами
- **[`hermes_state.py:SessionDB`](../../hermes_state.py)** — reference API (5658 строк)
- **[`hermes_cli/arcadedb.py:ArcadeDBAdapter`](../../hermes_cli/arcadedb.py)** — adapter (Phase 2)
- **[`hermes_cli/arcadedb_schema.py`](../../hermes_cli/arcadedb_schema.py)** — schema additions
- **[`hermes_cli/embedder.py:EmbedderProvider`](../../hermes_cli/embedder.py)** — для поиска
- **[`hermes_cli/graph_store.py:GraphStore`](../../hermes_cli/graph_store.py)** — для гибридного поиска
- **[`tools/session_search_tool.py`](../../tools/session_search_tool.py)** — адаптируется
- **[`hermes_cli/config.py`](../../hermes_cli/config.py)** — database.arcadedb config
- **[`hermes_constants.py`](../../hermes_constants.py)** — get_hermes_home()

### Связи внутри документации
- **[Phase 1: test_arcadedb_session.py](phase-1-testing.md#файл-4-teststest_arcadedb_sessionpy)** — все тесты
- **[Phase 2: transact() API](phase-2-adapter-v2.md#transaction-api)** — используется для атомарности

---

## Implementation Sequence

```
1. hermes_cli/arcadedb_helpers.py → shared utilities
2. hermes_cli/arcadedb_schema.py → новые индексы + vertex types
3. ArcadedbSessionDB.__init__() + close()
4. Session CRUD (8 методов)
5. Message CRUD (15 методов) ← CRITICAL
6. Search (3 метода) ← CRITICAL
7. Compression Locks (5 методов) ← CRITICAL
8. Session Meta (7 методов)
9. Session Listing (8 методов)
10. Session Titles (5 методов)
11. Session Deletion (10 методов)
12. Compression Cooldown (3 метода)
13. Meta Store (2 метода)
14. Handoff (6 методов)
15. Telegram Topics (11 методов)
16. Export (2 метода)
17. tools/session_search_tool.py → config-based init
18. Сделать тесты зелёными (S3-01 → S3-72)
```

## Notes

- **Message ID:** хеш `@rid` → 32-bit int для обратной совместимости. В будущем можно перейти на `@rid` string
- **Edge traversal:** `HAS_MESSAGE` edges создаются при каждом `append_message()`. Для `get_messages()` можно использовать edge traversal или `session_id` index — оба должны работать
- **FULL_TEXT index:** должен быть создан до первого search запроса. SchemaManager в `ensure_schema()` создаёт его
- **Snippet generation:** Python-side из-за отсутствия Lucene `snippet()` через SQL. Performance impact минимален (post-processing N результатов)
- **CJK:** LIKE fallback с ESCAPE. ArcadeDB поддерживает LIKE с `ESCAPE '\'`
- **Lazy embedder:** EmbedderProvider инициализируется лениво (только для search) чтобы не грузить модель при простых CRUD операциях
