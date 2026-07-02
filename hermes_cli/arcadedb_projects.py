"""ArcadeDB-backed project store (Phase 8.1).

Replaces: hermes_cli/projects_db.py (~727 lines, module functions)
With: ArcadeDB Project vertices + HAS_FOLDER edges + StateMeta

Links:
  Phase 8 spec: docs/arcadedb-migration/phase-8-other-dbs.md
  Reference:   hermes_cli/projects_db.py
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from hermes_cli.arcadedb import ArcadeDBAdapter
from hermes_cli.arcadedb_session import _q, _n


class ArcadedbProjectsDB:

    def __init__(self, adapter: ArcadeDBAdapter):
        self._adapter = adapter

    def create_project(self, name: str, slug: str = None,
                       folders: list[dict] = None, **kwargs) -> str:
        slug = slug or name.lower().replace(" ", "-")
        now = int(time.time())
        self._adapter.execute(
            f"CREATE VERTEX Project SET name = {_q(name)}, slug = {_q(slug)}, "
            f"description = {_q(kwargs.get('description', ''))}, "
            f"icon = {_q(kwargs.get('icon', ''))}, color = {_q(kwargs.get('color', ''))}, "
            f"primary_path = {_q(kwargs.get('primary_path', ''))}, "
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

    def update_project(self, project_id: str, **updates) -> None:
        parts = ", ".join(f"`{k}` = {_q(v)}" for k, v in updates.items())
        self._adapter.execute(
            f"UPDATE Project SET {parts} WHERE @rid = {_q(project_id)}"
        )

    def add_folder(self, project_id: str, path: str, label: str = "",
                   is_primary: bool = False) -> None:
        if is_primary:
            self._adapter.execute(
                f"UPDATE Project SET primary_path = {_q(path)} WHERE @rid = {_q(project_id)}"
            )
        # Store folder as edge property for now (HAS_FOLDER edge not yet in schema)
        self._adapter.execute(
            f"CREATE VERTEX ProjectFolder SET "
            f"project_id = {_q(project_id)}, path = {_q(path)}, "
            f"label = {_q(label)}, is_primary = {1 if is_primary else 0}, "
            f"added_at = {_n(time.time())}"
        )

    def remove_folder(self, project_id: str, path: str) -> None:
        self._adapter.execute(
            f"DELETE VERTEX ProjectFolder WHERE project_id = {_q(project_id)} AND path = {_q(path)}"
        )

    def set_primary(self, project_id: str, path: str) -> None:
        self._adapter.execute(
            f"UPDATE Project SET primary_path = {_q(path)} WHERE @rid = {_q(project_id)}"
        )

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

    def set_active(self, project_id: str) -> None:
        """Store active project pointer in StateMeta."""
        self._adapter.execute(
            f"DELETE VERTEX StateMeta WHERE key = 'active_project'"
        )
        self._adapter.execute(
            f"CREATE VERTEX StateMeta SET key = 'active_project', value = {_q(project_id)}"
        )

    def get_active_id(self) -> str | None:
        rows = self._adapter.query(
            "SELECT value FROM StateMeta WHERE key = 'active_project' LIMIT 1"
        )
        return rows[0]["value"] if rows else None

    def project_for_path(self, path: str) -> dict | None:
        """Find project by longest prefix match on primary_path."""
        rows = self._adapter.query(
            f"SELECT FROM Project WHERE primary_path LIKE {_q(path + '%')} "
            "ORDER BY created_at DESC"
        )
        if not rows:
            return None
        # Longest prefix match
        best, best_len = None, 0
        for r in rows:
            pp = r.get("primary_path", "")
            if path.startswith(pp) and len(pp) > best_len:
                best, best_len = r, len(pp)
        return best

    def record_discovered_repos(self, repos: list[dict]) -> None:
        for repo in repos:
            root = repo.get("root", "")
            label = repo.get("label", root)
            self._adapter.execute(
                f"CREATE VERTEX DiscoveredRepo SET "
                f"root = {_q(root)}, label = {_q(label)}, "
                f"last_seen = {_n(time.time())}"
            )

    def close(self) -> None:
        pass
