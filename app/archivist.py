from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any

from .database import dict_from_row, get_db, now_ts
from .identity import scrub_identity_text
from .layered_memory import (
    create_episode_from_event,
    record_user_message_event,
    refresh_memory_state,
    refresh_memory_summaries,
    store_layered_memories,
)
from .memory_conflicts import record_conflict
from .growth_guidance import extract_explicit_chat_guidance
from .llm_client import call_llm_api
from .memory_judge import judge_stored_memories
from .memory_policy import should_use_llm_for_extraction, should_use_llm_for_judge


MEMORY_TYPES = {
    "user_profile",
    "identity",
    "preference",
    "plan",
    "relationship",
    "boundary",
    "persona_feedback",
    "emotional_pattern",
}

TOPIC_BOUNDARY_RELEASE_PATTERNS = (
    r"^(?:以后|现在)?(?:可以|能)(?:再|主动)?(?:聊|提|提起|说起)\s*([^\n\r，。！？,.!?]{1,40})$",
    r"^([^\n\r，。！？,.!?]{1,40}?)(?:现在|以后)?(?:可以|能)(?:再|主动)?(?:聊|提|提起|说起)(?:了)?$",
    r"^(?:不用|不必|无需)(?:再)?(?:避开|回避|避免提|避免聊)\s*([^\n\r，。！？,.!?]{1,40})$",
    r"^([^\n\r，。！？,.!?]{1,40}?)(?:不用|不必|无需)(?:再)?(?:避开|回避|避免提|避免聊)(?:了)?$",
)

ADDRESS_BOUNDARY_RELEASE_PATTERNS = (
    r"^(?:你)?(?:现在|以后)?(?:可以|能)(?:再)?(?:叫我|称呼我)\s*([^\s，。！？,.!?]{1,24})(?:了|啦|呀|啊|吧)?$",
    r"^(?:从现在开始|以后|今后|现在)?\s*(?:你)?(?:就)?(?:叫我|称呼我)\s*([^\s，。！？,.!?]{1,24})(?:了|啦|呀|啊|吧)?$",
    r"^我叫\s*([^\s，。！？,.!?]{1,24})(?:了|啦|呀|啊|吧)?$",
    r"^(?:不用|不必|无需)(?:再)?(?:避免|避开)\s*(?:叫我|称呼我)\s*([^\s，。！？,.!?]{1,24})(?:了|啦|呀|啊|吧)?$",
)

ARCHIVIST_SYSTEM = """You are Archivist, the long-term memory curator for a multi-user companion chat system.

Extract only stable, future-useful facts from the user's latest message.
Do not record temporary small talk, private secrets, exact addresses, passwords, bank data, IDs, or precise locations.
Do not invent anything.

Allowed memory types:
- user_profile: stable user profile facts
- identity: preferred name, nickname, address form
- preference: durable likes/dislikes/interests
- plan: durable goals or longer-term plans
- relationship: desired relationship, distance, address rules
- boundary: hard constraints, forbidden topics, forbidden names, unwanted tone
- persona_feedback: how the user wants the persona to behave
- emotional_pattern: repeated or stable emotional/interaction patterns

Return JSON only:
{"memories":[{"type":"preference","text":"...","importance":0.7,"confidence":0.7}]}

If nothing should be stored, return {"memories":[]}.
"""


def extract_memories(user_text: str) -> list[dict[str, Any]]:
    user_text = user_text.strip()
    if not user_text:
        return []

    llm_memories = _extract_with_llm(user_text) if should_use_llm_for_extraction(user_text) else []
    if llm_memories:
        return llm_memories
    return _extract_with_rules(user_text)


def store_memories(
    *,
    user_id: int,
    persona_id: int | None,
    conversation_id: int | None,
    source_message_id: int | None,
    memories: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    stored: list[dict[str, Any]] = []
    ts = now_ts()

    with get_db() as db:
        for memory in memories:
            normalized = normalize_memory(memory)
            if not normalized:
                continue

            existing = _find_existing_memory(
                user_id=user_id,
                persona_id=persona_id,
                memory_type=normalized["type"],
                text=normalized["text"],
            )

            if existing:
                new_importance = max(float(existing["importance"]), normalized["importance"])
                new_confidence = min(1.0, max(float(existing["confidence"]), normalized["confidence"] + 0.08))
                db.execute(
                    """
                    UPDATE memories
                    SET importance = ?, confidence = ?, updated_at = ?, last_used_at = ?, archived = 0
                    WHERE id = ?
                    """,
                    (new_importance, new_confidence, ts, ts, existing["id"]),
                )
                updated = dict(existing)
                updated.update({"importance": new_importance, "confidence": new_confidence, "updated_at": ts})
                stored.append(updated)
                if normalized["type"] == "preference":
                    _archive_opposite_legacy_preferences(
                        db, user_id, persona_id, int(existing["id"]), normalized["text"], ts
                    )
                elif normalized["type"] == "identity":
                    _archive_replaced_legacy_addresses(
                        db, user_id, persona_id, int(existing["id"]), normalized["text"], ts
                    )
                continue

            cursor = db.execute(
                """
                INSERT INTO memories (
                    user_id, persona_id, conversation_id, type, text, importance, confidence,
                    source_message_id, archived, created_at, updated_at, last_used_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    user_id,
                    persona_id,
                    conversation_id,
                    normalized["type"],
                    normalized["text"],
                    normalized["importance"],
                    normalized["confidence"],
                    source_message_id,
                    ts,
                    ts,
                    ts,
                ),
            )
            stored.append({**normalized, "id": int(cursor.lastrowid)})
            if normalized["type"] == "preference":
                _archive_opposite_legacy_preferences(
                    db, user_id, persona_id, int(cursor.lastrowid), normalized["text"], ts
                )
            elif normalized["type"] == "identity":
                _archive_replaced_legacy_addresses(
                    db, user_id, persona_id, int(cursor.lastrowid), normalized["text"], ts
                )

    return stored


def extract_and_store(
    *,
    user_id: int,
    persona_id: int | None,
    conversation_id: int | None,
    source_message_id: int | None,
    user_text: str,
) -> list[dict[str, Any]]:
    memories = extract_memories(user_text)
    event = record_user_message_event(
        user_id=user_id,
        persona_id=persona_id,
        conversation_id=conversation_id,
        message_id=source_message_id,
        content=user_text,
    )
    released_topics = release_explicit_topic_boundaries(
        user_id=user_id,
        persona_id=persona_id,
        user_text=user_text,
        event_uid=str(event["uid"]),
    )
    released_addresses = release_explicit_address_boundaries(
        user_id=user_id,
        persona_id=persona_id,
        user_text=user_text,
        event_uid=str(event["uid"]),
    )
    episode = create_episode_from_event(
        user_id=user_id,
        persona_id=persona_id,
        conversation_id=conversation_id,
        event_uid=event["uid"],
        user_text=user_text,
        memories=memories,
    )

    legacy = store_memories(
        user_id=user_id,
        persona_id=persona_id,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        memories=memories,
    )
    layered = store_layered_memories(
        user_id=user_id,
        persona_id=persona_id,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        event_uid=event["uid"],
        episode_uid=episode["uid"] if episode else None,
        memories=memories,
    )
    stored = [{"layer": "legacy", **item} for item in legacy] + layered
    if should_use_llm_for_judge(len(stored)):
        try:
            judgements = judge_stored_memories(
                user_id=user_id,
                persona_id=persona_id,
                source_message_id=source_message_id,
                user_text=user_text,
                stored_memories=stored,
            )
        except Exception as exc:
            print("[MemoryJudge] failed:", exc)
            judgements = []
    else:
        judgements = []
    if released_topics or released_addresses:
        refresh_memory_state(user_id, persona_id)
        refresh_memory_summaries(user_id, persona_id)
    return stored + [{"layer": "judge", **item} for item in judgements]


def recall_memories(user_id: int, persona_id: int | None, query: str, limit: int = 10) -> list[dict[str, Any]]:
    keywords = _keywords(query)
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id, type, text, importance, confidence, updated_at
            FROM memories
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL) AND archived = 0
            ORDER BY importance DESC, confidence DESC, updated_at DESC
            LIMIT 80
            """,
            (user_id, persona_id),
        ).fetchall()

    scored = []
    for row in rows:
        item = dict_from_row(row) or {}
        score = _memory_score(item, keywords)
        if item.get("type") in {"identity", "relationship", "boundary", "persona_feedback"}:
            score += 3
        if score > 0:
            scored.append((score, item))

    scored.sort(key=lambda pair: (-pair[0], -float(pair[1].get("importance", 0)), -int(pair[1].get("updated_at", 0))))
    return [item for _, item in scored[:limit]]


def normalize_memory(memory: dict[str, Any]) -> dict[str, Any] | None:
    memory_type = str(memory.get("type", "")).strip()
    text = scrub_identity_text(str(memory.get("text", "")).strip())
    if memory_type == "constraint":
        memory_type = "boundary"
    if memory_type not in MEMORY_TYPES or not text:
        return None

    try:
        importance = float(memory.get("importance", 0.5))
    except Exception:
        importance = 0.5
    try:
        confidence = float(memory.get("confidence", 0.62))
    except Exception:
        confidence = 0.62

    return {
        "type": memory_type,
        "text": text[:600],
        "importance": max(0.0, min(1.0, importance)),
        "confidence": max(0.0, min(1.0, confidence)),
    }


def _extract_with_llm(user_text: str) -> list[dict[str, Any]]:
    try:
        raw = call_llm_api(
            [
                {"role": "system", "content": ARCHIVIST_SYSTEM},
                {"role": "user", "content": f"Latest user message:\n<<<\n{user_text}\n>>>"},
            ],
            task="memory",
        )
        obj = _extract_json(raw)
        memories = obj.get("memories", []) if isinstance(obj, dict) else []
        if not isinstance(memories, list):
            return []
        return [item for item in (normalize_memory(m) for m in memories if isinstance(m, dict)) if item]
    except Exception as exc:
        print("[Archivist] LLM extraction skipped:", exc)
        return []


def _extract_with_rules(text: str) -> list[dict[str, Any]]:
    memories: list[dict[str, Any]] = []

    boundary_spans: list[tuple[int, int]] = []
    boundary_patterns = [
        (r"(?:不要|别)\s*(?:叫我|称呼我)\s*([^\s，。！？,.!?]{1,24})", "不要称呼用户为{}", 0.9),
        (r"(?:不要|别)\s*(?:用|太)?\s*([^\n\r，。！？,.!?]{1,40})(?:的语气|这种语气|说话)", "用户不希望使用{}的表达方式", 0.78),
        (r"(?:不要|别)(?:再)?(?:主动)?(?:提|聊|提起|说起)\s*([^\n\r，。！？,.!?]{1,40})", "不要主动提{}", 0.86),
    ]

    for pattern, template, importance in boundary_patterns:
        for match in re.finditer(pattern, text):
            memories.append(
                {
                    "type": "boundary",
                    "text": template.format(match.group(1).strip()),
                    "importance": importance,
                    "confidence": 0.72,
                }
            )
            boundary_spans.append(match.span())

    patterns = [
        (r"(?:我叫|(?<!可以)(?<!能)叫我|你可以叫我)\s*([^\s，。！？,.!?]{1,24}?)(?:了|啦|呀|啊|吧)?(?=$|[，。！？,.!?])", "identity", "用户希望被称为{}", 0.92),
        (r"我(?:现在|其实|已经|又|现在又)?(?:很|最)?喜欢\s*([^\n\r，。！？,.!?]{1,40})", "preference", "用户喜欢{}", 0.72),
        (r"我(?:现在|其实|已经|又|现在又)?(?:很|最)?(?:不喜欢|讨厌)\s*([^\n\r，。！？,.!?]{1,40})", "preference", "用户不喜欢{}", 0.72),
    ]

    for pattern, memory_type, template, importance in patterns:
        for match in re.finditer(pattern, text):
            if memory_type == "identity" and _inside_spans(match.span(), boundary_spans):
                continue
            memories.append(
                {
                    "type": memory_type,
                    "text": template.format(_clean_preference_topic(match.group(1)) if memory_type == "preference" else match.group(1).strip()),
                    "importance": importance,
                    "confidence": 0.72,
                }
            )

    if any(keyword in text.lower() for keyword in ["deadline", "due"]) or any(k in text for k in ["考试", "作业", "项目", "目标"]):
        memories.append(
            {
                "type": "plan",
                "text": f"用户提到一个需要后续关注的安排：{text[:120]}",
                "importance": 0.62,
                "confidence": 0.58,
            }
        )

    explicit_feedback = extract_explicit_chat_guidance(text)
    if explicit_feedback:
        memories.append(
            {
                "type": "persona_feedback",
                "text": f"用户给出了对人格表现或说话方式的反馈：{explicit_feedback[:160]}",
                "importance": 0.68,
                "confidence": 0.68,
            }
        )

    return [item for item in (normalize_memory(memory) for memory in memories) if item]


def _inside_spans(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(start >= s and end <= e for s, e in spans)


def _find_existing_memory(user_id: int, persona_id: int | None, memory_type: str, text: str) -> dict | None:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT *
            FROM memories
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL) AND type = ? AND archived = 0
            ORDER BY updated_at DESC
            LIMIT 60
            """,
            (user_id, persona_id, memory_type),
        ).fetchall()

    for row in rows:
        item = dict_from_row(row) or {}
        old_text = str(item.get("text") or "")
        if memory_type == "preference":
            old_topic = _preference_topic(old_text)
            new_topic = _preference_topic(text)
            if old_topic and old_topic == new_topic and _preference_polarity(old_text) != _preference_polarity(text):
                continue
        if old_text == text or SequenceMatcher(None, old_text, text).ratio() >= 0.9:
            return item
    return None


def _archive_opposite_legacy_preferences(db, user_id: int, persona_id: int | None, current_id: int, text: str, ts: int) -> None:
    current_topic = _preference_topic(text)
    current_polarity = _preference_polarity(text)
    if not current_topic or not current_polarity:
        return
    rows = db.execute(
        """
        SELECT id, text
        FROM memories
        WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL) AND type = 'preference'
          AND archived = 0 AND id != ?
        """,
        (user_id, persona_id, current_id),
    ).fetchall()
    for row in rows:
        previous_text = str(row["text"] or "")
        if _preference_topic(previous_text) != current_topic:
            continue
        if _preference_polarity(previous_text) == current_polarity:
            continue
        db.execute("UPDATE memories SET archived = 1, updated_at = ? WHERE id = ?", (ts, int(row["id"])))


def _archive_replaced_legacy_addresses(db, user_id: int, persona_id: int | None, current_id: int, text: str, ts: int) -> None:
    match = re.match(r"用户希望被称为(.+)", str(text or ""))
    current_address = str(match.group(1) or "").strip() if match else ""
    if not current_address:
        return
    rows = db.execute(
        """
        SELECT id, text
        FROM memories
        WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL) AND type = 'identity'
          AND archived = 0 AND id != ?
        """,
        (user_id, persona_id, current_id),
    ).fetchall()
    for row in rows:
        previous = re.match(r"用户希望被称为(.+)", str(row["text"] or ""))
        previous_address = str(previous.group(1) or "").strip() if previous else ""
        if previous_address and previous_address != current_address:
            db.execute("UPDATE memories SET archived = 1, updated_at = ? WHERE id = ?", (ts, int(row["id"])))


def extract_explicit_topic_boundary_releases(user_text: str) -> list[str]:
    text = scrub_identity_text(str(user_text or "").strip())[:500]
    released: list[str] = []
    for segment in re.split(r"[，,。！？!?；;\n]+", text):
        part = segment.strip()
        for pattern in TOPIC_BOUNDARY_RELEASE_PATTERNS:
            match = re.search(pattern, part)
            if match:
                topic = _clean_preference_topic(match.group(1))
                if topic and topic not in released:
                    released.append(topic)
                break
    return released


def release_explicit_topic_boundaries(
    *,
    user_id: int,
    persona_id: int | None,
    user_text: str,
    event_uid: str,
) -> list[str]:
    released_topics = extract_explicit_topic_boundary_releases(user_text)
    if not released_topics:
        return []
    ts = now_ts()
    for topic in released_topics:
        with get_db() as db:
            legacy_rows = db.execute(
                """
                SELECT id, text
                FROM memories
                WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL)
                  AND type = 'boundary' AND archived = 0
                  AND text = ?
                """,
                (user_id, persona_id, f"不要主动提{topic}"),
            ).fetchall()
            fact_rows = db.execute(
                """
                SELECT uid, text
                FROM memory_facts
                WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL)
                  AND type = 'boundary' AND archived = 0 AND valid_to IS NULL
                  AND text = ?
                """,
                (user_id, persona_id, f"不要主动提{topic}"),
            ).fetchall()
            relation_rows = db.execute(
                """
                SELECT uid, text
                FROM memory_relations
                WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL)
                  AND predicate = 'boundary' AND archived = 0 AND valid_to IS NULL
                  AND text = ?
                """,
                (user_id, persona_id, f"不要主动提{topic}"),
            ).fetchall()
            for row in legacy_rows:
                db.execute("UPDATE memories SET archived = 1, updated_at = ? WHERE id = ?", (ts, int(row["id"])))
            for row in fact_rows:
                db.execute(
                    "UPDATE memory_facts SET archived = 1, valid_to = ?, updated_at = ? WHERE uid = ?",
                    (ts, ts, str(row["uid"])),
                )
            for row in relation_rows:
                db.execute(
                    "UPDATE memory_relations SET archived = 1, valid_to = ?, updated_at = ? WHERE uid = ?",
                    (ts, ts, str(row["uid"])),
                )
        for row in relation_rows:
            record_conflict(
                user_id=user_id,
                persona_id=persona_id,
                conflict_type="boundary_released",
                current_uid=event_uid,
                previous_uid=str(row["uid"]),
                current_text=f"用户明确允许再聊{topic}",
                previous_text=str(row["text"] or ""),
                resolution="prefer_current",
                reason=f"Explicit user release superseded topic boundary for {topic}.",
                status="resolved",
            )
    return released_topics


def extract_explicit_address_boundary_releases(user_text: str) -> list[str]:
    text = scrub_identity_text(str(user_text or "").strip())[:500]
    released: list[str] = []
    for segment in re.split(r"[，,。！？!?；;\n]+", text):
        part = segment.strip()
        for pattern in ADDRESS_BOUNDARY_RELEASE_PATTERNS:
            match = re.search(pattern, part)
            if match:
                address = _clean_preference_topic(match.group(1))
                if address and address not in released:
                    released.append(address)
                break
    return released


def release_explicit_address_boundaries(
    *,
    user_id: int,
    persona_id: int | None,
    user_text: str,
    event_uid: str,
) -> list[str]:
    released_addresses = extract_explicit_address_boundary_releases(user_text)
    if not released_addresses:
        return []
    ts = now_ts()
    for address in released_addresses:
        boundary_text = f"不要称呼用户为{address}"
        with get_db() as db:
            legacy_rows = db.execute(
                """
                SELECT id, text
                FROM memories
                WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL)
                  AND type = 'boundary' AND archived = 0 AND text = ?
                """,
                (user_id, persona_id, boundary_text),
            ).fetchall()
            fact_rows = db.execute(
                """
                SELECT uid, text
                FROM memory_facts
                WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL)
                  AND type = 'boundary' AND archived = 0 AND valid_to IS NULL AND text = ?
                """,
                (user_id, persona_id, boundary_text),
            ).fetchall()
            relation_rows = db.execute(
                """
                SELECT uid, text
                FROM memory_relations
                WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL)
                  AND predicate = 'boundary' AND archived = 0 AND valid_to IS NULL AND text = ?
                """,
                (user_id, persona_id, boundary_text),
            ).fetchall()
            for row in legacy_rows:
                db.execute("UPDATE memories SET archived = 1, updated_at = ? WHERE id = ?", (ts, int(row["id"])))
            for row in fact_rows:
                db.execute(
                    "UPDATE memory_facts SET archived = 1, valid_to = ?, updated_at = ? WHERE uid = ?",
                    (ts, ts, str(row["uid"])),
                )
            for row in relation_rows:
                db.execute(
                    "UPDATE memory_relations SET archived = 1, valid_to = ?, updated_at = ? WHERE uid = ?",
                    (ts, ts, str(row["uid"])),
                )
        for row in relation_rows:
            record_conflict(
                user_id=user_id,
                persona_id=persona_id,
                conflict_type="address_boundary_released",
                current_uid=event_uid,
                previous_uid=str(row["uid"]),
                current_text=f"用户明确允许使用称呼{address}",
                previous_text=str(row["text"] or ""),
                resolution="prefer_current",
                reason=f"Explicit user release superseded forbidden address {address}.",
                status="resolved",
            )
    return released_addresses


def _clean_preference_topic(topic: str) -> str:
    clean = str(topic or "").strip()
    return re.sub(r"(?:了|啦|呀|啊|吧)$", "", clean).strip() or clean


def _preference_topic(text: str) -> str:
    match = re.search(r"用户(?:不喜欢|讨厌|喜欢)(.+)", str(text or ""))
    return _clean_preference_topic(match.group(1)) if match else ""


def _preference_polarity(text: str) -> str:
    if "用户不喜欢" in text or "用户讨厌" in text:
        return "dislike"
    if "用户喜欢" in text:
        return "like"
    return ""


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


def _keywords(query: str) -> set[str]:
    return set(re.findall(r"[\w\u4e00-\u9fff]{2,}", query.lower()))


def _memory_score(memory: dict, keywords: set[str]) -> float:
    if not keywords:
        return 1.0
    text = str(memory.get("text") or "").lower()
    score = sum(1 for keyword in keywords if keyword in text)
    if score:
        return float(score)
    shared = set("".join(keywords)) & set(text)
    useful = {ch for ch in shared if ch.strip() and ch not in "的了呢啊是我你他她它，。！？,.!?"}
    return min(len(useful) / 8, 1.5)
