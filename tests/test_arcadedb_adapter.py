"""Tests for ArcadeDBAdapter v2 — psycopg transport (Phase 2).

ArcadeDB only supports "simple" query mode — no bind parameters.
All queries use string formatting (_q helper).
"""

import pytest

pytestmark = pytest.mark.skip_phase2

try:
    from hermes_cli.arcadedb import ArcadeDBConfig, ArcadeDBAdapter, ArcadeDBError
    HAS_ADAPTER = True
except ImportError:
    HAS_ADAPTER = False

pytest_plugins = ["tests.fixtures.arcadedb_fixtures"]

def _q(val):
    if val is None: return "NULL"
    if isinstance(val, str):
        return "'" + val.replace("\\","\\\\").replace("'","\\'") + "'"
    return str(val)


@pytest.mark.skipif(not HAS_ADAPTER, reason="Phase 2 not yet implemented")
class TestConnection:
    def test_connect_success(self, arcadedb_adapter):
        assert arcadedb_adapter.connected is True

    def test_connect_failure(self):
        config = ArcadeDBConfig(host="10.255.255.1", port=5432, timeout=1.0)
        adapter = ArcadeDBAdapter(config)
        with pytest.raises(ArcadeDBError):
            adapter.connect()
        adapter.close()

    def test_double_connect_idempotent(self, arcadedb_adapter):
        arcadedb_adapter.connect()
        arcadedb_adapter.connect()
        assert arcadedb_adapter.connected is True


@pytest.mark.skipif(not HAS_ADAPTER, reason="Phase 2 not yet implemented")
class TestTransaction:
    @pytest.fixture(autouse=True)
    def _setup(self, arcadedb_adapter):
        arcadedb_adapter.execute("CREATE VERTEX TYPE TestTx IF NOT EXISTS")
        # Clean up from previous runs
        try: arcadedb_adapter.execute("DELETE VERTEX TestTx")
        except: pass

    def test_commit(self, arcadedb_adapter):
        def _do(cur):
            cur.execute(f"CREATE VERTEX TestTx SET name = {_q('tx-test-1')}")
        arcadedb_adapter.transact(_do)
        rows = arcadedb_adapter.query(
            f"SELECT FROM TestTx WHERE name = {_q('tx-test-1')}"
        )
        assert len(rows) == 1

    def test_rollback(self, arcadedb_adapter):
        try:
            def _do(cur):
                cur.execute(f"CREATE VERTEX TestTx SET name = {_q('rollback-me')}")
                raise RuntimeError("simulated failure")
            arcadedb_adapter.transact(_do)
        except RuntimeError:
            pass
        rows = arcadedb_adapter.query(
            f"SELECT FROM TestTx WHERE name = {_q('rollback-me')}"
        )
        assert len(rows) == 0

    def test_transact_atomic(self, arcadedb_adapter):
        def _do(cur):
            cur.execute(f"CREATE VERTEX TestTx SET name = {_q('atom-1')}")
            cur.execute(f"CREATE VERTEX TestTx SET name = {_q('atom-2')}")
        arcadedb_adapter.transact(_do)
        rows = arcadedb_adapter.query(
            f"SELECT FROM TestTx WHERE name LIKE {_q('atom-%')}"
        )
        assert len(rows) == 2


@pytest.mark.skipif(not HAS_ADAPTER, reason="Phase 2 not yet implemented")
class TestQueryMethods:
    @pytest.fixture(autouse=True)
    def _setup(self, arcadedb_adapter):
        arcadedb_adapter.execute("CREATE VERTEX TYPE TestQ IF NOT EXISTS")
        try: arcadedb_adapter.execute("DELETE VERTEX TestQ")
        except: pass

    def test_execute_insert(self, arcadedb_adapter):
        arcadedb_adapter.execute(
            f"CREATE VERTEX TestQ SET name = {_q('insert-ok')}"
        )
        rows = arcadedb_adapter.query(
            f"SELECT FROM TestQ WHERE name = {_q('insert-ok')}"
        )
        assert len(rows) == 1

    def test_query_select(self, arcadedb_adapter):
        arcadedb_adapter.execute(f"CREATE VERTEX TestQ SET name = {_q('sel')}")
        rows = arcadedb_adapter.query(
            f"SELECT FROM TestQ WHERE name = {_q('sel')}"
        )
        assert len(rows) == 1
        assert isinstance(rows[0], dict)

    def test_query_params(self, arcadedb_adapter):
        arcadedb_adapter.execute(
            f"CREATE VERTEX TestQ SET name = {_q('par')}, age = 42"
        )
        rows = arcadedb_adapter.query(
            f"SELECT FROM TestQ WHERE age = 42"
        )
        assert len(rows) == 1


@pytest.mark.skipif(not HAS_ADAPTER, reason="Phase 2 not yet implemented")
class TestVectorHandling:
    @pytest.fixture(autouse=True)
    def _setup(self, arcadedb_adapter):
        arcadedb_adapter.execute("CREATE VERTEX TYPE TestV IF NOT EXISTS")
        arcadedb_adapter.execute("CREATE PROPERTY TestV.embedding IF NOT EXISTS LIST")
        try: arcadedb_adapter.execute("DELETE VERTEX TestV")
        except: pass

    def test_vector_sql_literal(self, arcadedb_adapter):
        vec = [0.1, 0.2, 0.3, 0.4]
        vec_str = ArcadeDBAdapter._vec(vec)
        assert vec_str == "[0.1, 0.2, 0.3, 0.4]"
        arcadedb_adapter.execute(
            f"CREATE VERTEX TestV SET name = {_q('v1')}, embedding = {vec_str}"
        )
        rows = arcadedb_adapter.query(
            f"SELECT FROM TestV WHERE name = {_q('v1')}"
        )
        assert len(rows) == 1

    def test_vector_neighbors_parameter(self, arcadedb_adapter):
        vec = [0.1, 0.2, 0.3, 0.4]
        v = ArcadeDBAdapter._vec(vec)
        arcadedb_adapter.execute(f"CREATE VERTEX TestV SET name = {_q('n1')}, embedding = {v}")
        arcadedb_adapter.execute(f"CREATE VERTEX TestV SET name = {_q('n2')}, embedding = {v}")
        rows = arcadedb_adapter.query(
            f"SELECT expand(`vector.neighbors`('TestV[embedding]', {v}, 2))"
        )
        assert len(rows) >= 1
