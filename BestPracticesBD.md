# Best Practices — Графовые Базы Данных (ArcadeDB + Neo4j)

> Источники: документация ArcadeDB (v26.7.1), use-cases Graph RAG, Knowledge Graph,
> Vector Search, Schema, плюс общие принципы графового моделирования (Neo4j-style).

---

## 1. Node vs Edge vs Property — Куда класть данные

### Правило

| Сущность | Хранить как | Почему |
|----------|-------------|--------|
| Имеет идентичность и жизненный цикл | **Vertex** (Node) | `Session`, `Message`, `Task`, `Profile` |
| Отношение со смыслом и атрибутами | **Edge** (Relationship) | `HAS_MESSAGE {seq, role}`, `ASSIGNED_TO {at}` |
| Атрибут, по которому фильтруют | **Property on Vertex** | `Task.status`, `Session.created_at` |
| Контекст конкретной связи | **Property on Edge** | `HAS_MESSAGE.seq`, `MENTIONS.weight` |
| Тяжёлые данные (embeddings, JSON, BLOB) | **External Property** (EXTERNAL true) | 4KB embedding на вершине убивает кэш |

### ⚠ Практическое правило

> Если пишете `WHERE e.some_field IN (...)` — скорее всего это свойство вершины, не ребра.
> Если traversite и на каждом ребре разное значение — свойство ребра.

### Подтверждение из документации

ArcadeDB Graph RAG use-case: вершины `Chunk`, `Entity`, `Person`, `Concept`, `Organization` —
рёбра `MENTIONS`, `RELATES_TO`, `WORKS_AT`, `AUTHORED` — каждое со своими атрибутами.

---

## 2. Разные типы рёбер (is-a Edge) вместо generic + type field

### ❌ Плохо

```sql
CREATE EDGE TYPE LINK EXTENDS E   -- одно ребро на все случаи
  (type STRING)                    -- поле-дискриминатор
```

### ✅ Хорошо

```sql
CREATE EDGE TYPE MENTIONS EXTENDS E
CREATE EDGE TYPE DEPENDS_ON EXTENDS E
CREATE EDGE TYPE BLOCKED_BY EXTENDS E    -- наследуют всё от E
```

Каждый тип ребра = одна семантика. Потом можно искать без фильтрации по `type`:
```cypher
MATCH (t:Task)-[:BLOCKED_BY]->(blocker:Task)   -- только блокировки
```

### Подтверждение

ArcadeDB Knowledge Graph: `CO_AUTHORED`, `CITES`, `COVERS`, `AFFILIATED_WITH` —
отдельные типы. Никакого `RELATIONSHIP {type: "cites"}`.

---

## 3. Rich Edges — Property Pushdown

### Смысл

Дублируй **самые частые поля запросов** с целевой вершины прямо на ребро.
Это позволяет traversить без захода на соседнюю ноду.

```sql
CREATE EDGE TYPE HAS_MESSAGE EXTENDS E (
    seq        INTEGER,      -- порядковый номер (оригинал)
    role       STRING,       -- дубляж с Message
    tokens     INTEGER,      -- дубляж с Message
    created_at DATETIME      -- дубляж с Message
)
```

Теперь:
```cypher
MATCH (s:Session)-[m:HAS_MESSAGE]->(msg:Message)
WHERE m.role = 'user' AND m.tokens < 200
```
— **вообще не читает Message**. Только ребро. 10-100x ускорение на traversal-heavy запросах.

### Когда НЕ делать

Если поле **всегда** нужно вместе с вершиной (например, `Message.content`). Тогда дубляж только увеличит размер.

---

## 4. Денормализация для поиска — Fan Pattern

### Проблема

```cypher
MATCH (s:Session)-[:HAS_MESSAGE]->(m:Message)-[:MENTIONS]->(e:Entity)
WHERE e.name = 'PostgreSQL'          -- 3 hop-а до данных
```

### Решение

Повтори `entity_name` на сообщении:
```sql
CREATE PROPERTY Message.entity_name STRING  -- денормализация
CREATE INDEX ON Message (entity_name) NOT UNIQUE
```

Теперь:
```cypher
MATCH (s:Session)-[:HAS_MESSAGE]->(m:Message)
WHERE m.entity_name = 'PostgreSQL'          -- 1 hop + индекс
```

### Подтверждение

ArcadeDB Graph RAG использует `groupBy: 'source'` — поле `source` продублировано на каждый Chunk.
Не нужно traversить от Chunk к Document чтобы узнать источник.

### Компромисс

Запись дороже (обновлять в двух местах) vs чтение быстрее. Для read-heavy workloads (Hermes — поиск) денормализация оправдана.

---

## 5. Внешнее хранение тяжёлых полей (EXTERNAL true)

### Проблема

Вектор 1024d = 4 KB. Если на каждой вершине, traversal читает 4 KB ради 100 байт топологии.
Буферный кэш вытесняется, I/O растёт.

### Решение

```sql
CREATE PROPERTY Message.embedding LIST (EXTERNAL true)
```

Вектор уходит в парный **external bucket**. На основной вершине — только 8-байтовый указатель.
Загружается лениво — только когда запрос реально его читает.

### Когда использовать

| Когда | EXTERNAL? |
|-------|-----------|
| Embedding 1024d (4 KB) на каждом Message | ✅ Да |
| Embedding — самое горячее поле (каждый запрос его читает) | ❌ Нет — лишний lookup |
| 80% запросов traversal, 20% vector search | ✅ Да |
| Вектор-only workload | ❌ Нет |

### Настройка

```sql
-- INT8 encoding + external + LZ4 compression
CREATE PROPERTY Doc.embedding BINARY (EXTERNAL true, COMPRESSION lz4)
CREATE INDEX ON Doc (embedding) LSM_VECTOR METADATA {
    dimensions: 1024,
    similarity: 'COSINE',
    encoding: 'INT8',
    quantization: 'INT8'
}
```

LZ4 сжимает тексты ~2x, векторы обычно нет (шум), поэтому `COMPRESSION auto` —
попробует сжать и сохранит только если >10% экономии.

### Миграция существующих данных

```sql
ALTER PROPERTY Message.embedding EXTERNAL true   -- новые записи
REBUILD TYPE Message                              -- переписать существующие
```

---

## 6. Векторный + Графовый гибрид

### Базовая стратегия (три варианта)

**A. Семантический поиск по графу:**
```cypher
MATCH (s:Session)-[:HAS_MESSAGE]->(m:Message)
WHERE m.content CONTAINS 'error'
  AND vector.neighbors('Message[embedding]', :e5query, 20)
      FILTER {filter: [m.@rid]}
```

**B. Graph RAG — multi-hop entity bridge:**
```cypher
MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)
      -[:RELATES_TO*1..2]-(related:Entity)
      <-[:MENTIONS]-(other:Chunk)
WHERE c.source = 'document.txt'
RETURN other.content
```

**C. Гибрид dense + sparse + full-text (один запрос):**
```sql
SELECT expand(`vector.fuse`(
    `vector.neighbors`('Chunk[embedding]', :denseQuery, 50),
    `vector.sparseNeighbors`('Chunk[tokens,weights]', :qIdx, :qVal, 50),
    (SELECT @rid, $score FROM Chunk
     WHERE SEARCH_INDEX('Chunk[content]', :keywords) = true),
    { fusion: 'RRF', groupBy: 'source', groupSize: 1 }
)) LIMIT 10
```

### Стратегии фьюжн

| Стратегия | Когда | Формула |
|-----------|-------|---------|
| `RRF` (default) | Разные шкалы (cosine -1..1 vs BM25 0..inf) | `score = weight / (k + rank)` |
| `DBSF` | Оба источника ≈ нормальное распределение | mean ± 3σ norm + weighted sum |
| `LINEAR` | Есть проверенные веса с offline-экспериментов | min-max norm + weighted sum |

### Pre-filter через partition-aware HNSW

Если использовать `partitioned(tenant_id)` как bucket strategy, ArcadeDB автоматически
ограничивает HNSW поиск одной корзиной при `WHERE tenant_id = 'acme'`:

```sql
SELECT vector.neighbors('Doc[embedding]', :queryVector, 10)
FROM Doc
WHERE tenant_id = 'acme'
```

Никакого явного `filter` не нужно. Оптимизация прозрачна.

---

## 7. Индексы — что для чего

| Операция | Тип индекса | Пример |
|----------|-------------|--------|
| Exact match (статус, роль) | `NOT UNIQUE` | `Task.status`, `Message.role` |
| Range (даты, числа) | `NOT UNIQUE` | `Session.created_at` |
| Full-text | `FULL_TEXT` (Lucene) | `Message.content`, `Task.title` |
| Dense vector | `LSM_VECTOR {dimensions, similarity}` | `Message.embedding` (1024d, COSINE) |
| Sparse vector | `LSM_SPARSE_VECTOR {dimensions, modifier}` | SPLADE / BGE-M3 sparse |
| Graph traversal | Автоматически (на @out, @in) | Все рёбра |

### Рекомендации

- **INT8 quantization** для всех production workloads (4x память, 2.5x скорость, 95-98% recall)
- **adaptive efSearch** по умолчанию (два прохода: 2×k, затем 10×k если мало результатов)
- **Higher recall**: `maxConnections=32, beamWidth=200`
- **Fast indexing**: `maxConnections=12, beamWidth=80`
- **>100K vectors**: включить `addHierarchy: true` (HNSW multi-layer)
- **buildGraphNow: false** при создании индекса до загрузки данных

---

## 8. Bucket Strategy — Выбор стратегии партиционирования

| Стратегия | Когда | Эффект |
|-----------|-------|--------|
| `round-robin` (default) | Нет явного ключа партиции | Равномерно по корзинам |
| `thread` | Многопоточная запись | По номеру треда |
| `partitioned(key)` | Tenant-изоляция + быстрый lookup | Одна корзина на tenant + partition-aware HNSW |

Для Hermes (`profile` или `tenant_id` как ключ изоляции) — `partitioned(profile)`.

---

## 9. CQRS в графе — Read Model Subgraph (Графлеты)

Не обязательно traversить все Message для поиска. Создай отдельную **search-модель**:

```sql
CREATE VERTEX TYPE SearchMatter
  PROPERTIES:
    session_rid LINK,        -- ссылка на оригинальную сессию
    summary     STRING,      -- краткое содержание
    keywords    LIST,        -- извлечённые ключевые слова
    embedding   LIST,        -- E5 вектор сводки
    entity_ids  LIST,        -- упомянутые сущности
    created_at  DATETIME,
    project     STRING       -- денормализовано

CREATE INDEX ON SearchMatter (embedding) LSM_VECTOR
  METADATA {dimensions: 1024, similarity: 'COSINE', quantization: 'INT8'}
CREATE INDEX ON SearchMatter (created_at) NOT UNIQUE
CREATE INDEX ON SearchMatter (project) NOT UNIQUE
```

Запрос поиска:
```sql
SELECT expand(`vector.fuse`(
    `vector.neighbors`('SearchMatter[embedding]', :queryVec, 50),
    (SELECT @rid FROM SearchMatter
     WHERE SEARCH_INDEX('SearchMatter[summary]', :keywords) = true
       AND project = :project
       AND created_at BETWEEN :start AND :end),
    { fusion: 'RRF' }
)) LIMIT 10
```

**Плюсы**: не traversить всю модель, быстрее, можно строить асинхронно.
**Минусы**: дополнительное хранение, eventual consistency.

---

## 10. Временные метки на рёбрах

### ❌ Плохо — искать "сообщения за последний час"

```sql
-- надо traversite через 3 hop-а
SELECT FROM Session WHERE created_at > now() - 3600000
  -- не факт что сообщения в том же диапазоне
```

### ✅ Хорошо — created_at на ребре

```sql
CREATE EDGE TYPE HAS_MESSAGE (created_at DATETIME)
```

```cypher
MATCH (s:Session)-[m:HAS_MESSAGE]->(msg:Message)
WHERE m.created_at > now() - 3600000
RETURN msg
```

### ✅ Ещё лучше — TimeSeries тип (ArcadeDB native)

```sql
CREATE TIMESERIES TYPE MessageSeries
  KEYS (session_rid)
  RETENTION 365 DAYS
  VALUE TYPE Message
```

Автоматическое партиционирование по времени. Компактное хранение. PromQL-совместимые запросы.

---

## 11. Наследование типов (is-a hierarchy)

```sql
CREATE VERTEX TYPE Task EXTENDS V
CREATE EDGE TYPE TaskLink EXTENDS E           -- base edge
CREATE EDGE TYPE DependsOn EXTENDS TaskLink   -- is-a
CREATE EDGE TYPE BlockedBy EXTENDS TaskLink
CREATE EDGE TYPE LinkedTo EXTENDS TaskLink
```

Запрос к `TaskLink` видит все три типа. Запрос к `DependsOn` — только зависимости.
Polymorphic queries: `SELECT FROM TaskLink` = DependsOn + BlockedBy + LinkedTo.

---

## 12. Что НЕ нужно переносить в граф (оставить в SQLite)

| Данные | Почему не в графе |
|--------|-------------------|
| Пулл токенов / rate limits | Key-value, нет связей |
| Сессионные токены, nonce | Временные, нет графовой ценности |
| Сырые логи запросов | TimeSeries, не граф |
| Конфигурация | Key-value / документ, не сущность |
| Большие BLOB (файлы, изображения) | Binary storage, не граф |

---

## 13. Чеклист — спроси себя перед созданием типа

- [ ] Это сущность с идентичностью? → Vertex
- [ ] Это связь между сущностями? → Edge
- [ ] Это атрибут сущности? → Property on Vertex
- [ ] Это атрибут конкретной связи? → Property on Edge
- [ ] Это значение ищется через фильтр? → Index
- [ ] Это значение ищется семантически? → LSM_VECTOR index
- [ ] Какие 3 самых частых запроса? → Денормализация под них
- [ ] Сколько данных? >100K → partition + hierarchy
- [ ] Embedding >1KB → EXTERNAL true
- [ ] Это нужно "каждому второму" запросу? → Property Pushdown на ребро

---

## 14. Итоговая схема для Hermes Agent (search-optimized)

### Write Model

```
(:Session {id, title, model, created_at, ...})
(:Message {content, created_at, role, tokens, embedding LIST(EXTERNAL), ...})
(:Task {id, title, status, priority, ...})
(:Profile {name, ...})
(:Entity {name, type, aliases, ...})
(:Fact {content, embedding LIST(EXTERNAL), ...})
(:Project {name, path, description, ...})
(:Repo {url, branch, ...})

(:Session)-[:HAS_MESSAGE {seq, role, tokens, created_at}]->(:Message)
(:Task)-[:ASSIGNED_TO {at}]->(:Profile)
(:Task)-[:DEPENDS_ON]->(:Task)
(:Task)-[:BLOCKED_BY]->(:Task)
(:Task)-[:HAS_RUN {status, started_at}]->(:TaskRun)
(:Task)-[:HAS_COMMENT {created_at}]->(:TaskComment)
(:Task)-[:HAS_EVENT {type, created_at}]->(:TaskEvent)
(:Session)-[:MENTIONS]->(:Entity)
(:Entity)-[:RELATED_TO {weight}]->(:Entity)
(:Entity)-[:HAS_FACT]->(:Fact)
(:Project)-[:CONTAINS]->(:Project)
(:Project)-[:IS_REPO]->(:Repo)
```

### Read Model (Search Matter)

```
(:SearchMatter {
    session_rid → Session,
    summary STRING,
    keywords LIST,
    embedding LIST (EXTERNAL true),
    entity_names LIST,
    created_at DATETIME,
    profile STRING
})
  -[:BELONGS_TO]->(:Session)

Indexes:
  LSM_VECTOR ON embedding {dimensions: 1024, similarity: COSINE, quantization: INT8}
  NOT UNIQUE ON created_at
  NOT UNIQUE ON profile
  FULL_TEXT ON summary
```

### Пример поискового запроса

```sql
SELECT expand(`vector.fuse`(
    `vector.neighbors`('SearchMatter[embedding]', :queryVec, 50),
    (SELECT @rid FROM SearchMatter
     WHERE SEARCH_INDEX('SearchMatter[summary]', :keywords) = true
       AND profile = :profile
       AND created_at BETWEEN :startDate AND :endDate),
    { fusion: 'RRF', groupBy: 'session_rid', groupSize: 1 }
)) LIMIT 20
```

---

## Источники

1. **ArcadeDB Documentation** — docs.arcadedb.com
   - Vector Search Concepts (HNSW, quantization, encoding, efSearch)
   - Vector Embeddings How-To (index creation, tuning, batch ingestion)
   - Extended Functions — Vector (40+ vector SQL functions)
   - Graph RAG Use Case (Chunk → Entity → MENTIONS, hybrid fuse)
   - Knowledge Graph Use Case (Researcher → Paper, vector + full-text hybrid)
   - Schema Concepts (external properties, buckets, inheritance, materialized views)
2. **Neo4j Graph Data Modeling** — общие принципы property graph model
3. **JVector 4.0.0** — HNSW + Vamana алгоритмы под капотом ArcadeDB
