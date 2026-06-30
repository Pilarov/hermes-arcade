"""ArcadeDB adapter over the HTTP/JSON API.

Uses ``httpx`` for synchronous requests against
``POST /api/v1/command/{database}``.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class ArcadeDBError(Exception):
    ...


@dataclass
class ArcadeDBConfig:
    host: str = "localhost"
    port: int = 2480
    database: str = "hermes"
    user: str = "root"
    password: str = "hermes123"
    timeout: float = 30.0


class ArcadeDBAdapter:
    def __init__(self, config: Optional[ArcadeDBConfig] = None) -> None:
        self._cfg = config or ArcadeDBConfig()
        auth = base64.b64encode(
            f"{self._cfg.user}:{self._cfg.password}".encode()
        ).decode()
        self._headers = {
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/json",
        }
        self._base = f"http://{self._cfg.host}:{self._cfg.port}"
        self._cmd_url = f"{self._base}/api/v1/command/{self._cfg.database}"
        self._client: Optional[httpx.Client] = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    def connect(self) -> None:
        self._client = httpx.Client(
            headers=self._headers,
            timeout=httpx.Timeout(self._cfg.timeout),
        )
        self._check()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _check(self) -> None:
        resp = self._client.get(f"{self._base}/api/v1/server")
        if resp.status_code != 200:
            raise ArcadeDBError(
                f"server unreachable: {resp.status_code}"
            )

    def execute(
        self,
        sql: str,
        language: str = "sql",
        params: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        if self._client is None:
            raise ArcadeDBError("not connected")
        body: Dict[str, Any] = {
            "language": language,
            "command": sql,
            "serializer": "record",
        }
        if params:
            body["params"] = params
        resp = self._client.post(self._cmd_url, json=body)
        if resp.status_code != 200:
            raise ArcadeDBError(
                f"command failed ({resp.status_code}): {resp.text}"
            )
        data = resp.json()
        return data.get("result", [])

    def query(
        self,
        sql: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        return self.execute(sql, language="sql", params=params)

    def execute_script(self, script: str) -> List[Dict[str, Any]]:
        return self.execute(script, language="sqlscript")
