# Phase 0: ArcadeDB Lifecycle Manager

| Поле | Значение |
|------|----------|
| **Номер** | Phase 0 |
| **Название** | ArcadeDB Lifecycle Manager |
| **Новых строк** | ~400 |
| **Сложность** | Medium |
| **Зависит от** | None |
| **Разблокирует** | Phase 1 (Testing), Phase 2 (Adapter v2) |

---

## Overview

ArcadeDB Lifecycle Manager отвечает за управление жизненным циклом Docker-контейнера
ArcadeDB. Hermes сам запускает/останавливает контейнер, проверяет здоровье,
перезапускает при падении.

### Принцип: Zero-config для пользователя

Пользователь не должен знать, что под капотом Docker. При старте `hermes` или
`hermes gateway` Lifecycle Manager автоматически:
1. Проверяет наличие Docker
2. Проверяет, запущен ли уже ArcadeDB (по порту 5432)
3. Если нет — запускает контейнер
4. Ждёт health check (SELECT 1)
5. Возвращает управление

При завершении: `hermes gateway stop` → graceful shutdown контейнера.

---

## Files

### Новые файлы

| Файл | Строки | Назначение |
|------|--------|------------|
| `hermes_cli/arcadedb_lifecycle.py` | ~400 | Класс `ArcadeDBLifecycle` |

### Модифицируемые файлы

| Файл | Изменение | Связь |
|------|-----------|-------|
| `hermes_cli/config.py` | Добавить `database.arcadedb.*` блок в `DEFAULT_CONFIG` | [см. config.py:DEFAULT_CONFIG](../../hermes_cli/config.py) |
| `docker-compose.yml` | Добавить сервис `arcadedb` | [см. docker-compose.yml](../../docker-compose.yml) |
| `docker-compose.windows.yml` | Добавить сервис `arcadedb` | [см. docker-compose.windows.yml](../../docker-compose.windows.yml) |
| `cli.py` | Добавить `lifecycle.ensure_started()` при старте CLI | [см. Phase 4: cli.py switch](phase-4-consumers.md#cli-py) |
| `gateway/run.py` | Добавить `lifecycle.ensure_started()` при старте gateway | [см. Phase 4: gateway/run.py switch](phase-4-consumers.md#gateway-run-py) |
| `pyproject.toml` | Без изменений (использует `subprocess` + `psutil`) | [см. pyproject.toml](../../pyproject.toml) |

---

## API Specification

```python
# hermes_cli/arcadedb_lifecycle.py

from dataclasses import dataclass, field
from hermes_cli.config import load_config

@dataclass
class ArcadeDBLifecycleConfig:
    """Конфигурация ArcadeDB lifecycle."""
    enabled: bool = True
    auto_start: bool = True
    host: str = "localhost"
    port: int = 5432
    http_port: int = 2480
    database: str = "hermes"
    user: str = "root"
    password: str = ""          # генерируется при первом запуске
    docker_image: str = "arcadedb/arcadedb:26.7.1"
    memory_limit: str = "4g"    # -Xmx JVM
    data_dir: str = ""          # ~/.hermes/arcadedb/data
    timeout: float = 30.0

class ArcadeDBLifecycle:
    """Manages ArcadeDB Docker container lifecycle."""

    # ---- Lifecycle ----

    def __init__(self, config: ArcadeDBLifecycleConfig = None):
        """
        Загружает конфиг из config.yaml (database.arcadedb.*).
        Если password пуст — генерирует 32-char hex.
        Сохраняет пароль в config.yaml (persisted).
        """

    def ensure_started(self) -> bool:
        """
        Идемпотентно гарантирует что ArcadeDB запущен и здоров.
        Используется при старте hermes и hermes gateway.

        Flow:
          1. check_docker() — проверяет наличие docker CLI
          2. If not running → start()
          3. wait_healthy(timeout=30s, interval=2s)
          4. ensure_schema() — создаёт БД и типы через Phase 2 adapter

        Returns:
          True если ArcadeDB готов к работе

        Raises:
          ArcadeDBLifecycleError если Docker не установлен и auto_start=True
        """

    def start(self) -> None:
        """
        Запускает Docker-контейнер.

        Команда:
          docker run -d --name hermes-arcadedb
            -p 5432:5432 -p 2480:2480
            -e ARCADEDB_ROOT_PASSWORD=<password>
            -e JAVA_OPTS="-Xmx<memory_limit> -Xms256m"
            -v <data_dir>:/storage
            --restart unless-stopped
            arcadedb/arcadedb:26.7.1

        Ожидает появления порта 5432, затем вызывает wait_healthy().
        """

    def stop(self) -> None:
        """
        Graceful shutdown:
          docker stop hermes-arcadedb
          (опционально) docker rm hermes-arcadedb
        """

    def restart(self) -> None:
        """Перезапуск: stop() + start()."""

    # ---- Health ----

    def is_running(self) -> bool:
        """
        Проверяет запущен ли контейнер:
          docker ps --filter name=hermes-arcadedb --format '{{.Status}}'

        Returns True если контейнер в статусе 'Up'.
        """

    def is_healthy(self) -> bool:
        """
        Проверяет здоровье ArcadeDB через psycopg:
          conn = psycopg.connect(host, port, dbname, user, password, connect_timeout=2)
          conn.execute("SELECT 1")
          conn.close()

        Returns True если SELECT 1 успешен.
        Не импортит ArcadedbAdapter напрямую (только psycopg для health check).
        """

    def wait_healthy(self, timeout: float = 30.0, interval: float = 2.0) -> bool:
        """
        Polls is_healthy() каждые interval секунд.
        Returns True когда здоров.
        Raises TimeoutError если timeout исчерпан.
        """

    # ---- Docker ----

    def check_docker(self) -> bool:
        """
        Проверяет наличие Docker:
          docker --version

        Returns True если Docker CLI доступен.
        """

    # ---- Schema ----

    def ensure_schema(self) -> None:
        """
        Создаёт БД и схему если первый запуск.

        Использует ArcadedbAdapter v2 (Phase 2) или HTTP API (fallback):
          - CREATE DATABASE <name> IF NOT EXISTS
          - SchemaManager.create_all() (из arcadedb_schema.py)

        Идемпотентно — если схема уже есть, ничего не делает.
        """

    # ---- Config ----

    @staticmethod
    def from_config() -> 'ArcadeDBLifecycle':
        """Factory: читает database.arcadedb.* из config.yaml."""

    @staticmethod
    def is_enabled() -> bool:
        """Проверяет database.arcadedb.enabled в config.yaml."""

    # ---- Password management ----

    def _ensure_password(self) -> str:
        """Генерирует и сохраняет пароль если не задан."""

    def _save_config(self) -> None:
        """Сохраняет изменения в config.yaml (persists password)."""
```

---

## Config Block (добавить в `hermes_cli/config.py`)

```python
# В DEFAULT_CONFIG dict (hermes_cli/config.py:line ~120)

"database": {
    "arcadedb": {
        "enabled": False,              # default: SQLite
        "auto_start": True,            # auto-manage container
        "host": "localhost",
        "port": 5432,                  # PostgreSQL wire protocol
        "http_port": 2480,             # HTTP Studio (optional)
        "database": "hermes",
        "user": "root",
        "password": "",                # auto-generated on first start
        "docker_image": "arcadedb/arcadedb:26.7.1",
        "memory_limit": "4g",
        "data_dir": "",                # empty = ~/.hermes/arcadedb/data
        "timeout": 30.0,               # connect/query timeout
    },
},
```

### Связи с конфигом

- Текущий `auxiliary.embedding` блок остаётся без изменений → [см. config.py:auxiliary.embedding](../../hermes_cli/config.py)
- Env vars `ARCADE_HOST`, `ARCADE_PORT` etc → **удаляются** из `tools/session_search_tool.py` (мигрируют в config.yaml) → [см. Phase 3: session_search_tool](phase-3-sessiondb.md#session-search-tool)
- `OPTIONAL_ENV_VARS` в `config.py` — **не добавляем** (это config, не secret)

---

## Docker Compose (добавить в существующие файлы)

### В `docker-compose.yml` (дополнить существующий)

```yaml
services:
  # ... существующие gateway, dashboard сервисы ...

  arcadedb:
    image: arcadedb/arcadedb:26.7.1
    container_name: hermes-arcadedb
    ports:
      - "5432:5432"    # PostgreSQL wire protocol (основной)
      - "2480:2480"    # HTTP API + Studio (опционально)
    environment:
      ARCADEDB_ROOT_PASSWORD: ${ARCADEDB_PASSWORD:-hermes123}
      JAVA_OPTS: "-Xmx4g -Xms256m"
    volumes:
      - arcadedb_data:/storage
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:2480/api/v1/server"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  arcadedb_data:
```

### В `docker-compose.windows.yml` (тот же блок)

Адаптировать под Windows paths для volumes.

---

## Файл `hermes_cli/arcadedb_lifecycle.py`

### Структура

```
hermes_cli/arcadedb_lifecycle.py (~400 строк)
│
├── [1-30]   Imports: subprocess, psutil, logging, dataclasses
├── [32-50]  ArcadeDBLifecycleConfig dataclass
├── [52-68]  ArcadeDBLifecycleError exception class
│
├── [70-100] ArcadeDBLifecycle.__init__()
├── [102-140] ArcadeDBLifecycle.ensure_started()
├── [142-180] ArcadeDBLifecycle.start()
├── [182-200] ArcadeDBLifecycle.stop()
├── [202-210] ArcadeDBLifecycle.restart()
│
├── [212-230] ArcadeDBLifecycle.is_running()
├── [232-260] ArcadeDBLifecycle.is_healthy()
├── [262-290] ArcadeDBLifecycle.wait_healthy()
├── [292-310] ArcadeDBLifecycle.check_docker()
│
├── [312-350] ArcadeDBLifecycle.ensure_schema()
│
├── [352-380] ArcadeDBLifecycle._ensure_password()
├── [382-400] ArcadeDBLifecycle._save_config()
│
└── [402-410] Module-level: _LIFECYCLE_INSTANCE singleton
```

### Dependencies

```python
# Внутренние зависимости от других файлов репозитория
from hermes_cli.config import load_config           # [см. config.py:load_config](../../hermes_cli/config.py)
from hermes_cli.arcadedb_schema import SchemaManager  # [см. arcadedb_schema.py](../../hermes_cli/arcadedb_schema.py)
# ArcadedbAdapter импортируется ЛЕНИВО (только в ensure_schema)
# чтобы избежать циклического импорта с Phase 2
```

---

## Тест-кейсы

**Тестовый файл:** `tests/test_arcadedb_lifecycle.py` → [см. Phase 1: lifecycle tests](phase-1-testing.md#test_arcadedb_lifecycle)

| ID | Тест | Описание | Ожидаемое поведение |
|----|------|----------|---------------------|
| L0-01 | `test_docker_available` | Docker CLI доступен | `check_docker() == True` |
| L0-02 | `test_docker_unavailable_graceful` | Docker отсутствует, auto_start=True | `ArcadeDBLifecycleError` с понятным сообщением |
| L0-03 | `test_docker_unavailable_fallback` | Docker отсутствует, auto_start=False | `ensure_started() == False`, no exception |
| L0-04 | `test_start_stop_container` | Полный цикл start → stop | start() успешен, container running, stop() успешен |
| L0-05 | `test_health_check` | is_healthy() против запущенного контейнера | `SELECT 1` успешен → True |
| L0-06 | `test_health_check_unhealthy` | is_healthy() против выключенного | Connection refused → False |
| L0-07 | `test_wait_healthy_timeout` | Контейнер не запускается >30s | `TimeoutError` |
| L0-08 | `test_ensure_started_idempotent` | Двойной вызов ensure_started() | Второй вызов сразу возвращает True |
| L0-09 | `test_ensure_schema_idempotent` | Двойной вызов ensure_schema() | Не вызывает ошибок |
| L0-10 | `test_config_persisted` | Password генерируется и сохраняется | После `_ensure_password()` config.yaml содержит password |
| L0-11 | `test_config_read` | `from_config()` читает config.yaml | Все поля соответствуют config.yaml |
| L0-12 | `test_password_generation` | Пустой password → генерация | 32 hex chars, сохранён в config |

### Фикстуры для тестов (в `tests/fixtures/arcadedb_fixtures.py`)

```python
# [см. Phase 1: fixtures](phase-1-testing.md#arcadedb_fixtures)

@pytest.fixture
def arcadedb_config(tmp_path):
    """Temp config с database.arcadedb для тестов."""

@pytest.fixture
def mock_docker_available(mocker):
    """Mocks docker CLI как доступный."""

@pytest.fixture
def mock_docker_unavailable(mocker):
    """Mocks docker CLI как отсутствующий."""

@pytest.fixture
def arcadedb_lifecycle(arcadedb_config, mock_docker_available):
    """Возвращает ArcadeDBLifecycle с temp config."""

@pytest.fixture
def running_arcadedb(arcadedb_lifecycle):
    """Запускает ArcadeDB контейнер для интеграционных тестов."""
```

---

## Acceptance Criteria

- [ ] `ArcadeDBLifecycle.ensure_started()` запускает Docker-контейнер и ждёт health
- [ ] Docker absent → graceful сообщение об ошибке
- [ ] `auto_start: false` → skip, работать с внешним ArcadeDB
- [ ] `is_healthy()` проверяет `SELECT 1` через psycopg
- [ ] Password auto-generation + persist в config.yaml
- [ ] Конфиг читается из `database.arcadedb.*` секции config.yaml
- [ ] `docker-compose.yml` содержит сервис arcadedb
- [ ] Все 12 тест-кейсов проходят (Phase 1)
- [ ] Нет утечек контейнеров после тестов (cleanup)

---

## Cross-References

### Предшествующие фазы
- **None** — это точка входа

### Последующие фазы
- **[→ Phase 1: Testing Framework](phase-1-testing.md)** — тесты для lifecycle manager
- **[→ Phase 2: Adapter v2](phase-2-adapter-v2.md)** — использует `lifecycle.ensure_started()` перед connect
- **[→ Phase 3: ArcadedbSessionDB](phase-3-sessiondb.md)** — использует `lifecycle.ensure_started()` при init
- **[→ Phase 4: Consumer Migration](phase-4-consumers.md)** — `cli.py` и `gateway/run.py` вызывают lifecycle

### Связи с существующими файлами
- **[`hermes_cli/config.py:DEFAULT_CONFIG`](../../hermes_cli/config.py)** — добавить `database.arcadedb` блок
- **[`hermes_cli/arcadedb_schema.py:SchemaManager`](../../hermes_cli/arcadedb_schema.py)** — used in `ensure_schema()`
- **[`docker-compose.yml`](../../docker-compose.yml)** — добавить сервис
- **[`docker-compose.windows.yml`](../../docker-compose.windows.yml)** — добавить сервис
- **[`tools/session_search_tool.py:_init_graph_store`](../../tools/session_search_tool.py)** — перестанет читать env vars, перейдёт на config

### Связи внутри документации
- **[Phase 1: test_arcadedb_lifecycle.py](phase-1-testing.md#test_arcadedb_lifecycle)** — тест-контракты
- **[Phase 1: arcadedb_fixtures.py](phase-1-testing.md#arcadedb_fixtures)** — shared fixtures

---

## Implementation Sequence

```
1. Config block (hermes_cli/config.py)
2. ArcadeDBLifecycleConfig dataclass
3. check_docker() + _ensure_password()
4. start() + stop()
5. is_healthy() + wait_healthy()
6. ensure_started() — главный entry point
7. ensure_schema() — делегирует в SchemaManager
8. Docker compose entries
9. Интеграция в cli.py и gateway/run.py (Phase 4)
```

## Notes

- **Singleton pattern:** `ArcadeDBLifecycle` — один инстанс на процесс
  - CLI: создаётся в `cli.py`, живёт пока работает CLI
  - Gateway: создаётся в `gateway/run.py`, живёт пока работает gateway
  - Не нужно IPC между процессами (каждый управляет своим контейнером)
- **Graceful shutdown:** `atexit.register(lifecycle.stop)` или `try/finally`
- **Port conflicts:** проверять, не занят ли порт 5432 другим процессом
- **Windows Docker:** использовать `docker.exe` из PATH
