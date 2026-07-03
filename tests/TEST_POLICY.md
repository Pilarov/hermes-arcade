# ArcadeDB Test Policy

## Core Rule: Zero Cross-Test Contamination

Every test MUST leave the shared ArcadeDB in the same logical state it found it.
Tests are free to leave garbage data (not worth the cost of perfect cleanup), but
must NEVER cause another test to fail.

## Unique IDs

- **Use `uuid.uuid4().hex[:8]` suffix** for all session IDs, vertex names, and
  lock identifiers — never hardcoded strings like `"test-1"`, `"test-2"`.
- Pattern: `f"{descriptive-name}-{unique_suffix}"`
- Update on every test run — `id(self)` or `uuid4()` ensure no collision.

## No Assumptions About Clean State

- Tests MUST NOT assume the database is empty. Previous runs leave data.
  Previous tests in the same run may have failed and left partial data.

## Data Accumulation Tolerance

- `test_query_select`, `test_query_params`, `test_vector_sql_literal`:
  these create TestQ/TestV with unique IDs per run. They tolerate 1000 old rows
  from prior runs (they filter by `WHERE name = {_q(uid)}`).

## Pool Corruption Awareness

- ArcadeDB PG protocol has documented "Transaction not active" limitations.
  Tests that use >20 `transact()` calls per process may hit pool corruption.
  Isolate heavy-write tests. Prefer `execute()` (read path, uses pool) over
  `transact()` (write path, uses fresh connection) where acceptable.

## Test Dependencies

- All test classes are independent. No test depends on side effects from another.
- Fixtures are function-scoped (fresh adapter + session per test).
- Schema initialization (`SchemaManager.create_all()`) is idempotent —
  runs in `arcadedb_session` fixture, safe to call N times.

## E2E vs Unit

- Unit tests (`test_arcadedb_*.py`): use direct `ArcadeDBAdapter` / `ArcadedbSessionDB`
  via fixtures. Fast, isolated, test one thing.
- E2E tests (`test_hermes_arcade_e2e.py`): use `create_session_db()` factory.
  Test the full integration path including config loading, lifecycle manager,
  embedder setup. Run LAST to avoid pool corruption interference.

## Known Limitations (Not Code Bugs)

- **Pool corruption**: After ~20 `transact()` calls, ArcadeDB PG simple query mode
  enters "Transaction not active" state. Fresh connections mitigate but cannot
  fully fix — server-side state corruption. Accept as known limitation.
- **`vector.fuse()`**: Requires ArcadeDB ≥26.5.1. Not available in 26.4.2.
- **`DELETE VERTEX` with edges**: ArcadeDB PG protocol hangs on cascade delete.
  Use soft-delete (SET deleted = true) instead.
