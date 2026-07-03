"""ArcadeDB Lifecycle Manager — auto-managed Docker container.

Phase 0 of ArcadeDB native storage migration.
Manages lifecycle: start, health check, stop, schema init.

Links:
  Phase 0 spec: docs/arcadedb-migration/phase-0-lifecycle.md
  Config:       hermes_cli/config.py (database.arcadedb.*)
  Schema:       hermes_cli/arcadedb_schema.py (SchemaManager)
  Tests:        tests/test_arcadedb_lifecycle.py (Phase 1)

Usage:
  lifecycle = ArcadeDBLifecycle.from_config()
  lifecycle.ensure_started()      # idempotent start + health wait
  # ... agent work ...
  lifecycle.stop()                # graceful shutdown
"""

from __future__ import annotations

import logging
import os
import secrets
import subprocess
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_CONTAINER_NAME = "hermes-arcadedb"


class ArcadeDBLifecycleError(Exception):
    """Raised when ArcadeDB lifecycle operations fail."""


@dataclass
class ArcadeDBLifecycleConfig:
    enabled: bool = False
    auto_start: bool = True
    host: str = "localhost"
    port: int = 5432
    http_port: int = 2480
    database: str = "hermes"
    user: str = "root"
    password: str = ""
    docker_image: str = "arcadedb/arcadedb:26.7.1"
    memory_limit: str = "4g"
    data_dir: str = ""
    timeout: float = 30.0


class ArcadeDBLifecycle:

    def __init__(self, config: ArcadeDBLifecycleConfig | None = None):
        self._cfg = config or ArcadeDBLifecycleConfig()
        self._ensure_password()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ensure_started(self) -> bool:
        """Idempotent: ensure ArcadeDB is running and healthy.

        Called by CLI and gateway at startup when
        `database.arcadedb.enabled` is True.

        Returns True if ArcadeDB is ready, False if auto_start=False
        and container is not running.
        """
        if not self._cfg.enabled:
            logger.debug("ArcadeDB is disabled in config")
            return False

        if not self.check_docker():
            if self._cfg.auto_start:
                raise ArcadeDBLifecycleError(
                    "Docker is required for ArcadeDB auto-start. "
                    "Install Docker or set database.arcadedb.auto_start=false."
                )
            logger.warning("Docker not available and auto_start=False, ArcadeDB not started")
            return False

        if self.is_running():
            logger.debug("ArcadeDB container already running")
        else:
            if not self._cfg.auto_start:
                logger.info("ArcadeDB not running and auto_start=False")
                return False
            self.start()

        if not self.wait_healthy(timeout=self._cfg.timeout):
            raise ArcadeDBLifecycleError(
                f"ArcadeDB did not become healthy within {self._cfg.timeout}s"
            )

        logger.info("ArcadeDB is healthy (port %s)", self._cfg.port)
        return True

    def start(self) -> None:
        """Start the ArcadeDB Docker container."""
        if self.is_running():
            logger.debug("Container %s already running, skipping start", _CONTAINER_NAME)
            return

        data_dir = self._cfg.data_dir or self._default_data_dir()
        os.makedirs(data_dir, exist_ok=True)

        cmd = [
            "docker", "run", "-d",
            "--name", _CONTAINER_NAME,
            "-p", f"{self._cfg.port}:5432",
            "-p", f"{self._cfg.http_port}:2480",
            "-e", f'JAVA_OPTS=-Darcadedb.server.rootPassword={self._cfg.password} '
                   f'-Darcadedb.server.plugins=Postgres:com.arcadedb.postgres.PostgresProtocolPlugin '
                   f'-Darcadedb.server.defaultDatabases={self._cfg.database}[root:{self._cfg.password}] '
                   f'-Xmx{self._cfg.memory_limit} -Xms256m',
            "-v", f"{data_dir}:/storage",
            "--restart", "unless-stopped",
            self._cfg.docker_image,
        ]

        logger.info("Starting ArcadeDB container: %s", " ".join(cmd))
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            raise ArcadeDBLifecycleError(
                f"Failed to start ArcadeDB container: {e.stderr.strip()}"
            ) from e

    def stop(self) -> None:
        """Gracefully stop the ArcadeDB container."""
        if not self.is_running():
            logger.debug("Container %s not running, skipping stop", _CONTAINER_NAME)
            return

        logger.info("Stopping ArcadeDB container...")
        try:
            subprocess.run(
                ["docker", "stop", "-t", "30", _CONTAINER_NAME],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as e:
            logger.warning("Failed to stop container: %s", e.stderr.strip())

    def restart(self) -> None:
        """Stop + start."""
        self.stop()
        self.start()

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        """Check if the Docker container is running."""
        try:
            result = subprocess.run(
                [
                    "docker", "ps",
                    "--filter", f"name={_CONTAINER_NAME}",
                    "--format", "{{.Status}}",
                ],
                capture_output=True, text=True, check=True,
            )
            return result.stdout.strip().startswith("Up")
        except subprocess.CalledProcessError:
            return False

    def is_healthy(self) -> bool:
        """Check if ArcadeDB responds via HTTP API."""
        try:
            import httpx
            resp = httpx.post(
                f"http://{self._cfg.host}:2480/api/v1/command/{self._cfg.database}",
                json={"language": "sql", "command": "SELECT 1"},
                headers={"Authorization": f"Basic {self._http_auth()}"},
                timeout=3,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def _http_auth(self) -> str:
        import base64
        creds = f"{self._cfg.user}:{self._cfg.password}"
        return base64.b64encode(creds.encode()).decode()

    def wait_healthy(self, timeout: float = 30.0, interval: float = 2.0) -> bool:
        """Poll is_healthy() until timeout.

        Returns True when healthy, raises TimeoutError otherwise.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_healthy():
                return True
            time.sleep(interval)
        return False

    # ------------------------------------------------------------------
    # Docker
    # ------------------------------------------------------------------

    def check_docker(self) -> bool:
        """Check if Docker CLI is available."""
        try:
            subprocess.run(
                ["docker", "--version"],
                capture_output=True, check=True,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def ensure_schema(self) -> None:
        """Create database and schema if not already present.

        Uses SchemaManager from arcadedb_schema.py (lazy import to avoid
        circular dependency with ArcadeDBAdapter from Phase 2).
        """
        from hermes_cli.arcadedb_schema import SchemaManager
        from hermes_cli.arcadedb import ArcadeDBConfig as AdapterConfig
        from hermes_cli.arcadedb import ArcadeDBAdapter

        cfg = AdapterConfig(
            host=self._cfg.host,
            port=self._cfg.port,
            database=self._cfg.database,
            user=self._cfg.user,
            password=self._cfg.password,
            timeout=self._cfg.timeout,
        )
        adapter = ArcadeDBAdapter(cfg)
        try:
            adapter.connect()
            manager = SchemaManager(adapter)
            manager.create_all()
            logger.info("ArcadeDB schema initialized")
        finally:
            adapter.close()

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls) -> ArcadeDBLifecycle:
        """Factory: read database.arcadedb.* from config.yaml."""
        from hermes_cli.config import load_config

        config = load_config()
        arcadedb_cfg = config.get("database", {}).get("arcadedb", {})

        return cls(ArcadeDBLifecycleConfig(
            enabled=arcadedb_cfg.get("enabled", False),
            auto_start=arcadedb_cfg.get("auto_start", True),
            host=arcadedb_cfg.get("host", "localhost"),
            port=arcadedb_cfg.get("port", 5432),
            http_port=arcadedb_cfg.get("http_port", 2480),
            database=arcadedb_cfg.get("database", "hermes"),
            user=arcadedb_cfg.get("user", "root"),
            password=arcadedb_cfg.get("password", ""),
            docker_image=arcadedb_cfg.get("docker_image", "arcadedb/arcadedb:26.7.1"),
            memory_limit=arcadedb_cfg.get("memory_limit", "4g"),
            data_dir=arcadedb_cfg.get("data_dir", ""),
            timeout=arcadedb_cfg.get("timeout", 30.0),
        ))

    @staticmethod
    def is_enabled() -> bool:
        """Check if database.arcadedb.enabled is True in config.yaml."""
        try:
            from hermes_cli.config import load_config
            config = load_config()
            return config.get("database", {}).get("arcadedb", {}).get("enabled", False)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _ensure_password(self) -> None:
        """Generate and persist password if empty."""
        if not self._cfg.password:
            self._cfg.password = secrets.token_hex(16)
            self._save_config()
            logger.info("Generated new ArcadeDB password")

    def _save_config(self) -> None:
        """Persist password back to config.yaml."""
        try:
            from pathlib import Path
            from ruamel.yaml import YAML

            from hermes_constants import get_hermes_home

            config_path = get_hermes_home() / "config.yaml"
            if not config_path.exists():
                logger.debug("config.yaml not found, cannot persist password")
                return

            yaml = YAML()
            yaml.preserve_quotes = True
            with open(config_path, encoding="utf-8") as f:
                data = yaml.load(f)

            data.setdefault("database", {}).setdefault("arcadedb", {})[
                "password"
            ] = self._cfg.password

            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(data, f)

            logger.debug("ArcadeDB password persisted to config.yaml")
        except Exception as e:
            logger.warning("Failed to persist ArcadeDB password: %s", e)

    @staticmethod
    def _default_data_dir() -> str:
        """Return default ArcadeDB data directory."""
        from hermes_constants import get_hermes_home

        return str(get_hermes_home() / "arcadedb" / "data")


# ------------------------------------------------------------------
# Module-level singleton
# ------------------------------------------------------------------

_LIFECYCLE: ArcadeDBLifecycle | None = None


def get_lifecycle() -> ArcadeDBLifecycle:
    """Get or create the module-level lifecycle singleton."""
    global _LIFECYCLE
    if _LIFECYCLE is None:
        _LIFECYCLE = ArcadeDBLifecycle.from_config()
    return _LIFECYCLE


def reset_lifecycle() -> None:
    """Reset the singleton (for tests)."""
    global _LIFECYCLE
    _LIFECYCLE = None
