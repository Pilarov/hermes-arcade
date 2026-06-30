"""Shared fixtures for ArcadeDB migration tests (Phase 1).

These fixtures support TDD contracts for Phases 2-8.
Modules not yet implemented will be gracefully skipped.

Phase dependencies:
  Phase 0: ArcadeDBLifecycle — available now
  Phase 2: ArcadeDBAdapter, ArcadeDBConfig — NOT YET (will skip)
  Phase 3: ArcadedbSessionDB — NOT YET (will skip)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Conditional imports — modules from future phases
# ---------------------------------------------------------------------------

try:
    from hermes_cli.arcadedb_lifecycle import (
        ArcadeDBLifecycle,
        ArcadeDBLifecycleConfig,
        reset_lifecycle,
    )
    HAS_LIFECYCLE = True
except ImportError:
    HAS_LIFECYCLE = False

try:
    from hermes_cli.arcadedb import ArcadeDBConfig, ArcadeDBAdapter, ArcadeDBError
    HAS_ADAPTER = True
except ImportError:
    HAS_ADAPTER = False

try:
    from hermes_cli.arcadedb_session import ArcadedbSessionDB
    HAS_SESSION = True
except ImportError:
    HAS_SESSION = False

try:
    from hermes_cli.embedder import EmbedderProvider, EmbeddingResult
    HAS_EMBEDDER = True
except ImportError:
    HAS_EMBEDDER = False
    EmbedderProvider = None
    EmbeddingResult = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_DOCKER_IMAGE = os.environ.get(
    "ARCADEDB_TEST_IMAGE", "arcadedb/arcadedb:26.7.1"
)
TEST_CONTAINER_NAME = "hermes-arcadedb-test"
TEST_DB = "hermes_test"
TEST_USER = "root"
TEST_PASSWORD = "test123"
TEST_PORT = int(os.environ.get("ARCADEDB_TEST_PORT", "5432"))
TEST_HOST = os.environ.get("ARCADEDB_TEST_HOST", "localhost")


# ---------------------------------------------------------------------------
# ArcadeDB container (scope=session)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def arcadedb_container():
    """Start ArcadeDB Docker container for the test session.

    In CI: uses ARCADEDB_TEST_HOST env var (external ArcadeDB).
    Locally: starts a Docker container, waits for health, cleans up.
    """
    import subprocess

    ci_host = os.environ.get("ARCADEDB_TEST_HOST")
    if ci_host:
        yield {"host": ci_host, "port": TEST_PORT}
        return

    # Local: try to start container
    try:
        subprocess.run(["docker", "--version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("Docker not available — set ARCADEDB_TEST_HOST for CI")

    # Remove any stale container
    subprocess.run(
        ["docker", "rm", "-f", TEST_CONTAINER_NAME],
        capture_output=True,
    )

    data_dir = Path.home() / ".hermes" / "arcadedb" / "test-data"
    data_dir.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.run(
            [
                "docker", "run", "-d",
                "--name", TEST_CONTAINER_NAME,
                "-p", f"{TEST_PORT}:5432",
                "-e", f"ARCADEDB_ROOT_PASSWORD={TEST_PASSWORD}",
                "-v", f"{data_dir}:/storage",
                TEST_DOCKER_IMAGE,
            ],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        pytest.skip(f"Docker daemon not available: {e.stderr.decode()[:120]}")

    # Wait for health
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            import psycopg
            conn = psycopg.connect(
                host="localhost", port=TEST_PORT,
                dbname=TEST_DB, user=TEST_USER, password=TEST_PASSWORD,
                connect_timeout=2,
            )
            conn.execute("SELECT 1")
            conn.close()
            break
        except Exception:
            time.sleep(2)
    else:
        subprocess.run(["docker", "rm", "-f", TEST_CONTAINER_NAME])
        pytest.fail("ArcadeDB container did not become healthy in 60s")

    yield {"host": "localhost", "port": TEST_PORT}

    # Cleanup
    subprocess.run(
        ["docker", "stop", "-t", "10", TEST_CONTAINER_NAME],
        capture_output=True,
    )
    subprocess.run(
        ["docker", "rm", "-f", TEST_CONTAINER_NAME],
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def arcadedb_config(arcadedb_container):
    """ArcadeDBConfig for the test container."""
    if not HAS_ADAPTER:
        pytest.skip("ArcadeDBAdapter not yet implemented (Phase 2)")
    return ArcadeDBConfig(
        host=arcadedb_container["host"],
        port=arcadedb_container["port"],
        database=TEST_DB,
        user=TEST_USER,
        password=TEST_PASSWORD,
        timeout=10.0,
    )


@pytest.fixture
def arcadedb_lifecycle_config():
    """Lifecycle config with test defaults."""
    return ArcadeDBLifecycleConfig(
        enabled=True,
        auto_start=False,       # don't auto-start in tests
        host=TEST_HOST,
        port=TEST_PORT,
        database=TEST_DB,
        user=TEST_USER,
        password=TEST_PASSWORD,
        timeout=10.0,
    )


# ---------------------------------------------------------------------------
# Adapter fixture (Phase 2)
# ---------------------------------------------------------------------------

@pytest.fixture
def arcadedb_adapter(arcadedb_config):
    """ArcadeDBAdapter connected to test ArcadeDB."""
    if not HAS_ADAPTER:
        pytest.skip("ArcadeDBAdapter not yet implemented (Phase 2)")
    adapter = ArcadeDBAdapter(arcadedb_config)
    adapter.connect()
    # Clean database before each test
    try:
        adapter.execute("DELETE VERTEX V")
    except Exception:
        pass
    yield adapter
    adapter.close()


# ---------------------------------------------------------------------------
# Embedder fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_embedder():
    """Mock EmbedderProvider returning deterministic vectors (1024d)."""
    embedder = MagicMock()
    embedder.name = "mock"
    embedder.dimensions = 1024
    embedder.embed.return_value = [
        MagicMock(dense=[0.1] * 1024, sparse=None)
    ]
    embedder.embed_query.return_value = MagicMock(
        dense=[0.2] * 1024, sparse=None
    )
    return embedder


@pytest.fixture
def real_embedder():
    """Real FastembedProvider (requires fastembed dependency)."""
    if not HAS_EMBEDDER:
        pytest.skip("EmbedderProvider not available")
    from hermes_cli.embedder import FastembedProvider
    provider = FastembedProvider()
    if not provider.is_available():
        pytest.skip("fastembed not installed")
    provider.initialize()
    yield provider
    provider.shutdown()


# ---------------------------------------------------------------------------
# SessionDB fixtures (Phase 3)
# ---------------------------------------------------------------------------

@pytest.fixture
def arcadedb_session(arcadedb_adapter, mock_embedder):
    """ArcadedbSessionDB with adapter + mock embedder."""
    if not HAS_SESSION:
        pytest.skip("ArcadedbSessionDB not yet implemented (Phase 3)")
    session = ArcadedbSessionDB(adapter=arcadedb_adapter, embedder=mock_embedder)
    yield session
    session.close()


@pytest.fixture
def sqlite_session(tmp_path):
    """SQLite SessionDB for comparison tests."""
    from hermes_state import SessionDB
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    yield db
    db.close()


# ---------------------------------------------------------------------------
# Data fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def session_data():
    """Predefined session dict for tests."""
    return {
        "id": "test-session-001",
        "source": "cli",
        "model": "test-model-v1",
        "started_at": time.time(),
    }


@pytest.fixture
def message_data():
    """Predefined message dicts for tests."""
    now = time.time()
    return [
        {"role": "user", "content": "Hello, how are you?",
         "timestamp": now - 100},
        {"role": "assistant", "content": "I'm doing well, thank you!",
         "timestamp": now - 90},
        {"role": "user", "content": "Can you search my old sessions?",
         "timestamp": now - 80},
        {"role": "assistant", "content": "Let me search for that...",
         "timestamp": now - 70, "tool_calls": [
             {"name": "session_search", "arguments": {"query": "old sessions"}}
         ]},
    ]


@pytest.fixture
def multimodal_message_data():
    """Message with multimodal content for content encoding tests."""
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": "Describe this image"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,ABC123"}},
        ],
        "timestamp": time.time(),
    }


# ---------------------------------------------------------------------------
# Lifecycle fixtures (Phase 0 — works now)
# ---------------------------------------------------------------------------

@pytest.fixture
def lifecycle(arcadedb_lifecycle_config, monkeypatch):
    """ArcadeDBLifecycle with test config."""
    if not HAS_LIFECYCLE:
        pytest.skip("ArcadeDBLifecycle not available")
    reset_lifecycle()
    # Mock subprocess to avoid calling real docker in unit tests
    monkeypatch.setattr("subprocess.run", MagicMock(return_value=MagicMock()))
    lc = ArcadeDBLifecycle(arcadedb_lifecycle_config)
    yield lc
    reset_lifecycle()


@pytest.fixture
def lifecycle_no_docker(arcadedb_lifecycle_config, monkeypatch):
    """Lifecycle with Docker unavailable."""
    if not HAS_LIFECYCLE:
        pytest.skip("ArcadeDBLifecycle not available")
    reset_lifecycle()
    monkeypatch.setattr(
        "subprocess.run",
        MagicMock(side_effect=FileNotFoundError("docker"))
    )
    lc = ArcadeDBLifecycle(arcadedb_lifecycle_config)
    yield lc
    reset_lifecycle()
