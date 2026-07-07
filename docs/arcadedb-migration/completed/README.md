# ArcadeDB Migration — As-Built Documentation

Документация по реализованным фазам. Отражает **фактическое состояние** кода,
включая отклонения от ТЗ, вызванные особенностями ArcadeDB 26.7.2-SNAPSHOT.

## Реализованные фазы

| Фаза | Название | Статус |
|------|----------|--------|
| 0 | Lifecycle Manager | ✅ Готово |
| 1 | Testing Framework | ✅ Готово |
| 2 | Adapter (HTTP-first + PG vector) | ✅ Готово |
| 3 | ArcadedbSessionDB + SearchMatter | ✅ Готово |
| 4 | Consumer Migration (Factory) | ✅ Готово |
| — | Redis Compression Locks | ✅ Готово (Phase X) |
| — | LLM SearchMatter Summarization | ✅ Готово |
| 5 | Data Migration Tool | ⏳ Не начато |
| 6 | KanbanDB | ⏳ Не начато |
| 7 | Memory Store | ⏳ Не начато |
| 8 | Other Databases | ⏳ Не начато |

## Текущий статус (07.07.2026)

**127/127 тестов (100%). 0 XFAIL, 0 FAILED.**

- ArcadeDB 26.7.2-SNAPSHOT + Redis 7 на `176.108.249.180`
- `create_session_db()` factory: форсирует ArcadeDB через `ARCADEDB_TEST_HOST` env var
- Compression locks: Redis SET NX EX (атомарно, 0 XFAIL)
- Векторный поиск: HTTP API (port 2480), работает на 1024d
- PG протокол: psycopg3 на Linux (system libpq), SSH-fallback на Windows

## Ключевые отклонения от ТЗ

### HTTP-first архитектура (вместо psycopg-only)

**ТЗ предполагало:** psycopg + PG wire protocol для всего.

**Реальность:** 
- HTTP API (port 2480) — CRUD, schema, векторный INSERT
- PG wire (port 5432) — ТОЛЬКО векторный поиск (psycopg3)
- `_SqlCollector`: батчинг SQL → один HTTP POST с `language=sqlscript`, implicit транзакция
- pg8000/psycopg2 — НЕСОВМЕСТИМЫ с ArcadeDB SCRAM-SHA-256 auth

### SCRAM-SHA-256 Auth

ArcadeDB 26.7.x использует SCRAM-SHA-256 для PG протокола. Работает только с
системным libpq (Linux psql, psycopg3). Все Python PG-драйверы с bundled libpq
несовместимы. См. `ARCADE_QUIRKS.md` §11.

### Redis Distributed Locks

DB-based CAS (UNIQUE constraint) неработоспособен под READ_COMMITTED (9 XFAIL).
Заменён на Redis `SET NX EX` — детерминированно, атомарно, crash-safe.
Fallback на DB-CAS при недоступности Redis.

### ARCADEDB_TEST_HOST для тестов

Factory `create_session_db()` проверяет `ARCADEDB_TEST_HOST` env var.
Если установлен — форсирует ArcadeDB независимо от `database.arcadedb.enabled`.
Обходит lifecycle-проверку (Docker health check).

## Интеграционный тест: 127/127 PASSED

100 unit/integration + 27 E2E тестов. Запуск на боевом сервере без SSH-туннелей.
Время прогона: 84 секунды (против 700 через туннель с Windows).

## Следующие шаги

| Приоритет | Задача |
|-----------|--------|
| HIGH | Починить Gateway provider routing (OpenRouter перехватывает DeepSeek) |
| HIGH | Phase 5: Data Migration Tool (SQLite → ArcadeDB) |
| MEDIUM | Dashboard auth для внешнего доступа |
| LOW | Phase 6-8: KanbanDB, Memory Store, вспомогательные БД |
