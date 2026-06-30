# Phase 2: ArcadedbAdapter v2 (psycopg)

| Поле | Значение |
|------|----------|
| **Номер** | Phase 2 |
| **Название** | ArcadedbAdapter — PostgreSQL Wire Protocol |
| **Новых строк** | ~300 (перезапись) |
| **Сложность** | Medium |
| **Зависит от** | Phase 0 (Lifecycle), Phase 1 (Tests) |
| **Разблокирует** | Phase 3 (SessionDB), Phase 6 (KanbanDB), Phase 7 (Memory Store), Phase 8 (Other DBs) |

---

## Overview

Полная перезапись `hermes_cli/arcadedb.py` с HTTP API (httpx) на PostgreSQL Wire
Protocol (psycopg). Текущие 102 строки заменяются на ~300 строк с поддержкой:
- Транзакций (BEGIN/COMMIT/ROLLBACK)
- Connection pooling (psycopg_pool)
- Prepared statements
- Row dict factory
- Vector SQL-literal workaround для Jackson bug

### Целевое состояние

```python
# Было: HTTP REST API (нет транзакций, нет пула)
adapter = ArcadeDBAdapter(config)  # httpx Client
adapter.query("SELECT ...")        # один запрос

# Стало: PostgreSQL Wire Protocol (транзакции, пул)
adapter = ArcadeDBAdapter(config)  # psycopg connection pool
with adapter.transact() as cur:    # транзакция
    cur.execute("INSERT ...")
    cur.execute("UPDATE ...")
# commit автоматически
```

---

## Files

### Перезаписываемый файл

| Файл | Было | Стало | Назначение |
|------|------|-------|------------|
| `hermes_cli/arcadedb.py` | 102 строки | ~300 строк | Новый адаптер на psycopg |

### Модифицируемые файлы

| Файл | Изменение | Связь |
|------|-----------|-------|
| `pyproject.toml` | Добавить `psycopg[binary]>=3.1,<4` в core dependencies | [см. pyproject.toml](../../pyproject.toml) |
| `hermes_cli/graph_store.py` | Переключить на новый `ArcadeDBAdapter` API | [см. graph_store.py](../../hermes_cli/graph_store.py) |
| `hermes_cli/arcadedb_schema.py` | Убрать HTTP-specific fallbacks | [см. arcadedb_schema.py](../../hermes_cli/arcadedb_schema.py) |
| `hermes_cli/arcadedb_migrate.py` | Переключить ArcadeDBWriter на новый адаптер | [см. arcadedb_migrate.py](../../hermes_cli/arcadedb_migrate.py) |
| `tools/session_search_tool.py` | Адаптировать `_init_graph_store()` | [см. session_search_tool.py](../../tools/session_search_tool.py) |

---

## API Specification

```python
# hermes_cli/arcadedb.py (~300 строк)

from dataclasses import dataclass, field
from psycopg import Connection, Cursor
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
import json
import logging

class ArcadeDBError(Exception):
    """Base exception for all ArcadeDB errors."""
    pass


@dataclass
class ArcadeDBConfig:
    """ArcadeDB connection configuration."""
    host: str = "localhost"
    port: int = 5432               # PostgreSQL wire protocol
    database: str = "hermes"
    user: str = "root"
    password: str = ""             # from config.yaml
    timeout: float = 30.0          # connect + query timeout
    pool_min: int = 2              # минимальный размер пула
    pool_max: int = 10             # максимальный размер пула
    pool_timeout: float = 10.0     # timeout получения connection из пула


class ArcadeDBAdapter:
    """
    PostgreSQL wire protocol adapter for ArcadeDB.

    Использует psycopg с connection pool и dict_row factory.
    Поддерживает транзакции и prepared statements.
    Векторы передаются как SQL literals (Jackson bug workaround).
    """

    # ---- Lifecycle ----

    def __init__(self, config: ArcadeDBConfig = None):
        """
        Создаёт ConnectionPool с заданной конфигурацией.
        Не подключается сразу (lazy).

        Args:
            config: ArcadeDBConfig или None для defaults.
        """

    @property
    def connected(self) -> bool:
        """True если хотя бы одно соединение в пуле активно."""

    def connect(self) -> None:
        """
        Инициализирует connection pool.
        Идемпотентный — повторный вызов не создаёт новый пул.

        При первой ошибке соединения выбрасывает ArcadeDBError.
        """

    def close(self) -> None:
        """
        Закрывает connection pool.
        Идемпотентный.
        Гарантирует что все соединения закрыты.
        """

    def _check_health(self) -> bool:
        """
        Проверяет здоровье ArcadeDB:
          try: pool.connection() → SELECT 1
        Returns True если здоров.
        """

    # ---- Connection Management ----

    def get_conn(self) -> Connection:
        """
        Получает соединение из пула.
        Используется транзакционными контекстными менеджерами.

        Returns:
            psycopg.Connection с autocommit=False, row_factory=dict_row.

        Raises:
            ArcadeDBError если пул не инициализирован.
        """

    def put_conn(self, conn: Connection) -> None:
        """
        Возвращает соединение в пул.
        Вызывается после завершения операции.
        """

    # ---- Transaction API ----

    def begin(self) -> None:
        """Начинает транзакцию на текущем соединении."""

    def commit(self) -> None:
        """Коммитит текущую транзакцию."""

    def rollback(self) -> None:
        """Откатывает текущую транзакцию."""

    def transact(self, fn: callable) -> any:
        """
        Выполняет fn(cursor) в транзакции. Атомарный.

        Usage:
            def _do(cur):
                cur.execute("INSERT ...")
                cur.execute("UPDATE ...")
                return cur.fetchall()

            rows = adapter.transact(_do)

        Если fn выбрасывает исключение → rollback.
        Если успешно → commit → return fn's return value.

        Args:
            fn: callable(Cursor) -> any

        Returns:
            Результат вызова fn.

        Raises:
            Исключение из fn (после rollback).
            ArcadeDBError если соединение недоступно.
        """

    # ---- Query API ----

    def execute(
        self,
        sql: str,
        params: dict = None,
        language: str = "sql",
    ) -> list[dict]:
        """
        Выполняет SQL команду. Auto-commit (вне транзакции).

        Args:
            sql: SQL строка (psycopg placeholders: %s, %(name)s).
            params: параметры для подстановки.
            language: 'sql' или 'sqlscript'.

        Returns:
            list[dict] — результат запроса.

        Raises:
            ArcadeDBError при ошибке.
        """

    def query(
        self,
        sql: str,
        params: dict = None,
    ) -> list[dict]:
        """
        SELECT запрос. Shortcut для execute(language='sql').

        Args:
            sql: SELECT SQL строка.
            params: параметры.

        Returns:
            list[dict] с результатами. Каждый dict соответствует sqlite3.Row.
        """

    def execute_script(self, script: str) -> list[dict]:
        """
        Выполняет multi-statement скрипт. Auto-commit.

        Args:
            script: SQL script (несколько команд разделённых ';').

        Returns:
            Результат последней команды.
        """

    # ---- Vector Workaround ----

    @staticmethod
    def _vec(val: list[float]) -> str:
        """
        Форматирует Python float[] как JSON-array SQL literal.

        Причина: ArcadeDB 26.7.1-SNAPSHOT Jackson bug.
        Векторы нельзя передавать через bind-параметры (:name).
        Используются SQL literals: embedding = [0.1, 0.2, ...].

        Args:
            val: list[float] длиной _VECTOR_DIM (1024).

        Returns:
            JSON строка для вставки в SQL.
        """
        return json.dumps([float(x) for x in val], allow_nan=False)

    @staticmethod
    def _parse_vec(s: str) -> list[float]:
        """
        Парсит JSON-array обратно в list[float].
        Используется при чтении векторов из БД.

        Args:
            s: JSON-array строка.

        Returns:
            list[float].
        """
        return json.loads(s)
```

---

## Файл `hermes_cli/arcadedb.py` (~300 строк)

### Структура (по секциям)

```
hermes_cli/arcadedb.py
│
├── [1-15]   Module docstring + imports (psycopg, psycopg_pool, json, logging)
├── [17-23]  ArcadeDBError exception
├── [25-52]  ArcadeDBConfig dataclass
│
├── [54-80]  ArcadeDBAdapter.__init__()
├── [82-90]  ArcadeDBAdapter.connected (property)
├── [92-130] ArcadeDBAdapter.connect()
├── [132-150] ArcadeDBAdapter.close()
├── [152-170] ArcadeDBAdapter._check_health()
│
├── [172-190] ArcadeDBAdapter.get_conn()
├── [192-200] ArcadeDBAdapter.put_conn()
│
├── [202-215] ArcadeDBAdapter.begin()
├── [216-225] ArcadeDBAdapter.commit()
├── [226-235] ArcadeDBAdapter.rollback()
├── [237-280] ArcadeDBAdapter.transact()
│
├── [282-320] ArcadeDBAdapter.execute()
├── [322-335] ArcadeDBAdapter.query()
├── [337-360] ArcadeDBAdapter.execute_script()
│
├── [362-375] ArcadeDBAdapter._vec() (static)
├── [376-385] ArcadeDBAdapter._parse_vec() (static)
│
└── [387-400] Module-level singleton helper (_ADAPTER_INSTANCE)
```

### Connection Pool

```python
def connect(self) -> None:
    """Инициализирует connection pool."""
    if self._pool is not None:
        return  # уже инициализирован (идемпотентный)

    conninfo = (
        f"host={self._cfg.host} "
        f"port={self._cfg.port} "
        f"dbname={self._cfg.database} "
        f"user={self._cfg.user} "
        f"password={self._cfg.password} "
        f"connect_timeout={int(self._cfg.timeout)}"
    )

    self._pool = ConnectionPool(
        conninfo=conninfo,
        min_size=self._cfg.pool_min,   # 2 соединения всегда готовы
        max_size=self._cfg.pool_max,   # до 10 при пиковой нагрузке
        timeout=self._cfg.pool_timeout, # таймаут получения conn из пула
        kwargs={
            "autocommit": False,         # ручное управление транзакциями
            "row_factory": dict_row,     # возвращает dict, не tuple
            "prepare_threshold": 5,      # prepared statement после 5 вызовов
        },
    )
    self._pool.open()  # форсирует открытие пула
```

### Transaction Implementation

```python
def transact(self, fn: callable) -> any:
    """Выполняет fn(cursor) в транзакции. Атомарный."""
    if self._pool is None:
        raise ArcadeDBError("not connected")

    conn = self._pool.getconn()
    cur = conn.cursor()

    try:
        result = fn(cur)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        self._pool.putconn(conn)
```

### Execute Implementation

```python
def execute(self, sql: str, params: dict = None, language: str = "sql") -> list[dict]:
    """Выполняет SQL в auto-commit режиме."""
    if self._pool is None:
        raise ArcadeDBError("not connected")

    conn = self._pool.getconn()
    cur = conn.cursor()

    try:
        if language == "sqlscript":
            # sqlscript: выполняем как multi-statement
            cur.execute(sql, params)
        elif language == "cypher":
            # Cypher: требует префикс {cypher}
            cur.execute("{cypher} " + sql, params)
        else:
            cur.execute(sql, params)

        if cur.description is not None:
            rows = cur.fetchall()
        else:
            rows = [{"rowcount": cur.rowcount}] if cur.rowcount >= 0 else []

        conn.commit()
        return rows
    except Exception as e:
        conn.rollback()
        raise ArcadeDBError(str(e)) from e
    finally:
        cur.close()
        self._pool.putconn(conn)
```

### Vector Workaround

```python
@staticmethod
def _vec(val: list[float]) -> str:
    """Format vector as JSON-array SQL literal."""
    return json.dumps([float(x) for x in val], allow_nan=False)

# Usage example in callers (Phase 3, 6, 7):
# sql = f"INSERT INTO Message SET embedding = {ArcadeDBAdapter._vec(emb.dense)}, content = %s"
# adapter.execute(sql, {"content": "Hello"})
```

---

## Изменения в `pyproject.toml`

```toml
# В [project.dependencies] добавить:
"psycopg[binary]>=3.1,<4",

# В [project.optional-dependencies] dev — уже есть:
dev = [..., "mcp==1.26.0", ...]

# Убедиться что psycopg НЕ конфликтует с существующими:
# Проверить что нет других PostgreSQL-зависимостей с конфликтующими версиями
```

### Связи в `pyproject.toml`
- **[`pyproject.toml:[project.dependencies]`](../../pyproject.toml)** — строка ~100, добавить psycopg
- `httpx` остаётся (используется в других местах)
- `psycopg[binary]` — binary wheel включает libpq, не требует системной установки

---

## Изменения в consumers старого HTTP API

### `hermes_cli/graph_store.py` (существующий, 376 строк)

**Текущее состояние:** использует `ArcadeDBAdapter` через HTTP API.

**Изменения:**
- Убрать `self._db` тип с HTTP adapter на новый psycopg adapter
- Метод connect/close — заменить на новый API
- `self._db.execute(sql, params)` → остаётся тот же синтаксис (совместимость)
- `_vec()` → убрать, использовать `ArcadeDBAdapter._vec()` напрямую
- GraphStore.__init__ принимает `ArcadeDBAdapter`, API не меняется

**Связь:** [см. graph_store.py](../../hermes_cli/graph_store.py)

### `hermes_cli/arcadedb_schema.py` (существующий, 594 строки)

**Изменения:**
- `SchemaManager.__init__()` принимает `ArcadeDBAdapter` как и раньше
- DDL запросы не меняются (CREATE VERTEX TYPE etc)
- Убрать HTTP-specific error handling

**Связь:** [см. arcadedb_schema.py](../../hermes_cli/arcadedb_schema.py)

### `hermes_cli/arcadedb_migrate.py` (существующий, 453 строки)

**Изменения:**
- `ArcadeDBWriter` использует новый `ArcadeDBAdapter`
- `insert_vertex()` / `ensure_edge()` → тот же API, новый транспорт
- Vector embedding вставка → через `ArcadeDBAdapter._vec()`

**Связь:** [см. arcadedb_migrate.py](../../hermes_cli/arcadedb_migrate.py)

### `tools/session_search_tool.py` (существующий, 1138 строк)

**Изменения:**
- `_init_graph_store()` читает ArcadeDBConfig вместо env vars
- Перестаёт использовать `ARCADE_HOST`, `ARCADE_PORT`, `ARCADE_PASSWORD` env vars
- Использует `database.arcadedb.*` из config.yaml

**Связи:**
- **[`tools/session_search_tool.py:_init_graph_store`](../../tools/session_search_tool.py)**
- **[→ Phase 3: session_search_tool адаптация](phase-3-sessiondb.md#session-search-tool)**

---

## Тест-кейсы

**Тестовый файл:** `tests/test_arcadedb_adapter.py` → [см. Phase 1: adapter tests](phase-1-testing.md#файл-3-teststest_arcadedb_adapterpy)

### Сводка тестов (15 тест-кейсов)

| ID | Тест | Описание | Ключевое свойство |
|----|------|----------|-------------------|
| A2-01 | `test_connect_success` | Подключение к работающему ArcadeDB | `connected == True` |
| A2-02 | `test_connect_failure` | Ошибка при недоступном хосте | `ArcadeDBError` |
| A2-03 | `test_commit` | INSERT в транзакции → COMMIT | Данные видны |
| A2-04 | `test_rollback` | INSERT в транзакции → ROLLBACK | Данных нет |
| A2-05 | `test_transact_context_manager` | transact() atomic commit | Обе записи есть |
| A2-06 | `test_transact_rollback_on_error` | transact() с ошибкой | Rollback, 0 записей |
| A2-07 | `test_vector_insert_sql_literal` | Вектор через SQL literal | Успешно |
| A2-08 | `test_vector_parameter_binding_fails` | Вектор через параметр | ArcadeDBError |
| A2-09 | `test_vector_neighbors_parameter` | vector.neighbors(:qv) | Работает |
| A2-10 | `test_pool_basic` | Connection pool — получение conn | 2 соединения рабочие |
| A2-11 | `test_pool_reuse` | Pool переиспользует соединения | Тот же conn |
| A2-12 | `test_execute_insert` | execute() — INSERT | 1 запись создана |
| A2-13 | `test_query_select` | query() — SELECT | list[dict] |
| A2-14 | `test_query_params` | query() с параметрами | Правильная запись |
| A2-15 | `test_execute_script` | execute_script() | Multi-statement |

---

## Acceptance Criteria

- [ ] `hermes_cli/arcadedb.py` переписан с httpx на psycopg
- [ ] Connection pool работает (min=2, max=10)
- [ ] `transact()` обеспечивает атомарность (commit/rollback)
- [ ] `execute()` / `query()` возвращают `list[dict]` (dict_row)
- [ ] `_vec()` форматирует векторы как SQL literals
- [ ] Cypher запросы работают с префиксом `{cypher}`
- [ ] Все 15 тестов из `test_arcadedb_adapter.py` проходят (зелёные)
- [ ] Существующие consumers (`graph_store.py`, `session_search_tool.py`, `arcadedb_migrate.py`) работают
- [ ] `pyproject.toml` содержит `psycopg[binary]>=3.1,<4`
- [ ] Jackson float[] bug обойдён (векторы через SQL literals)

---

## Cross-References

### Предшествующие фазы
- **[← Phase 0: Lifecycle Manager](phase-0-lifecycle.md)** — `ensure_started()` перед первым connect
- **[← Phase 1: Testing](phase-1-testing.md)** — 15 тестов определяют API контракт

### Последующие фазы
- **[→ Phase 3: ArcadedbSessionDB](phase-3-sessiondb.md)** — главный consumer
- **[→ Phase 6: KanbanDB](phase-6-kanbandb.md)** — использует адаптер для CAS
- **[→ Phase 7: Memory Store](phase-7-memory-store.md)** — использует адаптер
- **[→ Phase 8: Other DBs](phase-8-other-dbs.md)** — все БД используют адаптер

### Связи с существующими файлами
- **[`hermes_cli/arcadedb.py`](../../hermes_cli/arcadedb.py)** ← **перезаписывается**
- **[`hermes_cli/graph_store.py`](../../hermes_cli/graph_store.py)** — consumer адаптера
- **[`hermes_cli/arcadedb_schema.py`](../../hermes_cli/arcadedb_schema.py)** — consumer адаптера
- **[`hermes_cli/arcadedb_migrate.py`](../../hermes_cli/arcadedb_migrate.py)** — consumer адаптера
- **[`tools/session_search_tool.py`](../../tools/session_search_tool.py)** — consumer адаптера
- **[`pyproject.toml`](../../pyproject.toml)** — +psycopg dependency
- **[`hermes_cli/embedder.py`](../../hermes_cli/embedder.py)** — используется для векторов

### Связи внутри документации
- **[Phase 0: lifecycle.ensure_started()](phase-0-lifecycle.md#api-specification)**
- **[Phase 1: test_arcadedb_adapter.py](phase-1-testing.md#файл-3-teststest_arcadedb_adapterpy)**
- **[Phase 1: arcadedb_fixtures.py](phase-1-testing.md#файл-1-testsfixturesarcadedb_fixturespy)**

---

## Implementation Sequence

```
1. pyproject.toml → добавить psycopg[binary]
2. Переписать ArcadeDBConfig → port=5432 вместо 2480
3. Переписать __init__() → ConnectionPool
4. connect() / close()
5. execute() / query() / execute_script()
6. transact() — главная инновация vs HTTP API
7. _vec() / _parse_vec() — vector workaround
8. Переключить graph_store.py
9. Переключить arcadedb_schema.py
10. Переключить arcadedb_migrate.py
11. Переключить session_search_tool.py
12. Сделать тесты зелёными (A2-01 → A2-15)
```

## Notes

- **psycopg vs psycopg2:** используем psycopg 3.x (современный, async-ready)
- **binary wheel:** `psycopg[binary]` включает libpq, не требует системной установки PostgreSQL
- **Pool vs single connection:** пул нужен для concurrent gateway запросов
- **dict_row:** важно для обратной совместимости с sqlite3.Row (dict-like access)
- **Prepared statements:** psycopg автоматически готовит statement после 5 вызовов
- **Retry:** не нужно — ArcadeDB сервер сам обрабатывает retry
- **SQLite _execute_write pattern:** заменяется на `transact()` для multi-statement атомарности
