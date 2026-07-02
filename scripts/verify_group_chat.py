from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.database as database


def seed_personas() -> tuple[int, list[int]]:
    ts = database.now_ts()
    with database.get_db() as db:
        user_id = int(
            db.execute(
                "INSERT INTO users (username, password_hash, created_at, updated_at) VALUES ('group-user', 'x', ?, ?)",
                (ts, ts),
            ).lastrowid
        )
        db.execute(
            "INSERT INTO user_profiles (user_id, nickname, created_at, updated_at) VALUES (?, '群聊测试用户', ?, ?)",
            (user_id, ts, ts),
        )
        personas = []
        for name, summary, style in (
            ("栖夏", "安静、会接住情绪", "短句自然"),
            ("观澜", "冷静、擅长整理问题", "克制清楚"),
            ("小满", "轻快、会带一点吐槽", "活泼但不刷屏"),
        ):
            persona_id = int(
                db.execute(
                    """
                    INSERT INTO personas (
                        user_id, name, summary, prompt, relationship, speaking_style,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, '朋友', ?, ?, ?)
                    """,
                    (user_id, name, summary, f"你是{name}。", style, ts, ts),
                ).lastrowid
            )
            personas.append(persona_id)
    return user_id, personas


def verify_group_chat_flow() -> None:
    import app.group_chat as group_chat
    import app.server as server
    from app.llm_client import LLMProviderError

    user_id, persona_ids = seed_personas()
    calls: list[str] = []
    def fake_llm(messages, task="chat"):
        assert task == "group_chat"
        joined = "\n".join(str(item.get("content") or "") for item in messages)
        assert "Group members" in joined
        assert "Recent group messages" in joined
        assert "Turn policy" in joined
        calls.append("turn")
        return json.dumps(
            {
                "messages": [
                    {"persona_id": persona_ids[1], "content": "group reply 1[[expression:gesture:点头]]", "reason": "organize"},
                    {"persona_id": persona_ids[0], "content": "group reply 2[[expression:mood:微笑]]", "reason": "follow"},
                ]
            },
            ensure_ascii=False,
        )
        if "Group Router" in joined:
            raise AssertionError("group chat should use a single turn call")
            return json.dumps(
                {
                    "speakers": [
                        {"persona_id": persona_ids[1], "reason": "整理问题"},
                        {"persona_id": persona_ids[0], "reason": "接住情绪"},
                    ]
                },
                ensure_ascii=False,
            )
        generated_count += 1
        calls.append(f"reply-{generated_count}")
        if generated_count == 1:
            return "group reply 1[[expression:gesture:点头]]"
        return "group reply 2[[expression:mood:微笑]]"

    group_chat.call_llm_api = fake_llm

    assert group_chat._group_turn_policy_context("你们怎么看？")["multi_speaker_invited"] is True
    assert group_chat._group_turn_policy_context("我先安静一下")["silence_allowed"] is True

    group = server.create_group_conversation_endpoint(
        server.GroupConversationCreateRequest(title="小客厅", persona_ids=persona_ids),
        {"id": user_id},
    )["group_conversation"]
    assert group["title"] == "小客厅"
    assert len(group["members"]) == 3

    listed = server.group_conversations({"id": user_id})["group_conversations"]
    assert listed[0]["id"] == group["id"]
    assert listed[0]["message_count"] == 0

    result = server.group_chat_endpoint(
        server.GroupChatRequest(
            group_conversation_id=group["id"],
            message="一对一有点无聊，能不能你们一起聊？",
            client_message_id="group-chat-1",
        ),
        {"id": user_id},
    )
    assert calls == ["turn"]
    assert result["route"]["speakers"][0]["persona_id"] == persona_ids[1]
    assert len(result["replies"]) == 2
    assert result["replies"][0]["speaker_persona_id"] == persona_ids[1]
    assert result["replies"][1]["speaker_persona_id"] == persona_ids[0]
    assert result["replies"][0]["content"] == "group reply 1"
    assert result["replies"][1]["content"] == "group reply 2"
    assert result["replies"][0]["expressions"][0]["label"] == "点头"
    assert result["replies"][1]["expressions"][0]["label"] == "微笑"

    repeated = server.group_chat_endpoint(
        server.GroupChatRequest(
            group_conversation_id=group["id"],
            message=result["messages"][0]["content"],
            client_message_id="group-chat-1",
        ),
        {"id": user_id},
    )
    assert calls == ["turn"]
    assert repeated["user_message_id"] == result["user_message_id"]
    assert len(repeated["replies"]) == 2
    assert [item["id"] for item in repeated["messages"]] == [item["id"] for item in result["messages"]]

    messages = server.group_conversation_messages(group["id"], {"id": user_id})["messages"]
    assert [item["speaker_type"] for item in messages] == ["user", "persona", "persona"]
    assert messages[1]["speaker_name"] == "观澜"
    assert messages[2]["speaker_name"] == "栖夏"
    with database.get_db() as db:
        relation_rows = db.execute(
            """
            SELECT persona_id, other_persona_id, affinity
            FROM group_member_relations
            WHERE group_conversation_id = ?
            ORDER BY persona_id, other_persona_id
            """,
            (group["id"],),
        ).fetchall()
    assert len(relation_rows) == 2
    assert {int(row["persona_id"]) for row in relation_rows} == {persona_ids[0], persona_ids[1]}
    assert all(int(row["affinity"] or 0) >= 1 for row in relation_rows)
    refreshed_group = server.group_conversations({"id": user_id})["group_conversations"][0]
    related_members = [
        member for member in refreshed_group["members"]
        if int(member["persona_id"]) in {persona_ids[0], persona_ids[1]}
    ]
    assert all(member["group_relations"] for member in related_members)
    assert {
        int(relation["other_persona_id"])
        for member in related_members
        for relation in member["group_relations"]
    } == {persona_ids[0], persona_ids[1]}
    assert {relation["status"] for member in related_members for relation in member["group_relations"]} == {"new"}
    assert messages[1]["expressions"][0]["expression_type"] == "gesture"
    assert messages[1]["expressions"][0]["label"] == "点头"
    assert messages[2]["expressions"][0]["expression_type"] == "mood"
    assert messages[2]["expressions"][0]["label"] == "微笑"
    server.admin_update_expression_asset(
        "gesture",
        "点头",
        server.ExpressionAssetUpdateRequest(enabled=False, admin_note="group hide history"),
        {"id": user_id, "role": "admin"},
    )
    hidden_messages = server.group_conversation_messages(group["id"], {"id": user_id})["messages"]
    assert hidden_messages[1]["content"] == "group reply 1"
    assert hidden_messages[1]["expressions"] == []
    expression_usage_hidden = server.admin_expression_usage(
        {"id": user_id, "role": "admin"},
        target_user_id=user_id,
        persona_id=persona_ids[1],
        limit=1,
        usage_limit=8,
    )
    assert len(expression_usage_hidden["recent"]) == 1
    assert expression_usage_hidden["counted"] == expression_usage_hidden["summary"]["window"]
    assert expression_usage_hidden["summary"]["group"] >= 1
    assert expression_usage_hidden["summary"]["disabled_asset"] >= 1
    assert any(item["kind"] == "disabled_asset_history" for item in expression_usage_hidden["insights"])
    hidden_usage_item = next(item for item in expression_usage_hidden["recent"] if item["label"] == "点头")
    assert hidden_usage_item["asset_enabled"] is False
    assert hidden_usage_item["asset_known"] is True
    assert hidden_usage_item["display_text"] == "点头"
    assert hidden_usage_item["group"] == "acknowledgement"
    assert hidden_usage_item["cooldown_turns"] == 3
    server.admin_update_expression_asset(
        "gesture",
        "点头",
        server.ExpressionAssetUpdateRequest(enabled=True, admin_note="group restore history"),
        {"id": user_id, "role": "admin"},
    )
    expression_usage = server.admin_expression_usage(
        {"id": user_id, "role": "admin"},
        target_user_id=user_id,
        persona_id=persona_ids[1],
        limit=1,
        usage_limit=8,
    )
    assert expression_usage["preference"]["mode"] == "normal"
    assert expression_usage["summary"]["group"] >= 1
    assert expression_usage["recent"][0]["scope"] == "group"
    assert expression_usage["recent"][0]["label"] == "点头"
    assert expression_usage["recent"][0]["asset_enabled"] is True
    assert expression_usage["recent"][0]["cooldown_turns"] == 3
    assert expression_usage["counts"][0]["tag"] == "gesture:点头"
    assert expression_usage["counts"][0]["asset_enabled"] is True
    assert expression_usage["counts"][0]["cooldown_turns"] == 3

    listed = server.group_conversations({"id": user_id})["group_conversations"]
    assert listed[0]["message_count"] == 3
    assert listed[0]["unread_count"] == 0

    read = server.mark_group_read(group["id"], {"id": user_id})
    assert read["last_read_group_message_id"] == messages[-1]["id"]

    removed = server.remove_group_conversation_member(group["id"], persona_ids[2], {"id": user_id})[
        "group_conversation"
    ]
    assert len(removed["members"]) == 2
    assert persona_ids[2] not in {int(member["persona_id"]) for member in removed["members"]}
    restored = server.add_group_conversation_member(
        group["id"],
        server.GroupMemberRequest(persona_id=persona_ids[2]),
        {"id": user_id},
    )["group_conversation"]
    assert len(restored["members"]) == 3
    assert persona_ids[2] in {int(member["persona_id"]) for member in restored["members"]}

    fallback_calls: list[str] = []

    def flaky_llm(messages, task="chat"):
        fallback_calls.append("turn")
        raise LLMProviderError("temporary outage", status_code=503)
        joined = "\n".join(str(item.get("content") or "") for item in messages)
        if "Group Router" in joined:
            fallback_calls.append("router")
            return json.dumps(
                {"speakers": [{"persona_id": persona_ids[0], "reason": "temporary fallback"}]},
                ensure_ascii=False,
            )
        fallback_calls.append("reply")
        raise LLMProviderError("temporary outage", status_code=503)

    group_chat.call_llm_api = flaky_llm
    fallback = server.group_chat_endpoint(
        server.GroupChatRequest(
            group_conversation_id=group["id"],
            message="哈喽",
            client_message_id="group-chat-fallback",
        ),
        {"id": user_id},
    )
    assert fallback_calls == ["turn"]
    assert fallback["degraded"] is True
    assert fallback["replies"] == []
    assert fallback["error_message"]
    assert len(fallback["messages"]) == 1
    assert fallback["messages"][0]["speaker_type"] == "user"
    assert fallback["messages"][0]["reply_status"] == "error"

    recovery_calls: list[str] = []

    def recovered_llm(messages, task="chat"):
        recovery_calls.append("turn")
        return json.dumps(
            {"messages": [{"persona_id": persona_ids[0], "content": "recovered group reply", "reason": "retry"}]},
            ensure_ascii=False,
        )

    group_chat.call_llm_api = recovered_llm
    recovered = server.group_chat_endpoint(
        server.GroupChatRequest(
            group_conversation_id=group["id"],
            message=fallback["messages"][0]["content"],
            client_message_id="group-chat-fallback",
        ),
        {"id": user_id},
    )
    assert recovery_calls == ["turn"]
    assert recovered["degraded"] is False
    assert recovered["user_message_id"] == fallback["user_message_id"]
    assert len(recovered["replies"]) == 1
    assert recovered["messages"][0]["id"] == fallback["messages"][0]["id"]
    assert recovered["messages"][1]["content"] == "recovered group reply"

    route_failure_calls: list[str] = []

    def broken_router(messages, task="chat"):
        route_failure_calls.append("turn")
        return "not json"

    group_chat.call_llm_api = broken_router
    route_failed = server.group_chat_endpoint(
        server.GroupChatRequest(
            group_conversation_id=group["id"],
            message="有人接一下吗",
            client_message_id="group-chat-router-failed",
        ),
        {"id": user_id},
    )
    assert route_failure_calls == ["turn"]
    assert route_failed["degraded"] is True
    assert route_failed["degraded_reason"] == "turn_parse_failed"
    assert route_failed["route"]["speakers"] == []
    assert route_failed["replies"] == []
    assert len(route_failed["messages"]) == 1
    assert route_failed["messages"][0]["speaker_type"] == "user"

    updated = server.patch_group_conversation(
        group["id"],
        server.GroupConversationUpdateRequest(title="夜聊小组", pinned=True),
        {"id": user_id},
    )["group_conversation"]
    assert updated["title"] == "夜聊小组"
    assert int(updated["pinned_at"]) > 0

    quiet_calls: list[str] = []

    def quiet_llm(messages, task="chat"):
        quiet_calls.append("turn")
        return json.dumps({"messages": []}, ensure_ascii=False)
        joined = "\n".join(str(item.get("content") or "") for item in messages)
        if "Group Router" in joined:
            quiet_calls.append("router")
            return json.dumps({"speakers": []}, ensure_ascii=False)
        quiet_calls.append("unexpected_reply")
        return "should not happen"

    group_chat.call_llm_api = quiet_llm
    quiet = server.group_chat_endpoint(
        server.GroupChatRequest(
            group_conversation_id=group["id"],
            message="我先安静一下。",
            client_message_id="group-chat-quiet",
        ),
        {"id": user_id},
    )
    assert quiet_calls == ["turn"]
    assert quiet["route"]["speakers"] == []
    assert quiet["replies"] == []
    assert quiet["degraded"] is False

    quiet_empty_expected = server.group_chat_endpoint(
        server.GroupChatRequest(
            group_conversation_id=group["id"],
            message="你们怎么看？",
            client_message_id="group-chat-empty-expected",
        ),
        {"id": user_id},
    )
    assert quiet_calls == ["turn", "turn"]
    assert quiet_empty_expected["degraded"] is True
    assert quiet_empty_expected["degraded_reason"] == "empty_expected_reply"
    assert quiet_empty_expected["route"]["speakers"] == []
    assert quiet_empty_expected["replies"] == []
    assert len(quiet_empty_expected["messages"]) == 1
    assert quiet_empty_expected["messages"][0]["reply_status"] == "error"

    autonomous_calls: list[str] = []

    def autonomous_llm(messages, task="chat"):
        joined = "\n".join(str(item.get("content") or "") for item in messages)
        assert "Recent group messages" in joined
        assert "familiarity" in joined
        autonomous_calls.append("turn")
        return json.dumps(
            {"messages": [{"persona_id": persona_ids[1], "content": "I can add one thought.", "reason": "continue"}]},
            ensure_ascii=False,
        )

    group_chat.call_llm_api = autonomous_llm
    autonomous = group_chat.autonomous_group_turn(
        user_id=user_id,
        group_conversation_id=group["id"],
        client_message_id="group-auto-1",
        min_idle_seconds=0,
    )
    assert autonomous_calls == ["turn"]
    assert autonomous["skipped"] is False
    assert len(autonomous["replies"]) == 1
    assert autonomous["replies"][0]["speaker_persona_id"] == persona_ids[1]
    repeated_auto = group_chat.autonomous_group_turn(
        user_id=user_id,
        group_conversation_id=group["id"],
        client_message_id="group-auto-1",
        min_idle_seconds=0,
    )
    assert autonomous_calls == ["turn"]
    assert repeated_auto["reason"] == "reused"
    assert repeated_auto["messages"][0]["id"] == autonomous["messages"][0]["id"]

    archived = server.patch_group_conversation(
        group["id"],
        server.GroupConversationUpdateRequest(status="archived"),
        {"id": user_id},
    )["group_conversation"]
    assert archived["status"] == "archived"
    assert server.group_conversations({"id": user_id})["group_conversations"] == []
    assert server.group_conversations({"id": user_id}, status="archived")["group_conversations"][0]["id"] == group["id"]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database.DB_PATH = Path(tmp) / "group.db"
        database.init_db()
        verify_group_chat_flow()
    print("Group chat verification passed")


if __name__ == "__main__":
    main()
