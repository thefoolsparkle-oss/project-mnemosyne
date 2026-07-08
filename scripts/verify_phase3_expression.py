from __future__ import annotations

import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.database as database


def seed_chat() -> tuple[int, int, int]:
    ts = database.now_ts()
    with database.get_db() as db:
        user_id = int(
            db.execute(
                "INSERT INTO users (username, password_hash, created_at, updated_at) VALUES ('phase3', 'x', ?, ?)",
                (ts, ts),
            ).lastrowid
        )
        db.execute(
            "INSERT INTO user_profiles (user_id, nickname, created_at, updated_at) VALUES (?, '表达测试用户', ?, ?)",
            (user_id, ts, ts),
        )
        persona_id = int(
            db.execute(
                """
                INSERT INTO personas (user_id, name, summary, prompt, relationship, speaking_style, created_at, updated_at)
                VALUES (?, '栖夏', '安静而可靠', '自然聊天', '像朋友一样', '简短自然', ?, ?)
                """,
                (user_id, ts, ts),
            ).lastrowid
        )
        conversation_id = int(
            db.execute(
                "INSERT INTO conversations (user_id, persona_id, title, created_at, updated_at) VALUES (?, ?, '表达验证', ?, ?)",
                (user_id, persona_id, ts, ts),
            ).lastrowid
        )
    return user_id, persona_id, conversation_id


def disable_chat_side_effects(chat) -> None:
    chat.extract_and_store = lambda **kwargs: []
    chat.update_interaction_insight = lambda *args, **kwargs: {}
    chat.maybe_queue_revision_from_feedback = lambda *args, **kwargs: None
    chat.recall_memories = lambda *args, **kwargs: []
    chat.recall_layered_memory = lambda *args, **kwargs: []
    chat.should_use_semantic_recall = lambda: False
    chat.insight_prompt = lambda *args, **kwargs: ""
    chat.conversation_summary_prompt = lambda *args, **kwargs: ""
    chat.state_prompt = lambda *args, **kwargs: ""
    chat.summary_prompt = lambda *args, **kwargs: ""
    chat.layered_memory_prompt = lambda *args, **kwargs: ""
    chat.semantic_memory_prompt = lambda *args, **kwargs: ""
    chat.discovery_prompt = lambda *args, **kwargs: ""
    chat.policy_snapshot = lambda: {}
    chat.should_refresh_summary = lambda count: False


def verify_protocol(chat, server, user_id: int, persona_id: int, conversation_id: int) -> None:
    assets = server.expression_assets({"id": user_id})["assets"]
    assert {item["label"] for item in assets} >= {"微笑", "轻笑", "担心", "轻声", "停顿", "点头"}
    assert chat.STRUCTURED_EXPRESSION_LABELS["mood"] >= {"微笑", "轻笑", "担心"}
    assert all(item["asset_kind"] == "text_badge" for item in assets)
    assert all("media_url" in item and "thumbnail_url" in item and "alt_text" in item for item in assets)
    assert all("admin_note" not in item for item in assets)
    smile_asset = next(item for item in assets if item["label"] == "微笑")
    concern_asset = next(item for item in assets if item["label"] == "担心")
    assert smile_asset["prompt_hint"]
    assert smile_asset["group"] == "warmth"
    assert smile_asset["risk_level"] == "low"
    assert "笑一下" in smile_asset["aliases"]
    assert smile_asset["cooldown_turns"] == 4
    assert concern_asset["intensity"] == 2
    assert concern_asset["risk_level"] == "medium"
    assert concern_asset["cooldown_turns"] == 8
    assert "[[expression:mood:担心]]" in chat.CHAT_RENDERING_RULES
    assert concern_asset["prompt_hint"] in chat.CHAT_RENDERING_RULES
    assert "风险：medium" in chat.CHAT_RENDERING_RULES
    assert "强度：2" in chat.CHAT_RENDERING_RULES
    assert "冷却：8轮" in chat.CHAT_RENDERING_RULES
    assert "风险为 medium 的标签" in chat.CHAT_RENDERING_RULES
    disabled_asset = server.admin_update_expression_asset(
        "mood",
        "担心",
        server.ExpressionAssetUpdateRequest(enabled=False, admin_note="phase3 test disable"),
        {"id": user_id, "role": "admin"},
    )["asset"]
    assert disabled_asset["enabled"] is False
    assert disabled_asset["history"][0]["event_kind"] == "enabled"
    assert disabled_asset["history"][0]["after"]["enabled"] == 0
    public_assets = server.expression_assets({"id": user_id})["assets"]
    assert "担心" not in {item["label"] for item in public_assets}
    admin_assets = server.admin_expression_assets({"id": user_id, "role": "admin"})["assets"]
    admin_concern = next(item for item in admin_assets if item["label"] == "担心")
    assert admin_concern["enabled"] is False
    assert admin_concern["admin_note"] == "phase3 test disable"
    assert "[[expression:mood:担心]]" not in chat.chat_rendering_rules_prompt()
    disabled_direct = chat._extract_reply_presentation("我有点担心。[[expression:mood:担心]]")
    assert disabled_direct["content"] == "我有点担心。"
    assert disabled_direct["expressions"] == []
    restored_asset = server.admin_update_expression_asset(
        "mood",
        "担心",
        server.ExpressionAssetUpdateRequest(enabled=True, admin_note="phase3 test restore"),
        {"id": user_id, "role": "admin"},
    )["asset"]
    assert restored_asset["enabled"] is True
    assert restored_asset["admin_note"] == "phase3 test restore"
    assert "[[expression:mood:担心]]" in chat.chat_rendering_rules_prompt()
    assert "风险：medium" in chat.chat_rendering_rules_prompt()
    cooldown_asset = server.admin_update_expression_asset(
        "gesture",
        "点头",
        server.ExpressionAssetUpdateRequest(enabled=True, cooldown_turns=1, admin_note="phase3 cooldown override"),
        {"id": user_id, "role": "admin"},
    )["asset"]
    assert cooldown_asset["cooldown_turns"] == 1
    assert cooldown_asset["cooldown_turns_override"] == 1
    assert "冷却：1轮" in chat.chat_rendering_rules_prompt()
    try:
        server.admin_update_expression_asset(
            "mood",
            "不存在",
            server.ExpressionAssetUpdateRequest(enabled=False, admin_note="phase3 unknown"),
            {"id": user_id, "role": "admin"},
        )
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 404
    else:
        assert False, "unknown expression asset should return 404"
    archived_asset = server.admin_update_expression_asset(
        "tone",
        "停顿",
        server.ExpressionAssetUpdateRequest(
            enabled=True,
            lifecycle_status="archived",
            admin_note="phase3 archive",
        ),
        {"id": user_id, "role": "admin"},
    )["asset"]
    assert archived_asset["lifecycle_status"] == "archived"
    assert archived_asset["enabled"] is False
    assert archived_asset["history"][0]["event_kind"] == "lifecycle"
    assert archived_asset["history"][0]["after"]["lifecycle_status"] == "archived"
    public_after_archive = server.expression_assets({"id": user_id})["assets"]
    assert "停顿" not in {item["label"] for item in public_after_archive}
    assert "[[expression:tone:停顿]]" not in chat.chat_rendering_rules_prompt()
    archived_direct = chat._extract_reply_presentation("先停一下。[[expression:tone:停顿]]")
    assert archived_direct["content"] == "先停一下。"
    assert archived_direct["expressions"] == []
    restored_lifecycle = server.admin_update_expression_asset(
        "tone",
        "停顿",
        server.ExpressionAssetUpdateRequest(
            enabled=True,
            lifecycle_status="active",
            admin_note="phase3 unarchive",
        ),
        {"id": user_id, "role": "admin"},
    )["asset"]
    assert restored_lifecycle["enabled"] is True
    assert restored_lifecycle["lifecycle_status"] == "active"
    paused_asset = server.admin_update_expression_asset(
        "gesture",
        "\u70b9\u5934",
        server.ExpressionAssetUpdateRequest(
            enabled=True,
            lifecycle_status="paused",
            admin_note="phase3 pause",
        ),
        {"id": user_id, "role": "admin"},
    )["asset"]
    assert paused_asset["lifecycle_status"] == "paused"
    assert paused_asset["enabled"] is False
    assert paused_asset["history"][0]["event_kind"] == "lifecycle"
    assert "\u70b9\u5934" not in {item["label"] for item in server.expression_assets({"id": user_id})["assets"]}
    assert "\u70b9\u5934" not in chat.active_expression_labels().get("gesture", set())
    resumed_asset = server.admin_update_expression_asset(
        "gesture",
        "\u70b9\u5934",
        server.ExpressionAssetUpdateRequest(
            enabled=True,
            lifecycle_status="active",
            admin_note="phase3 resume",
        ),
        {"id": user_id, "role": "admin"},
    )["asset"]
    assert resumed_asset["lifecycle_status"] == "active"
    assert resumed_asset["enabled"] is True
    assert "\u70b9\u5934" in chat.active_expression_labels().get("gesture", set())
    assert "[[expression:tone:停顿]]" in chat.chat_rendering_rules_prompt()
    media_asset = server.admin_update_expression_asset(
        "mood",
        "微笑",
        server.ExpressionAssetUpdateRequest(
            enabled=True,
            asset_kind="image",
            media_url="/uploads/expression/smile.png",
            thumbnail_url="/uploads/expression/smile-thumb.png",
            alt_text="微笑贴图",
            admin_note="phase3 media",
        ),
        {"id": user_id, "role": "admin"},
    )["asset"]
    assert media_asset["asset_kind"] == "image"
    assert media_asset["media_url"] == "/uploads/expression/smile.png"
    assert media_asset["thumbnail_url"] == "/uploads/expression/smile-thumb.png"
    assert media_asset["alt_text"] == "微笑贴图"
    assert media_asset["history"][0]["event_kind"] == "media"
    assert media_asset["history"][0]["after"]["media_url"] == "/uploads/expression/smile.png"
    public_media_asset = next(item for item in server.expression_assets({"id": user_id})["assets"] if item["label"] == "微笑")
    assert public_media_asset["asset_kind"] == "image"
    assert public_media_asset["media_url"]
    restored_media_asset = server.admin_update_expression_asset(
        "mood",
        "微笑",
        server.ExpressionAssetUpdateRequest(
            enabled=True,
            asset_kind="text_badge",
            media_url="",
            thumbnail_url="",
            alt_text="",
            admin_note="phase3 media restore",
        ),
        {"id": user_id, "role": "admin"},
    )["asset"]
    assert restored_media_asset["asset_kind"] == "text_badge"
    assert restored_media_asset["media_url"] == ""
    risk_policy = {"suppress_all": False, "recent_labels": ["微笑"]}
    assert chat._apply_expression_policy(
        [{"type": "mood", "label": "担心", "source_text": "[[expression:mood:担心]]"}],
        risk_policy,
    ) == []
    assert chat._expression_scene_context("我今天有点累，陪我一下")["expression_scene"] == "support_needed"
    assert chat._expression_scene_context("哈哈这个好好玩")["expression_scene"] == "playful"
    assert chat._expression_scene_context("好。")["expression_scene"] == "ordinary"
    assert chat._apply_expression_policy(
        [{"type": "tone", "label": "轻声", "source_text": "[[expression:tone:轻声]]"}],
        {"suppress_all": False, "expression_scene": "ordinary", "expression_allowed_groups": ["warmth", "acknowledgement"]},
    ) == []
    assert chat._apply_expression_policy(
        [{"type": "mood", "label": "担心", "source_text": "[[expression:mood:担心]]"}],
        {
            "suppress_all": False,
            "recent_labels": ["微笑"],
            "expression_scene": "support_needed",
            "expression_allowed_groups": ["support", "care", "warmth", "acknowledgement"],
        },
    )[0]["label"] == "担心"
    assert chat._persona_expression_style_context(
        {"summary": "安静而可靠", "relationship": "像朋友一样", "speaking_style": "简短自然"}
    )["expression_persona_style"] == "restrained"
    style_update = server.admin_update_persona_expression_style(
        server.PersonaExpressionStyleUpdateRequest(
            persona_id=persona_id,
            style="warm",
            preferred_groups=["care", "support"],
            avoid_labels=["轻声"],
            admin_note="phase3 style override",
        ),
        {"id": user_id, "role": "admin"},
        target_user_id=user_id,
    )["style_setting"]
    assert style_update["explicit"] is True
    assert style_update["style"] == "warm"
    assert style_update["preferred_groups"] == ["care", "support"]
    assert style_update["avoid_labels"] == ["轻声"]
    configured_style = chat._persona_expression_style_context(
        {"summary": "安静而可靠", "relationship": "像朋友一样", "speaking_style": "简短自然"},
        user_id=user_id,
        persona_id=persona_id,
    )
    assert configured_style["expression_persona_style"] == "warm"
    assert configured_style["expression_persona_style_source"] == "admin"
    assert configured_style["expression_persona_avoid_labels"] == ["轻声"]
    assert chat._expression_selection_agent(
        "我今天有点累，陪我一下",
        "我在，先慢慢来。",
        {
            "suppress_all": False,
            "expression_scene": "support_needed",
            "expression_allowed_groups": ["support", "care", "warmth", "acknowledgement"],
            "expression_persona_avoid_labels": ["轻声"],
        },
    )[0]["label"] == "担心"
    assert chat._apply_expression_policy(
        [{"type": "mood", "label": "轻笑", "source_text": "[[expression:mood:轻笑]]"}],
        {
            "suppress_all": False,
            "expression_scene": "playful",
            "expression_allowed_groups": ["warmth", "acknowledgement"],
            "expression_persona_avoid_labels": ["轻笑"],
        },
    ) == []
    usage_with_style = server.admin_expression_usage(
        {"id": user_id, "role": "admin"},
        target_user_id=user_id,
        persona_id=persona_id,
        limit=1,
        usage_limit=8,
    )
    assert usage_with_style["style_setting"]["style"] == "warm"
    assert usage_with_style["style_setting"]["admin_note"] == "phase3 style override"
    assert usage_with_style["style_history"][0]["style"] == "warm"
    assert usage_with_style["style_history"][0]["avoid_labels"] == ["轻声"]
    server.admin_update_persona_expression_style(
        server.PersonaExpressionStyleUpdateRequest(
            persona_id=persona_id,
            style="",
            preferred_groups=[],
            avoid_labels=[],
            admin_note="phase3 style reset",
        ),
        {"id": user_id, "role": "admin"},
        target_user_id=user_id,
    )
    support_agent = chat._expression_selection_agent(
        "我今天有点累，陪我一下",
        "我在，先慢慢来。",
        {
            "suppress_all": False,
            "expression_scene": "support_needed",
            "expression_allowed_groups": ["support", "care", "warmth", "acknowledgement"],
        },
    )
    assert support_agent[0]["label"] == "轻声"
    assert support_agent[0]["source_text"] == "selection_agent:support_needed"
    playful_agent = chat._expression_selection_agent(
        "哈哈这个好好玩",
        "确实有点好笑。",
        {
            "suppress_all": False,
            "expression_scene": "playful",
            "expression_allowed_groups": ["warmth", "acknowledgement"],
            "expression_persona_avoid_labels": ["轻笑"],
        },
    )
    assert playful_agent[0]["label"] == "微笑"
    assert chat._expression_selection_agent(
        "哈哈这个好好玩",
        "确实有点好笑。",
        {
            "suppress_all": False,
            "preference_churn": True,
            "expression_scene": "playful",
            "expression_allowed_groups": ["warmth", "acknowledgement"],
        },
    ) == []
    churn_support_agent = chat._expression_selection_agent(
        "我今天有点累，陪我一下",
        "我在，先慢慢来。",
        {
            "suppress_all": False,
            "preference_churn": True,
            "expression_scene": "support_needed",
            "expression_allowed_groups": ["support", "care", "warmth", "acknowledgement"],
        },
    )
    assert churn_support_agent[0]["label"] == "轻声"
    assert chat._expression_selection_agent(
        "我们继续讨论一下今天要做的事情",
        "好，我们慢慢拆。",
        {"suppress_all": False, "expression_scene": "ordinary", "expression_allowed_groups": ["warmth", "acknowledgement"]},
    ) == []
    assert chat._expression_selection_agent(
        "好",
        "嗯，我在。",
        {"suppress_all": True, "expression_scene": "ordinary", "expression_allowed_groups": ["warmth", "acknowledgement"]},
    ) == []
    cooldown_policy = {
        "suppress_all": False,
        "recent_labels": ["点头"],
        "recent_label_distances": {"点头": 0},
    }
    assert chat._apply_expression_policy(
        [{"type": "gesture", "label": "点头", "source_text": "[[expression:gesture:点头]]"}],
        cooldown_policy,
    ) == []
    cooldown_policy["recent_label_distances"] = {"点头": 1}
    assert chat._apply_expression_policy(
        [{"type": "gesture", "label": "点头", "source_text": "[[expression:gesture:点头]]"}],
        cooldown_policy,
    )[0]["label"] == "点头"
    assert chat._apply_expression_policy(
        [{"type": "gesture", "label": "点头", "source_text": "[[expression:gesture:点头]]"}],
        risk_policy,
    )[0]["label"] == "点头"
    server.admin_update_expression_asset(
        "gesture",
        "点头",
        server.ExpressionAssetUpdateRequest(enabled=True, cooldown_turns=3, admin_note="phase3 cooldown restore"),
        {"id": user_id, "role": "admin"},
    )
    server.admin_update_expression_asset(
        "mood",
        "轻笑",
        server.ExpressionAssetUpdateRequest(enabled=False, admin_note="phase3 fallback disable"),
        {"id": user_id, "role": "admin"},
    )
    disabled_fallback = chat._extract_reply_presentation("（轻笑）还在呢。")
    assert disabled_fallback["content"] == "还在呢。"
    assert disabled_fallback["expressions"] == []
    disabled_alias_fallback = chat._extract_reply_presentation("（偷笑）还在呢。")
    assert disabled_alias_fallback["content"] == "还在呢。"
    assert disabled_alias_fallback["expressions"] == []
    server.admin_update_expression_asset(
        "mood",
        "轻笑",
        server.ExpressionAssetUpdateRequest(enabled=True, admin_note="phase3 fallback restore"),
        {"id": user_id, "role": "admin"},
    )

    direct = chat._extract_reply_presentation("先缓一缓。[[expression:mood:微笑]]")
    assert direct["content"] == "先缓一缓。"
    assert direct["expressions"] == [
        {"type": "mood", "label": "微笑", "source_text": "[[expression:mood:微笑]]"}
    ]

    rejected = chat._extract_reply_presentation("知道了。[[expression:mood:大哭]]")
    assert rejected["content"] == "知道了。"
    assert rejected["expressions"] == []
    rejected_fallback = chat._extract_reply_presentation("（眨眼）知道了。")
    assert rejected_fallback["content"] == "知道了。"
    assert rejected_fallback["expressions"] == []

    fallback = chat._extract_reply_presentation("（轻笑）还在呢。")
    assert fallback["content"] == "还在呢。"
    assert fallback["expressions"][0]["label"] == "轻笑"
    alias_fallback = chat._extract_reply_presentation("（轻轻点头）知道了。")
    assert alias_fallback["content"] == "知道了。"
    assert alias_fallback["expressions"][0]["type"] == "gesture"
    assert alias_fallback["expressions"][0]["label"] == "点头"

    captured = {}

    def model_reply(messages, task="chat"):
        captured["messages"] = messages
        return "先坐一会儿。[[expression:tone:轻声]]"

    chat.call_llm_api = model_reply
    result = chat.db_chat(
        user_id,
        persona_id,
        "我今天有点累。",
        conversation_id=conversation_id,
        client_message_id="phase3-expression",
    )
    assert result["reply"] == "先坐一会儿。"
    assert result["expressions"] == [
        {"type": "tone", "label": "轻声", "source_text": "[[expression:tone:轻声]]"}
    ]
    prompt = "\n".join(str(item.get("content") or "") for item in captured["messages"])
    assert "[[expression:mood:微笑]]" in prompt
    assert "至多一个程序标签" in prompt
    assert "轻表达资源目录" in prompt
    assert "support_needed" in prompt
    assert "restrained" in prompt
    assert "近期没有已展示的提示" in prompt

    loaded = server.conversation_messages(conversation_id, {"id": user_id})["messages"]
    assistant = loaded[-1]
    assert assistant["role"] == "assistant" and assistant["content"] == "先坐一会儿。"
    assert assistant["expressions"][0]["expression_type"] == "tone"
    assert assistant["expressions"][0]["label"] == "轻声"
    server.admin_update_expression_asset(
        "tone",
        "轻声",
        server.ExpressionAssetUpdateRequest(enabled=False, admin_note="phase3 hide history"),
        {"id": user_id, "role": "admin"},
    )
    hidden_loaded = server.conversation_messages(conversation_id, {"id": user_id})["messages"]
    assert hidden_loaded[-1]["content"] == "先坐一会儿。"
    assert hidden_loaded[-1]["expressions"] == []
    usage = server.admin_expression_usage(
        {"id": user_id, "role": "admin"},
        target_user_id=user_id,
        persona_id=persona_id,
        limit=1,
        usage_limit=8,
    )
    assert len(usage["recent"]) == 1
    assert usage["counted"] == usage["summary"]["window"]
    assert usage["summary"]["window"] >= 1
    assert usage["summary"]["single"] >= 1
    assert usage["summary"]["disabled_asset"] >= 1
    assert any(item["kind"] == "disabled_asset_history" for item in usage["insights"])
    assert any(
        item["kind"] == "disabled_asset_history" and item["tag"] == "tone:轻声"
        for item in usage["review_items"]
    )
    hidden_usage_item = next(item for item in usage["recent"] if item["label"] == "轻声")
    assert hidden_usage_item["asset_enabled"] is False
    assert hidden_usage_item["asset_known"] is True
    assert hidden_usage_item["display_text"] == "轻声"
    assert hidden_usage_item["risk_level"] == "low"
    assert hidden_usage_item["group"] == "support"
    assert hidden_usage_item["cooldown_turns"] == 4
    assert hidden_usage_item["source_kind"] == "model"
    assert usage["summary"]["source_model"] >= 1
    hidden_count = next(item for item in usage["counts"] if item["tag"] == "tone:轻声")
    assert hidden_count["asset_enabled"] is False
    assert hidden_count["display_text"] == "轻声"
    assert hidden_count["cooldown_turns"] == 4
    assert hidden_count["source_counts"]["model"] >= 1
    server.admin_update_expression_asset(
        "tone",
        "轻声",
        server.ExpressionAssetUpdateRequest(enabled=True, admin_note="phase3 restore history"),
        {"id": user_id, "role": "admin"},
    )
    captured.clear()
    chat.call_llm_api = lambda messages, task="chat": (
        captured.setdefault("messages", messages) and "我还在。[[expression:mood:微笑]]"
    )
    cooldown = chat.db_chat(
        user_id,
        persona_id,
        "那你就陪我一会儿。",
        conversation_id=conversation_id,
        client_message_id="phase3-expression-cooldown",
    )
    assert cooldown["reply"] == "我还在。"
    assert cooldown["expressions"] == []
    prompt = "\n".join(str(item.get("content") or "") for item in captured["messages"])
    assert "上一条回复刚显示过非语言提示" in prompt

    captured.clear()
    chat.call_llm_api = lambda messages, task="chat": (
        captured.setdefault("messages", messages) and "嗯。[[expression:tone:轻声]]"
    )
    repeated = chat.db_chat(
        user_id,
        persona_id,
        "嗯。",
        conversation_id=conversation_id,
        client_message_id="phase3-expression-repeat",
    )
    assert repeated["expressions"] == []
    prompt = "\n".join(str(item.get("content") or "") for item in captured["messages"])
    assert "近期已经展示过这些标签：轻声" in prompt

    chat.call_llm_api = lambda messages, task="chat": "好。[[expression:mood:微笑]]"
    distinct = chat.db_chat(
        user_id,
        persona_id,
        "好。",
        conversation_id=conversation_id,
        client_message_id="phase3-expression-distinct",
    )
    assert distinct["expressions"][0]["label"] == "微笑"

    ts = database.now_ts()
    with database.get_db() as db:
        for index in range(5):
            message_id = int(
                db.execute(
                    """
                    INSERT INTO messages (conversation_id, user_id, persona_id, role, content, created_at)
                    VALUES (?, ?, ?, 'assistant', ?, ?)
                    """,
                    (conversation_id, user_id, persona_id, f"selector seeded {index}", ts + index),
                ).lastrowid
            )
            db.execute(
                """
                INSERT INTO message_expressions (
                    message_id, user_id, persona_id, conversation_id,
                    expression_type, label, source_text, created_at
                )
                VALUES (?, ?, ?, ?, 'gesture', '点头', 'selection_agent:ordinary', ?)
                """,
                (message_id, user_id, persona_id, conversation_id, ts + index),
            )
    selector_usage = server.admin_expression_usage(
        {"id": user_id, "role": "admin"},
        target_user_id=user_id,
        persona_id=persona_id,
        limit=8,
        usage_limit=8,
    )
    assert selector_usage["summary"]["source_selection_agent"] >= 5
    assert selector_usage["summary"]["scene_ordinary"] >= 5
    selector_count = next(item for item in selector_usage["counts"] if item["tag"] == "gesture:点头")
    assert selector_count["source_counts"]["selection_agent"] >= 5
    assert selector_count["scene_counts"]["ordinary"] >= 5
    assert selector_usage["recent"][0]["scene_kind"] == "ordinary"
    assert any(item["kind"] == "selection_agent_label" for item in selector_usage["review_items"])
    assert selector_usage["style_suggestions"][0]["style"] == "restrained"
    assert "acknowledgement" in selector_usage["style_suggestions"][0]["preferred_groups"]
    scene_pressure_policy = chat._recent_expression_policy(user_id, persona_id, conversation_id)
    assert scene_pressure_policy["recent_expression_scene_counts"]["ordinary"] >= 5
    assert "ordinary" in scene_pressure_policy["expression_congested_scenes"]
    scene_pressure_policy["suppress_all"] = False
    scene_pressure_policy.update(chat._expression_scene_context("嗯。"))
    assert "Scene rhythm feedback" in chat._expression_policy_prompt(scene_pressure_policy)
    assert chat._expression_selection_agent("嗯。", "嗯，我在。", scene_pressure_policy) == []

    assert chat._expression_preference_intent("以后别发表情了") == "disable"
    assert chat._expression_preference_intent("表情少一点") == "subtle"
    assert chat._expression_preference_intent("现在可以发表情了") == "enable"

    listed = server.personas({"id": user_id})["personas"][0]
    assert listed["expression_preference"] == {"enabled": True, "mode": "normal", "explicit": False}
    profile_subtle = server.update_persona_expression_preference(
        persona_id,
        server.ExpressionPreferenceUpdateRequest(mode="subtle"),
        {"id": user_id},
    )
    assert profile_subtle["expression_preference"]["enabled"] is True
    assert profile_subtle["expression_preference"]["mode"] == "subtle"
    assert profile_subtle["expression_preference"]["explicit"] is True
    profile_usage = server.admin_expression_usage(
        {"id": user_id, "role": "admin"},
        target_user_id=user_id,
        persona_id=persona_id,
        limit=4,
        usage_limit=4,
    )
    assert profile_usage["preference_history"][0]["mode"] == "subtle"
    assert profile_usage["preference_history"][0]["source"] == "profile_setting"
    policy = chat._recent_expression_policy(user_id, persona_id, conversation_id)
    assert policy["expression_preference"]["mode"] == "subtle"
    assert policy["subtle_mode"] is True
    assert policy["suppress_all"] is True
    assert "用户选择了克制轻表达" in chat._expression_policy_prompt(policy)
    with database.get_db() as db:
        db.execute(
            "DELETE FROM message_expressions WHERE user_id = ? AND persona_id = ? AND conversation_id = ?",
            (user_id, persona_id, conversation_id),
        )
    policy = chat._recent_expression_policy(user_id, persona_id, conversation_id)
    assert policy["subtle_mode"] is True
    assert policy["suppress_all"] is False
    policy.update(chat._expression_scene_context("嗯。"))
    subtle_prompt = chat._expression_policy_prompt(policy)
    assert "ordinary" in subtle_prompt
    assert "普通闲聊不要添加" in subtle_prompt
    profile_disabled = server.update_persona_expression_preference(
        persona_id,
        server.ExpressionPreferenceUpdateRequest(mode="off"),
        {"id": user_id},
    )
    assert profile_disabled["expression_preference"]["enabled"] is False
    assert profile_disabled["expression_preference"]["mode"] == "off"
    assert profile_disabled["expression_preference"]["explicit"] is True
    detail = server.persona_detail(persona_id, {"id": user_id})["persona"]
    assert detail["expression_preference"]["enabled"] is False
    assert detail["expression_preference"]["mode"] == "off"
    with database.get_db() as db:
        row = db.execute(
            "SELECT enabled, mode, source_message_id FROM expression_preferences WHERE user_id = ? AND persona_id = ?",
            (user_id, persona_id),
        ).fetchone()
    assert int(row["enabled"]) == 0
    assert row["mode"] == "off"
    assert row["source_message_id"] is None
    policy = chat._recent_expression_policy(user_id, persona_id, conversation_id)
    assert int(policy["expression_preference"]["enabled"]) == 0
    profile_enabled = server.update_persona_expression_preference(
        persona_id,
        server.ExpressionPreferenceUpdateRequest(mode="normal"),
        {"id": user_id},
    )
    assert profile_enabled["expression_preference"]["enabled"] is True
    assert profile_enabled["expression_preference"]["mode"] == "normal"
    listed = server.personas({"id": user_id})["personas"][0]
    assert listed["expression_preference"] == {"enabled": True, "mode": "normal", "explicit": True}
    policy = chat._recent_expression_policy(user_id, persona_id, conversation_id)
    assert int(policy["expression_preference"]["enabled"]) == 1

    smile_label = "微笑"
    nod_label = "点头"

    captured.clear()
    chat.call_llm_api = lambda messages, task="chat": (
        captured.setdefault("messages", messages) and f"好。[[expression:mood:{smile_label}]]"
    )
    disabled = chat.db_chat(
        user_id,
        persona_id,
        "以后别发表情了。",
        conversation_id=conversation_id,
        client_message_id="phase3-expression-disable",
    )
    assert disabled["reply"] == "好。"
    assert disabled["expressions"] == []
    prompt = "\n".join(str(item.get("content") or "") for item in captured["messages"])
    assert "explicitly turned off expression labels" in prompt
    with database.get_db() as db:
        row = db.execute(
            "SELECT enabled, source_message_id FROM expression_preferences WHERE user_id = ? AND persona_id = ?",
            (user_id, persona_id),
        ).fetchone()
    assert int(row["enabled"]) == 0
    assert int(row["source_message_id"]) == disabled["user_message_id"]
    chat_pref_usage = server.admin_expression_usage(
        {"id": user_id, "role": "admin"},
        target_user_id=user_id,
        persona_id=persona_id,
        limit=4,
        usage_limit=4,
    )
    assert chat_pref_usage["preference_history"][0]["mode"] == "off"
    assert chat_pref_usage["preference_history"][0]["source"] == "chat_intent"
    assert int(chat_pref_usage["preference_history"][0]["source_message_id"]) == int(disabled["user_message_id"])

    captured.clear()
    chat.call_llm_api = lambda messages, task="chat": (
        captured.setdefault("messages", messages) and f"嗯。[[expression:mood:{smile_label}]]"
    )
    subtle = chat.db_chat(
        user_id,
        persona_id,
        "表情少一点。",
        conversation_id=conversation_id,
        client_message_id="phase3-expression-subtle",
    )
    assert subtle["reply"] == "嗯。"
    assert subtle["expressions"][0]["label"] == smile_label
    prompt = "\n".join(str(item.get("content") or "") for item in captured["messages"])
    assert "用户选择了克制轻表达" in prompt
    with database.get_db() as db:
        row = db.execute(
            "SELECT enabled, mode, source_message_id FROM expression_preferences WHERE user_id = ? AND persona_id = ?",
            (user_id, persona_id),
        ).fetchone()
    assert int(row["enabled"]) == 1
    assert row["mode"] == "subtle"
    assert int(row["source_message_id"]) == subtle["user_message_id"]

    captured.clear()
    chat.call_llm_api = lambda messages, task="chat": (
        captured.setdefault("messages", messages) and f"还在。[[expression:mood:{smile_label}]]"
    )
    subtle_next = chat.db_chat(
        user_id,
        persona_id,
        "还在吗？",
        conversation_id=conversation_id,
        client_message_id="phase3-expression-disabled-next",
    )
    assert subtle_next["reply"] == "还在。"
    assert subtle_next["expressions"] == []
    prompt = "\n".join(str(item.get("content") or "") for item in captured["messages"])
    assert "用户选择了克制轻表达" in prompt

    captured.clear()
    chat.call_llm_api = lambda messages, task="chat": (
        captured.setdefault("messages", messages) and f"可以。[[expression:gesture:{nod_label}]]"
    )
    enabled = chat.db_chat(
        user_id,
        persona_id,
        "现在可以发表情了。",
        conversation_id=conversation_id,
        client_message_id="phase3-expression-enable",
    )
    assert enabled["reply"] == "可以。"
    assert enabled["expressions"][0]["label"] == nod_label
    prompt = "\n".join(str(item.get("content") or "") for item in captured["messages"])
    assert "explicitly turned off expression labels" not in prompt
    with database.get_db() as db:
        row = db.execute(
            "SELECT enabled, mode, source_message_id FROM expression_preferences WHERE user_id = ? AND persona_id = ?",
            (user_id, persona_id),
        ).fetchone()
    assert int(row["enabled"]) == 1
    assert row["mode"] == "normal"
    assert int(row["source_message_id"]) == enabled["user_message_id"]
    restored_pref_usage = server.admin_expression_usage(
        {"id": user_id, "role": "admin"},
        target_user_id=user_id,
        persona_id=persona_id,
        limit=4,
        usage_limit=4,
    )
    assert restored_pref_usage["preference_history"][0]["mode"] == "normal"
    assert restored_pref_usage["preference_history"][0]["source"] == "chat_intent"
    assert restored_pref_usage["preference_feedback"]["churn"] is True
    assert restored_pref_usage["preference_feedback"]["change_count"] >= 2
    assert restored_pref_usage["feedback_signal"]["positive"] >= 1
    assert restored_pref_usage["feedback_signal"]["negative"] >= 2
    assert restored_pref_usage["feedback_signal"]["net"] <= -1
    assert "scene_counts" in restored_pref_usage["feedback_signal"]
    resource_feedback = restored_pref_usage["feedback_signal"]["resource_feedback"]
    assert resource_feedback
    assert resource_feedback[0]["negative"] >= 1
    assert resource_feedback[0]["evidence_count"] >= 1
    assert "scene_counts" in resource_feedback[0]
    assert any(item["kind"] == "preference_changes" for item in restored_pref_usage["insights"])
    assert any(item["kind"] == "expression_negative_feedback" for item in restored_pref_usage["insights"])
    assert any("运行时已收紧" in item["text"] for item in restored_pref_usage["insights"])
    churn_policy = chat._recent_expression_policy(user_id, persona_id, conversation_id)
    assert churn_policy["preference_churn"] is True
    assert churn_policy["preference_feedback"]["change_count"] >= 2
    churn_prompt = chat._expression_policy_prompt(churn_policy)
    assert "近期用户多次切换轻表达偏好" in churn_prompt
    churn_policy.update(chat._expression_scene_context("哈哈这个有点好笑"))
    assert chat._expression_selection_agent("哈哈这个有点好笑", "确实挺有意思。", churn_policy) == []

    ts = database.now_ts()
    with database.get_db() as db:
        for index in range(3):
            message_id = int(
                db.execute(
                    """
                    INSERT INTO messages (conversation_id, user_id, persona_id, role, content, created_at)
                    VALUES (?, ?, ?, 'assistant', ?, ?)
                    """,
                    (conversation_id, user_id, persona_id, f"批量审查样本 {index}", ts + index),
                ).lastrowid
            )
            db.execute(
                """
                INSERT INTO message_expressions (
                    message_id, user_id, persona_id, conversation_id,
                    expression_type, label, source_text, created_at
                )
                VALUES (?, ?, ?, ?, 'mood', '微笑', '[[expression:mood:微笑]]', ?)
                """,
                (message_id, user_id, persona_id, conversation_id, ts + index),
            )
    bulk = server.admin_apply_expression_review_cooldowns(
        server.ExpressionReviewBulkRequest(
            target_user_id=user_id,
            persona_id=persona_id,
            limit=4,
            usage_limit=4,
            admin_note="phase3 bulk review note",
        ),
        {"id": user_id, "role": "admin"},
    )
    assert bulk["applied_count"] >= 1
    applied_smile = next(item for item in bulk["applied"] if item["label"] == "微笑")
    assert applied_smile["previous_cooldown_turns"] == 4
    assert applied_smile["cooldown_turns"] == 6
    assert "phase3 bulk review note" in applied_smile["admin_note"]
    smile_asset = next(item for item in bulk["assets"] if item["expression_type"] == "mood" and item["label"] == "微笑")
    assert smile_asset["cooldown_turns"] == 6
    assert smile_asset["admin_note"] == applied_smile["admin_note"]
    assert smile_asset["history"][0]["event_kind"] == "cooldown"
    assert smile_asset["history"][0]["updated_by_user_id"] == user_id
    assert smile_asset["history"][0]["created_at"] >= ts
    assert "phase3 bulk review note" in smile_asset["history"][0]["admin_note"]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database.DB_PATH = Path(tmp) / "phase3.db"
        import app.db_chat as chat
        import app.server as server

        database.init_db()
        user_id, persona_id, conversation_id = seed_chat()
        disable_chat_side_effects(chat)
        verify_protocol(chat, server, user_id, persona_id, conversation_id)
    print("Phase 3 lightweight expression verification passed")


if __name__ == "__main__":
    main()
