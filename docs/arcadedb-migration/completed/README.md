# ArcadeDB Migration — As-Built Documentation

Документация по реализованным фазам. Отражает **фактическое состояние** кода,
включая отклонения от ТЗ, вызванные особенностями ArcadeDB 26.7.1-SNAPSHOT.

## Реализованные фазы

| Фаза | Название | ТЗ | As-Built | Статус |
|------|----------|-----|----------|--------|
| 0 | Lifecycle Manager | [phase-0-lifecycle.md](../phase-0-lifecycle.md) | [phase-0-lifecycle.md](phase-0-lifecycle.md) | Готово |
| 1 | Testing Framework | [phase-1-testing.md](../phase-1-testing.md) | [phase-1-testing.md](phase-1-testing.md) | Готово |
| 2 | Adapter v2 (psycopg) | [phase-2-adapter-v2.md](../phase-2-adapter-v2.md) | [phase-2-adapter-v2.md](phase-2-adapter-v2.md) | Готово |
| 3 | ArcadedbSessionDB | [phase-3-sessiondb.md](../phase-3-sessiondb.md) | [phase-3-sessiondb.md](phase-3-sessiondb.md) | Готово |
| 4 | Consumer Migration | [phase-4-consumers.md](../phase-4-consumers.md) | [phase-4-consumers.md](phase-4-consumers.md) | Factory готов, consumers — нет (TD-8) |

## Ключевые отклонения от ТЗ

### ArcadeDB PG Protocol — Simple Query Mode Only

**ТЗ предполагало:** полноценный PostgreSQL wire protocol с bind-параметрами
и psycopg-level транзакциями (`autocommit=False` + `conn.commit()`).

**Реальность:** ArcadeDB поддерживает только «simple» query mode. Нет
extended query protocol → нет prepared statements, нет bind-параметров.

**Решение:**
- `autocommit=True` всегда
- Dict-параметры авто-конвертируются через `ArcadeDBAdapter._fmt()`
- Multi-param INSERT/UPDATE используют `_q()`/`_n()` string formatting
- Транзакции через SQL `BEGIN`/`COMMIT`/`ROLLBACK`

### ArcadeDB SQL Dialect Differences

| Ожидалось (стандартный SQL) | ArcadeDB |
|---|---|
| `LIMIT X OFFSET Y` | `LIMIT X SKIP Y` |
| `LIKE ... ESCAPE '\'` | `LIKE ...` (без ESCAPE) |
| `FROM a, b` | Не поддерживается |
| `DELETE VERTEX` (cascade) | Зависает → `UPDATE active=0` |
| `RETURN @rid` (CREATE VERTEX) | Не поддерживается |
| `LET s = (SELECT ...)` | Поддерживается, но нестабильно |

### Интеграционный тест: 10/10 PASSED

Проверено на ArcadeDB 26.7.1-SNAPSHOT (Docker, SSH-туннель).
Все 10 операций (factory → CRUD → search → locks → export) работают.

## Следующий шаг: TD-8 (CRITICAL)

30+ consumers всё ещё создают `SessionDB()` напрямую вместо `create_session_db()`.
Без этого ArcadeDB не активируется при реальном запуске Hermes.

См. [phase-4-consumers.md](phase-4-consumers.md) — план переключения.
