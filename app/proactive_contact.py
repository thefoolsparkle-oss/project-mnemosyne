from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from .database import dict_from_row, get_db, now_ts


PROACTIVE_CONTACT_TYPES = {"followup", "care", "reminder", "interest"}
PROACTIVE_CONTACT_EVENT_TYPES = {"candidate_opened", "candidate_seen", "candidate_dismissed", "candidate_replied"}
DEFAULT_PROACTIVE_CONTACT = {
    "enabled": False,
    "max_per_day": 1,
    "quiet_start": "22:00",
    "quiet_end": "09:00",
    "allowed_types": ["followup", "care", "reminder"],
}
PROACTIVE_CONTACT_MIN_IDLE_SECONDS = 6 * 60 * 60
SENSITIVE_BLOCK_PATTERNS = {
    "self_harm": r"自杀|轻生|不想活|伤害自己|suicide|kill myself|self[-\s]?harm",
    "violence_or_abuse": r"家暴|性侵|强迫|威胁我|要杀|打死|violence|abuse|assault",
    "medical_or_legal_crisis": r"急诊|抢救|报警|起诉|被抓|emergency|lawsuit|arrested",
}
SENSITIVE_WATCH_PATTERNS = {
    "emotional_distress": r"难过|崩溃|焦虑|抑郁|失眠|分手|去世|死亡|葬礼|panic|depress|grief",
    "high_stakes_life": r"裁员|失业|欠债|考试|手术|住院|debt|surgery|hospital",
}


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


def proactive_contact_candidates(
    user_id: int,
    *,
    at_ts: int | None = None,
    limit: int = 5,
    include_blocked: bool = False,
) -> dict[str, Any]:
    ts = int(at_ts or now_ts())
    settings = proactive_contact_settings(user_id)
    allowed_now = bool(settings.get("enabled"))
    blocked_reason = "" if allowed_now else "disabled_by_user"
    if allowed_now and _is_quiet_time(settings, ts):
        allowed_now = False
        blocked_reason = "quiet_hours"
    requested_limit = max(1, min(int(limit or 5), 20))
    candidates = _candidate_rows(user_id, ts, limit=max(requested_limit * 4, requested_limit))
    max_per_day = int(settings.get("max_per_day") or 1)
    usage_today = proactive_contact_daily_usage(user_id, at_ts=ts)
    remaining_today = max(0, max_per_day - usage_today)
    if allowed_now and remaining_today <= 0:
        allowed_now = False
        blocked_reason = "daily_limit"
    allowed_types = set(settings.get("allowed_types") or [])
    feedback_policy = proactive_contact_feedback_policy(user_id)
    suppressed_types = set(feedback_policy.get("suppressed_types") or [])
    handled_today = _handled_candidates_today(user_id, ts)
    topic_boundaries = _user_topic_boundaries(user_id)
    reviewed_candidates = []
    blocked_candidates = []
    for item in candidates:
        candidate_type = str(item.get("type") or "")
        if candidate_type not in allowed_types:
            continue
        if (int(item.get("conversation_id") or 0), candidate_type) in handled_today:
            continue
        reviewed = _with_arbitration(
            item,
            feedback_policy=feedback_policy,
            topic_boundaries=topic_boundaries,
            suppressed_types=suppressed_types,
        )
        if reviewed.get("risk_level") == "blocked":
            blocked_candidates.append(reviewed)
        else:
            reviewed_candidates.append(reviewed)
    candidates = reviewed_candidates
    if blocked_reason == "daily_limit":
        candidates = []
        blocked_candidates = []
    else:
        candidate_limit = remaining_today if allowed_now else max_per_day
        candidates = candidates[:max(0, candidate_limit)]
        blocked_candidates = blocked_candidates[:max(0, requested_limit)]
    return {
        "settings": settings,
        "allowed_now": allowed_now,
        "blocked_reason": blocked_reason,
        "usage_today": usage_today,
        "remaining_today": remaining_today,
        "feedback_policy": feedback_policy,
        "candidates": candidates if settings.get("enabled") else [],
        "blocked_candidates": blocked_candidates if include_blocked and settings.get("enabled") else [],
        "arbitration_summary": {
            "low": sum(1 for item in candidates if item.get("risk_level") == "low"),
            "watch": sum(1 for item in candidates if item.get("risk_level") == "watch"),
            "blocked": len(blocked_candidates) if include_blocked and settings.get("enabled") else 0,
        },
    }


def proactive_contact_daily_usage(user_id: int, *, at_ts: int | None = None) -> int:
    ts = int(at_ts or now_ts())
    start_ts = _local_day_start_ts(ts)
    end_ts = start_ts + 24 * 60 * 60
    with get_db() as db:
        row = db.execute(
            """
            SELECT COUNT(*) AS count
            FROM proactive_contact_events
            WHERE user_id = ?
              AND event_type IN ('candidate_opened', 'candidate_seen')
              AND created_at >= ?
              AND created_at < ?
              AND created_at <= ?
            """,
            (user_id, start_ts, end_ts, ts),
        ).fetchone()
    return int(row["count"] if row else 0)


def _handled_candidates_today(user_id: int, ts: int) -> set[tuple[int, str]]:
    start_ts = _local_day_start_ts(ts)
    end_ts = start_ts + 24 * 60 * 60
    with get_db() as db:
        rows = db.execute(
            """
            SELECT conversation_id, candidate_type
            FROM proactive_contact_events
            WHERE user_id = ?
              AND event_type IN ('candidate_opened', 'candidate_seen', 'candidate_dismissed', 'candidate_replied')
              AND conversation_id IS NOT NULL
              AND created_at >= ?
              AND created_at < ?
              AND created_at <= ?
            """,
            (user_id, start_ts, end_ts, ts),
        ).fetchall()
    return {
        (int(row["conversation_id"]), str(row["candidate_type"] or ""))
        for row in rows
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


def proactive_contact_event_summary(user_id: int, *, days: int = 30) -> dict[str, Any]:
    window_days = max(1, min(int(days or 30), 180))
    start_ts = now_ts() - window_days * 24 * 60 * 60
    by_event = {event_type: 0 for event_type in sorted(PROACTIVE_CONTACT_EVENT_TYPES)}
    by_type: dict[str, dict[str, int]] = {}
    with get_db() as db:
        rows = db.execute(
            """
            SELECT event_type, candidate_type, COUNT(*) AS count
            FROM proactive_contact_events
            WHERE user_id = ?
              AND created_at >= ?
            GROUP BY event_type, candidate_type
            """,
            (user_id, start_ts),
        ).fetchall()
    for row in rows:
        event_type = str(row["event_type"] or "")
        candidate_type = str(row["candidate_type"] or "unknown")
        count = int(row["count"] or 0)
        by_event[event_type] = by_event.get(event_type, 0) + count
        by_type.setdefault(candidate_type, {})[event_type] = count
    opened = by_event.get("candidate_opened", 0)
    replied = by_event.get("candidate_replied", 0)
    dismissed = by_event.get("candidate_dismissed", 0)
    return {
        "window_days": window_days,
        "by_event": by_event,
        "by_type": by_type,
        "opened": opened,
        "replied": replied,
        "dismissed": dismissed,
        "reply_rate": round(replied / opened, 3) if opened else 0,
        "dismiss_rate": round(dismissed / opened, 3) if opened else 0,
    }


def proactive_contact_feedback_policy(user_id: int, *, days: int = 30) -> dict[str, Any]:
    summary = proactive_contact_event_summary(user_id, days=days)
    suppressed_types = []
    reasons: dict[str, str] = {}
    for candidate_type, counts in (summary.get("by_type") or {}).items():
        dismissed = int(counts.get("candidate_dismissed") or 0)
        replied = int(counts.get("candidate_replied") or 0)
        opened = int(counts.get("candidate_opened") or 0) + int(counts.get("candidate_seen") or 0)
        if dismissed >= 2 and replied == 0 and dismissed >= opened:
            suppressed_types.append(str(candidate_type))
            reasons[str(candidate_type)] = "recent_dismissals_without_replies"
    return {
        "window_days": int(summary.get("window_days") or days),
        "suppressed_types": sorted(suppressed_types),
        "reasons": reasons,
    }


def _candidate_rows(user_id: int, ts: int, *, limit: int) -> list[dict[str, Any]]:
    candidates = [
        *_reminder_candidate_rows(user_id, ts, limit=limit),
        *_interest_candidate_rows(user_id, ts, limit=limit),
        *_conversation_candidate_rows(user_id, ts, limit=limit),
    ]
    candidates = sorted(
        candidates,
        key=lambda item: (
            -float(item.get("priority") or 0),
            -int(item.get("last_message_at") or 0),
            int(item.get("conversation_id") or 0),
            str(item.get("type") or ""),
        ),
    )
    return candidates[:limit]


def _conversation_candidate_rows(user_id: int, ts: int, *, limit: int) -> list[dict[str, Any]]:
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
                   , conversation_summaries.summary_text
                   , conversation_summaries.key_points_json
            FROM conversations
            JOIN personas ON personas.id = conversations.persona_id
            LEFT JOIN conversation_summaries
              ON conversation_summaries.conversation_id = conversations.id
             AND conversation_summaries.status = 'active'
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
        memory_basis = _candidate_memory_basis(item, candidate_type)
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
            "memory_basis": memory_basis,
            "risk_notes": _candidate_risk_notes(memory_basis, candidate_type),
            "draft_text": _draft_text(candidate_type),
            "priority": 0.7 if candidate_type == "followup" else 0.45,
        })
        if len(candidates) >= limit:
            break
    return candidates


def _interest_candidate_rows(user_id: int, ts: int, *, limit: int) -> list[dict[str, Any]]:
    avoided_topics = set(_user_topic_boundaries(user_id))
    disliked_topics = set(_user_disliked_topics(user_id))
    with get_db() as db:
        rows = db.execute(
            """
            SELECT memory_relations.uid,
                   memory_relations.persona_id,
                   memory_relations.conversation_id,
                   memory_relations.object,
                   memory_relations.text,
                   memory_relations.updated_at,
                   personas.name AS persona_name,
                   personas.avatar_url AS persona_avatar_url
            FROM memory_relations
            JOIN personas ON personas.id = memory_relations.persona_id
            WHERE memory_relations.user_id = ?
              AND memory_relations.archived = 0
              AND memory_relations.valid_to IS NULL
              AND memory_relations.predicate = 'preference'
              AND memory_relations.conversation_id IS NOT NULL
              AND personas.status = 'active'
            ORDER BY memory_relations.updated_at DESC
            LIMIT ?
            """,
            (user_id, max(1, min(limit * 3, 30))),
        ).fetchall()
    candidates: list[dict[str, Any]] = []
    seen_topics: set[str] = set()
    for row in rows:
        item = dict_from_row(row) or {}
        topic = str(item.get("object") or _preference_topic(str(item.get("text") or ""))).strip()
        if not topic or topic in seen_topics:
            continue
        if topic in avoided_topics or topic in disliked_topics:
            continue
        text = str(item.get("text") or "")
        if _is_negative_preference(text):
            continue
        if not (_is_positive_preference(text) or topic):
            continue
        source_ts = int(item.get("updated_at") or 0)
        if not source_ts or ts - source_ts < PROACTIVE_CONTACT_MIN_IDLE_SECONDS:
            continue
        memory_basis = {
            "strength": "direct",
            "has_summary": False,
            "evidence": [
                {"kind": "preference_memory", "text": text[:120]},
            ],
        }
        candidates.append({
            "type": "interest",
            "conversation_id": int(item["conversation_id"]),
            "persona_id": int(item["persona_id"]),
            "persona_name": str(item.get("persona_name") or ""),
            "persona_avatar_url": str(item.get("persona_avatar_url") or ""),
            "reason": "explicit_interest",
            "last_message_at": source_ts,
            "idle_seconds": max(0, ts - source_ts),
            "last_excerpt": text[:80],
            "memory_basis": memory_basis,
            "risk_notes": _candidate_risk_notes(memory_basis, "interest"),
            "draft_text": _draft_text("interest"),
            "priority": 0.35,
            "source_uid": str(item.get("uid") or ""),
            "topic": topic,
        })
        seen_topics.add(topic)
        if len(candidates) >= limit:
            break
    return candidates


def _reminder_candidate_rows(user_id: int, ts: int, *, limit: int) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT memory_state.persona_id,
                   memory_state.key,
                   memory_state.value_json,
                   memory_state.source_uids_json,
                   memory_state.updated_at,
                   personas.name AS persona_name,
                   personas.avatar_url AS persona_avatar_url
            FROM memory_state
            JOIN personas ON personas.id = memory_state.persona_id
            WHERE memory_state.user_id = ?
              AND memory_state.key = 'dynamic.reminders.active'
              AND personas.status = 'active'
            ORDER BY memory_state.updated_at DESC
            LIMIT ?
            """,
            (user_id, max(1, min(limit * 2, 20))),
        ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        state_row = dict_from_row(row) or {}
        state_items = _json_list(state_row.get("value_json"))
        row_source_uids = _json_list(state_row.get("source_uids_json"))
        for item in state_items:
            if not isinstance(item, dict):
                continue
            if str(item.get("lifecycle") or "") in {"expired", "resolved"}:
                continue
            if str(item.get("injection_policy") or "") == "recall_only":
                continue
            source_uid = str(item.get("source_uid") or (row_source_uids[0] if row_source_uids else "")).strip()
            source = _source_memory_for_uid(user_id, source_uid)
            if not source or not source.get("conversation_id"):
                continue
            source_ts = int(source.get("updated_at") or item.get("updated_at") or state_row.get("updated_at") or 0)
            if not source_ts or ts - source_ts < PROACTIVE_CONTACT_MIN_IDLE_SECONDS:
                continue
            reminder_text = str(item.get("text") or item.get("value") or source.get("text") or "").strip()
            if not reminder_text:
                continue
            memory_basis = {
                "strength": "direct",
                "has_summary": False,
                "evidence": [
                    {"kind": "dynamic_state", "text": reminder_text[:120]},
                    {"kind": "source_memory", "text": str(source.get("text") or "")[:120]},
                ],
            }
            candidates.append({
                "type": "reminder",
                "conversation_id": int(source["conversation_id"]),
                "persona_id": int(state_row["persona_id"]),
                "persona_name": str(state_row.get("persona_name") or ""),
                "persona_avatar_url": str(state_row.get("persona_avatar_url") or ""),
                "reason": "active_reminder",
                "last_message_at": source_ts,
                "idle_seconds": max(0, ts - source_ts),
                "last_excerpt": str(source.get("text") or reminder_text)[:80],
                "memory_basis": memory_basis,
                "risk_notes": _candidate_risk_notes(memory_basis, "reminder"),
                "draft_text": _draft_text("reminder"),
                "priority": 1.0 + float(item.get("urgency") or 0) * 0.2,
                "source_uid": source_uid,
            })
            if len(candidates) >= limit:
                return candidates
    return candidates


def _source_memory_for_uid(user_id: int, uid: str) -> dict[str, Any] | None:
    if not uid:
        return None
    with get_db() as db:
        row = db.execute(
            """
            SELECT uid, user_id, persona_id, conversation_id, text, updated_at
            FROM memory_facts
            WHERE user_id = ? AND uid = ? AND archived = 0
            UNION ALL
            SELECT uid, user_id, persona_id, conversation_id, text, updated_at
            FROM memory_relations
            WHERE user_id = ? AND uid = ? AND archived = 0
            LIMIT 1
            """,
            (user_id, uid, user_id, uid),
        ).fetchone()
    return dict_from_row(row) if row else None


def _candidate_memory_basis(item: dict[str, Any], candidate_type: str) -> dict[str, Any]:
    last_excerpt = str(item.get("last_content") or "").strip()[:80]
    summary_text = str(item.get("summary_text") or "").strip()
    key_points = _decode_key_points(item.get("key_points_json"))[:3]
    evidence = []
    if last_excerpt:
        evidence.append({"kind": "last_message", "text": last_excerpt})
    if key_points:
        for point in key_points:
            evidence.append({"kind": "key_point", "text": point[:120]})
    elif summary_text:
        evidence.append({"kind": "summary", "text": summary_text[:120]})
    strength = "weak"
    if candidate_type == "followup" and last_excerpt:
        strength = "direct"
    elif key_points or summary_text:
        strength = "contextual"
    return {
        "strength": strength,
        "has_summary": bool(summary_text or key_points),
        "evidence": evidence[:4],
    }


def _candidate_risk_notes(memory_basis: dict[str, Any], candidate_type: str) -> list[str]:
    notes = []
    if not (memory_basis.get("evidence") or []):
        notes.append("no_memory_basis")
    if candidate_type == "care" and memory_basis.get("strength") == "weak":
        notes.append("long_idle_only")
    return notes


def _with_arbitration(
    item: dict[str, Any],
    *,
    feedback_policy: dict[str, Any],
    topic_boundaries: list[str],
    suppressed_types: set[str],
) -> dict[str, Any]:
    reviewed = dict(item)
    candidate_type = str(reviewed.get("type") or "")
    risk_notes = list(reviewed.get("risk_notes") or [])
    reasons = []
    decision = "allow"
    risk_level = "low"

    if candidate_type in suppressed_types:
        reasons.append("recent_dismissals_without_replies")
        decision = "block"
        risk_level = "blocked"

    if "no_memory_basis" in risk_notes:
        reasons.append("no_memory_basis")
        decision = "block"
        risk_level = "blocked"
    elif "long_idle_only" in risk_notes:
        reasons.append("long_idle_only")
        if decision != "block":
            decision = "watch"
            risk_level = "watch"
    else:
        reasons.append(f"memory_basis_{(reviewed.get('memory_basis') or {}).get('strength') or 'weak'}")

    boundary_hits = _topic_boundary_hits(reviewed, topic_boundaries)
    if boundary_hits:
        reviewed["boundary_hits"] = boundary_hits
        reasons.append("topic_boundary_match")
        decision = "block"
        risk_level = "blocked"

    sensitivity = _sensitivity_hits(reviewed)
    if sensitivity["blocked"]:
        reviewed["sensitivity"] = sensitivity
        reasons.append("sensitive_content_blocked")
        decision = "block"
        risk_level = "blocked"
    elif sensitivity["watch"]:
        reviewed["sensitivity"] = sensitivity
        reasons.append("sensitive_content_watch")
        if decision != "block":
            decision = "watch"
            risk_level = "watch"

    reviewed["risk_level"] = risk_level
    reviewed["arbitration"] = {
        "decision": decision,
        "reasons": list(dict.fromkeys(reasons)),
        "policy": "proactive_contact_local_rules_v1",
    }
    return reviewed


def _topic_boundary_hits(item: dict[str, Any], topics: list[str]) -> list[str]:
    haystack = _candidate_text_blob(item).lower()
    hits = []
    for topic in topics:
        topic_text = str(topic or "").strip()
        if topic_text and topic_text.lower() in haystack:
            hits.append(topic_text)
    return list(dict.fromkeys(hits))[:5]


def _sensitivity_hits(item: dict[str, Any]) -> dict[str, list[str]]:
    haystack = _candidate_text_blob(item)
    blocked = [
        key
        for key, pattern in SENSITIVE_BLOCK_PATTERNS.items()
        if re.search(pattern, haystack, flags=re.IGNORECASE)
    ]
    watch = [
        key
        for key, pattern in SENSITIVE_WATCH_PATTERNS.items()
        if re.search(pattern, haystack, flags=re.IGNORECASE)
    ]
    return {"blocked": blocked, "watch": watch}


def _candidate_text_blob(item: dict[str, Any]) -> str:
    parts = [
        str(item.get("last_excerpt") or ""),
        str(item.get("reason") or ""),
        str(item.get("draft_text") or ""),
        str(item.get("topic") or ""),
    ]
    basis = item.get("memory_basis") or {}
    for evidence in basis.get("evidence") or []:
        if isinstance(evidence, dict):
            parts.append(str(evidence.get("text") or ""))
    return "\n".join(parts)


def _user_topic_boundaries(user_id: int) -> list[str]:
    topics = []
    with get_db() as db:
        insight = db.execute(
            "SELECT topic_model_json, guidance_json FROM user_insights WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        memory_rows = db.execute(
            """
            SELECT text, object
            FROM memory_relations
            WHERE user_id = ?
              AND archived = 0
              AND predicate = 'boundary'
              AND valid_to IS NULL
            ORDER BY updated_at DESC
            LIMIT 50
            """,
            (user_id,),
        ).fetchall()
        flat_rows = db.execute(
            """
            SELECT text
            FROM memories
            WHERE user_id = ?
              AND archived = 0
              AND type = 'boundary'
            ORDER BY updated_at DESC
            LIMIT 50
            """,
            (user_id,),
        ).fetchall()
    if insight:
        topic_model = _json_dict(insight["topic_model_json"])
        guidance = _json_dict(insight["guidance_json"])
        topics.extend(str(item) for item in topic_model.get("avoid_topics") or [])
        for rule in guidance.get("do_not") or []:
            topics.extend(_topics_from_boundary_text(str(rule)))
    for row in memory_rows:
        text = str(row["text"] or "")
        obj = str(row["object"] or "").strip()
        if "不要主动提" in text and obj:
            topics.append(obj)
        topics.extend(_topics_from_boundary_text(text))
    for row in flat_rows:
        topics.extend(_topics_from_boundary_text(str(row["text"] or "")))
    return [item for item in dict.fromkeys(topic.strip(" 。.!?") for topic in topics) if item]


def _user_disliked_topics(user_id: int) -> list[str]:
    topics = []
    with get_db() as db:
        rows = db.execute(
            """
            SELECT object, text
            FROM memory_relations
            WHERE user_id = ?
              AND archived = 0
              AND valid_to IS NULL
              AND predicate = 'preference'
            ORDER BY updated_at DESC
            LIMIT 80
            """,
            (user_id,),
        ).fetchall()
    for row in rows:
        text = str(row["text"] or "")
        if _is_negative_preference(text):
            topics.append(str(row["object"] or _preference_topic(text)))
    return [item for item in dict.fromkeys(topic.strip(" 。.!?") for topic in topics) if item]


def _is_positive_preference(text: str) -> bool:
    return "喜欢" in text and not _is_negative_preference(text)


def _is_negative_preference(text: str) -> bool:
    lowered = text.lower()
    return any(marker in text for marker in ("不喜欢", "讨厌", "不想聊", "别主动提", "不要主动提")) or any(
        marker in lowered
        for marker in ("dislike", "do not proactively bring up", "don't proactively bring up")
    )


def _preference_topic(text: str) -> str:
    for pattern in (r"用户(?:很|最)?喜欢(.+)", r"用户(?:不喜欢|讨厌)(.+)"):
        match = re.search(pattern, text)
        if match:
            return str(match.group(1) or "").strip(" 。.!?")
    return ""


def _topics_from_boundary_text(text: str) -> list[str]:
    patterns = [
        r"Do not proactively bring up\s+(.+?)(?:[.。]|$)",
        r"不要(?:再)?主动(?:提|聊|提起|说起)\s*([^，。！？,.!?]{1,40})",
        r"别(?:再)?主动(?:提|聊|提起|说起)\s*([^，。！？,.!?]{1,40})",
    ]
    topics = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            topic = str(match.group(1) or "").strip()
            if topic:
                topics.append(topic)
    return topics


def _json_dict(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except Exception:
        value = {}
    return value if isinstance(value, dict) else {}


def _json_list(raw: Any) -> list[Any]:
    try:
        value = json.loads(str(raw or "[]"))
    except Exception:
        value = []
    return value if isinstance(value, list) else []


def _decode_key_points(raw: Any) -> list[str]:
    try:
        value = json.loads(str(raw or "[]"))
    except Exception:
        value = []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


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
    if candidate_type == "reminder":
        return "我记得你之前让我提醒这件事，轻轻提一下。"
    if candidate_type == "interest":
        return "我想起你之前喜欢的东西，想轻轻接一句。"
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


def _local_day_start_ts(ts: int) -> int:
    return int(datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
