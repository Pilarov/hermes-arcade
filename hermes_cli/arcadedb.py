"""ArcadeDB adapter — HTTP API only (ArcadeDB REST, port 2480).

Phase 2 of ArcadeDB native storage migration — unified HTTP adapter.

All SQL operations go through the ArcadeDB HTTP API. No PostgreSQL
wire protocol, no connection pool, no pool corruption. Stateless HTTP
requests with Basic auth.

Key features:
  - Single transport: httpx → POST /api/v1/command/{database}
  - dict params auto-converted to string formatting (_fmt)
  - Vector SQL-literal workaround for Jackson float[] bug
  - HttpCursor: cursor-like wrapper for transaction-style multi-SQL
  - transact() → HttpCursor (each statement individually via HTTP)
  - No pg dependency, no connection pool, no pool corruption

Links:
  Phase 2 spec: docs/arcadedb-migration/phase-2-adapter-v2.md
  Tests:        tests/test_arcadedb_adapter.py
"""

from __future__ import annotations

import base64
import json
import logging
import re
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

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

    class HttpCursor:
        """Cursor-like wrapper that sends SQL via HTTP API.

        Each execute() sends a single SQL statement via HTTP POST.
        fetchall()/fetchone() return results from the last execute().
        """
        def __init__(self, adapter: "ArcadeDBAdapter"):
            self._adapter = adapter
            self._last_rows: List[Dict[str, Any]] = []
            self.rowcount = 0
            self.description = None

        def execute(self, sql: str, params=None) -> None:
            if isinstance(params, dict):
                sql = ArcadeDBAdapter._fmt(sql, params)
            elif isinstance(params, (tuple, list)):
                sql = ArcadeDBAdapter._fmt_tuple(sql, params)
            self._last_rows = self._adapter._http_execute(sql)

        def execute_strict(self, sql: str) -> None:
            self._last_rows = self._adapter._http_execute_strict(sql)
            self.rowcount = len(self._last_rows)

        def fetchall(self) -> List[Dict[str, Any]]:
            return self._last_rows

        def fetchone(self) -> Optional[Dict[str, Any]]:
            return self._last_rows[0] if self._last_rows else None

        def close(self) -> None:
            pass

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def __init__(self, config: Optional[ArcadeDBConfig] = None) -> None:
        self._cfg = config or ArcadeDBConfig()
        self._lock = threading.RLock()
        self._http: Optional[httpx.Client] = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        with self._lock:
            if self._connected:
                return
            self._http_client()
            self._ensure_database()
            self._check_health()
            self._connected = True

    def _ensure_database(self) -> None:
        """Create database via HTTP if it doesn't exist (idempotent)."""
        try:
            client = self._http_client()
            auth = self._http_auth()
            client.post(
                "/api/v1/server",
                json={"command": f"create database {self._cfg.database}"},
                headers={"Authorization": f"Basic {auth}"},
            )
        except Exception:
            pass  # Database may already exist

    def close(self) -> None:
        with self._lock:
            if self._http is not None:
                self._http.close()
                self._http = None
            self._connected = False

    def _check_health(self) -> bool:
        try:
            rows = self._http_execute("SELECT 1")
            return bool(rows)
        except Exception as e:
            raise ArcadeDBError(f"health check failed: {e}") from e

    # Backward compat stubs (graph_store.py uses these)
    def get_conn(self):
        return None

    def put_conn(self, conn) -> None:
        pass

    # ------------------------------------------------------------------
    # HTTP transport — the single method everything routes through
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

    def _http_execute(self, sql: str) -> List[Dict[str, Any]]:
        """Execute SQL via HTTP, ignoring idempotency errors."""
        return self._http_send(sql, ignore_already_errors=True)

    def _http_execute_strict(self, sql: str) -> List[Dict[str, Any]]:
        """Execute SQL via HTTP, raising ALL errors (including duplicates).

        Used by CAS operations (try_acquire_compression_lock) that need
        to detect duplicate key violations.
        """
        return self._http_send(sql, ignore_already_errors=False)

    def _http_send(
        self, sql: str, ignore_already_errors: bool = True,
    ) -> List[Dict[str, Any]]:
        client = self._http_client()
        resp = client.post(
            f"/api/v1/command/{self._cfg.database}",
            json={"language": "sql", "command": sql},
            headers={"Authorization": f"Basic {self._http_auth()}"},
        )
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                detail = body.get("detail", "")
            except Exception:
                detail = resp.text
            if ignore_already_errors and any(marker in detail for marker in (
                "already exists", "already defined", "already assigned",
            )):
                return []
            raise ArcadeDBError(
                f"HTTP {resp.status_code}: {detail or resp.text[:200]}"
            )
        data = resp.json()
        if data.get("result"):
            result = data["result"]
            if isinstance(result, list):
                return [dict(r) if isinstance(r, dict) else {"value": r}
                        for r in result]
            return [{"result": str(result)}]
        return []

    def _http_send_script(self, script: str) -> List[Dict[str, Any]]:
        """Execute sqlscript batch via HTTP (implicit BEGIN/COMMIT)."""
        client = self._http_client()
        resp = client.post(
            f"/api/v1/command/{self._cfg.database}",
            json={"language": "sqlscript", "command": script},
            headers={"Authorization": f"Basic {self._http_auth()}"},
        )
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                detail = body.get("detail", "")
            except Exception:
                detail = resp.text
            raise ArcadeDBError(
                f"HTTP {resp.status_code}: {detail or resp.text[:200]}"
            )
        data = resp.json()
        if data.get("result"):
            result = data["result"]
            if isinstance(result, list):
                return [dict(r) if isinstance(r, dict) else {"value": r}
                        for r in result]
            return [{"result": str(result)}]
        return []

    # ------------------------------------------------------------------
    # Transaction API
    # ------------------------------------------------------------------

    def transact(self, fn):
        """Execute fn(cursor) via sqlscript batch (implicit BEGIN/COMMIT).

        All SQL statements in fn(cursor) are collected and sent as one
        HTTP POST with language='sqlscript'. ArcadeDB wraps in implicit
        BEGIN/COMMIT — data is visible across statements.

        fetchall()/fetchone() flush and return results. Any remaining
        SQL in the buffer is flushed after fn returns.
        """
        collector = ArcadeDBAdapter._SqlCollector(self)
        result = fn(collector)
        if collector._sqls:
            collector._flush()
        return result

    class _SqlCollector:
        """Collects SQL, sends as sqlscript batch on fetchall()."""
        def __init__(self, adapter):
            self._adapter = adapter
            self._sqls: list = []
            self._last_rows: list = []
            self.rowcount = 0
            self.description = None

        def execute(self, sql, params=None):
            if isinstance(params, dict):
                sql = ArcadeDBAdapter._fmt(sql, params)
            elif isinstance(params, (tuple, list)):
                sql = ArcadeDBAdapter._fmt_tuple(sql, params)
            self._sqls.append(sql)

        def execute_strict(self, sql):
            self._sqls.append(sql)

        def fetchall(self):
            self._flush()
            return self._last_rows

        def fetchone(self):
            self._flush()
            return self._last_rows[0] if self._last_rows else None

        def _flush(self):
            if not self._sqls:
                return
            script = ";".join(self._sqls)
            self._sqls = []
            self._last_rows = self._adapter._http_send_script(script)
            self.rowcount = len(self._last_rows)

        def close(self):
            pass

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def execute(
        self,
        sql: str,
        params=None,
        language: str = "sql",
    ) -> List[Dict[str, Any]]:
        """Execute a SQL command via HTTP API.

        Dict/tuple params are auto-converted to string formatting (_fmt).
        """
        if isinstance(params, dict):
            sql = self._fmt(sql, params)
        elif isinstance(params, (tuple, list)):
            sql = self._fmt_tuple(sql, params)

        if language == "cypher":
            sql = "{cypher} " + sql

        return self._http_execute(sql)

    def query(
        self,
        sql: str,
        params=None,
    ) -> List[Dict[str, Any]]:
        """SELECT shortcut."""
        return self.execute(sql, params, language="sql")

    def execute_script(self, script: str) -> List[Dict[str, Any]]:
        """Execute multi-statement script."""
        return self.execute(script, language="sql")

    # ------------------------------------------------------------------
    # Dict-param → string formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _fmt(sql: str, params: dict) -> str:
        """Replace %(name)s placeholders with SQL-escaped string values."""
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
    # Vector helper
    # ------------------------------------------------------------------

    @staticmethod
    def _vec(val: List[float]) -> str:
        """Format float list as JSON-array SQL literal."""
        return json.dumps([float(x) for x in val], allow_nan=False)

    @staticmethod
    def _parse_vec(s: str) -> List[float]:
        """Parse JSON-array SQL literal back to List[float]."""
        return json.loads(s)

    @staticmethod
    def _vec_to_bytes(vec: List[float]) -> Dict[str, str]:
        """Convert float32 list to $bytes typed marker for HTTP API.

        ArcadeDB HTTP API accepts vectors via typed markers
        (see docs: reference/http-api/http.html#vector-http-typed-markers).
        This packs float32 values into bytes and base64-encodes them.
        """
        packed = struct.pack(f'{len(vec)}f', *[float(x) for x in vec])
        return {"$bytes": base64.b64encode(packed).decode()}

    # ------------------------------------------------------------------
    # Retry helper
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
