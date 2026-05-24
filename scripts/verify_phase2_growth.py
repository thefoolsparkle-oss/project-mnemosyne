from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.database as database


def seed_user() -> int:
    ts = database.now_ts()
    with database.get_db() as db:
        user_id = int(
            db.execute(
                "INSERT INTO users (username, password_hash, created_at, updated_at) VALUES ('phase2', 'x', ?, ?)",
                (ts, ts),
            ).lastrowid
        )
        db.execute(
            "INSERT INTO user_profiles (user_id, nickname, created_at, updated_at) VALUES (?, '月', ?, ?)",
            (user_id, ts, ts),
        )
    return user_id


def forged_persona() -> dict:
    return {
        "name": "清和",
        "summary": "安静而可靠",
        "prompt": "自然聊天",
        "traits": ["温和"],
        "relationship": "像朋友一样",
        "speaking_style": "短句",
        "boundaries": ["不要说教"],
        "memory_profile": {},
        "psychological_profile": {},
        "psychological_fit_notes": "",
        "appearance_description": "",
        "desired_image": "",
        "growth_notes": "慢慢熟悉用户的节奏",
    }


def verify_growth_chain(server, sculptor, user_id: int) -> None:
    original_forge = server.forge_persona
    server.forge_persona = lambda **kwargs: forged_persona()
    try:
        persona = server.create_persona(server.PersonaCreateRequest(description="quiet"), {"id": user_id})["persona"]
    finally:
        server.forge_persona = original_forge
    persona_id = int(persona["id"])
    ts = database.now_ts()

    with database.get_db() as db:
        initial = db.execute(
            "SELECT change_type FROM persona_versions WHERE persona_id = ? AND version = 1",
            (persona_id,),
        ).fetchone()
        assert initial["change_type"] == "initial_forge"
        db.execute(
            """
            INSERT INTO memory_facts (
                uid, user_id, persona_id, type, text, importance, confidence,
                valid_from, created_at, updated_at
            )
            VALUES ('FACT-GROWTH', ?, ?, 'persona_feedback', '用户希望回复短一点', 0.9, 0.95, ?, ?, ?)
            """,
            (user_id, persona_id, ts, ts, ts),
        )
        db.execute(
            """
            INSERT INTO memory_state (
                user_id, persona_id, persona_scope, key, value_json, updated_at
            )
            VALUES (?, ?, ?, 'interaction_style', ?, ?)
            """,
            (user_id, persona_id, str(persona_id), json.dumps("短句", ensure_ascii=False), ts),
        )
        suggestion_id = int(
            db.execute(
                """
                INSERT INTO persona_revision_suggestions (
                    user_id, persona_id, status, base_version, reason, suggestion_json,
                    source_context_json, created_at, updated_at
                )
                VALUES (?, ?, 'pending', 1, '用户多次希望回复短一点', ?, ?, ?, ?)
                """,
                (
                    user_id,
                    persona_id,
                    json.dumps({**forged_persona(), "speaking_style": "更简短自然", "change_notes": ["减少长段回复"]}, ensure_ascii=False),
                    json.dumps(
                        {
                            "feedback_facts": [{"uid": "FACT-GROWTH", "text": "用户希望回复短一点"}],
                            "feedback_relations": [],
                            "state": {"interaction_style": ["短句"]},
                            "summaries": [{"text": "偏好短句"}],
                            "recent_traces": [],
                        },
                        ensure_ascii=False,
                    ),
                    ts,
                    ts,
                ),
            ).lastrowid
        )

    applied = sculptor.apply_revision_suggestion(
        user_id,
        suggestion_id,
        reviewer_user_id=user_id,
        decision_note="采纳用户明确提出的短回复偏好",
    )
    assert applied["version"] == 2
    with database.get_db() as db:
        version = db.execute(
            "SELECT change_type, source_suggestion_id, change_notes_json FROM persona_versions WHERE persona_id = ? AND version = 2",
            (persona_id,),
        ).fetchone()
        suggestion = db.execute(
            "SELECT status, applied_version, decided_at, decided_by_user_id, decision_note FROM persona_revision_suggestions WHERE id = ?",
            (suggestion_id,),
        ).fetchone()
    assert version["change_type"] == "sculptor_review"
    assert int(version["source_suggestion_id"]) == suggestion_id
    assert "减少长段回复" in json.loads(version["change_notes_json"])
    assert suggestion["status"] == "applied" and int(suggestion["applied_version"]) == 2
    assert int(suggestion["decided_by_user_id"]) == user_id and int(suggestion["decided_at"]) > 0
    assert suggestion["decision_note"] == "采纳用户明确提出的短回复偏好"
    shown = sculptor.list_revision_suggestions(user_id, persona_id)[0]
    assert shown["base_version"] == 1
    assert shown["evidence_summary"]["memory_count"] == 1
    assert any(change["field"] == "speaking_style" for change in shown["changes"])

    growth = server.persona_growth(persona_id, {"id": user_id})["growth"]
    signal_kinds = {item["kind"] for item in growth["signals"]}
    assert {"memory", "attention", "adaptation"} <= signal_kinds
    assert growth["latest_reviewed_change"]["version"] == 2
    assert growth["latest_reviewed_change"]["unseen"] is True
    assert growth["latest_reviewed_change"]["highlights"] == ["回应方式更贴近你的偏好"]
    adaptation = next(item for item in growth["signals"] if item["kind"] == "adaptation")
    assert "回应方式更贴近你的偏好" in adaptation["text"]
    assert "减少长段回复" not in adaptation["text"]
    card = next(item for item in server.personas({"id": user_id})["personas"] if int(item["id"]) == persona_id)
    assert card["growth_notice"]["version"] == 2
    server.mark_persona_growth_viewed(persona_id, {"id": user_id})
    card = next(item for item in server.personas({"id": user_id})["personas"] if int(item["id"]) == persona_id)
    assert card["growth_notice"] is None
    growth = server.persona_growth(persona_id, {"id": user_id})["growth"]
    assert growth["latest_reviewed_change"]["unseen"] is False

    original_llm = sculptor._llm_suggestion
    sculptor._llm_suggestion = lambda current, context, reason: {
        **forged_persona(),
        "speaking_style": "一句一回应",
        "change_notes": ["继续缩短回复"],
    }
    try:
        stale = sculptor.generate_revision_suggestion(user_id, persona_id, "下一次微调")
    finally:
        sculptor._llm_suggestion = original_llm
    assert stale["base_version"] == 2 and stale["stale"] is False

    server.update_persona(
        persona_id,
        server.PersonaUpdateRequest(summary="用户刚刚亲自调整过的资料"),
        {"id": user_id},
    )
    pending = next(item for item in sculptor.list_revision_suggestions(user_id, persona_id) if item["id"] == stale["id"])
    assert pending["stale"] is True
    try:
        sculptor.apply_revision_suggestion(user_id, stale["id"])
        raise AssertionError("stale suggestion should not overwrite a newer persona version")
    except ValueError as exc:
        assert "当前人格已为 v3" in str(exc)


def verify_chat_feedback_queues_candidate(server, sculptor, chat, archivist, user_id: int) -> None:
    original_forge = server.forge_persona
    original_chat_llm = chat.call_llm_api
    original_mirror = chat.update_interaction_insight
    original_semantic = chat.should_use_semantic_recall
    original_extraction_policy = archivist.should_use_llm_for_extraction
    server.forge_persona = lambda **kwargs: {**forged_persona(), "name": "南枝"}
    try:
        persona = server.create_persona(server.PersonaCreateRequest(description="quiet"), {"id": user_id})["persona"]
        persona_id = int(persona["id"])
        chat.call_llm_api = lambda *args, **kwargs: "好，我会说短一点。"
        chat.update_interaction_insight = lambda *args, **kwargs: {}
        chat.should_use_semantic_recall = lambda: False
        archivist.should_use_llm_for_extraction = lambda text: False

        chat.db_chat(
            user_id,
            persona_id,
            "你以后会不会忘记我？",
            client_message_id="phase2-feedback-0",
        )
        assert sculptor.list_revision_suggestions(user_id, persona_id) == []

        reply = chat.db_chat(
            user_id,
            persona_id,
            "你以后回复短一点，不要说教。",
            client_message_id="phase2-feedback-1",
        )
        assert reply["reply"] == "好，我会说短一点。"
        pending = sculptor.list_revision_suggestions(user_id, persona_id)
        assert len(pending) == 1
        candidate = pending[0]
        assert candidate["origin"] == "explicit_feedback"
        assert int(candidate["trigger_message_id"]) == int(reply["user_message_id"])
        assert candidate["trigger_memory_uids"]
        assert "会不会忘记我" not in candidate["suggestion"]["speaking_style"]

        chat.db_chat(
            user_id,
            persona_id,
            "你回复还是短一点就好。",
            client_message_id="phase2-feedback-2",
        )
        same_base = sculptor.list_revision_suggestions(user_id, persona_id)
        assert len(same_base) == 1
        candidate = same_base[0]
        assert len(candidate["trigger_memory_uids"]) >= 2
        assert "你回复还是短一点" in candidate["suggestion"]["speaking_style"]
        manual = sculptor.generate_revision_suggestion(
            user_id,
            persona_id,
            "管理员手动复核",
            use_llm=False,
        )
        assert manual["origin"] == "manual"
        assert len([item for item in sculptor.list_revision_suggestions(user_id, persona_id) if item["status"] == "pending"]) == 2

        sculptor.apply_revision_suggestion(user_id, int(candidate["id"]))
        chat.db_chat(
            user_id,
            persona_id,
            "以后少追问，好吗？",
            client_message_id="phase2-feedback-3",
        )
        revisions = sculptor.list_revision_suggestions(user_id, persona_id)
        assert next(item for item in revisions if item["id"] == manual["id"])["stale"]
        dismissed = sculptor.dismiss_revision_suggestion(
            user_id,
            int(manual["id"]),
            reviewer_user_id=user_id,
            decision_note="已由更贴近原始反馈的自动候选覆盖",
        )
        assert dismissed["status"] == "dismissed"
        assert dismissed["decision_note"] == "已由更贴近原始反馈的自动候选覆盖"
        assert int(dismissed["decided_by_user_id"]) == user_id and int(dismissed["decided_at"]) > 0
        next_pending = [item for item in revisions if item["status"] == "pending" and not item["stale"]]
        assert len(next_pending) == 1
        assert int(next_pending[0]["base_version"]) == int(candidate["base_version"]) + 1
        assert next_pending[0]["suggestion"]["speaking_style"].count("你以后回复短一点") == 1
    finally:
        server.forge_persona = original_forge
        chat.call_llm_api = original_chat_llm
        chat.update_interaction_insight = original_mirror
        chat.should_use_semantic_recall = original_semantic
        archivist.should_use_llm_for_extraction = original_extraction_policy


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database.DB_PATH = Path(tmp) / "phase2.db"
        import app.sculptor as sculptor
        import app.archivist as archivist
        import app.db_chat as chat
        import app.server as server

        database.init_db()
        user_id = seed_user()
        verify_growth_chain(server, sculptor, user_id)
        verify_chat_feedback_queues_candidate(server, sculptor, chat, archivist, user_id)
    print("Phase 2 persona growth verification passed")


if __name__ == "__main__":
    main()
