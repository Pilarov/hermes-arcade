# Phase 2: ArcadeDBAdapter — As-Built (HTTP-first)

## Файлы

| Файл | Строки | Назначение |
|------|--------|------------|
| `hermes_cli/arcadedb.py` | 484 | HTTP-транспорт + `_SqlCollector` + `pg_query()` (Живая версия, 07.07) |
| `hermes_cli/redis_lock.py` | 110 | `RedisLockManager` для compression locks (добавлен 07.07) |

## Архитектура (финальная)

```
┌─────────────────────────────────────────┐
│           ArcadeDBAdapter               │
│                                         │
│  CRUD (83 метода)   → HTTP API :2480   │
│    _SqlCollector     → один POST        │
│    implicit транзакция                  │
│                                         │
│  Векторный поиск    → PG wire :5432    │
│    pg_query()        → psycopg3 (Linux) │
│                       → SSH+subprocess  │
│                                         │
│  Compression locks  → Redis :6379       │
│    SET NX EX                            │
│    (fallback: DB UNIQUE CAS)            │
└─────────────────────────────────────────┘
```

## Ключевые решения

### HTTP-first (вместо psycopg-only)
- **Причина**: ArcadeDB Discussion #399 — psycopg2/psycopg3/pg8000 несовместимы с PG wire протоколом
- **Решение**: HTTP API (port 2480) для CRUD и schema-операций
- **Исключение**: `vector.neighbors` через HTTP работает (подтверждено: 1024d векторы)
- **PG протокол**: только для векторного поиска через psycopg3 на Linux

### _SqlCollector (паттерн)
- Накопление SQL-стейтментов → один HTTP POST с `language=sqlscript`
- Implicit BEGIN/COMMIT — все операции в одной транзакции
- Используется в 83 методах `ArcadedbSessionDB`

### pg_query() — двойной режим
- **Linux**: прямой psycopg3 (system libpq) → мгновенно
- **Windows**: SSH → subprocess Python на Linux-хосте
- Авто-детект через `sys.platform`

### _http_send — критический return
- Успешный HTTP-ответ: `return resp.json().get("result", [])`
- При ошибках 400/409: проверка на "already exists/already defined" маркеры → return []
- Прочие ошибки: raise ArcadeDBError

## Отклонения от первоначального ТЗ

| ТЗ | Реальность |
|----|-----------|
| psycopg + PG connection pool | HTTP API, без пула |
| `autocommit=False` | Autocommit=True обязателен |
| psycopg2-binary как зависимость | УДАЛЁН — несовместим |
| `dict_row` row_factory | УДАЛЁН — ручное построение dict |
