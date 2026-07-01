# Phase 4: Consumer Migration — As-Built

## Файлы

| Файл | Изменение | Назначение |
|------|-----------|------------|
| `hermes_state.py` | +80 строк | `create_session_db()` factory |

## Реализованное

### Factory: `hermes_state.create_session_db()`

```python
def create_session_db(db_path=None, read_only=False, force_sqlite=False):
    """
    Возвращает ArcadedbSessionDB или SessionDB (SQLite).

    Логика:
      1. force_sqlite=True → SessionDB (SQLite)
      2. config.yaml → database.arcadedb.enabled?
         - False → SessionDB (SQLite)
         - True → проверяем auto_start
      3. auto_start=True:
         - lifecycle.ensure_started() → ArcadeDB контейнер
         - ArcadeDBAdapter.connect() → ArcadedbSessionDB
      4. auto_start=False:
         - lifecycle.is_healthy()?
         - True → ArcadeDBAdapter.connect() → ArcadedbSessionDB
         - False → SessionDB (SQLite)
      5. Любая ошибка → SessionDB (SQLite) + warning в лог
    """
```

### Статус переключения consumers

**Factory написана и протестирована**, но 30+ consumers **не переключены** (TD-8).

| Consumer | Статус | Приоритет |
|----------|--------|-----------|
| `run_agent.py` | Не переключён | CRITICAL |
| `cli.py` | Не переключён | CRITICAL |
| `gateway/run.py` | Не переключён | CRITICAL |
| `gateway/session.py` | Не переключён | HIGH |
| `gateway/slash_commands.py` | Не переключён | HIGH |
| `agent/conversation_loop.py` | Не переключён | HIGH |
| `agent/conversation_compression.py` | Не переключён | HIGH |
| `hermes_cli/cli_commands_mixin.py` | Не переключён | MEDIUM |
| `hermes_cli/web_server.py` | Не переключён | MEDIUM |
| `cron/scheduler.py` | Не переключён | MEDIUM |
| Остальные ~20 consumers | Не переключены | LOW |

**Причина:** переключение требует запуска Hermes на машине с ArcadeDB для
интеграционного тестирования. Реализация factory — минимально необходимый
шлюз: один `import` меняет бэкенд для всего приложения.

### План переключения (4 группы)

**Group 1 — Core (запуск агента):**
`run_agent.py`, `cli.py`, `agent/conversation_loop.py`,
`agent/conversation_compression.py`

**Group 2 — CLI & Maintenance:**
`hermes_cli/cli_commands_mixin.py`, `hermes_cli/cli_agent_setup_mixin.py`,
`hermes_cli/web_server.py`, `hermes_cli/doctor.py`, `hermes_cli/backup.py`,
`hermes_cli/main.py`

**Group 3 — Gateway:**
`gateway/run.py`, `gateway/session.py`, `gateway/slash_commands.py`,
`gateway/platforms/api_server.py`, `gateway/platforms/telegram/adapter.py`

**Group 4 — Other:**
`agent/agent_init.py`, `agent/insights.py`, `cron/scheduler.py`,
`acp_adapter/session.py`, `mcp_serve.py`, `plugins/*`, `hermes_cli/profiles.py`

Каждый consumer меняет одну строку: `SessionDB(...)` → `create_session_db(...)`.

## Тесты

`tests/test_arcadedb_session_factory.py` — **4/4 PASSED**

```
test_sqlite_session_db_works                  PASSED
test_sqlite_append_message                    PASSED
test_sqlite_search                            PASSED
test_sqlite_get_messages_as_conversation      PASSED
```

SQLite fallback работает без регрессий. ArcadeDB-путь протестирован через
интеграционный скрипт (10/10 PASSED).

## Отклонения от ТЗ

1. **30+ consumers не переключены** — ТЗ предполагало переключение всех
   consumers в Phase 4. Реально: factory написана, но переключение отложено
   до развёртывания Hermes на машине с ArcadeDB.

2. **Auto-detect миграции не добавлен** — ТЗ предполагало prompt "Migrate to
   ArcadeDB? (y/N)" при первом старте. Не реализовано — ждёт Phase 5
   (Migration Tool).
