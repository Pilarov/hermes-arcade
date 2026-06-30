# Phase 6: KanbanDB Wrapper — ArcadedbKanbanDB

| Поле | Значение |
|------|----------|
| **Номер** | Phase 6 |
| **Название** | KanbanDB Wrapper |
| **Новых строк** | ~1,000 |
| **Сложность** | **CRITICAL** |
| **Зависит от** | Phase 2 (Adapter v2), Phase 3 (SessionDB) |
| **Разблокирует** | Production kanban functionality |

---

## Overview

Адаптация KanbanDB (`hermes_cli/kanban_db.py`, 8,723 строки, ~80 функций) для работы
с ArcadeDB вместо SQLite. Существующий модуль остаётся для SQLite fallback —
добавляется ArcadeDB-совместимый wrapper.

### Стратегия

Создать `ArcadedbKanbanDB` класс с тем же API что и модульные функции в
`kanban_db.py`. Добавить `--backend arcadedb` опцию в `kanban_db_path()`.

### Ключевые отличия

| SQLite | ArcadeDB | Примечание |
|--------|----------|------------|
| `write_txn()` context manager | `adapter.transact()` | Тот же паттерн, другой движок |
| CAS `UPDATE WHERE claim_lock IS NULL` | `UPDATE ... WHERE claim_lock IS NULL` | MVCC вместо WAL lock |
| `task_links` junction table | `DEPENDS_ON` / `BLOCKED_BY` edges | Граф вместо таблицы |
| `BEGIN IMMEDIATE` | MVCC optimistic locking | ConcurrentModificationException |
| `file lock` (fcntl.flock) | Не нужно | ArcadeDB server-side |
| Multi-board: отдельные .db файлы | `BELONGS_TO_BOARD` edges | Одна БД, разные board vertices |
| `ALTER TABLE ADD COLUMN` | `CREATE PROPERTY ... IF NOT EXISTS` | Schema evolution |
| `PRAGMA integrity_check` | `SELECT count(*) FROM Task` | Health check |

---

## Files

### Новые файлы

| Файл | Строки | Назначение |
|------|--------|------------|
| `hermes_cli/arcadedb_kanban.py` | ~1,000 | `ArcadedbKanbanDB` класс |

### Модифицируемые файлы

| Файл | Изменение | Связь |
|------|-----------|-------|
| `hermes_cli/kanban_db.py` | Добавить `ArcadedbKanbanDB` path в `kanban_db_path()` | [см. kanban_db.py](../../hermes_cli/kanban_db.py) |
| `hermes_cli/kanban.py` | Добавить поддержку ArcadeDB backend для dispatch daemon | [см. kanban.py](../../hermes_cli/kanban.py) |
| `hermes_cli/arcadedb_schema.py` | Добавить индексы для Kanban (если не хватает) | [см. arcadedb_schema.py](../../hermes_cli/arcadedb_schema.py) |

---

## API Specification

```python
# hermes_cli/arcadedb_kanban.py (~1,000 строк)

class ArcadedbKanbanDB:
    """
    ArcadeDB-backed Kanban board. Совместим с kanban_db.py модулем.

    Реализует подмножество из ~80 функций kanban_db.py,
    сфокусированное на CRUD + CAS claim + dispatch.
    """

    def __init__(self, adapter: ArcadeDBAdapter, board_slug: str = "default"):
        """
        Args:
            adapter: ArcadeDBAdapter (Phase 2).
            board_slug: идентификатор доски (замена отдельным .db файлам).
        """

    def close(self) -> None: ...

    # ---- Board Management ----
    def board_exists(self, slug: str) -> bool: ...
    def get_current_board(self) -> str: ...
    def set_current_board(self, slug: str) -> None: ...

    # ---- Task CRUD ----
    def create_task(self, title: str, body: str = "", assignee: str = None,
                    priority: int = 0, tenant: str = "", **kwargs) -> str: ...
    def get_task(self, task_id: str) -> dict | None: ...
    def list_tasks(self, status: str = None, assignee: str = None,
                   tenant: str = None, limit: int = 50) -> list[dict]: ...
    def update_task(self, task_id: str, **updates) -> bool: ...
    def delete_task(self, task_id: str) -> bool: ...

    # ---- Task Linking (DAG) ----
    def link_tasks(self, parent_id: str, child_id: str) -> bool: ...
    def unlink_tasks(self, parent_id: str, child_id: str) -> bool: ...
    def parent_ids(self, task_id: str) -> list[str]: ...
    def child_ids(self, task_id: str) -> list[str]: ...

    # ---- Claim & Dispatch (CAS) ← CRITICAL ----
    def claim_task(self, task_id: str, worker_profile: str,
                   ttl_seconds: int = 300) -> str | None: ...
    def release_stale_claims(self) -> int: ...
    def heartbeat_claim(self, task_id: str, run_id: str) -> bool: ...
    def complete_task(self, task_id: str, result: str = "") -> bool: ...
    def block_task(self, task_id: str, kind: str = "manual") -> bool: ...
    def unblock_task(self, task_id: str) -> bool: ...

    # ---- Runs ----
    def list_runs(self, task_id: str, limit: int = 20) -> list[dict]: ...
    def get_run(self, run_id: str) -> dict | None: ...

    # ---- Comments ----
    def add_comment(self, task_id: str, author: str, body: str) -> str: ...
    def list_comments(self, task_id: str) -> list[dict]: ...

    # ---- Events (audit log) ----
    def list_events(self, task_id: str, limit: int = 50) -> list[dict]: ...

    # ---- Notifications ----
    def add_notify_sub(self, task_id: str, subscriber: str) -> None: ...
    def list_notify_subs(self, task_id: str) -> list[dict]: ...
    def remove_notify_sub(self, task_id: str, subscriber: str) -> None: ...

    # ---- Stats ----
    def board_stats(self) -> dict: ...
    def known_assignees(self) -> list[str]: ...
```

---

## Файл `hermes_cli/arcadedb_kanban.py` (~1,000 строк)

### Структура

```
hermes_cli/arcadedb_kanban.py
│
├── [1-20]   Imports + ArcadeDBKanbanError
├── [22-85]  ArcadedbKanbanDB.__init__() + close()
│
├── [87-120]  Board management (3 метода)
├── [122-280] Task CRUD (5 методов)
│
├── [282-400] Task Linking (4 метода) ← DAG через edges
├── [402-600] Claim & Dispatch (7 методов) ← CRITICAL
├── [602-680] Runs (2 метода)
├── [682-750] Comments (2 метода)
├── [752-810] Events (1 метод)
├── [812-880] Notifications (3 метода)
│
├── [882-950] Stats & Assignees (2 метода)
│
└── [952-1000] Private helpers (_ensure_board, _task_to_dict, _cycle_check)
```

### CAS Claim Pattern (CRITICAL)

```python
def claim_task(self, task_id: str, worker_profile: str,
               ttl_seconds: int = 300) -> str | None:
    """
    Atomic CAS claim на задачу.

    SQLite: UPDATE ... WHERE id=? AND status='ready' AND claim_lock IS NULL
    ArcadeDB: тот же UPDATE через MVCC.

    Returns:
        run_id (str) если claim успешен, None если задача уже занята.

    Raises:
        ArcadeDBKanbanError если задача не существует или не в статусе 'ready'.
    """
    now_ts = time.time()
    run_id = f"run_{task_id}_{int(now_ts)}"

    def _do(cur):
        # 1. Проверяем что задача существует и в статусе 'ready'
        cur.execute(
            "SELECT status, claim_lock FROM Task WHERE @rid = %s",
            (task_id,)
        )
        task = cur.fetchone()
        if not task:
            raise ArcadeDBKanbanError(f"Task {task_id} not found")
        if task["status"] != "ready":
            raise ArcadeDBKanbanError(
                f"Task {task_id} is '{task['status']}', expected 'ready'"
            )
        if task["claim_lock"] is not None:
            return None  # уже занята

        # 2. Атомарный CAS: обновить только если claim_lock IS NULL
        cur.execute(
            "UPDATE Task SET "
            "status = 'running', "
            "claim_lock = %(lock)s, "
            "claim_expires = %(exp)s, "
            "started_at = COALESCE(started_at, %(now)s) "
            "WHERE @rid = %(rid)s AND claim_lock IS NULL",
            {
                "lock": run_id,
                "exp": now_ts + ttl_seconds,
                "now": now_ts,
                "rid": task_id,
            }
        )

        if cur.rowcount == 0:
            # Потеряли гонку — другой worker уже захватил
            return None

        # 3. Создать TaskRun
        cur.execute(
            "CREATE VERTEX TaskRun SET "
            "profile = %(profile)s, "
            "status = 'running', "
            "started_at = %(now)s, "
            "claim_lock = %(lock)s, "
            "claim_expires = %(exp)s",
            {
                "profile": worker_profile,
                "now": now_ts,
                "lock": run_id,
                "exp": now_ts + ttl_seconds,
            }
        )

        # Получить @rid созданного TaskRun
        cur.execute(
            "SELECT @rid FROM TaskRun WHERE claim_lock = %s ORDER BY @rid DESC LIMIT 1",
            (run_id,)
        )
        run_vertex = cur.fetchone()

        # 4. Создать HAS_RUN edge
        cur.execute(
            "CREATE EDGE HAS_RUN FROM "
            "(SELECT FROM Task WHERE @rid = %(tid)s) TO "
            "(SELECT FROM TaskRun WHERE @rid = %(rid)s) "
            "SET status = 'running', started_at = %(now)s",
            {
                "tid": task_id,
                "rid": run_vertex["@rid"],
                "now": now_ts,
            }
        )

        # 5. Обновить Task.current_run_id
        cur.execute(
            "UPDATE Task SET current_run_id = %s WHERE @rid = %s",
            (run_vertex["@rid"], task_id)
        )

        # 6. Создать Event
        cur.execute(
            "CREATE VERTEX TaskEvent SET "
            "kind = 'claimed', "
            "payload = %s, "
            "created_at = %s",
            (json.dumps({"profile": worker_profile, "run_id": run_id}), now_ts)
        )

        cur.execute(
            "CREATE EDGE HAS_EVENT FROM "
            "(SELECT FROM Task WHERE @rid = %(tid)s) TO "
            "(SELECT FROM TaskEvent WHERE payload = %(p)s AND created_at = %(ts)s LIMIT 1) "
            "SET type = 'claimed', created_at = %(ts)s",
            {
                "tid": task_id,
                "p": json.dumps({"profile": worker_profile, "run_id": run_id}),
                "ts": now_ts,
            }
        )

        return run_id

    return self._adapter.transact(_do)
```

### Task Linking через Edges

```python
def link_tasks(self, parent_id: str, child_id: str) -> bool:
    """
    Создаёт DEPENDS_ON edge между задачами.

    Вместо INSERT INTO task_links(parent_id, child_id).
    Проверяет циклы через BFS по DEPENDS_ON edges.
    """
    # Проверка циклов
    if self._would_cycle(parent_id, child_id):
        raise ArcadeDBKanbanError(
            f"Adding {parent_id} → {child_id} would create a cycle"
        )

    def _do(cur):
        cur.execute(
            "CREATE EDGE DEPENDS_ON FROM "
            "(SELECT FROM Task WHERE @rid = %(parent)s) TO "
            "(SELECT FROM Task WHERE @rid = %(child)s)",
            {"parent": parent_id, "child": child_id}
        )
        return True

    try:
        return self._adapter.transact(_do)
    except ArcadeDBError:
        return False  # edge уже существует

def parent_ids(self, task_id: str) -> list[str]:
    """Возвращает всех родителей задачи через DEPENDS_ON edges."""
    rows = self._adapter.query(
        "SELECT expand(in('DEPENDS_ON')) FROM Task WHERE @rid = %s",
        (task_id,)
    )
    return [r["@rid"] for r in rows]

def child_ids(self, task_id: str) -> list[str]:
    """Возвращает всех детей задачи через DEPENDS_ON edges."""
    rows = self._adapter.query(
        "SELECT expand(out('DEPENDS_ON')) FROM Task WHERE @rid = %s",
        (task_id,)
    )
    return [r["@rid"] for r in rows]

def _would_cycle(self, new_parent: str, start_child: str) -> bool:
    """
    Проверяет, создаст ли добавление new_parent → start_child цикл.

    BFS вверх от new_parent: если встречаем start_child → цикл.
    В отличие от SQLite variant, использует edge traversal вместо recursive CTE.
    """
    visited = {start_child}
    queue = [new_parent]

    while queue:
        current = queue.pop(0)
        if current in visited:
            return True
        visited.add(current)

        # Получаем всех родителей через DEPENDS_ON edges
        parents = self.parent_ids(current)
        queue.extend(parents)

    return False
```

### Stale Claim Release

```python
def release_stale_claims(self) -> int:
    """Освобождает задачи с истёкшими claims."""
    now_ts = time.time()

    def _do(cur):
        cur.execute(
            "UPDATE Task SET "
            "status = 'ready', "
            "claim_lock = NULL, "
            "claim_expires = NULL, "
            "current_run_id = NULL "
            "WHERE status = 'running' AND claim_expires < %s",
            (now_ts,)
        )
        return cur.rowcount

    return self._adapter.transact(_do)
```

---

## Интеграция с существующим `kanban_db.py`

```python
# В hermes_cli/kanban_db.py (добавить в kanban_db_path() и сопутствующие функции)

def kanban_db_path(board: str = None) -> Path:
    """Возвращает путь к kanban БД. Поддерживает SQLite и ArcadeDB."""
    # ... существующая логика для SQLite ...

    # Если ARCADE_KANBAN_ENABLED — возвращаем ArcadeDB-путь
    if os.environ.get("ARCADE_KANBAN_ENABLED"):
        return Path("arcadedb://") / (board or "default")

    # ... существующая SQLite логика ...

def connect(db_path: Path = None):
    """Подключается к kanban БД. Авто-определяет SQLite или ArcadeDB."""
    path = db_path or kanban_db_path()

    if str(path).startswith("arcadedb://"):
        from hermes_cli.config import load_config
        from hermes_cli.arcadedb import ArcadeDBConfig, ArcadeDBAdapter
        from hermes_cli.arcadedb_kanban import ArcadedbKanbanDB

        config = load_config()
        cfg = config["database"]["arcadedb"]

        adapter = ArcadeDBAdapter(ArcadeDBConfig(
            host=cfg["host"], port=cfg["port"],
            database=cfg["database"],
            user=cfg["user"], password=cfg["password"],
        ))
        adapter.connect()
        return ArcadedbKanbanDB(adapter, board_slug=str(path).split("/")[-1])

    # ... существующая SQLite логика ...
```

---

## Тест-кейсы

**Тестовый файл:** `tests/test_arcadedb_kanban.py` → [см. Phase 1: kanban tests](phase-1-testing.md#files-8-14)

| ID | Тест | Описание |
|----|------|----------|
| K6-01 | `test_create_task` | create_task() → task в БД |
| K6-02 | `test_get_task` | get_task() → правильные поля |
| K6-03 | `test_list_tasks_by_status` | list_tasks(status='ready') |
| K6-04 | `test_link_tasks` | link_tasks() → DEPENDS_ON edge |
| K6-05 | `test_cycle_detection` | _would_cycle() → True для цикла |
| K6-06 | `test_unlink_tasks` | unlink_tasks() → edge удалён |
| K6-07 | `test_claim_task_success` | CAS claim → run_id returned |
| K6-08 | `test_claim_task_conflict` | Два concurrent claim → один проигрывает |
| K6-09 | `test_release_stale_claims` | Истёкшие claims → released |
| K6-10 | `test_heartbeat_claim` | heartbeat → продлевает claim_expires |
| K6-11 | `test_complete_task` | complete_task() → status='done' |
| K6-12 | `test_block_unblock` | block → unblock → ready |
| K6-13 | `test_task_runs` | HAS_RUN edges создаются |
| K6-14 | `test_comments` | add_comment → HAS_COMMENT edge |
| K6-15 | `test_board_isolation` | Две доски → изолированные задачи |

---

## Acceptance Criteria

- [ ] `ArcadedbKanbanDB` реализует CRUD + CAS + DAG linking
- [ ] CAS claim атомарный (MVCC): два concurrent claim → один успешен
- [ ] `DEPENDS_ON` edges заменяют `task_links` таблицу
- [ ] Cycle detection работает через edge traversal
- [ ] Stale claims освобождаются корректно
- [ ] Multi-board изоляция через `BELONGS_TO_BOARD` edges
- [ ] Интеграция с `kanban_db_path()` — прозрачное переключение
- [ ] Все 15 тестов проходят

---

## Cross-References

### Предшествующие фазы
- **[← Phase 2: Adapter v2](phase-2-adapter-v2.md)** — `ArcadeDBAdapter` для всех запросов
- **[← Phase 3: SessionDB](phase-3-sessiondb.md)** — общие helpers (`_now()`, `_maybe_epoch()`)
- **[← Phase 5: Migration Tool](phase-5-migration-tool.md)** — `migrate_kanban()`

### Связи с существующими файлами
- **[`hermes_cli/kanban_db.py`](../../hermes_cli/kanban_db.py)** ← **добавляется ArcadeDB path** (8,723 строки — не модифицируется ядро)
- **[`hermes_cli/kanban.py`](../../hermes_cli/kanban.py)** — dispatch daemon адаптируется
- **[`hermes_cli/arcadedb_schema.py`](../../hermes_cli/arcadedb_schema.py)** — существующая схема (Task, TaskRun, edges)
- **[`hermes_cli/arcadedb.py:ArcadeDBAdapter`](../../hermes_cli/arcadedb.py)** — adapter (Phase 2)
- **[`plugins/kanban/`](../../plugins/kanban/)** — kanban plugin (dashboard, systemd)
- **[`hermes_cli/arcadedb_helpers.py`](../../hermes_cli/arcadedb_helpers.py)** — shared helpers (Phase 3)

### Связи внутри документации
- **[Phase 1: test_arcadedb_kanban.py](phase-1-testing.md#files-8-14)** — тесты
- **[Phase 2: transact() API](phase-2-adapter-v2.md#transaction-api)** — атомарность CAS
