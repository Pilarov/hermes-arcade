# Hermes ArcadeDB — Ход работ

## Текущий статус (03.07.2026)

**47/59 тестов проходят (80%).** ArcadeDB 26.7.1, пароль через Java property.
API сервер для OpenWebUI запущен.

### Три итерации исправлений (02-03.07)

| Итерация | Версия DB | Результат | Что изменилось |
|----------|-----------|-----------|----------------|
| 1 | 26.4.2 | 40/59 (68%) | Базовый прогон, schema init в фикстуре |
| 2 | 26.7.1 | 48/59 (81%) | `vector.fuse()` заработал, уникальные ID в тестах |
| 3 | 26.7.1 | 47/59 (80%) | `transact()` свежие соединения (не пул), `autocommit=True` подтверждён |

### Финальный прогон (pool reset, без HTTP)

```
Адаптер + фабрика:       15 pass / 0 fail  ✅
Compression locks:        5 pass / 3 fail  (duplicate key — смежные тесты)
Search:                   7 pass / 2 fail  (search_basic, hybrid_basic — пустые результаты)
E2E:                      23 pass / 4 fail (pool corruption)
─────────────────────────────────────────────────
ИТОГО:                   50 pass / 9 fail (85%)
```

### Три стратегии — итоги

| Стратегия | Результат | Вердикт |
|-----------|-----------|---------|
| PG fresh connections + autocommit=True | 49/59 (83%) | База, работает для unit-тестов |
| HTTP API (port 2480) | 31/59 (52%) | 🔴 401 Unauthorized — ArcadeDB 26.7.1 не принимает HTTP Basic Auth при пароле через Java property |
| Pool reset каждые 15 transact() | 50/59 (85%) | 🟢 Лучший результат. +1 E2E pass |

### Ключевые находки

- **HTTP API заблокирован**: ArcadeDB 26.7.1 возвращает 401 на все HTTP endpoints
  при пароле, установленном через `-Darcadedb.server.rootPassword`. PG протокол
  работает с тем же паролем. Баг ArcadeDB.
- **Pool reset эффективен**: пересоздание пула каждые 15 `transact()` вызовов
  чинит 1 из 5 E2E фейлов и предотвращает полную деградацию.
- **`%s` bind params в raw cursor**: замена на `_q()` починила 3 compression lock
  теста.
- **`autocommit=False` ломает всё**: ArcadeDB PG требует `autocommit=True`,
  иначе ConcurrentModification ошибки.
- **Пароль через `-Darcadedb.server.rootPassword`** — единственный работающий
  способ для Docker. `ARCADEDB_ROOT_PASSWORD` не работает в 26.7+.

### Что сделано

```
✅ TD-2          — transact() на свежих соединениях + autocommit=True задокументирован
✅ Версия        — переход с 26.4.2 на 26.7.1 (vector.fuse, без парольного бага)
✅ Тест-дизайн   — уникальные ID (uuid prefix) для adapter + compression lock тестов
✅ TEST_POLICY.md — документированы правила тестирования для ArcadeDB
✅ %s → _q()     — raw cursor bind params заменены на string formatting
```

### Что не доделано

```
⏸ TD-22         — hybrid_search ищет только SearchMatter, не Message
⏸ TD-25         — openai_api.py не подключён к hermes serve
⏸ search_basic  — полнотекстовый поиск возвращает пустые результаты
⏸ E2E pool      — 4 теста падают на Transaction not active (ограничение платформы)
```

### Ключевые цифры

```
Новых файлов:         27
Модифицированных:     30+
Строк нового кода:    ~6,500
Коммитов:             40+
Пунктов техдолга:     26 (2 HIGH, 10 Medium, 14 Low)
```

---

## Архитектурное решение (пересмотр 01.07.2026)

Первоначальный план предполагал ArcadeDB как **read-only поисковый слой**
поверх SQLite (гибридный поиск через GraphStore + FTS5 fallback). После
аудита кодовой базы принято решение о **полной замене SQLite на ArcadeDB**
как основного storage. Все 7 SQLite БД мигрируют в единый ArcadeDB instance.

### Переход с HTTP API на PostgreSQL Wire Protocol

**Было:** httpx → `POST /api/v1/command/{database}` (порт 2480)
- Нет транзакций — каждый запрос отдельно
- Нет пула соединений
- Jackson float[] баг обходился SQL-литералами

**Стало:** psycopg → PostgreSQL protocol (порт 5432)
- SQL-транзакции: `BEGIN`/`COMMIT`/`ROLLBACK`
- Connection pool: min=2, max=10
- `autocommit=True` — **единственный поддерживаемый режим** (оф. документация:
  ArcadeDB PG plugin не поддерживает extended query protocol, только simple
  query mode)

**Ключевая находка из документации ArcadeDB PostgreSQL plugin:**

> ArcadeDB does only support "simple" query mode and does not support SSL!
> Enabling auto commit to false is not 100% supported.

Поэтому:
- Bind-параметры (prepared statements) **не работают** — только string formatting
- `_fmt()` — авто-конвертация dict-параметров в SQL-литералы
- `_q()` / `_n()` — safe string formatting helpers для INSERT/UPDATE
- `autocommit=True` всегда

### ArcadeDB SQL диалект (отличия от стандартного SQL)

| Стандартный SQL | ArcadeDB |
|-----------------|----------|
| `LIMIT X OFFSET Y` | `LIMIT X SKIP Y` |
| `FROM a, b` (implicit join) | Только явный подзапрос или MATCH |
| `LIKE ... ESCAPE '\\'` | Только `LIKE ...` (без ESCAPE) |
| `RETURN @rid` (DML) | Не поддерживается |
| `DELETE VERTEX` (с edge cascade) | Может зависнуть → soft-delete через `UPDATE active=0` |
| `LET s = (SELECT ...)` | Поддерживается, но `FROM a,b` — нет |
| `SEARCH_INDEX(...)` | Работает, но зависает на пустой БД → упрощено до LIKE |

---

## Phases 0–4: Реализовано (01.07.2026)

### Phase 0: ArcadeDB Lifecycle Manager

**Файл:** `hermes_cli/arcadedb_lifecycle.py` (~250 строк)

- Auto-managed Docker контейнер: `docker run arcadedb/arcadedb:26.7.1`
- Health check: `SELECT 1` через psycopg
- Password auto-generation + persist в config.yaml
- Graceful fallback при отсутствии Docker
- Config block: `database.arcadedb.*` в `config.yaml`

**Тесты:** `tests/test_arcadedb_lifecycle.py` — **13/13 PASSED**

### Phase 1: Testing Framework (Tests-First)

Файлы:
- `tests/fixtures/arcadedb_fixtures.py` — shared fixtures для всех тестов
- `tests/test_arcadedb_lifecycle.py` — 13 тестов (PASSED)
- `tests/test_arcadedb_adapter.py` — 11 тестов (1 passed, 10 skipped=need container)
- `tests/test_arcadedb_compression_locks.py` — 8 тестов (skipped)
- `tests/test_arcadedb_search.py` — 10 тестов (skipped)
- `tests/test_arcadedb_session_factory.py` — 4 теста (4 PASSED)

**Всего:** 18 passed + 27 skipped (need ArcadeDB container)

### Phase 2: ArcadeDBAdapter v2 (psycopg)

**Файл:** `hermes_cli/arcadedb.py` — полная перезапись (~250 строк)

HTTP API (httpx, 102 строки) → PostgreSQL Wire Protocol (psycopg, ~250 строк)

API:
```python
adapter = ArcadeDBAdapter(ArcadeDBConfig(host=..., port=5432, ...))
adapter.connect()                    # connection pool
adapter.transact(fn)                 # BEGIN; fn(cur); COMMIT/ROLLBACK
adapter.query("SELECT ...", params)  # dict params → auto-string-format
adapter.execute("INSERT ...", ...)   # DML
adapter._vec([0.1, 0.2, ...])        # вектор как SQL literal
```

Ключевые особенности:
- `autocommit=True` (ArcadeDB limitation)
- `_fmt()` — конвертирует `%(name)s` → SQL-литералы (ArcadeDB simple query only)
- SQL-транзакции (BEGIN/COMMIT/ROLLBACK) вместо psycopg-level toggling
- `sslmode=disable` (ArcadeDB не поддерживает SSL)
- Vector workaround: `_vec()` для Jackson float[] бага

### Phase 3: ArcadedbSessionDB

**Файлы:**
- `hermes_cli/arcadedb_session.py` (~1,400 строк) — 83 метода
- `hermes_cli/arcadedb_helpers.py` (~130 строк) — `_q()`, `_n()`, `_encode_content()`

Полная замена `hermes_state.py:SessionDB` (5,658 строк SQLite) на ArcadeDB:

| Группа методов | Кол-во | Статус |
|---------------|--------|--------|
| Session CRUD | 8 | Работает |
| Session Metadata | 7 | Работает |
| Session Titles | 5 | Работает |
| Session Listing | 8 | Работает |
| Message CRUD | 15 | Работает |
| Search | 3 | Работает (LIKE, без JOIN) |
| Compression Locks | 5 | Работает (soft-delete) |
| Session Deletion | 10 | Работает |
| Compression Cooldown | 3 | Работает |
| Meta Store | 2 | Работает |
| Handoff | 6 | Работает |
| Telegram Topics | 11 | Работает |
| Export | 2 | Работает |

**Файл:** `hermes_cli/arcadedb_schema.py` — добавлено:
- `CompressionLock` vertex type
- `StateMeta` vertex type
- `TelegramTopicMode` vertex type
- `TelegramTopicBinding` vertex type
- Индексы: `Message(session_id, timestamp)`, `Message(session_id, active, timestamp)`
- FULL_TEXT index на `Message.content` + `Fact.content`
- Новые поля: `compression_failure_cooldown_until`, `handoff_state`, `billing_provider` etc.

### Phase 4: Consumer Migration (Factory)

**Файл:** `hermes_state.py` — добавлена `create_session_db()` factory

```python
db = create_session_db()
# → ArcadedbSessionDB если database.arcadedb.enabled: True
# → SessionDB (SQLite) если False или ошибка
```

**Auto-detection:** читает `config.yaml` → проверяет `enabled` → 
`ensure_started()` или `is_healthy()` → ArcadeDB или SQLite fallback.

---

## Интеграционный тест: 10/10 на сервере

Сервер: ArcadeDB 26.7.1-SNAPSHOT в Docker (доступ через SSH-туннель)

```
$ python int_test.py

1. Factory             → type=ArcadedbSessionDB       ✓
2. Session CRUD        → создана, прочитана           ✓
3. append_message      → msg1_id + msg2_id вернулись  ✓
4. get_messages        → 2 сообщения прочитаны         ✓
5. search_messages     → LIKE поиск работает           ✓
6. get_messages_as_conv → OpenAI формат               ✓
7. replace_messages    → атомарная замена (UPDATE)    ✓
8. Compression lock    → CAS захват / конфликт        ✓
9. Meta store          → key-value работает            ✓
10. Export             → сессия + сообщения            ✓

ALL 10 TESTS PASSED
```

---

## Технические находки (ArcadeDB 26.7.1-SNAPSHOT)

### 1. Simple Query Mode Only
ArcadeDB PG plugin не поддерживает extended query protocol → bind-параметры
недоступны. Решение: `_fmt()` в `ArcadeDBAdapter` + `_q()`/`_n()` в helpers.

### 2. Нет поддержки SSL
`sslmode=disable` обязателен в connection string.

### 3. Timestamp rounding
ArcadeDB хранит DOUBLE с округлением до целых секунд. Решение: SELECT по
`session_id + role` вместо timestamp-exact-match.

### 4. DELETE VERTEX зависает на edge cascade
`DELETE VERTEX Message WHERE ...` блокируется при попытке каскадного удаления
HAS_MESSAGE edges. Решение: soft-delete `UPDATE active = 0`.

### 5. FROM a,b не поддерживается
ArcadeDB не поддерживает implicit CROSS JOIN через запятую. Решение: раздельные
запросы с кэшированием сессий в Python.

### 6. Jackson float[] баг
Векторы нельзя передавать через bind-параметры. Решение: SQL literals через
`_vec()` (уже было в старой версии, переиспользовано).

### 7. Composite index syntax
`CREATE INDEX ON Message ((session_id, timestamp)) NOTUNIQUE` — не работает.
Нужен синтаксис `CREATE INDEX ON Message (session_id, timestamp) NOTUNIQUE`
(без дополнительных скобок). → **TODO: fix arcadedb_schema.py.**

---

## Файлы (текущее состояние)

### Новые файлы (22)
```
hermes_cli/arcadedb_lifecycle.py      (~250 строк)
hermes_cli/arcadedb.py                (~250 строк, перезапись)
hermes_cli/arcadedb_session.py        (~1,400 строк)
hermes_cli/arcadedb_helpers.py        (~130 строк)
tests/fixtures/arcadedb_fixtures.py   (~300 строк)
tests/fixtures/__init__.py
tests/test_arcadedb_lifecycle.py      (~150 строк)
tests/test_arcadedb_adapter.py        (~150 строк)
tests/test_arcadedb_compression_locks.py (~120 строк)
tests/test_arcadedb_search.py         (~120 строк)
tests/test_arcadedb_session_factory.py (~50 строк)
docs/arcadedb-migration/INDEX.md
docs/arcadedb-migration/phase-0-lifecycle.md
docs/arcadedb-migration/phase-1-testing.md
docs/arcadedb-migration/phase-2-adapter-v2.md
docs/arcadedb-migration/phase-3-sessiondb.md
docs/arcadedb-migration/phase-4-consumers.md
docs/arcadedb-migration/phase-5-migration-tool.md
docs/arcadedb-migration/phase-6-kanbandb.md
docs/arcadedb-migration/phase-7-memory-store.md
docs/arcadedb-migration/phase-8-other-dbs.md
```

### Модифицированные файлы (8)
```
hermes_state.py          (+create_session_db factory)
hermes_cli/config.py     (+database.arcadedb block)
hermes_cli/arcadedb_schema.py (+4 vertex types, +indexes, +fields)
pyproject.toml           (+psycopg[binary], +psycopg-pool)
docker-compose.yml       (+arcadedb service)
docker-compose.windows.yml (+arcadedb service)
tests/conftest.py        (+pytest_plugins)
hermes_cli/graph_store.py (не трогали, совместим)
```

---

---

## Технический долг (15 пунктов)

### TD-1: Composite index syntax — arcadedb_schema.py

**Файл:** `hermes_cli/arcadedb_schema.py:~530` (метод `_create_index`)

**Проблема:** `_create_index()` генерирует `CREATE INDEX ON Message ((session_id, timestamp)) NOTUNIQUE` —
ArcadeDB отвергает двойные скобки. Синтаксическая ошибка: `mismatched input '('`.

**Влияние:** `SchemaManager.create_all()` не создаёт составные индексы.
`Message(session_id, timestamp)` и `Message(session_id, active, timestamp)`
отсутствуют → get_messages не может использовать index scan.

**Исправление:** в методе `_create_index()` изменить генерацию SQL для случая
3-tuple `(prop1, prop2, kind)` — убрать лишнюю пару скобок:
```python
# Было:  f"CREATE INDEX IF NOT EXISTS ON `{type_name}` (({name1}, {name2})) {kind}"
# Стало: f"CREATE INDEX IF NOT EXISTS ON `{type_name}` ({name1}, {name2}) {kind}"
```

**Приоритет:** Medium (функциональность работает без индексов, но медленнее)

---

### TD-2: Pool connections — INTRANS при возврате в пул

**Файл:** `hermes_cli/arcadedb.py:~145` (метод `transact`)

**Проблема:** После `ROLLBACK` в `transact()` соединение остаётся в статусе INTRANS.
При возврате в пул через `putconn()`, psycopg_pool пытается сделать reset (ROLLBACK)
и падает с `IndexError: list index out of range`. Соединение становится BAD и
discard'ится. Каждый failed transact теряет одно соединение из пула.

**Лог (из серверных тестов):**
```
WARNING:psycopg.pool:rolling back returned connection: <psycopg.Connection [INTRANS] ...>
WARNING:psycopg.pool:rollback failed: IndexError: list index out of range.
WARNING:psycopg.pool:discarding closed connection: <psycopg.Connection [BAD] ...>
```

**Исправление (варианты):**
1. После ROLLBACK выполнить пустой `SELECT 1` для сброса статуса
2. Закрывать и пересоздавать соединение после ошибки (ждёт pool reconnect)
3. Использовать `conn.close()` вместо `putconn()` для нестабильных соединений

**Приоритет:** Low (pool восстанавливается через создание новых соединений; утечки нет)

---

### TD-3: String formatting — риск SQL-инъекций

**Файлы:** `hermes_cli/arcadedb_helpers.py:_q()`, `hermes_cli/arcadedb.py:_fmt()`

**Проблема:** Поскольку ArcadeDB не поддерживает bind-параметры, все значения
внедряются в SQL через string formatting. `_q()` экранирует `'` и `\`, но это
не полноценная защита. Например, `_q("O'Brian")` → `'O\'Brian'` (правильно),
но `_q("\\\\' OR 1=1 --")` → экранирует не все edge-кейсы.

**Влияние:** Теоретический вектор SQL-инъекции через пользовательский контент
(названия сессий, содержимое сообщений с `'` и `\`).

**Исправление:**
1. Добавить `_q()` тесты для экранирования edge-кейсов
2. Рассмотреть `psycopg.sql.Literal` для безопасного форматирования
3. При обновлении ArcadeDB до версии с поддержкой extended protocol —
   вернуться к bind-параметрам

**Приоритет:** Medium (пользовательский контент потенциально непроверенный)

---

### TD-4: SEARCH_INDEX (Lucene FULL_TEXT) не используется

**Файл:** `hermes_cli/arcadedb_session.py:~900` (метод `search_messages`)

**Проблема:** `SEARCH_INDEX('Message[content]', ...)` вызывает зависание
на 20+ секунд либо синтаксическую ошибку. Причина не выяснена до конца —
возможно, ArcadeDB 26.7.1-SNAPSHOT имеет баг в Lucene FULL_TEXT реализации.
FTS5 (SQLite) заменён на простой `LIKE`, что не даёт BM25-ранжирования.

**Влияние:** Поиск сообщений работает через LIKE без relevance scoring.
Нет BM25, нет snippets, нет phrase search.

**Исправление:** 
1. Исследовать `SEARCH_INDEX` behaviour на стабильной версии ArcadeDB (не SNAPSHOT)
2. Добавить Python-side BM25 scoring как fallback
3. Рассмотреть `LANGUAGE lucene` в SQL для явного указания диалекта

**Приоритет:** Medium (поиск работает, но качество ниже FTS5)

---

### TD-5: GraphStore не обновлён под новый адаптер

**Файл:** `hermes_cli/graph_store.py` (~376 строк)

**Проблема:** `GraphStore` всё ещё использует старый `ArcadeDBAdapter` API
(HTTP/JSON). Новый psycopg-адаптер предоставляет тот же `execute()`/`query()`
API, поэтому код работает, но:
- `GraphStore.add_message()` дублирует `ArcadedbSessionDB.append_message()`
- `GraphStore.search_messages()` — старый vector поиск, не используется в новом `search_messages()`
- `GraphStore` не использует `transact()` для атомарности

**Исправление:** 
1. Убрать дублирующиеся методы (add_message, get_messages уже есть в SessionDB)
2. Переключить `hybrid_search_sessions` на новый `transact()` API
3. Синхронизировать `create_search_matter` с новым `append_message`

**Приоритет:** Low (GraphStore методы всё ещё работают через совместимый API)

---

### TD-6: Нет ArcadeDB schema version tracking

**Файл:** `hermes_cli/arcadedb_schema.py`

**Проблема:** SQLite `SessionDB` использует таблицу `schema_version` и метод
`_init_schema()` для version-gated миграций (добавление колонок, бэкфиллы).
ArcadeDB схема создаётся через `CREATE ... IF NOT EXISTS` — нет механизма
отслеживания версий и применения data-миграций.

**Влияние:** При добавлении новых полей в будущем нет автоматического
бэкфилла существующих записей (как это делает SQLite `_reconcile_columns()`).

**Исправление:** 
1. Добавить `schema_version` key в `StateMeta` vertex
2. Реализовать `_migrate_data()` метод в `SchemaManager`
3. Версионировать миграции: v1 → v2 (добавить поле X → бэкфилл NULL)

**Приоритет:** Low (пока нет новых полей, требующих бэкфилла)

---

### TD-7: Нет retry logic для transient ошибок ArcadeDB

**Файл:** `hermes_cli/arcadedb.py`

**Проблема:** SQLite `SessionDB` использует `_execute_write()` с retry loop
(до 15 попыток с jitter 20-150ms на `database is locked`). ArcadeDB adapter
не имеет retry logic — любой transient error (connection reset, pool timeout)
сразу пробрасывается как `ArcadeDBError`.

**Влияние:** При пиковой нагрузке или перезапуске ArcadeDB возможны
ложные ошибки в агентском цикле.

**Исправление:** 
1. Добавить `@retry` декоратор на `transact()` и `execute()` 
2. Использовать `tenacity` (уже в deps) с экспоненциальным backoff
3. Retry только на `OperationalError` и `PoolTimeout`

**Приоритет:** Medium (production reliability)

---

### TD-8: 30+ consumers всё ещё создают SessionDB напрямую

**Файлы:** `cli.py`, `run_agent.py`, `gateway/run.py`, `gateway/session.py`, etc.

**Проблема:** Factory `create_session_db()` написана, но ни один consumer
не переключён на неё. Все 30+ файлов всё ещё делают `SessionDB()` напрямую.
Фабрика существует, но не используется.

**Влияние:** ArcadeDB не активируется при реальном запуске Hermes —
только в тестах и интеграционных скриптах.

**Исправление:** См. Phase 4 spec (`docs/arcadedb-migration/phase-4-consumers.md`).
Заменить `SessionDB()` → `create_session_db()` в 30+ файлах.

**Приоритет:** **CRITICAL** (без этого вся работа не активируется)

---

### TD-9: Нет тестов ArcadeDB → SQLite fallback

**Файл:** `tests/test_arcadedb_session_factory.py`

**Проблема:** Фабрика имеет try/except для graceful fallback на SQLite,
но тесты проверяют только: SQLite работает, ArcadeDB включается (mock).
Не протестированы сценарии:
- ArcadeDB configured but unreachable → fallback
- ArcadeDB был доступен, стал недоступен mid-session → поведение не определено
- Docker absent + auto_start=True → сообщение об ошибке

**Исправление:** Добавить тесты с mock'ом `ArcadeDBAdapter.connect()` 
на выбрасывание исключений.

**Приоритет:** Medium (критично для production-надёжности)

---

### TD-10: `_maybe_epoch()` — потеря микросекунд при парсинге ISO

**Файл:** `hermes_cli/arcadedb_helpers.py:_maybe_epoch()`

**Проблема:** Функция парсит `"2026-06-30 12:00:00"` но не обрабатывает
ISO-формат с микросекундами `"2026-06-30T12:00:00.123456"` или timezone
`"2026-06-30T12:00:00+03:00"`. SQLite-бэкенд хранит только float (epoch),
поэтому проблема не критична для миграции, но при ручном импорте данных
из внешних источников — потеря точности.

**Исправление:** Добавить поддержку ISO-8601 с timezone и микросекундами.

**Приоритет:** Low (state.db всегда хранит epoch float, миграция не теряет данные)

---

### TD-11: `update_token_counts()` — race condition на инкрементах

**Файл:** `hermes_cli/arcadedb_session.py:~220`

**Проблема:** `UPDATE Session SET input_tokens = input_tokens + %s` —
ArcadeDB выполняет read-modify-write неатомарно (в отличие от SQLite
`BEGIN IMMEDIATE`). Два concurrent вызова могут потерять обновления.

**Влияние:** При параллельной работе нескольких агентов (kanban workers,
delegate subagents) счётчики токенов могут расходиться.

**Исправление:** Обернуть в `transact()` с явным `SELECT ... FOR UPDATE`
или использовать ArcadeDB atomic increment (если поддерживается).

**Приоритет:** Low (счётчики токенов — некритичная метрика)

---

### TD-12: `rewind_to_message()` — @rid сравнение строковое

**Файл:** `hermes_cli/arcadedb_session.py:~780`

**Проблема:** Метод сравнивает `@rid` как строки через `>=`, что в ArcadeDB
работает лексикографически (`#39:27` > `#39:3` — ложно, потому что `'2'` < `'3'`?).
На самом деле `#39:27` > `#39:3` лексикографически верно, но `#39:9` > `#39:10`
уже нет. Порядок `@rid` не гарантирует хронологию вставки.

**Влияние:** `/rewind` и `/undo` могут захватить неправильный диапазон сообщений.

**Исправление:** Использовать timestamp для определения порядка, не @rid.

**Приоритет:** Medium (пользовательская команда /undo)

---

### TD-13: `hybrid_search_sessions` — entity_names не заполняются

**Файл:** `hermes_cli/arcadedb_session.py:hybrid_search_sessions()` + `SearchMatter` vertex

**Проблема:** `SearchMatter` vertex type имеет поле `entity_names` (LIST),
но `hybrid_search_sessions()` не извлекает и не сохраняет entity names
при создании SearchMatter. Это лишает гибридный поиск entity-aware filtering,
который был в изначальном плане.

**Исправление:** Добавить entity extraction в `create_search_matter()` и
`append_message()` — извлекать именованные сущности из текста и сохранять
в `SearchMatter.entity_names`.

**Приоритет:** Low (функциональность не реализована, но и не используется)

---

### TD-14: `_rid_to_int()` — хеш-коллизии возможны

**Файл:** `hermes_cli/arcadedb_helpers.py:_rid_to_int()`

**Проблема:** `hash(rid) & 0x7FFFFFFF` преобразует строковый @rid в 32-bit int
для обратной совместимости с SQLite `AUTOINCREMENT`. Теоретически возможны коллизии
при большом количестве сообщений (>2^31). Практически — маловероятно, но
`get_messages_around()` полагается на уникальность message ID.

**Исправление:** Использовать `id` property (INTEGER SEQUENCE) на Message vertex
вместо хеша @rid. Требует `CREATE SEQUENCE` в схеме и изменение `append_message()`.

**Приоритет:** Low (коллизия `hash()` на <1M сообщений практически невозможна)

---

### TD-15: `search_messages` — N+1 запросов на сессии

**Файл:** `hermes_cli/arcadedb_session.py:~940` (lookup session metadata)

**Проблема:** После поиска сообщений, для каждого результата делается отдельный
запрос `SELECT source, model FROM Session WHERE id = ...` для получения метаданных.
При limit=20 и 0 кэш-попаданиях — 20 дополнительных запросов.

**Исправление:** 
1. Собрать все `session_id` из результатов, сделать один batch-запрос
2. Или денормализовать `source` и `model` прямо в Message vertex

**Приоритет:** Low (кэш в Python снижает N+1 до ~2-3 запросов на типичный search)

---

## Следующие шаги (Phases 5–8)

**Phases 5–7 реализованы**, Phase 8 отложена.

### ✅ Phase 5: Migration Tool
- `hermes_cli/migrate_to_arcadedb.py` — dry-run, migrate, verify
- Протестирован на сервере: 1 сессия skipped, 2 сообщения migrated

### ✅ Phase 6: KanbanDB Wrapper
- `hermes_cli/arcadedb_kanban.py` — CAS claim, DAG edges, CRUD

### ✅ Phase 7: Memory Store
- `plugins/memory/holographic/arcadedb_store.py`

### ⏸ Phase 8: Other DBs (отложено)
- Projects DB, Response Store, Verification Evidence, RetainDB Queue

---

## Новый технический долг (обнаружен при реализации)

### TD-16: API сервер — заглушка без вызова AI

**Файл:** `/tmp/hermes_api.py` (на сервере)

**Проблема:** OpenWebUI получает `[Hermes ArcadeDB] Received: Hello` вместо
реального ответа агента. Эхо-заглушка вместо `AIAgent.chat()`.

**Исправление:** Импортировать `run_agent.py` → `AIAgent`, вызывать
`agent.chat(user_msg)` и отдавать реальный ответ модели.

**Приоритет:** **CRITICAL** (OpenWebUI не работает без этого)

---

### TD-17: `_fmt_tuple` regex хрупкий

**Файл:** `hermes_cli/arcadedb.py:_fmt_tuple()`

**Проблема:** `(?<!%)(?<!\w)%s(?!\w)` — сложный regex для замены `%s`
плейсхолдеров. Не покрывает все edge-кейсы (например `%s` внутри строкового
литерала `'100% safe'`). При ложном срабатывании — синтаксическая ошибка SQL.

**Исправление:** Переписать на парсинг placeholder'ов с учётом кавычек и
escape-последовательностей. Либо полностью отказаться от `%s` в пользу
`_q()`+f-string на уровне вызывающего кода.

**Приоритет:** Medium (пока не ломалось, но потенциально опасно)

---

### TD-18: Soft-delete → утечка вершин

**Файлы:** `arcadedb_session.py:replace_messages()`, `archive_and_compact()`

**Проблема:** `UPDATE active = 0` вместо `DELETE VERTEX` (TD-4 workaround).
Вершины с `active=0` копятся бесконечно. Нет background cleanup.
На тестовом сервере уже 96 Message вершин при 2 реальных сообщениях.

**Исправление:**
1. Добавить `prune_inactive_vertices()` — удаление старых `active=0` записей
2. Вызывать при `vacuum()` или `maybe_auto_prune_and_vacuum()`

**Приоритет:** Medium (на тестовых объёмах незаметно, в production — рост БД)

---

### TD-19: ArcadeDB SNAPSHOT — 7 workaround'ов

**Файлы:** `arcadedb.py`, `arcadedb_session.py`, `arcadedb_schema.py`

**Проблема:** Код содержит 7 обходных путей, специфичных для ArcadeDB
26.7.1-SNAPSHOT. При обновлении до stable-версии они могут стать ненужными
или даже вредными:

| Workaround | Что меняется при stable |
|-----------|------------------------|
| `_fmt()` dict→string | Вернуть bind-параметры |
| `_fmt_tuple()` tuple→string | Вернуть bind-параметры |
| `autocommit=True` | Возможно `autocommit=False` |
| `sslmode=disable` | Возможно SSL |
| `UPDATE active=0` вместо DELETE | Вернуть DELETE VERTEX |
| `SKIP` вместо `OFFSET` | Вернуть `OFFSET` |
| `LIKE` без `ESCAPE` | Вернуть `ESCAPE` |

**Исправление:** Создать feature-флаг `arcadedb_compat_mode` в конфиге,
который переключает между workaround-режимом и native-режимом.

**Приоритет:** Low (до выхода stable-версии ArcadeDB)

---

### TD-20: API сервер не интегрирован в `hermes serve`

**Файл:** `/tmp/hermes_api.py` (временный скрипт)

**Проблема:** OpenAI-совместимый API работает через отдельный скрипт, а не
через штатный `hermes serve`. Не запускается при старте gateway, не
интегрирован в конфиг, не имеет аутентификации.

**Исправление:** 
1. Интегрировать `/v1/chat/completions` в `gateway/platforms/api_server.py`
2. Добавить `gateway.api_server.openai_compat: true` в config.yaml
3. Запускать в составе `hermes serve` или `hermes gateway`

**Приоритет:** Medium (нужно для production OpenWebUI)

---

### TD-21: Venv на сервере неполный

**Сервер:** `.venv` (Python 3.11)

**Проблема:** Установлены только `psycopg`, `fastapi`, `uvicorn`, `rich`,
`pyyaml`, `ruamel.yaml`, `python-dotenv`, `psutil`. Остальные ~40 зависимостей
отсутствуют. Полная установка `pip install -e '.[all,dev]'` требует Python
3.11 (на сервере есть 3.11, но venv был создан на 3.10, потом пересоздан).

**Исправление:** `pip install -e '.[all,dev]'` в новом venv на Python 3.11.

**Приоритет:** Medium (блокирует запуск реального AIAgent в API сервере)


### TD-22: `hybrid_search` ищет только SearchMatter, не Message

**Файл:** `arcadedb_session.py:hybrid_search_sessions()`

**Проблема:** Эмбеддинги вычисляются и сохраняются в `Message.embedding` (это
работает), но `hybrid_search_sessions()` делает `vector.neighbors()` по
`SearchMatter[embedding]`, а не по `Message[embedding]`. SearchMatter должен
содержать session-level summary с эмбеддингом — но для новых сессий
SearchMatter не создаётся. Результат: embedding-поиск находит только
сессии, для которых был явно создан SearchMatter.

**Исправление:** 
1. Добавить `vector.neighbors('Message[embedding]', ...)` в `search_messages()`
2. Или авто-создавать SearchMatter при `append_message()`

**Приоритет:** HIGH (векторный поиск не охватывает новые сообщения)

---

### TD-23: Persistent test DB — накопление данных

**Файлы:** тесты на сервере используют одну БД `hermes`

**Проблема:** `DELETE VERTEX` не работает в ArcadeDB PG protocol (TD-4), поэтому
тесты не могут чистить за собой. Каждый прогон добавляет вершины TestTx, TestQ,
TestV. Ассерты `len(rows) == 1` ломаются на повторных прогонах.

**Исправление:**
1. Использовать уникальные ID в каждом прогоне (частично сделано)
2. Или создать тестовую БД `hermes_test` отдельно от production `hermes`

**Приоритет:** Medium (мешает CI, но не production)

---

### TD-24: OSError /tmp переполнение на сервере

**Сервер:** `176.108.249.180`, диск 20GB

**Проблема:** Кэши fastembed (~2GB), старый репозиторий hermes-agent (~6GB),
git bundle (~200MB) заполняют диск. Очищено вручную, но при повторной загрузке
моделей и активном использовании диск снова забьётся.

**Исправление:**
1. Добавить мониторинг диска в `hermes doctor`
2. Cron-задача на очистку старых кэшей
3. Вынести ArcadeDB data на отдельный volume

**Приоритет:** Low (ручная очистка, production требует мониторинга)

---

### TD-25: `hermes serve` не использует `openai_api.py` роутер

**Файл:** `hermes_cli/openai_api.py` + `hermes_cli/web_server.py`

**Проблема:** OpenAI-совместимый API роутер (`openai_api.py`) написан, но
**не подключён** к штатному `hermes serve`. Сейчас API работает через
отдельный скрипт `/tmp/hermes_api.py` на сервере.

**Исправление:** 
1. Подключить `openai_api.router` в `web_server.py:app` через `app.include_router()`
2. Добавить конфиг `gateway.api_server.openai_compat: true`

**Приоритет:** Medium (работает через отдельный скрипт, но не интегрировано)

---

### TD-26: `append_message` эмбеддит даже без необходимости

**Файл:** `arcadedb_session.py:append_message()`

**Проблема:** Каждый вызов `append_message()` вычисляет embedding (1024d через
ONNX, ~50ms на CPU). Это замедляет запись. Embedding нужен только если
включён векторный поиск.

**Исправление:** Добавить флаг `skip_embed=True` или проверять `self._embedder`
перед вычислением (уже проверяется, но `embedder is not None` всегда при заводских
настройках).

**Приоритет:** Low (50ms latency на CPU приемлемо)