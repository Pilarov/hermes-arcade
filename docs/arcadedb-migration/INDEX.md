# ArcadeDB Native Storage Migration — Master Plan

## Overview

Полная замена SQLite storage на ArcadeDB как единственный backend для Hermes Agent.
Все 7 SQLite баз данных мигрируют в единый ArcadeDB instance.

**Ключевое решение:** Tests-first подход — Фаза 1 определяет контракты поведения,
все последующие фазы реализуют код под эти контракты.

```
┌─────────────────────────────────────────────────────────────┐
│                    Hermes Agent Application                  │
├─────────────────────────────────────────────────────────────┤
│  ArcadedbSessionDB   ArcadedbKanbanDB   ArcadedbMemoryStore │
│  (Phase 3)           (Phase 6)          (Phase 7)           │
│  ArcadedbProjectsDB  ArcadedbResponseStore (Phase 8)       │
├─────────────────────────────────────────────────────────────┤
│              ArcadedbAdapter v2 (Phase 2)                    │
│         PostgreSQL Wire Protocol (psycopg)                  │
├─────────────────────────────────────────────────────────────┤
│           ArcadeDB Lifecycle Manager (Phase 0)              │
│         Auto-managed Docker container                       │
├─────────────────────────────────────────────────────────────┤
│                      ArcadeDB Server                         │
│              Port 5432 (PostgreSQL protocol)                │
│              Port 2480 (HTTP Studio, optional)              │
└─────────────────────────────────────────────────────────────┘
```

## Фазы выполнения

| Фаза | Название | Строки | Сложность | Зависимости | Документ |
|------|----------|--------|-----------|-------------|----------|
| 0 | Lifecycle Manager | 400 | Medium | None | [phase-0-lifecycle.md](phase-0-lifecycle.md) |
| **1** | **Testing Framework** | **2,500** | **CRITICAL** | **Phase 0** | **[phase-1-testing.md](phase-1-testing.md)** |
| 2 | Adapter v2 (psycopg) | 300 | Medium | Phase 0, 1 | [phase-2-adapter-v2.md](phase-2-adapter-v2.md) |
| 3 | ArcadedbSessionDB | 3,500 | **CRITICAL** | Phase 1, 2 | [phase-3-sessiondb.md](phase-3-sessiondb.md) |
| 4 | Consumer Migration | 200 | High | Phase 3 | [phase-4-consumers.md](phase-4-consumers.md) |
| 5 | Data Migration Tool | 600 | Medium | Phase 3, 4 | [phase-5-migration-tool.md](phase-5-migration-tool.md) |
| 6 | KanbanDB Wrapper | 1,000 | **CRITICAL** | Phase 2, 3 | [phase-6-kanbandb.md](phase-6-kanbandb.md) |
| 7 | Memory Store | 200 | Medium | Phase 2, 3 | [phase-7-memory-store.md](phase-7-memory-store.md) |
| 8 | Other DBs | 400 | Low-Medium | Phase 2, 3 | [phase-8-other-dbs.md](phase-8-other-dbs.md) |
| — | Integration & E2E | — | High | All phases | [phase-1-testing.md](phase-1-testing.md) (Part 2) |

## Tests-First Architecture

Фаза 1 создаёт: ~30 тестовых файлов, ~2,500 строк

Тесты **падают на старте** (ожидаемо) — они определяют контракты:
- `test_arcadedb_adapter.py` — определяет API адаптера
- `test_arcadedb_session.py` — определяет 80+ методов SessionDB
- `test_arcadedb_session_factory.py` — определяет factory+fallback
- `test_arcadedb_kanban.py` — определяет CAS и edge-логику
- `test_arcadedb_migration.py` — определяет протокол миграции
- `test_arcadedb_compression_locks.py` — определяет lock-протокол
- `test_arcadedb_search.py` — определяет FTS5→Lucene эквиваленты
- `test_arcadedb_lifecycle.py` — определяет Docker lifecycle
- ... ещё ~22 файла

Каждая последующая фаза должна сделать свои тесты зелёными.

## Ключевые технические решения

### 1. PostgreSQL Wire Protocol (psycopg) вместо HTTP API
- **Файл:** `hermes_cli/arcadedb.py` — полная перезапись
- **Причина:** транзакции (BEGIN/COMMIT/ROLLBACK) не доступны через HTTP
- **Депенденси:** `psycopg[binary]>=3.1,<4` в `pyproject.toml`
- **Connection pooling:** `psycopg_pool.ConnectionPool` (min=2, max=10)

### 2. Jackson float[] workaround
- Векторы передаются как SQL literals: `f"embedding = {_vec(emb.dense)}"`
- **Helper:** `_vec(val: list[float]) -> str` в `hermes_cli/arcadedb.py`
- **Обоснование:** ArcadeDB 26.7.1-SNAPSHOT Jackson bug — параметр-bind ломает float[]

### 3. Auto-managed Docker container
- **Файл:** `hermes_cli/arcadedb_lifecycle.py` (~400 строк)
- Hermes сам запускает/останавливает ArcadeDB через `docker run`
- Data volume: `~/.hermes/arcadedb/data`
- Health check: `SELECT 1` через psycopg (timeout 2s)

### 4. Graceful fallback на SQLite
- `hermes_state.py` становится **factory**: `create_session_db()`
- Если `database.arcadedb.enabled: false` — возвращает SQLite SessionDB
- Все consumers вызывают factory, не зная backend

## Total Scope

| Метрика | Значение |
|---------|----------|
| Новых файлов | ~42 |
| Модифицированных файлов | ~77 |
| Новых строк кода | ~8,600 |
| Тестовых строк | ~2,500 |
| Фазы выполнения | 8 |
| Public методов ArcadedbSessionDB | ~80 |
| Потребителей SessionDB для переключения | 30+ |
| SQLite БД для миграции | 7 |

## Order of Execution

```
Phase 0: Lifecycle (docker infra)
    ↓
Phase 1: Tests (contracts first — WILL FAIL on start)
    ↓
Phase 2: Adapter v2 (connection + transactions)
    → tests/test_arcadedb_adapter.py goes GREEN
    ↓
Phase 3: SessionDB (80+ methods)
    → tests/test_arcadedb_session.py goes GREEN
    ↓
Phase 4: Consumers (30+ files switch to factory)
    → existing tests pass with ArcadeDB backend
    ↓
Phase 5: Migration tool
    → tests/test_arcadedb_migration.py goes GREEN
    ↓
Phases 6-8: KanbanDB + Memory Store + Other DBs (can parallel with 5)
    → their test files go GREEN
    ↓
Phase 1 Part 2: Integration & E2E tests (end-to-end validation)
    → all integration tests GREEN
```

## Critical Path (minimum for session storage)

```
Phase 0 → Phase 1 (partial) → Phase 2 → Phase 3 → Phase 4
```

После Phase 4: Hermes может запускать CLI сессии через ArcadeDB.

## Files Inventory

### New files (~8,600 lines)

```
hermes_cli/
├── arcadedb_lifecycle.py          (400)  ← Phase 0
├── arcadedb.py                    (300)  ← Phase 2 (перезапись)
├── arcadedb_session.py            (3,500) ← Phase 3
├── arcadedb_kanban.py             (1,000) ← Phase 6
├── arcadedb_helpers.py            (150)  ← Phase 3 (shared utils)
├── migrate_to_arcadedb.py         (600)  ← Phase 5

plugins/memory/holographic/
├── arcadedb_store.py              (200)  ← Phase 7

tests/
├── test_arcadedb_adapter.py       (200)  ← Phase 1
├── test_arcadedb_session.py       (800)  ← Phase 1
├── test_arcadedb_session_factory.py (150) ← Phase 1
├── test_arcadedb_kanban.py        (350)  ← Phase 1
├── test_arcadedb_migration.py     (250)  ← Phase 1
├── test_arcadedb_compression_locks.py (150) ← Phase 1
├── test_arcadedb_search.py        (250)  ← Phase 1
├── test_arcadedb_lifecycle.py     (150)  ← Phase 1
├── test_arcadedb_telegram_topics.py (100) ← Phase 1
├── e2e/
│   ├── test_cli_arcadedb.py       (200)  ← Phase 1
│   └── test_gateway_arcadedb.py   (200)  ← Phase 1
└── fixtures/
    └── arcadedb_fixtures.py       (150)  ← Phase 1
```

### Modified files (~77 files)

```
pyproject.toml                     ← +psycopg dependency (Phase 2)
hermes_cli/config.py               ← +database.arcadedb block (Phase 0)
hermes_cli/arcadedb_schema.py      ← +indexes + new types (Phase 3)
hermes_state.py                    ← becomes factory (Phase 4)
docker-compose.yml                 ← +ArcadeDB service (Phase 0)
docker-compose.windows.yml         ← +ArcadeDB service (Phase 0)

# 30+ consumer files (Phase 4)
run_agent.py
cli.py
agent/conversation_loop.py
agent/conversation_compression.py
agent/context_compressor.py
agent/agent_runtime_helpers.py
agent/agent_init.py
agent/insights.py
gateway/run.py
gateway/session.py
gateway/slash_commands.py
gateway/platforms/api_server.py
gateway/platforms/telegram/adapter.py
hermes_cli/cli_commands_mixin.py
hermes_cli/cli_agent_setup_mixin.py
hermes_cli/web_server.py
hermes_cli/doctor.py
hermes_cli/backup.py
hermes_cli/main.py
hermes_cli/profiles.py
cron/scheduler.py
acp_adapter/session.py
mcp_serve.py
plugins/hermes-achievements/dashboard/plugin_api.py
# ... все 30+ consumers

# Kanban (Phase 6)
hermes_cli/kanban_db.py            ← +ArcadedbKanbanDB path
hermes_cli/kanban.py               ← +dispatch support

# Memory (Phase 7)
plugins/memory/holographic/store.py ← +ArcadedbMemoryStore path
```

## Risk Matrix

| Риск | Вероятность | Влияние | Mitigation | Фаза |
|------|-------------|---------|------------|------|
| Jackson float[] через psycopg | High | Medium | SQL literals workaround, `_vec()` | Phase 2 |
| Docker не установлен | Medium | High | Graceful SQLite fallback | Phase 0 |
| ArcadeDB 26.7.1 нестабильна | Medium | High | Pin version, health checks | Phase 0 |
| FTS5 → Lucene эквивалентность | Medium | Medium | Python snippet gen, LIKE fallback | Phase 3 |
| Compression lock races | Low | **CRITICAL** | MVCC + explicit transactions | Phase 3 |
| Perf regression >2x SQLite | Medium | Medium | Pool, batch, index tuning | Phase 2-3 |
| Миграция data loss | Low | **CRITICAL** | Dry-run, verify, archive | Phase 5 |

## Document Structure Convention

Каждый phase document содержит:
1. **Header** — номер, название, сложность, зависимости
2. **Overview** — что делает фаза
3. **Files** — какие файлы создаёт/изменяет (с путями)
4. **API/Contract Specification** — все public методы, signatures, return types
5. **Implementation Notes** — технические решения и компромиссы
6. **Cross-References** — связи с другими фазами ([см. Phase N](../phase-X-*.md)), связи с существующими файлами ([`file.py:123`](../../file.py))
7. **Test Cases** — что проверяется в каждом тесте
8. **Acceptance Criteria** — когда фаза считается завершённой
9. **Dependency Graph** — что нужно для старта, что разблокирует

## Document Status

| Фаза | Документ | Строк | Статус |
|------|----------|-------|--------|
| 0 | [phase-0-lifecycle.md](phase-0-lifecycle.md) | ~450 | Written |
| 1 | [phase-1-testing.md](phase-1-testing.md) | ~1,200 | Written — 15 тестовых файлов, 200+ кейсов |
| 2 | [phase-2-adapter-v2.md](phase-2-adapter-v2.md) | ~400 | Written — перезапись arcadedb.py |
| 3 | [phase-3-sessiondb.md](phase-3-sessiondb.md) | ~800 | Written — 80+ методов, 72 теста |
| 4 | [phase-4-consumers.md](phase-4-consumers.md) | ~350 | Written — 30+ файлов переключения |
| 5 | [phase-5-migration-tool.md](phase-5-migration-tool.md) | ~400 | Written — auto-detect + ручная |
| 6 | [phase-6-kanbandb.md](phase-6-kanbandb.md) | ~450 | Written — CAS, DAG edges |
| 7 | [phase-7-memory-store.md](phase-7-memory-store.md) | ~320 | Written — Holographic Memory |
| 8 | [phase-8-other-dbs.md](phase-8-other-dbs.md) | ~280 | Written — Projects, Response, Verification, RetainDB |

**Total documentation:** ~4,800 строк, 10 файлов

## Quick Reference — Cross-References Matrix

| Из \ В | Phase 0 | Phase 1 | Phase 2 | Phase 3 | Phase 4 | Phase 5 | Phase 6 | Phase 7 | Phase 8 |
|--------|---------|---------|---------|---------|---------|---------|---------|---------|---------|
| **Phase 0** | — | Tests | Adapter | SessionDB | Factory | — | — | — | — |
| **Phase 1** | Fixtures | — | Contracts | Contracts | Contracts | Contracts | Contracts | Contracts | Contracts |
| **Phase 2** | Lifecycle | Tests | — | SessionDB | — | Migrator | KanbanDB | Memory | Other DBs |
| **Phase 3** | — | Tests | Adapter | — | Factory | Migrator | — | Helpers | Helpers |
| **Phase 4** | Lifecycle | Tests | — | SessionDB | — | Auto-detect | — | — | — |
| **Phase 5** | — | Tests | Adapter | SessionDB | Factory | — | Kanban | Memory | Projects |
| **Phase 6** | — | Tests | Adapter | Helpers | — | Migrator | — | — | — |
| **Phase 7** | — | Tests | Adapter | Helpers | — | Migrator | — | — | — |
| **Phase 8** | — | Tests | Adapter | Helpers | — | Migrator | — | — | — |

## Key Files Map (by Phase)

```
Phase 0: hermes_cli/arcadedb_lifecycle.py (NEW, 400ln)
         hermes_cli/config.py (MODIFY: +database.arcadedb block)
         docker-compose.yml (MODIFY: +arcadedb service)

Phase 1: tests/fixtures/arcadedb_fixtures.py (NEW, 200ln)
         tests/test_arcadedb_*.py (NEW, 15 files, 2,500ln)

Phase 2: hermes_cli/arcadedb.py (REWRITE, 300ln)
         pyproject.toml (MODIFY: +psycopg dep)
         hermes_cli/graph_store.py (MODIFY: switch adapter)

Phase 3: hermes_cli/arcadedb_session.py (NEW, 3,500ln) ← BIGGEST
         hermes_cli/arcadedb_helpers.py (NEW, 150ln)
         hermes_cli/arcadedb_schema.py (MODIFY: +indexes)
         tools/session_search_tool.py (MODIFY: config-based init)

Phase 4: hermes_state.py (MODIFY: +factory)
         run_agent.py, cli.py, gateway/*, agent/* (MODIFY: 30+ files)

Phase 5: hermes_cli/migrate_to_arcadedb.py (NEW, 600ln)
         cli.py (MODIFY: +migrate command)

Phase 6: hermes_cli/arcadedb_kanban.py (NEW, 1,000ln)
         hermes_cli/kanban_db.py (MODIFY: +ArcadeDB path)

Phase 7: plugins/memory/holographic/arcadedb_store.py (NEW, 200ln)

Phase 8: hermes_cli/arcadedb_projects.py (NEW, 100ln)
         gateway/platforms/api_server.py (MODIFY: +ArcadeDB variant)
         agent/verification_evidence.py (MODIFY: +ArcadeDB variant)
         plugins/memory/retaindb/__init__.py (MODIFY: +ArcadeDB variant)
```

## Start Here

→ [Phase 0: ArcadeDB Lifecycle Manager](phase-0-lifecycle.md)
→ [Phase 1: Testing Framework](phase-1-testing.md)
