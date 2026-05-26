from __future__ import annotations

from typing import Any

from .database import dict_from_row, get_db, now_ts


def record_conflict(
    *,
    user_id: int,
    persona_id: int | None,
    conflict_type: str,
    current_uid: str,
    current_text: str,
    previous_uid: str | None = None,
    previous_text: str = "",
    resolution: str = "prefer_current",
    reason: str = "",
    status: str = "open",
) -> dict[str, Any]:
    ts = now_ts()
    with get_db() as db:
        db.execute(
            """
            INSERT INTO memory_conflicts (
                user_id, persona_id, conflict_type, status,
                current_uid, previous_uid, current_text, previous_text,
                resolution, reason, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(current_uid, previous_uid, conflict_type)
            DO UPDATE SET current_text = excluded.current_text,
                          previous_text = excluded.previous_text,
                          resolution = excluded.resolution,
                          reason = excluded.reason,
                          status = excluded.status,
                          updated_at = excluded.updated_at
            """,
            (
                user_id,
                persona_id,
                conflict_type,
                status,
                current_uid,
                previous_uid,
                current_text,
                previous_text,
                resolution,
                reason,
                ts,
                ts,
            ),
        )
        row = db.execute(
            """
            SELECT *
            FROM memory_conflicts
            WHERE current_uid = ? AND previous_uid IS ? AND conflict_type = ?
            """,
            (current_uid, previous_uid, conflict_type),
        ).fetchone()
    return dict_from_row(row) or {}


def detect_preference_conflicts(
    *,
    user_id: int,
    persona_id: int | None,
    current_uid: str,
    current_text: str,
    current_object: str,
) -> list[dict[str, Any]]:
    polarity = _preference_polarity(current_text)
    if not polarity or not current_object:
        return []
    opposite = "dislike" if polarity == "like" else "like"
    with get_db() as db:
        rows = db.execute(
            """
            SELECT uid, text, object
            FROM memory_relations
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL)
              AND predicate = 'preference' AND archived = 0
              AND uid != ?
            ORDER BY updated_at DESC
            LIMIT 120
            """,
            (user_id, persona_id, current_uid),
        ).fetchall()
    conflicts = []
    for row in rows:
        item = dict_from_row(row) or {}
        if str(item.get("object") or "") != current_object:
            continue
        if _preference_polarity(str(item.get("text") or "")) == opposite:
            conflicts.append(
                record_conflict(
                    user_id=user_id,
                    persona_id=persona_id,
                    conflict_type="preference_polarity",
                    current_uid=current_uid,
                    previous_uid=str(item["uid"]),
                    current_text=current_text,
                    previous_text=str(item.get("text") or ""),
                    resolution="prefer_current",
                    reason=f"Explicit newer preference replaced older preference for {current_object}.",
                    status="resolved",
                )
            )
    return conflicts


def list_conflicts(
    user_id: int,
    persona_id: int | None = None,
    status: str | None = "open",
    limit: int = 50,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    params: list[Any] = [user_id]
    persona_clause = ""
    status_clause = ""
    if persona_id is not None:
        persona_clause = "AND persona_id = ?"
        params.append(persona_id)
    if status:
        status_clause = "AND status = ?"
        params.append(status)
    params.append(limit)
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT *
            FROM memory_conflicts
            WHERE user_id = ? {persona_clause} {status_clause}
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict_from_row(row) or {} for row in rows]


def update_conflict_status(user_id: int, conflict_id: int, status: str) -> dict[str, Any]:
    if status not in {"open", "resolved", "dismissed"}:
        raise ValueError("invalid conflict status")
    with get_db() as db:
        db.execute(
            """
            UPDATE memory_conflicts
            SET status = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (status, now_ts(), conflict_id, user_id),
        )
        row = db.execute("SELECT * FROM memory_conflicts WHERE id = ? AND user_id = ?", (conflict_id, user_id)).fetchone()
    item = dict_from_row(row)
    if not item:
        raise ValueError("conflict not found")
    return item


def _preference_polarity(text: str) -> str:
    if any(word in text for word in ("讨厌", "不喜欢", "dislike", "hate")):
        return "dislike"
    if any(word in text for word in ("喜欢", "like", "love")):
        return "like"
    return ""
