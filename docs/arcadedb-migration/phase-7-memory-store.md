# Phase 7: Memory Store — Holographic Memory Migration

| Поле | Значение |
|------|----------|
| **Номер** | Phase 7 |
| **Название** | Holographic Memory Store → ArcadeDB |
| **Новых строк** | ~200 |
| **Сложность** | Medium |
| **Зависит от** | Phase 2 (Adapter v2), Phase 3 (SessionDB helpers) |
| **Разблокирует** | — (независим от других фаз) |

---

## Overview

Миграция Holographic Memory Store (`plugins/memory/holographic/store.py`, 578 строк)
с SQLite на ArcadeDB. Замена HRR-векторов (фазовая алгебра) на реальные эмбеддинги
через EmbedderProvider, FTS5 — на Lucene FULL_TEXT.

### Ключевые отличия

| SQLite | ArcadeDB | Примечание |
|--------|----------|------------|
| `facts` table | `Fact` vertices | Уже определены в схеме |
| `entities` table | `Entity` vertices | Уже определены в схеме |
| `fact_entities` junction | `MENTIONS` edges | Граф вместо таблицы |
| `memory_banks` table | Не нужно | LSM_VECTOR index = "bank" |
| `facts_fts` (FTS5) | `FULL_TEXT` index на `Fact.content` | Lucene BM25 |
| `hrr_vector` BLOB | `embedding` LIST (LSM_VECTOR) | Настоящие эмбеддинги |
| Trust-based ranking | Trust scoring на вершине | WHERE filter вместо веса |

---

## Files

### Новые файлы

| Файл | Строки | Назначение |
|------|--------|------------|
| `plugins/memory/holographic/arcadedb_store.py` | ~200 | `ArcadedbMemoryStore` класс |

### Модифицируемые файлы

| Файл | Изменение | Связь |
|------|-----------|-------|
| `plugins/memory/holographic/store.py` | Добавить `ArcadedbMemoryStore` path в factory/logic | [см. store.py](../../plugins/memory/holographic/store.py) |
| `hermes_cli/arcadedb_schema.py` | Добавить `FULL_TEXT` index на `Fact.content` | [см. arcadedb_schema.py](../../hermes_cli/arcadedb_schema.py) |

---

## API Specification

```python
# plugins/memory/holographic/arcadedb_store.py

class ArcadedbMemoryStore:
    """
    ArcadeDB-backed memory store. Совместим с MemoryStore API.

    Заменяет:
      - facts table → Fact vertices
      - entities table → Entity vertices
      - fact_entities junction → HAS_FACT edges
      - memory_banks → не нужны (ArcadeDB vector index = "bank")
      - facts_fts (FTS5) → FULL_TEXT index на Fact.content
      - hrr_vector BLOB → embedding LIST (LSM_VECTOR index)
    """

    def __init__(self, adapter: ArcadeDBAdapter, embedder: EmbedderProvider):
        """adapter из Phase 2, embedder из Phase 3."""

    # ---- CRUD ----
    def add_fact(self, content: str, category: str = "general",
                 tags: list[str] = None) -> int: ...
    def update_fact(self, fact_id: int, content: str = None,
                    trust_delta: float = 0, tags: list[str] = None,
                    category: str = None) -> None: ...
    def remove_fact(self, fact_id: int) -> None: ...
    def list_facts(self, category: str = None, min_trust: float = 0,
                   limit: int = 50) -> list[dict]: ...

    # ---- Search ----
    def search_facts(self, query: str, category: str = None,
                     min_trust: float = 0, limit: int = 20) -> list[dict]: ...

    # ---- Feedback ----
    def record_feedback(self, fact_id: int, helpful: bool) -> dict: ...

    # ---- Maintenance ----
    def rebuild_all_vectors(self, dim: int = 1024) -> None: ...
    def close(self) -> None: ...
```

---

## Implementation Details

### `add_fact()` — CRITICAL

```python
def add_fact(self, content: str, category: str = "general",
             tags: list[str] = None) -> int:
    """
    Добавляет факт с авто-embedding и entity extraction.

    SQLite: INSERT + extract entities + HRR vector + FTS trigger
    ArcadeDB: CREATE VERTEX Fact + MENTIONS edges + FULL_TEXT index (auto)
    """
    now_ts = time.time()
    tags_json = json.dumps(tags or [])

    # Вычисляем embedding (замена HRR вектора)
    emb = self._embedder.embed([content])[0]

    # Извлекаем entities (regex-based NER — та же логика)
    entity_names = self._extract_entities(content)

    def _do(cur):
        # Insert Fact vertex
        cur.execute(
            "CREATE VERTEX Fact SET "
            "content = %s, category = %s, tags = %s, "
            "trust_score = 0.5, retrieval_count = 0, helpful_count = 0, "
            f"embedding = {ArcadeDBAdapter._vec(emb.dense)}, "
            "created_at = %s, updated_at = %s",
            (content, category, tags_json, now_ts, now_ts)
        )

        # Get created fact
        cur.execute(
            "SELECT FROM Fact WHERE content = %s AND created_at = %s LIMIT 1",
            (content, now_ts)
        )
        fact = cur.fetchone()

        # Create entities + MENTIONS edges
        for name in entity_names:
            self._ensure_entity(cur, name)
            cur.execute(
                "CREATE EDGE MENTIONS FROM "
                "(SELECT FROM Fact WHERE @rid = %s) TO "
                "(SELECT FROM Entity WHERE name = %s) "
                "SET weight = 1.0",
                (fact["@rid"], name)
            )

        return hash(fact["@rid"]) & 0x7FFFFFFF

    return self._adapter.transact(_do)
```

### `search_facts()` — гибридный поиск

```python
def search_facts(self, query: str, category: str = None,
                 min_trust: float = 0, limit: int = 20) -> list[dict]:
    """
    Поиск фактов через FULL_TEXT (Lucene) или vector hybrid.

    SQLite: facts_fts MATCH ? ORDER BY rank
    ArcadeDB: SEARCH_INDEX('Fact[content]', ?) BM25
    """
    q_vec = self._embedder.embed_query(query)

    # Hybrid: fuse dense + fulltext
    sql = """
        SELECT expand(`vector.fuse`(
            `vector.neighbors`('Fact[embedding]', %(qv)s, %(tk)s),
            (SELECT @rid FROM Fact
             WHERE SEARCH_INDEX('Fact[content]', %(kw)s) = true
               AND trust_score >= %(mt)s
        """
    if category:
        sql += " AND category = %(cat)s"
    sql += """
            ),
            { fusion: 'RRF' }
        )) LIMIT %(lim)s
    """

    params = {
        "qv": q_vec.dense,
        "tk": limit * 2,
        "kw": query,
        "mt": min_trust,
        "lim": limit,
    }
    if category:
        params["cat"] = category

    return self._adapter.query(sql, params)
```

### `record_feedback()` — trust scoring

```python
def record_feedback(self, fact_id: int, helpful: bool) -> dict:
    """Асимметричная корректировка trust_score."""
    delta = 0.05 if helpful else -0.10

    def _do(cur):
        cur.execute(
            "UPDATE Fact SET "
            "trust_score = (SELECT trust_score + %s FROM Fact WHERE @rid = %s), "
            "retrieval_count = retrieval_count + 1, "
            f"helpful_count = helpful_count + {1 if helpful else 0}, "
            "updated_at = %s "
            "WHERE @rid = %s "
            "RETURN AFTER trust_score, retrieval_count, helpful_count",
            (delta, fact_id, time.time(), fact_id)
        )
        result = cur.fetchone()
        return {
            "old_trust": result["trust_score"] - delta,
            "new_trust": result["trust_score"]
        }

    return self._adapter.transact(_do)
```

### Schema additions (в `arcadedb_schema.py`)

```sql
-- FULL_TEXT index для поиска фактов
CREATE INDEX IF NOT EXISTS ON Fact (content) FULL_TEXT
  METADATA { analyzer: 'StandardAnalyzer', similarity: 'BM25' }
```

---

## Тест-кейсы

**Тестовый файл:** `tests/test_arcadedb_memory.py` → [см. Phase 1: memory tests](phase-1-testing.md#files-8-14)

| ID | Тест | Описание |
|----|------|----------|
| MEM-01 | `test_add_fact` | add_fact() с авто-embedding |
| MEM-02 | `test_search_facts_dense` | Векторный поиск фактов |
| MEM-03 | `test_search_facts_hybrid` | Full-text + dense = vector.fuse |
| MEM-04 | `test_search_facts_category` | Фильтр по category |
| MEM-05 | `test_search_facts_trust` | Фильтр по min_trust |
| MEM-06 | `test_entity_linking` | Entity extraction + MENTIONS edges |
| MEM-07 | `test_feedback_helpful` | helpful → trust +0.05 |
| MEM-08 | `test_feedback_unhelpful` | unhelpful → trust -0.10 |
| MEM-09 | `test_rebuild_vectors` | rebuild_all_vectors() |
| MEM-10 | `test_update_fact` | Частичное обновление факта |

---

## Acceptance Criteria

- [ ] `ArcadedbMemoryStore` реализует все 8 публичных методов `MemoryStore`
- [ ] HRR векторы заменены на LSM_VECTOR embeddings (1024d, COSINE, INT8)
- [ ] FTS5 заменён на FULL_TEXT Lucene (BM25)
- [ ] Entity extraction + `MENTIONS` edges работают
- [ ] Trust scoring (asymmetric: +0.05 / -0.10) работает
- [ ] `rebuild_all_vectors()` перевычисляет все эмбеддинги
- [ ] Все 10 тестов проходят

---

## Cross-References

### Предшествующие фазы
- **[← Phase 2: Adapter v2](phase-2-adapter-v2.md)** — `ArcadeDBAdapter` + `_vec()` для векторов
- **[← Phase 3: SessionDB](phase-3-sessiondb.md)** — общие helpers (`_now()`, `_encode_content()`)
- **[← Phase 5: Migration Tool](phase-5-migration-tool.md)** — `migrate_memory()`

### Связи с существующими файлами
- **[`plugins/memory/holographic/store.py`](../../plugins/memory/holographic/store.py)** — reference API (578 строк)
- **[`plugins/memory/holographic/holographic.py`](../../plugins/memory/holographic/holographic.py)** — HRR vectors (заменяется на embedding)
- **[`hermes_cli/embedder.py:EmbedderProvider`](../../hermes_cli/embedder.py)** — embedding provider
- **[`hermes_cli/arcadedb_schema.py`](../../hermes_cli/arcadedb_schema.py)** — Fact, Entity vertex types уже определены
- **[`hermes_cli/arcadedb.py:ArcadeDBAdapter._vec`](../../hermes_cli/arcadedb.py)** — vector literal formatting (Phase 2)
- **[`hermes_cli/arcadedb_helpers.py`](../../hermes_cli/arcadedb_helpers.py)** — shared utilities (Phase 3)

### Связи внутри документации
- **[Phase 1: test_arcadedb_memory.py](phase-1-testing.md#files-8-14)** — тестовые контракты
- **[Phase 2: _vec() workaround](phase-2-adapter-v2.md#vector-workaround)**

### Последующие фазы
- (Memory Store не блокирует другие фазы — выполняется параллельно)

---

## Implementation Sequence

```
1. Создать plugins/memory/holographic/arcadedb_store.py (~200 строк)
2. Добавить FULL_TEXT index в arcadedb_schema.py (section Fact indexes)
3. Реализовать add_fact() с авто-embedding
4. Реализовать search_facts() с vector.fuse(dense + fulltext)
5. Реализовать entity extraction + MENTIONS edges
6. Реализовать record_feedback() (asymmetric trust scoring)
7. Реализовать update_fact / list_facts / rebuild_all_vectors
8. Сделать тесты зелёными (MEM-01 → MEM-10)
```

## Notes

- **HRR → Embedder:** Замена HRR (фазовая алгебра из `holographic.py`) на реальные
  эмбеддинги через `FastembedProvider`. Это даёт лучшую семантическую близость,
  но требует загрузки модели (~1.1GB для e5-large).
- **FTS5 → Lucene:** `FULL_TEXT` Lucene index использует BM25 similarity — совместимый
  с FTS5 ranking.
- **memory_banks удалены:** Векторный индекс ArcadeDB (`LSM_VECTOR`) заменяет
  HRR-bundles. `search_facts` использует `vector.neighbors` напрямую.
- **Entity extraction:** Сохраняется та же regex-based NER логика из `store.py`.
  `MENTIONS` edge заменяет `fact_entities` junction table.
