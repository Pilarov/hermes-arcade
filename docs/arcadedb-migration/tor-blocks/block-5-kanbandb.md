# Блок 5: KanbanDB — Атомарность (AUD-6,28)

## Статус: HIGH — критические баги

### Проблемы из аудита

| ID | Что | Где |
|----|-----|-----|
| AUD-6 | `add_comment` линкует к TaskRun вместо TaskComment | `arcadedb_kanban.py:273-275` |
| AUD-28 | CAS claim зависит от `cur.rowcount` (ненадёжно в ArcadeDB) | `arcadedb_kanban.py:172-173` |

### Что ещё нужно (из аудита)

| Проблема | Описание |
|----------|----------|
| `create_task` race | `SELECT @rid FROM Task ORDER BY @rid DESC LIMIT 1` — гонка при concurrent insert |
| `release_stale_claims` возвращает 0 | Всегда 0 вместо реального count |
| `add_comment` — dead code | `HAS_COMMENT` edge создаётся, но линкуется к неверному типу |

---

## ТЗ

### 5.1 Fix: `add_comment` edge target

```python
# Было (строка 273-275):
f"CREATE EDGE HAS_COMMENT FROM "
f"(SELECT FROM Task WHERE @rid = {_q(task_id)}) TO "
f"(SELECT FROM TaskRun WHERE @rid = {_q(comment_rid)}) "  # ← BUG: TaskRun

# Стало:
f"CREATE EDGE HAS_COMMENT FROM "
f"(SELECT FROM Task WHERE @rid = {_q(task_id)}) TO "
f"(SELECT FROM TaskComment WHERE @rid = {_q(comment_rid)}) "  # ← TaskComment
```

### 5.2 Fix: CAS claim — SELECT вместо rowcount

```python
def claim_task(self, task_id, worker_profile, ttl_seconds=300):
    def _do(cur):
        # UPDATE ... WHERE status='ready' AND claim_lock IS NULL
        cur.execute(
            f"UPDATE Task SET status = 'running', claim_lock = {_q(run_id)}, ... "
            f"WHERE @rid = {_q(task_id)} AND claim_lock IS NULL"
        )
        
        # Верифицировать через SELECT (не rowcount!)
        cur.execute(
            f"SELECT claim_lock FROM Task WHERE @rid = {_q(task_id)}"
        )
        row = cur.fetchone()
        if row["claim_lock"] != run_id:
            return None  # проиграли гонку
        
        # ... create TaskRun + HAS_RUN edge ...
        return run_id
    return self._adapter.transact(_do)
```

### 5.3 Fix: `release_stale_claims` возвращать реальный count

```python
def release_stale_claims(self) -> int:
    now = time.time()
    # SELECT сначала чтобы знать сколько
    rows = self._adapter.query(
        f"SELECT count(*) as cnt FROM Task "
        f"WHERE status = 'running' AND claim_expires < {_n(now)}"
    )
    count = rows[0].get("cnt", 0) if rows else 0
    
    self._adapter.execute(
        f"UPDATE Task SET status = 'ready', claim_lock = NULL, claim_expires = NULL "
        f"WHERE status = 'running' AND claim_expires < {_n(now)}"
    )
    return count
```

### 5.4 Fix: `create_task` — получить @rid атомарно

```python
def create_task(self, title, body="", ...):
    def _do(cur):
        cur.execute(
            f"CREATE VERTEX Task SET title = {_q(title)}, ..."
        )
        # Внутри той же транзакции — гонки нет
        cur.execute(
            "SELECT @rid FROM Task ORDER BY @rid DESC LIMIT 1"
        )
        return cur.fetchone()["@rid"]
    return self._adapter.transact(_do)
```

---

## Acceptance Criteria

- [ ] `add_comment(task_id, "author", "body")` — `HAS_COMMENT` edge от Task к TaskComment
- [ ] `claim_task(task_id, "w1")` — успех; `claim_task(task_id, "w2")` — None (конфликт)
- [ ] Два concurrent `claim_task` → ровно 1 успех (проверено через SELECT, не rowcount)
- [ ] `release_stale_claims()` — возвращает реальное количество освобождённых задач
- [ ] `create_task("test")` — возвращает @rid в рамках одной транзакции

## Ссылки

- ArcadeDB MVCC: `ConcurrentModificationException` → https://docs.arcadedb.com/arcadedb/concepts/transactions.html
- BestPracticesBD.md: CAS через SELECT-after-UPDATE
