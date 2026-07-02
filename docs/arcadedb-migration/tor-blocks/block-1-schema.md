# Блок 1: Схема БД — Фундамент (AUD-1,2,3)

## Статус: CRITICAL — разблокирует 4 модуля

### Проблемы из аудита

| ID | Что | Где |
|----|-----|-----|
| AUD-1 | 7 vertex types не в схеме: ProjectFolder, DiscoveredRepo, Response, Conversation, VerificationEvent, VerificationState, PendingIngest | `arcadedb_schema.py:VERTICES` |
| AUD-2 | 5 свойств Project не в схеме: slug, icon, color, primary_path, archived | `arcadedb_schema.py:~275` |
| AUD-3 | `_VECTOR_DIM=1024` жёстко — OpenAI даёт 1536, Ollama 768 | `arcadedb_schema.py:30` |

### Влияние

Без этого блока не работают:
- `ArcadedbProjectsDB` — все CRUD операции
- `ArcadedbResponseStore` — LRU кэш API ответов
- `ArcadedbVerificationStore` — аудит-лог
- `ArcadedbWriteQueue` — crash-safe очередь RetainDB
- Все не-fastembed эмбеддеры (OpenAI, Ollama, openai-compatible)

---

## ТЗ

### 1.1 Добавить 7 vertex types в VERTICES dict

```python
# arcadedb_schema.py:VERTICES

"ProjectFolder": {
    "props": [
        ("project_id", "STRING"),
        ("path", "STRING"),
        ("label", "STRING"),
        ("is_primary", "INTEGER", "DEFAULT 0"),
        ("added_at", "DOUBLE"),
    ],
    "indexes": [
        (("project_id", "path"), "NOTUNIQUE"),
    ],
},
"DiscoveredRepo": {
    "props": [
        ("root", "STRING"),
        ("label", "STRING"),
        ("last_seen", "DOUBLE"),
    ],
    "indexes": [
        ("root", "UNIQUE"),
    ],
},
"Response": {
    "props": [
        ("response_id", "STRING"),
        ("data", "STRING"),
        ("accessed_at", "DOUBLE"),
    ],
    "indexes": [
        ("response_id", "UNIQUE"),
        ("accessed_at", "NOTUNIQUE"),
    ],
},
"Conversation": {
    "props": [
        ("name", "STRING"),
        ("response_id", "STRING"),
    ],
    "indexes": [
        ("name", "UNIQUE"),
    ],
},
"VerificationEvent": {
    "props": [
        ("command", "STRING"),
        ("cwd", "STRING"),
        ("session_id", "STRING"),
        ("exit_code", "INTEGER"),
        ("output_summary", "STRING"),
        ("created_at", "DOUBLE"),
    ],
    "indexes": [
        (("session_id", "cwd", "created_at"), "NOTUNIQUE"),
    ],
},
"VerificationState": {
    "props": [
        ("session_id", "STRING"),
        ("cwd", "STRING"),
        ("changed_paths_json", "STRING"),
        ("last_event_id", "INTEGER"),
        ("updated_at", "DOUBLE"),
    ],
    "indexes": [
        (("session_id", "cwd"), "NOTUNIQUE"),
    ],
},
"PendingIngest": {
    "props": [
        ("user_id", "STRING"),
        ("session_id", "STRING"),
        ("messages_json", "STRING"),
        ("last_error", "STRING"),
        ("created_at", "DOUBLE"),
    ],
    "indexes": [
        ("created_at", "NOTUNIQUE"),
    ],
},
```

### 1.2 Добавить свойства Project

```python
# В VERTICES["Project"]["props"] добавить:
("slug", "STRING"),
("icon", "STRING"),
("color", "STRING"),
("primary_path", "STRING"),
("archived", "INTEGER", "DEFAULT 0"),
```

### 1.3 Динамический `_VECTOR_DIM`

```python
# arcadedb_schema.py — заменить константу на конфигурируемую
_VECTOR_DIM = 1024  # default

def set_vector_dim(dim: int):
    global _VECTOR_DIM
    _VECTOR_DIM = dim

# При создании индекса — читать dim из конфига или пробить через embedder
```

Индексы `LSM_VECTOR` создаются с актуальной размерностью:
```python
f"dimensions:{_VECTOR_DIM},similarity:'COSINE',quantization:'INT8'"
```

### 1.4 Провалидировать размерность при старте

В `create_session_db()`: после инициализации эмбеддера — проверить что `embedder.dimensions` соответствует `_VECTOR_DIM`. При mismatch — WARNING + fallback на LIKE-поиск.

---

## Acceptance Criteria

- [ ] `SchemaManager.create_all()` создаёт все 7 новых vertex types без ошибок
- [ ] `ArcadedbProjectsDB.create_project("test")` — успешно
- [ ] `ArcadedbResponseStore.put("id", {})` — успешно
- [ ] `ArcadedbVerificationStore.record_terminal_result(...)` — успешно
- [ ] `ArcadedbWriteQueue.enqueue(...)` — успешно
- [ ] При смене эмбеддера на `openai` → `_VECTOR_DIM = 1536` → индексы создаются с правильной размерностью
- [ ] При mismatch: WARNING в лог, fallback на LIKE

## Ссылки

- ArcadeDB Schema docs: `CREATE VERTEX TYPE`, `CREATE PROPERTY` → https://docs.arcadedb.com/arcadedb/concepts/schema.html
- Vector embeddings how-to: `dimensions` must match model output → https://docs.arcadedb.com/arcadedb/how-to/data-modeling/vector-embeddings.html
