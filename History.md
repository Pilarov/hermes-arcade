# Hermes ArcadeDB — Ход работ

## Предыстория

Hermes Agent использует SQLite (`state.db`) для хранения сессий и сообщений, FTS5 для полнотекстового поиска. Задача: добавить ArcadeDB для гибридного векторного поиска, не ломая обратную совместимость. В перспективе — перенести остальные модули (holographic memory, kanban).

---

## Этап 0: Инфраструктура

### 0.1 Docker Compose

- Написан `docker-compose.yml` для ArcadeDB (образ arcadedb/arcadedb:latest)
- Проброшен порт 2480 (HTTP API), 5432 не экспозится
- ArcadeDB запущен на сервере 176.108.249.180, работает 26.7.1-SNAPSHOT
- Создана БД `hermes`, в ней вершины: Session(4), Message(26), SearchMatter(4), Fact(4)

### 0.2 Python-окружение

- Установлен Python 3.11.15 (через deadsnakes PPA)
- `hermes-agent` склонирован с GitHub, venv, `pip install -e .` прошёл
- Установлен `fastembed` (модель intfloat/multilingual-e5-large, dim=1024)
- Создан `~/.hermes/config.yaml` (Deepseek, auxiliary.embedding.provider: fastembed)
- Создан `~/.hermes/.env` (OPENAI_API_KEY + OPENAI_BASE_URL)

**Инсайт:** fastembed в Docker с 4 CPU грузит e5-large ~1-2 минуты. Первый запуск — скачивание модели (~1.1GB). Следующие — из кэша.

### 0.3 Выбор транспорта: HTTP API vs psycopg

**Решено:** HTTP API (порт 2480, httpx), не PostgreSQL wire protocol (порт 5432).

Причина: ArcadeDB 26.7.1-SNAPSHOT имеет баг — vector arrays, переданные через
параметр-бinding (:name), ломаются. Jackson десериализует JSON-array элементы
как Java `float[]` вместо `Double`, и LSM_VECTOR индекс их отвергает.

Workaround: передаём векторы как JSON-array SQL-литералы прямо в теле команды.
Поисковые запросы (`vector.neighbors`) принимают :qv нормально — там нет Jackson
на пути.

Дополнительно: не нужен `psycopg[binary]`, порт 5432 не обязательно открывать.

**Необходимое условие:** все `INSERT/UPDATE` с векторами используют ранд-строку
`{_vec(emb.dense)}` вместо `:embedding`.

### 0.4 Освобождение диска

- Почищен pip cache (-2.8GB)
- Переустановлен fastembed с чистого кэша (-2.1GB, потом +1.1GB)
- Итог: ~5GB освобождено (17GB занято → ~12-14GB)

---

## Этап 1: EmbedderProvider

**Файл:** `hermes_cli/embedder.py`

Создан плагин-видный EmbedderProvider:

- `EmbedderProvider` (ABC) — единый контракт: `embed(text)`, `embed_batch(texts)`, `embed_query(text)`, `dimensions`, `model_name`
- `FastembedProvider` — реализация через `fastembed.TextEmbedding` (ONNX, CPU)
- `create_embedder(config)` — фабрика (читает `provider`, `model`, `cache_dir` из config.yaml)

**Решено:** multilingual-e5-large вместо BGE-M3 из плана.
- Загрузка: 1.1GB vs 2.8GB
- 1024d, хорошая multilingual поддержка
- Без sparse (SPLADE/BGE-M3 sparse) — fuse с BM25 вместо dense+sparse+BM25

---

## Этап 2: ArcadeDBAdapter

**Файл:** `hermes_cli/arcadedb.py`

HTTP-клиент для ArcadeDB REST API:

- `ArcadeDBConfig` — dataclass с host, port, database, user, password
- `ArcadeDBAdapter.query(sql, params)` — `POST /api/v1/command/{database}`, возвращает `list[dict]`
- `ArcadeDBAdapter.execute(sql, params)` — то же, для мутаций
- `ArcadeDBAdapter.health()` — проверка доступности
- `_format_server_response(raw)` — парсинг ArcadeDB ответа (`{"result": [...], "error": ...}`)

**Особенности:**
- httpx, не psycopg (см. решение в 0.3)
- Нет пула соединений (ArcadeDB HTTP API stateless)
- Векторы форматируются через `json.dumps([float(x) for x in val])` прям в SQL

---

## Этап 3: Граф-схема

**Файл:** `hermes_cli/arcadedb_schema.py`

Созданы типы и индексы:
- `Session` (id, source, model, title, started_at)
- `Message` (content, role, timestamp, embedding)
- `SearchMatter` (session_rid, summary, keywords, embedding, created_at, profile, model)
- `Fact` (content, category, tags, embedding, created_at)
- `HAS_MESSAGE` edge (Session → Message)

Индексы:
- LSM_VECTOR: SearchMatter[embedding], Fact[embedding] (1024d, COSINE, INT8)
- FULL_TEXT: SearchMatter[summary], Fact[content]

**Что не вошло из плана:** Entity, Task, Run, Project — будут при миграции соответствующих модулей.

---

## Этап 4: ArcadeDBAdapter (новая версия)

**Файл:** `hermes_cli/arcadedb.py`

Переписан на HTTP API вместо psycopg после discovery бага с Jackson float[].

**Текущий API:**
- `query(sql, params)` — SELECT, возвращает list[dict]
- `execute(sql, params)` — INSERT/UPDATE/DELETE
- `health()` — ping

**Нет:** пула соединений, транзакций (пока не нужно — пишем по одной команде)

---

## Этап 4 (продолжение): GraphStore + гибридный поиск

**Файл:** `hermes_cli/graph_store.py`

`GraphStore` — высокоуровневая обёртка: embedder + ArcadeDBAdapter в один API.

- `add_message(session_id, role, content)` — авто-ембеддинг + INSERT + HAS_MESSAGE edge
- `search_sessions(query, top_k, profile, days)` — чисто векторный поиск по SearchMatter
- `hybrid_search_sessions(query, keywords, top_k)` — vector.fuse(dense, fulltext)
- `search_messages(query, top_k)` — прямой поиск по Message.embedding
- `search_facts(query, top_k, category)` — поиск по Fact
- `add_fact(content, category, tags)` — факт + эмбеддинг + Entity edge

**Критические детали:**
- `add_message` вставляет embedding через SQL-литерал (`_vec()`)
- `hybrid_search_sessions` — единственный public метод для session_search
- Fuse стратегия: RRF, groupBy session_rid, groupSize 1

**Инсайт:** Создание SearchMatter как CQRS read-model оказалось прагматичным решением.
Вместо того чтобы traversить граф от SearchMatter к Session (через session_rid LINK),
храним session_rid как свойство и делаем второй запрос `SELECT FROM Session WHERE @rid = :rid`.
Это 2 запроса вместо 1, но на порядки меньше накладных расходов чем travers графа на каждый результат.
Для production стоит денормализовать session_id в SearchMatter и читать всё одним запросом.

---

## Этап 4 (продолжение): session_search_tool

**Файл:** `tools/session_search_tool.py`

Добавлен гибридный поиск в существующий session_search tool:

- `_init_graph_store()` — ленивая инициализация GraphStore
- `_discover_hybrid(graph, fts_db, query, ...)` — поиск через ArcadeDB
- `_discover(...)` — диспетчер: сначала гибрид, при недоступности → FTS5 fallback
- `_get_embed_config()` — читает `auxiliary.embedding.provider` из config.yaml (не из env var!)

Session_search toolset включён в _HERMES_CORE_TOOLS во всех платформах.

---

## Git-инфраструктура

**Создан форк:** `github.com/Pilarov/hermes-arcade` (fork от NousResearch/hermes-agent)
**Remote origin:** изменён на сервере с upstream на форк (git@github.com:Pilarov/hermes-arcade.git)
**SSH:** настроен ключ для git push/pull с github.com
**Workflow:** локальный commit → push в форк → pull на сервере (заменил scp)

---

## Открытые вопросы

1. **SearchMatter.session_id — денормализовать?** Сейчас держим `session_rid` (LINK до Session).
   При поиске делаем второй запрос `SELECT FROM Session WHERE @rid = :rid`.
   Можно хранить `session_id` (идентификатор из SQLite) прямо на SearchMatter — читать всё одним запросом.
2. **Message.embedding — пока не заполнен** (только SearchMatter заполнен).
   Запланирован запуск `arcadedb_migrate.py --embed`.
3. **Параметры ArcadeDB** пока через env vars (`ARCADE_HOST`, `ARCADE_PORT`, etc.),
   хотя конфиг уже поддерживает `auxiliary.embedding`. Нужно перенести в `database.arcadedb.*`.
4. **SessionDB не переключается на ArcadeDB** — SQLite остаётся основным storage.
   ArcadeDB — read-only поисковый слой.
