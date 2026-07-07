"""High-level graph store — embedder + ArcadeDB in one API.

Auto-embeds text on insert, provides vector/hybrid search.

Known ArcadeDB 26.7.1-SNAPSHOT quirk: vector arrays CANNOT be passed
through HTTP parameter binding (:name / ?), because Jackson deserialises
the JSON-array elements as Java float[] primitives rather than Double
objects, and the LSM_VECTOR index rejects those.

Workaround: pass vector arrays as JSON-array SQL literals directly in
the command string for INSERT/UPDATE.  vector.neighbors() and friends
DO accept parameterised query vectors, so search methods use :qv params.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from hermes_cli.arcadedb import ArcadeDBAdapter
from hermes_cli.embedder import EmbedderProvider

logger = logging.getLogger(__name__)


def _now() -> float:
    return time.time()


def _vec(val: List[float]) -> str:
    """Format a float list as a JSON-array SQL literal."""
    return json.dumps([float(x) for x in val], allow_nan=False)


class GraphStore:
    """Wraps ArcadeDBAdapter + EmbedderProvider for the Hermes domain."""

    def __init__(
        self,
        db: ArcadeDBAdapter,
        embedder: EmbedderProvider,
        auto_embed: bool = True,
    ) -> None:
        self._db = db
        self._embedder = embedder
        self._auto_embed = auto_embed

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    def create_session(self, **props: Any) -> Dict[str, Any]:
        props.setdefault("started_at", _now())
        sql = "INSERT INTO Session SET " + ", ".join(
            f"`{k}` = :{k}" for k in props
        )
        self._db.execute(sql, params=props)
        return self.get_session(props["id"])

    def get_session(self, session_id: str) -> Dict[str, Any]:
        rows = self._db.query(
            "SELECT FROM Session WHERE id = :id", params={"id": session_id}
        )
        if not rows:
            raise KeyError(f"Session {session_id} not found")
        return rows[0]

    # ------------------------------------------------------------------
    # Message (with optional auto-embedding)
    # ------------------------------------------------------------------

    def add_message(
        self,
        session_rid_or_id: str,
        role: str,
        content: str,
        timestamp: Optional[float] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        ts = timestamp or _now()

        param_props: Dict[str, Any] = {
            "content": content,
            "role": role,
            "timestamp": ts,
        }
        for k, v in extra.items():
            if k != "embedding":
                param_props[k] = v

        sql_parts: List[str] = []
        for k in param_props:
            sql_parts.append(f"`{k}` = :{k}")

        if self._auto_embed and content:
            emb = self._embedder.embed([content])[0]
            sql_parts.append(f"`embedding` = {_vec(emb.dense)}")
        elif "embedding" in extra:
            sql_parts.append(f"`embedding` = {_vec(extra['embedding'])}")

        sql = "INSERT INTO Message SET " + ", ".join(sql_parts)
        self._db.execute(sql, params=param_props)

        rows = self._db.query(
            "SELECT FROM Message WHERE timestamp = :ts AND role = :role LIMIT 1",
            params={"ts": ts, "role": role},
        )
        msg = rows[0] if rows else None
        if msg is None:
            raise RuntimeError("Failed to retrieve inserted Message")

        self._db.execute(
            "CREATE EDGE HAS_MESSAGE FROM "
            "(SELECT FROM Session WHERE id = :sid) TO "
            "(SELECT FROM Message WHERE @rid = :rid) "
            "SET seq = 0, role = :role, tokens = :tokens, created_at = :ts",
            params={
                "sid": session_rid_or_id,
                "rid": str(msg["@rid"]),
                "role": role,
                "tokens": len(content.split()),
                "ts": ts,
            },
        )
        return msg

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        return self._db.query(
            "SELECT expand(out('HAS_MESSAGE')) "
            "FROM Session WHERE id = :sid ORDER BY timestamp",
            params={"sid": session_id},
        )

    # ------------------------------------------------------------------
    # Vector search — Messages  (params work for vector.neighbors!)
    # ------------------------------------------------------------------

    def search_messages(
        self, query: str, top_k: int = 10
    ) -> List[Dict[str, Any]]:
        q = self._embedder.embed_query(query)
        params: Dict[str, Any] = {"qv": q.dense, "tk": top_k}
        sql = "SELECT expand(`vector.neighbors`('Message[embedding]', :qv, :tk))"
        return self._db.query(sql, params=params)

    # ------------------------------------------------------------------
    # SearchMatter — session-level CQRS read model
    # ------------------------------------------------------------------

    def create_search_matter(
        self,
        session_rid: str,
        summary: str,
        profile: str = "",
        model: str = "",
    ) -> Dict[str, Any]:
        emb = self._embedder.embed([summary])[0]

        self._db.execute(
            "INSERT INTO SearchMatter SET "
            "session_rid = :sr, "
            "summary = :s, "
            f"embedding = {_vec(emb.dense)}, "
            "created_at = :ts, "
            "profile = :p, "
            "model = :m",
            params={
                "sr": session_rid,
                "s": summary,
                "ts": _now(),
                "p": profile,
                "m": model,
            },
        )
        rows = self._db.query(
            "SELECT FROM SearchMatter WHERE session_rid = :rid LIMIT 1",
            params={"rid": session_rid},
        )
        if rows:
            return rows[0]
        raise RuntimeError("Failed to create SearchMatter")

    def search_sessions(
        self,
        query: str,
        top_k: int = 10,
        profile: Optional[str] = None,
        days: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        q = self._embedder.embed_query(query)
        params: Dict[str, Any] = {"qv": q.dense, "tk": top_k}

        filters = []
        if profile:
            filters.append("profile = :profile")
            params["profile"] = profile
        if days is not None:
            cutoff = _now() - days * 86400
            filters.append("created_at >= :cutoff")
            params["cutoff"] = cutoff

        if filters:
            sql = (
                "SELECT FROM SearchMatter WHERE @rid IN ["
                "  expand(`vector.neighbors`('SearchMatter[embedding]', :qv, :tk))"
                f"] AND {' AND '.join(filters)}"
            )
        else:
            sql = "SELECT expand(`vector.neighbors`('SearchMatter[embedding]', :qv, :tk))"
        return self._db.query(sql, params=params)

    def hybrid_search_sessions(
        self,
        query: str,
        keywords: str = "",
        top_k: int = 10,
        profile: Optional[str] = None,
        days: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        q = self._embedder.embed_query(query)
        params: Dict[str, Any] = {"qv": q.dense, "tk": top_k}

        where = ""
        if profile or days is not None:
            parts = []
            if profile:
                parts.append("profile = :profile")
                params["profile"] = profile
            if days is not None:
                parts.append("created_at >= :cutoff")
                params["cutoff"] = _now() - days * 86400
            where = " WHERE " + " AND ".join(parts)

        sql = (
            "SELECT expand(`vector.fuse`(\n"
            "    `vector.neighbors`('SearchMatter[embedding]', :qv, :tk),\n"
        )
        if keywords:
            kw_filter = f"{where} AND SEARCH_INDEX('SearchMatter[summary]', :kw) = true" if where \
                else f" WHERE SEARCH_INDEX('SearchMatter[summary]', :kw) = true"
            sql += f"    (SELECT @rid FROM SearchMatter{kw_filter}),\n"
            params["kw"] = keywords
        else:
            sql += f"    (SELECT @rid FROM SearchMatter{where}),\n"
        sql += (
            "    { fusion: 'RRF', groupBy: 'session_rid', groupSize: 1 }\n"
            ")) LIMIT :tk2"
        )
        params["tk2"] = top_k
        return self._db.query(sql, params=params)

    # ------------------------------------------------------------------
    # Memory (Fact + Entity)
    # ------------------------------------------------------------------

    def add_fact(
        self,
        content: str,
        category: str = "general",
        tags: Optional[List[str]] = None,
        entities: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        emb = self._embedder.embed([content])[0]

        self._db.execute(
            "INSERT INTO Fact SET "
            "content = :c, "
            "category = :cat, "
            "tags = :tags, "
            f"embedding = {_vec(emb.dense)}, "
            "created_at = :ts, "
            "updated_at = :ts2",
            params={
                "c": content,
                "cat": category,
                "tags": json.dumps(tags or []),
                "ts": _now(),
                "ts2": _now(),
            },
        )

        rows = self._db.query(
            "SELECT FROM Fact WHERE content = :c LIMIT 1",
            params={"c": content},
        )
        if not rows:
            raise RuntimeError("Failed to insert Fact")
        fact = rows[0]

        if entities:
            for name in entities:
                self._ensure_entity(name)
                self._db.execute(
                    "CREATE EDGE HAS_FACT FROM "
                    "(SELECT FROM Entity WHERE name = :en) "
                    "TO (SELECT FROM Fact WHERE @rid = :rid)",
                    params={"en": name, "rid": str(fact["@rid"])},
                )
        return fact

    def search_facts(
        self,
        query: str,
        top_k: int = 10,
        category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        q = self._embedder.embed_query(query)
        params: Dict[str, Any] = {"qv": q.dense, "tk": top_k}

        if category:
            sql = (
                "SELECT FROM Fact WHERE @rid IN ["
                "  expand(`vector.neighbors`('Fact[embedding]', :qv, :tk))"
                "] AND category = :cat"
            )
            params["cat"] = category
        else:
            sql = "SELECT expand(`vector.neighbors`('Fact[embedding]', :qv, :tk))"
        return self._db.query(sql, params=params)

    # ------------------------------------------------------------------
    # Entity
    # ------------------------------------------------------------------

    def _ensure_entity(self, name: str) -> Dict[str, Any]:
        rows = self._db.query(
            "SELECT FROM Entity WHERE name = :n", params={"n": name}
        )
        if rows:
            return rows[0]
        self._db.execute(
            "INSERT INTO Entity SET name = :n, created_at = :ts",
            params={"n": name, "ts": _now()},
        )
        rows = self._db.query(
            "SELECT FROM Entity WHERE name = :n", params={"n": name}
        )
        return rows[0]

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def ensure_profile(
        self, name: str, display_name: Optional[str] = None
    ) -> Dict[str, Any]:
        rows = self._db.query(
            "SELECT FROM Profile WHERE name = :n", params={"n": name}
        )
        if rows:
            return rows[0]
        self._db.execute(
            "INSERT INTO Profile SET "
            "name = :n, display_name = :dn, created_at = :ts",
            params={"n": name, "dn": display_name or name, "ts": _now()},
        )
        rows = self._db.query(
            "SELECT FROM Profile WHERE name = :n", params={"n": name}
        )
        return rows[0]

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._embedder.shutdown()
        self._db.close()
