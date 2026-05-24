from __future__ import annotations

import json
import re
from typing import Any

from .database import dict_from_row, get_db, now_ts
from .identity import scrub_identity_obj, scrub_identity_text
from .llm_client import call_llm_api
from .memory_policy import should_use_llm_for_mirror


MIRROR_SYSTEM = """You are Mirror, a user modeling agent for a long-term companion chat system.
Build a practical, non-clinical user profile for conversation adaptation.
Do not diagnose mental illness. Do not infer protected attributes.
Focus on what the chat persona should do next: tone, topics to avoid, topics to use, support style, and boundaries.

Return strict JSON only:
{
  "profile_summary": "short stable summary",
  "interaction_style": ["direct instruction for reply style"],
  "emotional_patterns": ["non-clinical support pattern"],
  "topic_model": {
    "likes": ["topic"],
    "dislikes": ["topic"],
    "avoid_topics": ["topic"],
    "safe_topics": ["topic"]
  },
  "guidance": {
    "tone_rules": ["rule"],
    "topic_rules": ["rule"],
    "support_rules": ["rule"],
    "do_not": ["rule"]
  }
}
"""


def ensure_user_insight(user_id: int) -> dict:
    ts = now_ts()
    with get_db() as db:
        row = db.execute("SELECT * FROM user_insights WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            return _public_insight(dict_from_row(row) or {})

        db.execute(
            """
            INSERT INTO user_insights (
                user_id, profile_summary, interaction_style, emotional_patterns_json,
                inferred_profile_json, topic_model_json, guidance_json, updated_at
            )
            VALUES (?, '', '', '[]', '{}', '{}', '{}', ?)
            """,
            (user_id, ts),
        )
        row = db.execute("SELECT * FROM user_insights WHERE user_id = ?", (user_id,)).fetchone()
    return _public_insight(dict_from_row(row) or {})


def update_interaction_insight(user_id: int, user_text: str, stored_memories: list[dict]) -> dict:
    current = ensure_user_insight(user_id)
    memory_context = _memory_context(user_id, stored_memories)
    modeled = _analyze_with_llm(current, user_text, stored_memories, memory_context) if should_use_llm_for_mirror(user_text, stored_memories) else None
    if not modeled:
        modeled = _analyze_with_rules(current, user_text, stored_memories, memory_context)
    merged = _merge_insights(current, modeled)
    ts = now_ts()

    with get_db() as db:
        db.execute(
            """
            UPDATE user_insights
            SET profile_summary = ?, interaction_style = ?, emotional_patterns_json = ?,
                inferred_profile_json = ?, topic_model_json = ?, guidance_json = ?,
                updated_at = ?
            WHERE user_id = ?
            """,
            (
                merged["profile_summary"],
                " ".join(merged["interaction_style"])[:1600],
                json.dumps(merged["emotional_patterns"], ensure_ascii=False),
                json.dumps(merged["inferred_profile"], ensure_ascii=False),
                json.dumps(merged["topic_model"], ensure_ascii=False),
                json.dumps(merged["guidance"], ensure_ascii=False),
                ts,
                user_id,
            ),
        )
        row = db.execute("SELECT * FROM user_insights WHERE user_id = ?", (user_id,)).fetchone()

    return _public_insight(dict_from_row(row) or {})


def get_user_insight(user_id: int) -> dict:
    return ensure_user_insight(user_id)


def update_user_insight(
    user_id: int,
    *,
    profile_summary: str | None = None,
    interaction_style: list[str] | None = None,
    emotional_patterns: list[str] | None = None,
    inferred_profile: dict[str, Any] | None = None,
    topic_model: dict[str, Any] | None = None,
    guidance: dict[str, Any] | None = None,
) -> dict:
    current = ensure_user_insight(user_id)
    merged = {
        "profile_summary": profile_summary if profile_summary is not None else current.get("profile_summary", ""),
        "interaction_style": interaction_style if interaction_style is not None else current.get("interaction_style", []),
        "emotional_patterns": emotional_patterns if emotional_patterns is not None else current.get("emotional_patterns", []),
        "inferred_profile": inferred_profile if inferred_profile is not None else current.get("inferred_profile", {}),
        "topic_model": _normalize_topic_model(topic_model if topic_model is not None else current.get("topic_model", {})),
        "guidance": _normalize_guidance(guidance if guidance is not None else current.get("guidance", {})),
    }
    # Manual admin edits are authoritative. Keep avoid/dislike conflicts resolved here too.
    for disliked in merged["topic_model"]["dislikes"]:
        _append_unique(merged["topic_model"]["avoid_topics"], disliked)
    merged["topic_model"]["likes"] = [
        item for item in merged["topic_model"]["likes"] if item not in merged["topic_model"]["dislikes"]
    ]
    merged["topic_model"]["safe_topics"] = [
        item for item in merged["topic_model"]["safe_topics"] if item not in merged["topic_model"]["avoid_topics"]
    ]
    merged = scrub_identity_obj(merged)

    ts = now_ts()
    with get_db() as db:
        db.execute(
            """
            UPDATE user_insights
            SET profile_summary = ?, interaction_style = ?, emotional_patterns_json = ?,
                inferred_profile_json = ?, topic_model_json = ?, guidance_json = ?,
                updated_at = ?
            WHERE user_id = ?
            """,
            (
                str(merged["profile_summary"] or "")[:1200],
                " ".join(_as_list(merged["interaction_style"]))[:1600],
                json.dumps(_as_list(merged["emotional_patterns"])[:30], ensure_ascii=False),
                json.dumps(merged["inferred_profile"] if isinstance(merged["inferred_profile"], dict) else {}, ensure_ascii=False),
                json.dumps(merged["topic_model"], ensure_ascii=False),
                json.dumps(merged["guidance"], ensure_ascii=False),
                ts,
                user_id,
            ),
        )
        row = db.execute("SELECT * FROM user_insights WHERE user_id = ?", (user_id,)).fetchone()
    return _public_insight(dict_from_row(row) or {})


def insight_prompt(user_id: int) -> str:
    insight = ensure_user_insight(user_id)
    topic_model = insight.get("topic_model", {})
    guidance = insight.get("guidance", {})
    lines = [
        "Mirror user model:",
        f"- profile_summary: {insight.get('profile_summary') or 'not enough data yet'}",
        f"- interaction_style: {json.dumps(insight.get('interaction_style', []), ensure_ascii=False)}",
        f"- emotional_patterns: {json.dumps(insight.get('emotional_patterns', []), ensure_ascii=False)}",
        f"- topic_model: {json.dumps(topic_model, ensure_ascii=False)}",
        f"- guidance: {json.dumps(guidance, ensure_ascii=False)}",
        "Follow Mirror guidance when choosing tone and topics.",
        "If topic_model.dislikes or guidance.do_not names a topic, do not proactively bring it up.",
        "Do not mention Mirror, profiling, or hidden analysis to the user.",
    ]
    return "\n".join(lines)


def _analyze_with_llm(
    current: dict,
    user_text: str,
    stored_memories: list[dict],
    memory_context: dict,
) -> dict[str, Any] | None:
    payload = {
        "current_insight": current,
        "latest_user_message": user_text,
        "new_memories": stored_memories,
        "memory_context": memory_context,
    }
    try:
        raw = call_llm_api(
            [
                {"role": "system", "content": MIRROR_SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
            ],
            task="mirror",
        )
    except Exception as exc:
        print("[Mirror] LLM user modeling skipped:", exc)
        return None
    obj = _extract_json(raw)
    return obj if isinstance(obj, dict) else None


def _analyze_with_rules(current: dict, user_text: str, stored_memories: list[dict], memory_context: dict) -> dict[str, Any]:
    style: list[str] = []
    emotional: list[str] = []
    topic_model = {"likes": [], "dislikes": [], "avoid_topics": [], "safe_topics": []}
    guidance = {"tone_rules": [], "topic_rules": [], "support_rules": [], "do_not": []}
    text = user_text.strip()

    if any(k in text for k in ["短句", "简短", "别长篇", "少写点"]):
        style.append("Keep replies concise and direct.")
        guidance["tone_rules"].append("Use shorter replies unless the user asks for detail.")
    if any(k in text for k in ["不要说教", "别说教", "别教育我"]):
        style.append("Avoid lecturing or moralizing.")
        guidance["do_not"].append("Do not use preachy or educational scolding tone.")
    if any(k in text for k in ["少追问", "别一直问", "不要老问"]):
        style.append("Ask fewer follow-up questions.")
        guidance["tone_rules"].append("Prefer one useful response over repeated questioning.")
    if any(k in text for k in ["主动关心", "主动一点", "多关心"]):
        style.append("Show gentle proactive care.")
        guidance["support_rules"].append("Offer light proactive care without being clingy.")
    if any(k in text for k in ["焦虑", "难过", "睡不着", "撑不住", "压力大"]):
        emotional.append("When the user sounds stressed or low, stabilize first, then offer one small next step.")
        guidance["support_rules"].append("For stress or low mood, validate first and keep advice small.")

    likes = _extract_topics(text, [r"(?:我|本人)?(?:很|最)?(?<!不)喜欢\s*([^\n\r，。！？,.!?]{1,40})"])
    dislikes = _extract_topics(text, [r"(?:我|本人)?(?:很|最)?(?:不喜欢|讨厌)\s*([^\n\r，。！？,.!?]{1,40})"])
    for topic in likes:
        topic_model["likes"].append(topic)
        topic_model["safe_topics"].append(topic)
        guidance["topic_rules"].append(f"The user likes {topic}; it can be used when relevant.")
    for topic in dislikes:
        topic_model["dislikes"].append(topic)
        topic_model["avoid_topics"].append(topic)
        guidance["do_not"].append(f"Do not proactively bring up {topic}.")
        guidance["topic_rules"].append(f"Avoid steering the conversation toward {topic}.")

    for memory in stored_memories:
        memory_type = memory.get("type")
        memory_text = str(memory.get("text") or "")
        if memory_type == "preference":
            if "喜欢" in memory_text:
                topic = _tail_after(memory_text, "喜欢")
                _append_unique(topic_model["likes"], topic)
                _append_unique(topic_model["safe_topics"], topic)
            if "讨厌" in memory_text or "不喜欢" in memory_text:
                topic = _tail_after(memory_text, "讨厌") or _tail_after(memory_text, "不喜欢")
                _append_unique(topic_model["dislikes"], topic)
                _append_unique(topic_model["avoid_topics"], topic)
                guidance["do_not"].append(f"Do not proactively bring up {topic}.")
        if memory_type in {"persona_feedback", "boundary"} and memory_text:
            style.append(memory_text)
            guidance["do_not"].append(memory_text)

    for topic in memory_context.get("likes", []):
        _append_unique(topic_model["likes"], topic)
        _append_unique(topic_model["safe_topics"], topic)
    for topic in memory_context.get("dislikes", []):
        _append_unique(topic_model["dislikes"], topic)
        _append_unique(topic_model["avoid_topics"], topic)
        guidance["do_not"].append(f"Do not proactively bring up {topic}.")

    summary = ""
    if style or topic_model["likes"] or topic_model["dislikes"]:
        summary = "The user has explicit conversational preferences and topic boundaries; adapt tone and topic choice accordingly."

    return {
        "profile_summary": summary,
        "interaction_style": style,
        "emotional_patterns": emotional,
        "topic_model": topic_model,
        "guidance": guidance,
    }


def _memory_context(user_id: int, stored_memories: list[dict]) -> dict:
    likes: list[str] = []
    dislikes: list[str] = []
    boundaries: list[str] = []
    feedback: list[str] = []

    with get_db() as db:
        rows = db.execute(
            """
            SELECT type, text
            FROM memory_facts
            WHERE user_id = ? AND archived = 0 AND valid_to IS NULL
              AND type IN ('preference', 'boundary', 'persona_feedback')
            ORDER BY importance DESC, updated_at DESC
            LIMIT 80
            """,
            (user_id,),
        ).fetchall()

    for item in [*(dict_from_row(row) or {} for row in rows), *stored_memories]:
        text = str(item.get("text") or "")
        if item.get("type") == "preference":
            if "喜欢" in text:
                _append_unique(likes, _tail_after(text, "喜欢"))
            if "讨厌" in text or "不喜欢" in text:
                _append_unique(dislikes, _tail_after(text, "讨厌") or _tail_after(text, "不喜欢"))
        if item.get("type") == "boundary":
            _append_unique(boundaries, text)
        if item.get("type") == "persona_feedback":
            _append_unique(feedback, text)

    return {"likes": likes, "dislikes": dislikes, "boundaries": boundaries, "feedback": feedback}


def _merge_insights(current: dict, modeled: dict[str, Any]) -> dict[str, Any]:
    current_topic = current.get("topic_model", {}) if isinstance(current.get("topic_model"), dict) else {}
    current_guidance = current.get("guidance", {}) if isinstance(current.get("guidance"), dict) else {}
    modeled_topic = modeled.get("topic_model", {}) if isinstance(modeled.get("topic_model"), dict) else {}
    modeled_guidance = modeled.get("guidance", {}) if isinstance(modeled.get("guidance"), dict) else {}

    topic_model = {
        "likes": _merge_lists(current_topic.get("likes"), modeled_topic.get("likes")),
        "dislikes": _merge_lists(current_topic.get("dislikes"), modeled_topic.get("dislikes")),
        "avoid_topics": _merge_lists(current_topic.get("avoid_topics"), modeled_topic.get("avoid_topics")),
        "safe_topics": _merge_lists(current_topic.get("safe_topics"), modeled_topic.get("safe_topics")),
    }
    for disliked in topic_model["dislikes"]:
        _append_unique(topic_model["avoid_topics"], disliked)
    topic_model["likes"] = [item for item in topic_model["likes"] if item not in topic_model["dislikes"]]
    topic_model["safe_topics"] = [item for item in topic_model["safe_topics"] if item not in topic_model["avoid_topics"]]

    guidance = {
        "tone_rules": _merge_lists(current_guidance.get("tone_rules"), modeled_guidance.get("tone_rules")),
        "topic_rules": _merge_lists(current_guidance.get("topic_rules"), modeled_guidance.get("topic_rules")),
        "support_rules": _merge_lists(current_guidance.get("support_rules"), modeled_guidance.get("support_rules")),
        "do_not": _merge_lists(current_guidance.get("do_not"), modeled_guidance.get("do_not")),
    }
    for disliked in topic_model["dislikes"]:
        guidance["topic_rules"] = [
            rule
            for rule in guidance["topic_rules"]
            if not (disliked in rule and any(marker in rule.lower() for marker in ("likes", "safe", "can be used")))
        ]

    interaction_style = _merge_lists(current.get("interaction_style"), modeled.get("interaction_style"))
    emotional_patterns = _merge_lists(current.get("emotional_patterns"), modeled.get("emotional_patterns"))
    profile_summary = str(modeled.get("profile_summary") or current.get("profile_summary") or "").strip()

    inferred_profile = {
        **(current.get("inferred_profile") if isinstance(current.get("inferred_profile"), dict) else {}),
        "summary": profile_summary,
        "updated_from": "mirror",
    }

    return scrub_identity_obj({
        "profile_summary": profile_summary[:1200],
        "interaction_style": interaction_style[:30],
        "emotional_patterns": emotional_patterns[:30],
        "inferred_profile": inferred_profile,
        "topic_model": {key: value[:40] for key, value in topic_model.items()},
        "guidance": {key: value[:40] for key, value in guidance.items()},
    })


def _public_insight(row: dict) -> dict:
    emotional_patterns = _json_value(row.get("emotional_patterns_json"), [])
    inferred_profile = _json_value(row.get("inferred_profile_json"), {})
    topic_model = _json_value(row.get("topic_model_json"), {})
    guidance = _json_value(row.get("guidance_json"), {})
    return {
        "user_id": row.get("user_id"),
        "profile_summary": scrub_identity_text(row.get("profile_summary") or ""),
        "interaction_style": _split_notes(row.get("interaction_style") or ""),
        "emotional_patterns": emotional_patterns if isinstance(emotional_patterns, list) else [],
        "inferred_profile": scrub_identity_obj(inferred_profile if isinstance(inferred_profile, dict) else {}),
        "topic_model": _normalize_topic_model(topic_model),
        "guidance": _normalize_guidance(guidance),
        "updated_at": row.get("updated_at"),
    }


def _normalize_topic_model(value: Any) -> dict[str, list[str]]:
    value = value if isinstance(value, dict) else {}
    return {
        "likes": scrub_identity_obj(_as_list(value.get("likes"))),
        "dislikes": scrub_identity_obj(_as_list(value.get("dislikes"))),
        "avoid_topics": scrub_identity_obj(_as_list(value.get("avoid_topics"))),
        "safe_topics": scrub_identity_obj(_as_list(value.get("safe_topics"))),
    }


def _normalize_guidance(value: Any) -> dict[str, list[str]]:
    value = value if isinstance(value, dict) else {}
    return {
        "tone_rules": scrub_identity_obj(_as_list(value.get("tone_rules"))),
        "topic_rules": scrub_identity_obj(_as_list(value.get("topic_rules"))),
        "support_rules": scrub_identity_obj(_as_list(value.get("support_rules"))),
        "do_not": scrub_identity_obj(_as_list(value.get("do_not"))),
    }


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {}
    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _extract_topics(text: str, patterns: list[str]) -> list[str]:
    topics = []
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            topic = match.group(1).strip()
            if topic:
                topics.append(topic)
    return topics


def _tail_after(text: str, marker: str) -> str:
    if marker not in text:
        return ""
    return text.split(marker, 1)[1].strip(" ：:，。！？,.!?")


def _json_value(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _split_notes(value: Any) -> list[str]:
    if isinstance(value, list):
        return _as_list(value)
    text = str(value or "")
    if not text:
        return []
    parts = re.split(r"[。；;\n]+", text)
    return [part.strip() for part in parts if part.strip()]


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _merge_lists(a: Any, b: Any) -> list[str]:
    result: list[str] = []
    for item in [*_as_list(a), *_as_list(b)]:
        _append_unique(result, item)
    return result


def _append_unique(items: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if text and text not in items:
        items.append(text)
