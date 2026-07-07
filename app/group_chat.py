from __future__ import annotations

import json
import re
from typing import Any

from .database import dict_from_row, get_db, now_ts
from .db_chat import (
    EXPRESSION_POLICY_LOOKBACK,
    _apply_expression_policy,
    _expression_scene_context,
    _expression_selection_agent,
    _extract_reply_presentation,
    _persona_expression_style_context,
    _safe_context,
    _safe_reply_error,
    get_persona_for_user,
)
from .expression_assets import active_expression_labels
from .identity import scrub_identity_text
from .layered_memory import layered_memory_prompt, recall_layered_memory, summary_prompt
from .llm_client import LLMProviderError, call_llm_api


MAX_GROUP_MEMBERS = 6
MAX_GROUP_MESSAGES_PER_TURN = 3
MAX_AUTONOMOUS_GROUP_MESSAGES = 2
MAX_PERSONA_MESSAGES_AFTER_USER = 4
GROUP_HISTORY_LIMIT = 24
GROUP_AUTONOMOUS_MIN_IDLE_SECONDS = 45
GROUP_AUTONOMOUS_USER_WINDOW_SECONDS = 600
GROUP_MEMBER_MEMORY_LIMIT = 520


def create_group_conversation(user_id: int, persona_ids: list[int], title: str = "") -> dict:
    persona_ids = _unique_ints(persona_ids)
    if len(persona_ids) < 2:
        raise ValueError("group chat needs at least two personas")
    if len(persona_ids) > MAX_GROUP_MEMBERS:
        raise ValueError(f"group chat can include at most {MAX_GROUP_MEMBERS} personas")
    personas = [_persona_for_user(user_id, persona_id) for persona_id in persona_ids]
    ts = now_ts()
    group_title = str(title or "").strip()[:80] or _default_group_title(personas)
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO group_conversations (user_id, title, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, group_title, ts, ts),
        )
        group_id = int(cursor.lastrowid)
        db.executemany(
            """
            INSERT INTO group_members (
                group_conversation_id, user_id, persona_id, display_name, joined_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    group_id,
                    user_id,
                    int(persona["id"]),
                    str(persona.get("name") or "").strip(),
                    ts,
                )
                for persona in personas
            ],
        )
    return group_conversation(user_id, group_id)


def list_group_conversations(user_id: int, status: str = "active") -> list[dict]:
    if status not in {"active", "archived"}:
        raise ValueError("invalid group conversation status")
    with get_db() as db:
        rows = db.execute(
            """
            SELECT group_conversations.*,
                   (
                       SELECT group_messages.content
                       FROM group_messages
                       WHERE group_messages.group_conversation_id = group_conversations.id
                       ORDER BY group_messages.id DESC
                       LIMIT 1
                   ) AS last_message,
                   (
                       SELECT group_messages.speaker_type
                       FROM group_messages
                       WHERE group_messages.group_conversation_id = group_conversations.id
                       ORDER BY group_messages.id DESC
                       LIMIT 1
                   ) AS last_message_speaker_type,
                   (
                       SELECT group_messages.speaker_persona_id
                       FROM group_messages
                       WHERE group_messages.group_conversation_id = group_conversations.id
                       ORDER BY group_messages.id DESC
                       LIMIT 1
                   ) AS last_message_speaker_persona_id,
                   (
                       SELECT COUNT(*)
                       FROM group_messages
                       WHERE group_messages.group_conversation_id = group_conversations.id
                   ) AS message_count,
                   (
                       SELECT COUNT(*)
                       FROM group_messages
                       WHERE group_messages.group_conversation_id = group_conversations.id
                         AND group_messages.speaker_type = 'persona'
                         AND group_messages.id > group_conversations.last_read_group_message_id
                   ) AS unread_count
            FROM group_conversations
            WHERE user_id = ? AND status = ?
            ORDER BY pinned_at DESC, updated_at DESC
            """,
            (user_id, status),
        ).fetchall()
    return [_with_group_members(dict_from_row(row) or {}) for row in rows]


def group_conversation(user_id: int, group_conversation_id: int) -> dict:
    with get_db() as db:
        row = db.execute(
            """
            SELECT *
            FROM group_conversations
            WHERE id = ? AND user_id = ?
            """,
            (group_conversation_id, user_id),
        ).fetchone()
    group = dict_from_row(row)
    if not group:
        raise ValueError("group conversation not found")
    return _with_group_members(group)


def group_messages(user_id: int, group_conversation_id: int, *, mark_read: bool = True) -> list[dict]:
    _assert_group_owner(user_id, group_conversation_id)
    with get_db() as db:
        rows = db.execute(
            """
            SELECT group_messages.*, personas.name AS speaker_name, personas.avatar_url AS speaker_avatar_url
            FROM group_messages
            LEFT JOIN personas ON personas.id = group_messages.speaker_persona_id
            WHERE group_messages.group_conversation_id = ? AND group_messages.user_id = ?
            ORDER BY group_messages.id ASC
            """,
            (group_conversation_id, user_id),
        ).fetchall()
        messages = [dict_from_row(row) for row in rows]
        if mark_read and messages:
            db.execute(
                """
                UPDATE group_conversations
                SET last_read_group_message_id = ?, last_read_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (max(int(item["id"]) for item in messages), now_ts(), group_conversation_id, user_id),
            )
    return _attach_group_expressions(user_id, messages)


def update_group_conversation(
    user_id: int,
    group_conversation_id: int,
    *,
    title: str | None = None,
    status: str | None = None,
    pinned: bool | None = None,
) -> dict:
    allowed_status = {"active", "archived"}
    clean_title = title.strip()[:80] if title is not None else None
    clean_status = status.strip() if status is not None else None
    if clean_title is not None and not clean_title:
        raise ValueError("title cannot be empty")
    if clean_status is not None and clean_status not in allowed_status:
        raise ValueError("invalid group conversation status")
    if clean_title is None and clean_status is None and pinned is None:
        raise ValueError("nothing to update")
    fields: list[str] = []
    params: list[Any] = []
    if clean_title is not None:
        fields.append("title = ?")
        params.append(clean_title)
    if clean_status is not None:
        fields.append("status = ?")
        params.append(clean_status)
    if pinned is not None:
        fields.append("pinned_at = ?")
        params.append(now_ts() if pinned else 0)
    fields.append("updated_at = ?")
    params.append(now_ts())
    params.extend([group_conversation_id, user_id])
    with get_db() as db:
        cursor = db.execute(
            f"""
            UPDATE group_conversations
            SET {", ".join(fields)}
            WHERE id = ? AND user_id = ?
            """,
            params,
        )
        if cursor.rowcount == 0:
            raise ValueError("group conversation not found")
    return group_conversation(user_id, group_conversation_id)


def add_group_member(user_id: int, group_conversation_id: int, persona_id: int) -> dict:
    group = group_conversation(user_id, group_conversation_id)
    if group.get("status") != "active":
        raise ValueError("group conversation not active")
    active_count = sum(1 for member in group.get("members") or [] if int(member.get("is_active") or 0))
    if active_count >= MAX_GROUP_MEMBERS:
        raise ValueError(f"group chat can include at most {MAX_GROUP_MEMBERS} personas")
    persona = _persona_for_user(user_id, int(persona_id))
    ts = now_ts()
    with get_db() as db:
        existing = db.execute(
            """
            SELECT id, is_active
            FROM group_members
            WHERE group_conversation_id = ? AND user_id = ? AND persona_id = ?
            """,
            (group_conversation_id, user_id, int(persona_id)),
        ).fetchone()
        if existing and int(existing["is_active"] or 0):
            raise ValueError("persona already in group")
        if existing:
            db.execute(
                """
                UPDATE group_members
                SET is_active = 1, display_name = ?, joined_at = ?
                WHERE id = ?
                """,
                (str(persona.get("name") or "").strip(), ts, int(existing["id"])),
            )
        else:
            db.execute(
                """
                INSERT INTO group_members (
                    group_conversation_id, user_id, persona_id, display_name, joined_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (group_conversation_id, user_id, int(persona_id), str(persona.get("name") or "").strip(), ts),
            )
        db.execute(
            "UPDATE group_conversations SET updated_at = ? WHERE id = ? AND user_id = ?",
            (ts, group_conversation_id, user_id),
        )
    return group_conversation(user_id, group_conversation_id)


def remove_group_member(user_id: int, group_conversation_id: int, persona_id: int) -> dict:
    group = group_conversation(user_id, group_conversation_id)
    if group.get("status") != "active":
        raise ValueError("group conversation not active")
    active_members = [member for member in group.get("members") or [] if int(member.get("is_active") or 0)]
    if int(persona_id) not in {int(member["persona_id"]) for member in active_members}:
        raise ValueError("persona not in group")
    if len(active_members) <= 2:
        raise ValueError("group chat needs at least two active personas")
    ts = now_ts()
    with get_db() as db:
        db.execute(
            """
            UPDATE group_members
            SET is_active = 0
            WHERE group_conversation_id = ? AND user_id = ? AND persona_id = ?
            """,
            (group_conversation_id, user_id, int(persona_id)),
        )
        db.execute(
            "UPDATE group_conversations SET updated_at = ? WHERE id = ? AND user_id = ?",
            (ts, group_conversation_id, user_id),
        )
    return group_conversation(user_id, group_conversation_id)


def mark_group_conversation_read(user_id: int, group_conversation_id: int) -> dict:
    ts = now_ts()
    with get_db() as db:
        row = db.execute(
            """
            SELECT group_conversations.id, COALESCE(MAX(group_messages.id), 0) AS latest_message_id
            FROM group_conversations
            LEFT JOIN group_messages ON group_messages.group_conversation_id = group_conversations.id
            WHERE group_conversations.id = ? AND group_conversations.user_id = ?
            GROUP BY group_conversations.id
            """,
            (group_conversation_id, user_id),
        ).fetchone()
        if not row:
            raise ValueError("group conversation not found")
        latest_message_id = int(row["latest_message_id"] or 0)
        db.execute(
            """
            UPDATE group_conversations
            SET last_read_group_message_id = ?, last_read_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (latest_message_id, ts, group_conversation_id, user_id),
        )
    return {"ok": True, "group_conversation_id": group_conversation_id, "last_read_group_message_id": latest_message_id}


def group_chat(
    *,
    user_id: int,
    group_conversation_id: int,
    message: str,
    client_message_id: str | None = None,
) -> dict:
    content = str(message or "").strip()
    if not content:
        raise ValueError("message is required")
    group = group_conversation(user_id, group_conversation_id)
    if group.get("status") != "active":
        raise ValueError("group conversation not active")
    members = [member for member in group.get("members") or [] if int(member.get("is_active") or 0)]
    if len(members) < 2:
        raise ValueError("group conversation needs at least two active personas")

    ts = now_ts()
    client_message_id = str(client_message_id or "").strip()[:80]
    existing_user_message = _group_user_message_by_client_id(user_id, group_conversation_id, client_message_id)
    if existing_user_message:
        if str(existing_user_message.get("content") or "").strip() != content:
            raise ValueError("client message id already used for another group message")
        user_message_id = int(existing_user_message["id"])
        existing_messages = _group_turn_messages_for_user_message(user_id, group_conversation_id, user_message_id)
        existing_replies = [message for message in existing_messages if message.get("speaker_type") == "persona"]
        if existing_replies or str(existing_user_message.get("reply_status") or "") == "answered":
            return {
                "group_conversation_id": group_conversation_id,
                "user_message_id": user_message_id,
                "route": _route_from_group_replies(existing_replies),
                "replies": existing_replies,
                "messages": existing_messages,
                "degraded": False,
                "degraded_reason": "",
                "error_message": "",
            }
    else:
        with get_db() as db:
            cursor = db.execute(
                """
                INSERT INTO group_messages (
                    group_conversation_id, user_id, speaker_type, content,
                    reply_status, client_message_id, created_at
                )
                VALUES (?, ?, 'user', ?, 'generating', ?, ?)
                """,
                (group_conversation_id, user_id, content, client_message_id, ts),
            )
            user_message_id = int(cursor.lastrowid)
            db.execute(
                "UPDATE group_conversations SET updated_at = ? WHERE id = ?",
                (ts, group_conversation_id),
            )

    history = _recent_group_messages(user_id, group_conversation_id)
    turn = _generate_group_turn(user_id, group_conversation_id, members, history, content)
    planned_messages = turn.get("messages") or []
    replies: list[dict] = []
    for item in planned_messages[:MAX_GROUP_MESSAGES_PER_TURN]:
        speaker_id = int(item["persona_id"])
        persona = _persona_for_user(user_id, speaker_id)
        expression_policy = _recent_group_expression_policy(user_id, speaker_id, group_conversation_id)
        expression_policy.update(_expression_scene_context(content))
        expression_policy.update(_persona_expression_style_context(persona, user_id=user_id, persona_id=speaker_id))
        candidate_expressions = item.get("expressions") or _expression_selection_agent(content, item["content"], expression_policy)
        reply = {
            "content": item["content"],
            "expressions": _apply_expression_policy(candidate_expressions, expression_policy),
        }
        replies.append(_store_persona_group_reply(user_id, group_conversation_id, persona, reply))
    _update_group_member_relations(
        user_id,
        group_conversation_id,
        planned_messages[:MAX_GROUP_MESSAGES_PER_TURN],
    )

    route = {"speakers": [{"persona_id": item["persona_id"], "reason": item.get("reason", "")} for item in planned_messages]}
    degraded = bool(turn.get("degraded"))
    error_message = _group_degraded_message(turn) if degraded else ""
    with get_db() as db:
        db.execute(
            """
            UPDATE group_messages
            SET reply_status = ?, reply_error = ?
            WHERE id = ? AND user_id = ? AND speaker_type = 'user'
            """,
            ("error" if degraded and not replies else "answered", error_message if degraded and not replies else "", user_message_id, user_id),
        )
    return {
        "group_conversation_id": group_conversation_id,
        "user_message_id": user_message_id,
        "route": route,
        "replies": replies,
        "messages": [*_messages_for_ids(user_id, [user_message_id]), *replies],
        "degraded": degraded,
        "degraded_reason": str(turn.get("reason") or "") if degraded else "",
        "error_code": str(turn.get("error_code") or "") if degraded else "",
        "error_message": error_message,
    }


def autonomous_group_turn(
    *,
    user_id: int,
    group_conversation_id: int,
    client_message_id: str | None = None,
    min_idle_seconds: int = GROUP_AUTONOMOUS_MIN_IDLE_SECONDS,
) -> dict:
    group = group_conversation(user_id, group_conversation_id)
    if group.get("status") != "active":
        raise ValueError("group conversation not active")
    members = [member for member in group.get("members") or [] if int(member.get("is_active") or 0)]
    if len(members) < 2:
        raise ValueError("group conversation needs at least two active personas")

    client_message_id = str(client_message_id or "").strip()[:80]
    existing = _group_autonomous_messages_by_client_id(user_id, group_conversation_id, client_message_id) if client_message_id else []
    if existing:
        return {
            "group_conversation_id": group_conversation_id,
            "route": _route_from_group_replies(existing),
            "replies": existing,
            "messages": existing,
            "skipped": False,
            "reason": "reused",
            "degraded": False,
            "degraded_reason": "",
            "error_message": "",
        }

    eligibility = _autonomous_group_eligibility(user_id, group_conversation_id, min_idle_seconds=min_idle_seconds)
    if not eligibility["ok"]:
        return {
            "group_conversation_id": group_conversation_id,
            "route": {"speakers": []},
            "replies": [],
            "messages": [],
            "skipped": True,
            "reason": eligibility["reason"],
            "degraded": False,
            "degraded_reason": "",
            "error_message": "",
        }

    history = _recent_group_messages(user_id, group_conversation_id)
    turn = _generate_group_autonomous_turn(user_id, group_conversation_id, members, history)
    planned_messages = turn.get("messages") or []
    replies: list[dict] = []
    for index, item in enumerate(planned_messages[:MAX_AUTONOMOUS_GROUP_MESSAGES]):
        speaker_id = int(item["persona_id"])
        persona = _persona_for_user(user_id, speaker_id)
        seed_text = _group_expression_seed_text(history)
        expression_policy = _recent_group_expression_policy(user_id, speaker_id, group_conversation_id)
        expression_policy.update(_expression_scene_context(seed_text))
        expression_policy.update(_persona_expression_style_context(persona, user_id=user_id, persona_id=speaker_id))
        candidate_expressions = item.get("expressions") or _expression_selection_agent(seed_text, item["content"], expression_policy)
        reply = {
            "content": item["content"],
            "expressions": _apply_expression_policy(candidate_expressions, expression_policy),
        }
        replies.append(
            _store_persona_group_reply(
                user_id,
                group_conversation_id,
                persona,
                reply,
                client_message_id=client_message_id if index == 0 else "",
            )
        )
    previous_speaker_id = _last_persona_speaker_id(history)
    relation_sequence = ([{"persona_id": previous_speaker_id}] if previous_speaker_id else []) + planned_messages[
        :MAX_AUTONOMOUS_GROUP_MESSAGES
    ]
    _update_group_member_relations(user_id, group_conversation_id, relation_sequence)

    degraded = bool(turn.get("degraded"))
    return {
        "group_conversation_id": group_conversation_id,
        "route": {"speakers": [{"persona_id": item["persona_id"], "reason": item.get("reason", "")} for item in planned_messages]},
        "replies": replies,
        "messages": replies,
        "skipped": not bool(replies),
        "reason": "quiet" if not replies and not degraded else "",
        "degraded": degraded,
        "degraded_reason": str(turn.get("reason") or "") if degraded else "",
        "error_code": str(turn.get("error_code") or "") if degraded else "",
        "error_message": _group_degraded_message(turn) if degraded else "",
    }


def _generate_group_turn(
    user_id: int,
    group_conversation_id: int,
    members: list[dict],
    history: list[dict],
    user_message: str,
) -> dict:
    relation_context = _group_relation_context_by_persona(user_id, group_conversation_id)
    member_lines = [
        _group_member_prompt_context(
            member,
            relation_context.get(int(member["persona_id"]), []),
            _group_member_memory_context(user_id, int(member["persona_id"]), user_message),
        )
        for member in members
    ]
    messages = [
        {
            "role": "system",
            "content": (
                "You are the conductor for a natural multi-persona group chat.\n"
                "In one pass, decide whether anyone should speak and write the actual messages.\n"
                "Return strict JSON only: "
                "{\"messages\":[{\"persona_id\":123,\"content\":\"short natural message\",\"reason\":\"short\"}]}.\n"
                f"Choose 0 to {MAX_GROUP_MESSAGES_PER_TURN} messages. Do not make everyone answer by default.\n"
                "A normal turn may have one speaker, two speakers, or silence.\n"
                "Let personas respond to each other when it feels natural: one may answer the user, another may add a "
                "different angle, tease lightly, disagree gently, or ask another persona a follow-up.\n"
                "If the user addresses the group, asks for everyone's view, says 'you all / everyone / together', "
                "or leaves an opening, prefer 2 distinct speakers when it feels natural and let one persona react to "
                "another instead of both answering the user in parallel.\n"
                "Only return an empty messages array when the user's message clearly closes the topic, asks for quiet, "
                "or no human group member would reasonably speak.\n"
                "If the user says things like '你们聊', '你们怎么看', or leaves an opening, the personas may carry the "
                "conversation for 2-3 short messages without waiting for another user prompt.\n"
                "Do not write generic assistant filler. Do not repeat the same content across speakers.\n"
                "Each member object includes group_stance_hint. Use it to keep speakers distinct and avoid parallel duplicate answers.\n"
                "Use the speaker rhythm context to avoid letting the same persona dominate. If there is a suggested next "
                "speaker, strongly consider them unless the current message clearly belongs to someone else.\n"
                "Each content must be a chat bubble from that persona only, usually one short sentence.\n"
                "No speaker names inside content. No stage directions. No markdown.\n"
            ),
        },
        {"role": "system", "content": "Group members:\n" + json.dumps(member_lines, ensure_ascii=False)},
        {"role": "system", "content": "Recent group messages:\n" + _format_group_history(history)},
        {"role": "system", "content": "Speaker rhythm:\n" + json.dumps(_group_speaker_rhythm_context(members, history), ensure_ascii=False)},
        {"role": "system", "content": "Turn policy:\n" + json.dumps(_group_turn_policy_context(user_message), ensure_ascii=False)},
        {"role": "user", "content": user_message},
    ]
    try:
        raw = call_llm_api(messages, task="group_chat")
    except LLMProviderError as exc:
        safe_error = _safe_reply_error(exc)
        return {"messages": [], "degraded": True, "reason": "turn_unavailable", "error_code": safe_error["code"], "safe_error_message": safe_error["message"]}
    parsed = _parse_route_json(raw)
    valid_ids = {int(member["persona_id"]) for member in members}
    planned: list[dict] = []
    used_speakers: set[int] = set()
    for item in parsed.get("messages") or []:
        if not isinstance(item, dict):
            continue
        try:
            persona_id = int(item.get("persona_id"))
        except Exception:
            continue
        content = scrub_identity_text(str(item.get("content") or "")).strip()
        if persona_id not in valid_ids or persona_id in used_speakers or not content:
            continue
        presentation = _extract_reply_presentation(content)
        planned.append(
            {
                "persona_id": persona_id,
                "content": presentation["content"],
                "expressions": presentation.get("expressions") or [],
                "reason": str(item.get("reason") or "")[:200],
            }
        )
        used_speakers.add(persona_id)
        if len(planned) >= MAX_GROUP_MESSAGES_PER_TURN:
            break
    if "messages" not in parsed:
        return {"messages": [], "degraded": True, "reason": "turn_parse_failed"}
    if not planned and _group_user_message_expects_reply(user_message):
        return {"messages": [], "degraded": True, "reason": "empty_expected_reply"}
    return {"messages": planned}


def _generate_group_autonomous_turn(
    user_id: int,
    group_conversation_id: int,
    members: list[dict],
    history: list[dict],
) -> dict:
    relation_context = _group_relation_context_by_persona(user_id, group_conversation_id)
    memory_query = _format_group_history(history)[-1200:]
    member_lines = [
        _group_member_prompt_context(
            member,
            relation_context.get(int(member["persona_id"]), []),
            _group_member_memory_context(user_id, int(member["persona_id"]), memory_query),
        )
        for member in members
    ]
    messages = [
        {
            "role": "system",
            "content": (
                "You are continuing a natural multi-persona group chat while the user is briefly idle.\n"
                "Decide whether the personas should continue from the recent messages without demanding the user's reply.\n"
                "Return strict JSON only: "
                "{\"messages\":[{\"persona_id\":123,\"content\":\"short natural message\",\"reason\":\"short\"}]}.\n"
                f"Choose 0 to {MAX_AUTONOMOUS_GROUP_MESSAGES} messages. Prefer 0 if the conversation already feels complete.\n"
                "Good autonomous turns can answer another persona, add a small different angle, lightly disagree, or let the topic rest.\n"
                "Each member object includes group_stance_hint. Use it to keep speakers distinct and avoid parallel duplicate answers.\n"
                "Use the speaker rhythm context to prefer someone who has not spoken in the current mini-thread, or let "
                "the group rest if only the same speaker would repeat themselves.\n"
                "Do not greet the user, do not ask 'are you still there', and do not force everyone to speak.\n"
                "Each content must be a chat bubble from that persona only, usually one short sentence.\n"
                "No speaker names inside content. No stage directions. No markdown.\n"
            ),
        },
        {"role": "system", "content": "Group members:\n" + json.dumps(member_lines, ensure_ascii=False)},
        {"role": "system", "content": "Recent group messages:\n" + _format_group_history(history)},
        {"role": "system", "content": "Speaker rhythm:\n" + json.dumps(_group_speaker_rhythm_context(members, history), ensure_ascii=False)},
        {"role": "user", "content": "Continue only if the group would naturally say one more thing now."},
    ]
    try:
        raw = call_llm_api(messages, task="group_chat")
    except LLMProviderError as exc:
        safe_error = _safe_reply_error(exc)
        return {"messages": [], "degraded": True, "reason": "turn_unavailable", "error_code": safe_error["code"], "safe_error_message": safe_error["message"]}
    parsed = _parse_route_json(raw)
    valid_ids = {int(member["persona_id"]) for member in members}
    planned: list[dict] = []
    used_speakers: set[int] = set()
    for item in parsed.get("messages") or []:
        if not isinstance(item, dict):
            continue
        try:
            persona_id = int(item.get("persona_id"))
        except Exception:
            continue
        content = scrub_identity_text(str(item.get("content") or "")).strip()
        if persona_id not in valid_ids or persona_id in used_speakers or not content:
            continue
        presentation = _extract_reply_presentation(content)
        planned.append(
            {
                "persona_id": persona_id,
                "content": presentation["content"],
                "expressions": presentation.get("expressions") or [],
                "reason": str(item.get("reason") or "")[:200],
            }
        )
        used_speakers.add(persona_id)
        if len(planned) >= MAX_AUTONOMOUS_GROUP_MESSAGES:
            break
    if "messages" not in parsed:
        return {"messages": [], "degraded": True, "reason": "turn_parse_failed"}
    return {"messages": planned}


def _group_member_prompt_context(
    member: dict,
    relations: list[str] | None = None,
    memory_context: dict | None = None,
) -> dict:
    return {
        "persona_id": int(member["persona_id"]),
        "name": member.get("display_name") or member.get("name") or "",
        "summary": member.get("summary") or "",
        "relationship": member.get("relationship") or "",
        "speaking_style": member.get("speaking_style") or "",
        "turn_count": int(member.get("turn_count") or 0),
        "last_spoke_at": int(member.get("last_spoke_at") or 0),
        "group_relations": relations or [],
        "memory_context": memory_context or {},
        "group_stance_hint": _group_member_stance_hint(member, relations or []),
        "persona_prompt": _safe_context(str(member.get("prompt") or ""))[:360],
    }


def _group_member_stance_hint(member: dict, relations: list[str] | None = None) -> str:
    name = str(member.get("display_name") or member.get("name") or "TA").strip()
    summary = str(member.get("summary") or "").strip()
    style = str(member.get("speaking_style") or "").strip()
    relationship = str(member.get("relationship") or "").strip()
    relation_hint = _group_relation_stance_hint(relations or [])
    pieces = [
        f"Speak from {name}'s own angle.",
        f"summary={summary}" if summary else "",
        f"relationship={relationship}" if relationship else "",
        f"style={style}" if style else "",
        relation_hint,
        "If another member already covered the main answer, react to that member or add a distinct angle instead of repeating.",
    ]
    return _safe_context(" ".join(piece for piece in pieces if piece))[:360]


def _group_relation_stance_hint(relations: list[str]) -> str:
    relation_text = " | ".join(str(item or "").strip() for item in relations[:2] if str(item or "").strip())
    if not relation_text:
        return "group_relation_stance=no established member dynamic yet; keep the first exchange light."
    return (
        "group_relation_stance="
        f"{relation_text}. Build on familiar members, and if tension is present, disagree in a specific but non-hostile way."
    )


def _group_member_memory_context(user_id: int, persona_id: int, query: str) -> dict:
    try:
        stable_summary = _safe_context(summary_prompt(user_id, persona_id))[:GROUP_MEMBER_MEMORY_LIMIT]
    except Exception:
        stable_summary = "Stable memory summary: unavailable."
    try:
        layered = recall_layered_memory(user_id, persona_id, query, limit=8)
        relevant_memory = _safe_context(layered_memory_prompt(layered))[:GROUP_MEMBER_MEMORY_LIMIT]
    except Exception:
        relevant_memory = "Layered long-term memory: unavailable."
    return {
        "stable_summary": stable_summary,
        "relevant_memory": relevant_memory,
    }


def _group_degraded_message(turn: dict) -> str:
    safe_message = str(turn.get("safe_error_message") or "").strip()
    if safe_message:
        return safe_message.replace("回复", "群聊回复", 1)
    if turn.get("reason") == "turn_parse_failed":
        return "群聊刚才说乱了格式，这句话已经留在当前会话里。稍后再试一次。"
    return "群聊暂时没有成功接上，这句话已经留在当前会话里。稍后再试一次。"


def _group_user_message_expects_reply(user_message: str) -> bool:
    text = re.sub(r"\s+", "", str(user_message or "").strip().lower())
    if not text:
        return False
    quiet_markers = (
        "不用回",
        "别回",
        "不要回",
        "安静",
        "先别说",
        "先不要说",
        "我先走",
        "我先睡",
        "晚安",
        "再见",
        "拜拜",
        "quiet",
        "silence",
    )
    if any(marker in text for marker in quiet_markers):
        return False
    explicit_markers = (
        "?",
        "？",
        "吗",
        "呢",
        "谁",
        "怎么",
        "为什么",
        "咋",
        "如何",
        "什么",
        "你们",
        "大家",
        "一起",
        "聊聊",
        "说说",
        "看看",
        "接",
        "有人",
        "在吗",
        "reply",
        "answer",
        "everyone",
        "together",
    )
    if any(marker in text for marker in explicit_markers):
        return True
    return len(text) >= 2


def _group_turn_policy_context(user_message: str) -> dict[str, Any]:
    text = re.sub(r"\s+", "", str(user_message or "").strip().lower())
    multi_speaker_markers = (
        "你们",
        "大家",
        "所有人",
        "一起",
        "都说",
        "都来",
        "你俩",
        "你們",
        "everyone",
        "youall",
        "together",
        "both",
    )
    quiet_markers = (
        "不用回",
        "别回",
        "不要回",
        "安静",
        "先别说",
        "先不要说",
        "quiet",
        "silence",
    )
    return {
        "expects_reply": _group_user_message_expects_reply(user_message),
        "multi_speaker_invited": any(marker in text for marker in multi_speaker_markers),
        "silence_allowed": any(marker in text for marker in quiet_markers),
        "guidance": (
            "If multi_speaker_invited is true and the topic is still open, prefer 2 distinct speakers. "
            "If silence_allowed is true, an empty messages array is acceptable."
        ),
    }


def _group_speaker_rhythm_context(members: list[dict], history: list[dict]) -> dict[str, Any]:
    member_ids = [int(member["persona_id"]) for member in members if member.get("persona_id")]
    member_set = set(member_ids)
    recent_counts = {persona_id: 0 for persona_id in member_ids}
    current_thread_counts = {persona_id: 0 for persona_id in member_ids}
    last_persona_id: int | None = None
    last_user_index = -1
    for index, message in enumerate(history):
        if message.get("speaker_type") == "user":
            last_user_index = index
        if message.get("speaker_type") != "persona" or not message.get("speaker_persona_id"):
            continue
        persona_id = int(message["speaker_persona_id"])
        if persona_id not in member_set:
            continue
        recent_counts[persona_id] = recent_counts.get(persona_id, 0) + 1
        last_persona_id = persona_id
    for message in history[last_user_index + 1 :]:
        if message.get("speaker_type") != "persona" or not message.get("speaker_persona_id"):
            continue
        persona_id = int(message["speaker_persona_id"])
        if persona_id in member_set:
            current_thread_counts[persona_id] = current_thread_counts.get(persona_id, 0) + 1
    ranked = sorted(
        members,
        key=lambda member: (
            current_thread_counts.get(int(member["persona_id"]), 0),
            recent_counts.get(int(member["persona_id"]), 0),
            int(member.get("turn_count") or 0),
        ),
    )
    suggested = [int(member["persona_id"]) for member in ranked if int(member["persona_id"]) != last_persona_id]
    if not suggested and ranked:
        suggested = [int(ranked[0]["persona_id"])]
    quiet_member_ids = [
        persona_id
        for persona_id in member_ids
        if current_thread_counts.get(persona_id, 0) == 0
    ]
    return {
        "last_persona_id": last_persona_id,
        "recent_persona_counts": recent_counts,
        "current_thread_persona_counts": current_thread_counts,
        "quiet_member_ids_in_current_thread": quiet_member_ids,
        "suggested_next_persona_ids": suggested[:2],
        "guidance": (
            "Prefer a quiet or lower-count member when the user addressed the group. "
            "If the last persona already answered fully, another persona should react to them or stay silent."
        ),
    }


def _group_expression_seed_text(history: list[dict]) -> str:
    for item in reversed(history or []):
        content = str(item.get("content") or "").strip()
        if content:
            return content
    return ""


def _store_persona_group_reply(
    user_id: int,
    group_conversation_id: int,
    persona: dict,
    presentation: dict,
    *,
    client_message_id: str = "",
) -> dict:
    ts = now_ts()
    reply = str(presentation.get("content") or "").strip() or "我在。"
    expressions = list(presentation.get("expressions") or [])
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO group_messages (
                group_conversation_id, user_id, speaker_type, speaker_persona_id, content,
                reply_status, client_message_id, created_at
            )
            VALUES (?, ?, 'persona', ?, ?, 'answered', ?, ?)
            """,
            (group_conversation_id, user_id, int(persona["id"]), reply, str(client_message_id or "")[:80], ts),
        )
        message_id = int(cursor.lastrowid)
        if expressions:
            db.executemany(
                """
                INSERT INTO group_message_expressions (
                    group_message_id, user_id, persona_id, group_conversation_id,
                    expression_type, label, source_text, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        message_id,
                        user_id,
                        int(persona["id"]),
                        group_conversation_id,
                        str(item.get("type") or "gesture")[:40],
                        str(item.get("label") or "")[:80],
                        str(item.get("source_text") or "")[:200],
                        ts,
                    )
                    for item in expressions
                ],
            )
        db.execute(
            """
            UPDATE group_members
            SET last_spoke_at = ?, turn_count = turn_count + 1
            WHERE group_conversation_id = ? AND persona_id = ?
            """,
            (ts, group_conversation_id, int(persona["id"])),
        )
        db.execute(
            "UPDATE group_conversations SET updated_at = ? WHERE id = ?",
            (ts, group_conversation_id),
        )
    return _messages_for_ids(user_id, [message_id])[0]


def _recent_group_messages(user_id: int, group_conversation_id: int) -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT group_messages.*, personas.name AS speaker_name
            FROM group_messages
            LEFT JOIN personas ON personas.id = group_messages.speaker_persona_id
            WHERE group_messages.group_conversation_id = ? AND group_messages.user_id = ?
            ORDER BY group_messages.id DESC
            LIMIT ?
            """,
            (group_conversation_id, user_id, GROUP_HISTORY_LIMIT),
        ).fetchall()
    return list(reversed([dict_from_row(row) for row in rows]))


def _group_user_message_by_client_id(user_id: int, group_conversation_id: int, client_message_id: str) -> dict | None:
    if not client_message_id:
        return None
    with get_db() as db:
        row = db.execute(
            """
            SELECT *
            FROM group_messages
            WHERE user_id = ?
              AND group_conversation_id = ?
              AND speaker_type = 'user'
              AND client_message_id = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (user_id, group_conversation_id, client_message_id),
        ).fetchone()
    return dict_from_row(row) if row else None


def _group_autonomous_messages_by_client_id(user_id: int, group_conversation_id: int, client_message_id: str) -> list[dict]:
    if not client_message_id:
        return []
    with get_db() as db:
        rows = db.execute(
            """
            SELECT group_messages.*, personas.name AS speaker_name, personas.avatar_url AS speaker_avatar_url
            FROM group_messages
            LEFT JOIN personas ON personas.id = group_messages.speaker_persona_id
            WHERE group_messages.user_id = ?
              AND group_messages.group_conversation_id = ?
              AND group_messages.speaker_type = 'persona'
              AND group_messages.client_message_id = ?
            ORDER BY group_messages.id ASC
            """,
            (user_id, group_conversation_id, client_message_id),
        ).fetchall()
    return _attach_group_expressions(user_id, [dict_from_row(row) for row in rows])


def _autonomous_group_eligibility(
    user_id: int,
    group_conversation_id: int,
    *,
    min_idle_seconds: int,
) -> dict:
    ts = now_ts()
    with get_db() as db:
        latest = db.execute(
            """
            SELECT id, speaker_type, reply_status, created_at
            FROM group_messages
            WHERE user_id = ? AND group_conversation_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, group_conversation_id),
        ).fetchone()
        if not latest:
            return {"ok": False, "reason": "empty"}
        latest_status = str(latest["reply_status"] or "")
        if str(latest["speaker_type"] or "") == "user" and latest_status in {"generating", "error"}:
            return {"ok": False, "reason": "last_user_turn_unresolved"}
        if ts - int(latest["created_at"] or 0) < max(0, int(min_idle_seconds)):
            return {"ok": False, "reason": "too_fresh"}
        last_user = db.execute(
            """
            SELECT id, created_at
            FROM group_messages
            WHERE user_id = ?
              AND group_conversation_id = ?
              AND speaker_type = 'user'
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, group_conversation_id),
        ).fetchone()
        if not last_user:
            return {"ok": False, "reason": "no_recent_user"}
        if ts - int(last_user["created_at"] or 0) > GROUP_AUTONOMOUS_USER_WINDOW_SECONDS:
            return {"ok": False, "reason": "user_window_expired"}
        persona_count = db.execute(
            """
            SELECT COUNT(*) AS count
            FROM group_messages
            WHERE user_id = ?
              AND group_conversation_id = ?
              AND speaker_type = 'persona'
              AND id > ?
            """,
            (user_id, group_conversation_id, int(last_user["id"])),
        ).fetchone()
    if int(persona_count["count"] or 0) >= MAX_PERSONA_MESSAGES_AFTER_USER:
        return {"ok": False, "reason": "turn_cap"}
    return {"ok": True, "reason": ""}


def _group_turn_messages_for_user_message(user_id: int, group_conversation_id: int, user_message_id: int) -> list[dict]:
    with get_db() as db:
        next_user = db.execute(
            """
            SELECT MIN(id) AS next_id
            FROM group_messages
            WHERE user_id = ?
              AND group_conversation_id = ?
              AND speaker_type = 'user'
              AND id > ?
            """,
            (user_id, group_conversation_id, user_message_id),
        ).fetchone()
        upper_bound = int(next_user["next_id"] or 0) if next_user else 0
        if upper_bound:
            rows = db.execute(
                """
                SELECT group_messages.*, personas.name AS speaker_name, personas.avatar_url AS speaker_avatar_url
                FROM group_messages
                LEFT JOIN personas ON personas.id = group_messages.speaker_persona_id
                WHERE group_messages.user_id = ?
                  AND group_messages.group_conversation_id = ?
                  AND group_messages.id >= ?
                  AND group_messages.id < ?
                ORDER BY group_messages.id ASC
                """,
                (user_id, group_conversation_id, user_message_id, upper_bound),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT group_messages.*, personas.name AS speaker_name, personas.avatar_url AS speaker_avatar_url
                FROM group_messages
                LEFT JOIN personas ON personas.id = group_messages.speaker_persona_id
                WHERE group_messages.user_id = ?
                  AND group_messages.group_conversation_id = ?
                  AND group_messages.id >= ?
                ORDER BY group_messages.id ASC
                """,
                (user_id, group_conversation_id, user_message_id),
            ).fetchall()
    return _attach_group_expressions(user_id, [dict_from_row(row) for row in rows])


def _route_from_group_replies(replies: list[dict]) -> dict:
    return {
        "speakers": [
            {"persona_id": int(reply["speaker_persona_id"]), "reason": "reused"}
            for reply in replies
            if reply.get("speaker_persona_id")
        ]
    }


def _group_relation_context_by_persona(user_id: int, group_conversation_id: int) -> dict[int, list[str]]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT group_member_relations.persona_id,
                   group_member_relations.affinity,
                   group_member_relations.tension,
                   group_member_relations.note,
                   personas.name AS other_name
            FROM group_member_relations
            JOIN personas ON personas.id = group_member_relations.other_persona_id
            WHERE group_member_relations.user_id = ?
              AND group_member_relations.group_conversation_id = ?
            ORDER BY group_member_relations.updated_at DESC
            LIMIT 24
            """,
            (user_id, group_conversation_id),
        ).fetchall()
    result: dict[int, list[str]] = {}
    for row in rows:
        persona_id = int(row["persona_id"])
        text = (
            f"With {row['other_name']}: familiarity {int(row['affinity'] or 0)}, "
            f"tension {int(row['tension'] or 0)}"
        )
        note = str(row["note"] or "").strip()
        if note:
            text += f"; {note}"
        result.setdefault(persona_id, []).append(text)
    return result


def _group_relation_state_by_persona(user_id: int, group_conversation_id: int) -> dict[int, list[dict]]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT group_member_relations.persona_id,
                   group_member_relations.other_persona_id,
                   group_member_relations.affinity,
                   group_member_relations.tension,
                   group_member_relations.note,
                   group_member_relations.updated_at,
                   personas.name AS other_name
            FROM group_member_relations
            JOIN personas ON personas.id = group_member_relations.other_persona_id
            WHERE group_member_relations.user_id = ?
              AND group_member_relations.group_conversation_id = ?
              AND personas.status = 'active'
            ORDER BY group_member_relations.updated_at DESC, group_member_relations.id DESC
            LIMIT 48
            """,
            (user_id, group_conversation_id),
        ).fetchall()
    result: dict[int, list[dict]] = {}
    for row in rows:
        persona_id = int(row["persona_id"])
        affinity = int(row["affinity"] or 0)
        tension = int(row["tension"] or 0)
        result.setdefault(persona_id, []).append(
            {
                "other_persona_id": int(row["other_persona_id"]),
                "other_name": str(row["other_name"] or ""),
                "affinity": affinity,
                "tension": tension,
                "status": _group_relation_status(affinity, tension),
                "note": str(row["note"] or "").strip(),
                "updated_at": int(row["updated_at"] or 0),
            }
        )
    return result


def _group_relation_status(affinity: int, tension: int) -> str:
    if tension >= 6 and affinity <= 2:
        return "tense"
    if tension >= 4:
        return "careful"
    if affinity >= 8:
        return "close"
    if affinity >= 3:
        return "familiar"
    return "new"


def _last_persona_speaker_id(history: list[dict]) -> int | None:
    for message in reversed(history):
        if message.get("speaker_type") == "persona" and message.get("speaker_persona_id"):
            return int(message["speaker_persona_id"])
    return None


def _update_group_member_relations(user_id: int, group_conversation_id: int, speaker_turns: list[Any]) -> None:
    sequence = _normalise_relation_turns(speaker_turns)
    pairs: list[tuple[int, int, int, str]] = []
    for left, right in zip(sequence, sequence[1:]):
        left_id = int(left["persona_id"])
        right_id = int(right["persona_id"])
        if left_id == right_id:
            continue
        tension_delta = 1 if _turn_suggests_relation_tension(right) else 0
        note = "recently disagreed or added tension in this group" if tension_delta else "recently exchanged turns in this group"
        pairs.append((left_id, right_id, tension_delta, note))
        pairs.append((right_id, left_id, tension_delta, note))
    if not pairs:
        return
    ts = now_ts()
    with get_db() as db:
        db.executemany(
            """
            INSERT INTO group_member_relations (
                group_conversation_id, user_id, persona_id, other_persona_id,
                affinity, tension, note, updated_at
            )
            VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(group_conversation_id, persona_id, other_persona_id)
            DO UPDATE SET
                affinity = MIN(20, group_member_relations.affinity + 1),
                tension = CASE
                    WHEN excluded.tension > 0 THEN MIN(20, group_member_relations.tension + excluded.tension)
                    ELSE MAX(0, group_member_relations.tension - 1)
                END,
                note = excluded.note,
                updated_at = excluded.updated_at
            """,
            [
                (group_conversation_id, user_id, persona_id, other_persona_id, tension_delta, note, ts)
                for persona_id, other_persona_id, tension_delta, note in pairs
            ],
        )


def _normalise_relation_turns(speaker_turns: list[Any]) -> list[dict]:
    sequence: list[dict] = []
    for item in speaker_turns:
        if isinstance(item, dict):
            persona_id = item.get("persona_id") or item.get("speaker_persona_id")
            content = str(item.get("content") or "")
            reason = str(item.get("reason") or "")
        else:
            persona_id = item
            content = ""
            reason = ""
        try:
            persona_id = int(persona_id)
        except Exception:
            continue
        if not persona_id:
            continue
        sequence.append({"persona_id": persona_id, "content": content, "reason": reason})
    return sequence


def _turn_suggests_relation_tension(turn: dict) -> bool:
    text = re.sub(r"\s+", "", f"{turn.get('content') or ''} {turn.get('reason') or ''}".lower())
    if not text:
        return False
    markers = (
        "but",
        "however",
        "disagree",
        "different",
        "\u4e0d\u8fc7",
        "\u53ef\u662f",
        "\u4f46\u662f",
        "\u6211\u4e0d\u592a\u540c\u610f",
        "\u4e0d\u540c\u610f",
        "\u53cd\u800c",
        "\u522b\u8fd9\u6837",
    )
    return any(marker in text for marker in markers)


def _messages_for_ids(user_id: int, message_ids: list[int]) -> list[dict]:
    if not message_ids:
        return []
    placeholders = ",".join("?" for _ in message_ids)
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT group_messages.*, personas.name AS speaker_name, personas.avatar_url AS speaker_avatar_url
            FROM group_messages
            LEFT JOIN personas ON personas.id = group_messages.speaker_persona_id
            WHERE group_messages.user_id = ? AND group_messages.id IN ({placeholders})
            ORDER BY group_messages.id ASC
            """,
            (user_id, *message_ids),
        ).fetchall()
    return _attach_group_expressions(user_id, [dict_from_row(row) for row in rows])


def _with_group_members(group: dict) -> dict:
    if not group:
        return {}
    with get_db() as db:
        rows = db.execute(
            """
            SELECT group_members.*, personas.name, personas.summary, personas.prompt,
                   personas.relationship, personas.speaking_style, personas.avatar_url
            FROM group_members
            JOIN personas ON personas.id = group_members.persona_id
            WHERE group_members.group_conversation_id = ?
              AND group_members.is_active = 1
              AND personas.status = 'active'
            ORDER BY group_members.id ASC
            """,
            (int(group["id"]),),
        ).fetchall()
    result = dict(group)
    relation_state = _group_relation_state_by_persona(int(group["user_id"]), int(group["id"]))
    members = []
    for row in rows:
        member = dict_from_row(row)
        member["group_relations"] = relation_state.get(int(member["persona_id"]), [])
        members.append(member)
    result["members"] = members
    return result


def _recent_group_expression_policy(user_id: int, persona_id: int, group_conversation_id: int) -> dict:
    with get_db() as db:
        preference_row = db.execute(
            """
            SELECT enabled, mode, source_message_id, updated_at
            FROM expression_preferences
            WHERE user_id = ? AND persona_id = ?
            """,
            (user_id, persona_id),
        ).fetchone()
        preference = {"enabled": True, "mode": "normal", "source_message_id": None, "updated_at": 0}
        if preference_row:
            enabled = bool(int(preference_row["enabled"] or 0))
            mode = str(preference_row["mode"] or "").strip() or ("normal" if enabled else "off")
            if mode not in {"off", "subtle", "normal"}:
                mode = "normal" if enabled else "off"
            preference = {
                "enabled": mode != "off",
                "mode": mode,
                "source_message_id": preference_row["source_message_id"],
                "updated_at": int(preference_row["updated_at"] or 0),
            }
        if not preference["enabled"]:
            return {
                "expression_preference": preference,
                "disabled_by_user": True,
                "recent_assistant_messages_checked": 0,
                "suppress_all": True,
                "recent_labels": [],
            }
        message_rows = db.execute(
            """
            SELECT id
            FROM group_messages
            WHERE user_id = ? AND speaker_persona_id = ? AND group_conversation_id = ?
              AND speaker_type = 'persona'
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, persona_id, group_conversation_id, EXPRESSION_POLICY_LOOKBACK),
        ).fetchall()
        message_ids = [int(row["id"]) for row in message_rows]
        expression_rows = []
        if message_ids:
            placeholders = ", ".join("?" for _ in message_ids)
            expression_rows = db.execute(
                f"""
                SELECT group_message_id, label
                FROM group_message_expressions
                WHERE user_id = ? AND persona_id = ? AND group_conversation_id = ?
                  AND group_message_id IN ({placeholders})
                ORDER BY id ASC
                """,
                (user_id, persona_id, group_conversation_id, *message_ids),
            ).fetchall()
    labels_by_message: dict[int, list[str]] = {message_id: [] for message_id in message_ids}
    for row in expression_rows:
        label = str(row["label"] or "").strip()
        if label:
            labels_by_message.setdefault(int(row["group_message_id"]), []).append(label)
    recent_labels = list(
        dict.fromkeys(label for message_id in message_ids for label in labels_by_message.get(message_id, []))
    )
    recent_label_distances: dict[str, int] = {}
    for distance, message_id in enumerate(message_ids):
        for label in labels_by_message.get(message_id, []):
            recent_label_distances.setdefault(label, distance)
    return {
        "expression_preference": preference,
        "disabled_by_user": False,
        "recent_assistant_messages_checked": len(message_ids),
        "subtle_mode": preference["mode"] == "subtle",
        "suppress_all": (
            bool(message_ids and labels_by_message.get(message_ids[0]))
            or (preference["mode"] == "subtle" and bool(recent_labels))
        ),
        "recent_labels": recent_labels,
        "recent_label_distances": recent_label_distances,
    }


def _attach_group_expressions(user_id: int, messages: list[dict]) -> list[dict]:
    message_ids = [int(item["id"]) for item in messages if item and item.get("id")]
    expressions_by_message: dict[int, list[dict]] = {message_id: [] for message_id in message_ids}
    if message_ids:
        placeholders = ",".join("?" for _ in message_ids)
        with get_db() as db:
            rows = db.execute(
                f"""
                SELECT group_message_id, expression_type, label, source_text, created_at
                FROM group_message_expressions
                WHERE user_id = ? AND group_message_id IN ({placeholders})
                ORDER BY id ASC
                """,
                (user_id, *message_ids),
            ).fetchall()
        for row in rows:
            item = dict_from_row(row) or {}
            expression_type = str(item.get("expression_type") or "")
            label = str(item.get("label") or "")
            if label not in active_expression_labels().get(expression_type, set()):
                continue
            message_id = int(item.pop("group_message_id"))
            expressions_by_message.setdefault(message_id, []).append(item)
    for message in messages:
        if message and message.get("speaker_type") == "persona":
            message["expressions"] = expressions_by_message.get(int(message["id"]), [])
        else:
            message["expressions"] = []
    return messages


def _assert_group_owner(user_id: int, group_conversation_id: int) -> None:
    with get_db() as db:
        row = db.execute(
            "SELECT id FROM group_conversations WHERE id = ? AND user_id = ?",
            (group_conversation_id, user_id),
        ).fetchone()
    if not row:
        raise ValueError("group conversation not found")


def _persona_for_user(user_id: int, persona_id: int) -> dict:
    return get_persona_for_user(user_id, persona_id)


def _default_group_title(personas: list[dict]) -> str:
    names = [str(persona.get("name") or "").strip() for persona in personas if persona.get("name")]
    return "、".join(names[:3])[:80] or "新的群聊"


def _unique_ints(values: list[int]) -> list[int]:
    result: list[int] = []
    for value in values or []:
        try:
            item = int(value)
        except Exception:
            continue
        if item > 0 and item not in result:
            result.append(item)
    return result


def _format_group_history(history: list[dict]) -> str:
    if not history:
        return "No messages yet."
    lines: list[str] = []
    for item in history[-GROUP_HISTORY_LIMIT:]:
        speaker_type = item.get("speaker_type")
        if speaker_type == "user":
            speaker = "User"
        elif speaker_type == "persona":
            speaker = item.get("speaker_name") or f"Persona#{item.get('speaker_persona_id')}"
        else:
            speaker = "System"
        content = scrub_identity_text(str(item.get("content") or "")).strip()
        if content:
            lines.append(f"{speaker}: {content}")
    return "\n".join(lines) or "No messages yet."


def _parse_route_json(raw: str) -> dict:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}
