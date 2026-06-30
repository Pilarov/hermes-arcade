# Phase 1: Testing Framework & Test Cases

| Поле | Значение |
|------|----------|
| **Номер** | Phase 1 |
| **Название** | Testing Framework — Contracts First |
| **Новых строк** | ~2,500 (тесты) + ~500 (фикстуры) |
| **Сложность** | **CRITICAL** |
| **Зависит от** | Phase 0 (Lifecycle) |
| **Разблокирует** | Phase 2 (Adapter), Phase 3 (SessionDB), Phase 5 (Migration), Phase 6 (KanbanDB), Phase 7 (Memory Store), Phase 8 (Other DBs) |

---

## Overview

Фаза 1 создаёт полный тестовый каркас **до** написания production-кода. Все тесты
заведомо **падают на старте** — они определяют контракты поведения, которые
последующие фазы должны реализовать.

### Принцип: TDD для замены storage

Каждый тестовый файл = спецификация API:
- **Arrange:** фикстуры подготавливают ArcadeDB контейнер, embedder, конфиг
- **Act:** вызывают метод будущего API
- **Assert:** проверяют возвращаемые значения, side effects, edge cases

Ветка `main` после Phase 1 должна содержать падающие тесты —
это нормально. Они задают target для Phase 2+.

---

## Files

### Новые файлы (~3,000 строк)

| Файл | Строки | Тестируемый компонент | Связанная фаза |
|------|--------|----------------------|----------------|
| `tests/fixtures/arcadedb_fixtures.py` | ~200 | Shared fixtures | Все фазы |
| `tests/test_arcadedb_lifecycle.py` | ~150 | `ArcadeDBLifecycle` | Phase 0 |
| `tests/test_arcadedb_adapter.py` | ~200 | `ArcadedbAdapter` (psycopg) | Phase 2 |
| `tests/test_arcadedb_session.py` | ~800 | `ArcadedbSessionDB` (80+ методов) | Phase 3 |
| `tests/test_arcadedb_session_factory.py` | ~150 | `create_session_db()` factory | Phase 4 |
| `tests/test_arcadedb_search.py` | ~250 | FTS5→Lucene эквиваленты | Phase 3 |
| `tests/test_arcadedb_compression_locks.py` | ~150 | Compression locks protocol | Phase 3 |
| `tests/test_arcadedb_telegram_topics.py` | ~100 | Telegram topic tables | Phase 3 |
| `tests/test_arcadedb_migration.py` | ~250 | SQLite→ArcadeDB миграция | Phase 5 |
| `tests/test_arcadedb_kanban.py` | ~350 | KanbanDB CAS + edges | Phase 6 |
| `tests/test_arcadedb_memory.py` | ~150 | MemoryStore (HRR→vector) | Phase 7 |
| `tests/test_arcadedb_projects.py` | ~100 | Projects DB | Phase 7 |
| `tests/e2e/test_cli_arcadedb.py` | ~200 | CLI session lifecycle | Phase 4+ |
| `tests/e2e/test_gateway_arcadedb.py` | ~200 | Gateway session lifecycle | Phase 4+ |
| `tests/e2e/test_migration_e2e.py` | ~150 | Full migration flow | Phase 5 |

### Модифицируемые файлы

| Файл | Изменение | Связь |
|------|-----------|-------|
| `pyproject.toml` | Добавить `psycopg[binary]>=3.1,<4` в dev + test deps | [см. pyproject.toml](../../pyproject.toml) |
| `tests/conftest.py` | Импортировать arcadedb fixtures | [см. conftest.py](../../tests/conftest.py) |

---

## Файл 1: `tests/fixtures/arcadedb_fixtures.py` (~200 строк)

Это ядро тестовой инфраструктуры. Все тесты используют эти фикстуры.

### Структура

```
tests/fixtures/arcadedb_fixtures.py
│
├── [1-15]   Imports: pytest, psycopg, subprocess, tempfile, os, yaml
├── [17-50]  Константы (DOCKER_IMAGE, TEST_DB, TEST_USER, TEST_PASSWORD, ...)
│
├── [52-80]  @pytest.fixture(scope="session") arcadedb_container
│   ├── Запускает Docker-контейнер (scope=session = один на все тесты)
│   ├── Ждёт health check (SELECT 1)
│   ├── Создаёт тестовую БД
│   ├── Применяет schema (SchemaManager.create_all)
│   └── yield → после тестов: останавливает контейнер (если managed)
│
├── [82-110] @pytest.fixture arcadedb_conn
│   ├── Создаёт psycopg connection к тестовой БД
│   ├── autocommit=True (для schema операций)
│   └── yield → закрывает connection
│
├── [112-150] @pytest.fixture arcadedb_adapter
│   ├── Создаёт ArcadedbAdapter(arcadedb_config)
│   ├── connect()
│   └── yield → close()
│
├── [152-180] @pytest.fixture arcadedb_session
│   ├── Создаёт ArcadedbSessionDB() с arcadedb_adapter + mock_embedder
│   └── yield → close()
│
├── [182-200] @pytest.fixture arcadedb_config
│   ├── Temp ArcadeDBConfig с тестовыми параметрами
│   └── host=localhost, port=5432, database=hermes_test
│
├── [202-220] @pytest.fixture mock_embedder
│   ├── Mock EmbedderProvider
│   ├── embed(["text"]) → [EmbeddingResult(dense=[0.1]*1024)]
│   └── Используется во всех тестах без real model loading
│
├── [222-240] @pytest.fixture session_data
│   ├── Предопределённые session dicts для тестов
│   └── {id, source, model, started_at, ...}
│
└── [242-260] @pytest.fixture message_data
    ├── Предопределённые message dicts для тестов
    └── [{role, content, timestamp, ...}, ...]
```

### Dependencies

```python
# Импорты из будущих модулей (Phase 2, 3)
from hermes_cli.arcadedb import ArcadeDBAdapter, ArcadeDBConfig       # Phase 2
from hermes_cli.arcadedb_lifecycle import ArcadeDBLifecycle            # Phase 0
from hermes_cli.arcadedb_session import ArcadedbSessionDB              # Phase 3
from hermes_cli.arcadedb_schema import SchemaManager                    # existing
from hermes_cli.embedder import EmbedderProvider, EmbeddingResult       # existing
```

### Связи
- **[→ Phase 0: ArcadeDBLifecycle](phase-0-lifecycle.md)** — used in `arcadedb_container` fixture
- **[→ Phase 2: ArcadeDBAdapter](phase-2-adapter-v2.md)** — used in `arcadedb_adapter` fixture
- **[→ Phase 3: ArcadedbSessionDB](phase-3-sessiondb.md)** — used in `arcadedb_session` fixture
- **[`hermes_cli/arcadedb_schema.py`](../../hermes_cli/arcadedb_schema.py)** — schema creation
- **[`tests/conftest.py`](../../tests/conftest.py)** — auto-import fixtures

---

## Файл 2: `tests/test_arcadedb_lifecycle.py` (~150 строк)

Тестирует `ArcadeDBLifecycle` из Phase 0.

### Тест-кейсы

```python
# tests/test_arcadedb_lifecycle.py

class TestDockerDetection:
    """Проверка обнаружения Docker."""

    def test_docker_available(self, mock_docker_cli):
        """L0-01: Docker CLI доступен → check_docker() == True"""
        # Arrange: mock subprocess.run('docker --version') → success
        # Act: lifecycle.check_docker()
        # Assert: True

    def test_docker_unavailable(self, mock_docker_cli_missing):
        """L0-02: Docker CLI отсутствует → check_docker() == False"""
        # Arrange: mock subprocess.run('docker --version') → FileNotFoundError
        # Act: lifecycle.check_docker()
        # Assert: False

class TestContainerLifecycle:
    """Проверка жизненного цикла контейнера."""

    def test_start_stop(self, arcadedb_lifecycle):
        """L0-04: start() → container running, stop() → stopped"""
        # Arrange: arcadedb_lifecycle с mock docker
        # Act: lifecycle.start()
        # Assert: lifecycle.is_running() == True
        # Act: lifecycle.stop()
        # Assert: lifecycle.is_running() == False

    def test_ensure_started_idempotent(self, arcadedb_lifecycle):
        """L0-08: Двойной вызов ensure_started()"""
        # Act: lifecycle.ensure_started()
        # Act: lifecycle.ensure_started()  # второй раз
        # Assert: оба возвращают True, контейнер не перезапущен

class TestHealthCheck:
    """Проверка health проверок."""

    def test_healthy(self, arcadedb_lifecycle, running_arcadedb):
        """L0-05: SELECT 1 успешен"""
        # Act: lifecycle.is_healthy()
        # Assert: True

    def test_unhealthy(self, arcadedb_lifecycle):
        """L0-06: Нет контейнера → is_healthy() == False"""
        # Arrange: контейнер не запущен
        # Act: lifecycle.is_healthy()
        # Assert: False

    def test_wait_healthy_timeout(self, arcadedb_lifecycle):
        """L0-07: Контейнер не становится здоровым > timeout"""
        # Arrange: mock is_healthy() всегда возвращает False
        # Act: lifecycle.wait_healthy(timeout=1.0)
        # Assert: TimeoutError

class TestConfig:
    """Проверка конфигурации."""

    def test_config_read(self, tmp_config_yaml):
        """L0-11: from_config() читает config.yaml"""
        # Arrange: временный config.yaml с database.arcadedb
        # Act: lifecycle = ArcadeDBLifecycle.from_config()
        # Assert: lifecycle.config == expected_config

    def test_password_generation(self, tmp_config_empty_password):
        """L0-12: Пустой password → генерация 32 hex chars"""
        # Arrange: config с password=""
        # Act: lifecycle = ArcadeDBLifecycle.from_config()
        # Assert: len(lifecycle.config.password) == 32
        # Assert: password saved to config

class TestSchema:
    """Проверка инициализации схемы."""

    def test_ensure_schema_idempotent(self, arcadedb_lifecycle, running_arcadedb):
        """L0-09: Двойной вызов ensure_schema()"""
        # Act: lifecycle.ensure_schema()
        # Act: lifecycle.ensure_schema()  # второй раз
        # Assert: оба успешны, нет дубликатов типов

class TestGracefulDegradation:
    """Проверка fallback при отсутствии Docker."""

    def test_auto_start_true_no_docker(self, arcadedb_lifecycle_no_docker):
        """L0-02b: auto_start=True + Docker отсутствует → Error"""
        # Act: lifecycle.ensure_started()
        # Assert: ArcadeDBLifecycleError с message "Docker is required"

    def test_auto_start_false(self, arcadedb_lifecycle_disabled):
        """L0-03: auto_start=False → False без ошибки"""
        # Act: lifecycle.ensure_started()
        # Assert: False
```

### Связи
- **Фикстуры:** [`tests/fixtures/arcadedb_fixtures.py`](#файл-1-testsfixturesarcadedb_fixturespy)
- **Тестируемый модуль:** [`hermes_cli/arcadedb_lifecycle.py`](../../hermes_cli/arcadedb_lifecycle.py) ← Phase 0
- **[→ Phase 0: Lifecycle Manager](phase-0-lifecycle.md)** — спецификация тестируемого API

---

## Файл 3: `tests/test_arcadedb_adapter.py` (~200 строк)

Тестирует `ArcadedbAdapter` из Phase 2.

### Тест-кейсы

```python
# tests/test_arcadedb_adapter.py

class TestConnection:
    """Проверка подключения."""

    def test_connect_success(self, arcadedb_config, running_arcadedb):
        """A2-01: Успешное подключение к работающему ArcadeDB"""
        # Arrange: ArcadedbConfig с правильными параметрами
        # Act: adapter = ArcadedbAdapter(config); adapter.connect()
        # Assert: adapter.connected == True

    def test_connect_failure(self, arcadedb_config_wrong_host):
        """A2-02: Ошибка подключения к недоступному хосту"""
        # Arrange: ArcadedbConfig с неверным host
        # Act: adapter.connect()
        # Assert: ArcadeDBError с connection refused

class TestTransaction:
    """Проверка транзакций."""

    def test_commit(self, arcadedb_adapter):
        """A2-03: INSERT в транзакции → COMMIT → данные видны"""
        # Arrange: adapter.begin()
        # Act: adapter.execute("CREATE VERTEX TestTx SET name = 'tx-test'")
        # Act: adapter.commit()
        # Assert: SELECT FROM TestTx → ['tx-test']

    def test_rollback(self, arcadedb_adapter):
        """A2-04: INSERT в транзакции → ROLLBACK → данных нет"""
        # Arrange: adapter.begin()
        # Act: adapter.execute("CREATE VERTEX TestTx SET name = 'rolled-back'")
        # Act: adapter.rollback()
        # Assert: SELECT FROM TestTx → []

    def test_transact_context_manager(self, arcadedb_adapter):
        """A2-05: transact() обёртка — atomic commit"""
        # Arrange + Act:
        def _do(cur):
            cur.execute("CREATE VERTEX TestTx SET name = 'cm1'")
            cur.execute("CREATE VERTEX TestTx SET name = 'cm2'")
            return True
        result = arcadedb_adapter.transact(_do)
        # Assert: result == True, обе записи есть

    def test_transact_rollback_on_error(self, arcadedb_adapter):
        """A2-06: transact() с ошибкой → rollback, ни одной записи"""
        def _do(cur):
            cur.execute("CREATE VERTEX TestTx SET name = 'ok'")
            raise ValueError("mid-transaction failure")
        # Act + Assert: освобождает ValueError, записей нет

class TestVectorHandling:
    """Проверка работы с векторами (Jackson workaround)."""

    def test_vector_insert_sql_literal(self, arcadedb_adapter):
        """A2-07: Вектор через SQL literal → успешно"""
        # Arrange: вектор [0.1, 0.2, 0.3]
        # Act: INSERT с embedding = [0.1, 0.2, 0.3] (literal)
        # Assert: запись создана

    def test_vector_parameter_binding_fails(self, arcadedb_adapter):
        """A2-08: Вектор через параметр (:embedding) → ошибка Jackson"""
        # Arrange: вектор [0.1, 0.2, 0.3]
        # Act: INSERT с embedding = :embedding (parameter)
        # Assert: ArcadeDBError (Jackson float[] bug)

    def test_vector_neighbors_parameter(self, arcadedb_adapter):
        """A2-09: vector.neighbors() с параметром (:qv) → работает"""
        # Arrange: вставить записи с векторами
        # Act: vector.neighbors('TestVec[embedding]', :qv, 3)
        # Assert: возвращает правильные соседи

class TestConnectionPool:
    """Проверка connection pool."""

    def test_pool_basic(self, arcadedb_adapter_with_pool):
        """A2-10: Connection pool — базовое получение соединения"""
        # Arrange: adapter с ConnectionPool(min=2, max=5)
        # Act: conn1 = adapter.get_conn()
        # Act: conn2 = adapter.get_conn()
        # Assert: оба соединения рабочие

    def test_pool_reuse(self, arcadedb_adapter_with_pool):
        """A2-11: Pool переиспользует соединения"""
        # Arrange: взять + вернуть соединение
        # Act: conn1 = adapter.get_conn(); adapter.put_conn(conn1)
        # Act: conn2 = adapter.get_conn()  # должен быть тот же conn
        # Assert: conn1 == conn2 (pool reuse)

class TestQueryMethods:
    """Проверка CRUD методов."""

    def test_execute_insert(self, arcadedb_adapter):
        """A2-12: execute() — INSERT"""
        # Act + Assert: одна запись создана

    def test_query_select(self, arcadedb_adapter):
        """A2-13: query() — SELECT"""
        # Arrange: вставить запись
        # Act: adapter.query("SELECT FROM TestQ WHERE ...")
        # Assert: list[dict] с правильной записью

    def test_query_params(self, arcadedb_adapter):
        """A2-14: query() с параметрами"""
        # Act: adapter.query("SELECT FROM TestQ WHERE name = %s", {"name": "test"})
        # Assert: правильная запись

    def test_execute_script(self, arcadedb_adapter):
        """A2-15: execute_script() — несколько команд"""
        # Act: adapter.execute_script("BEGIN; INSERT ...; COMMIT;")
        # Assert: обе записи созданы (атомарно)
```

### Связи
- **Фикстуры:** [`tests/fixtures/arcadedb_fixtures.py`](#файл-1-testsfixturesarcadedb_fixturespy)
- **Тестируемый модуль:** [`hermes_cli/arcadedb.py`](../../hermes_cli/arcadedb.py) ← Phase 2
- **[→ Phase 2: Adapter v2](phase-2-adapter-v2.md)** — спецификация тестируемого API
- **Workaround:** Jackson float[] bug → [см. Phase 2: vector handling](phase-2-adapter-v2.md#implementation-notes)
- **Connection pool:** [см. Phase 2: pool section](phase-2-adapter-v2.md#connection-pool)

---

## Файл 4: `tests/test_arcadedb_session.py` (~800 строк)

Тестирует `ArcadedbSessionDB` из Phase 3. **Самый объёмный тестовый файл.**

### Группы тестов

```python
# tests/test_arcadedb_session.py

class TestSessionLifecycle:
    # -- 8 тестов --
    def test_create_session(self, arcadedb_session):
        """S3-01: create_session() → session в БД"""
    def test_ensure_session_idempotent(self, arcadedb_session):
        """S3-02: ensure_session() → INSERT OR IGNORE"""
    def test_end_session(self, arcadedb_session):
        """S3-03: end_session() → ended_at + end_reason"""
    def test_reopen_session(self, arcadedb_session):
        """S3-04: reopen_session() → сброс ended_at"""
    def test_get_session(self, arcadedb_session):
        """S3-05: get_session() → dict с правильными полями"""
    def test_get_session_not_found(self, arcadedb_session):
        """S3-06: get_session(nonexistent) → None"""
    def test_resolve_session_id(self, arcadedb_session):
        """S3-07: resolve_session_id(prefix) → полный ID"""
    def test_resolve_session_id_ambiguous(self, arcadedb_session):
        """S3-08: ambiguous prefix → None"""

class TestMessageCRUD:
    # -- 12 тестов --
    def test_append_message(self, arcadedb_session):
        """S3-09: append_message() → возвращает message ID"""
    def test_append_message_json_content(self, arcadedb_session):
        """S3-10: multimodal content → _CONTENT_JSON_PREFIX"""
    def test_append_message_tool_calls(self, arcadedb_session):
        """S3-11: tool_calls → сохранены как JSON"""
    def test_append_message_reasoning(self, arcadedb_session):
        """S3-12: reasoning поля → сохранены"""
    def test_get_messages(self, arcadedb_session):
        """S3-13: get_messages() → ordered by timestamp"""
    def test_get_messages_inactive(self, arcadedb_session):
        """S3-14: include_inactive=False → только active=1"""
    def test_get_messages_compacted(self, arcadedb_session):
        """S3-15: compacted rows видны при include_inactive=True"""
    def test_get_messages_as_conversation(self, arcadedb_session):
        """S3-16: OpenAI format, decoding"""
    def test_get_messages_around(self, arcadedb_session):
        """S3-17: window ±5 around anchor"""
    def test_get_anchored_view(self, arcadedb_session):
        """S3-18: anchored window + bookends"""
    def test_replace_messages(self, arcadedb_session):
        """S3-19: replace_messages() → old rows gone, new present"""
    def test_replace_messages_atomicity(self, arcadedb_session):
        """S3-20: ошибка mid-transaction → rollback, состояние не изменилось"""

class TestCompactionAndUndo:
    # -- 6 тестов --
    def test_archive_and_compact(self, arcadedb_session):
        """S3-21: old rows soft-deleted, new rows active"""
    def test_rewind_to_message(self, arcadedb_session):
        """S3-22: сообщения >= target → active=0"""
    def test_rewind_idempotency(self, arcadedb_session):
        """S3-23: повторный rewind → тот же rewound_count"""
    def test_restore_rewound(self, arcadedb_session):
        """S3-24: restore_rewound() → active=1"""
    def test_rewind_invalid_target(self, arcadedb_session):
        """S3-25: rewind на assistant message → ValueError"""
    def test_list_recent_user_messages(self, arcadedb_session):
        """S3-26: list_recent_user_messages() → только user, newest first"""

class TestSearch:
    # -- 8 тестов --
    def test_search_messages_basic(self, arcadedb_session):
        """S3-27: search_messages(query) → BM25 ranked results"""
    def test_search_messages_snippets(self, arcadedb_session):
        """S3-28: каждый результат содержит snippet с контекстом"""
    def test_search_messages_cjk(self, arcadedb_session):
        """S3-29: CJK символов → LIKE fallback (3+ chars)"""
    def test_search_messages_cjk_short(self, arcadedb_session):
        """S3-30: короткий CJK (1-2 chars) → LIKE substring"""
    def test_search_messages_source_filter(self, arcadedb_session):
        """S3-31: source_filter → только matching source"""
    def test_search_messages_role_filter(self, arcadedb_session):
        """S3-32: role_filter → только user/assistant"""
    def test_search_messages_exclude_sources(self, arcadedb_session):
        """S3-33: exclude_sources=['subagent'] → скрыты"""
    def test_search_messages_sort(self, arcadedb_session):
        """S3-34: sort='newest' → timestamp DESC"""

class TestHybridSearch:
    # -- 4 теста --
    def test_hybrid_search_dense(self, arcadedb_session):
        """S3-35: hybrid_search_sessions() → dense vector results"""
    def test_hybrid_search_fulltext(self, arcadedb_session):
        """S3-36: SEARCH_INDEX → BM25 full-text results"""
    def test_hybrid_search_fuse(self, arcadedb_session):
        """S3-37: vector.fuse(dense + fulltext) → RRF results"""
    def test_hybrid_search_filters(self, arcadedb_session):
        """S3-38: profile + days filters"""

class TestCompressionLocks:
    # -- 5 тестов --
    def test_acquire_lock(self, arcadedb_session):
        """S3-39: try_acquire_compression_lock() → True"""
    def test_acquire_lock_conflict(self, arcadedb_session):
        """S3-40: два конкурирующих acquire → один проигрывает"""
    def test_refresh_lock(self, arcadedb_session):
        """S3-41: refresh_compression_lock() → продлевает TTL"""
    def test_release_lock(self, arcadedb_session):
        """S3-42: release_compression_lock() → success"""
    def test_acquire_expired_lock(self, arcadedb_session):
        """S3-43: lock с истёкшим TTL → можно перехватить"""

class TestSessionMeta:
    # -- 6 тестов --
    def test_update_session_meta(self, arcadedb_session):
        """S3-44: update_session_meta() → model_config обновлён"""
    def test_set_get_meta(self, arcadedb_session):
        """S3-45: set_meta() / get_meta() key-value"""
    def test_update_token_counts(self, arcadedb_session):
        """S3-46: token counters инкремент + absolute"""
    def test_update_session_model(self, arcadedb_session):
        """S3-47: update_session_model() → model обновлён"""
    def test_update_session_cwd(self, arcadedb_session):
        """S3-48: update_session_cwd() → cwd + git info"""
    def test_backfill_repo_roots(self, arcadedb_session):
        """S3-49: backfill_repo_roots() → batch update"""

class TestSessionListing:
    # -- 5 тестов --
    def test_list_sessions_rich(self, arcadedb_session):
        """S3-50: list_sessions_rich() → previews + durations"""
    def test_search_sessions(self, arcadedb_session):
        """S3-51: search_sessions() → filtered listing"""
    def test_search_sessions_by_id(self, arcadedb_session):
        """S3-52: search_sessions_by_id(query) → prefix/substring"""
    def test_session_count(self, arcadedb_session):
        """S3-53: session_count() → фильтры работают"""
    def test_distinct_session_cwds(self, arcadedb_session):
        """S3-54: distinct_session_cwds() → unique cwds"""

class TestSessionTitles:
    # -- 5 тестов --
    def test_set_session_title(self, arcadedb_session):
        """S3-55: set_session_title() → сохранён"""
    def test_title_duplicate_rejected(self, arcadedb_session):
        """S3-56: duplicate title → False"""
    def test_get_session_by_title(self, arcadedb_session):
        """S3-57: get_session_by_title() → правильная сессия"""
    def test_resolve_session_by_title(self, arcadedb_session):
        """S3-58: resolve_session_by_title() → latest lineage"""
    def test_get_next_title_in_lineage(self, arcadedb_session):
        """S3-59: 'session' → 'session #2' → 'session #3'"""

class TestSessionDeletion:
    # -- 4 теста --
    def test_delete_session(self, arcadedb_session):
        """S3-60: delete_session() → сессия + сообщения удалены"""
    def test_delete_session_cascade(self, arcadedb_session):
        """S3-61: cascade delete delegate children"""
    def test_delete_empty_sessions(self, arcadedb_session):
        """S3-62: delete_empty_sessions() → только empty ended"""
    def test_prune_sessions(self, arcadedb_session):
        """S3-63: prune_sessions(older_than_days) → старые удалены"""

class TestHandoff:
    # -- 4 теста --
    def test_request_handoff(self, arcadedb_session):
        """S3-64: request_handoff() → handoff_state='pending'"""
    def test_claim_handoff(self, arcadedb_session):
        """S3-65: claim_handoff() → pending→running"""
    def test_complete_handoff(self, arcadedb_session):
        """S3-66: complete_handoff() → completed"""
    def test_fail_handoff(self, arcadedb_session):
        """S3-67: fail_handoff() → failed с error"""

class TestArchivalAndMaintenance:
    # -- 3 теста --
    def test_set_session_archived(self, arcadedb_session):
        """S3-68: set_session_archived() → archived=1, cascade"""
    def test_vacuum(self, arcadedb_session):
        """S3-69: vacuum() → no errors"""
    def test_maybe_auto_prune(self, arcadedb_session):
        """S3-70: maybe_auto_prune_and_vacuum() → idempotent"""

class TestExport:
    # -- 2 теста --
    def test_export_session(self, arcadedb_session):
        """S3-71: export_session() → dict with messages"""
    def test_export_all(self, arcadedb_session):
        """S3-72: export_all() → list[dict]"""
```

### Всего: **72+ тест-кейсов** для SessionDB.

### Связи
- **Фикстуры:** [`tests/fixtures/arcadedb_fixtures.py`](#файл-1-testsfixturesarcadedb_fixturespy)
- **Mock embedder:** используется для search тестов (без загрузки real модели)
- **Тестируемый модуль:** [`hermes_cli/arcadedb_session.py`](../../hermes_cli/arcadedb_session.py) ← Phase 3
- **[→ Phase 3: ArcadedbSessionDB](phase-3-sessiondb.md)** — полный API
- **[`hermes_state.py:SessionDB`](../../hermes_state.py)** — существующий SQLite API (reference)

---

## Файл 5: `tests/test_arcadedb_session_factory.py` (~150 строк)

Тестирует фабрику `create_session_db()` из Phase 4.

```python
class TestSessionFactory:
    def test_arcadedb_enabled_returns_arcadedb(self, arcadedb_config_enabled):
        """F4-01: database.arcadedb.enabled=True → ArcadedbSessionDB"""
    def test_arcadedb_disabled_returns_sqlite(self, arcadedb_config_disabled):
        """F4-02: database.arcadedb.enabled=False → SessionDB (SQLite)"""
    def test_arcadedb_unavailable_falls_back(self, arcadedb_config_no_container):
        """F4-03: ArcadeDB недоступен → fallback на SQLite"""
    def test_same_api_both_backends(self, arcadedb_session, sqlite_session):
        """F4-04: Оба бэкенда имеют одинаковые public методы"""
    def test_session_db_no_break(self, sqlite_session):
        """F4-05: Существующий SQLite SessionDB не сломан"""
```

### Связи
- **[→ Phase 4: Consumer Migration](phase-4-consumers.md)** — где фабрика используется
- **[`hermes_state.py`](../../hermes_state.py)** — становится factory

---

## Файл 6: `tests/test_arcadedb_compression_locks.py` (~150 строк)

Изолированные тесты протокола compression locks.

```python
class TestCompressionLocks:
    def test_acquire_first(self, arcadedb_session):
        """CL-01: Первый acquire → success"""
    def test_acquire_conflict_same_session(self, arcadedb_session):
        """CL-02: Два acquire на одну сессию → второй fail"""
    def test_acquire_expired(self, arcadedb_session):
        """CL-03: Истёкший lock можно перехватить"""
    def test_refresh_extends(self, arcadedb_session):
        """CL-04: refresh() продлевает expires_at"""
    def test_release_enables_reacquire(self, arcadedb_session):
        """CL-05: После release → другой acquire success"""
    def test_release_non_owner(self, arcadedb_session):
        """CL-06: Release не-writerом → no-op"""
    def test_get_holder_unexpired(self, arcadedb_session):
        """CL-07: get_holder() → правильный holder"""
    def test_concurrent_compressors(self, arcadedb_session):
        """CL-08: 10 concurrent acquirers → только 1 получает lock"""
```

### Связи
- **[→ Phase 3: SessionDB — compression locks](phase-3-sessiondb.md#compression-locks)**
- **[`hermes_state.py:CompressionLock`](../../hermes_state.py)** — reference implementation
- **[`agent/conversation_compression.py`](../../agent/conversation_compression.py)** — consumer

---

## Файл 7: `tests/test_arcadedb_search.py` (~250 строк)

FTS5→Lucene эквивалентность.

```python
class TestFullTextSearch:
    def test_bm25_ranking(self, arcadedb_session):
        """SR-01: BM25 ranking сохраняется при миграции"""
    def test_boolean_operators(self, arcadedb_session):
        """SR-02: AND/OR/NOT работают"""
    def test_phrase_search(self, arcadedb_session):
        """SR-03: "exact phrase" работает"""
    def test_prefix_search(self, arcadedb_session):
        """SR-04: deploy* → префиксный поиск"""

class TestCJKSearch:
    def test_cjk_trigram(self, arcadedb_session):
        """SR-05: CJK 3+ chars → LIKE substring"""
    def test_cjk_short(self, arcadedb_session):
        """SR-06: CJK 1-2 chars → LIKE substring fallback"""
    def test_cjk_mixed(self, arcadedb_session):
        """SR-07: ASCII + CJK смешанный запрос"""

class TestSnippetGeneration:
    def test_snippet_around_match(self, arcadedb_session):
        """SR-08: Snippet содержит match + контекст"""
    def test_snippet_markers(self, arcadedb_session):
        """SR-09: >>> / <<< маркеры совместимы с FTS5"""
    def test_snippet_max_tokens(self, arcadedb_session):
        """SR-10: Snippet не превышает 40 токенов"""

class TestHybridSearch:
    def test_dense_vector_search(self, arcadedb_session):
        """SR-11: vector.neighbors() возвращает релевантные"""
    def test_hybrid_fuse(self, arcadedb_session):
        """SR-12: vector.fuse(dense + fulltext) → объединённый rank"""
    def test_hybrid_group_by(self, arcadedb_session):
        """SR-13: groupBy: 'session_rid', groupSize: 1"""
```

### Связи
- **[→ Phase 3: SessionDB — search](phase-3-sessiondb.md#search)**
- **[`hermes_state.py:search_messages`](../../hermes_state.py)** — FTS5 reference
- **[`hermes_cli/graph_store.py`](../../hermes_cli/graph_store.py)** — существующий гибридный поиск

---

## Файлы 8-14: Остальные тесты

### `tests/test_arcadedb_telegram_topics.py` (~100 строк)
- Topic mode enable/disable → [см. Phase 3: telegram](phase-3-sessiondb.md#telegram-topic-mode)
- Topic bind/unbind
- Session linking/unlinking
- Cascade delete при удалении сессии

### `tests/test_arcadedb_migration.py` (~250 строк)
- Dry-run → [см. Phase 5: migration](phase-5-migration-tool.md)
- Full migration state.db → ArcadeDB
- Partial migration (--state-only)
- Data integrity after migration (row count, content match)
- Idempotency (повторная миграция)

### `tests/test_arcadedb_kanban.py` (~350 строк)
- Task CRUD → [см. Phase 6: KanbanDB](phase-6-kanbandb.md)
- CAS claim pattern
- Atomic claim (два concurrent claim → один win)
- Edge traversal (DEPENDS_ON, BLOCKED_BY)
- Multi-board isolation
- Stale claim release
- Cycle detection in task_links

### `tests/test_arcadedb_memory.py` (~150 строк)
- Fact CRUD → [см. Phase 7: Memory Store](phase-7-other-dbs.md#memory-store)
- Entity extraction + linking
- HRR → vector search
- FTS → FULL_TEXT search
- Trust scoring

### `tests/test_arcadedb_projects.py` (~100 строк)
- Project CRUD → [см. Phase 7: Projects](phase-7-other-dbs.md#projects)
- Folder management
- Primary folder switching
- Board reference
- Cascade delete

### `tests/e2e/test_cli_arcadedb.py` (~200 строк)
- Full CLI session lifecycle
- /new, /resume, /model, /compress, /undo, /history
- Context compression с ArcadeDB sessions
- Session search через hybrid search
- Factory switch (ArcadeDB → SQLite → ArcadeDB)

### `tests/e2e/test_gateway_arcadedb.py` (~200 строк)
- Gateway session lifecycle
- Telegram message round-trip
- /undo через gateway slash commands
- Handoff protocol
- Gateway restart → session recovery

### `tests/e2e/test_migration_e2e.py` (~150 строк)
- Full migration flow (auto-detect state.db)
- CLI `hermes migrate --arcadedb`
- Verification after migration
- Fallback to SQLite

---

## Фикстуры детально

### `tests/fixtures/arcadedb_fixtures.py` (псевдокод)

```python
import pytest
import psycopg
import tempfile
import yaml
import time
from pathlib import Path

# Константы
TEST_DOCKER_IMAGE = "arcadedb/arcadedb:26.7.1"
TEST_CONTAINER_NAME = "hermes-arcadedb-test"
TEST_DB = "hermes_test"
TEST_USER = "root"
TEST_PASSWORD = "test123"
TEST_PORT = 5432


@pytest.fixture(scope="session")
def arcadedb_container():
    """
    Запускает ArcadeDB Docker контейнер для всей тестовой сессии.
    Scope=session: один контейнер на все тесты (~5-10 минут на все тесты).

    При CI: контейнер должен быть уже запущен (env ARCADEDB_TEST_HOST).
    При локальной разработке: запускается автоматически.

    Останавливается после всех тестов.
    """
    import os
    import subprocess

    # Если CI указывает внешний ArcadeDB — используем его
    ci_host = os.environ.get("ARCADEDB_TEST_HOST")
    if ci_host:
        yield {"host": ci_host, "port": int(os.environ.get("ARCADEDB_TEST_PORT", "5432"))}
        return

    # Запускаем локальный контейнер
    subprocess.run([
        "docker", "run", "-d", "--name", TEST_CONTAINER_NAME,
        "-p", f"{TEST_PORT}:5432",
        "-e", f"ARCADEDB_ROOT_PASSWORD={TEST_PASSWORD}",
        TEST_DOCKER_IMAGE,
    ], check=True)

    # Ждём health
    _wait_for_arcadedb("localhost", TEST_PORT, TEST_USER, TEST_PASSWORD, timeout=60)

    # Создаём тестовую БД
    conn = psycopg.connect(
        host="localhost", port=TEST_PORT,
        dbname=TEST_DB, user=TEST_USER, password=TEST_PASSWORD,
    )
    conn.close()

    yield {"host": "localhost", "port": TEST_PORT}

    # Cleanup
    subprocess.run(["docker", "stop", TEST_CONTAINER_NAME], check=False)
    subprocess.run(["docker", "rm", TEST_CONTAINER_NAME], check=False)


@pytest.fixture
def arcadedb_config(arcadedb_container):
    """ArcadeDBConfig с тестовыми параметрами."""
    from hermes_cli.arcadedb import ArcadeDBConfig
    return ArcadeDBConfig(
        host=arcadedb_container["host"],
        port=arcadedb_container["port"],
        database=TEST_DB,
        user=TEST_USER,
        password=TEST_PASSWORD,
        timeout=10.0,
    )


@pytest.fixture
def arcadedb_adapter(arcadedb_config):
    """ArcadedbAdapter подключённый к тестовой БД."""
    from hermes_cli.arcadedb import ArcadeDBAdapter
    adapter = ArcadeDBAdapter(arcadedb_config)
    adapter.connect()
    # Очищаем БД перед каждым тестом
    adapter.execute("DELETE VERTEX V")
    yield adapter
    adapter.close()


@pytest.fixture
def mock_embedder():
    """Mock EmbedderProvider с deterministic output."""
    from unittest.mock import MagicMock
    from hermes_cli.embedder import EmbedderProvider, EmbeddingResult

    embedder = MagicMock(spec=EmbedderProvider)
    embedder.embed.return_value = [EmbeddingResult(dense=[0.1] * 1024)]
    embedder.embed_query.return_value = EmbeddingResult(dense=[0.2] * 1024)
    embedder.dimensions = 1024
    return embedder


@pytest.fixture
def arcadedb_session(arcadedb_adapter, mock_embedder):
    """ArcadedbSessionDB с ArcadeDB + mock embedder."""
    from hermes_cli.arcadedb_session import ArcadedbSessionDB
    session = ArcadedbSessionDB(adapter=arcadedb_adapter, embedder=mock_embedder)
    yield session
    session.close()


@pytest.fixture
def sqlite_session(tmp_path):
    """SQLite SessionDB для comparison тестов."""
    from hermes_state import SessionDB
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    yield db
    db.close()
```

### Связи фикстур с фазами

| Фикстура | Использует модуль из фазы | Фаза |
|----------|--------------------------|------|
| `arcadedb_container` | `ArcadeDBLifecycle` | Phase 0 |
| `arcadedb_config` | `ArcadeDBConfig` | Phase 2 |
| `arcadedb_adapter` | `ArcadeDBAdapter` | Phase 2 |
| `mock_embedder` | `EmbedderProvider` | Existing |
| `arcadedb_session` | `ArcadedbSessionDB` | Phase 3 |
| `sqlite_session` | `SessionDB` | Existing |

---

## Acceptance Criteria

- [ ] `tests/fixtures/arcadedb_fixtures.py` создан со всеми 8 фикстурами
- [ ] 15 тестовых файлов созданы (~2,500 строк)
- [ ] **Все тесты падают** (это ожидаемо на этом этапе)
- [ ] Каждый тест имеет чёткое имя с ID (L0-01, A2-01, S3-01, ...)
- [ ] Каждый тест документирует expected behaviour
- [ ] Фикстуры работают с CI (внешний ArcadeDB через env vars)
- [ ] Фикстуры работают локально (Docker managed)
- [ ] `scripts/run_tests.sh tests/test_arcadedb_*.py` выполняется (хотя тесты fail)
- [ ] Тесты не блокируют CI (skip если Docker недоступен в CI)
- [ ] `pyproject.toml` обновлён с `psycopg[binary]>=3.1,<4` в dev deps

---

## Dependency Graph

```
Phase 0 (Lifecycle)
    ↓
Phase 1 (Testing) ← определяет контракты для ВСЕХ последующих фаз
    ↓
    ├──→ Phase 2 (Adapter) → tests/test_arcadedb_adapter.py GREEN
    ├──→ Phase 3 (SessionDB) → tests/test_arcadedb_session.py GREEN
    ├──→ Phase 5 (Migration) → tests/test_arcadedb_migration.py GREEN
    ├──→ Phase 6 (KanbanDB) → tests/test_arcadedb_kanban.py GREEN
    └──→ Phase 7 (Memory Store) → tests/test_arcadedb_memory.py GREEN
    └──→ Phase 8 (Other DBs) → tests/test_arcadedb_projects.py GREEN
```

---

## Cross-References

### Предшествующие фазы
- **[← Phase 0: Lifecycle Manager](phase-0-lifecycle.md)** — нужен для `arcadedb_container` fixture

### Последующие фазы
- **[→ Phase 2: Adapter v2](phase-2-adapter-v2.md)** — должен сделать `test_arcadedb_adapter.py` зелёным
- **[→ Phase 3: ArcadedbSessionDB](phase-3-sessiondb.md)** — должен сделать `test_arcadedb_session.py` зелёным
- **[→ Phase 4: Consumer Migration](phase-4-consumers.md)** — factory tests
- **[→ Phase 5: Migration Tool](phase-5-migration-tool.md)** — migration tests
- **[→ Phase 6: KanbanDB](phase-6-kanbandb.md)** — kanban tests
- **[→ Phase 7: Memory Store](phase-7-memory-store.md)** — memory/projects tests
- **[→ Phase 8: Other DBs](phase-8-other-dbs.md)** — projects тесты

### Связи с существующими файлами
- **[`hermes_state.py:SessionDB`](../../hermes_state.py)** — reference API для тестов SessionDB
- **[`hermes_cli/arcadedb_schema.py:SchemaManager`](../../hermes_cli/arcadedb_schema.py)** — schema init в фикстурах
- **[`hermes_cli/embedder.py:EmbedderProvider`](../../hermes_cli/embedder.py)** — mock в фикстурах
- **[`pyproject.toml`](../../pyproject.toml)** — +psycopg dependency
- **[`tests/conftest.py`](../../tests/conftest.py)** — auto-import fixtures
- **[`scripts/run_tests.sh`](../../scripts/run_tests.sh)** — скрипт запуска тестов

---

## Implementation Sequence

```
1. pyproject.toml → добавить psycopg[binary] в dev deps
2. tests/conftest.py → import arcadedb fixtures
3. tests/fixtures/arcadedb_fixtures.py → все фикстуры
4. tests/test_arcadedb_lifecycle.py → 12 тестов (Phase 0 контракты)
5. tests/test_arcadedb_adapter.py → 15 тестов (Phase 2 контракты)
6. tests/test_arcadedb_compression_locks.py → 8 тестов (lock протокол)
7. tests/test_arcadedb_search.py → 13 тестов (FTS→Lucene)
8. tests/test_arcadedb_telegram_topics.py → ~6 тестов
9. tests/test_arcadedb_session.py → 72 теста (Phase 3 контракты)
10. tests/test_arcadedb_session_factory.py → 5 тестов (Phase 4)
11. tests/test_arcadedb_migration.py → ~8 тестов (Phase 5)
12. tests/test_arcadedb_kanban.py → ~15 тестов (Phase 6)
13. tests/test_arcadedb_memory.py → ~8 тестов (Phase 7)
14. tests/test_arcadedb_projects.py → ~5 тестов (Phase 7)
15. tests/e2e/ → 3 файла (~550 строк)
```

**После этого:** Все последующие фазы пишут код под эти тесты (TDD).
