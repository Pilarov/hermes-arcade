# Phase 4: Consumer Migration — Switching to ArcadedbSessionDB

| Поле | Значение |
|------|----------|
| **Номер** | Phase 4 |
| **Название** | Consumer Migration — Factory Switch |
| **Новых строк** | ~200 |
| **Модифицированных файлов** | ~30+ |
| **Сложность** | High |
| **Зависит от** | Phase 3 (ArcadedbSessionDB) |
| **Разблокирует** | Phase 5 (Migration Tool) |

---

## Overview

Переключение всех 30+ потребителей `SessionDB` на фабрику `create_session_db()`,
которая возвращает `ArcadedbSessionDB` или `SessionDB` (SQLite fallback).

### Стратегия

Минимальные изменения: каждый consumer вызывает `create_session_db()` вместо
`SessionDB()`. Сигнатуры методов не меняются. Логика не меняется.

**Главный файл:** `hermes_state.py` — становится factory + сохраняет `SessionDB`
для fallback.

---

## Files

### Модифицируемые файлы

#### Factory (1 файл)

| Файл | Изменение |
|------|-----------|
| `hermes_state.py` | Добавить `create_session_db()` factory. `SessionDB` класс остаётся для fallback |

#### Core (6 файлов)

| Файл | Строки | Текущий вызов | Новый вызов |
|------|--------|--------------|-------------|
| `run_agent.py` | ~12k | `SessionDB()` в `_ensure_db_session()` | `create_session_db()` |
| `cli.py` | ~11k | `SessionDB()` в `load_cli_config()` | `create_session_db()` |
| `agent/conversation_loop.py` | — | Через `run_agent.py` | Без изменений |
| `agent/conversation_compression.py` | — | Через `run_agent.py` | Без изменений |
| `agent/context_compressor.py` | — | `get_compression_failure_cooldown()` | Без изменений |
| `agent/agent_runtime_helpers.py` | — | `SessionDB` flush | Без изменений |

#### CLI subcommands (6 файлов)

| Файл | Строки | Изменение |
|------|--------|-----------|
| `hermes_cli/cli_commands_mixin.py` | — | `self.session_db` → factory |
| `hermes_cli/cli_agent_setup_mixin.py` | — | `SessionDB()` → factory |
| `hermes_cli/web_server.py` | — | `SessionDB()` → factory |
| `hermes_cli/doctor.py` | — | `SessionDB()` → factory |
| `hermes_cli/backup.py` | — | `SessionDB()` → factory |
| `hermes_cli/main.py` | — | `SessionDB()` → factory |

#### Gateway (5 файлов)

| Файл | Строки | Изменение |
|------|--------|-----------|
| `gateway/run.py` | — | `AsyncSessionDB(SessionDB())` |
| `gateway/session.py` | — | `SessionDB()` → factory |
| `gateway/slash_commands.py` | — | Через `session.py` |
| `gateway/platforms/api_server.py` | — | `SessionDB()` → factory |
| `gateway/platforms/telegram/adapter.py` | — | Через `gateway/run.py` |

#### Прочие (5+ файлов)

| Файл | Изменение |
|------|-----------|
| `agent/agent_init.py` | `SessionDB()` → factory |
| `agent/insights.py` | Принимает SessionDB instance |
| `cron/scheduler.py` | `SessionDB()` → factory |
| `acp_adapter/session.py` | `SessionDB()` → factory |
| `mcp_serve.py` | `SessionDB()` → factory |
| `plugins/hermes-achievements/dashboard/plugin_api.py` | `SessionDB()` → factory |
| `hermes_cli/profiles.py` | `SessionDB()` → factory (cross-profile reads) |
| `hermes_cli/profile_distribution.py` | `SessionDB()` → factory |

---

## Factory Implementation

### `hermes_state.py` — добавление фабрики

```python
# hermes_state.py (добавить в конец файла, перед AsyncSessionDB)

def create_session_db(
    db_path: str | Path = None,
    read_only: bool = False,
    force_sqlite: bool = False,
) -> 'SessionDB':
    """
    Factory: returns ArcadedbSessionDB or SessionDB (SQLite).

    Решение на основе config.yaml:
      database.arcadedb.enabled: True → ArcadedbSessionDB
      database.arcadedb.enabled: False → SessionDB (SQLite)

    Fallback:
      Если ArcadeDB недоступен → SessionDB (SQLite) + warning в лог.

    Args:
        db_path: путь к state.db (только для SQLite fallback).
        read_only: read-only режим (только для SQLite fallback).
        force_sqlite: принудительно SQLite (для миграции, тестов).

    Returns:
        SessionDB instance (ArcadeDB или SQLite).
    """
    from hermes_cli.config import load_config

    if force_sqlite:
        return SessionDB(db_path, read_only)

    config = load_config()
    arcadedb_cfg = config.get("database", {}).get("arcadedb", {})

    if not arcadedb_cfg.get("enabled", False):
        return SessionDB(db_path, read_only)

    try:
        from hermes_cli.arcadedb import ArcadeDBConfig, ArcadeDBAdapter
        from hermes_cli.arcadedb_session import ArcadedbSessionDB
        from hermes_cli.arcadedb_lifecycle import ArcadeDBLifecycle

        # Убедиться что ArcadeDB запущен
        lifecycle = ArcadeDBLifecycle.from_config()
        if not lifecycle.ensure_started():
            logging.warning("ArcadeDB not available, falling back to SQLite")
            return SessionDB(db_path, read_only)

        # Подключиться
        db_config = ArcadeDBConfig(
            host=arcadedb_cfg["host"],
            port=arcadedb_cfg["port"],
            database=arcadedb_cfg["database"],
            user=arcadedb_cfg["user"],
            password=arcadedb_cfg["password"],
        )
        adapter = ArcadeDBAdapter(db_config)
        adapter.connect()

        # Embedder (ленивый — только для search)
        embedder = None
        if arcadedb_cfg.get("embedding", {}).get("enabled", False):
            from hermes_cli.embedder import create_embedder
            emb_config = config.get("auxiliary", {}).get("embedding", {})
            embedder = create_embedder(emb_config)

        return ArcadedbSessionDB(adapter=adapter, embedder=embedder)

    except Exception as e:
        logging.warning(
            "ArcadeDB initialization failed (%s), falling back to SQLite", e
        )
        return SessionDB(db_path, read_only)


# AsyncSessionDB остаётся без изменений — принимает любой SessionDB-compatible объект
class AsyncSessionDB:
    def __init__(self, db: 'SessionDB'):
        self._db = db
    # ... rest unchanged
```

---

## Примеры изменений в consumers

### `cli.py` (самый крупный consumer)

```python
# Было (примерно строка 200):
from hermes_state import SessionDB

class HermesCLI:
    def __init__(self, ...):
        self.session_db = SessionDB()

# Стало:
from hermes_state import create_session_db

class HermesCLI:
    def __init__(self, ...):
        self.session_db = create_session_db()
```

### `run_agent.py`

```python
# Было:
from hermes_state import SessionDB
db = SessionDB()

# Стало:
from hermes_state import create_session_db
db = create_session_db()
```

### `gateway/run.py`

```python
# Было:
from hermes_state import SessionDB, AsyncSessionDB
class GatewayRunner:
    def __init__(self, ...):
        self._session_db = AsyncSessionDB(SessionDB())

# Стало:
from hermes_state import create_session_db, AsyncSessionDB
class GatewayRunner:
    def __init__(self, ...):
        self._session_db = AsyncSessionDB(create_session_db())
```

---

## Порядок переключения (по критичности)

### Group 1: Core (должен работать для запуска агента)
1. `run_agent.py` — AIAgent session lifecycle
2. `cli.py` — CLI session lifecycle
3. `agent/conversation_loop.py` — message flush
4. `agent/conversation_compression.py` — compression locks

### Group 2: CLI & Maintenance
5. `hermes_cli/cli_commands_mixin.py` — slash commands
6. `hermes_cli/cli_agent_setup_mixin.py` — setup
7. `hermes_cli/web_server.py` — dashboard API
8. `hermes_cli/doctor.py` — health checks
9. `hermes_cli/backup.py` — backup/restore
10. `hermes_cli/main.py` — root CLI entry

### Group 3: Gateway
11. `gateway/run.py` — gateway lifecycle
12. `gateway/session.py` — session management
13. `gateway/slash_commands.py` — /undo, /title
14. `gateway/platforms/api_server.py` — API server
15. `gateway/platforms/telegram/adapter.py` — topic cleanup

### Group 4: Other
16. `agent/agent_init.py` — agent setup
17. `agent/insights.py` — analytics
18. `cron/scheduler.py` — cron sessions
19. `acp_adapter/session.py` — ACP integration
20. `mcp_serve.py` — MCP server
21. `plugins/hermes-achievements/dashboard/plugin_api.py`
22. `hermes_cli/profiles.py`
23. `hermes_cli/profile_distribution.py`

---

## Тестирование

**Тестовые файлы:**
- `tests/test_arcadedb_session_factory.py` → [см. Phase 1: factory tests](phase-1-testing.md#файл-5-teststest_arcadedb_session_factorypy)
- `tests/e2e/test_cli_arcadedb.py` → [см. Phase 1: CLI e2e](phase-1-testing.md#tests-e2e)
- `tests/e2e/test_gateway_arcadedb.py` → [см. Phase 1: gateway e2e](phase-1-testing.md#tests-e2e)

### Ключевые тесты

| ID | Тест | Описание |
|----|------|----------|
| F4-01 | `test_arcadedb_enabled` | enabled=True → ArcadedbSessionDB |
| F4-02 | `test_arcadedb_disabled` | enabled=False → SessionDB (SQLite) |
| F4-03 | `test_arcadedb_unavailable` | Ошибка → fallback на SQLite |
| F4-04 | `test_same_api_both` | Идентичные public methods |
| F4-05 | `test_session_db_no_break` | SQLite SessionDB не сломан |
| CLI-E2E-01 | `test_cli_session_lifecycle` | Создать → chat → search через ArcadeDB |
| CLI-E2E-02 | `test_cli_undo_retry` | /undo, /retry работают |
| GW-E2E-01 | `test_gateway_message` | Telegram message → session → response |
| GW-E2E-02 | `test_gateway_undo` | /undo через gateway |

---

## Acceptance Criteria

- [ ] `create_session_db()` возвращает правильный backend по конфигу
- [ ] `hermes` CLI запускается с ArcadeDB (enabled=True)
- [ ] `hermes` CLI запускается с SQLite (enabled=False) — без регрессий
- [ ] `hermes gateway` запускается с обоими backends
- [ ] Все slash commands работают: /new, /resume, /undo, /retry, /compress, /title
- [ ] Session search работает через ArcadeDB (hybrid или FTS5 fallback)
- [ ] Compression locks работают (атомарный CAS)
- [ ] Gateway handoff работает
- [ ] Telegram topic mode работает
- [ ] Существующие тесты (SQLite) проходят без регрессий
- [ ] Новые тесты factory проходят (F4-01 – F4-05)
- [ ] E2E тесты проходят (CLI, Gateway)

---

## Cross-References

### Предшествующие фазы
- **[← Phase 0: Lifecycle](phase-0-lifecycle.md)** — `ensure_started()` в factory
- **[← Phase 1: Testing](phase-1-testing.md)** — factory tests + E2E
- **[← Phase 2: Adapter v2](phase-2-adapter-v2.md)** — `ArcadeDBAdapter`
- **[← Phase 3: ArcadedbSessionDB](phase-3-sessiondb.md)** — full SessionDB API

### Последующие фазы
- **[→ Phase 5: Migration Tool](phase-5-migration-tool.md)** — использует factory

### Связи с существующими файлами
- **[`hermes_state.py`](../../hermes_state.py)** ← **добавляется factory**
- **[`run_agent.py`](../../run_agent.py)** ← **переключается**
- **[`cli.py`](../../cli.py)** ← **переключается**
- **[`gateway/run.py`](../../gateway/run.py)** ← **переключается**
- **[`gateway/session.py`](../../gateway/session.py)** ← **переключается**
- **[`hermes_cli/web_server.py`](../../hermes_cli/web_server.py)** ← **переключается**
- **[`hermes_cli/config.py`](../../hermes_cli/config.py)** ← database.arcadedb.enabled gate
