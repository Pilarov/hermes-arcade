"""ArcadeDB adapter over PostgreSQL wire protocol (psycopg).

Phase 2 of ArcadeDB native storage migration.
Uses psycopg 3.x with connection pool and "simple" query mode
(ArcadeDB does not support extended protocol / prepared statements).

Key features:
  - autocommit=True (ArcadeDB PG plugin limitation)
  - SQL-level BEGIN/COMMIT/ROLLBACK for transactions
  - dict params auto-converted to string formatting (_fmt)
  - Vector SQL-literal workaround for Jackson float[] bug
  - Connection pool (min=2, max=10)

Links:
  Phase 2 spec: docs/arcadedb-migration/phase-2-adapter-v2.md
  Tests:        tests/test_arcadedb_adapter.py
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

logger = logging.getLogger(__name__)


class ArcadeDBError(Exception):
    """Raised on any ArcadeDB communication or query error."""


@dataclass
class ArcadeDBConfig:
    host: str = "localhost"
    port: int = 5432
    database: str = "hermes"
    user: str = "root"
    password: str = ""
    timeout: float = 30.0
    pool_min: int = 2
    pool_max: int = 10
    pool_timeout: float = 10.0


class ArcadeDBAdapter:

    def __init__(self, config: Optional[ArcadeDBConfig] = None) -> None:
        self._cfg = config or ArcadeDBConfig()
        self._pool: Optional[ConnectionPool] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._pool is not None

    def connect(self) -> None:
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
                },
            )
            self._check_health()
        except (PoolTimeout, psycopg.OperationalError) as e:
            if self._pool is not None:
                self._pool.close()
                self._pool = None
            raise ArcadeDBError(f"connection failed: {e}") from e

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def _check_health(self) -> bool:
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
        if self._pool is None:
            raise ArcadeDBError("not connected")
        return self._pool.getconn()

    def put_conn(self, conn) -> None:
        if self._pool is not None:
            self._pool.putconn(conn)

    # ------------------------------------------------------------------
    # Transaction API
    # ------------------------------------------------------------------

    def transact(self, fn):
        """Execute fn(cursor) inside BEGIN/COMMIT transaction.

        ArcadeDB PG plugin supports only simple query mode.
        We use SQL-level BEGIN/COMMIT/ROLLBACK instead of
        psycopg autocommit toggling.
        """
        if self._pool is None:
            raise ArcadeDBError("not connected")

        conn = self._pool.getconn()
        cur = conn.cursor()
        try:
            cur.execute("BEGIN")
            result = fn(cur)
            cur.execute("COMMIT")
            cur.close()
            self._pool.putconn(conn)
            return result
        except Exception:
            try:
                cur.execute("ROLLBACK")
            except Exception:
                pass
            cur.close()
            conn.close()  # discard bad connection
            raise

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def execute(
        self,
        sql: str,
        params=None,
        language: str = "sql",
    ) -> List[Dict[str, Any]]:
        """Execute a SQL command.

        ArcadeDB supports only "simple" query mode (no extended protocol).
        Dict params are auto-converted to string formatting via _fmt().
        Tuple params passed as-is (works for 1-3 simple params).

        Args:
            sql: SQL string with psycopg placeholders.
            params: dict, tuple, or None.
            language: 'sql', 'cypher', or 'sqlscript'.
        """
        if self._pool is None:
            raise ArcadeDBError("not connected")

        if isinstance(params, dict):
            sql = self._fmt(sql, params)
            params = None
        elif isinstance(params, (tuple, list)):
            sql = self._fmt_tuple(sql, params)
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
            err = str(e)
            if "Transaction not active" in err or "got no result" in err:
                cur.close()
                try: conn.close()
                except: pass
                # ArcadeDB server may have stale session state.
                # Use a fresh standalone connection.
                import psycopg as pg_raw
                try:
                    raw_conn = pg_raw.connect(
                        host=self._cfg.host, port=self._cfg.port,
                        dbname=self._cfg.database,
                        user=self._cfg.user, password=self._cfg.password,
                        connect_timeout=5, sslmode="disable",
                        autocommit=True, row_factory=dict_row,
                    )
                    raw_cur = raw_conn.cursor()
                    # Clear any stale server-side transaction state
                    try: raw_cur.execute("ROLLBACK")
                    except: pass
                    try: raw_cur.execute("COMMIT")
                    except: pass
                    raw_cur.execute(sql, params)
                    try:
                        rows = [dict(r) for r in raw_cur.fetchall()] if raw_cur.description else []
                    except:
                        rows = []
                    return rows
                finally:
                    try: raw_cur.close()
                    except: pass
                    try: raw_conn.close()
                    except: pass
            raise ArcadeDBError(err) from e
        finally:
            cur.close()
            self._pool.putconn(conn)

    def query(
        self,
        sql: str,
        params=None,
    ) -> List[Dict[str, Any]]:
        """SELECT shortcut."""
        return self.execute(sql, params, language="sql")

    def execute_script(self, script: str) -> List[Dict[str, Any]]:
        """Execute multi-statement script."""
        return self.execute(script, language="sqlscript")

    # ------------------------------------------------------------------
    # Dict-param → string formatting (ArcadeDB simple query compat)
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt(sql: str, params: dict) -> str:
        """Replace %(name)s placeholders with SQL-escaped string values.

        ArcadeDB does NOT support extended query protocol, so
        bind parameters (prepared statements) are not available.
        This method converts dict-param queries to plain SQL strings
        with inlined, properly escaped values.
        """
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

    @staticmethod
    def _fmt_tuple(sql: str, params: tuple) -> str:
        """Replace %s placeholders with SQL-escaped values from a tuple."""
        vals = list(params)
        def _repl(m):
            if not vals:
                return m.group(0)
            val = vals.pop(0)
            if val is None:
                return "NULL"
            if isinstance(val, str):
                escaped = val.replace("\\", "\\\\").replace("'", "\\'")
                return f"'{escaped}'"
            if isinstance(val, (int, float)):
                return str(val)
            return f"'{val}'"
        # Only replace %s that are bind placeholders (not inside strings)
        return re.sub(r"(?<!')(?<!%)(?<!\w)%s(?!\w)", _repl, sql)

    # ------------------------------------------------------------------
    # Vector workaround (ArcadeDB Jackson float[] bug)
    # ------------------------------------------------------------------

    @staticmethod
    def _vec(val: List[float]) -> str:
        """Format float list as JSON-array SQL literal.

        Workaround for ArcadeDB 26.7.1 Jackson bug:
        vector arrays CANNOT be passed through parameter binding.
        """
        return json.dumps([float(x) for x in val], allow_nan=False)

    @staticmethod
    def _parse_vec(s: str) -> List[float]:
        """Parse JSON-array SQL literal back to List[float]."""
        return json.loads(s)
