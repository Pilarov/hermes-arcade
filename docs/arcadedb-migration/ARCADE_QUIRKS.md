# ArcadeDB: Особенности, ограничения и workarounds

Собрано в ходе миграции Hermes Agent с SQLite на ArcadeDB 26.7.1 (июль 2026).

---

## 1. Docker / Развёртывание

### Пароль
- **Работает:** `-Darcadedb.server.rootPassword=...` в `JAVA_OPTS`
- **НЕ работает:** `ARCADEDB_ROOT_PASSWORD` env var (ломает неинтерактивный старт в 26.7+)
- **First boot:** Java property перезаписывает предустановленного root из Docker image
- **Повторный старт:** пароль читается из `config/server-users.jsonl` (хэш), Java property игнорируется
- **HTTP API:** требует авторизации Basic Auth. 401 = предустановленный пароль Docker image (не наш)
- **PG протокол:** принимает любой пароль (отдельный механизм аутентификации)

### defaultDatabases
- **Синтаксис:** `-Darcadedb.server.defaultDatabases=DBName[root:password]`
- **Проблема:** квадратные скобки `[...]` глобятся bash'ем → `hermes[root:hermes123]` съедается
- **Решение:** использовать `POST /api/v1/server {"command":"create database DB"}` после старта
- **Или:** экранировать в single quotes: `'hermes[root:hermes123]'` (работает в bash-скриптах)

### База данных
- Создаётся через HTTP API: `POST /api/v1/server {"command":"create database mydb"}`
- `mkdir databases/mydb` создаёт директорию, но сервер НЕ видит базу
- База должна быть создана через API или Studio

---

## 2. PostgreSQL Wire Protocol

### Только simple query mode
- **НЕТ:** extended protocol, prepared statements, bind parameters (`$1`, `?`)
- **НЕТ:** SSL/TLS
- **НЕТ:** connection pooling (Issue #1325, открыт с ноября 2023)
- **ДА:** `autocommit=True` (обязательно)
- **ДА:** SQL-level `BEGIN`/`COMMIT`/`ROLLBACK` (но с `autocommit=True` они no-op)

### Pool corruption
- После ~20 `transact()` вызовов пул входит в состояние "Transaction not active"
- `COMMIT` падает → `ROLLBACK` падает → соединение отбрасывается
- Свежие соединения НЕ помогают — сервер накапливает испорченное состояние
- **Решение:** не использовать PG для записей. Только для чтения (SELECT, vector.neighbors)

### Bind parameters
- `%s` не работает (нет prepared statements)
- **Решение:** string formatting через `_fmt()` (dict) и `_fmt_tuple()` (tuple)
- `_q()` экранирует `\` → `\\` и `'` → `\'`, None → NULL, bool → 0/1

### Диалект ArcadeDB SQL
| PostgreSQL | ArcadeDB |
|-----------|----------|
| `LIMIT X OFFSET Y` | `LIMIT X SKIP Y` |
| `LIKE ... ESCAPE '\'` | `LIKE ...` (без ESCAPE) |
| `FROM a, b` joins | ❌ Не поддерживается |
| `DELETE VERTEX` | Зависает на edge cascade → soft-delete (`UPDATE active=0`) |
| `RETURN @rid` | ❌ Не поддерживается |
| `expand(...)` | ✅ Поддерживается (не только OrientDB) |

---

## 3. HTTP API

### Транзакции — три способа

**sqlscript (implicit BEGIN/COMMIT):**
```json
{"language": "sqlscript", "command": "DELETE ...; INSERT ...; SELECT ..."}
```
- Все стейтменты в одном HTTP POST
- Атомарно: ошибка в любом стейтменте → откат всей пачки
- Результат — от последнего стейтмента (если SELECT). Для INSERT — запись.
- **Проблема:** `CREATE VERTEX ... IF NOT EXISTS` не работает в sqlscript
- **Проблема:** `CREATE PROPERTY` падает с "already exists" (500 вместо игнорирования)

**Сессионные транзакции (явные BEGIN/COMMIT):**
```
POST /api/v1/begin/{db}     → arcadedb-session-id: AS-xxx
POST /api/v1/command/{db}   → Header: arcadedb-session-id
POST /api/v1/commit/{db}    → Header: arcadedb-session-id
```
- Полноценный ACID, 30 сек timeout на сессию
- Не использовали — сложность управления сессиями

**Single statement (autocommit):**
```json
{"language": "sql", "command": "INSERT INTO ..."}
```
- Каждый запрос — отдельная транзакция
- Нет гарантии видимости между запросами (read-committed)

### Типизированные маркеры для векторов (v26.5.1+)
```json
{"params": {"q": {"$bytes": "base64..."}}}
```
- `$bytes`: base64 от int8 байтов (encoding='INT8')
- `$int8`: массив int8 чисел
- **Требует:** `BINARY` property + `encoding:'INT8'` на индексе
- **НЕ работает:** с `ARRAY_OF_FLOATS` (FLOAT32) → "Unsupported query vector type: byte[]"

### Ограничения HTTP API
- Inline JSON массив из 1024 float'ов (~7KB) не парсится сервером (26.7.1)
- `expand(vector.neighbors(..., [0.1, 0.1, ...], 3))` — inline массив **работает** для маленьких векторов (≤10 dims)
- Для больших векторов: использовать PG протокол или `$bytes` typed marker

---

## 4. Векторный поиск

### Работает через PG протокол
```python
def pg_query(self, sql):
    conn = psycopg.connect(...)  # свежее соединение
    cur.execute(sql, params or {})
    return [dict(r) for r in cur.fetchall()]
```
- Inline JSON массив из 1024 float'ов → **работает** через PG
- Только чтение → нет pool corruption
- Используется для `search_messages` и `hybrid_search_sessions`

### HNSW индекс
- `quantization:'INT8'`: 4x memory savings, 2.5x faster search. Рекомендовано.
- `encoding:'INT8'`: wire-level int8. Требует `BINARY` property. Не совместимо с `quantization:'INT8'`.
- `encoding:'FLOAT32'` (default): `ARRAY_OF_FLOATS` property.

### vector.fuse (hybrid search)
- Доступен с 26.5.1 (наш 26.7.1 поддерживает)
- Fuse dense (vector.neighbors) + sparse (BM25/full-text) результаты
- Синтаксис: `SELECT expand(vector.fuse(source1, source2, {fusion:'RRF', groupBy:'field'}))`
- `expand()` обязателен для `vector.fuse` и `vector.neighbors` в ArcadeDB SQL

---

## 5. Транзакции и изоляция

### Read committed — единственный уровень
- **Issue #1000 (открыт):** "level is always Read committed"
- REPEATABLE_READ и SERIALIZABLE **не поддерживаются**
- Следствие: SELECT в новой транзакции может не видеть COMMIT из предыдущей
- **Workaround:** retry SELECT с backoff (3 попытки × 50ms)

### sqlscript и уникальные индексы
- `INSERT` с UNIQUE индексом должен падать при дубликате
- **Проблема:** в sqlscript проверка уникальности может быть отложена
- **Workaround:** проверять существование перед INSERT (SELECT + retry)

### DELETE VERTEX с рёбрами
- ArcadeDB зависает при каскадном удалении через рёбра
- **Решение:** soft-delete (`UPDATE SET active=0, deleted=1`)

---

## 6. Производительность

### HTTP vs PG
| Операция | HTTP (ms) | PG (ms) |
|----------|-----------|---------|
| Одиночный SELECT | ~5 | ~1 |
| sqlscript batch (3 стейтмента) | ~8 | ~3 |
| 59 тестов (полный прогон) | 35 сек | 540 сек |

HTTP быстрее PG в 15 раз для тестового прогона (нет pool corruption overhead).

### Lucene FULL_TEXT индекс
- **Асинхронный:** индексация с задержкой ~15 секунд (inactivity timeout)
- Данные записаны мгновенно, поиск может не найти их сразу
- **Workaround:** `time.sleep(10)` после вставки перед поиском

### sqlscript batch performance
- Один HTTP POST вместо N отдельных запросов
- ~3× быстрее чем индивидуальные запросы (сокращение round-trips)

---

## 7. Схема

### Типы данных
- `LIST` = `ARRAY_OF_FLOATS` для векторных эмбеддингов (FLOAT32)
- `BINARY` = int8 байты для векторов (encoding='INT8')
- `DOUBLE` = timestamp (IEEE 754, ~15 знаков точности)
- `STRING` для текста

### Индексы
- `UNIQUE`: не допускает дубликаты (LSM Tree)
- `NOTUNIQUE`: допускает дубликаты
- `FULL_TEXT`: Lucene-based полнотекстовый поиск
- `LSM_VECTOR`: HNSW граф для векторного поиска
- **Composite:** `(("col1", "col2"), "NOTUNIQUE")` — tuple для составных индексов

### EXTERNAL свойства
- `LIST(EXTERNAL true)`: хранит данные в отдельном paired bucket
- Экономия памяти — основной bucket содержит только указатель (8 байт)

---

## 8. Python клиент (наш опыт)

### Рекомендуемая архитектура
```
CRUD          → HTTP sqlscript (httpx, port 2480)
  _SqlCollector: собирает SQL → один POST с language=sqlscript
  fetchall() отправляет накопленный батч

Векторный поиск → PG wire (psycopg, port 5432)
  pg_query(): свежее соединение, только чтение

Схема (DDL)  → HTTP single query (httpx)
  CREATE VERTEX TYPE, CREATE PROPERTY, CREATE INDEX
  Игнорировать "already exists" ошибки
```

### String formatting policy
- Dict params `%(name)s` → `_fmt()` auto-convert
- Tuple params `%s` → `_fmt_tuple()` auto-convert
- INSERT/UPDATE с >3 параметрами → `_q()`/`_n()` ручное форматирование
- `_http_execute_strict(sql)`: не игнорирует "already exists" (для CAS)

### SqlCollector паттерн
```python
class _SqlCollector:
    def execute(self, sql, params=None):
        self._sqls.append(formatted_sql)   # накапливает
    
    def fetchall(self):
        script = ";".join(self._sqls)      # склеивает
        self._sqls = []
        return self._adapter._http_send_script(script)
    
    def fetchone(self):
        return self.fetchall()[0] if rows else None
```
- fetchall() в середине `_do` → отправляет накопленный SQL → очищает буфер
- Следующий execute() → новый батч
- Подходит для create_session (SELECT → return early → INSERT → SELECT)

---

## 9. Известные баги ArcadeDB (по версиям)

| Версия | Баг | Статус |
|--------|-----|--------|
| 26.4.2 | Нет `vector.fuse()` | Исправлено в 26.5.1 |
| 26.7.1 | Inline 1024d float array через HTTP не парсится | Открыт |
| 26.7.1 | `CREATE PROPERTY ... IF NOT EXISTS` → 500 в sqlscript | Открыт |
| 26.8.1-SNAPSHOT | Парольный prompt ломает Docker неинтерактивный старт | Открыт |
| Все | PG Connection pooling не поддерживается (Issue #1325) | Открыт с 2023 |
| Все | Только Read Committed изоляция (Issue #1000) | Открыт |

---

---

## 11. PG Wire Protocol — SCRAM-SHA-256 Authentication

ArcadeDB 26.7.x PG plugin использует `AuthenticationSASL` (код 10, SCRAM-SHA-256).
Пароли хранятся как `PBKDF2WithHmacSHA256` — НЕ в SCRAM-формате.

| Клиент | Результат | Причина |
|--------|-----------|---------|
| **psql** (libpq) | ✅ Работает | Системный libpq (Linux) корректно обрабатывает SCRAM |
| **psycopg3** (Linux, system libpq) | ✅ Работает | Тот же libpq, что у psql |
| **psycopg3** (Windows, bundled libpq) | ❌ `password authentication failed` | Bundled libpq 14.x/18.x несовместим |
| **psycopg2** (любая платформа) | ❌ `PGRES_TUPLES_OK error` | Баг с 2015: psycopg2 несовместим с ArcadeDB PG |
| **pg8000** (любая версия) | ❌ `password authentication failed` | Собственная SCRAM-реализация несовместима |

**Решение**: на Linux — psycopg3 с системным libpq. На Windows — SSH+subprocess на Linux-хост.

**Источник**: ArcadeDB Discussion #399 — мейнтейнер подтвердил несовместимость psycopg2 с 2022 года.

## 12. Vector Search — PG vs HTTP

| Протокол | `vector.neighbors` | Причина |
|----------|-------------------|---------|
| **HTTP API** | ✅ Работает (4d и 1024d) | JSON-парсинг float[] корректный |
| **PG wire** | ❌ NPE `params[0] is null` | Jackson не может распарсить inline float[] через PG |

**Решение**: векторный поиск через HTTP API (порт 2480). PG используется только для CRUD через psycopg3.

**Важно**: ArcadeDB 26.7.2-SNAPSHOT: `vector.neighbors` через HTTP работает на 1024d векторах (подтверждено: 5 результатов из 9 SearchMatter вершин).

## 13. Hermes Provider Routing

Hermes Gateway маршрутизирует модели через **OpenRouter** (auto-detect), игнорируя кастомные `providers.*` настройки.

| Попытка | Результат |
|---------|-----------|
| `provider: openai` в config | Игнорируется — OpenRouter имеет приоритет |
| `disabled_providers` | Ключ не существует в Hermes 0.17.0 |
| `HERMES_PROVIDER=openai` env var | Не работает |
| `model.provider: openai` | Неправильный формат для этой версии |

**Решение**: использовать отдельный API-бридж (fastapi + httpx → DeepSeek напрямую) ИЛИ получить OpenRouter API-ключ.

## 10. Полезные ссылки

- [Docker docs](https://docs.arcadedb.com/arcadedb/how-to/operations/docker.html)
- [HTTP API reference](https://docs.arcadedb.com/arcadedb/reference/http-api/http.html)
- [SQL Script reference](https://docs.arcadedb.com/arcadedb/reference/sql/sql-script.html)
- [Vector Search](https://docs.arcadedb.com/arcadedb/concepts/vector-search.html)
- [Server Configuration](https://docs.arcadedb.com/arcadedb/how-to/operations/server.html)
- [Users & Security](https://docs.arcadedb.com/arcadedb/how-to/operations/users.html)
- [Issue #1325: PG Pool](https://github.com/ArcadeData/arcadedb/issues/1325)
- [Issue #1000: Isolation](https://github.com/ArcadeData/arcadedb/issues/1000)
- [Discussion #115: HTTP transactions](https://github.com/ArcadeData/arcadedb/discussions/115)
