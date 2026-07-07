# Phase 3: ArcadedbSessionDB — As-Built

## Файлы

| Файл | Строки | Назначение |
|------|--------|------------|
| `hermes_cli/arcadedb_session.py` | 1,819 | `ArcadedbSessionDB` — 83 метода + SearchMatter + Redis locks + LLM summary |
| `hermes_cli/arcadedb_helpers.py` | 168 | `_q()`, `_n()`, `_encode_content()`, `_decode_content()` |
| `hermes_cli/arcadedb_schema.py` | 782 | 30+ vertex types, без keywords/entity_names в SearchMatter |
| `hermes_cli/redis_lock.py` | 110 | `RedisLockManager` — SET NX EX, EVAL release, XX refresh |

## Реализованные методы (83 + дополнительные)

### Session CRUD (7)
`create_session`, `ensure_session`, `end_session`, `reopen_session`, `get_session`,
`resolve_session_id`, `delete_session`

### Messages (8)
`append_message`, `get_messages`, `get_messages_as_conversation`, `get_messages_around`,
`get_anchored_view`, `replace_messages`, `archive_and_compact`, `rewind_to_message`

### Search (4)
`search_messages`, `hybrid_search_sessions`, `search_sessions`, `search_sessions_by_id`

### Compression Locks (4) — Redis + DB fallback
`try_acquire_compression_lock`, `refresh_compression_lock`, `release_compression_lock`,
`get_compression_lock_holder`

**Redis**: SET NX EX (атомарно) → 0 XFAIL. DB fallback: UNIQUE constraint CAS.

### Compression Cooldown (4)
`record_compression_failure_cooldown`, `get_compression_failure_cooldown`,
`clear_compression_failure_cooldown`, `finalize_orphaned_compression_sessions`

### SearchMatter CQRS (3 новых)
`_create_search_matter`, `_summarize_session_dialog`, `_llm_summarize`

- Авто-создание на `end_session()` (первый вызов, проверка `ended_at IS NULL`)
- LLM-саммаризация с sliding window (по сообщениям, не по символам)
- Fallback на `_fallback_summary` при недоступности LLM
- Config: `search_matter.llm_summary`, `search_matter.sliding_window`

### Handoff (5)
`request_handoff`, `get_handoff_state`, `list_pending_handoffs`, `claim_handoff`,
`complete_handoff`, `fail_handoff`

### Titles (7)
`set_session_title`, `get_session_title`, `get_session_by_title`,
`resolve_session_by_title`, `get_next_title_in_lineage`, `set_session_archived`

### Telegram Topics (10)
`enable_telegram_topic_mode`, `disable_telegram_topic_mode`, etc.

### Meta, Tokens, Export (остальные)
`update_session_meta`, `get_meta`, `set_meta`, `update_token_counts`,
`export_session`, `export_all`, etc.

## Schema Additions

- **SearchMatter**: `session_rid` (LINK), `summary`, `embedding` (LSM_VECTOR), `profile`, `model`, `created_at`
- БЕЗ `keywords` и `entity_names` — удалены 06.07 (Lucene + векторы покрывают поиск)
- **CompressionLock**: `session_id` (UNIQUE), `holder`, `acquired_at`, `expires_at`
- **StateMeta**, **TelegramTopicMode**, **TelegramTopicBinding**

## Ключевые отклонения от ТЗ

| ТЗ | Реальность |
|----|-----------|
| FULL_TEXT Lucene = FTS5 | SEARCH_INDEX + LIKE fallback |
| entity extraction + graph edges | Отложено (Block 3 — Graph RAG) |
| keywords/entity_names в SearchMatter | Удалены — не нужны |
