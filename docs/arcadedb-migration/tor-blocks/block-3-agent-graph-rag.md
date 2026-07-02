# Блок 3: Агент — Graph RAG для Hermes (АРХИТЕКТУРНЫЙ ГЭП)

## Статус: НОВОЕ — агент не использует граф

### Проблема

Сейчас агент хранит сообщения в ArcadeDB как плоскую таблицу (как SQLite).
Но ArcadeDB даёт граф + векторы + полнотекстовый поиск в одном движке.
Агент НЕ использует графовые возможности.

### Что делает ArcadeDB Graph RAG (чего нет в Hermes)

| ArcadeDB Pattern | Hermes Current | Что нужно |
|-----------------|----------------|-----------|
| `Chunk → MENTIONS → Entity` | Нет | Извлекать сущности из сообщений |
| `Entity → RELATES_TO → Entity` | Нет | Связывать сущности (co-occurrence) |
| `SearchMatter` (session-level summary) | Схема есть, не создаётся | Создавать при `end_session()` |
| `groupBy: 'source'` dedup | Не используется | Уже в Block 2 |
| Multi-hop: entity → related entity → other sessions | Нет | Траверсить граф для контекста |
| `EXTERNAL true` для embedding'ов | Частично | Включить для Message.embedding |

### Архитектура из ArcadeDB Graph RAG

```
Document (сообщение)
  │
  ├── content (текст)
  ├── embedding (1024d, EXTERNAL true, LSM_VECTOR)
  │
  └── MENTIONS ──→ Entity
                    │
                    └── RELATES_TO ──→ Entity (co-occurrence)
                                         │
                                         └── MENTIONS ←── Document (другая сессия)
```

Запрос: "kubernetes deployment"
→ `vector.neighbors('Document[embedding]', ...)` — семантический поиск
→ `MATCH (d:Document)-[:MENTIONS]->(e:Entity)-[:RELATES_TO]-(e2)<-[:MENTIONS]-(d2:Document)` — multi-hop bridge
→ возвращает связанные документы из других сессий

---

## ТЗ

### 3.1 Entity Extraction на `append_message`

```python
def _extract_entities(self, text: str) -> list[str]:
    """Извлечь именованные сущности из текста сообщения.
    
    Использует простой regex для английского + Unicode для русского/китайского.
    В будущем — заменить на spaCy/stanza."""
    import re
    # Слова с заглавной буквы (English)
    capitalized = re.findall(r'\b[A-Z][a-z]{2,}\b', text)
    # Технические термины (k8s, docker, nginx, etc.)
    tech_terms = re.findall(r'\b[a-z]{3,}\.(?:com|io|org)\b|\b(?:k8s|api|cli|db)\b', text, re.I)
    return list(set(capitalized + tech_terms))[:10]
```

### 3.2 Создание MENTIONS edges

В `append_message()` после создания Message vertex:

```python
# Извлечь сущности
entities = self._extract_entities(content_str)

# Создать MENTIONS edges
for name in entities:
    self._adapter.execute(
        f"CREATE VERTEX Entity SET name = {_q(name)}, entity_type = 'extracted', "
        f"created_at = {_n(ts)}"
    )  # идемпотентно через UNIQUE constraint
    self._adapter.execute(
        f"CREATE EDGE MENTIONS FROM "
        f"(SELECT FROM Message WHERE @rid = {_q(msg_rid)}) TO "
        f"(SELECT FROM Entity WHERE name = {_q(name)}) "
        f"SET weight = 1.0"
    )
```

### 3.3 Создание RELATES_TO edges (co-occurrence)

При появлении двух сущностей в одном сообщении:

```python
# Для каждой пары сущностей в сообщении
for e1, e2 in itertools.combinations(entities, 2):
    self._adapter.execute(
        f"CREATE EDGE RELATES_TO FROM "
        f"(SELECT FROM Entity WHERE name = {_q(e1)}) TO "
        f"(SELECT FROM Entity WHERE name = {_q(e2)}) "
        f"SET weight = 1.0"
    )
```

### 3.4 Auto-create SearchMatter при end_session

В `end_session()` или новом методе `_create_search_matter()`:

```python
def _create_search_matter(self, session_id: str):
    """Создать session-level summary с embedding для быстрого поиска."""
    msgs = self.get_messages(session_id, include_inactive=False)
    if not msgs:
        return
    
    # Собрать summary из первых 3 и последних 3 сообщений
    user_msgs = [m for m in msgs if m.get("role") == "user"]
    summary_text = " | ".join(
        str(m.get("content", ""))[:200] for m in user_msgs[:3] + user_msgs[-3:]
    )
    
    # Извлечь keywords
    keywords = list(set(
        w.lower() for m in user_msgs
        for w in str(m.get("content", "")).split()[:20]
        if len(w) > 3
    ))[:20]
    
    # Вычислить embedding для summary
    emb = None
    if self._embedder:
        try:
            emb = self._embedder.embed([summary_text])[0]
        except Exception:
            pass
    
    emb_sql = f", embedding = {ArcadeDBAdapter._vec(emb.dense)}" if emb else ""
    
    self._adapter.execute(
        f"CREATE VERTEX SearchMatter SET "
        f"session_rid = {_q(self._get_session_rid(session_id))}, "
        f"summary = {_q(summary_text[:500])}, "
        f"keywords = {_q(json.dumps(keywords))}, "
        f"created_at = {_n(_now())}"
        f"{emb_sql}"
    )
```

### 3.5 Multi-hop entity search в `search_messages`

```python
def search_related_sessions(self, session_id: str, top_k: int = 5) -> list[dict]:
    """Найти сессии, связанные через общие сущности (multi-hop bridge)."""
    return self._adapter.query(
        "SELECT DISTINCT other_msg.session_id, other_msg.content "
        "FROM MATCH "
        "{type: Message, as: msg, where: (session_id = %(sid)s)}"
        " -MENTIONS-> {type: Entity, as: e}"
        " -RELATES_TO- {type: Entity, as: e2}"
        " <-MENTIONS- {type: Message, as: other_msg} "
        "RETURN other_msg.session_id, other_msg.content "
        f"LIMIT {top_k}",
        {"sid": session_id},
    )
```

---

## Acceptance Criteria

- [ ] `append_message("Kubernetes deployment on AWS")` — созданы Entity "Kubernetes", "AWS", MENTIONS edges
- [ ] Два сообщения с общей сущностью → RELATES_TO edge между сущностями
- [ ] `end_session()` → создан SearchMatter с summary + embedding
- [ ] `search_related_sessions(sid)` → возвращает сессии через общие сущности
- [ ] Entity extraction корректно работает для en/ru/zh

## Ссылки

- Graph RAG use case: https://docs.arcadedb.com/arcadedb/use-cases/graph-rag.html
- Knowledge Graph: https://docs.arcadedb.com/arcadedb/use-cases/knowledge-graph.html
- EXTERNAL properties: https://docs.arcadedb.com/arcadedb/concepts/schema.html#external-property-storage
- Vector embeddings how-to: https://docs.arcadedb.com/arcadedb/how-to/data-modeling/vector-embeddings.html
