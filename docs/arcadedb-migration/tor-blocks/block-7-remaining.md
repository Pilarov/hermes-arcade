# Блок 7: Функциональные потери + Оставшиеся баги (LOSS-1..7 + AUD-8,10-13,16,20-27)

## Статус: MEDIUM — не блокирует, но снижает качество

### Функциональные потери

| ID | Что потеряно | Почему | Mitigation |
|----|-------------|--------|------------|
| LOSS-1 | BM25 ranking | `SEARCH_INDEX` не работает через PG | Векторный поиск + LIKE |
| LOSS-2 | Phrase search | `LIKE` не поддерживает phrase | Векторный similarity |
| LOSS-3 | Boolean operators | `LIKE` не поддерживает AND/OR/NOT | Векторный similarity |
| LOSS-4 | `optimize_fts()` | Нет Lucene maintenance API | Ждать stable ArcadeDB |
| LOSS-5 | Cascade delete delegate children | `DELETE VERTEX` hang | Ручной cleanup |
| LOSS-6 | `api_call_count` | Не обновляется в SessionDB | Low prio |
| LOSS-7 | `\x00json:` vs `__JSON__:` | Incompatible с SQLite данными | Миграция данных |

### Оставшиеся баги из аудита

| ID | Что | Severity |
|----|-----|----------|
| AUD-8 | `claim_handoff` всегда True | HIGH |
| AUD-10 | `rewind` @rid строковое сравнение | HIGH |
| AUD-11 | `restore_rewound` всегда 0 | HIGH |
| AUD-12 | `replace_messages` missing fields | HIGH |
| AUD-13 | Composite index not stringified | HIGH |
| AUD-16 | Verification no pruning | HIGH |
| AUD-20 | `_fmt` silent NULL | MEDIUM |
| AUD-21 | `_rid_to_int` unstable | MEDIUM |
| AUD-22 | `_q` no null-byte handling | MEDIUM |
| AUD-23 | `get_messages_around` N+1 | MEDIUM |
| AUD-24 | `find_latest` missing agent_close | MEDIUM |
| AUD-25 | `update_session_cwd` NULL overwrite | MEDIUM |
| AUD-26 | `set_meta` non-atomic | MEDIUM |
| AUD-27 | `__JSON__:` collision risk | MEDIUM |

---

## ТЗ — приоритетные фиксы

### 7.1 `claim_handoff` — проверять rowcount (AUD-8)

```python
def claim_handoff(self, session_id: str) -> bool:
    rows = self._adapter.query(
        f"SELECT @rid FROM Session WHERE id = {_q(session_id)} "
        f"AND handoff_state = 'pending'"
    )
    if not rows:
        return False
    self._adapter.execute(
        f"UPDATE Session SET handoff_state = 'running' "
        f"WHERE @rid = {_q(rows[0]['@rid'])}"
    )
    return True
```

### 7.2 `rewind_to_message` — timestamp вместо @rid (AUD-10)

```python
# Вместо: WHERE @rid >= %(rid)s  (строковое сравнение!)
# Использовать: WHERE timestamp >= (SELECT timestamp FROM Message WHERE ...)
```

### 7.3 `restore_rewound` — возвращать реальный count (AUD-11)

```python
def restore_rewound(self, session_id: str, since_message_id: int) -> int:
    # ... UPDATE active = 1 ...
    count = self._adapter.query(
        f"SELECT count(*) FROM Message WHERE session_id = {_q(session_id)} "
        f"AND active = 1 AND compacted = 0"
    )
    return count[0].get("cnt", 0) if count else 0
```

### 7.4 `replace_messages` — все поля + edges (AUD-12)

```python
# Добавить в CREATE VERTEX Message:
f"platform_message_id = {_q(msg.get('platform_message_id'))}, "
f"codex_reasoning_items = {_q(msg.get('codex_reasoning_items'))}, "
f"reasoning_details = {_q(msg.get('reasoning_details'))}, "
f"observed = {_q(msg.get('observed'))}, "

# И создать HAS_MESSAGE edge как в append_message()
```

### 7.5 `_decode_content` — обратная совместимость (LOSS-7)

```python
_CONTENT_JSON_PREFIX = "__JSON__:"
_CONTENT_JSON_PREFIX_LEGACY = "\x00json:"  # SQLite prefix

def _decode_content(content):
    if content is None:
        return None
    if isinstance(content, str):
        if content.startswith(_CONTENT_JSON_PREFIX):
            return json.loads(content[len(_CONTENT_JSON_PREFIX):])
        if content.startswith(_CONTENT_JSON_PREFIX_LEGACY):
            return json.loads(content[len(_CONTENT_JSON_PREFIX_LEGACY):])
    return content
```

### 7.6 Verification pruning (AUD-16)

```python
def _prune_old_events(self):
    """Ограничить количество VerificationEvent."""
    # Не более 100 на (session_id, cwd)
    rows = self._adapter.query(
        "SELECT session_id, cwd, count(*) as cnt FROM VerificationEvent "
        "GROUP BY session_id, cwd HAVING cnt > 100"
    )
    for r in rows:
        excess = r["cnt"] - 100
        self._adapter.execute(
            f"DELETE FROM VerificationEvent WHERE @rid IN ("
            f"SELECT @rid FROM VerificationEvent "
            f"WHERE session_id = {_q(r['session_id'])} AND cwd = {_q(r['cwd'])} "
            f"ORDER BY created_at ASC LIMIT {excess}"
            f")"
        )
```

---

## Acceptance Criteria (выборочно)

- [ ] `claim_handoff(sid)` — False если не pending, True если pending → running
- [ ] `/undo 1` — правильный диапазон сообщений (timestamp-based)
- [ ] `restore_rewound` возвращает реальное число
- [ ] `replace_messages` сохраняет platform_message_id, reasoning_details
- [ ] Мигрированные данные с `\x00json:` читаются корректно
- [ ] `_prune_old_events` удаляет старые события

## Ссылки

- ArcadeDB SQL reference: https://docs.arcadedb.com/arcadedb/reference/sql/chapter.html
