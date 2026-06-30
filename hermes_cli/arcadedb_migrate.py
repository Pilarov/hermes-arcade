"""Migrate Hermes SQLite databases → ArcadeDB graph.

Reads all three SQLite databases (state.db, kanban.db, memory_store.db)
and inserts their data into ArcadeDB using the existing graph schema.

Idempotent: skips records that already exist (matched by id).

Usage:
    python -m hermes_cli.arcadedb_migrate
    python -m hermes_cli.arcadedb_migrate --embed          # also compute embeddings
    python -m hermes_cli.arcadedb_migrate --state-only     # only state.db
    python -m hermes_cli.arcadedb_migrate --kanban-only    # only kanban.db
    python -m hermes_cli.arcadedb_migrate --memory-only    # only memory_store.db
"""

from __future__ import annotations

import argparse
import calendar
import datetime
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from hermes_cli.arcadedb import ArcadeDBAdapter, ArcadeDBConfig
from hermes_cli.arcadedb_schema import SchemaManager
from hermes_cli.embedder import create_embedder, EmbedderProvider

logger = logging.getLogger(__name__)


def _vec(val: List[float]) -> str:
    return json.dumps([float(x) for x in val], allow_nan=False)


_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _maybe_epoch(val: Any) -> Any:
    """Convert ISO datetime string → epoch float if it looks like one."""
    if isinstance(val, str) and _ISO_RE.match(val):
        try:
            dt = datetime.datetime.strptime(val, "%Y-%m-%d %H:%M:%S")
            return calendar.timegm(dt.timetuple()) + dt.microsecond / 1_000_000
        except ValueError:
            pass
    return val


# ---------------------------------------------------------------------------
# SQLite readers
# ---------------------------------------------------------------------------

class SQLiteReader:
    """Iterate over rows in a SQLite table, yielding dicts."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._conn: Optional[sqlite3.Connection] = None

    def open(self) -> None:
        if not self._path.exists():
            raise FileNotFoundError(f"SQLite DB not found: {self._path}")
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        if self._conn:
            self._conn.close()

    def rows(self, table: str, order: str = "rowid") -> Iterator[Dict[str, Any]]:
        assert self._conn is not None
        cur = self._conn.execute(f"SELECT * FROM [{table}] ORDER BY {order}")
        for row in cur.fetchall():
            yield dict(row)

    def count(self, table: str) -> int:
        assert self._conn is not None
        return self._conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]


# ---------------------------------------------------------------------------
# ArcadeDB writer
# ---------------------------------------------------------------------------

class ArcadeDBWriter:
    """Inserts records into ArcadeDB, skipping duplicates."""

    def __init__(self, db: ArcadeDBAdapter) -> None:
        self._db = db
        self._stats: Dict[str, int] = {"inserted": 0, "skipped": 0, "errors": 0}

    def insert_vertex(self, type_name: str, props: Dict[str, Any]) -> Optional[str]:
        """Insert a vertex, skip if id already exists. Returns @rid or None."""
        id_val = props.get("id")
        if id_val is not None:
            existing = self._db.query(
                f"SELECT @rid FROM {type_name} WHERE id = :id LIMIT 1",
                params={"id": id_val},
            )
            if existing:
                self._stats["skipped"] += 1
                return str(existing[0]["@rid"])

        cols = []
        params: Dict[str, Any] = {}
        vec_literals: List[str] = []

        for k, v in props.items():
            if k == "embedding" and v is not None:
                if isinstance(v, list):
                    vec_literals.append(f"`embedding` = {_vec(v)}")
                continue
            pn = k.replace(".", "_")
            cols.append(f"`{pn}` = :{pn}")
            params[pn] = _maybe_epoch(v)

        sql = f"INSERT INTO {type_name} SET {', '.join(cols + vec_literals)}"
        try:
            res = self._db.execute(sql, params=params)
            self._stats["inserted"] += 1
            rid = None
            if res:
                rid = str(res[0].get("@rid", ""))
            return rid
        except Exception as e:
            self._stats["errors"] += 1
            logger.warning("Insert into %s failed (id=%s): %s", type_name, id_val, e)
            return None

    def ensure_edge(
        self,
        edge_type: str,
        from_type: str,
        from_id: Any,
        to_type: str,
        to_id: Any,
        props: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Create an edge if it doesn't exist."""
        existing = self._db.query(
            f"SELECT @rid FROM {edge_type} WHERE "
            f"@out IN (SELECT FROM {from_type} WHERE id = :fid) AND "
            f"@in IN (SELECT FROM {to_type} WHERE id = :tid) LIMIT 1",
            params={"fid": from_id, "tid": to_id},
        )
        if existing:
            return True

        set_clause = ""
        params: Dict[str, Any] = {"fid": from_id, "tid": to_id}
        if props:
            parts = []
            for k, v in props.items():
                pn = k.replace(".", "_")
                parts.append(f"`{pn}` = :{pn}")
                params[pn] = _maybe_epoch(v)
            set_clause = " SET " + ", ".join(parts)

        sql = (
            f"CREATE EDGE {edge_type} FROM "
            f"(SELECT FROM {from_type} WHERE id = :fid) TO "
            f"(SELECT FROM {to_type} WHERE id = :tid){set_clause}"
        )
        try:
            self._db.execute(sql, params=params)
            return True
        except Exception as e:
            logger.warning("Edge %s %s->%s failed: %s", edge_type, from_id, to_id, e)
            return False

    def stats(self) -> Dict[str, int]:
        return dict(self._stats)


# ---------------------------------------------------------------------------
# Migrators
# ---------------------------------------------------------------------------

def migrate_state(
    reader: SQLiteReader,
    writer: ArcadeDBWriter,
    embedder: Optional[EmbedderProvider],
) -> None:
    """Migrate sessions + messages from state.db."""

    # ---- Sessions ----
    n_sessions = reader.count("sessions")
    logger.info("Migrating %d sessions …", n_sessions)
    for i, row in enumerate(reader.rows("sessions", "started_at")):
        writer.insert_vertex("Session", row)
        if i > 0 and i % 100 == 0:
            logger.info("  sessions: %d/%d", i, n_sessions)

    # ---- Messages ----
    n_msgs = reader.count("messages")
    logger.info("Migrating %d messages …", n_msgs)

    # index session IDs to make edge creation faster
    session_ids: set = set()
    for row in reader.rows("sessions", "rowid"):
        session_ids.add(row["id"])
    reader.rows.cache_clear() if hasattr(reader.rows, "cache_clear") else None

    for i, row in enumerate(reader.rows("messages", "id")):
        msg_id = row.pop("id", None)
        session_id = row.pop("session_id", None)
        row["id"] = msg_id  # store message id for dedup

        if embedder and row.get("content"):
            emb = embedder.embed([row["content"]])[0]
            row["embedding"] = emb.dense

        rid = writer.insert_vertex("Message", row)
        if rid and session_id in session_ids:
            writer.ensure_edge(
                "HAS_MESSAGE", "Session", session_id, "Message", msg_id,
                props={"seq": i, "role": row.get("role", ""), "tokens": len((row.get("content") or "").split())},
            )
        if i > 0 and i % 100 == 0:
            logger.info("  messages: %d/%d token_count=%d errors=%d",
                        i, n_msgs, writer.stats()["inserted"], writer.stats()["errors"])

    # ---- Session parent-child edges ----
    logger.info("Creating session hierarchy edges …")
    for row in reader.rows("sessions", "started_at"):
        pid = row.get("parent_session_id")
        if pid and pid in session_ids:
            writer.ensure_edge(
                "HAS_CHILD_SESSION",
                "Session", pid,
                "Session", row["id"],
            )

    logger.info("State DB migration done — %s", writer.stats())


def migrate_kanban(
    reader: SQLiteReader,
    writer: ArcadeDBWriter,
) -> None:
    """Migrate tasks + runs + links from kanban.db."""

    # ---- Tasks ----
    n_tasks = reader.count("tasks")
    logger.info("Migrating %d tasks …", n_tasks)
    for i, row in enumerate(reader.rows("tasks", "created_at")):
        writer.insert_vertex("Task", row)
        if i > 0 and i % 100 == 0:
            logger.info("  tasks: %d/%d", i, n_tasks)

    # ---- TaskRuns ----
    n_runs = reader.count("task_runs")
    logger.info("Migrating %d task runs …", n_runs)
    for i, row in enumerate(reader.rows("task_runs", "started_at")):
        task_id = row.pop("task_id", None)
        row["id"] = row.get("id")
        rid = writer.insert_vertex("TaskRun", row)
        if rid and task_id:
            writer.ensure_edge("HAS_RUN", "Task", task_id, "TaskRun", row.get("id"))
        if i > 0 and i % 100 == 0:
            logger.info("  task_runs: %d/%d", i, n_runs)

    # ---- TaskLinks (DEPENDS_ON / BLOCKED_BY) ----
    n_links = reader.count("task_links")
    logger.info("Migrating %d task links …", n_links)
    for row in reader.rows("task_links"):
        parent = row["parent_id"]
        child = row["child_id"]
        # Determine link type: if child is blocked, it's BLOCKED_BY
        writer.ensure_edge("DEPENDS_ON", "Task", child, "Task", parent)


def migrate_memory(
    reader: SQLiteReader,
    writer: ArcadeDBWriter,
    embedder: Optional[EmbedderProvider],
) -> None:
    """Migrate facts + entities from memory_store.db."""

    # ---- Entities ----
    n_entities = reader.count("entities")
    logger.info("Migrating %d entities …", n_entities)
    entity_id_map: Dict[int, str] = {}  # sqlite entity_id → entity name
    for row in reader.rows("entities", "entity_id"):
        name = row["name"]
        entity_id_map[row["entity_id"]] = name
        writer.insert_vertex("Entity", {"id": name, "name": name, "entity_type": row.get("entity_type", "unknown")})

    # ---- Facts ----
    n_facts = reader.count("facts")
    logger.info("Migrating %d facts …", n_facts)
    fact_id_map: Dict[int, Any] = {}  # sqlite fact_id → {id:, content:}
    for row in reader.rows("facts", "fact_id"):
        fact_id = row.pop("fact_id", None)
        row["id"] = fact_id

        if embedder and row.get("content"):
            emb = embedder.embed([row["content"]])[0]
            row["embedding"] = emb.dense

        rid = writer.insert_vertex("Fact", row)
        if rid:
            fact_id_map[fact_id] = {"id": fact_id, "content": row.get("content", "")}

    # ---- Fact-Entity edges ----
    n_links = reader.count("fact_entities")
    logger.info("Migrating %d fact-entity links …", n_links)
    for row in reader.rows("fact_entities"):
        eid = row.get("entity_id")
        fid = row.get("fact_id")
        ename = entity_id_map.get(eid)
        if ename and fid in fact_id_map:
            writer.ensure_edge("HAS_FACT", "Entity", ename, "Fact", fid)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def get_hermes_home() -> Path:
    """Resolve ~/.hermes directory."""
    base = os.environ.get("HERMES_HOME")
    if base:
        return Path(base)
    return Path.home() / ".hermes"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Migrate SQLite → ArcadeDB")
    p.add_argument("--state-db", help="Path to state.db (default: ~/.hermes/state.db)")
    p.add_argument("--kanban-db", help="Path to kanban.db (default: ~/.hermes/kanban.db)")
    p.add_argument("--memory-db", help="Path to memory_store.db (default: ~/.hermes/memory_store.db)")
    p.add_argument("--host", default="localhost", help="ArcadeDB host (default: localhost)")
    p.add_argument("--port", type=int, default=2480, help="ArcadeDB HTTP port (default: 2480)")
    p.add_argument("--database", default="hermes", help="ArcadeDB database name (default: hermes)")
    p.add_argument("--user", default="root", help="ArcadeDB username (default: root)")
    p.add_argument("--password", default="hermes123", help="ArcadeDB password")
    p.add_argument("--embed", action="store_true", help="Compute embeddings during migration (slow)")
    p.add_argument("--state-only", action="store_true", help="Only migrate state.db")
    p.add_argument("--kanban-only", action="store_true", help="Only migrate kanban.db")
    p.add_argument("--memory-only", action="store_true", help="Only migrate memory_store.db")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    hermes_home = get_hermes_home()

    # Resolve DB paths
    state_db = args.state_db or str(hermes_home / "state.db")
    kanban_db = args.kanban_db or str(hermes_home / "kanban.db")
    memory_db = args.memory_db or str(hermes_home / "memory_store.db")

    # Check at least one DB exists
    do_state = not args.kanban_only and not args.memory_only
    do_kanban = not args.state_only and not args.memory_only
    do_memory = not args.state_only and not args.kanban_only

    if do_state and not os.path.exists(state_db):
        logger.warning("state.db not found at %s — skipping", state_db)
        do_state = False
    if do_kanban and not os.path.exists(kanban_db):
        logger.warning("kanban.db not found at %s — skipping", kanban_db)
        do_kanban = False
    if do_memory and not os.path.exists(memory_db):
        logger.warning("memory_store.db not found at %s — skipping", memory_db)
        do_memory = False

    if not any([do_state, do_kanban, do_memory]):
        logger.error("No databases found to migrate. Check paths:\n"
                     "  %s\n  %s\n  %s", state_db, kanban_db, memory_db)
        raise SystemExit(1)

    # Connect ArcadeDB
    logger.info("Connecting to ArcadeDB at %s:%d …", args.host, args.port)
    cfg = ArcadeDBConfig(
        host=args.host, port=args.port,
        database=args.database,
        user=args.user, password=args.password,
    )
    db = ArcadeDBAdapter(cfg)
    db.connect()

    # Ensure schema exists
    logger.info("Ensuring schema …")
    SchemaManager(db).create_all()

    writer = ArcadeDBWriter(db)

    # Embedder (optional)
    embedder: Optional[EmbedderProvider] = None
    if args.embed:
        logger.info("Initializing embedder (this may take a moment) …")
        embedder = create_embedder({"provider": "fastembed"})
        embedder.initialize()
        logger.info("Embedder ready — dim=%d", len(embedder.embed(["test"])[0].dense))

    # Migrate
    t0 = time.time()

    if do_state:
        logger.info("=== Migrating state.db ===")
        r = SQLiteReader(state_db)
        r.open()
        try:
            migrate_state(r, writer, embedder)
        finally:
            r.close()

    if do_kanban:
        logger.info("=== Migrating kanban.db ===")
        r = SQLiteReader(kanban_db)
        r.open()
        try:
            migrate_kanban(r, writer)
        finally:
            r.close()

    if do_memory:
        logger.info("=== Migrating memory_store.db ===")
        r = SQLiteReader(memory_db)
        r.open()
        try:
            migrate_memory(r, writer, embedder)
        finally:
            r.close()

    elapsed = time.time() - t0
    final = writer.stats()
    logger.info("Migration complete in %.1fs — inserted=%d skipped=%d errors=%d",
                elapsed, final["inserted"], final["skipped"], final["errors"])

    if embedder:
        embedder.shutdown()
    db.close()


if __name__ == "__main__":
    main()
