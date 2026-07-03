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
    hidden_count = next(item for item in usage["counts"] if item["tag"] == "tone:轻声")
    assert hidden_count["asset_enabled"] is False
    assert hidden_count["display_text"] == "轻声"
    assert hidden_count["cooldown_turns"] == 4
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
