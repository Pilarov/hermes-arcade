"""ArcadeDB adapter over PostgreSQL wire protocol (psycopg).

Phase 2 of ArcadeDB native storage migration.
Uses psycopg 3.x with connection pool and "simple" query mode
(ArcadeDB does not support extended protocol / prepared statements).

Key features:
  - autocommit=True (ArcadeDB PG plugin limitation)
  - SQL-level BEGIN/COMMIT/ROLLBACK for transactions
  - dict params auto-converted to string formatting (_fmt)
  - Vector SQL-literal workaround for Jackson float[] bug
  - Connection pool (min=2, max=10) with aggressive reset on corruption
  - Pool reset counter: after ~15 transact() calls, full pool recreation
  - Fresh psycopg connections for transact() to isolate from pool

Links:
  Phase 2 spec: docs/arcadedb-migration/phase-2-adapter-v2.md
  Tests:        tests/test_arcadedb_adapter.py
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

logger = logging.getLogger(__name__)

# Retry config (TD-7)
_MAX_RETRIES = 3
_RETRY_DELAY_S = 0.1


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

    # Pool reset after this many transact() calls to avoid server-side corruption
    _POOL_RESET_EVERY = 15

    def __init__(self, config: Optional[ArcadeDBConfig] = None) -> None:
        self._cfg = config or ArcadeDBConfig()
        self._pool: Optional[ConnectionPool] = None
        self._lock = threading.RLock()
        self._transact_count = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._pool is not None

    def connect(self) -> None:
        with self._lock:
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
        with self._lock:
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

    def _reset_pool_if_needed(self) -> None:
        """Periodically close and recreate the pool to clear server-side
        transaction state corruption (ArcadeDB PG simple query mode)."""
        self._transact_count += 1
        if self._transact_count >= self._POOL_RESET_EVERY and self._pool is not None:
            logger.debug("Resetting connection pool after %s transact calls",
                         self._transact_count)
            self._transact_count = 0
            old_pool = self._pool
            self._pool = None
            try:
                old_pool.close()
            except Exception:
                pass
            time.sleep(0.1)
            self.connect()

    # ------------------------------------------------------------------
    # Transaction API
    # ------------------------------------------------------------------

    def transact(self, fn):
        """Execute fn(cursor) via fresh psycopg connection.

        Uses a standalone connection (not the pool) for each transaction.
        Periodically resets the connection pool to prevent server-side
        transaction state accumulation (ArcadeDB PG simple query mode).

        Note: autocommit=True — each statement auto-commits. BEGIN/COMMIT
        are sent but effectively no-ops in simple query mode.
        """
        self._reset_pool_if_needed()
        conn = psycopg.connect(
            host=self._cfg.host, port=self._cfg.port,
            dbname=self._cfg.database,
            user=self._cfg.user, password=self._cfg.password,
            connect_timeout=5, sslmode="disable",
            autocommit=True, row_factory=dict_row,
        )
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
            conn.close()

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
        """Replace %s placeholders with SQL-escaped values from a tuple.

        Uses a simple replace-one-at-a-time loop instead of regex/split
        to avoid issues with %s inside string literals.
        """
        result = []
        param_idx = 0
        param_list = list(params)
        i = 0
        while i < len(sql):
            if sql[i:i+2] == "%s" and param_idx < len(param_list):
                val = param_list[param_idx]
                param_idx += 1
                if val is None:
                    result.append("NULL")
                elif isinstance(val, str):
                    escaped = val.replace("\\", "\\\\").replace("'", "\\'")
                    result.append(f"'{escaped}'")
                elif isinstance(val, (int, float)):
                    result.append(str(val))
                else:
                    result.append(f"'{val}'")
                i += 2
            else:
                result.append(sql[i])
                i += 1
        result.append(sql[i:]) if i < len(sql) else None
        return "".join(result)

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

    # ------------------------------------------------------------------
    # Retry helper (TD-7)
    # ------------------------------------------------------------------

    def _retry(self, fn, *args, **kwargs):
        """Execute fn with retry on transient errors."""
        last_err = None
        for attempt in range(_MAX_RETRIES):
            try:
                return fn(*args, **kwargs)
            except ArcadeDBError as e:
                err = str(e)
                if "connection" in err.lower() or "timeout" in err.lower():
                    last_err = e
                    logger.debug("Retry %s/%s: %s", attempt + 1, _MAX_RETRIES, e)
                    time.sleep(_RETRY_DELAY_S * (attempt + 1))
                    continue
                raise
        raise last_err
