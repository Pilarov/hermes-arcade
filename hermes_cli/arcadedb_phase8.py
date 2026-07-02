"""ArcadeDB-backed stores — Phase 8.

Projects DB, Response Store, Verification Evidence, RetainDB Queue.
All use the shared ArcadeDBAdapter (Phase 2).

Links:
  Phase 8 spec: docs/arcadedb-migration/phase-8-other-dbs.md
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from hermes_cli.arcadedb import ArcadeDBAdapter
from hermes_cli.arcadedb_session import _q, _n


# =============================================================================
# Projects DB
# =============================================================================

class ArcadedbProjectsDB:
    """ArcadeDB-backed project store."""

    def __init__(self, adapter: ArcadeDBAdapter):
        self._adapter = adapter

    def create_project(self, name: str, slug: str = None,
                       folders: list[dict] = None, **kwargs) -> str:
        slug = slug or name.lower().replace(" ", "-")
        now = int(time.time())
        self._adapter.execute(
            f"CREATE VERTEX Project SET "
            f"name = {_q(name)}, slug = {_q(slug)}, "
            f"description = {_q(kwargs.get('description', ''))}, "
            f"created_at = {now}"
        )
        rows = self._adapter.query(
            f"SELECT @rid FROM Project WHERE slug = {_q(slug)} LIMIT 1"
        )
        return rows[0]["@rid"] if rows else slug

    def list_projects(self, include_archived: bool = False) -> list[dict]:
        where = "" if include_archived else " WHERE archived = 0"
        return self._adapter.query(f"SELECT FROM Project{where} ORDER BY created_at DESC")

    def get_project(self, id_or_slug: str) -> dict | None:
        rows = self._adapter.query(
            f"SELECT FROM Project WHERE @rid = {_q(id_or_slug)} OR slug = {_q(id_or_slug)} LIMIT 1"
        )
        return rows[0] if rows else None

    def archive_project(self, project_id: str) -> None:
        self._adapter.execute(
            f"UPDATE Project SET archived = 1 WHERE @rid = {_q(project_id)}"
        )

    def restore_project(self, project_id: str) -> None:
        self._adapter.execute(
            f"UPDATE Project SET archived = 0 WHERE @rid = {_q(project_id)}"
        )

    def delete_project(self, project_id: str) -> None:
        self._adapter.execute(f"DELETE VERTEX Project WHERE @rid = {_q(project_id)}")

    def close(self) -> None:
        pass


# =============================================================================
# Response Store (LRU cache for API responses)
# =============================================================================

class ArcadedbResponseStore:
    """ArcadeDB-backed LRU response cache."""

    def __init__(self, adapter: ArcadeDBAdapter, max_size: int = 1000):
        self._adapter = adapter
        self._max_size = max_size

    def get(self, response_id: str) -> dict | None:
        rows = self._adapter.query(
            f"SELECT data FROM Response WHERE response_id = {_q(response_id)} LIMIT 1"
        )
        if rows:
            self._adapter.execute(
                f"UPDATE Response SET accessed_at = {_n(time.time())} "
                f"WHERE response_id = {_q(response_id)}"
            )
            return json.loads(rows[0]["data"]) if rows[0].get("data") else None
        return None

    def put(self, response_id: str, data: dict) -> None:
        self._adapter.execute(
            f"CREATE VERTEX Response SET "
            f"response_id = {_q(response_id)}, "
            f"data = {_q(json.dumps(data))}, "
            f"accessed_at = {_n(time.time())}"
        )
        # LRU eviction
        rows = self._adapter.query("SELECT count(*) as cnt FROM Response")
        count = rows[0].get("cnt", 0) if rows else 0
        if count > self._max_size:
            excess = count - self._max_size
            self._adapter.execute(
                f"DELETE FROM Response WHERE @rid IN "
                f"(SELECT @rid FROM Response ORDER BY accessed_at ASC LIMIT {excess})"
            )

    def delete(self, response_id: str) -> None:
        self._adapter.execute(
            f"DELETE VERTEX Response WHERE response_id = {_q(response_id)}"
        )

    def close(self) -> None:
        pass

    def __len__(self) -> int:
        rows = self._adapter.query("SELECT count(*) as cnt FROM Response")
        return rows[0].get("cnt", 0) if rows else 0


# =============================================================================
# Verification Evidence (audit ledger)
# =============================================================================

class ArcadedbVerificationStore:
    """ArcadeDB-backed verification audit trail."""

    def __init__(self, adapter: ArcadeDBAdapter):
        self._adapter = adapter

    def record_terminal_result(self, command: str, cwd: str, session_id: str,
                               exit_code: int, output: str) -> None:
        now = time.time()
        self._adapter.execute(
            f"CREATE VERTEX VerificationEvent SET "
            f"command = {_q(command)}, cwd = {_q(cwd)}, "
            f"session_id = {_q(session_id)}, exit_code = {exit_code}, "
            f"output_summary = {_q(output[:500])}, created_at = {_n(now)}"
        )

    def verification_status(self, session_id: str, cwd: str) -> str:
        rows = self._adapter.query(
            f"SELECT exit_code FROM VerificationEvent "
            f"WHERE session_id = {_q(session_id)} AND cwd = {_q(cwd)} "
            f"ORDER BY created_at DESC LIMIT 1"
        )
        if not rows:
            return "unverified"
        return "passed" if rows[0].get("exit_code") == 0 else "failed"

    def close(self) -> None:
        pass


# =============================================================================
# RetainDB Queue (write-behind durability queue)
# =============================================================================

class ArcadedbWriteQueue:
    """ArcadeDB-backed durability queue for RetainDB."""

    def __init__(self, adapter: ArcadeDBAdapter):
        self._adapter = adapter
        self._queue = []

    def enqueue(self, user_id: str, session_id: str, messages: list[dict]) -> None:
        now = time.time()
        self._adapter.execute(
            f"CREATE VERTEX PendingIngest SET "
            f"user_id = {_q(user_id)}, session_id = {_q(session_id)}, "
            f"messages_json = {_q(json.dumps(messages))}, "
            f"created_at = {_n(now)}"
        )

    def pending_rows(self) -> list[dict]:
        return self._adapter.query("SELECT FROM PendingIngest ORDER BY created_at")

    def flush_row(self, row: dict) -> bool:
        self._adapter.execute(
            f"DELETE VERTEX PendingIngest WHERE @rid = {_q(row['@rid'])}"
        )
        return True

    def close(self) -> None:
        pass
