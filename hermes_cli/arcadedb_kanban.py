"""ArcadeDB-backed Kanban board (Phase 6).

Replaces SQLite kanban_db.py for ArcadeDB native storage.
Implements core CRUD + CAS claim + DAG edge traversal.

Links:
  Phase 6 spec: docs/arcadedb-migration/phase-6-kanbandb.md
  Reference:   hermes_cli/kanban_db.py (8,723 lines — SQLite)
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from hermes_cli.arcadedb import ArcadeDBAdapter
from hermes_cli.arcadedb_session import _q, _n


class ArcadeDBKanbanError(Exception):
    pass


class ArcadedbKanbanDB:

    def __init__(self, adapter: ArcadeDBAdapter, board_slug: str = "default"):
        self._adapter = adapter
        self._board_slug = board_slug

    def close(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Task CRUD
    # ------------------------------------------------------------------

    def create_task(
        self, title: str, body: str = "", assignee: str = None,
        priority: int = 0, tenant: str = "", **kwargs,
    ) -> str:
        now = time.time()
        sql = (
            f"CREATE VERTEX Task SET "
            f"title = {_q(title)}, body = {_q(body)}, "
            f"assignee = {_q(assignee)}, status = {_q('ready')}, "
            f"priority = {priority}, created_at = {_n(now)}, "
            f"tenant = {_q(tenant)}, created_by = {_q(kwargs.get('created_by', ''))}, "
            f"workspace_kind = {_q('scratch')}"
        )

        def _do(cur):
            cur.execute(sql)
            cur.execute(
                "SELECT @rid FROM Task ORDER BY @rid DESC LIMIT 1"
            )
            return cur.fetchone()["@rid"]

        return self._adapter.transact(_do)

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        rows = self._adapter.query(
            "SELECT FROM Task WHERE @rid = %s", (task_id,)
        )
        return rows[0] if rows else None

    def list_tasks(
        self, status: str = None, assignee: str = None,
        tenant: str = None, limit: int = 50,
    ) -> List[Dict[str, Any]]:
        where = []
        params: List[Any] = []
        if status:
            where.append("status = %s"); params.append(status)
        if assignee:
            where.append("assignee = %s"); params.append(assignee)
        if tenant:
            where.append("tenant = %s"); params.append(tenant)

        w = " WHERE " + " AND ".join(where) if where else ""
        return self._adapter.query(
            f"SELECT FROM Task{w} ORDER BY created_at DESC LIMIT {limit}"
        )

    def update_task(self, task_id: str, **updates) -> bool:
        parts = ", ".join(
            f"`{k}` = {_q(v)}" for k, v in updates.items()
        )
        self._adapter.execute(
            f"UPDATE Task SET {parts} WHERE @rid = {_q(task_id)}"
        )
        return True

    def delete_task(self, task_id: str) -> bool:
        self._adapter.execute(
            f"DELETE VERTEX Task WHERE @rid = {_q(task_id)}"
        )
        return True

    # ------------------------------------------------------------------
    # Task Linking (DAG via DEPENDS_ON edges)
    # ------------------------------------------------------------------

    def link_tasks(self, parent_id: str, child_id: str) -> bool:
        if self._would_cycle(parent_id, child_id):
            raise ArcadeDBKanbanError(
                f"Link {parent_id} -> {child_id} would create a cycle"
            )

        def _do(cur):
            cur.execute(
                f"CREATE EDGE DEPENDS_ON FROM "
                f"(SELECT FROM Task WHERE @rid = {_q(parent_id)}) TO "
                f"(SELECT FROM Task WHERE @rid = {_q(child_id)})"
            )
            return True
        try:
            return self._adapter.transact(_do)
        except Exception:
            return False

    def unlink_tasks(self, parent_id: str, child_id: str) -> bool:
        self._adapter.execute(
            f"DELETE EDGE DEPENDS_ON WHERE "
            f"out() = (SELECT FROM Task WHERE @rid = {_q(parent_id)}) AND "
            f"in() = (SELECT FROM Task WHERE @rid = {_q(child_id)})"
        )
        return True

    def parent_ids(self, task_id: str) -> List[str]:
        rows = self._adapter.query(
            "SELECT expand(in('DEPENDS_ON')) FROM Task WHERE @rid = %s",
            (task_id,),
        )
        return [r["@rid"] for r in rows] if rows else []

    def child_ids(self, task_id: str) -> List[str]:
        rows = self._adapter.query(
            "SELECT expand(out('DEPENDS_ON')) FROM Task WHERE @rid = %s",
            (task_id,),
        )
        return [r["@rid"] for r in rows] if rows else []

    # ------------------------------------------------------------------
    # CAS Claim (CRITICAL)
    # ------------------------------------------------------------------

    def claim_task(
        self, task_id: str, worker_profile: str, ttl_seconds: int = 300,
    ) -> Optional[str]:
        now = time.time()
        run_id = f"run_{task_id.replace('#', '').replace(':', '_')}_{int(now)}"
        expires = now + ttl_seconds

        def _do(cur):
            cur.execute(
                f"SELECT status, claim_lock FROM Task WHERE @rid = {_q(task_id)}"
            )
            task = cur.fetchone()
            if not task:
                raise ArcadeDBKanbanError(f"Task {task_id} not found")
            if task["status"] != "ready":
                return None
            if task["claim_lock"] is not None:
                return None

            cur.execute(
                f"UPDATE Task SET status = {_q('running')}, "
                f"claim_lock = {_q(run_id)}, claim_expires = {_n(expires)}, "
                f"started_at = COALESCE(started_at, {_n(now)}) "
                f"WHERE @rid = {_q(task_id)} AND claim_lock IS NULL"
            )
            if cur.rowcount == 0:
                return None

            cur.execute(
                f"CREATE VERTEX TaskRun SET "
                f"profile = {_q(worker_profile)}, status = {_q('running')}, "
                f"started_at = {_n(now)}, claim_lock = {_q(run_id)}, "
                f"claim_expires = {_n(expires)}"
            )
            cur.execute(
                "SELECT @rid FROM TaskRun WHERE claim_lock = %s ORDER BY @rid DESC LIMIT 1",
                (run_id,),
            )
            run_row = cur.fetchone()

            cur.execute(
                f"CREATE EDGE HAS_RUN FROM "
                f"(SELECT FROM Task WHERE @rid = {_q(task_id)}) TO "
                f"(SELECT FROM TaskRun WHERE @rid = {_q(run_row['@rid'])}) "
                f"SET status = {_q('running')}, started_at = {_n(now)}"
            )
            return run_id

        return self._adapter.transact(_do)

    def release_stale_claims(self) -> int:
        now = time.time()
        self._adapter.execute(
            f"UPDATE Task SET status = {_q('ready')}, "
            f"claim_lock = NULL, claim_expires = NULL "
            f"WHERE status = {_q('running')} AND claim_expires < {_n(now)}"
        )
        return 0

    def heartbeat_claim(self, task_id: str, run_id: str) -> bool:
        self._adapter.execute(
            f"UPDATE Task SET claim_expires = {_n(time.time() + 300)} "
            f"WHERE @rid = {_q(task_id)} AND claim_lock = {_q(run_id)}"
        )
        return True

    def complete_task(self, task_id: str, result: str = "") -> bool:
        self._adapter.execute(
            f"UPDATE Task SET status = {_q('done')}, result = {_q(result)}, "
            f"claim_lock = NULL, claim_expires = NULL, "
            f"completed_at = {_n(time.time())} "
            f"WHERE @rid = {_q(task_id)}"
        )
        return True

    def block_task(self, task_id: str, kind: str = "manual") -> bool:
        self._adapter.execute(
            f"UPDATE Task SET status = {_q('blocked')}, "
            f"block_kind = {_q(kind)} WHERE @rid = {_q(task_id)}"
        )
        return True

    def unblock_task(self, task_id: str) -> bool:
        self._adapter.execute(
            f"UPDATE Task SET status = {_q('ready')}, "
            f"block_kind = NULL WHERE @rid = {_q(task_id)}"
        )
        return True

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def list_runs(self, task_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        return self._adapter.query(
            "SELECT expand(out('HAS_RUN')) FROM Task WHERE @rid = %s "
            "ORDER BY started_at DESC LIMIT %s",
            (task_id, limit),
        )

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        rows = self._adapter.query(
            "SELECT FROM TaskRun WHERE @rid = %s", (run_id,)
        )
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------

    def add_comment(self, task_id: str, author: str, body: str) -> str:
        now = time.time()

        def _do(cur):
            cur.execute(
                f"CREATE VERTEX TaskComment SET "
                f"author = {_q(author)}, body = {_q(body)}, "
                f"created_at = {_n(now)}"
            )
            cur.execute(
                "SELECT @rid FROM TaskComment ORDER BY @rid DESC LIMIT 1"
            )
            comment_rid = cur.fetchone()["@rid"]

            cur.execute(
                f"CREATE EDGE HAS_COMMENT FROM "
                f"(SELECT FROM Task WHERE @rid = {_q(task_id)}) TO "
                f"(SELECT FROM TaskRun WHERE @rid = {_q(comment_rid)}) "
                f"SET created_at = {_n(now)}"
            )
            return comment_rid

        return self._adapter.transact(_do)

    def list_comments(self, task_id: str) -> List[Dict[str, Any]]:
        return self._adapter.query(
            "SELECT expand(out('HAS_COMMENT')) FROM Task WHERE @rid = %s "
            "ORDER BY created_at", (task_id,)
        )

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _would_cycle(self, new_parent: str, start_child: str) -> bool:
        visited = {start_child}
        queue = [new_parent]
        while queue:
            current = queue.pop(0)
            if current in visited:
                return True
            visited.add(current)
            queue.extend(self.parent_ids(current))
        return False
