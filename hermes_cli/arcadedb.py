"""ArcadeDB adapter: PostgreSQL wire protocol (reads) + HTTP API (writes).

Phase 2 of ArcadeDB native storage migration.

Read path:  psycopg 3.x connection pool (PG wire protocol, port 5432).
Write path: HTTP API (ArcadeDB REST, port 2480).

The split avoids PG simple query mode pool corruption. Write operations
(INSERT/UPDATE/DELETE/CREATE/DROP) go to the HTTP API. Read operations
(SELECT) stay on the PG pool.

Key features:
  - autocommit=True (ArcadeDB PG plugin limitation)
  - dict params auto-converted to string formatting (_fmt)
  - Vector SQL-literal workaround for Jackson float[] bug
  - Connection pool (min=2, max=10) for reads only
  - HttpCursor: cursor-like wrapper that sends SQL via HTTP API
  - transact() uses HttpCursor — no PG pool involvement

Links:
  Phase 2 spec: docs/arcadedb-migration/phase-2-adapter-v2.md
  Tests:        tests/test_arcadedb_adapter.py
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
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

    _WRITE_KEYWORDS = (
        "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER",
        "TRUNCATE", "GRANT", "REVOKE",
    )

    def __init__(self, config: Optional[ArcadeDBConfig] = None) -> None:
        self._cfg = config or ArcadeDBConfig()
        self._pool: Optional[ConnectionPool] = None
        self._lock = threading.RLock()
        self._http: Optional[httpx.Client] = None

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

            # Create database via HTTP if it doesn't exist
            self._ensure_database()

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
            if self._http is not None:
                self._http.close()
                self._http = None

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
    # HTTP API — write path (avoids PG pool corruption entirely)
    # ------------------------------------------------------------------

    def _http_client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                base_url=f"http://{self._cfg.host}:2480",
                timeout=httpx.Timeout(self._cfg.timeout),
            )
        return self._http

    def _http_auth(self) -> str:
        creds = f"{self._cfg.user}:{self._cfg.password}"
        return base64.b64encode(creds.encode()).decode()

    def _ensure_database(self) -> None:
        """Create database via HTTP if it doesn't exist (idempotent)."""
        try:
            client = self._http_client()
            auth = self._http_auth()
            db = self._cfg.database
            client.post(
                "/api/v1/server",
                json={"command": f"create database {db}"},
                headers={"Authorization": f"Basic {auth}"},
            )
        except Exception as e:
            logger.debug("_ensure_database: %s", e)

    def _http_auth(self) -> str:
        creds = f"{self._cfg.user}:{self._cfg.password}"
        return base64.b64encode(creds.encode()).decode()

    @classmethod
    def _is_write(cls, sql: str) -> bool:
        s = sql.lstrip().upper()
        if s.startswith("{CYPHER}"):
            s = s[len("{CYPHER}"):].lstrip()
        return any(s.startswith(kw) for kw in cls._WRITE_KEYWORDS)

    def _http_execute(self, sql: str) -> List[Dict[str, Any]]:
        client = self._http_client()
        resp = client.post(
            f"/api/v1/command/{self._cfg.database}",
            json={"language": "sql", "command": sql},
            headers={"Authorization": f"Basic {self._http_auth()}"},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("result"):
            result = data["result"]
            if isinstance(result, list):
                return [dict(r) if isinstance(r, dict) else {"value": r}
                        for r in result]
            return [{"result": str(result)}]
        return []

    class HttpCursor:
        """Cursor-like object that sends SQL via HTTP API.

        Each execute() sends a single SQL statement to the ArcadeDB REST
        API (port 2480). This avoids PG simple query mode entirely for
        writes while providing the same auto-commit semantics.
        """
        def __init__(self, adapter: "ArcadeDBAdapter"):
            self._adapter = adapter
            self._last_rows: List[Dict[str, Any]] = []
            self.rowcount = 0
            self.description = None

        def execute(self, sql: str, params=None) -> None:
            self._last_rows = self._adapter._http_execute(sql)
            self.rowcount = len(self._last_rows)

        def fetchall(self) -> List[Dict[str, Any]]:
            return self._last_rows

        def fetchone(self) -> Optional[Dict[str, Any]]:
            return self._last_rows[0] if self._last_rows else None

        def close(self) -> None:
            pass

    # ------------------------------------------------------------------
    # Transaction API
    # ------------------------------------------------------------------

    def transact(self, fn):
        """Execute fn(cursor) via HTTP API (ArcadeDB REST, port 2480).

        Each SQL statement in fn(cursor) is sent individually via HTTP.
        Semantics match PG simple query mode: each statement auto-commits.
        No PG pool involvement — completely avoids pool corruption.
        """
        cur = ArcadeDBAdapter.HttpCursor(self)
        return fn(cur)

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

        Write operations go to HTTP API (port 2480). Read operations use
        the PG connection pool (port 5432). Dict/tuple params are
        auto-converted to string formatting (_fmt) for ArcadeDB compat.
        """
        if isinstance(params, dict):
            sql = self._fmt(sql, params)
            params = None
        elif isinstance(params, (tuple, list)):
            sql = self._fmt_tuple(sql, params)
            params = None

        if language == "cypher":
            sql = "{cypher} " + sql

        # Route writes to HTTP API to avoid PG pool corruption
        if self._is_write(sql):
            return self._http_execute(sql)

        if self._pool is None:
            raise ArcadeDBError("not connected")

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
