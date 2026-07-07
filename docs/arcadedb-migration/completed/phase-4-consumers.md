# Phase 4: Consumer Migration — As-Built

## Файлы

| Файл | Изменение | Назначение |
|------|-----------|------------|
| `hermes_state.py` | +30 строк | `create_session_db()` factory + `ARCADEDB_TEST_HOST` override |
| `hermes_cli/config.py` | +50 строк | `database.arcadedb.*`, `redis.*`, `search_matter.*`, `auxiliary.search_matter` |

## Реализованное

### Factory: `create_session_db()`

```python
db = create_session_db()  # → ArcadedbSessionDB (если настроен) или SessionDB (SQLite)
```

**Gating**: `config.yaml → database.arcadedb.enabled = true` → ArcadeDB.

**Тестовый режим**: `ARCADEDB_TEST_HOST` env var форсирует ArcadeDB, обходит lifecycle-проверку (Docker health). Берёт `ARCADEDB_TEST_PASSWORD`, `ARCADEDB_TEST_PORT`, `ARCADEDB_TEST_USER` из env.

**Fallback**: при ошибке инициализации → SQLite SessionDB.

### Config Blocks (добавлены 06-07.07)

| Блок | Назначение |
|------|-----------|
| `database.arcadedb` | host, port, database, user, password, enabled, auto_start |
| `redis` | host, port, enabled (compression locks через RedisLockManager) |
| `search_matter` | llm_summary, sliding_window (max_messages, overlap, max_recursion) |
| `auxiliary.search_matter` | provider/model для LLM-саммаризации (по умолчанию "auto") |

## Статус consumers (30+ файлов)

Все consumers используют `create_session_db()` factory → переключение на ArcadeDB автоматическое.
Ни один consumer НЕ инстанцирует `SessionDB()` напрямую (подтверждено grep'ом по `hermes_cli/`, `agent/`, `gateway/`).

Файлы: `cli.py`, `run_agent.py`, `gateway/run.py`, `gateway/session.py`, `gateway/slash_commands.py`, `cron/scheduler.py`, `hermes_cli/main.py`, `hermes_cli/web_server.py`, `hermes_cli/cli_commands_mixin.py`, `hermes_cli/oneshot.py`, `hermes_cli/goals.py`, `tui_gateway/server.py`, `mcp_serve.py`, и другие.

## Отклонения от ТЗ

- **Нет миграции 30+ consumers** — factory уже используется везде. Ручное переключение не требуется.
- **`ARCADEDB_TEST_HOST`** — не планировалось в ТЗ, добавлено для CI/тестов.
