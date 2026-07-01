# Phase 0: ArcadeDB Lifecycle Manager — As-Built

## Файлы

| Файл | Строки | Назначение |
|------|--------|------------|
| `hermes_cli/arcadedb_lifecycle.py` | 260 | `ArcadeDBLifecycle` класс |
| `hermes_cli/config.py` | +30 | `database.arcadedb.*` блок в `DEFAULT_CONFIG` |
| `docker-compose.yml` | +23 | Сервис `arcadedb` |
| `docker-compose.windows.yml` | +25 | Сервис `arcadedb` (Windows) |

## Реализованные методы

```
ArcadeDBLifecycle
├── ensure_started()    → idempotent start + health wait
├── start()             → docker run
├── stop()              → docker stop
├── restart()           → stop + start
├── is_running()        → docker ps --filter
├── is_healthy()        → psycopg SELECT 1
├── wait_healthy()      → poll is_healthy до timeout
├── check_docker()      → docker --version
├── ensure_schema()     → SchemaManager.create_all()
├── from_config()       → factory из config.yaml
├── is_enabled()        → чтение database.arcadedb.enabled
└── _ensure_password()  → генерация + persist пароля
```

## Config Block

```yaml
database:
  arcadedb:
    enabled: false           # default: SQLite
    auto_start: true         # auto-manage Docker container
    host: localhost
    port: 5432               # PostgreSQL wire protocol
    http_port: 2480          # HTTP Studio (optional)
    database: hermes
    user: root
    password: ""             # auto-generated on first start
    docker_image: arcadedb/arcadedb:26.7.1
    memory_limit: 4g         # -Xmx JVM heap
    data_dir: ""             # empty = ~/.hermes/arcadedb/data
    timeout: 30.0            # connect/query timeout
```

## Отклонения от ТЗ

1. **Docker image**: ТЗ — `arcadedb/arcadedb:latest`. Реальность — на сервере используется
   `arcadedata/arcadedb:latest` (другой реестр). Конфиг позволяет переопределить.

2. **Container name check**: `is_running()` проверяет `hermes-arcadedb`.
   Если контейнер запущен под другим именем (напр. `hermes-db-arcadedb-1`
   из docker-compose) — `is_running()` вернёт False и `ensure_started()`
   попытается запустить второй контейнер. **Решение**: `auto_start: false`
   для externally-managed контейнеров.

3. **Docker CLI required**: `check_docker()` проверяет `docker --version`.
   Не работает с podman/Docker Desktop без CLI в PATH.

## Тесты

`tests/test_arcadedb_lifecycle.py` — **13/13 PASSED**

```
TestDockerDetection
  test_docker_available          PASSED
  test_docker_unavailable        PASSED

TestContainerLifecycle
  test_start_calls_docker        PASSED
  test_start_already_running     PASSED
  test_stop_calls_docker         PASSED

TestHealthCheck
  test_healthy_mock              PASSED
  test_unhealthy_mock            PASSED
  test_wait_healthy_timeout      PASSED

TestEnsureStarted
  test_disabled_skips            PASSED
  test_auto_start_false_not_running PASSED
  test_auto_start_true_no_docker PASSED
  test_already_running_healthy   PASSED

TestConfig
  test_password_generation       PASSED
```
