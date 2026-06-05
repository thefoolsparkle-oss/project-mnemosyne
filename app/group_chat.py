from __future__ import annotations

import json
import re
from typing import Any

from .database import dict_from_row, get_db, now_ts
from .db_chat import (
    EXPRESSION_POLICY_LOOKBACK,
    _apply_expression_policy,
    _extract_reply_presentation,
    _expression_policy_prompt,
    _final_persona_lock,
    _persona_runtime_prompt,
    _safe_context,
    chat_rendering_rules_prompt,
    get_persona_for_user,
)
from .expression_assets import active_expression_labels
from .identity import scrub_identity_text
from .llm_client import LLMProviderError, call_llm_api


MAX_GROUP_MEMBERS = 6
MAX_GROUP_SPEAKERS_PER_TURN = 2
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
    route = _route_group_turn(user_id, group, members, history, content)
    speaker_ids = [int(item["persona_id"]) for item in route.get("speakers") or []]
    replies: list[dict] = []
    for speaker_id in speaker_ids[:MAX_GROUP_SPEAKERS_PER_TURN]:
        persona = _persona_for_user(user_id, speaker_id)
        reply = _generate_group_reply(
            user_id=user_id,
            group=group,
            group_conversation_id=group_conversation_id,
            members=members,
            history=_recent_group_messages(user_id, group_conversation_id),
            persona=persona,
            trigger_message=content,
        )
        replies.append(_store_persona_group_reply(user_id, group_conversation_id, persona, reply))

    return {
        "group_conversation_id": group_conversation_id,
        "user_message_id": user_message_id,
        "route": route,
        "replies": replies,
        "messages": [*_messages_for_ids(user_id, [user_message_id]), *replies],
    }


def _route_group_turn(
    user_id: int,
    group: dict,
    members: list[dict],
    history: list[dict],
    user_message: str,
) -> dict:
    member_lines = [
        {
            "persona_id": int(member["persona_id"]),
            "name": member.get("display_name") or member.get("name") or "",
            "summary": member.get("summary") or "",
            "speaking_style": member.get("speaking_style") or "",
            "turn_count": int(member.get("turn_count") or 0),
            "last_spoke_at": int(member.get("last_spoke_at") or 0),
        }
        for member in members
    ]
    messages = [
        {
            "role": "system",
            "content": (
                "You are Group Router for a multi-persona chat. Decide who should speak next.\n"
                "Return strict JSON only: {\"speakers\":[{\"persona_id\":123,\"reason\":\"short\"}]}.\n"
                f"Choose 0 to {MAX_GROUP_SPEAKERS_PER_TURN} speakers. Prefer restraint; do not make everyone answer.\n"
                "Pick a persona only if they can add something natural, answer directly, or respond to another persona.\n"
            ),
        },
        {"role": "system", "content": "Group members:\n" + json.dumps(member_lines, ensure_ascii=False)},
        {"role": "system", "content": "Recent group messages:\n" + _format_group_history(history)},
        {"role": "user", "content": user_message},
    ]
    route_failed = False
    try:
        raw = call_llm_api(messages, task="chat")
        parsed = _parse_route_json(raw)
    except Exception as exc:
        print("[GroupRouter] fallback:", exc)
        route_failed = True
        parsed = {}
    valid_ids = {int(member["persona_id"]) for member in members}
    speakers: list[dict] = []
    for item in parsed.get("speakers") or []:
        if not isinstance(item, dict):
            continue
        try:
            persona_id = int(item.get("persona_id"))
        except Exception:
            continue
        if persona_id in valid_ids and persona_id not in {speaker["persona_id"] for speaker in speakers}:
            speakers.append(
                {
                    "persona_id": persona_id,
                    "reason": str(item.get("reason") or "")[:200],
                }
            )
        if len(speakers) >= MAX_GROUP_SPEAKERS_PER_TURN:
            break
    if not speakers and (route_failed or "speakers" not in parsed):
        speakers = [{"persona_id": _fallback_speaker_id(members), "reason": "fallback_round_robin"}]
    return {"speakers": speakers}


def _generate_group_reply(
    *,
    user_id: int,
    group: dict,
    group_conversation_id: int,
    members: list[dict],
    history: list[dict],
    persona: dict,
    trigger_message: str,
) -> dict:
    member_context = [
        {
            "persona_id": int(member["persona_id"]),
            "name": member.get("display_name") or member.get("name") or "",
            "summary": member.get("summary") or "",
            "relationship": member.get("relationship") or "",
        }
        for member in members
    ]
    expression_policy = _recent_group_expression_policy(user_id, int(persona["id"]), group_conversation_id)
    messages = [
        {"role": "system", "content": _safe_context(str(persona["prompt"]))},
        {"role": "system", "content": _persona_runtime_prompt(persona)},
        {"role": "system", "content": chat_rendering_rules_prompt()},
        {
            "role": "system",
            "content": (
                "Group chat mode:\n"
                f"- You are speaking as {persona.get('name') or ''} inside a shared group chat.\n"
                "- Reply with one short natural message, usually 1-3 sentences.\n"
                "- You may respond to the user or to another persona's latest point.\n"
                "- Do not speak for other personas. Do not narrate stage directions.\n"
                "- If another persona already covered the point, add a different angle or keep it brief.\n"
            ),
        },
        {"role": "system", "content": "Group members:\n" + json.dumps(member_context, ensure_ascii=False)},
        {"role": "system", "content": "Recent group messages:\n" + _format_group_history(history)},
        {"role": "system", "content": _expression_policy_prompt(expression_policy)},
        {"role": "system", "content": _final_persona_lock(persona)},
        {"role": "user", "content": f"Latest user message: {trigger_message}"},
    ]
    try:
        reply = call_llm_api(messages, task="chat")
    except LLMProviderError:
        return {"content": _fallback_group_reply(persona, trigger_message), "expressions": []}
    presentation = _extract_reply_presentation(reply)
    presentation["expressions"] = _apply_expression_policy(presentation["expressions"], expression_policy)
    return presentation


def _fallback_group_reply(persona: dict, trigger_message: str) -> str:
    name = str(persona.get("name") or "").strip()
    text = str(trigger_message or "").strip()
    if len(text) <= 12:
        return "我在，刚才有点卡住，但我看到你说的了。"
    if name:
        return f"我在。刚才有点卡住，{name}先接住这句：我看到你说的了。"
    return "我在。刚才有点卡住，但我看到你说的了。"


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
            SELECT group_members.*, personas.name, personas.summary, personas.relationship,
                   personas.speaking_style, personas.avatar_url
            FROM group_members
            JOIN personas ON personas.id = group_members.persona_id
            WHERE group_members.group_conversation_id = ?
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


def _fallback_speaker_id(members: list[dict]) -> int:
    sorted_members = sorted(
        members,
        key=lambda item: (int(item.get("last_spoke_at") or 0), int(item.get("turn_count") or 0), int(item["persona_id"])),
    )
    return int(sorted_members[0]["persona_id"])


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
