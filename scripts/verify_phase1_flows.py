from __future__ import annotations

import json
import tempfile
import sys
from http.cookies import SimpleCookie
from pathlib import Path

from fastapi import BackgroundTasks, HTTPException, Response

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.database as database


UNNAMED = "\u672a\u547d\u540d"
NAME = "\u6e05\u548c"
NEUTRAL = "\u5173\u7cfb\u672a\u5b9a"


def seed_user() -> tuple[int, int]:
    ts = database.now_ts()
    with database.get_db() as db:
        user_id = int(
            db.execute(
                "INSERT INTO users (username, password_hash, created_at, updated_at) VALUES ('tester', 'x', ?, ?)",
                (ts, ts),
            ).lastrowid
        )
        db.execute(
            "INSERT INTO user_profiles (user_id, nickname, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (user_id, "\u6708", ts, ts),
        )
        persona_id = int(
            db.execute(
                """
                INSERT INTO personas (user_id, name, summary, prompt, relationship, speaking_style, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, NAME, "\u5b89\u9759", "\u81ea\u7136\u804a\u5929", NEUTRAL, "\u77ed\u53e5", ts, ts),
            ).lastrowid
        )
    return user_id, persona_id


def disable_chat_side_effects(chat) -> None:
    chat.extract_and_store = lambda **kwargs: []
    chat.update_interaction_insight = lambda *args, **kwargs: {}
    chat.recall_memories = lambda *args, **kwargs: []
    chat.recall_layered_memory = lambda *args, **kwargs: []
    chat.should_use_semantic_recall = lambda: False
    chat.insight_prompt = lambda *args, **kwargs: ""
    chat.conversation_summary_prompt = lambda *args, **kwargs: ""
    chat.state_prompt = lambda *args, **kwargs: ""
    chat.summary_prompt = lambda *args, **kwargs: ""
    chat.layered_memory_prompt = lambda *args, **kwargs: ""
    chat.semantic_memory_prompt = lambda *args, **kwargs: ""
    chat.policy_snapshot = lambda: {}
    chat.should_refresh_summary = lambda count: False


def verify_relationship_authority(forge) -> None:
    def relation(model_value: str, description: str = "", selections: dict | None = None) -> str:
        return forge.normalize_persona(
            {"relationship": model_value},
            selections=selections or {},
            description=description,
        )["relationship"]

    assert relation("\u604b\u4eba", "\u6211\u60f3\u548c\u4e00\u4e2a\u5b89\u9759\u6e29\u67d4\u7684\u5973\u751f\u804a\u5929") == NEUTRAL
    assert relation("\u966a\u4f34\u8005", "\u4e0d\u8981\u50cf\u604b\u4eba\u90a3\u6837\u9ecf") == NEUTRAL
    assert relation("\u604b\u4eba", "\u6211\u60f3\u8981\u604b\u4eba\u4e00\u6837\u7684\u5173\u7cfb") == "\u604b\u4eba"
    assert relation("\u540c\u684c", "\u60f3\u548c\u4e00\u4e2a\u50cf\u540c\u684c\u4e00\u6837\u7684\u4eba\u804a\u5929") == "\u540c\u684c"
    assert relation("\u604b\u4eba", selections={"relationship": ["\u50cf\u670b\u53cb\u4e00\u6837"]}) == "\u50cf\u670b\u53cb\u4e00\u6837"


def verify_naming_and_restore(server, user_id: int) -> None:
    base = {
        "name": UNNAMED,
        "summary": "\u5b89\u9759",
        "prompt": "prompt",
        "traits": [],
        "relationship": NEUTRAL,
        "speaking_style": "\u81ea\u7136",
        "boundaries": [],
    }
    user = {"id": user_id}
    original_forge = server.forge_persona
    server.forge_persona = lambda **kwargs: {**base, "name": kwargs.get("preferred_name") or UNNAMED}
    try:
        try:
            server.create_persona(server.PersonaCreateRequest(description="quiet"), user)
            raise AssertionError("unnamed persona should not be inserted")
        except HTTPException as exc:
            assert exc.status_code == 503

        made = server.create_persona(
            server.PersonaCreateRequest(description="quiet", preferred_name="\u77e5\u9065"),
            user,
        )["persona"]
        persona_id = int(made["id"])
        assert made["name"] == "\u77e5\u9065"
        ts = database.now_ts()
        with database.get_db() as db:
            conversation_id = int(
                db.execute(
                    "INSERT INTO conversations (user_id, persona_id, title, created_at, updated_at) VALUES (?, ?, 'record', ?, ?)",
                    (user_id, persona_id, ts, ts),
                ).lastrowid
            )
            db.execute(
                """
                INSERT INTO messages (conversation_id, user_id, persona_id, role, content, created_at)
                VALUES (?, ?, ?, 'user', ?, ?)
                """,
                (conversation_id, user_id, persona_id, "\u5bfc\u51fa\u6d4b\u8bd5", ts),
            )
            group_id = int(
                db.execute(
                    "INSERT INTO group_conversations (user_id, title, created_at, updated_at) VALUES (?, 'group', ?, ?)",
                    (user_id, ts, ts),
                ).lastrowid
            )
            db.execute(
                """
                INSERT INTO group_members (group_conversation_id, user_id, persona_id, display_name, joined_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (group_id, user_id, persona_id, "\u77e5\u9065", ts),
            )
            group_message_id = int(
                db.execute(
                    """
                    INSERT INTO group_messages (group_conversation_id, user_id, speaker_type, speaker_persona_id, content, created_at)
                    VALUES (?, ?, 'persona', ?, ?, ?)
                    """,
                    (group_id, user_id, persona_id, "\u7fa4\u804a\u5bfc\u51fa\u6d4b\u8bd5", ts),
                ).lastrowid
            )
        exported = server.export_persona_data(persona_id, user)
        exported_data = json.loads(exported.body.decode("utf-8"))
        assert exported_data["schema"] == "persona_export_v1"
        assert exported_data["persona"]["id"] == persona_id
        assert exported_data["conversations"][0]["id"] == conversation_id
        assert exported_data["messages"][0]["content"] == "\u5bfc\u51fa\u6d4b\u8bd5"
        server.delete_persona(persona_id, server.PersonaDeleteRequest(confirm_name="\u77e5\u9065"), user)
        assert any(int(item["id"]) == persona_id for item in server.deleted_personas(user)["personas"])
        assert not any(int(item["id"]) == conversation_id for item in server.conversations(user)["conversations"])
        server.restore_persona(persona_id, user)
        assert not any(int(item["id"]) == persona_id for item in server.deleted_personas(user)["personas"])
        assert any(int(item["id"]) == conversation_id for item in server.conversations(user)["conversations"])
        server.delete_persona(persona_id, server.PersonaDeleteRequest(confirm_name="\u77e5\u9065"), user)
        purged = server.purge_deleted_persona(persona_id, server.PersonaDeleteRequest(confirm_name="\u77e5\u9065"), user)
        assert purged["status"] == "purged"
        assert not any(int(item["id"]) == persona_id for item in server.deleted_personas(user)["personas"])
        try:
            server.export_persona_data(persona_id, user)
            raise AssertionError("purged persona should not export")
        except HTTPException as exc:
            assert exc.status_code == 404
        with database.get_db() as db:
            group_message = db.execute(
                "SELECT speaker_persona_id, content FROM group_messages WHERE id = ?",
                (group_message_id,),
            ).fetchone()
            assert group_message is not None
            assert group_message["speaker_persona_id"] is None
            assert "\u5df2\u6e05\u9664\u4eba\u683c" in group_message["content"]
            assert db.execute(
                "SELECT COUNT(*) AS count FROM group_members WHERE persona_id = ?",
                (persona_id,),
            ).fetchone()["count"] == 0
    finally:
        server.forge_persona = original_forge


def verify_chat_failure_and_idempotency(chat, server, user_id: int, persona_id: int) -> None:
    from app.llm_client import LLMProviderError

    disable_chat_side_effects(chat)
    ts = database.now_ts()
    with database.get_db() as db:
        older_id = int(
            db.execute(
                "INSERT INTO conversations (user_id, persona_id, title, created_at, updated_at) VALUES (?, ?, 'old', 1, 500)",
                (user_id, persona_id),
            ).lastrowid
        )
        target_id = int(
            db.execute(
                "INSERT INTO conversations (user_id, persona_id, title, created_at, updated_at) VALUES (?, ?, 'new', 1, 100)",
                (user_id, persona_id),
            ).lastrowid
        )
    chat.call_llm_api = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider down"))
    failed = chat.db_chat(user_id, persona_id, "\u8fd8\u5728\u5417", conversation_id=target_id, client_message_id="msg-failed")
    assert failed["degraded"] is True and failed["assistant_message_id"] is None
    assert failed["error_code"] == "reply_unavailable"
    listing = [
        item
        for item in server.conversations({"id": user_id})["conversations"]
        if int(item["persona_id"]) == persona_id
    ]
    assert int(listing[0]["id"]) == target_id and int(listing[0]["id"]) != older_id
    assert listing[0]["last_message_reply_status"] == "failed"
    messages = server.conversation_messages(target_id, {"id": user_id})["messages"]
    assert messages[-1]["reply_status"] == "failed" and messages[-1]["client_message_id"] == "msg-failed"

    chat.call_llm_api = lambda *args, **kwargs: "\u5728\u5462\u3002"
    answered = chat.db_chat(user_id, persona_id, "\u8fd8\u5728\u5417", client_message_id="msg-failed")
    repeated = chat.db_chat(user_id, persona_id, "\u8fd8\u5728\u5417", client_message_id="msg-failed")
    assert answered["reply"] == "\u5728\u5462\u3002" and repeated.get("reused_reply") is True
    with database.get_db() as db:
        stored = db.execute(
            "SELECT role, reply_status FROM messages WHERE conversation_id = ? ORDER BY id",
            (target_id,),
        ).fetchall()
    assert [item["role"] for item in stored] == ["user", "assistant"]
    assert stored[0]["reply_status"] == "answered"

    chat.call_llm_api = lambda *args, **kwargs: (_ for _ in ()).throw(
        LLMProviderError("MOONSHOT_API_KEY is not set, but config.yaml selects provider: kimi")
    )
    config_failed = chat.db_chat(user_id, persona_id, "配置是不是坏了", client_message_id="msg-config-failed")
    assert config_failed["error_code"] == "config_missing"
    assert "环境变量" in config_failed["error_message"]
    config_messages = server.conversation_messages(config_failed["conversation_id"], {"id": user_id})["messages"]
    assert config_messages[-1]["reply_error"] == config_failed["error_message"]

    chat.call_llm_api = lambda *args, **kwargs: (_ for _ in ()).throw(
        LLMProviderError("kimi request failed with status 429", status_code=429)
    )
    limited = chat.db_chat(user_id, persona_id, "再试一下", client_message_id="msg-rate-limited")
    assert limited["error_code"] == "rate_limited"
    assert "拥挤" in limited["error_message"]

    with database.get_db() as db:
        waiting_id = int(
            db.execute(
                "INSERT INTO conversations (user_id, persona_id, title, created_at, updated_at) VALUES (?, ?, 'waiting', ?, ?)",
                (user_id, persona_id, ts, ts),
            ).lastrowid
        )
        db.execute(
            """
            INSERT INTO messages
                (conversation_id, user_id, persona_id, role, content, reply_status, client_message_id, created_at)
            VALUES (?, ?, ?, 'user', 'wait', 'generating', 'msg-waiting', ?)
            """,
            (waiting_id, user_id, persona_id, ts),
        )
    model_calls = {"count": 0}

    def no_duplicate(*args, **kwargs):
        model_calls["count"] += 1
        return "unexpected"

    chat.call_llm_api = no_duplicate
    pending = chat.db_chat(user_id, persona_id, "wait", client_message_id="msg-waiting")
    assert pending.get("pending") is True and model_calls["count"] == 0


def verify_chat_defers_summary_refresh(chat, server, user_id: int, persona_id: int) -> None:
    disable_chat_side_effects(chat)
    original_llm = chat.call_llm_api
    original_summary_policy = chat.should_refresh_summary
    original_refresh = server.refresh_conversation_summary
    chat.call_llm_api = lambda *args, **kwargs: "这句先及时送达。"
    chat.should_refresh_summary = lambda count: True
    server.refresh_conversation_summary = lambda **kwargs: (_ for _ in ()).throw(
        AssertionError("summary task should run only after the response is returned")
    )
    tasks = BackgroundTasks()
    try:
        result = server.chat(
            server.ChatRequest(message="继续说吧", persona_id=persona_id, client_message_id="msg-summary-deferred"),
            tasks,
            {"id": user_id},
        )
        assert result["reply"] == "这句先及时送达。"
        assert result["conversation_summary"]["scheduled"] is True
        assert len(tasks.tasks) == 1
    finally:
        chat.call_llm_api = original_llm
        chat.should_refresh_summary = original_summary_policy
        server.refresh_conversation_summary = original_refresh


def verify_local_avatar_generation(server, user_id: int, persona_id: int) -> None:
    generated = server.generate_persona_avatar_placeholder(
        persona_id,
        server.PersonaAvatarGenerateRequest(desired_image="银色短发，安静的蓝灰色氛围"),
        {"id": user_id},
    )
    assert generated["ok"] is True
    assert generated["status"] == "generated"
    assert generated["url"].endswith(".svg")
    assert generated["persona"]["avatar_url"] == generated["url"]
    assert generated["persona"]["desired_image"] == "银色短发，安静的蓝灰色氛围"
    path = server.UPLOAD_DIR / str(user_id) / "generated" / Path(generated["url"]).name
    assert path.exists()
    assert "<svg" in path.read_text(encoding="utf-8")


def verify_tab_scoped_login(server, auth) -> None:
    auth.create_user("cookie_user", "password-cookie", "普通页")
    auth.create_user("tab_user", "password-tab", "独立页")

    cookie_response = Response()
    server.login(server.LoginRequest(username="cookie_user", password="password-cookie"), cookie_response)
    cookie = SimpleCookie()
    cookie.load(cookie_response.headers["set-cookie"])
    cookie_token = cookie[auth.SESSION_COOKIE].value

    tab_response = Response()
    isolated = server.login(
        server.LoginRequest(username="tab_user", password="password-tab", tab_session=True),
        tab_response,
    )
    tab_token = isolated["tab_session_token"]
    assert "set-cookie" not in tab_response.headers
    assert auth.current_user(session_token=cookie_token, authorization=f"Bearer {tab_token}")["username"] == "tab_user"
    assert auth.current_user(session_token=cookie_token, authorization=None)["username"] == "cookie_user"

    guest_response = Response()
    isolated_guest = server.guest_login(guest_response, tab_session=True)
    guest_token = isolated_guest["tab_session_token"]
    assert isolated_guest["user"]["is_guest"] is True
    assert "set-cookie" not in guest_response.headers
    assert bool(auth.current_user(session_token=cookie_token, authorization=f"Bearer {guest_token}")["is_guest"]) is True

    server.logout(Response(), cookie_token, f"Bearer {tab_token}")
    try:
        auth.current_user(session_token=None, authorization=f"Bearer {tab_token}")
        raise AssertionError("tab-scoped logout should delete only the bearer session")
    except HTTPException as exc:
        assert exc.status_code == 401
    assert auth.current_user(session_token=cookie_token, authorization=None)["username"] == "cookie_user"


def verify_profile_proactive_preferences(server, user_id: int) -> None:
    import app.proactive_contact as proactive_contact

    user = {"id": user_id}
    profile = server.profile(user)["profile"]
    proactive = profile["preferences"]["proactive_contact"]
    assert proactive["enabled"] is False
    assert proactive["max_per_day"] == 1
    assert proactive["quiet_start"] == "22:00"
    assert proactive["quiet_end"] == "09:00"

    updated = server.update_profile(
        server.ProfileUpdateRequest(
            nickname="\u6708",
            preferences={
                "theme": "quiet",
                "proactive_contact": {
                    "enabled": True,
                    "max_per_day": 99,
                    "quiet_start": "25:99",
                    "quiet_end": "08:30",
                    "allowed_types": ["followup", "unknown", "care"],
                },
            },
        ),
        user,
    )["profile"]
    assert updated["preferences"]["theme"] == "quiet"
    proactive = updated["preferences"]["proactive_contact"]
    assert proactive["enabled"] is True
    assert proactive["max_per_day"] == 3
    assert proactive["quiet_start"] == "22:00"
    assert proactive["quiet_end"] == "08:30"
    assert proactive["allowed_types"] == ["followup", "care"]

    ts = database.now_ts()
    old_ts = ts - 8 * 60 * 60
    with database.get_db() as db:
        persona_id = int(
            db.execute(
                """
                INSERT INTO personas (user_id, name, summary, prompt, relationship, speaking_style, created_at, updated_at)
                VALUES (?, '主动候选', '安静', '自然聊天', ?, '短句', ?, ?)
                """,
                (user_id, NEUTRAL, old_ts, old_ts),
            ).lastrowid
        )
        conversation_id = int(
            db.execute(
                "INSERT INTO conversations (user_id, persona_id, title, created_at, updated_at) VALUES (?, ?, '主动候选', ?, ?)",
                (user_id, persona_id, old_ts, old_ts),
            ).lastrowid
        )
        db.execute(
            """
            INSERT INTO messages (conversation_id, user_id, persona_id, role, content, created_at)
            VALUES (?, ?, ?, 'user', '我明天要去办一件事', ?)
            """,
            (conversation_id, user_id, persona_id, old_ts),
        )
        db.execute(
            """
            INSERT INTO conversation_summaries (
                user_id, persona_id, conversation_id, summary_text, key_points_json,
                source_message_count, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, 1, 'active', ?, ?)
            """,
            (
                user_id,
                persona_id,
                conversation_id,
                "用户提到明天要去办一件事，后续可以低频确认进展。",
                '["明天要去办一件事"]',
                old_ts,
                old_ts,
            ),
        )
    preview = proactive_contact.proactive_contact_candidates(user_id, at_ts=ts, limit=5)
    assert preview["allowed_now"] is True
    assert preview["blocked_reason"] == ""
    assert preview["candidates"][0]["type"] == "followup"
    assert preview["candidates"][0]["conversation_id"] == conversation_id
    assert preview["candidates"][0]["memory_basis"]["strength"] == "direct"
    assert preview["candidates"][0]["memory_basis"]["has_summary"] is True
    assert any(item["kind"] == "key_point" for item in preview["candidates"][0]["memory_basis"]["evidence"])
    assert preview["candidates"][0]["risk_level"] == "low"
    assert preview["candidates"][0]["arbitration"]["decision"] == "allow"
    api_preview = server.proactive_contact_candidate_preview(user, limit=5)
    assert api_preview["settings"]["enabled"] is True
    assert api_preview["candidates"][0]["conversation_id"] == conversation_id
    assert api_preview["candidates"][0]["memory_basis"]["evidence"]
    admin_preview = server.admin_proactive_contact_candidates({"id": user_id, "role": "admin"}, target_user_id=user_id, limit=5)
    assert admin_preview["candidates"][0]["conversation_id"] == conversation_id
    assert admin_preview["candidates"][0]["memory_basis"]["has_summary"] is True
    assert admin_preview["blocked_candidates"] == []

    event = server.record_proactive_contact_event_endpoint(
        server.ProactiveContactEventRequest(
            event_type="candidate_opened",
            conversation_id=conversation_id,
            persona_id=persona_id,
            candidate_type="followup",
            detail={"reason": preview["candidates"][0]["reason"]},
        ),
        user,
    )["event"]
    assert event["event_type"] == "candidate_opened"
    assert event["conversation_id"] == conversation_id
    assert event["persona_id"] == persona_id
    assert event["candidate_type"] == "followup"
    assert event["detail"]["reason"] == "old_user_message"
    admin_events = server.admin_proactive_contact_events({"id": user_id, "role": "admin"}, target_user_id=user_id, limit=5)
    assert admin_events["events"][0]["id"] == event["id"]
    assert admin_events["events"][0]["event_type"] == "candidate_opened"
    try:
        server.record_proactive_contact_event_endpoint(
            server.ProactiveContactEventRequest(event_type="bad", conversation_id=conversation_id),
            user,
        )
        raise AssertionError("unsupported proactive event type should fail")
    except HTTPException as exc:
        assert exc.status_code == 400

    after_event_ts = database.now_ts()
    server.update_profile(
        server.ProfileUpdateRequest(
            nickname="\u6708",
            preferences={
                "proactive_contact": {
                    "enabled": True,
                    "max_per_day": 2,
                    "quiet_start": "00:00",
                    "quiet_end": "00:00",
                    "allowed_types": ["followup", "care"],
                },
            },
        ),
        user,
    )
    handled_preview = proactive_contact.proactive_contact_candidates(user_id, at_ts=after_event_ts, limit=5)
    assert handled_preview["allowed_now"] is True
    assert handled_preview["usage_today"] == 1
    assert handled_preview["remaining_today"] == 1
    assert handled_preview["candidates"] == []

    server.update_profile(
        server.ProfileUpdateRequest(
            nickname="\u6708",
            preferences={
                "proactive_contact": {
                    "enabled": True,
                    "max_per_day": 1,
                    "quiet_start": "00:00",
                    "quiet_end": "00:00",
                    "allowed_types": ["followup", "care"],
                },
            },
        ),
        user,
    )
    limited_preview = proactive_contact.proactive_contact_candidates(user_id, at_ts=after_event_ts, limit=5)
    assert limited_preview["allowed_now"] is False
    assert limited_preview["blocked_reason"] == "daily_limit"
    assert limited_preview["usage_today"] == 1
    assert limited_preview["remaining_today"] == 0
    assert limited_preview["candidates"] == []

    with database.get_db() as db:
        care_conversation_id = int(
            db.execute(
                "INSERT INTO conversations (user_id, persona_id, title, created_at, updated_at) VALUES (?, ?, '涓诲姩鍏冲績', ?, ?)",
                (user_id, persona_id, old_ts + 1, old_ts + 1),
            ).lastrowid
        )
        db.execute(
            """
            INSERT INTO messages (conversation_id, user_id, persona_id, role, content, created_at)
            VALUES (?, ?, ?, 'assistant', ?, ?)
            """,
            (care_conversation_id, user_id, persona_id, "\u6211\u4e4b\u524d\u5728\u8fd9\u91cc\u3002", old_ts + 1),
        )
    server.update_profile(
        server.ProfileUpdateRequest(
            nickname="\u6708",
            preferences={
                "proactive_contact": {
                    "enabled": True,
                    "max_per_day": 2,
                    "quiet_start": "00:00",
                    "quiet_end": "00:00",
                    "allowed_types": ["care"],
                },
            },
        ),
        user,
    )
    care_preview = proactive_contact.proactive_contact_candidates(user_id, at_ts=after_event_ts, limit=5)
    assert care_preview["allowed_now"] is True
    assert care_preview["usage_today"] == 1
    assert care_preview["remaining_today"] == 1
    assert [item["type"] for item in care_preview["candidates"]] == ["care"]
    assert care_preview["candidates"][0]["conversation_id"] == care_conversation_id
    assert care_preview["candidates"][0]["memory_basis"]["strength"] == "weak"
    assert "long_idle_only" in care_preview["candidates"][0]["risk_notes"]
    assert care_preview["candidates"][0]["risk_level"] == "watch"
    assert care_preview["candidates"][0]["arbitration"]["decision"] == "watch"

    import app.layered_memory as layered_memory

    with database.get_db() as db:
        reminder_conversation_id = int(
            db.execute(
                "INSERT INTO conversations (user_id, persona_id, title, created_at, updated_at) VALUES (?, ?, 'reminder', ?, ?)",
                (user_id, persona_id, old_ts + 2, old_ts + 2),
            ).lastrowid
        )
        db.execute(
            """
            INSERT INTO messages (conversation_id, user_id, persona_id, role, content, created_at)
            VALUES (?, ?, ?, 'user', '明天提醒我带资料。', ?)
            """,
            (reminder_conversation_id, user_id, persona_id, old_ts + 2),
        )
        db.execute(
            """
            INSERT INTO memory_facts (
                uid, user_id, persona_id, conversation_id, type, text, importance, confidence,
                valid_from, created_at, updated_at
            )
            VALUES ('FACT-PROACTIVE-REMINDER', ?, ?, ?, 'plan', '用户明天提醒自己带资料，别忘了。', 0.82, 0.8, ?, ?, ?)
            """,
            (user_id, persona_id, reminder_conversation_id, old_ts + 2, old_ts + 2, old_ts + 2),
        )
    layered_memory.refresh_memory_state(user_id, persona_id)
    server.update_profile(
        server.ProfileUpdateRequest(
            nickname="\u6708",
            preferences={
                "proactive_contact": {
                    "enabled": True,
                    "max_per_day": 3,
                    "quiet_start": "00:00",
                    "quiet_end": "00:00",
                    "allowed_types": ["reminder"],
                },
            },
        ),
        user,
    )
    reminder_preview = proactive_contact.proactive_contact_candidates(user_id, at_ts=after_event_ts, limit=5)
    assert reminder_preview["allowed_now"] is True
    assert reminder_preview["candidates"][0]["type"] == "reminder"
    assert reminder_preview["candidates"][0]["reason"] == "active_reminder"
    assert reminder_preview["candidates"][0]["conversation_id"] == reminder_conversation_id
    assert reminder_preview["candidates"][0]["source_uid"] == "FACT-PROACTIVE-REMINDER"
    assert reminder_preview["candidates"][0]["risk_level"] == "low"
    assert any(item["kind"] == "dynamic_state" for item in reminder_preview["candidates"][0]["memory_basis"]["evidence"])

    with database.get_db() as db:
        interest_conversation_id = int(
            db.execute(
                "INSERT INTO conversations (user_id, persona_id, title, created_at, updated_at) VALUES (?, ?, 'interest', ?, ?)",
                (user_id, persona_id, old_ts + 3, old_ts + 3),
            ).lastrowid
        )
        db.execute(
            """
            INSERT INTO messages (conversation_id, user_id, persona_id, role, content, created_at)
            VALUES (?, ?, ?, 'user', '我喜欢青柠苏打。', ?)
            """,
            (interest_conversation_id, user_id, persona_id, old_ts + 3),
        )
        db.execute(
            """
            INSERT INTO memory_relations (
                uid, user_id, persona_id, conversation_id, type, subject, predicate, object, text,
                importance, confidence, valid_from, created_at, updated_at
            )
            VALUES ('REL-PROACTIVE-INTEREST', ?, ?, ?, 'preference', 'user', 'preference', '青柠苏打',
                    '用户喜欢青柠苏打', 0.78, 0.82, ?, ?, ?)
            """,
            (user_id, persona_id, interest_conversation_id, old_ts + 3, old_ts + 3, old_ts + 3),
        )
        blocked_interest_conversation_id = int(
            db.execute(
                "INSERT INTO conversations (user_id, persona_id, title, created_at, updated_at) VALUES (?, ?, 'blocked interest', ?, ?)",
                (user_id, persona_id, old_ts + 4, old_ts + 4),
            ).lastrowid
        )
        db.execute(
            """
            INSERT INTO messages (conversation_id, user_id, persona_id, role, content, created_at)
            VALUES (?, ?, ?, 'user', '我喜欢原神，但别主动提原神。', ?)
            """,
            (blocked_interest_conversation_id, user_id, persona_id, old_ts + 4),
        )
        db.execute(
            """
            INSERT INTO memory_relations (
                uid, user_id, persona_id, conversation_id, type, subject, predicate, object, text,
                importance, confidence, valid_from, created_at, updated_at
            )
            VALUES ('REL-PROACTIVE-INTEREST-BOUNDARY', ?, ?, ?, 'preference', 'user', 'preference', '原神',
                    '用户喜欢原神，但别主动提原神', 0.78, 0.82, ?, ?, ?)
            """,
            (user_id, persona_id, blocked_interest_conversation_id, old_ts + 4, old_ts + 4, old_ts + 4),
        )
        db.execute(
            """
            INSERT INTO user_insights (
                user_id, profile_summary, interaction_style, emotional_patterns_json,
                inferred_profile_json, topic_model_json, guidance_json,
                discovery_dimensions_json, curiosity_feedback_json, updated_at
            )
            VALUES (?, '', '', '[]', '{}', ?, ?, '{}', '{}', ?)
            ON CONFLICT(user_id) DO UPDATE SET
                topic_model_json = excluded.topic_model_json,
                guidance_json = excluded.guidance_json,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                json.dumps({"avoid_topics": ["原神"]}, ensure_ascii=False),
                json.dumps({"do_not": ["Do not proactively bring up 原神."]}, ensure_ascii=False),
                after_event_ts,
            ),
        )
    server.update_profile(
        server.ProfileUpdateRequest(
            nickname="\u6708",
            preferences={
                "proactive_contact": {
                    "enabled": True,
                    "max_per_day": 3,
                    "quiet_start": "00:00",
                    "quiet_end": "00:00",
                    "allowed_types": ["interest"],
                },
            },
        ),
        user,
    )
    interest_preview = proactive_contact.proactive_contact_candidates(user_id, at_ts=after_event_ts, limit=5, include_blocked=True)
    assert interest_preview["candidates"][0]["type"] == "interest"
    assert interest_preview["candidates"][0]["topic"] == "青柠苏打"
    assert interest_preview["candidates"][0]["conversation_id"] == interest_conversation_id
    assert interest_preview["candidates"][0]["source_uid"] == "REL-PROACTIVE-INTEREST"
    assert interest_preview["candidates"][0]["risk_level"] == "low"
    assert not any(item.get("topic") == "原神" for item in interest_preview["candidates"])
    assert not any(item.get("topic") == "原神" for item in interest_preview["blocked_candidates"])

    server.update_profile(
        server.ProfileUpdateRequest(
            nickname="\u6708",
            preferences={
                "proactive_contact": {
                    "enabled": True,
                    "max_per_day": 3,
                    "quiet_start": "00:00",
                    "quiet_end": "00:00",
                    "allowed_types": ["followup", "care"],
                },
            },
        ),
        user,
    )

    with database.get_db() as db:
        no_basis_conversation_id = int(
            db.execute(
                "INSERT INTO conversations (user_id, persona_id, title, created_at, updated_at) VALUES (?, ?, 'no basis', ?, ?)",
                (user_id, persona_id, old_ts + 2, old_ts + 2),
            ).lastrowid
        )
        db.execute(
            """
            INSERT INTO messages (conversation_id, user_id, persona_id, role, content, created_at)
            VALUES (?, ?, ?, 'assistant', '', ?)
            """,
            (no_basis_conversation_id, user_id, persona_id, old_ts + 2),
        )
    blocked_no_basis = proactive_contact.proactive_contact_candidates(user_id, at_ts=after_event_ts, limit=5, include_blocked=True)
    assert not any(int(item["conversation_id"]) == no_basis_conversation_id for item in blocked_no_basis["candidates"])
    assert any(
        int(item["conversation_id"]) == no_basis_conversation_id
        and item["risk_level"] == "blocked"
        and "no_memory_basis" in item["arbitration"]["reasons"]
        for item in blocked_no_basis["blocked_candidates"]
    )

    with database.get_db() as db:
        db.execute(
            """
            INSERT INTO user_insights (
                user_id, profile_summary, interaction_style, emotional_patterns_json,
                inferred_profile_json, topic_model_json, guidance_json,
                discovery_dimensions_json, curiosity_feedback_json, updated_at
            )
            VALUES (?, '', '', '[]', '{}', ?, ?, '{}', '{}', ?)
            ON CONFLICT(user_id) DO UPDATE SET
                topic_model_json = excluded.topic_model_json,
                guidance_json = excluded.guidance_json,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                json.dumps({"avoid_topics": ["原神"]}, ensure_ascii=False),
                json.dumps({"do_not": ["Do not proactively bring up 原神."]}, ensure_ascii=False),
                after_event_ts,
            ),
        )
        boundary_conversation_id = int(
            db.execute(
                "INSERT INTO conversations (user_id, persona_id, title, created_at, updated_at) VALUES (?, ?, 'topic boundary', ?, ?)",
                (user_id, persona_id, old_ts + 3, old_ts + 3),
            ).lastrowid
        )
        db.execute(
            """
            INSERT INTO messages (conversation_id, user_id, persona_id, role, content, created_at)
            VALUES (?, ?, ?, 'user', '我之前提到原神，但别主动聊这个。', ?)
            """,
            (boundary_conversation_id, user_id, persona_id, old_ts + 3),
        )
    server.update_profile(
        server.ProfileUpdateRequest(
            nickname="\u6708",
            preferences={
                "proactive_contact": {
                    "enabled": True,
                    "max_per_day": 3,
                    "quiet_start": "00:00",
                    "quiet_end": "00:00",
                    "allowed_types": ["followup", "care"],
                },
            },
        ),
        user,
    )
    boundary_preview = server.admin_proactive_contact_candidates({"id": user_id, "role": "admin"}, target_user_id=user_id, limit=8)
    assert any(
        int(item["conversation_id"]) == boundary_conversation_id
        and "topic_boundary_match" in item["arbitration"]["reasons"]
        and "原神" in item.get("boundary_hits", [])
        for item in boundary_preview["blocked_candidates"]
    )

    with database.get_db() as db:
        sensitive_conversation_id = int(
            db.execute(
                "INSERT INTO conversations (user_id, persona_id, title, created_at, updated_at) VALUES (?, ?, 'sensitive', ?, ?)",
                (user_id, persona_id, old_ts + 4, old_ts + 4),
            ).lastrowid
        )
        db.execute(
            """
            INSERT INTO messages (conversation_id, user_id, persona_id, role, content, created_at)
            VALUES (?, ?, ?, 'assistant', '你刚刚说你不想活，我在这里。', ?)
            """,
            (sensitive_conversation_id, user_id, persona_id, old_ts + 4),
        )
    sensitive_preview = server.admin_proactive_contact_candidates({"id": user_id, "role": "admin"}, target_user_id=user_id, limit=8)
    assert any(
        int(item["conversation_id"]) == sensitive_conversation_id
        and "sensitive_content_blocked" in item["arbitration"]["reasons"]
        and "self_harm" in item.get("sensitivity", {}).get("blocked", [])
        for item in sensitive_preview["blocked_candidates"]
    )

    server.record_proactive_contact_event_endpoint(
        server.ProactiveContactEventRequest(
            event_type="candidate_dismissed",
            conversation_id=care_conversation_id,
            persona_id=persona_id,
            candidate_type="care",
            detail={"reason": "not_today"},
        ),
        user,
    )
    dismissed_preview = proactive_contact.proactive_contact_candidates(user_id, at_ts=database.now_ts(), limit=5)
    assert dismissed_preview["allowed_now"] is True
    assert dismissed_preview["usage_today"] == 1
    assert not any(
        int(item["conversation_id"]) == care_conversation_id and item["type"] == "care"
        for item in dismissed_preview["candidates"]
    )
    reply_event = server.record_proactive_contact_event_endpoint(
        server.ProactiveContactEventRequest(
            event_type="candidate_replied",
            conversation_id=care_conversation_id,
            persona_id=persona_id,
            candidate_type="care",
            detail={"user_message_id": 123},
        ),
        user,
    )["event"]
    assert reply_event["event_type"] == "candidate_replied"
    admin_event_report = server.admin_proactive_contact_events({"id": user_id, "role": "admin"}, target_user_id=user_id, limit=1)
    assert admin_event_report["events"][0]["id"] == reply_event["id"]
    assert admin_event_report["summary"]["opened"] == 1
    assert admin_event_report["summary"]["dismissed"] == 1
    assert admin_event_report["summary"]["replied"] == 1
    assert admin_event_report["summary"]["reply_rate"] == 1
    assert admin_event_report["summary"]["dismiss_rate"] == 1
    assert admin_event_report["summary"]["by_type_summary"]["care"]["replied"] == 1
    assert admin_event_report["summary"]["by_type_summary"]["care"]["dismissed"] == 1
    assert admin_event_report["summary"]["by_type_summary"]["care"]["reply_rate"] == 0.5
    for _ in range(2):
        server.record_proactive_contact_event_endpoint(
            server.ProactiveContactEventRequest(
                event_type="candidate_dismissed",
                conversation_id=care_conversation_id,
                persona_id=persona_id,
                candidate_type="interest",
                detail={"reason": "not_relevant"},
            ),
            user,
        )
    feedback_policy = proactive_contact.proactive_contact_feedback_policy(user_id)
    assert "interest" in feedback_policy["suppressed_types"]
    assert feedback_policy["reasons"]["interest"] == "recent_dismissals_without_replies"
    assert feedback_policy["type_scores"]["interest"]["score"] <= -1
    assert feedback_policy["type_scores"]["interest"]["outcome"] == "suppressed"
    assert feedback_policy["type_scores"]["interest"]["action"] == "suppress_preview"
    assert feedback_policy["type_scores"]["interest"]["recovery_hint"]
    assert feedback_policy["type_scores"]["care"]["replied"] == 1
    feedback_summary = proactive_contact.proactive_contact_event_summary(user_id)
    assert feedback_summary["by_type_summary"]["interest"]["dismissed"] == 2
    assert feedback_summary["by_type_summary"]["interest"]["dismiss_rate"] == 1

    for _ in range(2):
        server.record_proactive_contact_event_endpoint(
            server.ProactiveContactEventRequest(
                event_type="candidate_dismissed",
                candidate_type="followup",
                detail={"reason": "too_much"},
            ),
            user,
        )
    with database.get_db() as db:
        suppressed_conversation_id = int(
            db.execute(
                "INSERT INTO conversations (user_id, persona_id, title, created_at, updated_at) VALUES (?, ?, 'suppressed followup', ?, ?)",
                (user_id, persona_id, old_ts + 5, old_ts + 5),
            ).lastrowid
        )
        db.execute(
            """
            INSERT INTO messages (conversation_id, user_id, persona_id, role, content, created_at)
            VALUES (?, ?, ?, 'user', '我明天还要继续整理房间。', ?)
            """,
            (suppressed_conversation_id, user_id, persona_id, old_ts + 5),
        )
    suppressed_preview = server.admin_proactive_contact_candidates({"id": user_id, "role": "admin"}, target_user_id=user_id, limit=8)
    assert any(
        int(item["conversation_id"]) == suppressed_conversation_id
        and "recent_dismissals_without_replies" in item["arbitration"]["reasons"]
        and item["feedback_score"] <= -1
        and item["feedback_outcome"] == "suppressed"
        and item["feedback_action"] == "suppress_preview"
        and item["feedback_recovery_hint"]
        and item["adjusted_priority"] <= item["priority"]
        for item in suppressed_preview["blocked_candidates"]
    )

    server.update_profile(
        server.ProfileUpdateRequest(
            nickname="\u6708",
            preferences={
                "proactive_contact": {
                    "enabled": True,
                    "max_per_day": 1,
                    "quiet_start": "00:00",
                    "quiet_end": "23:59",
                    "allowed_types": ["followup"],
                },
            },
        ),
        user,
    )
    quiet_preview = proactive_contact.proactive_contact_candidates(user_id, at_ts=after_event_ts, limit=5)
    assert quiet_preview["allowed_now"] is False
    assert quiet_preview["blocked_reason"] == "quiet_hours"
    assert quiet_preview["candidates"] == []


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database.DB_PATH = Path(tmp) / "phase1.db"
        import app.auth as auth
        import app.db_chat as chat
        import app.persona_forge as forge
        import app.server as server

        server.UPLOAD_DIR = Path(tmp) / "uploads"
        server.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        database.init_db()
        user_id, persona_id = seed_user()
        verify_relationship_authority(forge)
        verify_naming_and_restore(server, user_id)
        verify_chat_failure_and_idempotency(chat, server, user_id, persona_id)
        verify_chat_defers_summary_refresh(chat, server, user_id, persona_id)
        verify_local_avatar_generation(server, user_id, persona_id)
        verify_profile_proactive_preferences(server, user_id)
        verify_tab_scoped_login(server, auth)
    print("Phase 1 ordinary-user flow verification passed")


if __name__ == "__main__":
    main()
