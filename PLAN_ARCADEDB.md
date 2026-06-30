# План миграции Hermes Agent: SQLite → ArcadeDB + Embedder

**Ресурсы:** 4 CPU, 8 GB RAM, Docker
**Embedder:** multilingual-e5-large (dense 1024d) через `fastembed`
**ArcadeDB:** Docker-образ, HTTP API (httpx), не psycopg
**Python:** 3.11, `httpx`, `fastembed`

---

## Этап 0: Инфраструктура

### 0.1 Docker Compose для ArcadeDB
```yaml
services:
  arcadedb:
    image: arcadedb/arcadedb:latest
    ports:
      - "2480:2480"   # HTTP API + Studio
    environment:
      ARCADEDB_ROOT_PASSWORD: hermes
    volumes:
      - arcadedb_data:/storage
    restart: unless-stopped
volumes:
  arcadedb_data:
```

### 0.2 Конфиг Hermes (`config.yaml`)
```yaml
auxiliary:
  embedding:
    provider: fastembed
    model: intfloat/multilingual-e5-large
    dimensions: 1024
```

### 0.3 Новые зависимости
```
fastembed>=0.6,<1     # эмбеддинги (ONNX, без torch)
httpx>=0.28,<1        # HTTP-клиент для REST API ArcadeDB
```

---
**Решение:** Используем HTTP API (порт 2480) вместо PostgreSQL wire protocol (порт 5432).
Причина: ArcadeDB 26.7.1-SNAPSHOT имеет баг — vector arrays через параметр-бinding
ломаются (Jackson десериализует float[] как Double). HTTP API с прямыми SQL-литералами
обходит это. Дополнительно — не нужен psycopg и его бинарный протокол,
порт 5432 не обязательно экспозить.

---

## Этап 1: EmbedderProvider

**Файл:** `hermes_cli/embedder.py`

```
EmbedderProvider (ABC)
├── embed(text) -> NDArray[float32]      # один текст
├── embed_batch(texts) -> list[NDArray]  # батч
├── embed_query(text) -> NDArray         # с query-префиксом
├── dimensions -> int
├── model_name -> str
│
├── FastembedProvider
│   └── использует fastembed.TextEmbedding
│       multilingual-e5-large (1024d)
│
├── OpenAIEmbedder
│   └── text-embedding-3-small/large, аналог
│
└── OllamaEmbedder
    └── любой эмбеддер через Ollama API
```

**Особенности multilingual-e5-large:**
- `dense_vector` — 1024d, косинусная близость
- Без sparse-векторов (упрощение: fuse с BM25 вместо dense+sparse+BM25)
- Префиксы: `"query: "` для поиска, `"passage: "` для индексации

**Конфигурация:**
```yaml
auxiliary:
  embedding:
    provider: fastembed
    model: intfloat/multilingual-e5-large
    dimensions: 1024
```

---
**Решение:** Выбран multilingual-e5-large (1024d) вместо BGE-M3.
BGE-M3 требует 2.8GB на загрузку, в то время как e5-large ~1.1GB.
Для sparse-поиска используем FULL_TEXT Lucene-индекс + vector.fuse.
При ресурсах 8GB RAM можно держать обе модели.

---

## Этап 2: ArcadeDBAdapter

**Файл:** `hermes_cli/arcadedb.py`

```
ArcadeDBAdapter
│
├── __init__(config)        # ArcadeDBConfig (host, port, database, user, password)
├── query(sql, params)      # SELECT — возвращает list[dict]
├── execute(sql, params)    # INSERT/UPDATE/DELETE — возвращает результат
├── health()                # ping
│
├── schema:
│   ├── ensure_schema(sql_list)  # идемпотентное создание
│
└── vector:
    ├── search_dense(index_name, query_vec, k, filter=None)
    └── hybrid_fuse(dense, fulltext, ...)
```

**Транспорт:** HTTP API (`POST /api/v1/command/{database}`), httpx.
**Формат ответа:** ArcadeDB REST (`{"result": [...]})`.
**Особенность:** Векторы передаются JSON-литералами (`[0.1, 0.2, ...]`) прямо в SQL,
не через параметры (обходим Jackson float[] bug).

---

## Этап 3: Граф-схема ArcadeDB

**Файл:** `hermes_cli/arcadedb_schema.py`

```
VERTEX TYPES:
  Session ──HAS_MESSAGE──► Message (embedding LIST)

  SearchMatter              (CQRS read-model для поиска по сессиям)
    └── session_rid → Session
    └── summary, keywords, embedding, created_at, profile

  Fact ──HAS_ENTITY──► Entity

  (Task, Run, Kanban — следующие этапы)
```

**Индексы (ключевые):**
```sql
-- Dense vector
CREATE INDEX ON SearchMatter (embedding) LSM_VECTOR
  METADATA { dimensions: 1024, similarity: 'COSINE', quantization: 'INT8' };
CREATE INDEX ON Message (embedding) LSM_VECTOR
  METADATA { dimensions: 1024, similarity: 'COSINE', quantization: 'INT8' };
CREATE INDEX ON Fact (embedding) LSM_VECTOR
  METADATA { dimensions: 1024, similarity: 'COSINE', quantization: 'INT8' };

-- Full-text (Lucene BM25)
CREATE INDEX ON SearchMatter (summary) FULL_TEXT
  METADATA { analyzer: 'org.apache.lucene.analysis.standard.StandardAnalyzer',
             similarity: 'BM25' };
```

---
**Инсайт:** SearchMatter как CQRS read-model — не traversим граф для поиска.
Session-level summary с отдельным эмбеддингом. Быстрее, чем индексировать
каждое сообщение. Eventual consistency.

---

## Этап 4: Миграция Session/Message (state.db)

**Файлы:** `arcadedb_migrate.py` + `session_search_tool.py` + `graph_store.py`

### Стратегия

SQLite остаётся основным storage для `SessionDB` (обратная совместимость).
ArcadeDB — read-only search-слой через `SearchMatter`.

```
         (запись)                         (чтение)
  Hermes Agent ──► SQLite (state.db)     FTS5 fallback
       │
       └──► ArcadeDB SearchMatter ──► hybrid_search_sessions()
                ↑ (arcadedb_migrate.py)
```

### Скрипт миграции (`arcadedb_migrate.py`)
- `--content` — перенос Session + Message из SQLite в ArcadeDB
- `--embed` — вычисление эмбеддингов (пропуская уже заполненные)
- `--schema` — создание/обновление схемы

### Поиск (новый session_search)

**Текущий:** FTS5 → BM25 re-rank → bookends
**Новый:** GraphStore.hybrid_search_sessions() → bookends

```sql
-- Гибридный: dense + full-text
SELECT expand(`vector.fuse`(
    `vector.neighbors`('SearchMatter[embedding]', :qv, 50),
    (SELECT @rid FROM SearchMatter WHERE SEARCH_INDEX('SearchMatter[summary]', :kw) = true),
    { fusion: 'RRF', groupBy: 'session_rid', groupSize: 1 }
)) LIMIT :tk2
```

---

## Этап 5: Миграция Holographic Memory

**Не начато.** Запланировано:
- Embedder + ArcadeDB Fact вместо HRR-векторов
- `vector.fuse` вместо трёх весов (Jaccard + HRR + FTS5)
- trust_score → WHERE filter

---

## Этап 6: Миграция Kanban

**Не начато.** Запланировано:
- ArcadeDB ACID вместо compare-and-swap
- Task → Run → Comment → Event → Attachment граф

---

## Этап 7: Остальные миграции

**Не начато.** Projects, Verification Evidence, ResponseStore, RetainDB.

---

## Этап 8: Очистка и тесты

**Не начато.** Удаление holographic/retrieval.py, рефакторинг hermes_state.py,
интеграционные тесты.

---

## Итоговая архитектура (факт)

```
┌───────────────┐     ┌──────────────────────┐     ┌──────────────────┐
│  Hermes Agent │────▶│  ArcadeDBAdapter      │────▶│  ArcadeDB        │
│  (Python)      │     │  (httpx, REST API)    │     │  (Docker)        │
│               │     │                       │     │                  │
│  Embedder     │     │  DDL: CREATE TYPE     │     │  HTTP port 2480  │
│  Provider     │     │  SQL: SELECT/INSERT   │     │                  │
│  (fastembed)  │     │  Vector: neighbors    │     │  LSM_VECTOR      │
└───────┬───────┘     │  Fuse: hybrid         │     │  Lucene BM25     │
        │             └──────────────────────┘     └──────────────────┘
        │
        ▼
┌───────────────────────────────┐
│  multilingual-e5-large        │
│  ─ Dense (1024d)              │
│  ─ Cosine similarity          │
│  ─ Passage/query prefix       │
└───────────────────────────────┘
```

**Как это строилось:**

1. **Шаг 1** ─ EmbedderProvider (hermes_cli/embedder.py)
2. **Шаг 2** ─ Docker Compose + ArcadeDBAdapter (hermes_cli/arcadedb.py)
3. **Шаг 3** ─ Граф-схема + SearchMatter (arcadedb_schema.py, graph_store.py)
4. **Шаг 4** ─ Миграция SQLite → ArcadeDB + гибридный поиск (arcadedb_migrate.py, session_search_tool.py)
5. **Шаг 5+** ─ итеративная миграция остальных модулей
