from __future__ import annotations

import json
import re
from typing import Any

from .database import dict_from_row, get_db, now_ts
from .db_chat import (
    EXPRESSION_POLICY_LOOKBACK,
    _apply_expression_policy,
    _extract_reply_presentation,
    _safe_context,
    get_persona_for_user,
)
from .expression_assets import active_expression_labels
from .identity import scrub_identity_text
from .llm_client import LLMProviderError, call_llm_api


MAX_GROUP_MEMBERS = 6
MAX_GROUP_MESSAGES_PER_TURN = 3
GROUP_HISTORY_LIMIT = 24


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
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO group_messages (
                group_conversation_id, user_id, speaker_type, content,
                reply_status, client_message_id, created_at
            )
            VALUES (?, ?, 'user', ?, 'answered', ?, ?)
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
        reply = {
            "content": item["content"],
            "expressions": _apply_expression_policy(
                item.get("expressions") or [],
                _recent_group_expression_policy(user_id, speaker_id, group_conversation_id),
            ),
        }
        replies.append(_store_persona_group_reply(user_id, group_conversation_id, persona, reply))

    route = {"speakers": [{"persona_id": item["persona_id"], "reason": item.get("reason", "")} for item in planned_messages]}
    degraded = bool(turn.get("degraded"))
    return {
        "group_conversation_id": group_conversation_id,
        "user_message_id": user_message_id,
        "route": route,
        "replies": replies,
        "messages": [*_messages_for_ids(user_id, [user_message_id]), *replies],
        "degraded": degraded,
        "error_message": _group_degraded_message(turn) if degraded else "",
    }


def _generate_group_turn(
    user_id: int,
    group_conversation_id: int,
    members: list[dict],
    history: list[dict],
    user_message: str,
) -> dict:
    member_lines = [_group_member_prompt_context(member) for member in members]
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
                "If the user says things like '你们聊', '你们怎么看', or leaves an opening, the personas may carry the "
                "conversation for 2-3 short messages without waiting for another user prompt.\n"
                "Do not write generic assistant filler. Do not repeat the same content across speakers.\n"
                "Each content must be a chat bubble from that persona only, usually one short sentence.\n"
                "No speaker names inside content. No stage directions. No markdown.\n"
            ),
        },
        {"role": "system", "content": "Group members:\n" + json.dumps(member_lines, ensure_ascii=False)},
        {"role": "system", "content": "Recent group messages:\n" + _format_group_history(history)},
        {"role": "user", "content": user_message},
    ]
    try:
        raw = call_llm_api(messages, task="chat")
    except LLMProviderError:
        return {"messages": [], "degraded": True, "reason": "turn_unavailable"}
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
    return {"messages": planned}


def _group_member_prompt_context(member: dict) -> dict:
    return {
        "persona_id": int(member["persona_id"]),
        "name": member.get("display_name") or member.get("name") or "",
        "summary": member.get("summary") or "",
        "relationship": member.get("relationship") or "",
        "speaking_style": member.get("speaking_style") or "",
        "turn_count": int(member.get("turn_count") or 0),
        "last_spoke_at": int(member.get("last_spoke_at") or 0),
        "persona_prompt": _safe_context(str(member.get("prompt") or ""))[:360],
    }


def _group_degraded_message(turn: dict) -> str:
    if turn.get("reason") == "turn_parse_failed":
        return "群聊刚才说乱了格式，这句话已经留在当前会话里。稍后再试一次。"
    return "群聊暂时没有成功接上，这句话已经留在当前会话里。稍后再试一次。"


def _store_persona_group_reply(user_id: int, group_conversation_id: int, persona: dict, presentation: dict) -> dict:
    ts = now_ts()
    reply = str(presentation.get("content") or "").strip() or "我在。"
    expressions = list(presentation.get("expressions") or [])
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO group_messages (
                group_conversation_id, user_id, speaker_type, speaker_persona_id, content,
                reply_status, created_at
            )
            VALUES (?, ?, 'persona', ?, ?, 'answered', ?)
            """,
            (group_conversation_id, user_id, int(persona["id"]), reply, ts),
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
    result["members"] = [dict_from_row(row) for row in rows]
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
