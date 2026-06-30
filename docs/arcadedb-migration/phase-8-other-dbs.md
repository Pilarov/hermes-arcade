# Phase 8: Other Databases — Projects, Response Store, Verification, RetainDB

| Поле | Значение |
|------|----------|
| **Номер** | Phase 8 |
| **Название** | Projects DB, Response Store, Verification Evidence, RetainDB Queue |
| **Новых строк** | ~400 (суммарно) |
| **Сложность** | Low-Medium |
| **Зависит от** | Phase 2 (Adapter v2), Phase 3 (SessionDB helpers) |
| **Разблокирует** | — (независим от других фаз) |

---

## Overview

Миграция 4 оставшихся SQLite баз данных в ArcadeDB. Каждая БД — отдельный адаптер
с минимальным API, использующий общий `ArcadeDBAdapter` (Phase 2) и helpers (Phase 3).

---

## 8.1 Projects DB

**Исходный файл:** [`hermes_cli/projects_db.py`](../../hermes_cli/projects_db.py) (~727 строк, модульные функции)
**Новый файл:** `hermes_cli/arcadedb_projects.py` (~100 строк)

### Замена

| SQLite | ArcadeDB |
|--------|----------|
| `projects` table | `Project` vertices (уже в схеме) |
| `project_folders` table | `HAS_FOLDER` edges |
| `project_meta` (key-value) | `StateMeta` vertices (key=`active_project`) |
| `discovered_repos` table | `DiscoveredRepo` vertices |

### API

```python
# hermes_cli/arcadedb_projects.py

class ArcadedbProjectsDB:
    def __init__(self, adapter: ArcadeDBAdapter): ...

    def create_project(self, name: str, slug: str = None,
                       folders: list[dict] = None, **kwargs) -> str: ...
    def list_projects(self, include_archived: bool = False) -> list[dict]: ...
    def get_project(self, id_or_slug: str) -> dict | None: ...
    def update_project(self, project_id: str, **updates) -> None: ...
    def add_folder(self, project_id: str, path: str, label: str = "",
                   is_primary: bool = False) -> None: ...
    def remove_folder(self, project_id: str, path: str) -> None: ...
    def set_primary(self, project_id: str, path: str) -> None: ...
    def archive_project(self, project_id: str) -> None: ...
    def restore_project(self, project_id: str) -> None: ...
    def delete_project(self, project_id: str) -> None: ...
    def set_active(self, project_id: str) -> None: ...
    def get_active_id(self) -> str | None: ...
    def project_for_path(self, path: str) -> dict | None: ...
    def record_discovered_repos(self, repos: list[dict]) -> None: ...
    def close(self) -> None: ...
```

### Implementation notes

- **Primary folder:** `HAS_FOLDER` edge с property `is_primary: bool`. При смене primary — flip флаг на старом edge.
- **Cascade delete:** `DELETE VERTEX Project WHERE @rid = %s` — ArcadeDB удаляет dangling edges.
- **Longest prefix match:** `project_for_path()` — SELECT с `WHERE path LIKE %s%` или Python-side filter.

---

## 8.2 Response Store

**Исходный файл:** [`gateway/platforms/api_server.py:ResponseStore`](../../gateway/platforms/api_server.py) (line ~372, ~170 строк)
**Новый код:** в том же файле, добавить `ArcadedbResponseStore` класс

### Замена

| SQLite | ArcadeDB |
|--------|----------|
| `responses` table | `Response` vertices |
| `conversations` table | `Conversation` vertices |

### API

```python
class ArcadedbResponseStore:
    def __init__(self, adapter: ArcadeDBAdapter, max_size: int = 1000): ...

    def get(self, response_id: str) -> dict | None: ...
    def put(self, response_id: str, data: dict) -> None:
        """
        INSERT Response vertex с JSON data.
        LRU eviction: при max_size — DELETE старейшие по accessed_at.
        """
    def delete(self, response_id: str) -> None: ...
    def get_conversation(self, name: str) -> str | None: ...
    def set_conversation(self, name: str, response_id: str) -> None: ...
    def close(self) -> None: ...
    def __len__(self) -> int: ...
```

### LRU Eviction logic

```python
def put(self, response_id: str, data: dict) -> None:
    def _do(cur):
        # Вставить
        cur.execute(
            "CREATE VERTEX Response SET "
            "response_id = %s, data = %s, accessed_at = %s",
            (response_id, json.dumps(data), time.time())
        )
        # Если превышен max_size — удалить старейшие
        cur.execute("SELECT count(*) FROM Response")
        count = cur.fetchone()["count(*)"]
        if count > self._max_size:
            excess = count - self._max_size
            cur.execute(
                "DELETE FROM Response "
                "WHERE @rid IN (SELECT @rid FROM Response ORDER BY accessed_at ASC LIMIT %s)",
                (excess,)
            )
    self._adapter.transact(_do)
```

---

## 8.3 Verification Evidence

**Исходный файл:** [`agent/verification_evidence.py`](../../agent/verification_evidence.py) (~618 строк, модульные функции)
**Новый код:** в том же файле, добавить `ArcadedbVerificationStore` класс

### Замена

| SQLite | ArcadeDB |
|--------|----------|
| `verification_events` table | `VerificationEvent` vertices |
| `verification_state` table | `VerificationState` vertices |
| `meta` table | `StateMeta` vertices |

### API

```python
class ArcadedbVerificationStore:
    def __init__(self, adapter: ArcadeDBAdapter): ...

    def record_terminal_result(self, command: str, cwd: str, session_id: str,
                               exit_code: int, output: str) -> None:
        """
        Append-only: CREATE VERTEX VerificationEvent + UPSERT VerificationState.
        """

    def mark_workspace_edited(self, session_id: str, cwd: str,
                              paths: list[str]) -> None:
        """UPSERT VerificationState с changed_paths."""

    def verification_status(self, session_id: str, cwd: str) -> str:
        """
        Возвращает: 'not_applicable', 'unverified', 'passed', 'failed', 'stale'.
        """

    def _prune_old_events(self) -> None:
        """Трёхуровневый pruning (аналогичен SQLite версии)."""

    def close(self) -> None: ...
```

---

## 8.4 RetainDB Queue

**Исходный файл:** [`plugins/memory/retaindb/__init__.py`](../../plugins/memory/retaindb/__init__.py) (~80 строк, class on line 330)
**Новый код:** в том же файле, добавить ArcadeDB-вариант `_WriteQueue`

### Замена

| SQLite | ArcadeDB |
|--------|----------|
| `pending` table | `PendingIngest` vertices |

### API

```python
class ArcadedbWriteQueue:
    """
    ArcadeDB-backed durability queue.
    Заменяет SQLite `pending` table.
    """

    def __init__(self, client, adapter: ArcadeDBAdapter): ...

    def enqueue(self, user_id: str, session_id: str,
                messages: list[dict]) -> None:
        """INSERT PendingIngest vertex."""

    def _pending_rows(self) -> list[dict]:
        """Crash recovery: читает все pending строки."""

    def _flush_row(self, row: dict) -> bool:
        """
        POST to RetainDB API.
        On success → DELETE PendingIngest vertex.
        On failure → UPDATE last_error.
        """

    def _loop(self) -> None:
        """Daemon thread: queue.Queue consumer."""

    def shutdown(self) -> None: ...
```

---

## Тест-кейсы

### Projects (`tests/test_arcadedb_projects.py` → [см. Phase 1](phase-1-testing.md#files-8-14))

| ID | Тест | Описание |
|----|------|----------|
| PRJ-01 | `test_create_project` | create_project() с folders |
| PRJ-02 | `test_list_projects` | list_projects() с фильтром |
| PRJ-03 | `test_add_remove_folder` | add/remove folder edges |
| PRJ-04 | `test_set_primary_folder` | Смена primary folder |
| PRJ-05 | `test_archive_restore` | Archive → restore |
| PRJ-06 | `test_delete_cascade` | Delete project → cascade folders |
| PRJ-07 | `test_set_active` | set_active / get_active_id |
| PRJ-08 | `test_project_for_path` | Longest prefix match |

---

## Acceptance Criteria

### Projects
- [ ] `ArcadedbProjectsDB` реализует все ~18 методов
- [ ] Folders через `HAS_FOLDER` edges с `is_primary` property
- [ ] Primary folder switching атомарно
- [ ] Cascade delete работает (`DELETE VERTEX` → edges удалены)
- [ ] Все 8 тестов проходят

### Response Store
- [ ] LRU eviction работает (при превышении max_size)
- [ ] Conversation lookup работает
- [ ] `__len__()` возвращает правильное количество
- [ ] Permission tightening (0o600) → не нужно для ArcadeDB

### Verification Evidence
- [ ] Event logging работает (append-only)
- [ ] `verification_status()` возвращает правильный статус
- [ ] `_prune_old_events()` трёхуровневый

### RetainDB Queue
- [ ] Write-behind queue работает
- [ ] Crash recovery (replay pending при старте) работает

---

## Cross-References

### Предшествующие фазы
- **[← Phase 2: Adapter v2](phase-2-adapter-v2.md)** — `ArcadeDBAdapter` для всех БД
- **[← Phase 3: SessionDB](phase-3-sessiondb.md)** — общие helpers (`_now()`, `StateMeta`)
- **[← Phase 5: Migration Tool](phase-5-migration-tool.md)** — `migrate_projects()`

### Связи с существующими файлами

#### Projects
- **[`hermes_cli/projects_db.py`](../../hermes_cli/projects_db.py)** — reference API (727 строк)
- **[`hermes_cli/arcadedb_schema.py`](../../hermes_cli/arcadedb_schema.py)** — `Project` vertex type уже определён

#### Response Store
- **[`gateway/platforms/api_server.py`](../../gateway/platforms/api_server.py)** — `ResponseStore` класс (line 372)

#### Verification
- **[`agent/verification_evidence.py`](../../agent/verification_evidence.py)** — reference API (618 строк)

#### RetainDB
- **[`plugins/memory/retaindb/__init__.py`](../../plugins/memory/retaindb/__init__.py)** — `_WriteQueue` класс (line 330)

### Связи внутри документации
- **[Phase 1: test_arcadedb_projects.py](phase-1-testing.md#files-8-14)** — тесты projects

---

## Implementation Sequence

### Projects (~100 строк, 30 мин)
```
1. Создать hermes_cli/arcadedb_projects.py
2. Реализовать CRUD через Project vertices
3. Реализовать HAS_FOLDER edges с is_primary
4. Реализовать cascade delete + longest prefix match
5. Сделать тесты зелёными (PRJ-01 → PRJ-08)
```

### Response Store (~100 строк, 20 мин)
```
1. Добавить ArcadedbResponseStore в gateway/platforms/api_server.py
2. LRU eviction через application layer (DELETE oldest)
```

### Verification Evidence (~100 строк, 20 мин)
```
1. Добавить ArcadedbVerificationStore в agent/verification_evidence.py
2. Append-only event logging
3. Трёхуровневый pruning
```

### RetainDB Queue (~100 строк, 20 мин, опционально)
```
1. Добавить ArcadeDB-вариант в plugins/memory/retaindb/__init__.py
2. Write-behind queue с crash recovery
```
