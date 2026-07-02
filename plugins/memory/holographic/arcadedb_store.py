"""ArcadeDB-backed Memory Store (Phase 7).

Replaces MemoryStore (plugins/memory/holographic/store.py, 578 lines)
with ArcadeDB native Fact/Entity vertices + MENTIONS edges.

Links:
  Phase 7 spec: docs/arcadedb-migration/phase-7-memory-store.md
  Reference:   plugins/memory/holographic/store.py (SQLite MemoryStore)
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from hermes_cli.arcadedb import ArcadeDBAdapter
from hermes_cli.arcadedb_session import _q, _n


class ArcadedbMemoryStore:

    def __init__(self, adapter: ArcadeDBAdapter, embedder=None):
        self._adapter = adapter
        self._embedder = embedder

    def close(self) -> None:
        pass

    def add_fact(
        self, content: str, category: str = "general",
        tags: List[str] = None,
    ) -> int:
        now = time.time()
        tags_json = json.dumps(tags or [])

        emb_sql = ""
        if self._embedder:
            emb = self._embedder.embed([content])[0]
            from hermes_cli.arcadedb import ArcadeDBAdapter
            emb_sql = f", embedding = {ArcadeDBAdapter._vec(emb.dense)}"

        entities = self._extract_entities(content)

        def _do(cur):
            cur.execute(
                f"CREATE VERTEX Fact SET "
                f"content = {_q(content)}, category = {_q(category)}, "
                f"tags = {_q(tags_json)}, trust_score = 0.5, "
                f"retrieval_count = 0, helpful_count = 0, "
                f"created_at = {_n(now)}, updated_at = {_n(now)}"
                f"{emb_sql}"
            )
            cur.execute(
                "SELECT @rid FROM Fact WHERE content = %s AND created_at = %s LIMIT 1",
                (content, now),
            )
            fact = cur.fetchone()

            for name in entities:
                self._ensure_entity(cur, name)
                cur.execute(
                    f"CREATE EDGE MENTIONS FROM "
                    f"(SELECT FROM Fact WHERE @rid = {_q(fact['@rid'])}) TO "
                    f"(SELECT FROM Entity WHERE name = {_q(name)}) "
                    "SET weight = 1.0"
                )
            return hash(fact["@rid"]) & 0x7FFFFFFF

        return self._adapter.transact(_do)

    def search_facts(
        self, query: str, category: str = None,
        min_trust: float = 0, limit: int = 20,
    ) -> List[Dict[str, Any]]:
        sql = (
            f"SELECT FROM Fact "
            f"WHERE content LIKE {_q(f'%{query}%')} "
            f"AND trust_score >= {min_trust}"
        )
        if category:
            sql += f" AND category = {_q(category)}"
        sql += f" ORDER BY trust_score DESC LIMIT {limit}"
        return self._adapter.query(sql)

    def list_facts(
        self, category: str = None, min_trust: float = 0,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        sql = f"SELECT FROM Fact WHERE trust_score >= {min_trust}"
        if category:
            sql += f" AND category = {_q(category)}"
        sql += f" ORDER BY trust_score DESC LIMIT {limit}"
        return self._adapter.query(sql)

    def record_feedback(self, fact_id: int, helpful: bool) -> Dict[str, Any]:
        delta = 0.05 if helpful else -0.10
        self._adapter.execute(
            f"UPDATE Fact SET "
            f"trust_score = trust_score + {delta}, "
            f"retrieval_count = retrieval_count + 1, "
            f"helpful_count = helpful_count + {1 if helpful else 0}, "
            f"updated_at = {_n(time.time())} "
            f"WHERE @rid = {_q(fact_id)}"
        )
        return {"old_trust": None, "new_trust": None}

    def _ensure_entity(self, cur, name: str) -> None:
        cur.execute(
            f"SELECT FROM Entity WHERE name = {_q(name)}"
        )
        if not cur.fetchall():
            cur.execute(
                f"CREATE VERTEX Entity SET "
                f"name = {_q(name)}, entity_type = 'unknown', "
                f"created_at = {_n(time.time())}"
            )

    @staticmethod
    def _extract_entities(text: str) -> List[str]:
        import re
        return re.findall(r"\b[A-Z][a-z]+\b", text)[:5]
