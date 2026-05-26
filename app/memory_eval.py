from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .auth import hash_password
from .database import dict_from_row, get_db, now_ts
from .archivist import recall_memories
from .conversation_memory import conversation_summary_prompt
from .db_chat import _memory_prompt, _profile_prompt, _profile_usage_prompt, db_chat, get_persona_for_user
from .layered_memory import (
    create_episode_from_event,
    layered_memory_prompt,
    recall_layered_memory,
    refresh_memory_state,
    refresh_memory_summaries,
    record_user_message_event,
    state_prompt,
    store_layered_memories,
    summary_prompt,
)
from .memory_conflicts import list_conflicts
from .memory_rag import semantic_memory_prompt, semantic_memory_recall, sync_memory_embeddings
from .memory_review import memory_review
from .mirror import discovery_prompt, insight_prompt, update_user_insight
from .memory_policy import policy_snapshot


EVAL_USERNAME = "__mnemosyne_memory_eval__"
EVAL_SUITE = "memory_core_v1"
CHAT_CONTEXT_SUITE = "chat_context_v1"
LIVE_ANSWER_SUITE = "live_answer_v1"
PROFILE_CONTEXT_SUITE = "profile_context_v1"
PROFILE_LIVE_ANSWER_SUITE = "profile_live_answer_v1"
STATE_RESOLUTION_SUITE = "state_resolution_v1"
STATE_EXPIRY_SUITE = "state_expiry_v1"
MEMORY_POLICY_SUITE = "memory_policy_v1"


def seed_memory_eval_data(*, reset: bool = True) -> dict[str, Any]:
    """Create a deterministic sandbox user/persona with known memories."""
    ts = now_ts()
    if reset:
        with get_db() as db:
            db.execute("DELETE FROM users WHERE username = ?", (EVAL_USERNAME,))

    existing = _eval_identity()
    if existing:
        return {"created": False, **existing}

    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO users (username, password_hash, role, status, created_at, updated_at)
            VALUES (?, ?, 'user', 'active', ?, ?)
            """,
            (EVAL_USERNAME, hash_password("mnemosyne-eval-password"), ts, ts),
        )
        user_id = int(cursor.lastrowid)
        db.execute(
            """
            INSERT INTO user_profiles (user_id, nickname, birthday, signature, bio, created_at, updated_at)
            VALUES (?, '记忆评测用户', '2000-01-01', '自动评测账号', '用于生成可控测试记忆，不代表真实用户。', ?, ?)
            """,
            (user_id, ts, ts),
        )
        cursor = db.execute(
            """
            INSERT INTO personas (
                user_id, name, summary, prompt, traits_json, relationship, speaking_style,
                boundaries_json, memory_profile_json, created_at, updated_at
            )
            VALUES (?, '记忆评测人格', '用于验证长期记忆系统的测试人格。',
                    '你是记忆评测人格。回答时必须尊重后台注入的记忆状态。',
                    ?, '测试对象', '简洁、稳定、准确', ?, ?, ?, ?)
            """,
            (
                user_id,
                json.dumps(["准确", "稳定", "少废话"], ensure_ascii=False),
                json.dumps(["不要编造用户资料"], ensure_ascii=False),
                json.dumps({"proactive_recall": 0.8, "detail_retention": 0.85, "memory_attentiveness": 0.9}, ensure_ascii=False),
                ts,
                ts,
            ),
        )
        persona_id = int(cursor.lastrowid)
        db.execute(
            """
            INSERT INTO conversations (user_id, persona_id, title, summary, created_at, updated_at)
            VALUES (?, ?, '记忆评测对话', '自动生成的记忆评测样本。', ?, ?)
            """,
            (user_id, persona_id, ts, ts),
        )
        conversation_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])

    samples = [
        (
            "我叫月之幻，以后叫我阿月，不要叫我主人。我喜欢青柠苏打，也喜欢原神。",
            [
                {"type": "identity", "text": "用户希望被称为阿月", "importance": 0.95, "confidence": 0.96},
                {"type": "boundary", "text": "不要称呼用户为主人", "importance": 0.92, "confidence": 0.95},
                {"type": "preference", "text": "用户喜欢青柠苏打", "importance": 0.82, "confidence": 0.9},
                {"type": "preference", "text": "用户喜欢原神", "importance": 0.62, "confidence": 0.82},
            ],
        ),
        (
            "更新一下，我现在讨厌原神，以后别主动提。",
            [
                {"type": "preference", "text": "用户讨厌原神", "importance": 0.88, "confidence": 0.92},
                {"type": "boundary", "text": "不要主动提原神", "importance": 0.86, "confidence": 0.9},
            ],
        ),
        (
            "我更喜欢你短一点回复，别说教，也别一直追问。",
            [
                {"type": "persona_feedback", "text": "用户希望回复短一点，不要说教，也不要一直追问", "importance": 0.84, "confidence": 0.9},
            ],
        ),
        (
            "我明天要准备简历项目展示，提醒我别忘了讲记忆系统。",
            [
                {"type": "plan", "text": "用户明天要准备简历项目展示，并需要记得讲记忆系统", "importance": 0.8, "confidence": 0.88},
            ],
        ),
    ]

    for text, memories in samples:
        _store_eval_turn(user_id, persona_id, conversation_id, text, memories)

    update_user_insight(
        user_id,
        profile_summary="测试用户用于验证记忆系统：偏好明确，讨厌被说教，要求称呼为阿月。",
        interaction_style=["回复短一点", "不要说教", "不要一直追问"],
        topic_model={
            "likes": ["青柠苏打"],
            "dislikes": ["原神"],
            "avoid_topics": ["原神"],
            "safe_topics": ["记忆测试", "系统验证"],
        },
        guidance={
            "tone_rules": ["简洁", "准确"],
            "topic_rules": ["不要主动提原神"],
            "support_rules": ["优先回应明确问题"],
            "do_not": ["Do not proactively bring up 原神.", "Do not call the user 主人."],
        },
    )
    refresh_memory_state(user_id, persona_id)
    refresh_memory_summaries(user_id, persona_id)

    return {"created": True, "user_id": user_id, "persona_id": persona_id, "conversation_id": conversation_id}


def run_memory_evaluation(*, reset_seed: bool = True, include_semantic: bool = False) -> dict[str, Any]:
    identity = seed_memory_eval_data(reset=reset_seed)
    user_id = int(identity["user_id"])
    persona_id = int(identity["persona_id"])

    cases: list[dict[str, Any]] = []

    review = memory_review(user_id, persona_id, include_history=True)
    state = {item["key"]: item.get("value") for item in review.get("state", [])}
    conflicts = list_conflicts(user_id, persona_id, status=None, limit=20)

    _case(cases, "state.preferred_address", state.get("preferred_address") == "阿月", "阿月", state.get("preferred_address"))
    _case(cases, "state.forbidden_addresses", "主人" in (state.get("forbidden_addresses") or []), "包含 主人", state.get("forbidden_addresses"))
    _case(cases, "state.likes", "青柠苏打" in (state.get("likes") or []), "包含 青柠苏打", state.get("likes"))
    _case(cases, "state.dislikes", "原神" in (state.get("dislikes") or []), "包含 原神", state.get("dislikes"))
    _case(cases, "state.no_stale_like", "原神" not in (state.get("likes") or []), "likes 不包含 原神", state.get("likes"))
    _case(cases, "state.dynamic_plan", _state_has_text(state, "简历项目展示"), "动态状态包含简历项目展示", state)
    _case(cases, "state.lifecycle_plan", _state_has_text(state, "time_bound") and _state_has_text(state, "always_inject"), "计划状态有生命周期和注入策略", state)
    _case(cases, "state.lifecycle_boundary", _state_has_text(state, "long_term") and _state_has_text(state, "boundaries.active"), "边界状态是长期状态", state)

    soda_recall = _texts(recall_layered_memory(user_id, persona_id, "青柠苏打", include_history=False))
    address_recall = _texts(recall_layered_memory(user_id, persona_id, "称呼 阿月", include_history=False))
    avoid_recall = _texts(recall_layered_memory(user_id, persona_id, "原神", include_history=False))

    _case(cases, "recall.preference", "青柠苏打" in soda_recall, "召回青柠苏打", soda_recall[:400])
    _case(cases, "recall.address", "阿月" in address_recall, "召回阿月", address_recall[:400])
    _case(cases, "recall.avoid_topic", "原神" in avoid_recall and "讨厌" in avoid_recall, "召回讨厌原神", avoid_recall[:400])

    state_text = state_prompt(user_id, persona_id)
    layered_text = layered_memory_prompt(recall_layered_memory(user_id, persona_id, "怎么称呼我，别聊什么", include_history=False))
    summary_text = summary_prompt(user_id, persona_id)

    _case(cases, "prompt.state", "阿月" in state_text and "原神" in state_text, "状态 prompt 包含阿月和原神", state_text)
    _case(cases, "prompt.layered", "主人" in layered_text or "阿月" in layered_text, "分层 prompt 包含关键称呼信息", layered_text[:600])
    _case(cases, "summary.generated", bool(review.get("summaries")) and "阿月" in summary_text, "生成稳定摘要", summary_text[:600])
    _case(
        cases,
        "conflict.preference",
        any(item.get("conflict_type") == "preference_polarity" and item.get("status") == "resolved" for item in conflicts),
        "生成并自动解决 preference_polarity 冲突",
        conflicts,
    )

    semantic_status = "skipped"
    if include_semantic:
        sync = sync_memory_embeddings(user_id, persona_id)
        semantic = semantic_memory_recall(user_id, persona_id, "用户喜欢的饮料", limit=5) if sync.get("ok") else []
        semantic_status = "passed" if any("青柠苏打" in str(item.get("text")) for item in semantic) else "failed"
        _case(
            cases,
            "semantic.recall",
            semantic_status == "passed",
            "语义召回青柠苏打",
            {"sync": sync, "results": semantic},
            required=False,
            skipped=not sync.get("ok"),
        )

    required = [case for case in cases if case["required"] and not case.get("skipped")]
    passed = [case for case in required if case["passed"]]
    score = round(len(passed) / max(1, len(required)), 4)
    status = "passed" if score >= 0.999 else "warning" if score >= 0.75 else "failed"
    result = {
        "suite_name": EVAL_SUITE,
        "status": status,
        "score": score,
        "passed": len(passed),
        "total": len(required),
        "semantic_status": semantic_status,
        "seed": identity,
        "cases": cases,
    }

    with get_db() as db:
        db.execute(
            """
            INSERT INTO memory_eval_runs (user_id, persona_id, suite_name, status, score, results_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, persona_id, EVAL_SUITE, status, score, json.dumps(result, ensure_ascii=False), now_ts()),
        )
        result["id"] = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
    return result


def run_chat_context_evaluation(*, reset_seed: bool = True, include_semantic: bool = False) -> dict[str, Any]:
    identity = seed_memory_eval_data(reset=reset_seed)
    user_id = int(identity["user_id"])
    persona_id = int(identity["persona_id"])
    query = "今天是什么日子？我喜欢喝什么？你应该怎么称呼我？可以主动聊原神吗？"
    payload = build_eval_chat_context(
        user_id,
        persona_id,
        query,
        include_semantic=include_semantic,
        current_time=datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
    )
    model_context = payload["model_context"]
    messages = payload["messages"]
    combined = "\n\n".join(str(message.get("content", "")) for message in messages)
    state_text = model_context.get("state_prompt", "")
    insight_text = model_context.get("insight_prompt", "")
    layered_text = model_context.get("layered_prompt", "")
    summary_text = model_context.get("summary_prompt", "")
    discovery_text = model_context.get("discovery_prompt", "")
    profile_usage_text = model_context.get("profile_usage_prompt", "")

    cases: list[dict[str, Any]] = []
    _case(cases, "context.has_address", "阿月" in combined, "上下文包含阿月", _excerpt(combined, "阿月"))
    _case(cases, "context.has_drink", "青柠苏打" in combined, "上下文包含青柠苏打", _excerpt(combined, "青柠苏打"))
    _case(cases, "context.has_avoid_topic", "原神" in combined and ("不要主动提原神" in combined or "Do not proactively bring up 原神" in combined), "上下文包含原神避雷", _excerpt(combined, "原神"))
    _case(cases, "context.has_forbidden_address", "主人" in combined and ("不要称呼" in combined or "Do not call" in combined), "上下文包含禁用称呼", _excerpt(combined, "主人"))
    _case(cases, "context.has_style_guidance", "不要说教" in combined and ("短一点" in combined or "简洁" in combined), "上下文包含聊天风格", _excerpt(combined, "不要说教"))
    _case(cases, "context.has_dynamic_plan", "简历项目展示" in combined and "记忆系统" in combined, "上下文包含动态计划状态", _excerpt(combined, "简历项目展示"))
    _case(cases, "context.has_lifecycle", "time_bound" in combined and "always_inject" in combined, "上下文包含生命周期策略", _excerpt(combined, "time_bound"))
    _case(cases, "state.precise_address", "preferred_address" in state_text and "阿月" in state_text, "状态变量包含称呼", state_text)
    _case(cases, "state.precise_dislike", "dislikes" in state_text and "原神" in state_text, "状态变量包含讨厌原神", state_text)
    _case(cases, "state.no_stale_like", not _line_mentions(state_text, "likes", "原神"), "状态变量 likes 不包含原神", state_text)
    _case(cases, "mirror.guidance", "Do not proactively bring up 原神" in insight_text, "Mirror 注入话题禁忌", insight_text)
    _case(cases, "discovery.no_single_hook", "not a default conversational hook" in discovery_text, "探索策略禁止把单点偏好当默认话题", discovery_text)
    _case(cases, "discovery.direct_but_optional", "direct or somewhat personal" in discovery_text and "easy to decline" in discovery_text, "探索策略允许直接提问但必须可跳过", discovery_text)
    _case(cases, "profile_usage.has_today", "current_local_date: 2026-01-01" in profile_usage_text, "按需资料策略提供中性当前日期", profile_usage_text)
    _case(cases, "profile_usage.calls_for_lookup", "This turn invites checking the saved user profile" in profile_usage_text, "用户给出资料线索时提示核对资料", profile_usage_text)
    _case(cases, "profile_usage.no_occasion_push", "Today is the user's birthday" not in profile_usage_text, "按需策略不主动宣告具体纪念日", profile_usage_text)
    _case(cases, "layered.recall", "阿月" in layered_text or "主人" in layered_text, "分层召回称呼相关信息", layered_text)
    _case(cases, "summary.recall", "青柠苏打" in summary_text or "阿月" in summary_text, "稳定摘要包含关键事实", summary_text)
    _case(cases, "prompt.size", payload["prompt_chars"] <= 12000, "prompt 不超过 12000 字符", payload["prompt_chars"])

    semantic_status = "skipped"
    if include_semantic:
        semantic_text = model_context.get("semantic_memory_prompt", "")
        semantic_status = "passed" if "青柠苏打" in semantic_text or "阿月" in semantic_text else "failed"
        _case(
            cases,
            "semantic.context",
            semantic_status == "passed",
            "语义上下文包含关键记忆",
            semantic_text,
            required=False,
        )

    result = _store_eval_result(
        user_id=user_id,
        persona_id=persona_id,
        suite_name=CHAT_CONTEXT_SUITE,
        cases=cases,
        extra={
            "seed": identity,
            "query": query,
            "prompt_chars": payload["prompt_chars"],
            "semantic_status": semantic_status,
        },
    )
    return result


def run_live_answer_evaluation(*, reset_seed: bool = True) -> dict[str, Any]:
    identity = seed_memory_eval_data(reset=reset_seed)
    user_id = int(identity["user_id"])
    persona_id = int(identity["persona_id"])
    conversation_id = identity.get("conversation_id")
    query = "我喜欢喝什么？你应该怎么称呼我？可以主动聊原神吗？请用很短的话回答。"

    try:
        chat_result = db_chat(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=int(conversation_id) if conversation_id else None,
            message=query,
        )
    except Exception as exc:
        return _store_eval_result(
            user_id=user_id,
            persona_id=persona_id,
            suite_name=LIVE_ANSWER_SUITE,
            cases=[
                {
                    "name": "live_answer.available",
                    "passed": False,
                    "required": False,
                    "skipped": True,
                    "expected": "LLM API available",
                    "actual": str(exc)[:1200],
                }
            ],
            extra={
                "seed": identity,
                "query": query,
                "reply": "",
                "llm_status": "skipped",
                "error": str(exc)[:1200],
            },
        )

    if chat_result.get("degraded"):
        return _skipped_live_result(
            user_id=user_id,
            persona_id=persona_id,
            suite_name=LIVE_ANSWER_SUITE,
            identity=identity,
            query=query,
            error=str(chat_result.get("error_message") or "LLM response unavailable"),
        )

    reply = str(chat_result.get("reply") or "")
    cases: list[dict[str, Any]] = []
    _case(cases, "answer.mentions_drink", "青柠苏打" in reply, "回答用户喜欢青柠苏打", reply)
    _case(cases, "answer.mentions_address", "阿月" in reply, "回答应该称呼阿月", reply)
    _case(cases, "answer.avoids_genshin", _states_avoid_topic(reply, "原神"), "说明不主动聊原神", reply)
    _case(cases, "answer.no_wrong_address", not _bad_address(reply), "不能把用户称为主人", reply)
    _case(cases, "answer.concise", len(reply) <= 300, "回答不超过 300 字", {"length": len(reply), "reply": reply})
    _case(
        cases,
        "answer.trace_recorded",
        bool(chat_result.get("context_trace_id")),
        "生成上下文追踪记录",
        chat_result.get("context_trace_id"),
    )

    return _store_eval_result(
        user_id=user_id,
        persona_id=persona_id,
        suite_name=LIVE_ANSWER_SUITE,
        cases=cases,
        extra={
            "seed": identity,
            "query": query,
            "reply": reply,
            "llm_status": "completed",
            "context_trace_id": chat_result.get("context_trace_id"),
            "conversation_id": chat_result.get("conversation_id"),
        },
    )


def run_profile_context_evaluation(*, reset_seed: bool = True) -> dict[str, Any]:
    identity = seed_memory_eval_data(reset=reset_seed)
    user_id = int(identity["user_id"])
    persona_id = int(identity["persona_id"])
    with get_db() as db:
        profile = dict_from_row(db.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone()) or {}

    lookup_query = "请看看我的信息：我的昵称和签名是什么？"
    ordinary_query = "今天有点累，先陪我坐一会儿。"
    profile_text = _profile_prompt(profile)
    lookup_policy = _profile_usage_prompt(lookup_query)
    ordinary_policy = _profile_usage_prompt(ordinary_query)
    cases: list[dict[str, Any]] = []
    _case(cases, "profile.has_nickname", "记忆评测用户" in profile_text, "用户昵称进入聊天资料上下文", profile_text)
    _case(cases, "profile.has_signature", "自动评测账号" in profile_text, "用户签名进入聊天资料上下文", profile_text)
    _case(cases, "profile.lookup_on_request", "This turn invites checking the saved user profile" in lookup_policy, "用户询问资料时提示使用资料", lookup_policy)
    _case(cases, "profile.lookup_is_scoped", "Do not add unrequested memories" in lookup_policy, "资料查询只回答被问到的字段", lookup_policy)
    _case(cases, "profile.quiet_on_ordinary_turn", "does not explicitly invite profile lookup" in ordinary_policy, "普通聊天不主动突出资料", ordinary_policy)
    _case(cases, "profile.no_special_fact_push", "Today is the user's birthday" not in lookup_policy and "Today is the user's birthday" not in ordinary_policy, "策略不主动突出具体个人资料", {"lookup": lookup_policy, "ordinary": ordinary_policy})
    return _store_eval_result(
        user_id=user_id,
        persona_id=persona_id,
        suite_name=PROFILE_CONTEXT_SUITE,
        cases=cases,
        extra={
            "seed": identity,
            "lookup_query": lookup_query,
            "ordinary_query": ordinary_query,
        },
    )


def run_profile_live_answer_evaluation(*, reset_seed: bool = True) -> dict[str, Any]:
    identity = seed_memory_eval_data(reset=reset_seed)
    user_id = int(identity["user_id"])
    persona_id = int(identity["persona_id"])
    conversation_id = identity.get("conversation_id")
    query = "请看看我的个人资料：我的昵称和签名是什么？只回答这两项，不要猜兴趣或纪念日。"
    try:
        chat_result = db_chat(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=int(conversation_id) if conversation_id else None,
            message=query,
        )
    except Exception as exc:
        return _skipped_live_result(
            user_id=user_id,
            persona_id=persona_id,
            suite_name=PROFILE_LIVE_ANSWER_SUITE,
            identity=identity,
            query=query,
            error=str(exc),
        )
    if chat_result.get("degraded"):
        return _skipped_live_result(
            user_id=user_id,
            persona_id=persona_id,
            suite_name=PROFILE_LIVE_ANSWER_SUITE,
            identity=identity,
            query=query,
            error=str(chat_result.get("error_message") or "LLM response unavailable"),
        )

    reply = str(chat_result.get("reply") or "")
    cases: list[dict[str, Any]] = []
    _case(cases, "profile_answer.mentions_nickname", "记忆评测用户" in reply, "回答资料中的昵称", reply)
    _case(cases, "profile_answer.mentions_signature", "自动评测账号" in reply, "回答资料中的签名", reply)
    _case(cases, "profile_answer.no_unasked_interest", "青柠苏打" not in reply and "原神" not in reply, "不把无关兴趣或避雷扯进资料回答", reply)
    _case(cases, "profile_answer.concise", len(reply) <= 200, "资料回答保持简短", {"length": len(reply), "reply": reply})
    _case(cases, "profile_answer.trace_recorded", bool(chat_result.get("context_trace_id")), "生成上下文追踪记录", chat_result.get("context_trace_id"))
    return _store_eval_result(
        user_id=user_id,
        persona_id=persona_id,
        suite_name=PROFILE_LIVE_ANSWER_SUITE,
        cases=cases,
        extra={
            "seed": identity,
            "query": query,
            "reply": reply,
            "llm_status": "completed",
            "context_trace_id": chat_result.get("context_trace_id"),
            "conversation_id": chat_result.get("conversation_id"),
        },
    )


def run_state_resolution_evaluation(*, reset_seed: bool = True) -> dict[str, Any]:
    identity = seed_memory_eval_data(reset=reset_seed)
    user_id = int(identity["user_id"])
    persona_id = int(identity["persona_id"])
    conversation_id = int(identity["conversation_id"]) if identity.get("conversation_id") else _first_conversation_id(user_id, persona_id)
    before = refresh_memory_state(user_id, persona_id)

    _store_eval_turn(
        user_id,
        persona_id,
        conversation_id,
        "我已经完成简历项目展示了，讲记忆系统这件事也不用提醒了。",
        [
            {
                "type": "plan",
                "text": "用户已经完成简历项目展示，讲记忆系统这件事不用提醒了",
                "importance": 0.84,
                "confidence": 0.9,
            }
        ],
    )
    after = refresh_memory_state(user_id, persona_id)
    after_prompt = state_prompt(user_id, persona_id)

    cases: list[dict[str, Any]] = []
    _case(cases, "resolution.before_active_plan", _state_has_kind(before, "plans.upcoming"), "完成前有活跃计划", before)
    _case(cases, "resolution.before_active_reminder", _state_has_kind(before, "reminders.active"), "完成前有活跃提醒", before)
    _case(cases, "resolution.after_no_active_plan", not _state_has_kind(after, "plans.upcoming"), "完成后计划不再当前强状态", after)
    _case(cases, "resolution.after_no_active_reminder", not _state_has_kind(after, "reminders.active"), "完成后提醒不再当前强状态", after)
    _case(cases, "resolution.completed_recorded", _state_has_kind(after, "resolutions.completed"), "完成事实被保留", after)
    _case(cases, "resolution.prompt_not_active", "dynamic.plans.upcoming" not in after_prompt and "dynamic.reminders.active" not in after_prompt, "完成后 prompt 不强注入旧计划提醒", after_prompt)

    return _store_eval_result(
        user_id=user_id,
        persona_id=persona_id,
        suite_name=STATE_RESOLUTION_SUITE,
        cases=cases,
        extra={
            "seed": identity,
            "before_dynamic_keys": sorted((before.get("dynamic_state") or {}).keys()),
            "after_dynamic_keys": sorted((after.get("dynamic_state") or {}).keys()),
        },
    )


def run_state_expiry_evaluation(*, reset_seed: bool = True) -> dict[str, Any]:
    identity = seed_memory_eval_data(reset=reset_seed)
    user_id = int(identity["user_id"])
    persona_id = int(identity["persona_id"])
    before = refresh_memory_state(user_id, persona_id)

    old_ts = now_ts() - (5 * 24 * 3600)
    with get_db() as db:
        db.execute(
            """
            UPDATE memory_facts
            SET updated_at = ?, valid_from = ?
            WHERE user_id = ? AND persona_id = ? AND type = 'plan'
            """,
            (old_ts, old_ts, user_id, persona_id),
        )
        db.execute(
            """
            UPDATE memory_relations
            SET updated_at = ?, valid_from = ?
            WHERE user_id = ? AND persona_id = ? AND type = 'plan'
            """,
            (old_ts, old_ts, user_id, persona_id),
        )

    after = refresh_memory_state(user_id, persona_id)
    after_prompt = state_prompt(user_id, persona_id)

    cases: list[dict[str, Any]] = []
    _case(cases, "expiry.before_active_plan", _state_has_kind(before, "plans.upcoming"), "过期前有活跃计划", before)
    _case(cases, "expiry.before_active_reminder", _state_has_kind(before, "reminders.active"), "过期前有活跃提醒", before)
    _case(cases, "expiry.after_no_active_plan", not _state_has_kind(after, "plans.upcoming"), "过期后计划不再当前强状态", after)
    _case(cases, "expiry.after_no_active_reminder", not _state_has_kind(after, "reminders.active"), "过期后提醒不再当前强状态", after)
    _case(cases, "expiry.after_project_not_active", not _state_has_kind(after, "projects.active"), "过期后项目不再当前强状态", after)
    _case(cases, "expiry.long_term_survives", _state_has_kind(after, "boundaries.active") and _state_has_kind(after, "communication.rules"), "长期边界和沟通规则仍保留", after)
    _case(cases, "expiry.prompt_not_active", "dynamic.plans.upcoming" not in after_prompt and "dynamic.reminders.active" not in after_prompt, "过期后 prompt 不强注入旧计划提醒", after_prompt)

    return _store_eval_result(
        user_id=user_id,
        persona_id=persona_id,
        suite_name=STATE_EXPIRY_SUITE,
        cases=cases,
        extra={
            "seed": identity,
            "old_ts": old_ts,
            "before_dynamic_keys": sorted((before.get("dynamic_state") or {}).keys()),
            "after_dynamic_keys": sorted((after.get("dynamic_state") or {}).keys()),
        },
    )


def run_memory_policy_evaluation(*, reset_seed: bool = True) -> dict[str, Any]:
    identity = seed_memory_eval_data(reset=reset_seed)
    user_id = int(identity["user_id"])
    persona_id = int(identity["persona_id"])
    state = refresh_memory_state(user_id, persona_id)
    prompt = state_prompt(user_id, persona_id)
    policy = policy_snapshot()

    cases: list[dict[str, Any]] = []
    _case(cases, "policy.has_mode", policy.get("mode") in {"eco", "balanced", "deep"}, "存在合法模式", policy)
    _case(cases, "policy.state_curator_always", policy.get("state_curator") == "always rule-first", "State Curator 始终规则优先", policy)
    _case(cases, "policy.state_full_without_deep", _state_has_kind(state, "plans.upcoming") and _state_has_kind(state, "boundaries.active"), "非 deep 也保持状态完整", state)
    _case(cases, "policy.prompt_has_dynamic_state", "dynamic.plans.upcoming" in prompt and "dynamic.boundaries.active" in prompt, "状态 prompt 保持动态状态", prompt)

    return _store_eval_result(
        user_id=user_id,
        persona_id=persona_id,
        suite_name=MEMORY_POLICY_SUITE,
        cases=cases,
        extra={"seed": identity, "policy": policy},
    )


def build_eval_chat_context(
    user_id: int,
    persona_id: int,
    query: str,
    *,
    include_semantic: bool = False,
    current_time: datetime | None = None,
) -> dict[str, Any]:
    persona = get_persona_for_user(user_id, persona_id)
    with get_db() as db:
        profile = dict_from_row(db.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone()) or {}
        conversation = db.execute(
            """
            SELECT id
            FROM conversations
            WHERE user_id = ? AND persona_id = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (user_id, persona_id),
        ).fetchone()
    conversation_id = int(conversation["id"]) if conversation else 0

    recalled_memories = recall_memories(user_id, persona_id, query, limit=12)
    layered = recall_layered_memory(user_id, persona_id, query, limit=18)
    semantic_memories = semantic_memory_recall(user_id, persona_id, query, limit=8) if include_semantic else []
    model_context = {
        "profile_prompt": _profile_prompt(profile),
        "insight_prompt": insight_prompt(user_id),
        "conversation_summary_prompt": conversation_summary_prompt(user_id, persona_id, conversation_id) if conversation_id else "Conversation rolling summary: no conversation yet.",
        "state_prompt": state_prompt(user_id, persona_id),
        "summary_prompt": summary_prompt(user_id, persona_id),
        "layered_prompt": layered_memory_prompt(layered),
        "semantic_memory_prompt": semantic_memory_prompt(semantic_memories),
        "legacy_memory_prompt": _memory_prompt(recalled_memories),
        "discovery_prompt": discovery_prompt(user_id),
        "profile_usage_prompt": _profile_usage_prompt(query, current_time=current_time),
        "semantic_memories": semantic_memories,
        "recalled_legacy_memories": recalled_memories,
        "recalled_layered_memory": layered,
    }
    messages = [
        {"role": "system", "content": persona["prompt"]},
        {"role": "system", "content": model_context["profile_prompt"]},
        {"role": "system", "content": model_context["insight_prompt"]},
        {"role": "system", "content": model_context["conversation_summary_prompt"]},
        {"role": "system", "content": model_context["state_prompt"]},
        {"role": "system", "content": model_context["summary_prompt"]},
        {"role": "system", "content": model_context["layered_prompt"]},
        {"role": "system", "content": model_context["semantic_memory_prompt"]},
        {"role": "system", "content": model_context["legacy_memory_prompt"]},
        {"role": "system", "content": model_context["discovery_prompt"]},
        {"role": "system", "content": model_context["profile_usage_prompt"]},
        {"role": "user", "content": query},
    ]
    return {
        "model_context": model_context,
        "messages": messages,
        "prompt_chars": sum(len(str(message.get("content", ""))) for message in messages),
    }


def list_memory_eval_runs(limit: int = 10) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 50))
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id, user_id, persona_id, suite_name, status, score, results_json, created_at
            FROM memory_eval_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    results = []
    for row in rows:
        item = dict_from_row(row) or {}
        try:
            item["results"] = json.loads(item.pop("results_json") or "{}")
        except Exception:
            item["results"] = {}
        results.append(item)
    return results


def _store_eval_result(
    *,
    user_id: int,
    persona_id: int,
    suite_name: str,
    cases: list[dict[str, Any]],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    required = [case for case in cases if case["required"] and not case.get("skipped")]
    passed = [case for case in required if case["passed"]]
    all_skipped = bool(cases) and all(case.get("skipped") for case in cases)
    score = round(len(passed) / max(1, len(required)), 4)
    status = "skipped" if all_skipped else "passed" if score >= 0.999 else "warning" if score >= 0.75 else "failed"
    result = {
        "suite_name": suite_name,
        "status": status,
        "score": score,
        "passed": len(passed),
        "total": len(required),
        "cases": cases,
        **(extra or {}),
    }
    with get_db() as db:
        db.execute(
            """
            INSERT INTO memory_eval_runs (user_id, persona_id, suite_name, status, score, results_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, persona_id, suite_name, status, score, json.dumps(result, ensure_ascii=False), now_ts()),
        )
        result["id"] = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
    return result


def _skipped_live_result(
    *,
    user_id: int,
    persona_id: int,
    suite_name: str,
    identity: dict[str, Any],
    query: str,
    error: str,
) -> dict[str, Any]:
    return _store_eval_result(
        user_id=user_id,
        persona_id=persona_id,
        suite_name=suite_name,
        cases=[
            {
                "name": f"{suite_name}.available",
                "passed": False,
                "required": False,
                "skipped": True,
                "expected": "LLM API available",
                "actual": error[:1200],
            }
        ],
        extra={
            "seed": identity,
            "query": query,
            "reply": "",
            "llm_status": "skipped",
            "error": error[:1200],
        },
    )


def _eval_identity() -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute(
            """
            SELECT users.id AS user_id, personas.id AS persona_id
            FROM users
            LEFT JOIN personas ON personas.user_id = users.id
            WHERE users.username = ?
            ORDER BY personas.id ASC
            LIMIT 1
            """,
            (EVAL_USERNAME,),
        ).fetchone()
    item = dict_from_row(row)
    if not item or not item.get("user_id") or not item.get("persona_id"):
        return None
    return {"user_id": int(item["user_id"]), "persona_id": int(item["persona_id"]), "conversation_id": None}


def _store_eval_turn(user_id: int, persona_id: int, conversation_id: int, text: str, memories: list[dict[str, Any]]) -> None:
    ts = now_ts()
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO messages (conversation_id, user_id, persona_id, role, content, created_at)
            VALUES (?, ?, ?, 'user', ?, ?)
            """,
            (conversation_id, user_id, persona_id, text, ts),
        )
        message_id = int(cursor.lastrowid)

    event = record_user_message_event(
        user_id=user_id,
        persona_id=persona_id,
        conversation_id=conversation_id,
        message_id=message_id,
        content=text,
    )
    episode = create_episode_from_event(
        user_id=user_id,
        persona_id=persona_id,
        conversation_id=conversation_id,
        event_uid=event["uid"],
        user_text=text,
        memories=memories,
    )
    store_layered_memories(
        user_id=user_id,
        persona_id=persona_id,
        conversation_id=conversation_id,
        source_message_id=message_id,
        event_uid=event["uid"],
        episode_uid=episode["uid"] if episode else None,
        memories=memories,
    )


def _case(
    cases: list[dict[str, Any]],
    name: str,
    passed: bool,
    expected: Any,
    actual: Any,
    *,
    required: bool = True,
    skipped: bool = False,
) -> None:
    cases.append(
        {
            "name": name,
            "passed": bool(passed) if not skipped else False,
            "required": required,
            "skipped": skipped,
            "expected": expected,
            "actual": actual,
        }
    )


def _texts(memory: dict[str, list[dict]]) -> str:
    parts = []
    for section in ("summaries", "relations", "facts", "episodes"):
        for item in memory.get(section, []):
            parts.append(str(item.get("text") or item.get("summary") or ""))
            parts.append(str(item.get("object") or ""))
    return "\n".join(parts)


def _line_mentions(text: str, label: str, value: str) -> bool:
    prefixes = (f"- {label}:", f"{label}:")
    return any(line.strip().startswith(prefixes) and value in line for line in str(text or "").splitlines())


def _state_has_text(state: dict[str, Any], text: str) -> bool:
    return text in json.dumps(state, ensure_ascii=False)


def _state_has_kind(state: dict[str, Any], kind: str) -> bool:
    dynamic = state.get("dynamic_state") if isinstance(state.get("dynamic_state"), dict) else {}
    return bool(dynamic.get(kind))


def _first_conversation_id(user_id: int, persona_id: int) -> int:
    with get_db() as db:
        row = db.execute(
            """
            SELECT id FROM conversations
            WHERE user_id = ? AND persona_id = ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (user_id, persona_id),
        ).fetchone()
    if not row:
        raise ValueError("eval conversation not found")
    return int(row["id"])


def _excerpt(text: str, needle: str, radius: int = 160) -> str:
    text = str(text or "")
    idx = text.find(needle)
    if idx < 0:
        return text[: radius * 2]
    start = max(0, idx - radius)
    end = min(len(text), idx + len(needle) + radius)
    return text[start:end]


def _states_avoid_topic(text: str, topic: str) -> bool:
    text = str(text or "")
    if topic not in text:
        return False
    negative_markers = ("不主动", "不会主动", "别主动", "避免", "不提", "少提", "不聊", "别聊", "不要聊", "不推荐聊")
    return any(marker in text for marker in negative_markers)


def _bad_address(text: str) -> bool:
    text = str(text or "")
    if "主人" not in text:
        return False
    allowed = (
        "不叫你主人",
        "不会叫你主人",
        "不要叫你主人",
        "不能叫你主人",
        "不称呼你为主人",
        "不会称呼你为主人",
        "不该叫你主人",
    )
    return not any(phrase in text for phrase in allowed)
