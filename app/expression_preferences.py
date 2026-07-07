from __future__ import annotations

from typing import Any

from .database import dict_from_row, get_db, now_ts


VALID_EXPRESSION_MODES = {"off", "subtle", "normal"}


def record_expression_preference_event(
    user_id: int,
    persona_id: int,
    mode: str,
    *,
    source: str,
    source_message_id: int | None = None,
) -> dict[str, Any]:
    mode = _normalize_expression_mode(mode)
    ts = now_ts()
    with get_db() as db:
        event_id = int(
            db.execute(
                """
                INSERT INTO expression_preference_events (
                    user_id, persona_id, mode, source, source_message_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (int(user_id), int(persona_id), mode, str(source or "")[:40], source_message_id, ts),
            ).lastrowid
        )
    return {
        "id": event_id,
        "user_id": int(user_id),
        "persona_id": int(persona_id),
        "mode": mode,
        "source": str(source or "")[:40],
        "source_message_id": source_message_id,
        "created_at": ts,
    }


def expression_preference_events(user_id: int, persona_id: int, limit: int = 5) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id, user_id, persona_id, mode, source, source_message_id, created_at
            FROM expression_preference_events
            WHERE user_id = ? AND persona_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(user_id), int(persona_id), max(1, min(int(limit or 5), 20))),
        ).fetchall()
    return [dict_from_row(row) for row in rows]


def _normalize_expression_mode(mode: str) -> str:
    value = str(mode or "normal").strip()
    return value if value in VALID_EXPRESSION_MODES else "normal"
