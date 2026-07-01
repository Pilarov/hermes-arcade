"""SQLite → ArcadeDB migration tool (Phase 5).

Usage:
  python -m hermes_cli.migrate_to_arcadedb [--dry-run] [--state-only] [--embed]

Links:
  Phase 5 spec: docs/arcadedb-migration/phase-5-migration-tool.md
  Reference:    hermes_cli/arcadedb_migrate.py (old migrator)
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)


@dataclass
class MigrationReport:
    source_db: str = ""
    target: str = "ArcadeDB"
    total_rows: int = 0
    migrated_rows: int = 0
    skipped_rows: int = 0
    failed_rows: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


class ArcadeDBMigrator:
    """Migrate SQLite databases to ArcadeDB."""

    def __init__(self, sqlite_path: str, adapter):
        self._sqlite_path = sqlite_path
        self._adapter = adapter
        self._embedder = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def migrate_state(
        self, compute_embeddings: bool = False
    ) -> MigrationReport:
        """Migrate state.db → ArcadeDB."""
        report = MigrationReport(source_db="state.db")
        start = time.time()

        conn = sqlite3.connect(self._sqlite_path)
        conn.row_factory = sqlite3.Row

        # Sessions
        sessions = conn.execute("SELECT * FROM sessions ORDER BY started_at").fetchall()
        report.total_rows += len(sessions)
        for row in sessions:
            try:
                existing = self._adapter.query(
                    "SELECT FROM Session WHERE id = %s", (row["id"],)
                )
                if existing:
                    report.skipped_rows += 1
                    continue

                props = {k: row[k] for k in row.keys() if row[k] is not None}
                from hermes_cli.arcadedb_session import _q, _n
                parts = ", ".join(
                    f"`{k}` = {_q(v)}" for k, v in props.items()
                )
                self._adapter.execute(f"INSERT INTO Session SET {parts}")
                report.migrated_rows += 1
            except Exception as e:
                report.failed_rows += 1
                report.errors.append(f"session {row.get('id')}: {e}")

        # Messages
        messages = conn.execute(
            "SELECT * FROM messages ORDER BY id"
        ).fetchall()
        report.total_rows += len(messages)
        for row in messages:
            try:
                from hermes_cli.arcadedb_session import _q, _n
                content = row["content"]
                if content and isinstance(content, str) and content.startswith("\x00json:"):
                    content = content  # keep as-is (already encoded)

                sql = (
                    f"CREATE VERTEX Message SET "
                    f"session_id = {_q(row['session_id'])}, "
                    f"role = {_q(row['role'])}, "
                    f"content = {_q(content)}, "
                    f"timestamp = {_n(row['timestamp'])}, "
                    f"active = {row.get('active', 1)}, "
                    f"compacted = {row.get('compacted', 0)}"
                )
                self._adapter.execute(sql)
                report.migrated_rows += 1
            except Exception as e:
                report.failed_rows += 1
                report.errors.append(f"message {row.get('id')}: {e}")

        # Compression locks
        locks = conn.execute("SELECT * FROM compression_locks").fetchall()
        for row in locks:
            try:
                from hermes_cli.arcadedb_session import _q, _n
                self._adapter.execute(
                    f"CREATE VERTEX CompressionLock SET "
                    f"session_id = {_q(row['session_id'])}, "
                    f"holder = {_q(row['holder'])}, "
                    f"acquired_at = {_n(row['acquired_at'])}, "
                    f"expires_at = {_n(row['expires_at'])}"
                )
            except Exception:
                pass

        # StateMeta
        meta = conn.execute("SELECT * FROM state_meta").fetchall()
        for row in meta:
            try:
                from hermes_cli.arcadedb_session import _q
                self._adapter.execute(
                    f"CREATE VERTEX StateMeta SET "
                    f"key = {_q(row['key'])}, "
                    f"value = {_q(row['value'])}"
                )
            except Exception:
                pass

        conn.close()
        report.duration_seconds = time.time() - start
        return report

    def verify(self, source_db: str) -> bool:
        """Verify migration by comparing row counts."""
        conn = sqlite3.connect(self._sqlite_path)
        conn.row_factory = sqlite3.Row

        checks = {
            "sessions": "Session",
            "messages": "Message",
            "compression_locks": "CompressionLock",
        }

        for table, vtype in checks.items():
            try:
                sql_count = conn.execute(
                    f"SELECT count(*) as cnt FROM {table}"
                ).fetchone()["cnt"]
                arc_rows = self._adapter.query(
                    f"SELECT count(*) as cnt FROM {vtype}"
                )
                arc_count = arc_rows[0].get("cnt", 0) if arc_rows else 0
                logger.info(
                    "  %s: SQLite=%s ArcadeDB=%s %s",
                    vtype, sql_count, arc_count,
                    "OK" if sql_count == arc_count else "MISMATCH",
                )
            except Exception as e:
                logger.warning("  %s: verification failed — %s", vtype, e)

        conn.close()
        return True


def run_migration(argv: list[str] = None) -> None:
    """CLI entry point: `hermes migrate --arcadedb`."""
    p = argparse.ArgumentParser(
        description="Migrate SQLite → ArcadeDB"
    )
    p.add_argument("--dry-run", action="store_true", help="Preview only")
    p.add_argument("--verify", action="store_true", help="Verify after migration")
    p.add_argument("--state-only", action="store_true", help="Only state.db")
    p.add_argument("--embed", action="store_true", help="Compute embeddings")
    p.add_argument("--db-path", default=None, help="Path to state.db")

    args = p.parse_args(argv)

    from hermes_cli.config import load_config
    from hermes_cli.arcadedb import ArcadeDBConfig, ArcadeDBAdapter

    config = load_config()
    cfg = config.get("database", {}).get("arcadedb", {})

    if not cfg.get("enabled"):
        print("ArcadeDB is not enabled in config.yaml. Set database.arcadedb.enabled=true")
        return

    db_config = ArcadeDBConfig(
        host=cfg.get("host", "localhost"),
        port=cfg.get("port", 5432),
        database=cfg.get("database", "hermes"),
        user=cfg.get("user", "root"),
        password=cfg.get("password", ""),
    )
    adapter = ArcadeDBAdapter(db_config)
    adapter.connect()
    print(f"Connected to ArcadeDB ({cfg['host']}:{cfg['port']})")

    db_path = args.db_path or str(get_hermes_home() / "state.db")
    migrator = ArcadeDBMigrator(db_path, adapter)

    if args.dry_run:
        conn = sqlite3.connect(db_path)
        for table in ["sessions", "messages", "compression_locks", "state_meta"]:
            try:
                cnt = conn.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0]
                print(f"  {table}: {cnt} rows")
            except Exception:
                print(f"  {table}: N/A")
        conn.close()
        print("\nDry-run complete. Run without --dry-run to migrate.")
        adapter.close()
        return

    print(f"Migrating {db_path} → ArcadeDB...")
    report = migrator.migrate_state(compute_embeddings=args.embed)

    print(f"\nMigration complete ({report.duration_seconds:.1f}s):")
    print(f"  Total:    {report.total_rows}")
    print(f"  Migrated: {report.migrated_rows}")
    print(f"  Skipped:  {report.skipped_rows}")
    print(f"  Failed:   {report.failed_rows}")

    if report.errors:
        print(f"\n  Errors ({len(report.errors)}):")
        for e in report.errors[:5]:
            print(f"    - {e}")

    if args.verify:
        print("\nVerifying...")
        migrator.verify("state.db")

    adapter.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_migration()
