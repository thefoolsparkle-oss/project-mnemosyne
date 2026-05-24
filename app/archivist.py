from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any

from .database import dict_from_row, get_db, now_ts
from .identity import scrub_identity_text
from .layered_memory import create_episode_from_event, record_user_message_event, store_layered_memories
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
        (r"(?:我叫|叫我|你可以叫我)\s*([^\s，。！？,.!?]{1,24})", "identity", "用户希望被称为{}", 0.92),
        (r"我(?:最)?喜欢\s*([^\n\r，。！？,.!?]{1,40})", "preference", "用户喜欢{}", 0.72),
        (r"我讨厌\s*([^\n\r，。！？,.!?]{1,40})", "preference", "用户讨厌{}", 0.72),
    ]

    for pattern, memory_type, template, importance in patterns:
        for match in re.finditer(pattern, text):
            if memory_type == "identity" and _inside_spans(match.span(), boundary_spans):
                continue
            memories.append(
                {
                    "type": memory_type,
                    "text": template.format(match.group(1).strip()),
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

    explicit_feedback = [
        "回复短一点",
        "回复还是短一点",
        "回答短一点",
        "说短一点",
        "简短一点",
        "少说一点",
        "不要说教",
        "别说教",
        "少追问",
        "别一直问",
        "不要一直问",
        "主动一点",
        "多关心",
        "别太黏",
        "不要太黏",
    ]
    has_explicit_feedback = any(k in text for k in explicit_feedback)
    has_general_feedback = any(k in text for k in ["你以后", "你说话", "你回复", "别太", "不要太"])
    if has_explicit_feedback or has_general_feedback:
        memories.append(
            {
                "type": "persona_feedback",
                "text": f"用户给出了对人格表现或说话方式的反馈：{text[:160]}",
                "importance": 0.68 if has_explicit_feedback else 0.58,
                "confidence": 0.55,
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
        if old_text == text or SequenceMatcher(None, old_text, text).ratio() >= 0.9:
            return item
    return None


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
