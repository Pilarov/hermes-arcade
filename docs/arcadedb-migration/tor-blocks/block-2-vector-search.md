# Блок 2: Векторный поиск — Семантика (AUD-4,5,9 + LOSS-1,2,3)

## Статус: CRITICAL — поиск без фильтров бесполезен

### Проблемы из аудита

| ID | Что | Где |
|----|-----|-----|
| AUD-4 | `search_messages` игнорирует source_filter, exclude_sources, role_filter, sort | `arcadedb_session.py:893-976` |
| AUD-5 | `_q(emb.dense)` вместо `_vec(emb.dense)` в memory store | `arcadedb_store.py:40` |
| AUD-9 | OFFSET вместо SKIP в search_sessions, list_cron_job_runs | `arcadedb_session.py:388,399` |
| LOSS-1 | FTS5 BM25 ranking → LIKE без ранжирования | `search_messages` |
| LOSS-2 | Phrase search ("exact phrase") | `search_messages` |
| LOSS-3 | Boolean operators (AND/OR/NOT) | `search_messages` |

### Архитектурная рекомендация (из ArcadeDB Graph RAG)

ArcadeDB docs показывают канонический паттерн:

```sql
-- Три источника + группировка по source (сессии)
SELECT expand(`vector.fuse`(
    `vector.neighbors`('Chunk[embedding]', :denseQuery, 50),
    `vector.sparseNeighbors`('Chunk[tokens,weights]', :qIdx, :qVal, 50),
    (SELECT @rid, $score FROM Chunk
     WHERE SEARCH_INDEX('Chunk[content]', :keywords) = true),
    { fusion: 'RRF', groupBy: 'source', groupSize: 1 }
)) LIMIT 10
```

В Hermes эквивалент:
```sql
SELECT expand(`vector.fuse`(
    `vector.neighbors`('Message[embedding]', :denseQuery, 50),
    (SELECT @rid, $score FROM Message
     WHERE SEARCH_INDEX('Message[content]', :keywords) = true),
    { fusion: 'RRF', groupBy: 'session_id', groupSize: 1 }
)) LIMIT 20
```

---

## ТЗ

### 2.1 Исправить `search_messages` — применить все фильтры

```python
def search_messages(self, query, source_filter=None, exclude_sources=None,
                    role_filter=None, limit=20, offset=0, sort=None,
                    include_inactive=False):
```

Каждая из трёх веток (vector, CJK LIKE, LIKE) должна добавлять WHERE-условия:

```python
# Фильтры
filters = []
if source_filter:
    filters.append(f"s.source = {_q(source_filter)}")
if exclude_sources:
    excl = ", ".join(_q(s) for s in exclude_sources)
    filters.append(f"s.source NOT IN ({excl})")
if role_filter:
    filters.append(f"m.role = {_q(role_filter)}")

# Встраиваем в запрос
where = " AND ".join(filters) if filters else "1=1"
```

### 2.2 Исправить `_q` → `_vec` в `arcadedb_store.py`

```python
# Было (строка 40):
emb_sql = f", embedding = {_q(emb.dense)}"

# Стало:
from hermes_cli.arcadedb import ArcadeDBAdapter
emb_sql = f", embedding = {ArcadeDBAdapter._vec(emb.dense)}"
```

### 2.3 `OFFSET` → `SKIP` во всех методах

Глобальный поиск по кодовой базе:
```
OFFSET → SKIP
```
Файлы: `arcadedb_session.py` (3 места), `list_cron_job_runs`, `search_sessions`.

### 2.4 Добавить `groupBy: 'session_id'` в hybrid_search

```python
def hybrid_search_sessions(self, query, keywords="", top_k=10, ...):
    sql = (
        "SELECT expand(`vector.fuse`(\n"
        f"    `vector.neighbors`('SearchMatter[embedding]', {qv}, {top_k * 3}),\n"
        f"    (SELECT @rid FROM SearchMatter WHERE summary LIKE {_q(f'%{query}%')}),\n"
        "    { fusion: 'RRF', groupBy: 'session_rid', groupSize: 1 }\n"
        f")) LIMIT {top_k}"
    )
```

Это гарантирует что каждая сессия возвращается максимум 1 раз — нет дублирования.

### 2.5 Документировать потери FTS5

В History.md и README явно указать что:
- BM25 ranking → заменён на векторный similarity + LIKE fallback
- Phrase search → не поддерживается
- Boolean operators → не поддерживаются
- При обновлении ArcadeDB до версии с работающим `SEARCH_INDEX` через PG — включить обратно

---

## Acceptance Criteria

- [ ] `search_messages("query", source_filter="telegram")` — возвращает только telegram-сессии
- [ ] `search_messages("query", role_filter="user")` — только user-сообщения
- [ ] `search_messages("query", exclude_sources=["cron"])` — без cron
- [ ] `search_messages("query", sort="newest")` — сортировка по дате
- [ ] `search_sessions(limit=5, offset=5)` — вторая страница результатов
- [ ] `list_cron_job_runs("job-1", limit=5, offset=5)` — пагинация работает
- [ ] `add_fact()` в `ArcadedbMemoryStore` — embedding как JSON-array, не строка
- [ ] `hybrid_search_sessions("query")` — не более 1 результата на сессию (groupBy)

## Ссылки

- ArcadeDB Graph RAG: https://docs.arcadedb.com/arcadedb/use-cases/graph-rag.html
- `vector.fuse` docs: https://docs.arcadedb.com/arcadedb/concepts/vector-search.html#hybrid-search
- `groupBy` retrieval: https://docs.arcadedb.com/arcadedb/concepts/vector-search.html#vector-groupby
