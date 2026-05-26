from __future__ import annotations

import json
import re
from typing import Any

from .database import dict_from_row, get_db, now_ts
from .identity import scrub_identity_obj, scrub_identity_text
from .llm_client import call_llm_api
from .memory_policy import should_use_llm_for_mirror


DISCOVERY_DIMENSIONS = {
    "interests": "interests and tastes",
    "daily_rhythm": "daily rhythm",
    "values": "values and priorities",
    "comfort_style": "comfort style",
    "boundaries": "boundaries and annoyances",
    "ambitions": "plans and ambitions",
    "relationship_style": "relationship expectations",
}


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
            insight = _public_insight(dict_from_row(row) or {})
            discovery_dimensions = _seed_discovery_dimensions(
                insight.get("discovery_dimensions", {}),
                _dimensions_from_insight(insight),
                int(insight.get("updated_at") or ts),
            )
            if discovery_dimensions != insight.get("discovery_dimensions", {}):
                db.execute(
                    "UPDATE user_insights SET discovery_dimensions_json = ? WHERE user_id = ?",
                    (json.dumps(discovery_dimensions, ensure_ascii=False), user_id),
                )
                insight["discovery_dimensions"] = discovery_dimensions
            return insight

        db.execute(
            """
            INSERT INTO user_insights (
                user_id, profile_summary, interaction_style, emotional_patterns_json,
                inferred_profile_json, topic_model_json, guidance_json,
                discovery_dimensions_json, curiosity_feedback_json, updated_at
            )
            VALUES (?, '', '', '[]', '{}', '{}', '{}', '{}', '{}', ?)
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
    modeled = _overlay_explicit_topic_boundary_signals(modeled, user_text, memory_context)
    merged = _merge_insights(current, modeled)
    ts = now_ts()
    discovery_dimensions = _merge_discovery_dimensions(
        current.get("discovery_dimensions", {}),
        _observed_discovery_dimensions(user_text, stored_memories),
        ts,
    )
    curiosity_feedback = _update_curiosity_feedback(current.get("curiosity_feedback", {}), user_text, ts)

    with get_db() as db:
        db.execute(
            """
            UPDATE user_insights
            SET profile_summary = ?, interaction_style = ?, emotional_patterns_json = ?,
                inferred_profile_json = ?, topic_model_json = ?, guidance_json = ?,
                discovery_dimensions_json = ?, curiosity_feedback_json = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (
                merged["profile_summary"],
                " ".join(merged["interaction_style"])[:1600],
                json.dumps(merged["emotional_patterns"], ensure_ascii=False),
                json.dumps(merged["inferred_profile"], ensure_ascii=False),
                json.dumps(merged["topic_model"], ensure_ascii=False),
                json.dumps(merged["guidance"], ensure_ascii=False),
                json.dumps(discovery_dimensions, ensure_ascii=False),
                json.dumps(curiosity_feedback, ensure_ascii=False),
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
    discovery_dimensions = _seed_discovery_dimensions(
        current.get("discovery_dimensions", {}),
        _dimensions_from_insight(merged),
        ts,
    )

    with get_db() as db:
        db.execute(
            """
            UPDATE user_insights
            SET profile_summary = ?, interaction_style = ?, emotional_patterns_json = ?,
                inferred_profile_json = ?, topic_model_json = ?, guidance_json = ?,
                discovery_dimensions_json = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (
                str(merged["profile_summary"] or "")[:1200],
                " ".join(_as_list(merged["interaction_style"]))[:1600],
                json.dumps(_as_list(merged["emotional_patterns"])[:30], ensure_ascii=False),
                json.dumps(merged["inferred_profile"] if isinstance(merged["inferred_profile"], dict) else {}, ensure_ascii=False),
                json.dumps(merged["topic_model"], ensure_ascii=False),
                json.dumps(merged["guidance"], ensure_ascii=False),
                json.dumps(discovery_dimensions, ensure_ascii=False),
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


def discovery_prompt(
    user_id: int,
    *,
    recent_assistant_messages: list[str] | None = None,
    current_user_text: str = "",
) -> str:
    insight = ensure_user_insight(user_id)
    topic_model = insight.get("topic_model", {})
    discovery_dimensions = insight.get("discovery_dimensions", {})
    curiosity_feedback = insight.get("curiosity_feedback", {})
    known_topics = {
        *topic_model.get("likes", []),
        *topic_model.get("dislikes", []),
        *topic_model.get("avoid_topics", []),
    }
    evidence_count = (
        len(known_topics)
        + len(insight.get("interaction_style", []))
        + len(insight.get("emotional_patterns", []))
    )
    sparse_profile = evidence_count < 4
    lines = [
        "Conversation discovery policy:",
        "- Stored likes and memories are context, not a recurring conversation theme. A known favorite is not a default conversational hook.",
        "- Mention a remembered preference only when the user brings it up, it directly helps the current reply, or the current topic naturally reaches it.",
        "- Never funnel unrelated topics through one remembered detail merely to show familiarity.",
        "- Answer the user's current message first. Curiosity must feel like conversation, not an interview; normally ask no more than one exploratory question in a reply.",
        "- A question may be direct or somewhat personal when natural in this AI conversation, but it must be non-coercive and easy to decline or skip. Do not push after hesitation or refusal.",
        "- Do not infer broad identity, personality, or relationship needs from a single preference or anecdote.",
    ]
    if sparse_profile:
        lines.extend(
            [
                "- The user profile is currently sparse. Prefer gradually learning a different dimension over returning to an already known preference.",
                "- When the moment welcomes a question, explore one new area such as daily rhythm, other interests, values, comfort style, annoyances, ambitions, or boundaries.",
            ]
        )
    else:
        lines.append("- The profile has multiple signals, but keep discovering naturally instead of treating existing interests as a closed script.")
    covered_dimensions = [
        DISCOVERY_DIMENSIONS[key]
        for key in DISCOVERY_DIMENSIONS
        if int((discovery_dimensions.get(key) or {}).get("observed_count") or 0) > 0
    ]
    lightly_known_dimensions = [
        DISCOVERY_DIMENSIONS[key]
        for key in DISCOVERY_DIMENSIONS
        if int((discovery_dimensions.get(key) or {}).get("observed_count") or 0) == 0
    ]
    if covered_dimensions:
        lines.append(f"- Explicit user signals have already touched these areas: {json.dumps(covered_dimensions, ensure_ascii=False)}.")
    if lightly_known_dimensions:
        lines.append(
            f"- Areas not yet clearly learned: {json.dumps(lightly_known_dimensions, ensure_ascii=False)}. "
            "If a curious question fits naturally, prefer one of these instead of re-mining covered ground."
        )
        lines.append("- Discovery coverage is guidance, not a checklist: do not force a question just to fill an area.")
    if curiosity_feedback.get("status") == "cautious":
        lines.append(
            "- The user has explicitly said they do not want exploratory or personal questions. "
            "Do not initiate one now; stay with topics they choose until they clearly invite curiosity again."
        )
    elif curiosity_feedback.get("status") == "invited":
        lines.append(
            "- The user has explicitly welcomed natural curiosity. You may ask at most one optional question "
            "when it fits; this invitation never overrides boundaries or hesitation."
        )
    recent_text = "\n".join(str(item or "") for item in (recent_assistant_messages or [])[-4:])
    current_text = str(current_user_text or "")
    reusable_topics = [
        str(topic).strip()
        for topic in [*topic_model.get("likes", []), *topic_model.get("safe_topics", [])]
        if str(topic).strip()
    ]
    recently_repeated = list(dict.fromkeys(
        topic for topic in reusable_topics
        if topic in recent_text and topic not in current_text
    ))
    if recently_repeated:
        lines.extend(
            [
                f"- Recent assistant replies already used these remembered topics without a new user cue: {json.dumps(recently_repeated, ensure_ascii=False)}.",
                "- Do not bring these topics back in this reply unless answering the current message truly requires them. Move to the user's current concern or a new dimension instead.",
            ]
        )
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

    likes = _extract_topics(text, [r"(?:我|本人)?(?:现在又|现在|其实|已经|又)?(?:很|最)?(?<!不)喜欢\s*([^\n\r，。！？,.!?]{1,40})"])
    dislikes = _extract_topics(text, [r"(?:我|本人)?(?:现在又|现在|其实|已经|又)?(?:很|最)?(?:不喜欢|讨厌)\s*([^\n\r，。！？,.!?]{1,40})"])
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
            if "讨厌" in memory_text or "不喜欢" in memory_text:
                topic = _tail_after(memory_text, "讨厌") or _tail_after(memory_text, "不喜欢")
                _append_unique(topic_model["dislikes"], topic)
                _append_unique(topic_model["avoid_topics"], topic)
                guidance["do_not"].append(f"Do not proactively bring up {topic}.")
            elif "喜欢" in memory_text:
                topic = _tail_after(memory_text, "喜欢")
                _append_unique(topic_model["likes"], topic)
                _append_unique(topic_model["safe_topics"], topic)
        if memory_type == "boundary" and memory_text:
            boundary_topic = _topic_from_boundary_memory(memory_text)
            if boundary_topic:
                _append_topic_boundary_guidance(topic_model, guidance, boundary_topic)
            else:
                style.append(memory_text)
                guidance["do_not"].append(memory_text)
        elif memory_type == "persona_feedback" and memory_text:
            style.append(memory_text)
            guidance["do_not"].append(memory_text)

    for topic in memory_context.get("likes", []):
        _append_unique(topic_model["likes"], topic)
        _append_unique(topic_model["safe_topics"], topic)
    for topic in memory_context.get("dislikes", []):
        _append_unique(topic_model["dislikes"], topic)
        _append_unique(topic_model["avoid_topics"], topic)
        guidance["do_not"].append(f"Do not proactively bring up {topic}.")
    for memory_text in memory_context.get("boundaries", []):
        boundary_topic = _topic_from_boundary_memory(str(memory_text))
        if boundary_topic:
            _append_topic_boundary_guidance(topic_model, guidance, boundary_topic)

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
            if "讨厌" in text or "不喜欢" in text:
                _append_unique(dislikes, _tail_after(text, "讨厌") or _tail_after(text, "不喜欢"))
            elif "喜欢" in text:
                _append_unique(likes, _tail_after(text, "喜欢"))
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
    modeled_likes = set(_as_list(modeled_topic.get("likes"))) - set(_as_list(modeled_topic.get("dislikes")))
    modeled_dislikes = set(_as_list(modeled_topic.get("dislikes")))
    protected_boundary_topics = set(_as_list(modeled.get("active_boundary_topics")))
    released_topics = set(_as_list(modeled.get("released_topics")))
    clearable_likes = modeled_likes - protected_boundary_topics
    topic_model["dislikes"] = [item for item in topic_model["dislikes"] if item not in modeled_likes]
    topic_model["avoid_topics"] = [
        item for item in topic_model["avoid_topics"]
        if item not in clearable_likes and item not in released_topics
    ]
    topic_model["likes"] = [item for item in topic_model["likes"] if item not in modeled_dislikes]
    topic_model["safe_topics"] = [item for item in topic_model["safe_topics"] if item not in modeled_dislikes]
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
    for liked in clearable_likes:
        guidance["topic_rules"] = [
            rule for rule in guidance["topic_rules"]
            if not (liked in rule and any(marker in rule.lower() for marker in ("avoid", "do not", "不要", "避开")))
        ]
        guidance["do_not"] = [
            rule for rule in guidance["do_not"]
            if not (liked in rule and any(marker in rule.lower() for marker in ("avoid", "do not", "不要", "避开")))
        ]
    for released in released_topics:
        if released in topic_model["dislikes"]:
            continue
        guidance["topic_rules"] = [
            rule for rule in guidance["topic_rules"]
            if not (released in rule and any(marker in rule.lower() for marker in ("avoid", "do not", "不要", "避开")))
        ]
        guidance["do_not"] = [
            rule for rule in guidance["do_not"]
            if not (released in rule and any(marker in rule.lower() for marker in ("avoid", "do not", "不要", "避开")))
        ]
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


def _overlay_explicit_topic_boundary_signals(
    modeled: dict[str, Any],
    user_text: str,
    memory_context: dict[str, Any],
) -> dict[str, Any]:
    updated = dict(modeled or {})
    topic_model = _normalize_topic_model(updated.get("topic_model", {}))
    guidance = _normalize_guidance(updated.get("guidance", {}))
    active_topics = [
        topic
        for topic in (_topic_from_boundary_memory(text) for text in memory_context.get("boundaries", []))
        if topic
    ]
    for topic in _explicit_topic_boundaries(user_text):
        _append_unique(active_topics, topic)
    for topic in active_topics:
        _append_topic_boundary_guidance(topic_model, guidance, topic)
    released_topics = _released_topic_boundaries(user_text)
    if released_topics:
        updated["released_topics"] = released_topics
        active_topics = [topic for topic in active_topics if topic not in released_topics]
    updated["active_boundary_topics"] = active_topics
    updated["topic_model"] = topic_model
    updated["guidance"] = guidance
    return updated


def _append_topic_boundary_guidance(topic_model: dict[str, list[str]], guidance: dict[str, list[str]], topic: str) -> None:
    if not topic:
        return
    _append_unique(topic_model["avoid_topics"], topic)
    _append_unique(guidance["do_not"], f"Do not proactively bring up {topic}.")
    _append_unique(guidance["topic_rules"], f"Avoid steering the conversation toward {topic}.")


def _explicit_topic_boundaries(text: str) -> list[str]:
    return _extract_topics(
        text,
        [r"(?:不要|别)(?:再)?(?:主动)?(?:提|聊|提起|说起)\s*([^\n\r，。！？,.!?]{1,40})"],
    )


def _released_topic_boundaries(text: str) -> list[str]:
    patterns = (
        r"^(?:以后|现在)?(?:可以|能)(?:再|主动)?(?:聊|提|提起|说起)\s*([^\n\r，。！？,.!?]{1,40})$",
        r"^([^\n\r，。！？,.!?]{1,40}?)(?:现在|以后)?(?:可以|能)(?:再|主动)?(?:聊|提|提起|说起)(?:了)?$",
        r"^(?:不用|不必|无需)(?:再)?(?:避开|回避|避免提|避免聊)\s*([^\n\r，。！？,.!?]{1,40})$",
        r"^([^\n\r，。！？,.!?]{1,40}?)(?:不用|不必|无需)(?:再)?(?:避开|回避|避免提|避免聊)(?:了)?$",
    )
    released: list[str] = []
    for segment in re.split(r"[，,。！？!?；;\n]+", str(text or "")):
        part = segment.strip()
        if not part:
            continue
        for pattern in patterns:
            match = re.search(pattern, part)
            if match:
                topic = _tail_after(f"喜欢{match.group(1)}", "喜欢")
                _append_unique(released, topic)
                break
    return released


def _topic_from_boundary_memory(text: str) -> str:
    match = re.match(r"不要主动提(.+)", str(text or "").strip())
    return _tail_after(f"喜欢{match.group(1)}", "喜欢") if match else ""


def _public_insight(row: dict) -> dict:
    emotional_patterns = _json_value(row.get("emotional_patterns_json"), [])
    inferred_profile = _json_value(row.get("inferred_profile_json"), {})
    topic_model = _json_value(row.get("topic_model_json"), {})
    guidance = _json_value(row.get("guidance_json"), {})
    discovery_dimensions = _json_value(row.get("discovery_dimensions_json"), {})
    curiosity_feedback = _json_value(row.get("curiosity_feedback_json"), {})
    return {
        "user_id": row.get("user_id"),
        "profile_summary": scrub_identity_text(row.get("profile_summary") or ""),
        "interaction_style": _split_notes(row.get("interaction_style") or ""),
        "emotional_patterns": emotional_patterns if isinstance(emotional_patterns, list) else [],
        "inferred_profile": scrub_identity_obj(inferred_profile if isinstance(inferred_profile, dict) else {}),
        "topic_model": _normalize_topic_model(topic_model),
        "guidance": _normalize_guidance(guidance),
        "discovery_dimensions": _normalize_discovery_dimensions(discovery_dimensions),
        "curiosity_feedback": _normalize_curiosity_feedback(curiosity_feedback),
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


def _normalize_discovery_dimensions(value: Any) -> dict[str, dict[str, int]]:
    value = value if isinstance(value, dict) else {}
    normalized: dict[str, dict[str, int]] = {}
    for key in DISCOVERY_DIMENSIONS:
        item = value.get(key, {})
        if not isinstance(item, dict):
            continue
        count = max(0, int(item.get("observed_count") or 0))
        if count:
            normalized[key] = {
                "observed_count": count,
                "last_observed_at": max(0, int(item.get("last_observed_at") or 0)),
            }
    return normalized


def _normalize_curiosity_feedback(value: Any) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    status = str(value.get("status") or "neutral")
    if status not in {"neutral", "cautious", "invited"}:
        status = "neutral"
    return {
        "status": status,
        "declined_count": max(0, int(value.get("declined_count") or 0)),
        "invited_count": max(0, int(value.get("invited_count") or 0)),
        "last_signal": str(value.get("last_signal") or ""),
        "last_signal_at": max(0, int(value.get("last_signal_at") or 0)),
    }


def _update_curiosity_feedback(current: Any, user_text: str, ts: int) -> dict[str, Any]:
    feedback = _normalize_curiosity_feedback(current)
    signal = _curiosity_signal(user_text)
    if not signal:
        return feedback
    if signal == "declined":
        feedback["status"] = "cautious"
        feedback["declined_count"] += 1
    else:
        feedback["status"] = "invited"
        feedback["invited_count"] += 1
    feedback["last_signal"] = signal
    feedback["last_signal_at"] = ts
    return feedback


def _curiosity_signal(user_text: str) -> str:
    text = str(user_text or "").strip()
    declined_markers = (
        "别问我这些",
        "别再问我这些",
        "不要问这种",
        "不要问这类",
        "别问这么私人",
        "不要问这么私人",
        "我不喜欢被问这些",
        "我不想回答这类",
        "不要总问我",
        "别总问我",
    )
    invited_markers = (
        "你可以问我",
        "可以问我",
        "想了解我就问",
        "想知道什么可以问",
        "可以多问我",
        "多了解我一点",
    )
    if _has_any(text, declined_markers):
        return "declined"
    if _has_any(text, invited_markers):
        return "invited"
    return ""


def _observed_discovery_dimensions(user_text: str, stored_memories: list[dict]) -> set[str]:
    observed: set[str] = set()
    text = str(user_text or "")
    for memory in stored_memories:
        memory_type = str(memory.get("type") or "")
        memory_text = str(memory.get("text") or "")
        if memory_type == "preference":
            observed.add("interests")
        elif memory_type == "plan":
            observed.add("ambitions")
        elif memory_type == "boundary":
            observed.add("boundaries")
        elif memory_type == "relationship":
            observed.add("relationship_style")
        elif memory_type == "emotional_pattern":
            observed.add("comfort_style")
        elif memory_type == "persona_feedback":
            observed.add("relationship_style")
            if _has_any(memory_text, ("安慰", "陪", "难过", "焦虑", "低落", "情绪", "压力")):
                observed.add("comfort_style")
    if _has_any(text, ("作息", "早睡", "晚睡", "熬夜", "上班", "下班", "上课", "周末", "每天", "平时")):
        observed.add("daily_rhythm")
    if _has_any(text, ("在意", "看重", "重要的是", "原则", "意义", "价值", "希望成为")):
        observed.add("values")
    if _has_any(text, ("安慰", "陪我", "难过时", "焦虑时", "低落时", "压力大的时候")):
        observed.add("comfort_style")
    return observed


def _dimensions_from_insight(insight: dict[str, Any]) -> set[str]:
    observed: set[str] = set()
    topic_model = insight.get("topic_model", {}) if isinstance(insight.get("topic_model"), dict) else {}
    if any(topic_model.get(key) for key in ("likes", "dislikes", "avoid_topics", "safe_topics")):
        observed.add("interests")
    if insight.get("interaction_style"):
        observed.add("relationship_style")
    if insight.get("emotional_patterns"):
        observed.add("comfort_style")
    return observed


def _merge_discovery_dimensions(current: Any, observed: set[str], ts: int) -> dict[str, dict[str, int]]:
    merged = _normalize_discovery_dimensions(current)
    for key in observed:
        if key not in DISCOVERY_DIMENSIONS:
            continue
        previous = merged.get(key, {})
        merged[key] = {
            "observed_count": int(previous.get("observed_count") or 0) + 1,
            "last_observed_at": ts,
        }
    return merged


def _seed_discovery_dimensions(current: Any, observed: set[str], ts: int) -> dict[str, dict[str, int]]:
    merged = _normalize_discovery_dimensions(current)
    for key in observed:
        if key in DISCOVERY_DIMENSIONS and key not in merged:
            merged[key] = {"observed_count": 1, "last_observed_at": ts}
    return merged


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


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
            topic = re.sub(r"(?:了|啦|呀|啊|吧)$", "", match.group(1).strip()).strip()
            if topic:
                topics.append(topic)
    return topics


def _tail_after(text: str, marker: str) -> str:
    if marker not in text:
        return ""
    topic = text.split(marker, 1)[1].strip(" ：:，。！？,.!?")
    return re.sub(r"(?:了|啦|呀|啊|吧)$", "", topic).strip() or topic


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
