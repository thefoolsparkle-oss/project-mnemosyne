from __future__ import annotations

import json
from typing import Any

from .database import dict_from_row, get_db, now_ts
from .identity import scrub_identity_obj, scrub_identity_text
from .llm_client import call_llm_api


CONVERSATION_SUMMARY_SYSTEM = """You are a conversation summarizer for a long-term companion chat system.
Maintain a compact rolling summary of the conversation for continuity.
First respect the supplied state snapshot and pre-extracted continuity points, then summarize the new messages.
Preserve facts that affect continuity: promises, unresolved topics, emotional context, relationship tone, user instructions, state changes, and contradictions.
Do not invent facts.
Return strict JSON only:
{
  "summary_text": "compact summary in the same language as the chat when possible",
  "key_points": ["short point"],
  "state_updates": ["state-like point that should stay visible"],
  "open_threads": ["unresolved topic or promise"]
}
"""


def get_conversation_summary(user_id: int, persona_id: int, conversation_id: int) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute(
            """
            SELECT *
            FROM conversation_summaries
            WHERE user_id = ? AND persona_id = ? AND conversation_id = ? AND status = 'active'
            """,
            (user_id, persona_id, conversation_id),
        ).fetchone()
    summary = dict_from_row(row)
    return _decode(summary) if summary else None


def conversation_summary_prompt(user_id: int, persona_id: int, conversation_id: int) -> str:
    summary = get_conversation_summary(user_id, persona_id, conversation_id)
    if not summary or not summary.get("summary_text"):
        return "Conversation rolling summary: no prior summary yet."
    points = summary.get("key_points", [])
    lines = [
        "Conversation rolling summary:",
        str(summary.get("summary_text") or ""),
    ]
    if points:
        lines.append("Key continuity points:")
        for point in points[:12]:
            lines.append(f"- {point}")
    lines.append("Use this for continuity, but prefer explicit newer user messages when there is a conflict.")
    return "\n".join(lines)


def refresh_conversation_summary(
    *,
    user_id: int,
    persona_id: int,
    conversation_id: int,
    latest_message_id: int,
) -> dict[str, Any]:
    previous = get_conversation_summary(user_id, persona_id, conversation_id)
    covered_id = int(previous.get("covered_message_id") or 0) if previous else 0
    new_messages = _messages_after(user_id, persona_id, conversation_id, covered_id, latest_message_id)
    if not new_messages:
        return previous or {}

    state_snapshot = _state_snapshot(user_id, persona_id)
    pre_points = _pre_extract_key_points(new_messages, state_snapshot)
    modeled = _summarize_with_llm(previous, new_messages, state_snapshot, pre_points) or _fallback_summary(
        previous,
        new_messages,
        state_snapshot,
        pre_points,
    )
    modeled = scrub_identity_obj(modeled)
    summary_text = scrub_identity_text(str(modeled.get("summary_text") or "").strip())[:6000]
    key_points = _merge_points(
        pre_points,
        _as_list(modeled.get("key_points")),
        _as_list(modeled.get("state_updates")),
        _as_list(modeled.get("open_threads")),
    )[:50]
    key_points = scrub_identity_obj(key_points)
    if not summary_text and key_points:
        summary_text = "\n".join(key_points)
    ts = now_ts()
    source_count = int(previous.get("source_message_count") or 0) + len(new_messages) if previous else len(new_messages)

    with get_db() as db:
        db.execute(
            """
            INSERT INTO conversation_summaries (
                user_id, persona_id, conversation_id, summary_text, key_points_json,
                covered_message_id, source_message_count, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            ON CONFLICT(conversation_id)
            DO UPDATE SET summary_text = excluded.summary_text,
                          key_points_json = excluded.key_points_json,
                          covered_message_id = excluded.covered_message_id,
                          source_message_count = excluded.source_message_count,
                          status = 'active',
                          updated_at = excluded.updated_at
            """,
            (
                user_id,
                persona_id,
                conversation_id,
                summary_text,
                json.dumps(key_points, ensure_ascii=False),
                latest_message_id,
                source_count,
                ts,
                ts,
            ),
        )
        db.execute(
            "UPDATE conversations SET summary = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (summary_text[:2000], ts, conversation_id, user_id),
        )
        row = db.execute(
            "SELECT * FROM conversation_summaries WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
    return _decode(dict_from_row(row) or {})


def list_conversation_summaries(user_id: int, persona_id: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    params: list[Any] = [user_id]
    persona_clause = ""
    if persona_id is not None:
        persona_clause = "AND persona_id = ?"
        params.append(persona_id)
    params.append(limit)
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT *
            FROM conversation_summaries
            WHERE user_id = ? {persona_clause} AND status = 'active'
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_decode(dict_from_row(row) or {}) for row in rows]


def _messages_after(
    user_id: int,
    persona_id: int,
    conversation_id: int,
    covered_id: int,
    latest_message_id: int,
) -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id, role, content, created_at
            FROM messages
            WHERE user_id = ? AND persona_id = ? AND conversation_id = ?
              AND id > ? AND id <= ?
            ORDER BY id ASC
            LIMIT 80
            """,
            (user_id, persona_id, conversation_id, covered_id, latest_message_id),
        ).fetchall()
    return [dict_from_row(row) or {} for row in rows]


def _summarize_with_llm(
    previous: dict[str, Any] | None,
    new_messages: list[dict[str, Any]],
    state_snapshot: dict[str, Any],
    pre_points: list[str],
) -> dict[str, Any] | None:
    payload = {
        "previous_summary": previous or {},
        "state_snapshot": state_snapshot,
        "pre_extracted_key_points": pre_points,
        "new_messages": [
            {"id": item["id"], "role": item["role"], "content": item["content"]}
            for item in new_messages
        ],
    }
    try:
        raw = call_llm_api(
            [
                {"role": "system", "content": CONVERSATION_SUMMARY_SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
            ],
            task="summary",
        )
        data = _extract_json(raw)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        print("[ConversationSummary] LLM skipped:", exc)
        return None


def _fallback_summary(
    previous: dict[str, Any] | None,
    new_messages: list[dict[str, Any]],
    state_snapshot: dict[str, Any],
    pre_points: list[str],
) -> dict[str, Any]:
    old_text = str((previous or {}).get("summary_text") or "").strip()
    points = _as_list((previous or {}).get("key_points"))
    snippets = []
    for item in new_messages[-12:]:
        role = str(item.get("role") or "")
        content = str(item.get("content") or "").replace("\n", " ").strip()
        if content:
            snippets.append(f"{role}: {content[:240]}")
    new_text = "\n".join(snippets)
    summary_text = "\n".join(part for part in [old_text, new_text] if part).strip()
    if len(summary_text) > 6000:
        summary_text = summary_text[-6000:]
    state_points = _state_points(state_snapshot)
    points = _merge_points(points, state_points, pre_points, snippets[-8:])
    return {"summary_text": summary_text, "key_points": points[-50:]}


def _state_snapshot(user_id: int, persona_id: int) -> dict[str, Any]:
    # Imported lazily to avoid a module cycle at import time.
    from .layered_memory import refresh_memory_state
    from .mirror import get_user_insight

    state = refresh_memory_state(user_id, persona_id)
    insight = get_user_insight(user_id)
    return {
        "memory_state": state,
        "user_model": {
            "profile_summary": insight.get("profile_summary", ""),
            "interaction_style": insight.get("interaction_style", []),
            "topic_model": insight.get("topic_model", {}),
            "guidance": insight.get("guidance", {}),
        },
    }


def _pre_extract_key_points(new_messages: list[dict[str, Any]], state_snapshot: dict[str, Any]) -> list[str]:
    points = _state_points(state_snapshot)
    markers = (
        "记住",
        "以后",
        "不要",
        "别",
        "喜欢",
        "讨厌",
        "希望",
        "叫我",
        "称呼",
        "计划",
        "明天",
        "下次",
        "答应",
        "需要",
        "不喜欢",
    )
    for item in new_messages:
        role = str(item.get("role") or "")
        content = " ".join(str(item.get("content") or "").split())
        if not content:
            continue
        if role == "user" and any(marker in content for marker in markers):
            points.append(f"user_state_signal: {content[:220]}")
        elif role == "assistant" and any(marker in content for marker in ("答应", "会记住", "下次", "我会", "不会")):
            points.append(f"assistant_commitment: {content[:220]}")
    return _merge_points(points)[:30]


def _state_points(state_snapshot: dict[str, Any]) -> list[str]:
    state = state_snapshot.get("memory_state") if isinstance(state_snapshot, dict) else {}
    if not isinstance(state, dict):
        return []
    points = []
    for key in ("preferred_address", "forbidden_addresses", "likes", "dislikes", "interaction_style", "relationship_state"):
        value = state.get(key)
        if value not in (None, "", []):
            points.append(f"state.{key}: {json.dumps(value, ensure_ascii=False)}")
    dynamic = state.get("dynamic_state") if isinstance(state.get("dynamic_state"), dict) else {}
    for key, value in list(dynamic.items())[:8]:
        if value not in (None, "", []):
            points.append(f"state.dynamic.{key}: {json.dumps(value, ensure_ascii=False)}")
    return points


def _merge_points(*groups: list[str]) -> list[str]:
    merged = []
    seen = set()
    for group in groups:
        for point in _as_list(group):
            point = " ".join(str(point).split())[:500]
            if not point or point in seen:
                continue
            seen.add(point)
            merged.append(point)
    return merged


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    try:
        row["key_points"] = json.loads(row.pop("key_points_json") or "[]")
    except Exception:
        row["key_points"] = []
    if not isinstance(row["key_points"], list):
        row["key_points"] = []
    return row


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []
