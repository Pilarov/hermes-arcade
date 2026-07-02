"""Shared utilities for ArcadeDB-backed stores.

Used by:
  Phase 3: hermes_cli/arcadedb_session.py
  Phase 5: hermes_cli/migrate_to_arcadedb.py
  Phase 6: hermes_cli/arcadedb_kanban.py

Mirrors the internal helpers from hermes_state.py for compatibility.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

_CONTENT_JSON_PREFIX = "__JSON__:"
MAX_TITLE_LENGTH = 100

# Session sources excluded from browsing/searching by default.
_HIDDEN_SESSION_SOURCES = ("subagent", "tool")
_DEMOTED_SESSION_SOURCES = ("cron",)


def _now() -> float:
    """Current epoch as float (mirrors SessionDB convention)."""
    return time.time()


def _encode_content(content: Any) -> Optional[str]:
    """Encode content for storage.

    Multimodal content (list/dict) is stored with a JSON sentinel prefix.
    Strings pass through unchanged.  Mirrors SessionDB._encode_content().

    See: hermes_state.py:2961
    """
    if isinstance(content, (list, dict)):
        return _CONTENT_JSON_PREFIX + json.dumps(content, ensure_ascii=False)
    if content is None:
        return None
    return str(content)


def _decode_content(content: Optional[str]) -> Any:
    """Decode content from storage.

    Sentinel-prefixed strings are JSON-decoded back to list/dict.
    Other values pass through.  Mirrors SessionDB._decode_content().

    See: hermes_state.py:2979
    """
    if content is None:
        return None
    if isinstance(content, str):
        if content.startswith(_CONTENT_JSON_PREFIX):
            return json.loads(content[len(_CONTENT_JSON_PREFIX):])
        # LOSS-7: compatibility with legacy SQLite \x00json: prefix
        if content.startswith("\x00json:"):
            return json.loads(content[6:])
    return content


def _sanitize_title(title: str) -> Optional[str]:
    """Validate and sanitise a session title.

    Strips control characters, enforces MAX_TITLE_LENGTH.
    Returns None for invalid input.  Mirrors SessionDB.sanitize_title().
    """
    if not title or not isinstance(title, str):
        return None
    title = title.strip()
    if not title:
        return None
    title = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", title)
    return title[:MAX_TITLE_LENGTH]


def _maybe_epoch(val: Any) -> Any:
    """Convert ISO datetime string to epoch float.

    Handles strings like "2026-06-30 12:00:00".
    Numbers pass through unchanged.
    """
    import calendar
    import datetime

    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", val)
        if m:
            try:
                dt = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                return calendar.timegm(dt.timetuple()) + dt.microsecond / 1_000_000
            except ValueError:
                pass
    return val


def _format_timestamp(ts: Any) -> str:
    """Format epoch float/int as human-readable date string."""
    if ts is None:
        return "unknown"
    try:
        if isinstance(ts, (int, float)):
            from datetime import datetime
            dt = datetime.fromtimestamp(ts)
            return dt.strftime("%B %d, %Y at %I:%M %p")
        return str(ts)
    except (ValueError, OSError, OverflowError):
        return str(ts)


def _has_cjk(text: str) -> bool:
    """Detect CJK characters in a string.

    Used to decide between FULL_TEXT (Lucene) and LIKE fallback.
    """
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF or 0x3040 <= cp <= 0x309F or
            0xAC00 <= cp <= 0xD7AF or 0x3400 <= cp <= 0x4DBF):
            return True
    return False


def _rid_to_int(rid: str) -> int:
    """Convert ArcadeDB @rid (e.g. '#12:3') to a positive 32-bit int.

    Used for backward compatibility with SessionDB message IDs (AUTOINCREMENT).
    """
    return hash(rid) & 0x7FFFFFFF


def _q(val) -> str:
    """Quote a Python value as an ArcadeDB SQL literal.

    Returns 'NULL' for None, quoted string for str, or bare value otherwise.
    Safely escapes backslash and single-quote characters.
    Used to inline values in SQL (ArcadeDB PG protocol bind-param limit).

    Edge cases handled:
      "O'Brien"  → 'O\'Brien'
      "a\\b"     → 'a\\\\b'
      "'; DROP--" → '\'; DROP--'  (literal, not injection)
      None       → NULL
    """
    if val is None:
        return "NULL"
    if isinstance(val, str):
        escaped = val.replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, bool):
        return "1" if val else "0"
    return f"'{val}'"


def _n(val) -> str:
    """Format a numeric value or NULL for SQL literal."""
    if val is None:
        return "NULL"
    if isinstance(val, float):
        return repr(val)
    return str(val)
