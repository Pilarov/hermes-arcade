# Phase 2: ArcadeDBAdapter v2 — As-Built

## Файлы

| Файл | Было | Стало | Назначение |
|------|------|-------|------------|
| `hermes_cli/arcadedb.py` | 102 строки (HTTP/JSON) | 277 строк (psycopg) | Полная перезапись |
| `pyproject.toml` | — | +2 deps | `psycopg[binary]>=3.1,<4` + `psycopg-pool>=3.3,<4` |

## Реализованные методы

```
ArcadeDBConfig
├── host: str = "localhost"
├── port: int = 5432              # PostgreSQL wire protocol (не HTTP)
├── database: str = "hermes"
├── user: str = "root"
├── password: str = ""
├── timeout: float = 30.0
├── pool_min: int = 2
├── pool_max: int = 10
└── pool_timeout: float = 10.0

ArcadeDBAdapter
├── connect()              → ConnectionPool + health check
├── close()                → pool.close()
├── connected (property)   → bool
├── get_conn() / put_conn() → pool connection management
│
├── transact(fn)           → BEGIN; fn(cur); COMMIT/ROLLBACK
│
├── execute(sql, params, language)  → SQL command
├── query(sql, params)              → SELECT shortcut
├── execute_script(script)          → multi-statement
│
├── _fmt(sql, params: dict) → %(name)s → SQL literals (static)
├── _vec(val: list)         → float[] → JSON SQL literal
└── _parse_vec(s: str)     → JSON → list[float]
```

## Ключевые решения

### 1. autocommit=True (ArcadeDB limitation)

Официальная документация ArcadeDB PostgreSQL plugin:
> Enabling auto commit to false is not 100% supported.

Все запросы выполняются в режиме `autocommit=True`. Транзакции — через
SQL-команды `BEGIN`/`COMMIT`/`ROLLBACK` в методе `transact()`.

### 2. _fmt() — auto-convert dict params

ArcadeDB поддерживает только «simple» query mode (нет extended protocol →
нет prepared statements → нет bind-параметров для сложных запросов).
Метод `_fmt()` конвертирует `%(name)s` плейсхолдеры в SQL-литералы:

```python
# Было (не работает в ArcadeDB):
adapter.query("SELECT FROM T WHERE x = %(val)s", {"val": "hello"})

# Стало (авто-конвертация):
adapter.query("SELECT FROM T WHERE x = 'hello'")
```

Tuple-параметры (`%s`) для 1-3 простых значений проходят через psycopg bind.

### 3. sslmode=disable

ArcadeDB PG plugin не поддерживает SSL. `sslmode=disable` обязателен.

### 4. Connection pool management

ConnectionPool с `open=True` (явное открытие при создании). При ошибке
`PoolTimeout` или `OperationalError` пул закрывается и пробрасывается `ArcadeDBError`.

## Отклонения от ТЗ

| ТЗ | Реальность | Причина |
|----|-----------|---------|
| `autocommit=False` + `conn.commit()` | `autocommit=True` + SQL `BEGIN`/`COMMIT` | ArcadeDB limitation |
| Bind-параметры через `%(name)s` | `_fmt()` string formatting | Simple query mode only |
| `conn.autocommit = True/False` toggling | Не используется | ArcadeDB не поддерживает |
| Prepared statements (`prepare_threshold=5`) | Убран | Extended protocol недоступен |

## Тесты

`tests/test_arcadedb_adapter.py` — **1 PASSED, 10 SKIPPED** (нужен ArcadeDB контейнер)

```
PASSED: test_connect_failure — ArcadeDBError при недоступном хосте
SKIPPED: transactions, queries, vectors — ждут Docker container
```
