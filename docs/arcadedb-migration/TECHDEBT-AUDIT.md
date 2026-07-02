# ArcadeDB Migration — Tech Debt Audit (Code Review)

## Second Audit (03.07.2026)

After implementing Blocks 1-7. **Verification against original audit.**

| Verdict | Count | Details |
|---------|-------|---------|
| **FIXED** | 12 | AUD-1,2,3,4,5,6,7,9,11,13,29 + NEW-1 |
| **PARTIAL** | 1 | AUD-8 (claim_handoff — not atomic yet) |
| **UNCHANGED** | 22 | AUD-10,12,14,15,16,17,18,19,21,22,23,24,25,26,28,30 + LOSS-1..7 |
| **NEW** | 3 | NEW-2 (lock unused), NEW-3 (LIKE fetch unfiltered), NEW-4 (handoff TOCTOU) |

**Progress: 13/35 fixed (37%)** | **3 new issues** | **Total: 25 remaining**

**Key improvements since first audit:**
- Schema: all 7 vertex types + 5 properties + dynamic VECTOR_DIM
- Search: filters working across all paths, OFFSET→SKIP globally
- Embedding: `_vec()` in memory store, auto-embed on `append_message`
- Transactions: soft-delete in `clear_messages`, pool lock initialized
- Kanban: comment edge fixed
- API: auth with `verify_api_key`
- Tests: search subsystem now 10/10 (was 0/10); 65/77 overall (84%)

**Largest remaining gaps:**
1. AUD-10 (rewind @rid) — HIGH: /undo gives wrong range for cross-cluster messages
2. AUD-14 (pool lock) — HIGH: lock exists but never acquired — false safety
3. AUD-15 (_fmt_tuple naïve split) — HIGH: %s in string content breaks SQL
4. AUD-16 (no pruning) — HIGH: VerificationEvent unbounded growth
5. LOSS-7 (x00json: compat) — HIGH: SQLite data unreadable in ArcadeDB

---

## First Audit (01.07.2026)

Результат ревью всей написанной кодовой базы ArcadeDB-миграции
(02.07.2026). 37 пунктов, сгруппированных по приоритету и модулю.

---

## CRITICAL (8)

### AUD-1: `arcadedb_schema.py` — 7 vertex types missing from schema

**Файл:** `hermes_cli/arcadedb_schema.py:VERTICES`

**Проблема:** Следующие vertex types используются в других модулях, но не определены в `VERTICES`:
`ProjectFolder`, `DiscoveredRepo`, `Response`, `Conversation`,
`VerificationEvent`, `VerificationState`, `PendingIngest`.

ArcadeDB не позволяет `CREATE VERTEX TypeName SET ...` для незарегистрированного типа.
Все операции этих модулей падают с `Type with name 'X' was not found`.

**Затронутые модули:** `arcadedb_projects.py`, `api_server.py:ArcadedbResponseStore`,
`verification_evidence.py:ArcadedbVerificationStore`, `retaindb/__init__.py:ArcadedbWriteQueue`

**Исправление:** Добавить все 7 vertex types в `VERTICES` dict с их свойствами и индексами.

---

### AUD-2: `arcadedb_schema.py` — 5 Project properties missing

**Файл:** `hermes_cli/arcadedb_schema.py:~275` (Project vertex definition)

**Проблема:** `arcadedb_projects.py` ссылается на свойства, отсутствующие в схеме:
`slug`, `icon`, `color`, `primary_path`, `archived`. Все CRUD операции падают.

**Исправление:** Добавить свойства в `VERTICES["Project"]["props"]`.

---

### AUD-3: `arcadedb_schema.py` — `_VECTOR_DIM=1024` hardcoded

**Файл:** `hermes_cli/arcadedb_schema.py:30`

**Проблема:** Размерность векторного индекса жёстко 1024 (e5-large).
OpenAI text-embedding-3-small даёт 1536d, large — 3072d, Ollama nomic-embed-text — 768d.
При смене эмбеддера индекс отклоняет векторы другой размерности.

**Исправление:** Сделать `_VECTOR_DIM` конфигурируемым или авто-определяемым
при создании индекса.

---

### AUD-4: `arcadedb_session.py` — `search_messages` ignores filters

**Файл:** `hermes_cli/arcadedb_session.py:893-976`

**Проблема:** Параметры `source_filter`, `exclude_sources`, `role_filter`, `sort`
принимаются, но **не применяются** ни в одном из трёх search-путей (vector, CJK LIKE, LIKE).
Вызывающий код получает нефильтрованные результаты.

**Исправление:** Добавить WHERE-условия для source/role/exclude фильтров во все ветки.

---

### AUD-5: `arcadedb_store.py` — embedding serialized via `_q()` not `_vec()`

**Файл:** `plugins/memory/holographic/arcadedb_store.py:40`

**Проблема:** `_q(emb.dense)` превращает список float'ов в строку `'[0.1, 0.2, ...]'`
(quoted string), а не в JSON-array `[0.1, 0.2, ...]`. ArcadeDB `LIST`-тип
принимает второе, но не первое. Все embedding'и Fact хранятся как строки,
векторный поиск сломан.

**Исправление:** `ArcadeDBAdapter._vec(emb.dense)` вместо `_q(emb.dense)`.

---

### AUD-6: `arcadedb_kanban.py` — comment edge links to TaskRun, not TaskComment

**Файл:** `hermes_cli/arcadedb_kanban.py:273-275`

**Проблема:** `CREATE EDGE HAS_COMMENT FROM Task TO TaskRun` — цель ребра TaskRun,
а должна быть TaskComment. Comment vertex создан, но edge ведёт к несуществующему
или случайному TaskRun. Комментарии теряются.

**Исправление:** `TaskRun` → `TaskComment`.

---

### AUD-7: `arcadedb_session.py` — `clear_messages` uses DELETE VERTEX (known hang)

**Файл:** `hermes_cli/arcadedb_session.py:862`

**Проблема:** `DELETE VERTEX Message WHERE session_id = %s` — зависает на
edge cascade (TD-4/18). Метод `replace_messages` уже использует soft-delete,
но `clear_messages` — нет. Вызов приводит к дедлоку.

**Исправление:** `UPDATE Message SET active = 0, compacted = 1` вместо DELETE VERTEX.

---

### AUD-8: `arcadedb_session.py` — `claim_handoff` always returns True

**Файл:** `hermes_cli/arcadedb_session.py:1300-1306`

**Проблема:** `UPDATE ... WHERE id = %s AND handoff_state = 'pending'` —
возвращает `True` без проверки `cur.rowcount`. Два конкурентных claimant'а
оба думают что захватили handoff.

**Исправление:** Проверять rowcount или делать SELECT после UPDATE для верификации.

---

## HIGH (8)

### AUD-9: `arcadedb_session.py` — OFFSET instead of SKIP

**Файл:** `hermes_cli/arcadedb_session.py:388, 399`

**Проблема:** `search_sessions` и `list_cron_job_runs` используют `OFFSET`.
ArcadeDB требует `SKIP`. OFFSET молча игнорируется — пагинация сломана.

**Исправление:** `OFFSET` → `SKIP`.

---

### AUD-10: `arcadedb_session.py` — @rid string comparison in rewind

**Файл:** `hermes_cli/arcadedb_session.py:798`

**Проблема:** `WHERE @rid >= %(rid)s` — строковое сравнение, не числовое.
`"#12:3" < "#9:15"` (лексикографически). /undo захватывает неправильный диапазон.

**Исправление:** Использовать timestamp для определения порядка сообщений.

---

### AUD-11: `arcadedb_session.py` — `restore_rewound` always returns 0

**Файл:** `hermes_cli/arcadedb_session.py:837`

**Проблема:** Метод выполняет UPDATE но возвращает `0` с комментарием `# simplified`.
Вызывающий код думает что ни одно сообщение не восстановлено.

**Исправление:** Вернуть реальное количество восстановленных строк.

---

### AUD-12: `arcadedb_session.py` — `replace_messages` missing fields

**Файл:** `hermes_cli/arcadedb_session.py:702-740`

**Проблема:** При замене сообщений не копируются: `platform_message_id`,
`codex_reasoning_items`, `codex_message_items`, `reasoning_details`, `observed`.
Также не создаются HAS_MESSAGE edges для новых Message vertices.

**Исправление:** Добавить все поля в CREATE VERTEX, создавать HAS_MESSAGE edges.

---

### AUD-13: `arcadedb_schema.py` — composite index with metadata not stringified

**Файл:** `hermes_cli/arcadedb_schema.py:565-573`

**Проблема:** `(("session_id", "platform_message_id"), "NOTUNIQUE", "METADATA {...}")` —
в else-ветке `idx_def[0]` это tuple, не строка. SQL: `CREATE INDEX ON Message (('session_id', 'platform_message_id')) NOTUNIQUE ...` — двойные скобки, синтаксическая ошибка.

**Исправление:** Добавить `isinstance(props_str, tuple)` проверку в else-ветку.

---

### AUD-14: `arcadedb.py` — TOCTOU race on `self._pool`

**Файл:** `hermes_cli/arcadedb.py:59-131`

**Проблема:** `connect()`, `close()`, `execute()`, `transact()` читают/пишут `self._pool`
без блокировки. Два потока: concurrent `connect()` создают два пула; concurrent
`close()` + `execute()` → `AttributeError: 'NoneType' has no attribute 'putconn'`.

**Исправление:** Добавить `threading.Lock` вокруг всех операций с `self._pool`.

---

### AUD-15: `arcadedb.py` — `_fmt_tuple` splits `%s` inside string literals

**Файл:** `hermes_cli/arcadedb.py:295`

**Проблема:** `sql.split("%s")` не отличает плейсхолдеры от литерального `%s`
внутри строк. `"INSERT INTO t SET desc = 'Value: %s'"` — `%s` внутри кавычек
заменяется параметром. Некорректный SQL без ошибки.

**Исправление:** Заменить split на парсинг с учётом строковых литералов,
или перейти к генерации SQL без `%s` плейсхолдеров.

---

### AUD-16: `verification_evidence.py` — no pruning

**Файл:** `agent/verification_evidence.py:ArcadedbVerificationStore`

**Проблема:** SQLite-версия имеет трёхуровневый pruning (100 событий на сессию,
30 дней возраст, 10,000 всего). ArcadeDB-версия — нет. VerificationEvent
растут неограниченно.

**Исправление:** Реализовать эквивалентную логику pruning.

---

## MEDIUM (14)

### AUD-17: `arcadedb.py` — `transact`: `conn.close()` suppresses original exception

**Файл:** `hermes_cli/arcadedb.py:161`

**Проблема:** Если `conn.close()` бросает исключение после ROLLBACK, оно заменяет
оригинальное исключение из `fn(cur)`. Вызывающий код видит ошибку закрытия соединения
вместо реальной причины сбоя.

**Исправление:** Обернуть `conn.close()` в `try/except: pass`.

---

### AUD-18: `arcadedb.py` — retry triggers on error-text matching

**Файл:** `hermes_cli/arcadedb.py:213`

**Проблема:** `"Transaction not active" in str(e)` — fragile string match.
Локализация, изменение текста ошибки ArcadeDB — ломает retry.

**Исправление:** Триггерить на `psycopg.OperationalError` (connection-level ошибки).

---

### AUD-19: `arcadedb.py` — `_retry()` is dead code

**Файл:** `hermes_cli/arcadedb.py:335-349`

**Проблема:** Метод `_retry()` определён, но ни разу не вызывается.
`_MAX_RETRIES = 3` используется только здесь.

**Исправление:** Удалить или подключить к `execute()`.

---

### AUD-20: `arcadedb.py` — `_fmt` silently NULLs missing keys

**Файл:** `hermes_cli/arcadedb.py:277`

**Проблема:** `params.get(key)` возвращает `None` для отсутствующих ключей →
подставляется `NULL`. Баг в вызывающем коде (опечатка в имени ключа) молча
превращается в валидный SQL с неверным NULL.

**Исправление:** `params[key]` (KeyError) или лог warning.

---

### AUD-21: `arcadedb_helpers.py` — `_rid_to_int` cross-process instability

**Файл:** `hermes_cli/arcadedb_helpers.py:125-130`

**Проблема:** `hash(rid)` зависит от `PYTHONHASHSEED` — один и тот же @rid
даёт разные int в разных процессах/рестартах. Используется как message ID.

**Исправление:** Парсить `#12:3` → `(cluster << 32) | position` для стабильного ID.

---

### AUD-22: `arcadedb_helpers.py` — `_q` doesn't handle null bytes

**Файл:** `hermes_cli/arcadedb_helpers.py:133-155`

**Проблема:** `\x00` в строке не экранируется. ArcadeDB PG протокол reject'ит
null bytes в строковых литералах. Мультимодальный контент с `\x00json:` префиксом
(мигрированный из SQLite) ломается.

**Исправление:** Добавить `val.replace("\x00", "")` в `_q()`.

---

### AUD-23: `arcadedb_session.py` — `get_messages_around` N+1 queries

**Файл:** `hermes_cli/arcadedb_session.py:640-676`

**Проблема:** Для каждого сообщения в окне делается отдельный `SELECT FROM Message
WHERE @rid = %s`. Для окна в 11 сообщений — 12 round-trips.

**Исправление:** Собрать все @rid, сделать один `WHERE @rid IN (...)` запрос.

---

### AUD-24: `arcadedb_session.py` — `find_latest_gateway_session_for_peer` missing agent_close

**Файл:** `hermes_cli/arcadedb_session.py:151-169`

**Проблема:** SQLite-версия также ищет сессии с `end_reason = 'agent_close'`
как recoverable. ArcadeDB-версия — только `ended_at IS NULL`.
Gateway не восстанавливает сессии после agent_close.

**Исправление:** Добавить `OR end_reason = 'agent_close'` в WHERE.

---

### AUD-25: `arcadedb_session.py` — `update_session_cwd` overwrites with NULL

**Файл:** `hermes_cli/arcadedb_session.py:262-269`

**Проблема:** Всегда пишет `_q(git_branch)` и `_q(git_repo_root)`, включая `NULL`
когда параметры не переданы. Частичный вызов перезаписывает предыдущие значения.

**Исправление:** Записывать только переданные (не-None) значения.

---

### AUD-26: `arcadedb_session.py` — `set_meta` non-atomic upsert

**Файл:** `hermes_cli/arcadedb_session.py:1145-1159`

**Проблема:** SELECT → DELETE → CREATE — неатомарно. Между SELECT и DELETE
другой writer может изменить тот же ключ. Дубликаты StateMeta накапливаются.

**Исправление:** Обернуть в `transact()` или использовать unique constraint.

---

### AUD-27: `arcadedb_session.py` — `__JSON__:` sentinel collision risk

**Файл:** `hermes_cli/arcadedb_helpers.py:18`

**Проблема:** Если пользовательское сообщение начинается с `__JSON__:`,
`_decode_content()` попытается распарсить остаток как JSON. Маловероятно,
но возможно.

**Исправление:** Добавить checksum/длину в префикс, напр. `__JSON__123:`, или
хранить тип контента отдельным полем.

---

### AUD-28: `arcadedb_kanban.py` — CAS depends on `cur.rowcount`

**Файл:** `hermes_cli/arcadedb_kanban.py:172-173`

**Проблема:** `if cur.rowcount == 0: return None` — ArcadeDB PG протокол может
возвращать `-1` для UPDATE (undefined behaviour). CAS сломается, два worker'а
захватят одну задачу.

**Исправление:** SELECT после UPDATE для верификации: `SELECT claim_lock FROM Task WHERE @rid = ...`.

---

### AUD-29: `openai_api.py` — no authentication

**Файл:** `hermes_cli/openai_api.py:118-143`

**Проблема:** `/v1/chat/completions` endpoint без аутентификации. Любой с доступом
к порту 9119 может использовать агента.

**Исправление:** Добавить API key проверку через FastAPI dependency injection.

---

### AUD-30: `openai_api.py` — simulated streaming

**Файл:** `hermes_cli/openai_api.py:146-196`

**Проблема:** `agent.chat(user_msg)` вызывается синхронно, ждёт полный ответ,
потом режет на слова с `await asyncio.sleep(0.05)`. Клиент ждёт всё время ответа
до получения первого чанка.

**Исправление:** Интегрироваться с реальным streaming API AIAgent или документировать
как симулированный.

---

## Функциональные потери (7)

| ID | Что потеряно | Где | Важность |
|----|-------------|-----|---------|
| LOSS-1 | FTS5 BM25 ranking | `search_messages` | Medium |
| LOSS-2 | Phrase search ("exact phrase") | `search_messages` | Low |
| LOSS-3 | Boolean operators (AND/OR/NOT) | `search_messages` | Low |
| LOSS-4 | SessionDB `optimize_fts()` | `vacuum` | Low |
| LOSS-5 | Cascade delete delegate children | `delete_session` | Medium |
| LOSS-6 | `api_call_count` field never updated | Session lifecycle | Low |
| LOSS-7 | `\x00json:` → `__JSON__:` incompatible with SQLite data | `_decode_content` | High |

---

## Статистика

| Категория | Количество |
|-----------|-----------|
| CRITICAL | 8 |
| HIGH | 8 |
| MEDIUM | 14 |
| Функциональные потери | 7 |
| **Всего** | **37** |

## Рекомендуемый порядок исправления

1. **AUD-1,2,3** (arcadedb_schema.py) — один файл, разблокирует 4 модуля + все эмбеддеры
2. **AUD-4** (search_messages filters) — критично для поиска
3. **AUD-5** (memory store _q → _vec) — критично для векторного поиска
4. **AUD-6** (kanban comment edge) — критично для kanban
5. **AUD-7** (clear_messages DELETE VERTEX) — критично для стабильности
6. **AUD-9** (OFFSET → SKIP) — ломает пагинацию
7. **AUD-10** (rewind @rid comparison) — ломает /undo
8. **AUD-14** (TOCTOU pool lock) — production thread safety
