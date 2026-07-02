# Блок 4: Транзакции и Пул — Надёжность (AUD-7,14,15,17,18,19)

## Статус: HIGH — production stability

### Проблемы из аудита

| ID | Что | Где |
|----|-----|-----|
| AUD-7 | `clear_messages` использует DELETE VERTEX (hang) | `arcadedb_session.py:862` |
| AUD-14 | TOCTOU гонка на `self._pool` | `arcadedb.py:59-131` |
| AUD-15 | `_fmt_tuple` split — %s внутри кавычек | `arcadedb.py:295` |
| AUD-17 | `conn.close()` подавляет оригинальное исключение | `arcadedb.py:161` |
| AUD-18 | Retry на строковом матчинге ошибки | `arcadedb.py:213` |
| AUD-19 | `_retry()` — dead code | `arcadedb.py:335-349` |

### Архитектурная рекомендация (из ArcadeDB Transactions)

> ArcadeDB is ACID compliant with MVCC. On COMMIT conflict: `ConcurrentModificationException`. Retry the transaction.

Retry должен быть на `ConcurrentModificationException`, не на строковом матчинге.

> После ошибки — закрыть соединение. Пул создаст новое. Не возвращать плохие соединения в пул.

---

## ТЗ

### 4.1 `clear_messages` — soft-delete вместо DELETE VERTEX

```python
def clear_messages(self, session_id: str) -> None:
    # Soft-delete (как replace_messages)
    self._adapter.execute(
        f"UPDATE Message SET active = 0, compacted = 1 "
        f"WHERE session_id = {_q(session_id)}"
    )
    self._adapter.execute(
        f"UPDATE Session SET message_count = 0, tool_call_count = 0 "
        f"WHERE id = {_q(session_id)}"
    )
```

### 4.2 Thread-safe pool access

```python
class ArcadeDBAdapter:
    def __init__(self, config=None):
        self._cfg = config or ArcadeDBConfig()
        self._pool = None
        self._lock = threading.RLock()  # ADD
    
    def connect(self):
        with self._lock:
            if self._pool is not None:
                return
            # ... create pool ...
    
    def close(self):
        with self._lock:
            if self._pool is not None:
                self._pool.close()
                self._pool = None
    
    def execute(self, sql, params=None, language="sql"):
        with self._lock:
            if self._pool is None:
                raise ArcadeDBError("not connected")
            conn = self._pool.getconn()
        # ... execute without lock (fn uses its own conn) ...
```

### 4.3 `_fmt_tuple` — raise error на mismatch

```python
@staticmethod
def _fmt_tuple(sql: str, params: tuple) -> str:
    parts = sql.split("%s")
    if len(parts) - 1 != len(params):
        raise ArcadeDBError(
            f"Placeholder count mismatch: {len(parts)-1} placeholders, "
            f"{len(params)} params in: {sql[:100]}"
        )
    # ... existing logic ...
```

### 4.4 Retry на типах исключений вместо строк

```python
def execute(self, sql, params=None, language="sql"):
    # ... existing code ...
    except psycopg.OperationalError as e:
        err = str(e)
        if "connection" in err.lower() or "timeout" in err.lower():
            # Retry with fresh connection
            ...
    except psycopg.errors.InternalError_ as e:
        if "Transaction not active" in str(e) or "got no result" in str(e):
            # Retry with fresh connection
            ...
    except Exception as e:
        raise ArcadeDBError(str(e)) from e
```

### 4.5 Удалить `_retry()` или подключить

Либо удалить мёртвый код, либо подключить:

```python
def execute(self, sql, params=None, language="sql"):
    return self._retry(
        self._execute_impl, sql, params, language
    )
```

### 4.6 Сохранять оригинальное исключение в `transact()`

```python
def transact(self, fn):
    # ...
    except Exception as orig_exc:
        try:
            cur.execute("ROLLBACK")
        except Exception:
            pass
        cur.close()
        try:
            conn.close()
        except Exception:
            pass  # don't suppress orig_exc
        raise orig_exc  # original exception preserved
```

---

## Acceptance Criteria

- [ ] `clear_messages(sid)` — не hang-уется (soft-delete)
- [ ] 10 concurrent `execute()` вызовов — нет AttributeError
- [ ] `_fmt_tuple("INSERT ... %s ... %s", ("a",))` — `ArcadeDBError`, не passthrough
- [ ] `transact()` бросает оригинальное исключение из `fn(cur)`, не из `conn.close()`
- [ ] Retry срабатывает на `psycopg.OperationalError` + `InternalError_`
- [ ] `_retry()` либо удалён, либо используется

## Ссылки

- ArcadeDB Transactions (MVCC): https://docs.arcadedb.com/arcadedb/concepts/transactions.html
- psycopg 3 exceptions: `psycopg.OperationalError` — connection-level errors
- Thread safety: `threading.RLock` — reentrant lock
