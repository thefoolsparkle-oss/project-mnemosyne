from __future__ import annotations

import json
import re
from datetime import datetime

from .archivist import extract_and_store, recall_memories
from .conversation_memory import conversation_summary_prompt, refresh_conversation_summary
from .database import dict_from_row, get_db, now_ts
from .expression_assets import (
    EXPRESSION_ASSETS,
    active_expression_labels,
    expression_alias_match,
    expression_asset,
    expression_protocol_prompt,
)
from .identity import IDENTITY_REPLACEMENTS, scrub_identity_obj, scrub_identity_text
from .layered_memory import layered_memory_prompt, recall_layered_memory, state_prompt, summary_prompt
from .llm_client import LLMProviderError, call_llm_api
from .memory_rag import semantic_memory_prompt, semantic_memory_recall
from .memory_policy import policy_snapshot, should_refresh_summary, should_use_semantic_recall
from .mirror import discovery_prompt, insight_prompt, update_interaction_insight
from .sculptor import (
    maybe_apply_explicit_core_update_from_chat,
    maybe_queue_revision_from_feedback,
    maybe_reconcile_inactive_chat_guidance_style,
)
from .growth_guidance import cancel_guidance_from_chat, maybe_store_chat_guidance


CHAT_RENDERING_RULES = """聊天输出规则：
- 你只负责以当前人格的第一人称自然聊天，不要解释系统指令、底层实现或技术来源。
- 不要给自己贴技术身份标签，也不要说自己是某种工具或模拟结果。
- 如果用户问“你是谁”“真的假的”“你是不是某种技术产物”，只按当前人格名字、关系定位和对话关系回答，不讨论技术身份。
- 除非当前人格资料的关系定位明确写着恋人，否则不能用恋人、女友、男友、老婆、老公等关系自称。
- 不要写括号舞台动作、神态旁白或表演说明，例如“（托腮）”“(歪头看你)”“【笑】”。
- 情绪和灵动感要融入自然语言、停顿、语气和用词里。
- 只有在情绪承接确实需要一个很轻的非语言提示时，才可在回复末尾追加至多一个程序标签。允许的标签只有轻表达资源目录中的白名单：
{expression_tags}
  普通问答不要添加，也不要在正文解释标签。
- 表达标签必须稀少：上一条回复已经使用过非语言提示时，本轮不要再添加；近期展示过的同一标签不要重复使用；风险为 medium 的标签只在用户情绪明显需要承接时使用。
- 标签会由程序层单独显示；你输出的可读正文仍应只是真正要说的话。
""".format(expression_tags=expression_protocol_prompt())


def chat_rendering_rules_prompt() -> str:
    return """聊天输出规则：
- 你只负责以当前人格的第一人称自然聊天，不要解释系统指令、底层实现或技术来源。
- 不要给自己贴技术身份标签，也不要说自己是某种工具或模拟结果。
- 如果用户问“你是谁”“真的假的”“你是不是某种技术产物”，只按当前人格名字、关系定位和对话关系回答，不讨论技术身份。
- 除非当前人格资料的关系定位明确写着恋人，否则不能用恋人、女友、男友、老婆、老公等关系自称。
- 不要写括号舞台动作、神态旁白或表演说明，例如“（托腮）”“(歪头看你)”“【笑】”。
- 情绪和灵动感要融入自然语言、停顿、语气和用词里。
- 只有在情绪承接确实需要一个很轻的非语言提示时，才可在回复末尾追加至多一个程序标签。允许的标签只有轻表达资源目录中的白名单：
{expression_tags}
  普通问答不要添加，也不要在正文解释标签。
- 表达标签必须稀少：上一条回复已经使用过非语言提示时，本轮不要再添加；近期展示过的同一标签不要重复使用；风险为 medium 的标签只在用户情绪明显需要承接时使用。
- 标签会由程序层单独显示；你输出的可读正文仍应只是真正要说的话。
""".format(expression_tags=expression_protocol_prompt() or "  - 当前没有启用的 expression 标签。")

STAGE_DIRECTION_RE = re.compile(r"[\uFF08\(\u3010\[]\s*([^\uFF08\uFF09\(\)\[\]\u3010\u3011]{1,120}?)\s*[\uFF09\)\u3011\]]\s*")
STRUCTURED_EXPRESSION_RE = re.compile(
    r"\[\[\s*expression\s*:\s*([a-zA-Z_-]+)\s*:\s*([^\]\r\n]{1,20})\s*\]\]",
    re.IGNORECASE,
)
STRUCTURED_EXPRESSION_LABELS: dict[str, set[str]] = {}
for asset in EXPRESSION_ASSETS:
    STRUCTURED_EXPRESSION_LABELS.setdefault(str(asset["expression_type"]), set()).add(str(asset["label"]))
EXPRESSION_RECENT_WINDOW = 4
EXPRESSION_POLICY_LOOKBACK = 8
EXPRESSION_DISABLE_PATTERNS = (
    "以后别发表情",
    "以后不要发表情",
    "以后不用表情",
    "别发表情",
    "不要发表情",
    "不用表情",
    "别加表情",
    "不要加表情",
    "别用表情",
    "不要用表情",
    "别用轻表达",
    "不要轻表达",
    "关闭轻表达",
    "关掉轻表达",
    "别加动作",
    "不要动作",
    "别发动作",
    "不要发动作",
    "别用动作标签",
    "不要动作标签",
    "别加expression标签",
    "不要expression标签",
    "expressionoff",
    "disableexpression",
)
EXPRESSION_SUBTLE_PATTERNS = (
    "少发表情",
    "少用表情",
    "表情少一点",
    "轻表达少一点",
    "少用轻表达",
    "动作少一点",
    "少用动作标签",
    "表情克制一点",
    "轻表达克制一点",
    "subtleexpression",
)
EXPRESSION_ENABLE_PATTERNS = (
    "可以发表情了",
    "可以用表情了",
    "可以加表情了",
    "表情可以开",
    "正常发表情",
    "表情正常一点",
    "轻表达正常一点",
    "打开轻表达",
    "开启轻表达",
    "恢复轻表达",
    "可以用轻表达",
    "可以加动作了",
    "可以用动作标签",
    "动作标签可以开",
    "expressionon",
    "enableexpression",
)
EXPRESSION_SUPPORT_SCENE_MARKERS = (
    "累",
    "疲惫",
    "难过",
    "低落",
    "崩溃",
    "撑不住",
    "害怕",
    "焦虑",
    "失眠",
    "想哭",
    "陪我",
    "安慰",
    "抱抱",
    "不舒服",
    "委屈",
    "心烦",
    "压力",
)
EXPRESSION_PLAYFUL_SCENE_MARKERS = (
    "哈哈",
    "笑死",
    "好玩",
    "有趣",
    "开玩笑",
    "逗",
    "玩一下",
    "调侃",
)
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
    defer_summary_refresh: bool = False,
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

    expression_preference_update = _maybe_update_expression_preference_from_chat(
        user_id=user_id,
        persona_id=persona_id,
        user_text=message,
        source_message_id=user_message_id,
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
            "GrowthGuidanceStop",
            lambda: cancel_guidance_from_chat(user_id, persona_id, message, user_message_id),
            [],
        )
        _best_effort(
            "GrowthGuidance",
            lambda: maybe_store_chat_guidance(user_id, persona_id, message, user_message_id, stored_memories),
            None,
        )
        core_revision = _best_effort(
            "SculptorCoreUpdate",
            lambda: maybe_apply_explicit_core_update_from_chat(user_id, persona_id, message, user_message_id),
            None,
        )
        if core_revision:
            persona = get_persona_for_user(user_id, persona_id)
        queued_revision = _best_effort(
            "SculptorQueue",
            lambda: maybe_queue_revision_from_feedback(
                user_id,
                persona_id,
                user_message_id,
                allow_handled_core=bool(core_revision),
            ),
            None,
        )
        if queued_revision and queued_revision.get("status") == "applied":
            persona = get_persona_for_user(user_id, persona_id)
        reconciled_revision = _best_effort(
            "SculptorGuidanceReconcile",
            lambda: maybe_reconcile_inactive_chat_guidance_style(user_id, persona_id),
            None,
        )
        if reconciled_revision:
            persona = get_persona_for_user(user_id, persona_id)
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
    expression_policy = _recent_expression_policy(user_id, persona_id, int(conversation["id"]))
    expression_policy.update(_expression_scene_context(message))
    expression_policy.update(_persona_expression_style_context(persona))
    expression_policy_context = _expression_policy_prompt(expression_policy)
    active_preference_context = _active_preference_prompt(user_id, persona_id)
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
    expression_policy_context = _safe_context(expression_policy_context)
    active_preference_context = _safe_context(active_preference_context)
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
        {"role": "system", "content": chat_rendering_rules_prompt()},
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
        {"role": "system", "content": active_preference_context},
        {"role": "system", "content": expression_policy_context},
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
            "active_preference_prompt": active_preference_context,
            "expression_policy_prompt": expression_policy_context,
            "expression_policy": expression_policy,
            "expression_preference_update": expression_preference_update,
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
    expressions = _apply_expression_policy(presentation["expressions"], expression_policy)
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
        if defer_summary_refresh:
            conversation_summary = {"scheduled": True, "reason": "refresh after reply response"}
        else:
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
        "- The speaking style above is a baseline; for response style and support behavior, follow newer explicit active user preferences when they differ.\n"
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

    def replace_structured(match: re.Match[str]) -> str:
        expression_type = match.group(1).strip().lower()
        label = re.sub(r"\s+", "", match.group(2))
        if (
            not expressions
            and label in active_expression_labels().get(expression_type, set())
        ):
            expressions.append(
                {
                    "type": expression_type,
                    "label": label,
                    "source_text": match.group(0),
                }
            )
        return ""

    cleaned = STRUCTURED_EXPRESSION_RE.sub(replace_structured, original)
    structured_expression = bool(expressions)
    active_labels = active_expression_labels()
    allow_fallback_expressions = any(active_labels.values())

    def replace(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        if _looks_like_stage_direction(inner):
            label = _expression_label(inner)
            limit = 1 if structured_expression else 3
            if (
                allow_fallback_expressions
                and label
                and _fallback_expression_allowed(label)
                and label not in {item["label"] for item in expressions}
                and len(expressions) < limit
            ):
                expressions.append(
                    {
                        "type": _expression_type(label),
                        "label": label,
                        "source_text": inner,
                    }
                )
        return ""

    cleaned = STAGE_DIRECTION_RE.sub(replace, cleaned)
    cleaned = _clean_identity_leaks(cleaned)
    cleaned = re.sub(r"^[\s，,。！？!?、]*[\uFF09\)\u3011\]]+\s*", "", cleaned)
    lines = [line.rstrip() for line in cleaned.splitlines()]
    cleaned = "\n".join(line for line in lines if line.strip()).strip()
    return {"content": cleaned or "我在。", "expressions": expressions}


def _fallback_expression_allowed(label: str) -> bool:
    known_type = None
    for expression_type, labels in STRUCTURED_EXPRESSION_LABELS.items():
        if label in labels:
            known_type = expression_type
            break
    if not known_type:
        return False
    return label in active_expression_labels().get(known_type, set())


def _expression_preference_intent(text: str) -> str | None:
    compact = re.sub(r"\s+", "", str(text or "").lower())
    if not compact:
        return None
    disable_positions = [
        compact.rfind(pattern)
        for pattern in EXPRESSION_DISABLE_PATTERNS
        if compact.rfind(pattern) >= 0
    ]
    subtle_positions = [
        compact.rfind(pattern)
        for pattern in EXPRESSION_SUBTLE_PATTERNS
        if compact.rfind(pattern) >= 0
    ]
    enable_positions = [
        compact.rfind(pattern)
        for pattern in EXPRESSION_ENABLE_PATTERNS
        if compact.rfind(pattern) >= 0
    ]
    if not disable_positions and not subtle_positions and not enable_positions:
        return None
    latest = max(
        [("disable", position) for position in disable_positions]
        + [("subtle", position) for position in subtle_positions]
        + [("enable", position) for position in enable_positions],
        key=lambda item: item[1],
    )
    return latest[0]


def _maybe_update_expression_preference_from_chat(
    *,
    user_id: int,
    persona_id: int,
    user_text: str,
    source_message_id: int,
) -> dict | None:
    intent = _expression_preference_intent(user_text)
    if not intent:
        return None
    mode = {"disable": "off", "subtle": "subtle", "enable": "normal"}[intent]
    enabled = 0 if mode == "off" else 1
    ts = now_ts()
    with get_db() as db:
        db.execute(
            """
            INSERT INTO expression_preferences (user_id, persona_id, enabled, mode, source_message_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, persona_id) DO UPDATE SET
                enabled = excluded.enabled,
                mode = excluded.mode,
                source_message_id = excluded.source_message_id,
                updated_at = excluded.updated_at
            """,
            (user_id, persona_id, enabled, mode, source_message_id, ts),
        )
    return {
        "intent": intent,
        "enabled": bool(enabled),
        "mode": mode,
        "source_message_id": source_message_id,
        "updated_at": ts,
    }


def _recent_expression_policy(user_id: int, persona_id: int, conversation_id: int) -> dict:
    with get_db() as db:
        preference_row = db.execute(
            """
            SELECT enabled, mode, source_message_id, updated_at
            FROM expression_preferences
            WHERE user_id = ? AND persona_id = ?
            """,
            (user_id, persona_id),
        ).fetchone()
        preference = {
            "enabled": True,
            "mode": "normal",
            "source_message_id": None,
            "updated_at": 0,
        }
        if preference_row:
            mode = str(preference_row["mode"] or "").strip() or ("normal" if int(preference_row["enabled"] or 0) else "off")
            if mode not in {"off", "subtle", "normal"}:
                mode = "normal" if int(preference_row["enabled"] or 0) else "off"
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
            FROM messages
            WHERE user_id = ? AND persona_id = ? AND conversation_id = ? AND role = 'assistant'
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, persona_id, conversation_id, EXPRESSION_POLICY_LOOKBACK),
        ).fetchall()
        message_ids = [int(row["id"]) for row in message_rows]
        expression_rows = []
        if message_ids:
            placeholders = ", ".join("?" for _ in message_ids)
            expression_rows = db.execute(
                f"""
                SELECT message_id, label
                FROM message_expressions
                WHERE user_id = ? AND persona_id = ? AND conversation_id = ?
                  AND message_id IN ({placeholders})
                ORDER BY id ASC
                """,
                (user_id, persona_id, conversation_id, *message_ids),
            ).fetchall()
    labels_by_message: dict[int, list[str]] = {message_id: [] for message_id in message_ids}
    for row in expression_rows:
        label = str(row["label"] or "").strip()
        if label:
            labels_by_message.setdefault(int(row["message_id"]), []).append(label)
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


def _active_preference_prompt(user_id: int, persona_id: int) -> str:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT request_text
            FROM persona_growth_requests
            WHERE user_id = ? AND persona_id = ? AND withdrawn_at = 0
            ORDER BY updated_at DESC, id DESC
            LIMIT 5
            """,
            (user_id, persona_id),
        ).fetchall()
    preferences = [
        scrub_identity_text(str(row["request_text"] or "").strip())
        for row in rows
        if str(row["request_text"] or "").strip()
    ]
    if not preferences:
        return "Active user companionship preferences: none explicitly recorded outside chat."
    return (
        "Active user companionship preferences. These were written by the user and apply immediately to how you reply:\n"
        + "\n".join(f"- {item}" for item in preferences)
        + "\n- These active preferences override older stored style or support guidance when they conflict; prefer the newest active wording.\n"
        "- Follow these preferences naturally in the next reply. They adjust response style and support behavior, "
        "not your name or relationship identity."
    )


def _expression_policy_prompt(policy: dict) -> str:
    scene_prompt = _expression_scene_prompt(policy)
    style_prompt = _expression_persona_style_prompt(policy)
    if policy.get("disabled_by_user"):
        return (
            scene_prompt
            + "\n"
            + style_prompt
            + "\n"
            "Light expression preference: the user has explicitly turned off expression labels. "
            "Do not output [[expression:...]] tags, bracketed actions, emoji-like stage directions, "
            "or substitute nonverbal cues. Reply only with natural chat text."
        )
    if policy.get("suppress_all"):
        if policy.get("subtle_mode"):
            return (
                scene_prompt
                + "\n"
                + style_prompt
                + "\n"
                "本轮轻表达节奏约束：用户选择了克制轻表达，且近期已经展示过非语言提示，"
                "本轮不得输出 expression 标签，也不要用括号动作替代。"
            )
        return (
            scene_prompt
            + "\n"
            + style_prompt
            + "\n"
            "本轮轻表达节奏约束：上一条回复刚显示过非语言提示，"
            "本轮不得输出 expression 标签，也不要用括号动作替代。"
        )
    if policy.get("subtle_mode"):
        return (
            scene_prompt
            + "\n"
            + style_prompt
            + "\n"
            "本轮轻表达节奏约束：用户选择了克制轻表达。只有在明显需要安慰、确认或停顿时，"
            "才可使用至多一个 expression 标签；普通闲聊不要添加。"
        )
    recent_labels = [str(label) for label in policy.get("recent_labels") or [] if label]
    if recent_labels:
        return (
            scene_prompt
            + "\n"
            + style_prompt
            + "\n"
            "本轮轻表达节奏约束：近期已经展示过这些标签："
            f"{'、'.join(recent_labels)}。本轮不得重复这些标签；没有真正必要的新提示时不要添加标签。"
        )
    return scene_prompt + "\n" + style_prompt + "\n本轮轻表达节奏约束：近期没有已展示的提示；即便如此，也仅在确有必要时使用至多一个标签。"


def _expression_scene_context(user_text: str) -> dict:
    compact = re.sub(r"\s+", "", str(user_text or "").lower())
    if any(marker in compact for marker in EXPRESSION_SUPPORT_SCENE_MARKERS):
        return {
            "expression_scene": "support_needed",
            "expression_allowed_groups": ["support", "care", "warmth", "acknowledgement"],
        }
    if any(marker in compact for marker in EXPRESSION_PLAYFUL_SCENE_MARKERS):
        return {
            "expression_scene": "playful",
            "expression_allowed_groups": ["warmth", "acknowledgement"],
        }
    return {
        "expression_scene": "ordinary",
        "expression_allowed_groups": ["warmth", "acknowledgement"],
    }


def _expression_scene_prompt(policy: dict) -> str:
    scene = str(policy.get("expression_scene") or "unspecified")
    allowed = "、".join(str(item) for item in policy.get("expression_allowed_groups") or [])
    if scene == "support_needed":
        return (
            "本轮轻表达场景：support_needed（用户明显疲惫、低落、求陪伴或需要安慰）。"
            f"如确实需要，优先从这些资源分组选择：{allowed}；中风险标签仍要克制。"
        )
    if scene == "playful":
        return (
            "本轮轻表达场景：playful（轻松、打趣或玩笑）。"
            f"如确实需要，只从这些资源分组选择：{allowed}；不要使用担心类表达。"
        )
    if scene == "ordinary":
        return (
            "本轮轻表达场景：ordinary（普通信息、短确认或无明显情绪承接需求）。"
            f"普通闲聊不要为了活泼硬加标签；如确实需要，只从这些资源分组选择：{allowed}。"
        )
    return "本轮轻表达场景：unspecified。按总体节奏约束保持稀少。"


def _persona_expression_style_context(persona: dict) -> dict:
    text = re.sub(
        r"\s+",
        "",
        " ".join(
            str(persona.get(field) or "").lower()
            for field in ("summary", "relationship", "speaking_style", "growth_notes")
        ),
    )
    if any(marker in text for marker in ("安静", "简短", "克制", "慢", "少说", "沉稳", "可靠")):
        return {
            "expression_persona_style": "restrained",
            "expression_persona_preferred_groups": ["support", "acknowledgement"],
            "expression_persona_avoid_labels": ["轻笑"],
        }
    if any(marker in text for marker in ("活泼", "打趣", "俏皮", "开朗", "玩笑", "轻松")):
        return {
            "expression_persona_style": "playful",
            "expression_persona_preferred_groups": ["warmth", "acknowledgement"],
            "expression_persona_avoid_labels": ["担心"],
        }
    if any(marker in text for marker in ("恋人", "亲密", "陪伴", "温柔")):
        return {
            "expression_persona_style": "warm",
            "expression_persona_preferred_groups": ["warmth", "support"],
            "expression_persona_avoid_labels": [],
        }
    return {
        "expression_persona_style": "neutral",
        "expression_persona_preferred_groups": [],
        "expression_persona_avoid_labels": [],
    }


def _expression_persona_style_prompt(policy: dict) -> str:
    style = str(policy.get("expression_persona_style") or "neutral")
    preferred = "、".join(str(item) for item in policy.get("expression_persona_preferred_groups") or [])
    avoid = "、".join(str(item) for item in policy.get("expression_persona_avoid_labels") or [])
    if style == "restrained":
        return f"本轮人格轻表达风格：restrained。优先 {preferred or 'support、acknowledgement'}；避免 {avoid or '偏打趣标签'}。"
    if style == "playful":
        return f"本轮人格轻表达风格：playful。优先 {preferred or 'warmth、acknowledgement'}；避免 {avoid or '过度担心标签'}。"
    if style == "warm":
        return f"本轮人格轻表达风格：warm。优先 {preferred or 'warmth、support'}，但仍保持稀少。"
    return "本轮人格轻表达风格：neutral。按当前场景和节奏选择，避免固定套路。"


def _apply_expression_policy(expressions: list[dict], policy: dict) -> list[dict]:
    if policy.get("suppress_all"):
        return []
    scene = str(policy.get("expression_scene") or "")
    allowed_groups = {str(group) for group in policy.get("expression_allowed_groups") or [] if group}
    avoid_labels = {str(label) for label in policy.get("expression_persona_avoid_labels") or [] if label}
    recent_labels = {str(label) for label in policy.get("recent_labels") or [] if label}
    recent_label_distances = {
        str(label): int(distance)
        for label, distance in (policy.get("recent_label_distances") or {}).items()
        if label
    }
    kept: list[dict] = []
    for item in expressions:
        label = str(item.get("label") or "")
        if label in avoid_labels:
            continue
        asset = expression_asset(str(item.get("type") or ""), label)
        cooldown_turns = int((asset or {}).get("cooldown_turns") or EXPRESSION_RECENT_WINDOW)
        if label in recent_label_distances and recent_label_distances[label] < cooldown_turns:
            continue
        asset_group = str((asset or {}).get("group") or "")
        if scene and allowed_groups and asset_group and asset_group not in allowed_groups:
            continue
        if recent_labels and asset and asset.get("risk_level") == "medium" and scene != "support_needed":
            continue
        kept.append(item)
        break
    return kept


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
    alias = expression_alias_match(text)
    if alias:
        return alias["label"]
    compact = re.sub(r"\s+", "", text)
    for word in sorted(STAGE_DIRECTION_WORDS, key=len, reverse=True):
        if word in compact:
            return word
    return compact[:12]


def _expression_type(label: str) -> str:
    for expression_type, labels in active_expression_labels().items():
        if label in labels:
            return expression_type
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
