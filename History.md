# Hermes ArcadeDB — Ход работ

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

Сервер: `pilarovds@176.108.249.180`, ArcadeDB 26.7.1-SNAPSHOT в Docker
Доступ: только через SSH (порты 5432/2480 фильтруются хостингом)

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

## Следующие шаги (Phases 5–8)

### Phase 5: Migration Tool
- `hermes_cli/migrate_to_arcadedb.py` — SQLite → ArcadeDB
- `hermes migrate --arcadedb` CLI команда
- Auto-detect при первом старте
- Dry-run + verify + rollback

### Phase 6: KanbanDB Wrapper
- `hermes_cli/arcadedb_kanban.py` — CAS claim, DAG edges
- 8,723 строки `kanban_db.py` — не трогаем, добавляем ArcadeDB path

### Phase 7: Memory Store
- `plugins/memory/holographic/arcadedb_store.py`
- HRR vectors → LSM_VECTOR embeddings
- FTS5 → FULL_TEXT Lucene

### Phase 8: Other DBs
- Projects DB, Response Store, Verification Evidence, RetainDB Queue

### Технический долг
- Composite index syntax fix в `arcadedb_schema.py`
- Pool connection cleanup (warnings о INTRANS при закрытии)
- Поиск: добавить SEARCH_INDEX обратно когда заработает
- Композитные индексы: убрать дополнительные скобки в DDL
