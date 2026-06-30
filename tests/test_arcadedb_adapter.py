"""Tests for ArcadeDBAdapter v2 — psycopg transport (Phase 2).

Links:
  Phase 2: hermes_cli/arcadedb.py (REWRITE)
  Phase 2 spec: docs/arcadedb-migration/phase-2-adapter-v2.md
  Fixtures: tests/fixtures/arcadedb_fixtures.py

These tests DEFINE the API contract for ArcadeDBAdapter.
They will FAIL/SKIP until Phase 2 implements the psycopg rewrite.
"""

import pytest

pytestmark = pytest.mark.skip_phase2

try:
    from hermes_cli.arcadedb import ArcadeDBConfig, ArcadeDBAdapter, ArcadeDBError
    HAS_ADAPTER = True
except ImportError:
    HAS_ADAPTER = False


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_ADAPTER, reason="Phase 2 ArcadeDBAdapter not yet implemented")
class TestConnection:
    def test_connect_success(self, arcadedb_adapter):
        """A2-01: Successful connect to running ArcadeDB."""
        assert arcadedb_adapter.connected is True

    def test_connect_failure(self):
        """A2-02: Connect to unreachable host raises ArcadeDBError."""
        config = ArcadeDBConfig(host="10.255.255.1", port=5432, timeout=1.0)
        adapter = ArcadeDBAdapter(config)
        with pytest.raises(ArcadeDBError):
            adapter.connect()
        adapter.close()

    def test_double_connect_idempotent(self, arcadedb_adapter):
        """Double connect() is idempotent."""
        arcadedb_adapter.connect()
        arcadedb_adapter.connect()
        assert arcadedb_adapter.connected is True


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_ADAPTER, reason="Phase 2 ArcadeDBAdapter not yet implemented")
class TestTransaction:
    def test_commit(self, arcadedb_adapter):
        """A2-03: INSERT in transaction -> COMMIT -> data visible."""
        def _do(cur):
            cur.execute("CREATE VERTEX TestTx SET name = %s", ("tx-test-1",))
            return True
        arcadedb_adapter.transact(_do)
        rows = arcadedb_adapter.query("SELECT FROM TestTx WHERE name = %s", {"nm": "tx-test-1"})
        assert len(rows) == 1
        assert rows[0]["name"] == "tx-test-1"

    def test_rollback(self, arcadedb_adapter):
        """A2-04: INSERT in transaction -> ROLLBACK -> data not visible."""
        try:
            def _do(cur):
                cur.execute("CREATE VERTEX TestTx SET name = %s", ("rollback-test",))
                raise RuntimeError("simulated failure")
            arcadedb_adapter.transact(_do)
        except RuntimeError:
            pass
        rows = arcadedb_adapter.query(
            "SELECT FROM TestTx WHERE name = %s", {"nm": "rollback-test"}
        )
        assert len(rows) == 0

    def test_transact_atomic(self, arcadedb_adapter):
        """A2-05: transact() — two INSERTs, both visible or none."""
        def _do(cur):
            cur.execute("CREATE VERTEX TestTx SET name = %s", ("atom-1",))
            cur.execute("CREATE VERTEX TestTx SET name = %s", ("atom-2",))
            return True
        arcadedb_adapter.transact(_do)
        rows = arcadedb_adapter.query("SELECT FROM TestTx WHERE name LIKE %s", {"nm": "atom-%"})
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# Query API
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_ADAPTER, reason="Phase 2 ArcadeDBAdapter not yet implemented")
class TestQueryMethods:
    def test_execute_insert(self, arcadedb_adapter):
        """A2-12: execute() — INSERT returns result."""
        result = arcadedb_adapter.execute(
            "CREATE VERTEX TestQuery SET name = %s", {"nm": "insert-test"}
        )
        assert isinstance(result, list)

    def test_query_select(self, arcadedb_adapter):
        """A2-13: query() — SELECT returns list[dict]."""
        arcadedb_adapter.execute(
            "CREATE VERTEX TestQuery SET name = %s", {"nm": "select-test"}
        )
        rows = arcadedb_adapter.query(
            "SELECT FROM TestQuery WHERE name = %s", {"nm": "select-test"}
        )
        assert len(rows) == 1
        assert isinstance(rows[0], dict)

    def test_query_params(self, arcadedb_adapter):
        """A2-14: query() with parameterized WHERE clause."""
        arcadedb_adapter.execute(
            "CREATE VERTEX TestQuery SET name = %s, age = %s", {"nm": "param-test", "age": 42}
        )
        rows = arcadedb_adapter.query(
            "SELECT FROM TestQuery WHERE age = %s", {"ag": 42}
        )
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Vector handling
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_ADAPTER, reason="Phase 2 ArcadeDBAdapter not yet implemented")
class TestVectorHandling:
    def test_vector_sql_literal(self, arcadedb_adapter):
        """A2-07: Vector via SQL literal -> successful insert."""
        vec = [0.1, 0.2, 0.3, 0.4]
        vec_str = ArcadeDBAdapter._vec(vec)
        assert vec_str == "[0.1, 0.2, 0.3, 0.4]"

        arcadedb_adapter.execute(
            f"CREATE VERTEX TestVec SET name = 'vec-test', embedding = {vec_str}"
        )
        rows = arcadedb_adapter.query("SELECT FROM TestVec WHERE name = 'vec-test'")
        assert len(rows) == 1

    def test_vector_neighbors_parameter(self, arcadedb_adapter):
        """A2-09: vector.neighbors() with parameterized query vector."""
        vec = [0.1, 0.2, 0.3, 0.4]
        vec_str = ArcadeDBAdapter._vec(vec)

        arcadedb_adapter.execute(
            f"CREATE VERTEX TestVec SET name = 'n1', embedding = {vec_str}"
        )
        arcadedb_adapter.execute(
            f"CREATE VERTEX TestVec SET name = 'n2', embedding = {vec_str}"
        )

        rows = arcadedb_adapter.query(
            "SELECT expand(`vector.neighbors`('TestVec[embedding]', %s, 2))",
            {"qv": vec}
        )
        assert len(rows) >= 1
