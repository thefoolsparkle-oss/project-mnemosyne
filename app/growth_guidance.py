from __future__ import annotations

import json
import re
from typing import Any

from .database import dict_from_row, get_db, now_ts
from .identity import scrub_identity_text
from .layered_memory import refresh_memory_state, refresh_memory_summaries, store_layered_memories


GUIDANCE_FACET_PATTERNS = {
    "question_frequency": {
        "less": (r"少追问", r"少问(?:我)?", r"(?:不要|别|避免)(?:一直|总|老是|再)?(?:追问|问我|问下去)"),
        "more": (r"(?:可以|希望你|请你|你可以|以后)(?:主动|多|继续)?(?:追问|问我)", r"多问我(?:一点|一些)?"),
    },
    "response_length": {
        "brief": (r"(?:回复|回答|说得?|表达)(?:还是|再|更|尽量)?(?:短|简短|精简)(?:一点)?", r"短回复", r"不要(?:说得?)?太长"),
        "detailed": (r"(?:回复|回答|说得?|表达)(?:还是|再|更|尽量)?(?:详细|具体)(?:一点)?", r"详细回复", r"(?:多说|展开说)(?:一点)?", r"不要(?:说得?)?太短"),
    },
    "analysis_support": {
        "avoid": (r"(?:不要|别|先不要|别急着)(?:马上|立刻|替我|急着)?(?:分析|给建议|想办法|下结论)",),
        "invite": (r"(?:可以|请|希望你|你可以)(?:直接|帮我|替我)?(?:分析|给建议|想办法)", r"帮我(?:分析|想办法)"),
    },
    "lecture_style": {
        "avoid": (r"(?:不要|别)(?:再)?说教", r"(?:不要|别)(?:教育|教训)我"),
    },
    "initiative": {
        "more": (r"(?:主动一点|多关心我)",),
        "less": (r"(?:不要|别)太黏",),
    },
}

CHAT_GUIDANCE_NEGATED_RE = re.compile(
    r"(?:不是|并不是|并非|不想|不希望|没有要|没要|不用|不需要)\s*"
    r"(?:要|让)?\s*(?:你)?\s*.*"
    r"(?:回复|回答|说|表达|追问|问我|分析|给建议|想办法|说教|关心|黏)"
)
CHAT_GUIDANCE_DIRECTIVE_RE = re.compile(
    r"(?:以后|今后|从现在开始|接下来|我改主意了|请你|希望你|我希望你|你可以|麻烦你|"
    r"你(?:以后)?(?:回复|回答|说话)(?:还是|再|更|尽量)?|"
    r"安慰我(?:时|的时候)|我难过(?:时|的时候)|我安静(?:时|的时候)|"
    r"^(?:回复|回答|说|少追问|少问|不要|别|可以|帮我|主动一点|多关心))"
)
GUIDANCE_REFERENCE = r"(?:回复|回答|说|表达|短回复|详细回复|短一点|详细一点|追问|问我|多问|少问|分析|建议|想办法|说教|关心|黏)"
CHAT_GUIDANCE_CANCEL_RE = re.compile(
    rf"(?:不用|不需要|取消|撤销|停止|别再按|不要再按|不再按|不用再).*{GUIDANCE_REFERENCE}|"
    rf"{GUIDANCE_REFERENCE}.*(?:这条|这个|这种)?(?:偏好|要求|方式|规则)?\s*(?:不用了|取消|撤销|停止|作废)"
)
GUIDANCE_CLAUSE_LEAD_RE = re.compile(r"^(?:我改主意了|以后|今后|接下来|从现在开始)$")


def guidance_facets(detail_text: str) -> dict[str, str]:
    text = scrub_identity_text(str(detail_text or "").strip())
    facets: dict[str, str] = {}
    for facet, choices in GUIDANCE_FACET_PATTERNS.items():
        for direction, patterns in choices.items():
            if any(re.search(pattern, text) for pattern in patterns):
                facets[facet] = direction
                break
    return facets


def guidance_conflicts(new_text: str, previous_text: str) -> bool:
    new_facets = guidance_facets(new_text)
    previous_facets = guidance_facets(previous_text)
    return any(
        facet in previous_facets and previous_facets[facet] != direction
        for facet, direction in new_facets.items()
    )


def extract_explicit_chat_guidance(user_text: str) -> str:
    text = scrub_identity_text(str(user_text or "").strip())[:500]
    accepted: list[str] = []
    for part in re.split(r"[。！？!?；;\n]+|(?:，|,)?(?:而是|但是|不过)(?:，|,)?", text):
        segment = part.strip(" ，,")
        if not segment or not guidance_facets(segment):
            continue
        if CHAT_GUIDANCE_NEGATED_RE.search(segment) or CHAT_GUIDANCE_CANCEL_RE.search(segment):
            continue
        if CHAT_GUIDANCE_DIRECTIVE_RE.search(segment):
            accepted.append(segment)
    return "；".join(accepted)[:500]


def deactivate_guidance(
    user_id: int,
    persona_id: int,
    request_row: dict[str, Any],
    *,
    actor: str,
    reason: str,
    ts: int,
) -> None:
    try:
        memory_uids = [
            str(uid) for uid in json.loads(request_row.get("memory_uids_json") or "[]")
            if uid
        ]
    except Exception:
        memory_uids = []
    with get_db() as db:
        db.execute(
            """
            UPDATE persona_growth_requests
            SET withdrawn_at = ?, updated_at = ?, deactivation_actor = ?, deactivation_reason = ?
            WHERE id = ? AND user_id = ? AND persona_id = ? AND withdrawn_at = 0
            """,
            (ts, ts, actor, reason, int(request_row["id"]), user_id, persona_id),
        )
        for uid in memory_uids:
            for table in ("memory_facts", "memory_relations"):
                db.execute(
                    f"""
                    UPDATE {table}
                    SET archived = 1, valid_to = COALESCE(valid_to, ?), updated_at = ?
                    WHERE uid = ? AND user_id = ? AND persona_id = ? AND type = 'persona_feedback'
                    """,
                    (ts, ts, uid, user_id, persona_id),
                )
        if request_row.get("source_message_id"):
            db.execute(
                """
                UPDATE memories
                SET archived = 1, updated_at = ?
                WHERE user_id = ? AND persona_id = ? AND source_message_id = ? AND type = 'persona_feedback'
                """,
                (ts, user_id, persona_id, int(request_row["source_message_id"])),
            )


def supersede_conflicting_guidance(
    user_id: int,
    persona_id: int,
    detail_text: str,
    *,
    exclude_request_id: int | None = None,
) -> list[int]:
    if not guidance_facets(detail_text):
        return []
    with get_db() as db:
        active_rows = db.execute(
            """
            SELECT id, request_text, memory_uids_json, source_message_id
            FROM persona_growth_requests
            WHERE user_id = ? AND persona_id = ? AND withdrawn_at = 0
            ORDER BY updated_at DESC, id DESC
            """,
            (user_id, persona_id),
        ).fetchall()
    ts = now_ts()
    superseded: list[int] = []
    for row in active_rows:
        prior = dict_from_row(row) or {}
        if exclude_request_id and int(prior["id"]) == int(exclude_request_id):
            continue
        if not guidance_conflicts(detail_text, str(prior.get("request_text") or "")):
            continue
        deactivate_guidance(
            user_id,
            persona_id,
            prior,
            actor="adaptive_runtime",
            reason="已被用户较新的相处指导替代",
            ts=ts,
        )
        superseded.append(int(prior["id"]))
    return superseded


def _remaining_guidance_after_cancel(detail_text: str, cancelled_facets: dict[str, str]) -> str:
    text = scrub_identity_text(str(detail_text or "").strip())[:500]
    remaining: list[str] = []
    for part in re.split(r"[，,。！？!?；;\n]+|(?:而且|并且|同时|另外|但是|不过|而是)", text):
        segment = part.strip(" ，,")
        if not segment or GUIDANCE_CLAUSE_LEAD_RE.match(segment):
            continue
        facets = guidance_facets(segment)
        if any(facets.get(facet) == direction for facet, direction in cancelled_facets.items()):
            continue
        remaining.append(segment)
    return "；".join(remaining)[:500]


def _cancelled_guidance_facets(user_text: str) -> dict[str, str]:
    cancelled: dict[str, str] = {}
    for part in re.split(r"[，,。！？!?；;\n]+|(?:而且|并且|同时|另外|但是|不过|而是)", user_text):
        segment = part.strip(" ，,")
        if segment and CHAT_GUIDANCE_CANCEL_RE.search(segment):
            cancelled.update(guidance_facets(segment))
    return cancelled


def _store_remaining_guidance_from_chat(
    user_id: int,
    persona_id: int,
    detail_text: str,
    source_message_id: int | None,
) -> int | None:
    with get_db() as db:
        existing = db.execute(
            """
            SELECT id
            FROM persona_growth_requests
            WHERE user_id = ? AND persona_id = ? AND withdrawn_at = 0 AND request_text = ?
            ORDER BY id DESC LIMIT 1
            """,
            (user_id, persona_id, detail_text),
        ).fetchone()
    if existing:
        return None
    stored = store_layered_memories(
        user_id=user_id,
        persona_id=persona_id,
        conversation_id=None,
        # This is the remainder of an existing instruction, not a new
        # persona-change trigger from the cancellation message.
        source_message_id=None,
        event_uid=None,
        episode_uid=None,
        memories=[
            {
                "type": "persona_feedback",
                "text": f"用户在聊天中取消部分要求后仍保留的相处偏好：{detail_text}",
                "importance": 0.86,
                "confidence": 0.92,
            }
        ],
    )
    memory_uids = [
        str(item["uid"])
        for item in stored
        if item.get("uid") and item.get("layer") in {"L2", "L3"}
    ]
    ts = now_ts()
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO persona_growth_requests (
                user_id, persona_id, request_text, suggestion_id, memory_uids_json,
                request_origin, source_message_id, created_at, updated_at
            )
            VALUES (?, ?, ?, NULL, ?, 'chat_feedback', ?, ?, ?)
            """,
            (user_id, persona_id, detail_text, json.dumps(memory_uids, ensure_ascii=False), source_message_id, ts, ts),
        )
    return int(cursor.lastrowid)


def cancel_guidance_from_chat(
    user_id: int,
    persona_id: int,
    user_text: str,
    source_message_id: int | None = None,
) -> list[int]:
    text = scrub_identity_text(str(user_text or "").strip())[:500]
    cancelled_facets = _cancelled_guidance_facets(text)
    if not cancelled_facets:
        return []
    with get_db() as db:
        active_rows = db.execute(
            """
            SELECT id, request_text, memory_uids_json, source_message_id
            FROM persona_growth_requests
            WHERE user_id = ? AND persona_id = ? AND withdrawn_at = 0
            ORDER BY updated_at DESC, id DESC
            """,
            (user_id, persona_id),
        ).fetchall()
    ts = now_ts()
    stopped: list[int] = []
    remaining_texts: list[str] = []
    for row in active_rows:
        active = dict_from_row(row) or {}
        active_facets = guidance_facets(str(active.get("request_text") or ""))
        if not any(active_facets.get(facet) == direction for facet, direction in cancelled_facets.items()):
            continue
        remaining_text = _remaining_guidance_after_cancel(str(active.get("request_text") or ""), cancelled_facets)
        deactivate_guidance(
            user_id,
            persona_id,
            active,
            actor="chat_runtime",
            reason=(
                "用户在聊天中停止了这条指导的部分内容，未取消部分继续生效"
                if remaining_text
                else "用户在聊天中明确停止了这条指导"
            ),
            ts=ts,
        )
        stopped.append(int(active["id"]))
        if remaining_text and remaining_text not in remaining_texts:
            remaining_texts.append(remaining_text)
    for remaining_text in remaining_texts:
        _store_remaining_guidance_from_chat(user_id, persona_id, remaining_text, source_message_id)
    if stopped:
        refresh_memory_state(user_id, persona_id)
        refresh_memory_summaries(user_id, persona_id)
    return stopped


def maybe_store_chat_guidance(
    user_id: int,
    persona_id: int,
    detail_text: str,
    source_message_id: int,
    stored_memories: list[dict[str, Any]],
) -> dict[str, Any] | None:
    detail = extract_explicit_chat_guidance(detail_text)
    if not detail:
        return None
    with get_db() as db:
        existing = dict_from_row(db.execute(
            """
            SELECT id, request_text
            FROM persona_growth_requests
            WHERE user_id = ? AND persona_id = ? AND withdrawn_at = 0
              AND request_origin = 'chat_feedback' AND request_text = ?
            ORDER BY id DESC LIMIT 1
            """,
            (user_id, persona_id, detail),
        ).fetchone())
    if existing:
        superseded = supersede_conflicting_guidance(
            user_id,
            persona_id,
            detail,
            exclude_request_id=int(existing["id"]),
        )
        if superseded:
            refresh_memory_state(user_id, persona_id)
            refresh_memory_summaries(user_id, persona_id)
        return {"id": int(existing["id"]), "updated": False, "superseded_request_ids": superseded}
    memory_uids = list(dict.fromkeys(
        str(item.get("uid"))
        for item in stored_memories
        if item.get("uid") and item.get("layer") in {"L2", "L3"} and item.get("type") == "persona_feedback"
    ))
    if not memory_uids:
        stored = store_layered_memories(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=None,
            source_message_id=source_message_id,
            event_uid=None,
            episode_uid=None,
            memories=[
                {
                    "type": "persona_feedback",
                    "text": f"用户在聊天中明确提出的相处偏好：{detail}",
                    "importance": 0.86,
                    "confidence": 0.92,
                }
            ],
        )
        memory_uids = [
            str(item["uid"])
            for item in stored
            if item.get("uid") and item.get("layer") in {"L2", "L3"}
        ]
    ts = now_ts()
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO persona_growth_requests (
                user_id, persona_id, request_text, suggestion_id, memory_uids_json,
                request_origin, source_message_id, created_at, updated_at
            )
            VALUES (?, ?, ?, NULL, ?, 'chat_feedback', ?, ?, ?)
            """,
            (user_id, persona_id, detail, json.dumps(memory_uids, ensure_ascii=False), source_message_id, ts, ts),
        )
        request_id = int(cursor.lastrowid)
    superseded = supersede_conflicting_guidance(
        user_id,
        persona_id,
        detail,
        exclude_request_id=request_id,
    )
    refresh_memory_state(user_id, persona_id)
    refresh_memory_summaries(user_id, persona_id)
    return {"id": request_id, "updated": False, "superseded_request_ids": superseded}
