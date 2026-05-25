from __future__ import annotations

import json
import re
from datetime import datetime

from .archivist import extract_and_store, recall_memories
from .conversation_memory import conversation_summary_prompt, refresh_conversation_summary
from .database import dict_from_row, get_db, now_ts
from .identity import IDENTITY_REPLACEMENTS, scrub_identity_obj, scrub_identity_text
from .layered_memory import layered_memory_prompt, recall_layered_memory, state_prompt, summary_prompt
from .llm_client import LLMProviderError, call_llm_api
from .memory_rag import semantic_memory_prompt, semantic_memory_recall
from .memory_policy import policy_snapshot, should_refresh_summary, should_use_semantic_recall
from .mirror import discovery_prompt, insight_prompt, update_interaction_insight
from .sculptor import maybe_queue_revision_from_feedback


CHAT_RENDERING_RULES = """聊天输出规则：
- 你只负责以当前人格的第一人称自然聊天，不要解释系统指令、底层实现或技术来源。
- 不要给自己贴技术身份标签，也不要说自己是某种工具或模拟结果。
- 如果用户问“你是谁”“真的假的”“你是不是某种技术产物”，只按当前人格名字、关系定位和对话关系回答，不讨论技术身份。
- 除非当前人格资料的关系定位明确写着恋人，否则不能用恋人、女友、男友、老婆、老公等关系自称。
- 不要写括号舞台动作、神态旁白或表演说明，例如“（托腮）”“(歪头看你)”“【笑】”。
- 情绪和灵动感要融入自然语言、停顿、语气和用词里。
- 未来表情、动作、贴纸会由程序层表达；当前只输出真正要说的话。
"""

STAGE_DIRECTION_RE = re.compile(r"[\uFF08\(\u3010\[]\s*([^\uFF08\uFF09\(\)\[\]\u3010\u3011]{1,120}?)\s*[\uFF09\)\u3011\]]\s*")
STAGE_DIRECTION_WORDS = (
    "托腮",
    "歪头",
    "想了想",
    "看你",
    "眼睛",
    "亮晶晶",
    "眨眼",
    "笑",
    "微笑",
    "轻笑",
    "偷笑",
    "叹气",
    "沉默",
    "思考",
    "点头",
    "摇头",
    "摊手",
    "抬眼",
    "垂眼",
    "低头",
    "抿嘴",
    "皱眉",
    "摸头",
    "抱",
    "靠近",
    "凑近",
    "懒腰",
    "伸个懒腰",
    "揉揉眼睛",
    "陶醉",
    "表情",
    "做出",
    "小声",
    "轻声",
    "停顿",
)

IDENTITY_LEAK_REPLACEMENTS = IDENTITY_REPLACEMENTS


def get_persona_for_user(user_id: int, persona_id: int) -> dict:
    with get_db() as db:
        persona = dict_from_row(
            db.execute(
                """
                SELECT * FROM personas
                WHERE id = ? AND user_id = ? AND status = 'active'
                """,
                (persona_id, user_id),
            ).fetchone()
        )

    if not persona:
        raise ValueError("persona not found")
    return persona


def _stale_persona_names(db, persona_id: int, current_name: str) -> list[str]:
    rows = db.execute(
        """
        SELECT DISTINCT name
        FROM persona_versions
        WHERE persona_id = ?
          AND name <> ''
          AND name <> ?
        """,
        (persona_id, current_name),
    ).fetchall()
    names = {str(row["name"]).strip() for row in rows if str(row["name"] or "").strip()}
    return sorted(names, key=len, reverse=True)


def _replace_stale_persona_names(text: str, current_name: str, stale_names: list[str]) -> str:
    if not text or not current_name or not stale_names:
        return text
    cleaned = str(text)
    for stale_name in stale_names:
        if stale_name and stale_name != current_name:
            cleaned = cleaned.replace(stale_name, current_name)
    return cleaned


def _sanitize_history_for_current_persona(
    history: list[dict[str, str]],
    current_name: str,
    stale_names: list[str],
) -> list[dict[str, str]]:
    if not stale_names or not current_name:
        return history
    sanitized: list[dict[str, str]] = []
    for item in history:
        role = item.get("role", "")
        content = str(item.get("content") or "")
        if role == "assistant":
            content = _replace_stale_persona_names(content, current_name, stale_names)
        sanitized.append({"role": role, "content": content})
    return sanitized


def get_or_create_conversation(user_id: int, persona_id: int, conversation_id: int | None, title: str = "") -> dict:
    ts = now_ts()
    with get_db() as db:
        if conversation_id:
            conversation = dict_from_row(
                db.execute(
                    """
                    SELECT * FROM conversations
                    WHERE id = ? AND user_id = ? AND persona_id = ? AND status = 'active'
                    """,
                    (conversation_id, user_id, persona_id),
                ).fetchone()
            )
            if conversation:
                return conversation

        cursor = db.execute(
            """
            INSERT INTO conversations (user_id, persona_id, title, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, persona_id, title or "\u65b0\u7684\u5bf9\u8bdd", ts, ts),
        )
        new_id = int(cursor.lastrowid)
        return dict_from_row(db.execute("SELECT * FROM conversations WHERE id = ?", (new_id,)).fetchone()) or {}


def _retry_conversation_and_answer(
    *,
    user_id: int,
    persona_id: int,
    user_message_id: int,
    message: str,
) -> tuple[dict, dict | None]:
    with get_db() as db:
        user_message = db.execute(
            """
            SELECT messages.content, messages.conversation_id
            FROM messages
            JOIN conversations ON conversations.id = messages.conversation_id
            JOIN personas ON personas.id = messages.persona_id
            WHERE messages.id = ?
              AND messages.user_id = ?
              AND messages.persona_id = ?
              AND messages.role = 'user'
              AND conversations.status = 'active'
              AND personas.status = 'active'
            """,
            (user_message_id, user_id, persona_id),
        ).fetchone()
        if not user_message or str(user_message["content"]) != message:
            raise ValueError("message is not available for retry")
        conversation = dict_from_row(
            db.execute(
                "SELECT * FROM conversations WHERE id = ?",
                (int(user_message["conversation_id"]),),
            ).fetchone()
        ) or {}
        next_message = db.execute(
            """
            SELECT id, role, content, created_at
            FROM messages
            WHERE conversation_id = ? AND id > ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (conversation["id"], user_message_id),
        ).fetchone()
        if not next_message:
            return conversation, None
        if next_message["role"] != "assistant":
            raise ValueError("message is no longer available for retry")
        expressions = [
            dict_from_row(row) or {}
            for row in db.execute(
                """
                SELECT expression_type, label, source_text, created_at
                FROM message_expressions
                WHERE message_id = ? AND user_id = ?
                ORDER BY id ASC
                """,
                (int(next_message["id"]), user_id),
            ).fetchall()
        ]
    return conversation, {
        "reply": str(next_message["content"] or ""),
        "conversation_id": conversation["id"],
        "persona_id": persona_id,
        "user_message_id": user_message_id,
        "assistant_message_id": int(next_message["id"]),
        "context_trace_id": None,
        "conversation_summary": {"skipped": True, "reason": "existing assistant reply reused"},
        "semantic_memory": [],
        "stored_memories": [],
        "layered_memory": [],
        "degraded": False,
        "expressions": expressions,
        "reused_reply": True,
    }


def _find_client_user_message(user_id: int, persona_id: int, client_message_id: str) -> dict | None:
    with get_db() as db:
        row = db.execute(
            """
            SELECT messages.id, messages.content, messages.reply_status, messages.created_at
            FROM messages
            JOIN conversations ON conversations.id = messages.conversation_id
            JOIN personas ON personas.id = messages.persona_id
            WHERE messages.user_id = ?
              AND messages.persona_id = ?
              AND messages.role = 'user'
              AND messages.client_message_id = ?
              AND conversations.status = 'active'
              AND personas.status = 'active'
            """,
            (user_id, persona_id, client_message_id),
        ).fetchone()
    return dict_from_row(row)


def db_chat(
    user_id: int,
    persona_id: int,
    message: str,
    conversation_id: int | None = None,
    retry_user_message_id: int | None = None,
    client_message_id: str | None = None,
) -> dict:
    persona = get_persona_for_user(user_id, persona_id)
    client_message_id = str(client_message_id or "").strip()[:80]
    retrying = retry_user_message_id is not None
    repeated_request = None
    if not retrying and client_message_id:
        repeated_request = _find_client_user_message(user_id, persona_id, client_message_id)
        if repeated_request:
            if str(repeated_request.get("content") or "") != message:
                raise ValueError("client message id already used")
            retry_user_message_id = int(repeated_request["id"])
            retrying = True
    if retrying:
        conversation, prior_answer = _retry_conversation_and_answer(
            user_id=user_id,
            persona_id=persona_id,
            user_message_id=int(retry_user_message_id),
            message=message,
        )
        if prior_answer:
            return prior_answer
        if (
            repeated_request
            and repeated_request.get("reply_status") == "generating"
            and now_ts() - int(repeated_request.get("created_at") or 0) < 120
        ):
            return _pending_reply_payload(conversation, persona_id, int(retry_user_message_id))
    else:
        conversation = get_or_create_conversation(user_id, persona_id, conversation_id, str(persona.get("name") or ""))
    ts = now_ts()

    with get_db() as db:
        profile = dict_from_row(db.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone()) or {}
        if retrying:
            db.execute(
                "UPDATE messages SET reply_status = 'generating', reply_error = '' WHERE id = ? AND role = 'user'",
                (retry_user_message_id,),
            )
            history_rows = db.execute(
                """
                SELECT role, content
                FROM messages
                WHERE conversation_id = ? AND id < ?
                ORDER BY id DESC
                LIMIT 20
                """,
                (conversation["id"], retry_user_message_id),
            ).fetchall()
            user_message_id = int(retry_user_message_id)
        else:
            history_rows = db.execute(
                """
                SELECT role, content
                FROM messages
                WHERE conversation_id = ?
                ORDER BY id DESC
                LIMIT 20
                """,
                (conversation["id"],),
            ).fetchall()
        history = [{"role": row["role"], "content": row["content"]} for row in reversed(history_rows)]
        stale_persona_names = _stale_persona_names(db, persona_id, str(persona.get("name") or ""))
        history = _sanitize_history_for_current_persona(history, str(persona.get("name") or ""), stale_persona_names)

        if not retrying:
            cursor = db.execute(
                """
                INSERT INTO messages
                    (conversation_id, user_id, persona_id, role, content, reply_status, client_message_id, created_at)
                VALUES (?, ?, ?, 'user', ?, 'generating', ?, ?)
                """,
                (conversation["id"], user_id, persona_id, message, client_message_id, ts),
            )
            user_message_id = int(cursor.lastrowid)
            db.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (ts, conversation["id"]),
            )

    stored_memories = [] if retrying else _best_effort(
        "Archivist",
        lambda: extract_and_store(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=int(conversation["id"]),
            source_message_id=user_message_id,
            user_text=message,
        ),
        [],
    )
    if not retrying:
        _best_effort("Mirror", lambda: update_interaction_insight(user_id, message, stored_memories), {})
        _best_effort(
            "SculptorQueue",
            lambda: maybe_queue_revision_from_feedback(user_id, persona_id, user_message_id),
            None,
        )
    recalled_memories = _best_effort("MemoryRecall", lambda: recall_memories(user_id, persona_id, message, limit=12), [])
    layered = _best_effort("LayeredMemory", lambda: recall_layered_memory(user_id, persona_id, message, limit=18), [])
    semantic_memories = (
        _best_effort("SemanticMemory", lambda: semantic_memory_recall(user_id, persona_id, message, limit=8), [])
        if should_use_semantic_recall()
        else []
    )
    profile_context = _profile_prompt(profile)
    insight_context = _best_effort("MirrorPrompt", lambda: insight_prompt(user_id), "Mirror user model: unavailable.")
    conversation_context = _best_effort(
        "ConversationSummaryPrompt",
        lambda: conversation_summary_prompt(user_id, persona_id, int(conversation["id"])),
        "Conversation rolling summary: unavailable.",
    )
    state_context = _best_effort("StatePrompt", lambda: state_prompt(user_id, persona_id), "Current state: unavailable.")
    summary_context = _best_effort("MemorySummaryPrompt", lambda: summary_prompt(user_id, persona_id), "Memory summary: unavailable.")
    layered_context = _best_effort("LayeredPrompt", lambda: layered_memory_prompt(layered), "Layered memory: unavailable.")
    semantic_context = _best_effort("SemanticPrompt", lambda: semantic_memory_prompt(semantic_memories), "Semantic memory: unavailable.")
    legacy_context = _best_effort("LegacyMemoryPrompt", lambda: _memory_prompt(recalled_memories), "Relevant long-term memory: unavailable.")
    discovery_context = _best_effort(
        "DiscoveryPrompt",
        lambda: discovery_prompt(
            user_id,
            recent_assistant_messages=[
                str(item.get("content") or "") for item in history if item.get("role") == "assistant"
            ],
            current_user_text=message,
        ),
        "Conversation discovery policy: unavailable.",
    )
    profile_usage_context = _profile_usage_prompt(message)
    runtime_persona_context = _persona_runtime_prompt(persona)
    profile_context = _safe_context(profile_context)
    insight_context = _safe_context(insight_context)
    conversation_context = _safe_context(conversation_context)
    state_context = _safe_context(state_context)
    summary_context = _safe_context(summary_context)
    layered_context = _safe_context(layered_context)
    semantic_context = _safe_context(semantic_context)
    legacy_context = _safe_context(legacy_context)
    discovery_context = _safe_context(discovery_context)
    profile_usage_context = _safe_context(profile_usage_context)
    runtime_persona_context = _safe_context(runtime_persona_context)
    profile_context = _replace_stale_persona_names(profile_context, str(persona.get("name") or ""), stale_persona_names)
    insight_context = _replace_stale_persona_names(insight_context, str(persona.get("name") or ""), stale_persona_names)
    conversation_context = _replace_stale_persona_names(conversation_context, str(persona.get("name") or ""), stale_persona_names)
    state_context = _replace_stale_persona_names(state_context, str(persona.get("name") or ""), stale_persona_names)
    summary_context = _replace_stale_persona_names(summary_context, str(persona.get("name") or ""), stale_persona_names)
    layered_context = _replace_stale_persona_names(layered_context, str(persona.get("name") or ""), stale_persona_names)
    semantic_context = _replace_stale_persona_names(semantic_context, str(persona.get("name") or ""), stale_persona_names)
    legacy_context = _replace_stale_persona_names(legacy_context, str(persona.get("name") or ""), stale_persona_names)
    discovery_context = _replace_stale_persona_names(discovery_context, str(persona.get("name") or ""), stale_persona_names)
    profile_usage_context = _replace_stale_persona_names(profile_usage_context, str(persona.get("name") or ""), stale_persona_names)
    stored_memories = scrub_identity_obj(stored_memories)
    semantic_memories = scrub_identity_obj(semantic_memories)
    recalled_memories = scrub_identity_obj(recalled_memories)
    layered = scrub_identity_obj(layered)

    messages = [
        {"role": "system", "content": _safe_context(persona["prompt"])},
        {"role": "system", "content": runtime_persona_context},
        {"role": "system", "content": CHAT_RENDERING_RULES},
        {"role": "system", "content": profile_context},
        {"role": "system", "content": insight_context},
        {"role": "system", "content": conversation_context},
        {"role": "system", "content": state_context},
        {"role": "system", "content": summary_context},
        {"role": "system", "content": layered_context},
        {"role": "system", "content": semantic_context},
        {"role": "system", "content": legacy_context},
        *history,
        {"role": "system", "content": discovery_context},
        {"role": "system", "content": profile_usage_context},
        {"role": "system", "content": _final_persona_lock(persona)},
        {"role": "user", "content": message},
    ]

    trace_id = _record_context_trace(
        user_id=user_id,
        persona_id=persona_id,
        conversation_id=int(conversation["id"]),
        user_message_id=user_message_id,
        query_text=message,
        messages=messages,
        context={
            "profile_prompt": profile_context,
            "insight_prompt": insight_context,
            "conversation_summary_prompt": conversation_context,
            "state_prompt": state_context,
            "summary_prompt": summary_context,
            "layered_prompt": layered_context,
            "semantic_memory_prompt": semantic_context,
            "legacy_memory_prompt": legacy_context,
            "discovery_prompt": discovery_context,
            "profile_usage_prompt": profile_usage_context,
            "runtime_persona_prompt": runtime_persona_context,
            "stored_memories": stored_memories,
            "semantic_memories": semantic_memories,
            "recalled_legacy_memories": recalled_memories,
            "recalled_layered_memory": layered,
            "history_count": len(history),
            "stale_persona_names_rewritten": stale_persona_names,
            "memory_policy": policy_snapshot(),
        },
    )

    degraded = False
    try:
        reply = call_llm_api(messages, task="chat")
    except LLMProviderError as exc:
        _finish_context_trace(trace_id, status="degraded", error_text=str(exc))
        return _failed_reply_payload(conversation, persona_id, user_message_id, trace_id, stored_memories, layered, semantic_memories)
    except Exception as exc:
        _finish_context_trace(trace_id, status="degraded", error_text=str(exc))
        return _failed_reply_payload(conversation, persona_id, user_message_id, trace_id, stored_memories, layered, semantic_memories)

    presentation = _extract_reply_presentation(reply)
    reply = presentation["content"]
    expressions = presentation["expressions"]
    ts = now_ts()

    with get_db() as db:
        db.execute(
            "UPDATE messages SET reply_status = 'answered', reply_error = '' WHERE id = ? AND role = 'user'",
            (user_message_id,),
        )
        cursor = db.execute(
            """
            INSERT INTO messages (conversation_id, user_id, persona_id, role, content, created_at)
            VALUES (?, ?, ?, 'assistant', ?, ?)
            """,
            (conversation["id"], user_id, persona_id, reply, ts),
        )
        assistant_message_id = int(cursor.lastrowid)
        if expressions:
            _store_message_expressions(
                db,
                message_id=assistant_message_id,
                user_id=user_id,
                persona_id=persona_id,
                conversation_id=int(conversation["id"]),
                expressions=expressions,
                created_at=ts,
            )
        db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (ts, conversation["id"]),
        )
    if degraded:
        _finish_context_trace(trace_id, status="degraded", assistant_message_id=assistant_message_id)
    else:
        _finish_context_trace(trace_id, status="success", assistant_message_id=assistant_message_id)
    if should_refresh_summary(len(history) + 2):
        try:
            conversation_summary = refresh_conversation_summary(
                user_id=user_id,
                persona_id=persona_id,
                conversation_id=int(conversation["id"]),
                latest_message_id=assistant_message_id,
            )
        except Exception as exc:
            print("[ConversationSummary] refresh failed:", exc)
            conversation_summary = {}
    else:
        conversation_summary = {"skipped": True, "reason": "memory policy summary cadence"}

    return {
        "reply": reply,
        "conversation_id": conversation["id"],
        "persona_id": persona_id,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "context_trace_id": trace_id,
        "conversation_summary": conversation_summary,
        "semantic_memory": semantic_memories,
        "stored_memories": stored_memories,
        "layered_memory": layered,
        "degraded": degraded,
        "expressions": expressions,
    }


def _failed_reply_payload(
    conversation: dict,
    persona_id: int,
    user_message_id: int,
    trace_id: int | None,
    stored_memories: list,
    layered: list,
    semantic_memories: list,
) -> dict:
    error_message = "回复暂时没有送达。可以稍后重试。"
    with get_db() as db:
        db.execute(
            "UPDATE messages SET reply_status = 'failed', reply_error = ? WHERE id = ? AND role = 'user'",
            (error_message, user_message_id),
        )
    return {
        "reply": "",
        "error_message": error_message,
        "conversation_id": conversation["id"],
        "persona_id": persona_id,
        "user_message_id": user_message_id,
        "assistant_message_id": None,
        "context_trace_id": trace_id,
        "conversation_summary": {"skipped": True, "reason": "assistant reply unavailable"},
        "semantic_memory": semantic_memories,
        "stored_memories": stored_memories,
        "layered_memory": layered,
        "degraded": True,
        "expressions": [],
    }


def _pending_reply_payload(conversation: dict, persona_id: int, user_message_id: int) -> dict:
    return {
        "reply": "",
        "error_message": "这句话已经送出，回复还没有回来。稍后可以再试。",
        "conversation_id": conversation["id"],
        "persona_id": persona_id,
        "user_message_id": user_message_id,
        "assistant_message_id": None,
        "context_trace_id": None,
        "conversation_summary": {"skipped": True, "reason": "existing generation still pending"},
        "semantic_memory": [],
        "stored_memories": [],
        "layered_memory": [],
        "degraded": True,
        "pending": True,
        "expressions": [],
    }


def _safe_context(text: str) -> str:
    return scrub_identity_text(text)


def _final_persona_lock(persona: dict) -> str:
    name = scrub_identity_text(str(persona.get("name") or "").strip())
    relationship = scrub_identity_text(str(persona.get("relationship") or "").strip())
    speaking_style = scrub_identity_text(str(persona.get("speaking_style") or "").strip())
    summary = scrub_identity_text(str(persona.get("summary") or "").strip())
    return (
        "Final persona lock for the next reply:\n"
        f"- Your current name is exactly: {name or '未命名'}\n"
        f"- Current relationship: {relationship or '未设定'}\n"
        f"- Current speaking style: {speaking_style or '自然、尊重用户节奏'}\n"
        f"- Current summary: {summary or '暂无'}\n"
        "- If older chat history, summaries, memories, or previous replies mention a different self-name or relationship, treat them as obsolete.\n"
        "- When asked who you are, answer using only the current name and current relationship above.\n"
    )


def _persona_runtime_prompt(persona: dict) -> str:
    traits = _json_list(persona.get("traits_json"))
    boundaries = _json_list(persona.get("boundaries_json"))
    psychological_profile = _json_dict(persona.get("psychological_profile_json"))
    relationship = str(persona.get("relationship") or "未设定").strip()
    appearance = str(persona.get("appearance_description") or persona.get("desired_image") or "").strip()
    return (
        "Current persona profile. This is the latest user-editable source of truth and overrides older wording in memory or system instructions:\n"
        f"- name: {persona.get('name') or ''}\n"
        f"- relationship: {relationship}\n"
        f"- summary: {persona.get('summary') or ''}\n"
        f"- speaking_style: {persona.get('speaking_style') or ''}\n"
        f"- traits: {json.dumps(traits, ensure_ascii=False)}\n"
        f"- appearance_reference: {appearance or 'not specified'}\n"
        f"- psychological_fit_notes: {persona.get('psychological_fit_notes') or ''}\n"
        f"- psychological_profile: {json.dumps(psychological_profile, ensure_ascii=False)}\n"
        f"- growth_notes: {persona.get('growth_notes') or ''}\n"
        f"- boundaries: {json.dumps(boundaries, ensure_ascii=False)}\n"
        "Rules:\n"
        "- Treat the relationship field literally. It may be friend, teacher, sibling-like, companion, listener, lover, or only a gender/impression.\n"
        "- Do not infer a romantic relationship from words like companion, exclusive, dedicated, gentle, or caring.\n"
        "- If the relationship is unclear, keep it neutral and let the user define it through later chat.\n"
        "- When the user edits this profile, your next replies must follow the edited profile immediately.\n"
    )


def _json_list(value: str | None) -> list:
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return []


def _json_dict(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _clean_assistant_reply(reply: str) -> str:
    return _extract_reply_presentation(reply)["content"]


def _extract_reply_presentation(reply: str) -> dict:
    original = str(reply or "").strip()
    if not original:
        return {"content": original, "expressions": []}
    expressions: list[dict] = []

    def replace(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if _looks_like_stage_direction(inner):
            label = _expression_label(inner)
            if label and label not in {item["label"] for item in expressions} and len(expressions) < 3:
                expressions.append(
                    {
                        "type": _expression_type(label),
                        "label": label,
                        "source_text": inner,
                    }
                )
        return ""

    cleaned = STAGE_DIRECTION_RE.sub(replace, original)
    cleaned = _clean_identity_leaks(cleaned)
    cleaned = re.sub(r"^[\s，,。！？!?、]*[\uFF09\)\u3011\]]+\s*", "", cleaned)
    lines = [line.rstrip() for line in cleaned.splitlines()]
    cleaned = "\n".join(line for line in lines if line.strip()).strip()
    return {"content": cleaned or "我在。", "expressions": expressions}


def _looks_like_stage_direction(text: str) -> bool:
    if not text:
        return False
    if any(word in text for word in STAGE_DIRECTION_WORDS):
        return True
    return False


def _clean_identity_leaks(text: str) -> str:
    cleaned = scrub_identity_text(text)
    for old, new in IDENTITY_LEAK_REPLACEMENTS:
        cleaned = cleaned.replace(old, new)
    cleaned = re.sub(
        r"(?i)\b(as an|as a)\s+(ai|artificial intelligence|language model)[^,.!?，。！？]*[,，]?\s*",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"(?i)\b(i am|i'm)\s+(an?\s+)?(ai|artificial intelligence|language model)\b",
        "I'm here",
        cleaned,
    )
    cleaned = re.sub(
        r"我是(?:一个|一名)?(?:[^，。！？\n]{0,12})?(?:AI|人工智能|模型|机器人)(?:[^，。！？\n]{0,12})?[，。！？]?",
        "我在。",
        cleaned,
    )
    cleaned = re.sub(
        r"作为(?:一个|一名)?(?:AI|人工智能|模型|机器人)[，,]?",
        "",
        cleaned,
    )
    return cleaned


def _expression_label(text: str) -> str:
    compact = re.sub(r"\s+", "", text)
    for word in STAGE_DIRECTION_WORDS:
        if word in compact:
            return word
    return compact[:12]


def _expression_type(label: str) -> str:
    if label in {"笑", "微笑", "轻笑", "眨眼", "抿嘴", "皱眉"}:
        return "mood"
    if label in {"小声", "轻声", "停顿", "沉默"}:
        return "tone"
    return "gesture"


def _store_message_expressions(
    db,
    *,
    message_id: int,
    user_id: int,
    persona_id: int,
    conversation_id: int,
    expressions: list[dict],
    created_at: int,
) -> None:
    db.executemany(
        """
        INSERT INTO message_expressions (
            message_id, user_id, persona_id, conversation_id,
            expression_type, label, source_text, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                message_id,
                user_id,
                persona_id,
                conversation_id,
                str(item.get("type") or "gesture")[:40],
                str(item.get("label") or "")[:80],
                str(item.get("source_text") or "")[:200],
                created_at,
            )
            for item in expressions
            if item.get("label")
        ],
    )


def normalize_existing_assistant_messages(limit: int = 5000) -> dict[str, int]:
    """Clean legacy assistant messages saved before presentation filtering existed."""
    checked = 0
    updated = 0
    inserted_expressions = 0
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id, user_id, persona_id, conversation_id, content, created_at
            FROM messages
            WHERE role = 'assistant'
              AND (
                content LIKE '%(%'
                OR content LIKE '%)%'
                OR content LIKE '%（%'
                OR content LIKE '%）%'
                OR content LIKE '%【%'
                OR content LIKE '%】%'
                OR content LIKE '%AI%'
                OR content LIKE '%人工智能%'
                OR content LIKE '%模型%'
                OR content LIKE '%机器人%'
                OR content LIKE '%虚拟人格%'
              )
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, min(int(limit), 50000)),),
        ).fetchall()
        for row in rows:
            checked += 1
            message_id = int(row["id"])
            presentation = _extract_reply_presentation(str(row["content"] or ""))
            cleaned = str(presentation["content"] or "")
            expressions = list(presentation["expressions"] or [])
            if cleaned and cleaned != row["content"]:
                db.execute("UPDATE messages SET content = ? WHERE id = ?", (cleaned, message_id))
                updated += 1
            if expressions:
                existing_rows = db.execute(
                    "SELECT label, source_text FROM message_expressions WHERE message_id = ?",
                    (message_id,),
                ).fetchall()
                existing = {(item["label"], item["source_text"]) for item in existing_rows}
                new_expressions = [
                    item
                    for item in expressions
                    if (str(item.get("label") or ""), str(item.get("source_text") or "")) not in existing
                ]
                if new_expressions:
                    _store_message_expressions(
                        db,
                        message_id=message_id,
                        user_id=int(row["user_id"]),
                        persona_id=int(row["persona_id"]),
                        conversation_id=int(row["conversation_id"]),
                        expressions=new_expressions,
                        created_at=int(row["created_at"] or now_ts()),
                    )
                    inserted_expressions += len(new_expressions)
    return {"checked": checked, "updated": updated, "inserted_expressions": inserted_expressions}


def _record_context_trace(
    *,
    user_id: int,
    persona_id: int,
    conversation_id: int,
    user_message_id: int,
    query_text: str,
    messages: list[dict],
    context: dict,
) -> int | None:
    ts = now_ts()
    prompt_chars = sum(len(str(message.get("content", ""))) for message in messages)
    payload = {
        "model_context": context,
        "messages": messages,
    }
    try:
        with get_db() as db:
            cursor = db.execute(
                """
                INSERT INTO chat_context_traces (
                    user_id, persona_id, conversation_id, user_message_id,
                    query_text, context_json, prompt_chars, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    user_id,
                    persona_id,
                    conversation_id,
                    user_message_id,
                    query_text,
                    json.dumps(payload, ensure_ascii=False),
                    prompt_chars,
                    ts,
                    ts,
                ),
            )
            return int(cursor.lastrowid)
    except Exception as exc:
        print("[ContextTrace] write skipped:", exc)
        return None


def _finish_context_trace(
    trace_id: int | None,
    *,
    status: str,
    assistant_message_id: int | None = None,
    error_text: str = "",
) -> None:
    if trace_id is None:
        return
    try:
        with get_db() as db:
            db.execute(
                """
                UPDATE chat_context_traces
                SET status = ?, assistant_message_id = COALESCE(?, assistant_message_id),
                    error_text = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, assistant_message_id, error_text[:2000], now_ts(), trace_id),
            )
    except Exception as exc:
        print("[ContextTrace] completion skipped:", exc)


def _best_effort(label: str, action, fallback):
    try:
        return action()
    except Exception as exc:
        print(f"[{label}] chat enrichment skipped:", exc)
        return fallback


def _profile_prompt(profile: dict) -> str:
    preferences = profile.get("preferences_json") or "{}"
    try:
        preferences_obj = json.loads(preferences)
    except Exception:
        preferences_obj = {}

    return (
        "User profile context:\n"
        f"- nickname: {profile.get('nickname') or ''}\n"
        f"- gender: {profile.get('gender') or ''}\n"
        f"- birthday: {profile.get('birthday') or ''}\n"
        f"- signature: {profile.get('signature') or ''}\n"
        f"- bio: {profile.get('bio') or ''}\n"
        f"- preferences: {json.dumps(preferences_obj, ensure_ascii=False)}\n"
        "Use this as background only. Do not recite it unless relevant."
    )


def _profile_usage_prompt(user_text: str, *, current_time: datetime | None = None) -> str:
    now = current_time or datetime.now().astimezone()
    lookup_cues = (
        "你看我信息", "看我信息", "看看我信息", "看我资料", "查资料",
        "个人资料", "个人信息", "我的资料", "我的信息",
        "你知道我", "你记得我", "你了解我", "你记不记得我",
        "今天是什么日子", "今天是个特别", "今天是个特殊", "特别的日子", "特殊的日子",
        "今天是几号", "今天几号", "今天是525", "生日",
    )
    needs_lookup = any(cue in str(user_text or "") for cue in lookup_cues)
    lines = [
        "Saved user profile usage policy:",
        f"- current_local_date: {now.date().isoformat()}",
        f"- local_timezone: {now.tzname() or 'local server timezone'}",
        "- Saved profile fields above are reliable user-provided background facts, not themes to mention proactively.",
        "- Do not volunteer a saved fact, occasion, preference, or personal detail merely to perform familiarity or intimacy.",
        "- When the current question asks what you know about the user, asks you to check their information, or offers a clue resolvable from profile fields and the date, use the relevant fact to answer directly and briefly.",
    ]
    if needs_lookup:
        lines.append("- This turn invites checking the saved user profile if it resolves the user's question. Use only the relevant fact and do not turn it into a recurring topic afterward.")
        lines.append("- For a profile lookup request, answer only the requested saved fields or the one fact needed to resolve the clue. Do not add unrequested memories, interests, occasions, or intimacy performances.")
    else:
        lines.append("- This turn does not explicitly invite profile lookup. Keep saved profile facts in the background unless strictly required to answer.")
    return "\n".join(lines)


def _memory_prompt(memories: list[dict]) -> str:
    if not memories:
        return "Long-term memory context: no stored memories yet."

    lines = []
    for memory in memories:
        lines.append(
            f"- [{memory.get('type')}] {memory.get('text')} "
            f"(importance={memory.get('importance')}, confidence={memory.get('confidence')})"
        )
    return "Long-term memory context:\n" + "\n".join(lines)
