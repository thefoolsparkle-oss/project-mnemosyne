from __future__ import annotations

import tempfile
import sys
from http.cookies import SimpleCookie
from pathlib import Path

from fastapi import HTTPException, Response

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
        server.delete_persona(persona_id, server.PersonaDeleteRequest(confirm_name="\u77e5\u9065"), user)
        assert any(int(item["id"]) == persona_id for item in server.deleted_personas(user)["personas"])
        assert not any(int(item["id"]) == conversation_id for item in server.conversations(user)["conversations"])
        server.restore_persona(persona_id, user)
        assert not any(int(item["id"]) == persona_id for item in server.deleted_personas(user)["personas"])
        assert any(int(item["id"]) == conversation_id for item in server.conversations(user)["conversations"])
    finally:
        server.forge_persona = original_forge


def verify_chat_failure_and_idempotency(chat, server, user_id: int, persona_id: int) -> None:
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


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database.DB_PATH = Path(tmp) / "phase1.db"
        import app.auth as auth
        import app.db_chat as chat
        import app.persona_forge as forge
        import app.server as server

        database.init_db()
        user_id, persona_id = seed_user()
        verify_relationship_authority(forge)
        verify_naming_and_restore(server, user_id)
        verify_chat_failure_and_idempotency(chat, server, user_id, persona_id)
        verify_tab_scoped_login(server, auth)
    print("Phase 1 ordinary-user flow verification passed")


if __name__ == "__main__":
    main()
