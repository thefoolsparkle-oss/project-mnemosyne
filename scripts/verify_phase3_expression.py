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
    direct = chat._extract_reply_presentation("先缓一缓。[[expression:mood:微笑]]")
    assert direct["content"] == "先缓一缓。"
    assert direct["expressions"] == [
        {"type": "mood", "label": "微笑", "source_text": "[[expression:mood:微笑]]"}
    ]

    rejected = chat._extract_reply_presentation("知道了。[[expression:mood:大哭]]")
    assert rejected["content"] == "知道了。"
    assert rejected["expressions"] == []

    fallback = chat._extract_reply_presentation("（轻笑）还在呢。")
    assert fallback["content"] == "还在呢。"
    assert fallback["expressions"][0]["label"] == "轻笑"

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
    assert "近期没有已展示的提示" in prompt

    loaded = server.conversation_messages(conversation_id, {"id": user_id})["messages"]
    assistant = loaded[-1]
    assert assistant["role"] == "assistant" and assistant["content"] == "先坐一会儿。"
    assert assistant["expressions"][0]["expression_type"] == "tone"
    assert assistant["expressions"][0]["label"] == "轻声"

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
