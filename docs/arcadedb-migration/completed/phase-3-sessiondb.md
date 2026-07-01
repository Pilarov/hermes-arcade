# Phase 3: ArcadedbSessionDB — As-Built

## Файлы

| Файл | Строки | Назначение |
|------|--------|------------|
| `hermes_cli/arcadedb_session.py` | 1,415 | `ArcadedbSessionDB` — 83 публичных метода |
| `hermes_cli/arcadedb_helpers.py` | 130 | `_q()`, `_n()`, `_encode_content()`, etc. |
| `hermes_cli/arcadedb_schema.py` | +100 | 4 новых vertex types, индексы, поля |

## Реализованные методы (83)

### Session Lifecycle (8)
`create_session`, `ensure_session`, `end_session`, `reopen_session`,
`get_session`, `resolve_session_id`, `record_gateway_session_peer`,
`find_latest_gateway_session_for_peer`

### Session Metadata (7)
`update_session_meta`, `update_system_prompt`, `update_session_model`,
`update_session_billing_route`, `update_token_counts`, `update_session_cwd`,
`backfill_repo_roots`

### Session Titles (5)
`set_session_title`, `get_session_title`, `get_session_by_title`,
`resolve_session_by_title`, `get_next_title_in_lineage`, `set_session_archived`

### Session Listing (8)
`list_sessions_rich`, `list_cron_job_runs`, `search_sessions`,
`search_sessions_by_id`, `session_count`, `distinct_session_cwds`,
`get_compression_tip`, `resolve_resume_session_id`

### Message Storage (15)
`append_message`, `get_messages`, `get_messages_as_conversation`,
`get_messages_around`, `get_anchored_view`, `replace_messages`,
`archive_and_compact`, `rewind_to_message`, `restore_rewound`,
`list_recent_user_messages`, `clear_messages`, `message_count`,
`has_platform_message_id`

### Search (3)
`search_messages`, `hybrid_search_sessions`, `_build_snippet`

### Compression Locks (5)
`try_acquire_compression_lock`, `refresh_compression_lock`,
`release_compression_lock`, `get_compression_lock_holder`

### Session Deletion & Maintenance (10)
`delete_session`, `delete_sessions`, `delete_empty_sessions`,
`count_empty_sessions`, `prune_sessions`, `delete_session_if_empty`,
`vacuum`, `maybe_auto_prune_and_vacuum`, `prune_empty_ghost_sessions`,
`finalize_orphaned_compression_sessions`

### Compression Cooldown (3)
`record_compression_failure_cooldown`,
`get_compression_failure_cooldown`, `clear_compression_failure_cooldown`

### Meta Store (2)
`get_meta`, `set_meta`

### Handoff (6)
`request_handoff`, `get_handoff_state`, `list_pending_handoffs`,
`claim_handoff`, `complete_handoff`, `fail_handoff`

### Telegram Topic Mode (11)
`apply_telegram_topic_migration`, `enable_telegram_topic_mode`,
`disable_telegram_topic_mode`, `is_telegram_topic_mode_enabled`,
`bind_telegram_topic`, `get_telegram_topic_binding`,
`get_telegram_topic_binding_by_session`,
`list_telegram_topic_bindings_for_chat`, `delete_telegram_topic_binding`,
`is_telegram_session_linked_to_topic`, `list_unlinked_telegram_sessions_for_user`

### Export (2)
`export_session`, `export_all`

## Schema Additions

### Новые vertex types

```sql
CREATE VERTEX TYPE CompressionLock
  (session_id STRING, holder STRING, acquired_at DOUBLE, expires_at DOUBLE)
CREATE UNIQUE INDEX ON CompressionLock (session_id)

CREATE VERTEX TYPE StateMeta
  (key STRING, value STRING)
CREATE UNIQUE INDEX ON StateMeta (key)

CREATE VERTEX TYPE TelegramTopicMode
  (chat_id STRING, user_id STRING, enabled INTEGER, activated_at DOUBLE, ...)
CREATE UNIQUE INDEX ON TelegramTopicMode (chat_id)

CREATE VERTEX TYPE TelegramTopicBinding
  (chat_id STRING, thread_id STRING, user_id STRING, session_id STRING, ...)
CREATE UNIQUE INDEX ON TelegramTopicBinding (chat_id, thread_id)
```

### Новые поля на Session

```
compression_failure_cooldown_until DOUBLE
compression_failure_error STRING
handoff_state STRING
handoff_platform STRING
handoff_error STRING
parent_session_id STRING
billing_provider STRING
billing_base_url STRING
billing_mode STRING
```

### Новые индексы и поля на Message

```
session_id STRING                                    ← для прямого lookup
reasoning_details STRING
codex_reasoning_items STRING
codex_message_items STRING
INDEX (session_id, timestamp) NOTUNIQUE
INDEX (session_id, active, timestamp) NOTUNIQUE
INDEX (content) FULL_TEXT                          ← замена FTS5 (нестабильно)
INDEX (session_id, platform_message_id) NOTUNIQUE
```

## Отклонения от ТЗ

### 1. DELETE VERTEX → UPDATE active = 0

**ТЗ:** `DELETE VERTEX Message WHERE session_id = ...` с каскадным удалением edges.

**Реальность:** `DELETE VERTEX` зависает на ArcadeDB 26.7.1-SNAPSHOT при попытке
каскадного удаления `HAS_MESSAGE` edges.

**Решение:** Soft-delete через `UPDATE Message SET active = 0 WHERE session_id = ...`.
`replace_messages()` и `archive_and_compact()` используют этот подход.
Старые rows остаются в БД (не удаляются физически).

### 2. SEARCH_INDEX → LIKE

**ТЗ:** `SEARCH_INDEX('Message[content]', 'query')` через Lucene FULL_TEXT с BM25.

**Реальность:** `SEARCH_INDEX` вызывает зависание на ArcadeDB 26.7.1-SNAPSHOT.

**Решение:** `LIKE '%query%'` с Python-side snippet generation через `_build_snippet()`.
Нет BM25 ranking, нет phrase search.

### 3. FROM a,b → раздельные запросы

**ТЗ:** `FROM Message m, Session s WHERE m.session_id = s.id`

**Реальность:** ArcadeDB не поддерживает implicit CROSS JOIN через запятую.

**Решение:** Раздельные запросы — сначала Message, затем per-row Session lookup
с Python-кэшем `session_cache`.

### 4. Timestamp rounding

**ТЗ:** `WHERE timestamp = %s` для поиска только что вставленного сообщения.

**Реальность:** ArcadeDB округляет DOUBLE timestamp до целых секунд при хранении,
поэтому exact match не находит запись.

**Решение:** `SELECT ... WHERE session_id = %s AND role = %s ORDER BY @rid DESC LIMIT 1`
(без timestamp в WHERE).

### 5. OFFSET → SKIP, без ESCAPE

ArcadeDB SQL диалект: `LIMIT X SKIP Y` (не OFFSET), `LIKE` без `ESCAPE`.

### 6. RETURN @rid → отдельный SELECT

`CREATE VERTEX ... RETURN @rid` не поддерживается → отдельный SELECT.

### 7. Dict params → string formatting

Все `%(name)s` плейсхолдеры конвертируются через `ArcadeDBAdapter._fmt()`.
Сложные INSERT/UPDATE используют `_q()` / `_n()` из `arcadedb_helpers.py`.

## Helpers (`arcadedb_helpers.py`)

```python
_q(val)           → SQL-quote: 'escaped' или NULL
_n(val)           → number или NULL
_encode_content() → \x00json: префикс для multimodal
_decode_content() → json.loads() для \x00json: префикса
_now()            → time.time()
_sanitize_title() → валидация названий сессий
_maybe_epoch()    → ISO datetime → epoch float
_format_timestamp() → human-readable дата
_has_cjk()        → детектор CJK символов
_rid_to_int()     → @rid string → 32-bit int
```

## Интеграционный тест (10/10)

```
1. Factory             → ArcadedbSessionDB              PASSED
2. Session CRUD        → create + get                   PASSED
3. append_message      → 2 сообщения сохранены           PASSED
4. get_messages        → 2 сообщения прочитаны           PASSED
5. search_messages     → LIKE поиск работает             PASSED
6. get_messages_as_conv → OpenAI формат                 PASSED
7. replace_messages    → UPDATE active=0 атомарно        PASSED
8. Compression lock    → CAS захват + конфликт           PASSED
9. Meta store          → key-value                       PASSED
10. Export             → сессия + сообщения              PASSED
```
