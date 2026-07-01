"""ArcadeDB adapter over PostgreSQL wire protocol (psycopg).

Phase 2 of ArcadeDB native storage migration.
Replaces the legacy HTTP/JSON API with transactional PostgreSQL wire protocol.

Key improvements over HTTP API:
  - ACID transactions (BEGIN/COMMIT/ROLLBACK via transact())
  - Connection pooling (psycopg_pool, min=2, max=10)
  - dict_row factory (compatible with sqlite3.Row)
  - Prepared statements (auto-cached after 5 executions)
  - Vector SQL-literal workaround for ArcadeDB 26.7.1 Jackson bug

Links:
  Phase 2 spec: docs/arcadedb-migration/phase-2-adapter-v2.md
  Tests:        tests/test_arcadedb_adapter.py
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool, PoolTimeout
except ImportError:
    raise ImportError(
        "psycopg[binary] and psycopg-pool are required for ArcadeDB. "
        "Run: pip install 'psycopg[binary]>=3.1,<4' psycopg-pool"
    )

logger = logging.getLogger(__name__)


class ArcadeDBError(Exception):
    """Raised on any ArcadeDB communication or query error."""


@dataclass
class ArcadeDBConfig:
    """ArcadeDB connection configuration.

    Uses PostgreSQL wire protocol (port 5432) instead of HTTP API (2480).
    """
    host: str = "localhost"
    port: int = 5432                      # PostgreSQL wire protocol
    database: str = "hermes"
    user: str = "root"
    password: str = ""
    timeout: float = 30.0                 # connect + query timeout
    pool_min: int = 2                     # min pool connections
    pool_max: int = 10                    # max pool connections
    pool_timeout: float = 10.0            # timeout obtaining connection from pool


class ArcadeDBAdapter:
    """PostgreSQL wire protocol adapter for ArcadeDB.

    Uses psycopg 3.x with connection pool and dict_row factory.
    Supports transactions, prepared statements, and Cypher queries.
    Vectors are passed as SQL literals to work around a Jackson
    float[] deserialization bug in ArcadeDB 26.7.1.
    """

    def __init__(self, config: Optional[ArcadeDBConfig] = None) -> None:
        self._cfg = config or ArcadeDBConfig()
        self._pool: Optional[ConnectionPool] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """True if pool is initialized and at least one connection is live."""
        return self._pool is not None

    def connect(self) -> None:
        """Initialize the connection pool. Idempotent."""
        if self._pool is not None:
            return

        conninfo = (
            f"host={self._cfg.host} "
            f"port={self._cfg.port} "
            f"dbname={self._cfg.database} "
            f"user={self._cfg.user} "
            f"password={self._cfg.password} "
            f"connect_timeout={int(self._cfg.timeout)} "
            "sslmode=disable"
        )

        try:
            self._pool = ConnectionPool(
                conninfo=conninfo,
                min_size=self._cfg.pool_min,
                max_size=self._cfg.pool_max,
                timeout=self._cfg.pool_timeout,
                open=True,
                kwargs={
                    "autocommit": True,
                    "row_factory": dict_row,
                    "prepare_threshold": 5,
                },
            )
            self._check_health()
        except (PoolTimeout, psycopg.OperationalError) as e:
            if self._pool is not None:
                self._pool.close()
                self._pool = None
            raise ArcadeDBError(f"connection failed: {e}") from e

    def close(self) -> None:
        """Close the connection pool. Idempotent."""
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def _check_health(self) -> bool:
        """Verify ArcadeDB responds to SELECT 1."""
        try:
            conn = self._pool.getconn()
            try:
                conn.execute("SELECT 1")
                return True
            finally:
                self._pool.putconn(conn)
        except (PoolTimeout, psycopg.OperationalError) as e:
            raise ArcadeDBError(f"health check failed: {e}") from e

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def get_conn(self):
        """Get a connection from the pool.

        For low-level use by transact() and execute().
        Caller must call put_conn() afterwards.
        """
        if self._pool is None:
            raise ArcadeDBError("not connected")
        return self._pool.getconn()

    def put_conn(self, conn) -> None:
        """Return a connection to the pool."""
        if self._pool is not None:
            self._pool.putconn(conn)

    # ------------------------------------------------------------------
    # Transaction API (CRITICAL)
    # ------------------------------------------------------------------

    def transact(self, fn):
        """Execute fn(cursor) inside a single atomic transaction.

        If fn raises any exception, the transaction is rolled back
        and the exception is re-raised.  On success, the transaction
        is committed and fn's return value is returned to the caller.

        Usage::

            def _do(cur):
                cur.execute("INSERT INTO ...")
                cur.execute("UPDATE ...")
                return cur.fetchall()

            rows = adapter.transact(_do)
        """
        if self._pool is None:
            raise ArcadeDBError("not connected")

        conn = self._pool.getconn()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN")
            result = fn(cur)
            cur.execute("COMMIT")
            return result
        except Exception:
            try:
                cur.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            cur.close()
            self._pool.putconn(conn)
        # ------------------------------------------------------------------
        # Query API
        # ------------------------------------------------------------------
        def execute(
            self,
            sql: str,
            params = None,
            language: str = "sql",
        ) -> List[Dict[str, Any]]:
            """Execute a SQL command.

            ArcadeDB supports only "simple" query mode (no extended protocol).
            Dict params are automatically converted to string formatting.
            Tuple params (<5) work via psycopg bind.

            Args:
                sql: SQL string.
                params: dict or tuple of parameters (or None).
                language: 'sql', 'cypher', or 'sqlscript'.
            """
            if self._pool is None:
                raise ArcadeDBError("not connected")

            if isinstance(params, dict):
                sql = self._fmt(sql, params)
                params = None

            conn = self._pool.getconn()
            cur = conn.cursor()
            try:
                if language == "cypher":
                    sql = "{cypher} " + sql

                cur.execute(sql, params)

                try:
                    if cur.description is not None:
                        rows = [dict(r) for r in cur.fetchall()]
                    else:
                        rows = [{"rowcount": cur.rowcount}]
                except Exception:
                    rows = []
                return rows
            except Exception as e:
                raise ArcadeDBError(str(e)) from e
            finally:
                cur.close()
                self._pool.putconn(conn)

        @staticmethod
        def _fmt(sql: str, params: dict) -> str:
            """Convert dict-param SQL to string formatting.

            Replaces %(name)s placeholders with _q()-escaped values
            from the params dict.
            """
            import re
            def _repl(m):
                key = m.group(1)
                val = params.get(key)
                if val is None:
                    return "NULL"
                if isinstance(val, str):
                    escaped = val.replace("\\", "\\\\").replace("'", "\\'")
                    return f"'{escaped}'"
                if isinstance(val, (int, float)):
                    return str(val)
                return f"'{val}'"
            return re.sub(r"%\((\w+)\)s", _repl, sql)

    def query(
        self,
        sql: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """SELECT shortcut — equivalent to execute(sql, 'sql', params)."""
        return self.execute(sql, params, language="sql")

    def execute_script(self, script: str) -> List[Dict[str, Any]]:
        """Execute a multi-statement SQL script."""
        return self.execute(script, language="sqlscript")

    # ------------------------------------------------------------------
    # Vector workaround (ArcadeDB 26.7.1 Jackson float[] bug)
    # ------------------------------------------------------------------

    @staticmethod
    def _vec(val: List[float]) -> str:
        """Format a float list as a JSON-array SQL literal.

        Workaround for ArcadeDB 26.7.1-SNAPSHOT Jackson bug:
        vector arrays CANNOT be passed through parameter binding
        (:name / ?) because Jackson deserialises JSON-array elements
        as Java float[] primitives instead of Double objects, and
        the LSM_VECTOR index rejects those.

        Usage::

            sql = f"INSERT INTO Message SET embedding = {ArcadeDBAdapter._vec(emb)}"

        Args:
            val: List[float] — embedding vector.

        Returns:
            JSON-array string suitable for direct SQL interpolation.
        """
        return json.dumps([float(x) for x in val], allow_nan=False)

    @staticmethod
    def _parse_vec(s: str) -> List[float]:
        """Parse a JSON-array SQL literal back to List[float]."""
        return json.loads(s)
