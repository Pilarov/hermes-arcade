"""Tests for ArcadeDBLifecycle (Phase 0).

Links:
  Phase 0: hermes_cli/arcadedb_lifecycle.py
  Phase 0 spec: docs/arcadedb-migration/phase-0-lifecycle.md
  Fixtures: tests/fixtures/arcadedb_fixtures.py
"""

import subprocess
from unittest.mock import MagicMock

import pytest

from hermes_cli.arcadedb_lifecycle import (
    ArcadeDBLifecycle,
    ArcadeDBLifecycleConfig,
    ArcadeDBLifecycleError,
    reset_lifecycle,
)


# ---------------------------------------------------------------------------
# Docker detection
# ---------------------------------------------------------------------------

class TestDockerDetection:
    def test_docker_available(self, monkeypatch):
        """L0-01: Docker CLI available -> check_docker() == True."""
        monkeypatch.setattr(
            subprocess, "run",
            MagicMock(return_value=MagicMock(stdout="Docker version 26.0.0")),
        )
        lc = ArcadeDBLifecycle(ArcadeDBLifecycleConfig())
        assert lc.check_docker() is True

    def test_docker_unavailable(self, monkeypatch):
        """L0-02: Docker CLI absent -> check_docker() == False."""
        monkeypatch.setattr(
            subprocess, "run",
            MagicMock(side_effect=FileNotFoundError("docker")),
        )
        lc = ArcadeDBLifecycle(ArcadeDBLifecycleConfig())
        assert lc.check_docker() is False


# ---------------------------------------------------------------------------
# Container lifecycle
# ---------------------------------------------------------------------------

class TestContainerLifecycle:
    def test_start_calls_docker(self, monkeypatch):
        """L0-04: start() runs docker with correct args."""
        mock_run = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(subprocess, "run", mock_run)
        monkeypatch.setattr(
            ArcadeDBLifecycle, "is_running", lambda s: False
        )
        monkeypatch.setattr(
            ArcadeDBLifecycle, "check_docker", lambda s: True
        )

        lc = ArcadeDBLifecycle(ArcadeDBLifecycleConfig(
            password="test-pass",
            docker_image="arcadedb/arcadedb:26.7.1",
        ))
        lc.start()

        assert mock_run.called
        cmd_str = " ".join(mock_run.call_args[0][0])
        assert "hermes-arcadedb" in cmd_str
        assert "arcadedb/arcadedb:26.7.1" in cmd_str
        assert "-Darcadedb.server.rootPassword=test-pass" in cmd_str

    def test_start_already_running(self, monkeypatch):
        """L0-08: start() on running container -> no-op."""
        mock_run = MagicMock()
        monkeypatch.setattr(subprocess, "run", mock_run)
        monkeypatch.setattr(
            ArcadeDBLifecycle, "is_running", lambda s: True
        )

        lc = ArcadeDBLifecycle(ArcadeDBLifecycleConfig())
        lc.start()
        mock_run.assert_not_called()

    def test_stop_calls_docker(self, monkeypatch):
        """Stop sends docker stop."""
        mock_run = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(subprocess, "run", mock_run)
        monkeypatch.setattr(
            ArcadeDBLifecycle, "is_running", lambda s: True
        )

        lc = ArcadeDBLifecycle(ArcadeDBLifecycleConfig())
        lc.stop()
        assert mock_run.called
        assert "hermes-arcadedb" in " ".join(mock_run.call_args[0][0])


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

class TestHealthCheck:
    def test_healthy_mock(self, monkeypatch):
        """L0-05: is_healthy() returns True when SELECT 1 succeeds."""
        monkeypatch.setattr(
            ArcadeDBLifecycle, "is_healthy", lambda s: True
        )
        lc = ArcadeDBLifecycle(ArcadeDBLifecycleConfig(timeout=1.0))
        assert lc.is_healthy() is True

    def test_unhealthy_mock(self):
        """L0-06: is_healthy() returns False without connection."""
        lc = ArcadeDBLifecycle(ArcadeDBLifecycleConfig(
            host="10.255.255.1", port=9999, timeout=0.5,
        ))
        assert lc.is_healthy() is False

    def test_wait_healthy_timeout(self, monkeypatch):
        """L0-07: wait_healthy() raises timeout when never healthy."""
        monkeypatch.setattr(
            ArcadeDBLifecycle, "is_healthy", lambda s: False
        )
        lc = ArcadeDBLifecycle(ArcadeDBLifecycleConfig(timeout=1.0))
        assert lc.wait_healthy(timeout=0.3, interval=0.1) is False


# ---------------------------------------------------------------------------
# ensure_started
# ---------------------------------------------------------------------------

class TestEnsureStarted:
    def test_disabled_skips(self):
        """ensure_started() with enabled=False returns False."""
        lc = ArcadeDBLifecycle(ArcadeDBLifecycleConfig(enabled=False))
        assert lc.ensure_started() is False

    def test_auto_start_false_not_running(self, monkeypatch):
        """auto_start=False, not running -> returns False, no error."""
        monkeypatch.setattr(
            ArcadeDBLifecycle, "check_docker", lambda s: True
        )
        monkeypatch.setattr(
            ArcadeDBLifecycle, "is_running", lambda s: False
        )
        lc = ArcadeDBLifecycle(ArcadeDBLifecycleConfig(
            enabled=True, auto_start=False,
        ))
        assert lc.ensure_started() is False

    def test_auto_start_true_no_docker(self, monkeypatch):
        """L0-02b: auto_start=True + no Docker -> ArcadeDBLifecycleError."""
        monkeypatch.setattr(
            ArcadeDBLifecycle, "check_docker", lambda s: False
        )
        lc = ArcadeDBLifecycle(ArcadeDBLifecycleConfig(
            enabled=True, auto_start=True,
        ))
        with pytest.raises(ArcadeDBLifecycleError, match="Docker is required"):
            lc.ensure_started()

    def test_already_running_healthy(self, monkeypatch):
        """ensure_started() on healthy running -> instant success."""
        monkeypatch.setattr(
            ArcadeDBLifecycle, "check_docker", lambda s: True
        )
        monkeypatch.setattr(
            ArcadeDBLifecycle, "is_running", lambda s: True
        )
        monkeypatch.setattr(
            ArcadeDBLifecycle, "is_healthy", lambda s: True
        )
        lc = ArcadeDBLifecycle(ArcadeDBLifecycleConfig(
            enabled=True, timeout=1.0,
        ))
        assert lc.ensure_started() is True


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestConfig:
    def test_password_generation(self, tmp_path, monkeypatch):
        """L0-12: empty password -> auto-generate 32 hex chars."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "database:\n  arcadedb:\n    password: ''\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            "hermes_constants.get_hermes_home", lambda: tmp_path
        )
        monkeypatch.setattr(
            "hermes_cli.arcadedb_lifecycle.ArcadeDBLifecycle._save_config",
            lambda s: None,
        )
        lifecycle = ArcadeDBLifecycle(ArcadeDBLifecycleConfig(password=""))
        assert len(lifecycle._cfg.password) == 32
        assert all(c in "0123456789abcdef" for c in lifecycle._cfg.password)
