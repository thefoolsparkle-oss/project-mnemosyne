from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from .database import dict_from_row, get_db, now_ts


PROACTIVE_CONTACT_TYPES = {"followup", "care", "reminder", "interest"}
PROACTIVE_CONTACT_EVENT_TYPES = {"candidate_opened", "candidate_seen", "candidate_dismissed"}
DEFAULT_PROACTIVE_CONTACT = {
    "enabled": False,
    "max_per_day": 1,
    "quiet_start": "22:00",
    "quiet_end": "09:00",
    "allowed_types": ["followup", "care", "reminder"],
}
PROACTIVE_CONTACT_MIN_IDLE_SECONDS = 6 * 60 * 60


def normalize_profile_preferences(preferences: Any) -> dict[str, Any]:
    base = dict(preferences or {}) if isinstance(preferences, dict) else {}
    raw_contact = base.get("proactive_contact")
    contact = dict(raw_contact or {}) if isinstance(raw_contact, dict) else {}
    enabled = bool(contact.get("enabled", DEFAULT_PROACTIVE_CONTACT["enabled"]))
    try:
        max_per_day = int(contact.get("max_per_day", DEFAULT_PROACTIVE_CONTACT["max_per_day"]))
    except Exception:
        max_per_day = int(DEFAULT_PROACTIVE_CONTACT["max_per_day"])
    max_per_day = max(1, min(max_per_day, 3))
    allowed_types = contact.get("allowed_types", DEFAULT_PROACTIVE_CONTACT["allowed_types"])
    if not isinstance(allowed_types, list):
        allowed_types = DEFAULT_PROACTIVE_CONTACT["allowed_types"]
    allowed_types = [
        str(item)
        for item in allowed_types
        if str(item) in PROACTIVE_CONTACT_TYPES
    ]
    if not allowed_types:
        allowed_types = list(DEFAULT_PROACTIVE_CONTACT["allowed_types"])
    base["proactive_contact"] = {
        "enabled": enabled,
        "max_per_day": max_per_day,
        "quiet_start": _normalize_time_of_day(contact.get("quiet_start"), DEFAULT_PROACTIVE_CONTACT["quiet_start"]),
        "quiet_end": _normalize_time_of_day(contact.get("quiet_end"), DEFAULT_PROACTIVE_CONTACT["quiet_end"]),
        "allowed_types": list(dict.fromkeys(allowed_types))[:4],
    }
    return base


def proactive_contact_settings(user_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            "SELECT preferences_json FROM user_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    try:
        preferences = json.loads((row["preferences_json"] if row else "{}") or "{}")
    except Exception:
        preferences = {}
    return normalize_profile_preferences(preferences)["proactive_contact"]


def proactive_contact_candidates(user_id: int, *, at_ts: int | None = None, limit: int = 5) -> dict[str, Any]:
    ts = int(at_ts or now_ts())
    settings = proactive_contact_settings(user_id)
    allowed_now = bool(settings.get("enabled"))
    blocked_reason = "" if allowed_now else "disabled_by_user"
    if allowed_now and _is_quiet_time(settings, ts):
        allowed_now = False
        blocked_reason = "quiet_hours"
    candidates = _candidate_rows(user_id, ts, limit=max(1, min(int(limit or 5), 20)))
    max_per_day = int(settings.get("max_per_day") or 1)
    allowed_types = set(settings.get("allowed_types") or [])
    candidates = [
        item
        for item in candidates
        if str(item.get("type") or "") in allowed_types
    ]
    candidates = candidates[:max_per_day]
    return {
        "settings": settings,
        "allowed_now": allowed_now,
        "blocked_reason": blocked_reason,
        "candidates": candidates if settings.get("enabled") else [],
    }


def record_proactive_contact_event(
    user_id: int,
    event_type: str,
    *,
    persona_id: int | None = None,
    conversation_id: int | None = None,
    candidate_type: str = "",
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event_type = str(event_type or "").strip()
    if event_type not in PROACTIVE_CONTACT_EVENT_TYPES:
        raise ValueError("unsupported_event_type")
    candidate_type = str(candidate_type or "").strip()
    if candidate_type and candidate_type not in PROACTIVE_CONTACT_TYPES:
        raise ValueError("unsupported_candidate_type")
    detail_json = json.dumps(detail or {}, ensure_ascii=False)
    ts = now_ts()
    with get_db() as db:
        owner = db.execute(
            """
            SELECT conversations.id AS conversation_id, conversations.persona_id
            FROM conversations
            WHERE conversations.id = ?
              AND conversations.user_id = ?
            """,
            (conversation_id, user_id),
        ).fetchone() if conversation_id is not None else None
        if conversation_id is not None and not owner:
            raise ValueError("conversation_not_found")
        final_persona_id = persona_id
        if owner:
            final_persona_id = int(owner["persona_id"])
        row_id = int(
            db.execute(
                """
                INSERT INTO proactive_contact_events (
                    user_id, persona_id, conversation_id, event_type, candidate_type, detail_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, final_persona_id, conversation_id, event_type, candidate_type, detail_json, ts),
            ).lastrowid
        )
        row = db.execute(
            "SELECT * FROM proactive_contact_events WHERE id = ?",
            (row_id,),
        ).fetchone()
    return _event_from_row(row)


def proactive_contact_events(user_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT *
            FROM proactive_contact_events
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, max(1, min(int(limit or 20), 100))),
        ).fetchall()
    return [_event_from_row(row) for row in rows]


def _candidate_rows(user_id: int, ts: int, *, limit: int) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT conversations.id AS conversation_id,
                   conversations.persona_id,
                   conversations.title,
                   conversations.updated_at,
                   personas.name AS persona_name,
                   personas.avatar_url AS persona_avatar_url,
                   (
                       SELECT messages.role
                       FROM messages
                       WHERE messages.conversation_id = conversations.id
                       ORDER BY messages.id DESC
                       LIMIT 1
                   ) AS last_role,
                   (
                       SELECT messages.content
                       FROM messages
                       WHERE messages.conversation_id = conversations.id
                       ORDER BY messages.id DESC
                       LIMIT 1
                   ) AS last_content,
                   (
                       SELECT messages.created_at
                       FROM messages
                       WHERE messages.conversation_id = conversations.id
                       ORDER BY messages.id DESC
                       LIMIT 1
                   ) AS last_message_at
            FROM conversations
            JOIN personas ON personas.id = conversations.persona_id
            WHERE conversations.user_id = ?
              AND conversations.status = 'active'
              AND personas.status = 'active'
            ORDER BY conversations.updated_at DESC
            LIMIT ?
            """,
            (user_id, max(limit * 3, limit)),
        ).fetchall()
    candidates = []
    for row in rows:
        item = dict_from_row(row) or {}
        last_at = int(item.get("last_message_at") or 0)
        if not last_at or ts - last_at < PROACTIVE_CONTACT_MIN_IDLE_SECONDS:
            continue
        last_role = str(item.get("last_role") or "")
        if last_role not in {"user", "assistant"}:
            continue
        candidate_type = "followup" if last_role == "user" else "care"
        candidates.append({
            "type": candidate_type,
            "conversation_id": int(item["conversation_id"]),
            "persona_id": int(item["persona_id"]),
            "persona_name": str(item.get("persona_name") or ""),
            "persona_avatar_url": str(item.get("persona_avatar_url") or ""),
            "reason": "old_user_message" if last_role == "user" else "long_idle",
            "last_message_at": last_at,
            "idle_seconds": max(0, ts - last_at),
            "last_excerpt": str(item.get("last_content") or "")[:80],
            "draft_text": _draft_text(candidate_type),
        })
        if len(candidates) >= limit:
            break
    return candidates


def _event_from_row(row: Any) -> dict[str, Any]:
    item = dict_from_row(row) or {}
    try:
        detail = json.loads(str(item.get("detail_json") or "{}"))
    except Exception:
        detail = {}
    item["detail"] = detail if isinstance(detail, dict) else {}
    item.pop("detail_json", None)
    return item


def _draft_text(candidate_type: str) -> str:
    if candidate_type == "followup":
        return "我想起你前面说的事，想问问现在怎么样了。"
    return "我过来轻轻问一声：你现在还好吗？"


def _is_quiet_time(settings: dict[str, Any], ts: int) -> bool:
    current = datetime.fromtimestamp(ts).strftime("%H:%M")
    start = str(settings.get("quiet_start") or DEFAULT_PROACTIVE_CONTACT["quiet_start"])
    end = str(settings.get("quiet_end") or DEFAULT_PROACTIVE_CONTACT["quiet_end"])
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _normalize_time_of_day(value: Any, default: str) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
        return text
    return default
