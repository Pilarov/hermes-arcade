# Phase 5: Data Migration Tool — SQLite → ArcadeDB

| Поле | Значение |
|------|----------|
| **Номер** | Phase 5 |
| **Название** | Data Migration Tool |
| **Новых строк** | ~600 |
| **Сложность** | Medium |
| **Зависит от** | Phase 3 (ArcadedbSessionDB), Phase 4 (Factory) |
| **Разблокирует** | Production deployment |

---

## Overview

Инструмент миграции state.db (и других БД) из SQLite в ArcadeDB с поддержкой:

- **Авто-миграция**: при первом старте с `database.arcadedb.enabled=true` и наличии `state.db`
- **Ручная команда**: `hermes migrate --arcadedb` с опциями
- **Dry-run**: `hermes migrate --arcadedb --dry-run`
- **Частичная миграция**: `--state-only`, `--kanban-only`, `--memory-only`
- **Верификация**: сверка количества записей и checksum
- **Обратимость**: старый `state.db` архивируется (не удаляется)

---

## Files

### Новые файлы

| Файл | Строки | Назначение |
|------|--------|------------|
| `hermes_cli/migrate_to_arcadedb.py` | ~600 | Основной модуль миграции |

### Модифицируемые файлы

| Файл | Изменение | Связь |
|------|-----------|-------|
| `cli.py` | Добавить команду `hermes migrate --arcadedb` | [см. cli.py](../../cli.py) |
| `hermes_cli/main.py` | Добавить subcommand в argparse | [см. main.py](../../hermes_cli/main.py) |
| `hermes_state.py` | Factory определяет наличие state.db и предлагает миграцию | [см. Phase 4: factory](phase-4-consumers.md) |

---

## API Specification

```python
# hermes_cli/migrate_to_arcadedb.py (~600 строк)

from dataclasses import dataclass, field
from typing import Optional

@dataclass
class MigrationReport:
    """Результат миграции."""
    source_db: str               # 'state.db', 'kanban.db', 'memory_store.db'
    target: str                  # 'ArcadeDB'
    total_rows: int              # всего строк в источнике
    migrated_rows: int           # успешно мигрировано
    skipped_rows: int            # уже существовали (идемпотентность)
    failed_rows: int             # ошибки миграции
    duration_seconds: float      # время выполнения
    errors: list[str] = field(default_factory=list)

class ArcadeDBMigrator:
    """SQLite → ArcadeDB migration engine."""

    def __init__(self, sqlite_path: str, adapter: ArcadeDBAdapter):
        """
        Args:
            sqlite_path: путь к SQLite БД.
            adapter: ArcadeDBAdapter для записи.
        """

    # ---- Migration ----

    def migrate_state(self, compute_embeddings: bool = False) -> MigrationReport:
        """
        Миграция state.db → ArcadeDB.

        Переносит:
          sessions → Session vertices
          messages → Message vertices + HAS_MESSAGE edges
          compression_locks → CompressionLock vertices
          state_meta → StateMeta vertices
          telegram_topic_mode → TelegramTopicMode vertices
          telegram_topic_bindings → TelegramTopicBinding vertices
          parent_session_id → HAS_CHILD_SESSION edges

        Args:
            compute_embeddings: если True — вычислять эмбеддинги через fastembed.

        Returns:
            MigrationReport с результатами.
        """

    def migrate_kanban(self) -> MigrationReport:
        """
        Миграция kanban.db → ArcadeDB.

        Переносит:
          tasks → Task vertices
          task_runs → TaskRun vertices + HAS_RUN edges
          task_links → DEPENDS_ON edges
          task_comments → TaskComment vertices + HAS_COMMENT edges
          task_events → TaskEvent vertices + HAS_EVENT edges
          task_attachments → TaskAttachment vertices + HAS_ATTACHMENT edges
          kanban_notify_subs → KanbanNotifySub vertices

        Returns:
            MigrationReport.
        """

    def migrate_memory(self, compute_embeddings: bool = False) -> MigrationReport:
        """
        Миграция memory_store.db → ArcadeDB.

        Переносит:
          entities → Entity vertices
          facts → Fact vertices
          fact_entities → HAS_FACT edges
          memory_banks → не переносятся (redundant)

        Args:
            compute_embeddings: вычислять эмбеддинги для фактов.

        Returns:
            MigrationReport.
        """

    def migrate_projects(self) -> MigrationReport:
        """
        Миграция projects.db → ArcadeDB.

        Переносит:
          projects → Project vertices
          project_folders → HAS_FOLDER edges
          project_meta → StateMeta vertices (key='active_project')
          discovered_repos → DiscoveredRepo vertices
        """

    def migrate_all(self, compute_embeddings: bool = False) -> list[MigrationReport]:
        """
        Миграция всех доступных БД.
        Порядок: state → kanban → memory → projects.
        """

    # ---- Verification ----

    def verify(self, source_db: str) -> bool:
        """
        Верификация миграции.

        Сравнивает:
          - Количество записей (SQLite vs ArcadeDB)
          - Количество edges (task_links vs DEPENDS_ON)
          - Выборочные checksums (content hash)

        Returns:
            True если всё совпадает.
        """

    # ---- Dry Run ----

    def dry_run(self, source_db: str = "all") -> dict:
        """
        Предварительный просмотр миграции.

        Returns:
            {
                "source_db": str,
                "tables": {table_name: {"row_count": int, "sample_rows": list[dict]}},
                "estimated_duration_seconds": float,
                "disk_space_required_mb": float,
            }
        """

    # ---- Rollback ----

    def rollback(self, source_db: str) -> None:
        """
        Удаляет мигрированные данные из ArcadeDB.
        Используется после неудачной миграции.

        Удаляет все вершины и edges с source_tag='migrated'.
        """
```

---

## Файл `hermes_cli/migrate_to_arcadedb.py` (~600 строк)

### Структура

```
hermes_cli/migrate_to_arcadedb.py
│
├── [1-20]   Imports: sqlite3, json, logging, dataclasses
├── [22-50]  MigrationReport dataclass
├── [52-100] ArcadeDBMigrator.__init__() + helpers
│
├── [102-250] migrate_state() ← CRITICAL
├── [252-380] migrate_kanban()
├── [382-480] migrate_memory()
├── [482-530] migrate_projects()
├── [532-560] migrate_all()
│
├── [562-620] verify() + _verify_table()
├── [622-660] dry_run()
├── [662-700] rollback()
│
└── [702-750] CLI integration: run_migration(args)
```

### Ключевая логика миграции

```python
def migrate_state(self, compute_embeddings: bool = False) -> MigrationReport:
    """
    Миграция state.db → ArcadeDB. Идемпотентная.
    """
    import sqlite3

    report = MigrationReport(source_db="state.db", target="ArcadeDB")
    start = time.time()

    conn = sqlite3.connect(self._sqlite_path)
    conn.row_factory = sqlite3.Row

    # ---- Session vertices ----
    sessions = conn.execute("SELECT * FROM sessions").fetchall()
    report.total_rows += len(sessions)

    for row in sessions:
        try:
            props = {k: row[k] for k in row.keys() if row[k] is not None}
            # Проверяем идемпотентность
            existing = self._adapter.query(
                "SELECT FROM Session WHERE id = %s LIMIT 1",
                {"id": row["id"]}
            )
            if existing:
                report.skipped_rows += 1
                continue

            props["started_at"] = _maybe_epoch(props.get("started_at"))
            props["ended_at"] = _maybe_epoch(props.get("ended_at"))

            # Build INSERT with properties
            cols = ", ".join(f"`{k}` = %({k})s" for k in props)
            self._adapter.execute(
                f"INSERT INTO Session SET {cols}",
                props
            )
            report.migrated_rows += 1
        except Exception as e:
            report.failed_rows += 1
            report.errors.append(f"session {row.get('id')}: {e}")

    # ---- Message vertices + HAS_MESSAGE edges ----
    messages = conn.execute(
        "SELECT * FROM messages WHERE active = 1 ORDER BY id"
    ).fetchall()
    report.total_rows += len(messages)

    for row in messages:
        try:
            props = {k: row[k] for k in row.keys() if row[k] is not None}
            props["timestamp"] = _maybe_epoch(props.get("timestamp"))

            # Вычисляем embedding (опционально)
            if compute_embeddings and row["content"]:
                emb = self._embedder.embed([row["content"]])[0]
                embedding_sql = f", embedding = {ArcadeDBAdapter._vec(emb.dense)}"
            else:
                embedding_sql = ""

            cols = ", ".join(f"`{k}` = %({k})s" for k in props)
            sql = f"CREATE VERTEX Message SET {cols}{embedding_sql}"
            self._adapter.execute(sql, props)

            # HAS_MESSAGE edge
            self._adapter.execute(
                "CREATE EDGE HAS_MESSAGE FROM "
                "(SELECT FROM Session WHERE id = %(sid)s) TO "
                "(SELECT FROM Message WHERE session_id = %(sid)s AND timestamp = %(ts)s LIMIT 1) "
                "SET seq = 0, role = %(role)s, tokens = %(tokens)s, created_at = %(ts)s",
                {
                    "sid": row["session_id"],
                    "role": row.get("role", "user"),
                    "tokens": len(row["content"].split()) if row["content"] else 0,
                    "ts": props["timestamp"],
                }
            )
            report.migrated_rows += 1
        except Exception as e:
            report.failed_rows += 1
            report.errors.append(f"message {row.get('id')}: {e}")

    # ---- Parent session edges ----
    for row in sessions:
        if row.get("parent_session_id"):
            try:
                self._adapter.execute(
                    "CREATE EDGE HAS_CHILD_SESSION FROM "
                    "(SELECT FROM Session WHERE id = %s) TO "
                    "(SELECT FROM Session WHERE id = %s) "
                    "SET end_reason = %s, created_at = %s",
                    (row["parent_session_id"], row["id"],
                     row.get("end_reason", ""),
                     _maybe_epoch(row.get("started_at")))
                )
            except ArcadeDBError:
                pass  # Edge может уже существовать

    # ---- Compression locks ----
    locks = conn.execute("SELECT * FROM compression_locks").fetchall()
    for row in locks:
        try:
            self._adapter.execute(
                "INSERT INTO CompressionLock SET "
                "session_id = %s, holder = %s, "
                "acquired_at = %s, expires_at = %s",
                (row["session_id"], row["holder"],
                 row["acquired_at"], row["expires_at"])
            )
        except ArcadeDBError:
            pass

    # ---- StateMeta ----
    meta_rows = conn.execute("SELECT * FROM state_meta").fetchall()
    for row in meta_rows:
        try:
            self._adapter.execute(
                "INSERT INTO StateMeta SET key = %s, value = %s",
                (row["key"], row["value"])
            )
        except ArcadeDBError:
            pass

    conn.close()

    report.duration_seconds = time.time() - start
    return report
```

---

## CLI Integration

### `cli.py` — добавление команды

```python
# hermes migrate --arcadedb [опции]

# В process_command() или отдельном обработчике:
if canonical == "migrate":
    args = shlex.split(cmd_original)[1:]
    if "--arcadedb" in args or "-a" in args:
        _handle_arcadedb_migration(args)
```

### `hermes_cli/migrate_to_arcadedb.py` — CLI entry

```python
def run_migration(args: list[str]) -> None:
    """
    Entry point for `hermes migrate --arcadedb`.

    Options:
        --arcadedb, -a       Target ArcadeDB
        --dry-run             Preview only
        --state-only          Only state.db
        --kanban-only         Only kanban.db
        --memory-only         Only memory_store.db
        --all                 All databases (default)
        --embed               Compute embeddings
        --verify              Verify after migration
        --rollback            Rollback last migration
    """
    import argparse
    from hermes_cli.config import load_config
    from hermes_cli.arcadedb import ArcadeDBConfig, ArcadeDBAdapter
    from hermes_constants import get_hermes_home

    parser = argparse.ArgumentParser(description="Migrate SQLite → ArcadeDB")
    parser.add_argument("--arcadedb", "-a", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--state-only", action="store_true")
    parser.add_argument("--kanban-only", action="store_true")
    parser.add_argument("--memory-only", action="store_true")
    parser.add_argument("--all", action="store_true", default=True)
    parser.add_argument("--embed", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--rollback", action="store_true")

    opts = parser.parse_args(args)

    config = load_config()
    hermes_home = get_hermes_home()

    # ... инициализация adapter + migrator ...
    # ... запуск миграции ...
    # ... вывод результата ...
```

### Auto-detect при старте (в `create_session_db()`)

```python
# В hermes_state.py:create_session_db()

def create_session_db(...):
    # ... проверка database.arcadedb.enabled ...

    # Проверяем наличие state.db и предлагаем миграцию
    state_db_path = get_hermes_home() / "state.db"
    if state_db_path.exists():
        # Проверяем, не мигрирован ли уже
        adapter = ArcadeDBAdapter(db_config)
        adapter.connect()
        session_count = adapter.query("SELECT count(*) FROM Session")[0]["count(*)"]

        if session_count == 0:
            print()
            print(f"  [ArcadeDB] SQLite state.db detected ({state_db_path.stat().st_size:,} bytes)")
            print(f"  [ArcadeDB] Migrate to ArcadeDB? (y/N): ", end="")
            answer = input().strip().lower()
            if answer == "y":
                from hermes_cli.migrate_to_arcadedb import ArcadeDBMigrator
                migrator = ArcadeDBMigrator(str(state_db_path), adapter)
                report = migrator.migrate_state(compute_embeddings=True)
                print(f"  [ArcadeDB] Migrated: {report.migrated_rows} rows, "
                      f"skipped: {report.skipped_rows}, failed: {report.failed_rows}")
                print(f"  [ArcadeDB] Duration: {report.duration_seconds:.1f}s")
                # Архивируем старый state.db
                backup_path = state_db_path.with_suffix(".db.bak")
                state_db_path.rename(backup_path)
                print(f"  [ArcadeDB] Original state.db archived to {backup_path}")

    # ... остальная логика factory ...
```

---

## Тест-кейсы

**Тестовый файл:** `tests/test_arcadedb_migration.py` → [см. Phase 1: migration tests](phase-1-testing.md#files-8-14)

| ID | Тест | Описание |
|----|------|----------|
| M5-01 | `test_dry_run_state` | Dry-run показывает количество записей |
| M5-02 | `test_migrate_state_basic` | state.db → ArcadeDB успешно |
| M5-03 | `test_migrate_idempotent` | Повторная миграция — все skipped |
| M5-04 | `test_migrate_with_embeddings` | `--embed` вычисляет эмбеддинги |
| M5-05 | `test_verify_after_migration` | `verify()` возвращает True |
| M5-06 | `test_migrate_partial` | `--state-only` мигрирует только state |
| M5-07 | `test_migrate_kanban` | Kanban tasks → edges |
| M5-08 | `test_migrate_memory` | Memory facts → vertices |
| M5-09 | `test_rollback` | Rollback удаляет мигрированные данные |
| M5-10 | `test_auto_detect_prompt` | Factory предлагает миграцию при state.db |

### E2E тест

| ID | Тест | Описание |
|----|------|----------|
| MIG-E2E-01 | `test_full_migration_flow` | Auto-detect → migrate → verify → use |

---

## Acceptance Criteria

- [ ] `hermes migrate --arcadedb --dry-run` показывает preview
- [ ] `hermes migrate --arcadedb` мигрирует state.db
- [ ] Идемпотентная миграция (повторный запуск — все skipped)
- [ ] `--embed` вычисляет эмбеддинги
- [ ] Верификация после миграции успешна
- [ ] Rollback удаляет мигрированные данные
- [ ] Auto-detect предлагает миграцию при первом старте
- [ ] Старый state.db архивируется (не удаляется)
- [ ] Все тесты миграции проходят

---

## Cross-References

### Предшествующие фазы
- **[← Phase 2: Adapter v2](phase-2-adapter-v2.md)** — `ArcadeDBAdapter` для записи
- **[← Phase 3: ArcadedbSessionDB](phase-3-sessiondb.md)** — `ArcadeDBAdapter._vec()` для векторов
- **[← Phase 4: Factory](phase-4-consumers.md)** — auto-detect в `create_session_db()`

### Последующие фазы
- **[→ Phase 6: KanbanDB](phase-6-kanbandb.md)** — `migrate_kanban()` использует kanban схему
- **[→ Phase 7: Memory Store](phase-7-memory-store.md)** — `migrate_memory()` использует memory схему
- **[→ Phase 8: Other DBs](phase-8-other-dbs.md)** — `migrate_projects()`

### Связи с существующими файлами
- **[`hermes_cli/arcadedb.py:ArcadeDBAdapter`](../../hermes_cli/arcadedb.py)** — adapter для записи (Phase 2)
- **[`hermes_cli/arcadedb_migrate.py`](../../hermes_cli/arcadedb_migrate.py)** — reference (существующий мигратор)
- **[`hermes_cli/embedder.py`](../../hermes_cli/embedder.py)** — fastembed для эмбеддингов
- **[`hermes_constants.py:get_hermes_home`](../../hermes_constants.py)** — пути к БД
- **[`cli.py`](../../cli.py)** — добавление команды migrate
- **[`hermes_state.py`](../../hermes_state.py)** — auto-detect в factory
