from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
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
    assert growth["reviewed_changes"] == [
        {
            "version": 2,
            "created_at": growth["latest_reviewed_change"]["created_at"],
            "highlights": ["回应方式更贴近你的偏好"],
            "feedback": None,
        }
    ]
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
    assert growth["latest_reviewed_change"]["feedback"] is None
    feedback = server.set_persona_growth_feedback(
        persona_id,
        server.PersonaGrowthFeedbackRequest(reaction="helpful"),
        {"id": user_id},
    )["feedback"]
    assert feedback["reviewed_version"] == 2 and feedback["reaction"] == "helpful"
    growth = server.persona_growth(persona_id, {"id": user_id})["growth"]
    assert growth["latest_reviewed_change"]["feedback"]["reaction"] == "helpful"
    assert growth["reviewed_changes"][0]["feedback"]["reaction"] == "helpful"
    server.set_persona_growth_feedback(
        persona_id,
        server.PersonaGrowthFeedbackRequest(reaction="needs_adjustment", detail="还是太爱追问，希望留一点空间"),
        {"id": user_id},
    )
    admin_growth = server.admin_persona_growth({"id": user_id}, user_id, persona_id)
    assert len(admin_growth["user_feedback"]) == 1
    assert admin_growth["user_feedback"][0]["reviewed_version"] == 2
    assert admin_growth["user_feedback"][0]["reaction"] == "needs_adjustment"
    assert admin_growth["user_feedback"][0]["detail_text"] == "还是太爱追问，希望留一点空间"
    growth = server.persona_growth(persona_id, {"id": user_id})["growth"]
    assert growth["latest_reviewed_change"]["feedback"]["detail_text"] == "还是太爱追问，希望留一点空间"
    assert growth["latest_reviewed_change"]["feedback"]["followup_status"] == "waiting"
    assert growth["reviewed_changes"][0]["feedback"] == {
        "reaction": "needs_adjustment",
        "followup_status": "waiting",
    }
    feedback_queue = next(item for item in server.admin_personas({"id": user_id}, user_id)["personas"] if int(item["id"]) == persona_id)
    assert int(feedback_queue["adjustment_feedback_count"]) == 1
    user_feedback_queue = next(item for item in server.admin_users({"id": user_id})["users"] if int(item["id"]) == user_id)
    assert int(user_feedback_queue["adjustment_feedback_count"]) == 1
    resolved = server.admin_resolve_persona_growth_feedback(
        server.PersonaGrowthFeedbackResolutionRequest(reviewed_version=2, note="已记录，下次调整减少追问"),
        {"id": user_id},
        user_id,
        persona_id,
    )["feedback"]
    assert int(resolved["resolved_at"]) > 0 and int(resolved["resolved_by_user_id"]) == user_id
    assert resolved["resolution_note"] == "已记录，下次调整减少追问"
    growth = server.persona_growth(persona_id, {"id": user_id})["growth"]
    public_followup = growth["latest_reviewed_change"]["feedback"]
    assert public_followup["followup_status"] == "completed"
    assert int(public_followup["followed_up_at"]) == int(resolved["resolved_at"])
    assert "已记录，下次调整减少追问" not in json.dumps(growth, ensure_ascii=False)
    assert growth["reviewed_changes"][0]["feedback"] == {
        "reaction": "needs_adjustment",
        "followup_status": "completed",
        "followed_up_at": int(resolved["resolved_at"]),
    }
    feedback_queue = next(item for item in server.admin_personas({"id": user_id}, user_id)["personas"] if int(item["id"]) == persona_id)
    assert int(feedback_queue["adjustment_feedback_count"]) == 0
    server.set_persona_growth_feedback(
        persona_id,
        server.PersonaGrowthFeedbackRequest(reaction="needs_adjustment", detail="追问还是多了些"),
        {"id": user_id},
    )
    growth = server.persona_growth(persona_id, {"id": user_id})["growth"]
    assert growth["latest_reviewed_change"]["feedback"]["followup_status"] == "waiting"
    assert "followed_up_at" not in growth["latest_reviewed_change"]["feedback"]
    reopened = server.admin_persona_growth({"id": user_id}, user_id, persona_id)["user_feedback"][0]
    request = server.submit_persona_preference_request(
        persona_id,
        server.PersonaPreferenceRequest(detail="难过的时候先陪我，不要马上分析原因"),
        {"id": user_id},
    )["request"]
    assert request["status"] == "waiting_review"
    assert request["id"] > 0
    growth = server.persona_growth(persona_id, {"id": user_id})["growth"]
    assert growth["preference_requests"][0]["detail"] == "难过的时候先陪我，不要马上分析原因"
    assert growth["preference_requests"][0]["status"] == "waiting_review"
    admin_growth = server.admin_persona_growth({"id": user_id}, user_id, persona_id)
    assert admin_growth["preference_requests"][0]["request_text"] == "难过的时候先陪我，不要马上分析原因"
    assert admin_growth["preference_requests"][0]["suggestion_status"] == "pending"
    with database.get_db() as db:
        direct_fact = db.execute(
            """
            SELECT text, priority
            FROM memory_facts
            WHERE user_id = ? AND persona_id = ? AND text LIKE '用户主动提出的相处偏好：%'
            ORDER BY id DESC LIMIT 1
            """,
            (user_id, persona_id),
        ).fetchone()
        queued = db.execute(
            """
            SELECT id, status, origin, base_version
            FROM persona_revision_suggestions
            WHERE user_id = ? AND persona_id = ? AND origin = 'profile_request'
            ORDER BY id DESC LIMIT 1
            """,
            (user_id, persona_id),
        ).fetchone()
    assert "难过的时候先陪我" in direct_fact["text"] and direct_fact["priority"] == "high"
    assert queued["status"] == "pending" and queued["origin"] == "profile_request"
    assert int(queued["base_version"]) == 2
    request_queue = next(item for item in server.admin_personas({"id": user_id}, user_id)["personas"] if int(item["id"]) == persona_id)
    assert int(request_queue["pending_preference_request_count"]) == 1
    user_request_queue = next(item for item in server.admin_users({"id": user_id})["users"] if int(item["id"]) == user_id)
    assert int(user_request_queue["pending_preference_request_count"]) == 1
    revised = server.submit_persona_preference_request(
        persona_id,
        server.PersonaPreferenceRequest(detail="难过的时候只陪着我，不要分析也不要追问"),
        {"id": user_id},
    )
    assert revised["updated"] is True and revised["request"]["id"] == request["id"]
    growth = server.persona_growth(persona_id, {"id": user_id})["growth"]
    assert growth["preference_requests"][0]["detail"] == "难过的时候只陪着我，不要分析也不要追问"
    assert len(growth["preference_requests"]) == 1
    with database.get_db() as db:
        direct_facts = db.execute(
            """
            SELECT text
            FROM memory_facts
            WHERE user_id = ? AND persona_id = ? AND text LIKE '用户主动提出的相处偏好：%'
            """,
            (user_id, persona_id),
        ).fetchall()
    assert len(direct_facts) == 1
    assert "不要分析也不要追问" in direct_facts[0]["text"]
    withdrawn = server.withdraw_persona_preference_request(persona_id, request["id"], {"id": user_id})["request"]
    assert withdrawn["status"] == "withdrawn"
    growth = server.persona_growth(persona_id, {"id": user_id})["growth"]
    assert growth["preference_requests"][0]["status"] == "withdrawn"
    with database.get_db() as db:
        archived_fact = db.execute(
            """
            SELECT archived, valid_to
            FROM memory_facts
            WHERE user_id = ? AND persona_id = ? AND text LIKE '用户主动提出的相处偏好：%'
            """,
            (user_id, persona_id),
        ).fetchone()
    assert int(archived_fact["archived"]) == 1 and int(archived_fact["valid_to"]) > 0
    request_queue = next(item for item in server.admin_personas({"id": user_id}, user_id)["personas"] if int(item["id"]) == persona_id)
    assert int(request_queue["pending_preference_request_count"]) == 0
    assert int(reopened["resolved_at"]) == 0 and reopened["resolution_note"] == ""
    assert reopened["detail_text"] == "追问还是多了些"
    feedback_queue = next(item for item in server.admin_personas({"id": user_id}, user_id)["personas"] if int(item["id"]) == persona_id)
    assert int(feedback_queue["adjustment_feedback_count"]) == 1

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
        queue = next(item for item in server.admin_personas({"id": user_id}, user_id)["personas"] if int(item["id"]) == persona_id)
        assert int(queue["pending_revision_count"]) == 2
        assert int(queue["pending_auto_revision_count"]) == 1
        assert int(queue["stale_revision_count"]) == 0
        assert int(queue["adjustment_feedback_count"]) == 0
        user_queue = next(item for item in server.admin_users({"id": user_id})["users"] if int(item["id"]) == user_id)
        assert int(user_queue["pending_revision_count"]) == 2
        assert int(user_queue["pending_auto_revision_count"]) == 1
        assert int(user_queue["stale_revision_count"]) == 1
        assert int(user_queue["adjustment_feedback_count"]) == 1

        sculptor.apply_revision_suggestion(user_id, int(candidate["id"]))
        chat.db_chat(
            user_id,
            persona_id,
            "以后少追问，好吗？",
            client_message_id="phase2-feedback-3",
        )
        revisions = sculptor.list_revision_suggestions(user_id, persona_id)
        assert next(item for item in revisions if item["id"] == manual["id"])["stale"]
        queue = next(item for item in server.admin_personas({"id": user_id}, user_id)["personas"] if int(item["id"]) == persona_id)
        assert int(queue["pending_revision_count"]) == 1
        assert int(queue["pending_auto_revision_count"]) == 1
        assert int(queue["stale_revision_count"]) == 1
        assert int(queue["adjustment_feedback_count"]) == 0
        dismissed = sculptor.dismiss_revision_suggestion(
            user_id,
            int(manual["id"]),
            reviewer_user_id=user_id,
            decision_note="已由更贴近原始反馈的自动候选覆盖",
        )
        assert dismissed["status"] == "dismissed"
        assert dismissed["decision_note"] == "已由更贴近原始反馈的自动候选覆盖"
        assert int(dismissed["decided_by_user_id"]) == user_id and int(dismissed["decided_at"]) > 0
        queue = next(item for item in server.admin_personas({"id": user_id}, user_id)["personas"] if int(item["id"]) == persona_id)
        assert int(queue["pending_revision_count"]) == 1
        assert int(queue["pending_auto_revision_count"]) == 1
        assert int(queue["stale_revision_count"]) == 0
        assert int(queue["adjustment_feedback_count"]) == 0
        user_queue = next(item for item in server.admin_users({"id": user_id})["users"] if int(item["id"]) == user_id)
        assert int(user_queue["pending_revision_count"]) == 1
        assert int(user_queue["pending_auto_revision_count"]) == 1
        assert int(user_queue["stale_revision_count"]) == 1
        assert int(user_queue["adjustment_feedback_count"]) == 1
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


def verify_applied_preference_request_receipt(server, sculptor, user_id: int) -> None:
    original_forge = server.forge_persona
    server.forge_persona = lambda **kwargs: {**forged_persona(), "name": "微澜"}
    try:
        persona = server.create_persona(server.PersonaCreateRequest(description="gentle"), {"id": user_id})["persona"]
    finally:
        server.forge_persona = original_forge
    persona_id = int(persona["id"])
    request = server.submit_persona_preference_request(
        persona_id,
        server.PersonaPreferenceRequest(detail="安慰我时说简短一点，先听我说完"),
        {"id": user_id},
    )["request"]
    admin_growth = server.admin_persona_growth({"id": user_id}, user_id, persona_id)
    suggestion_id = int(admin_growth["preference_requests"][0]["suggestion_id"])
    sculptor.apply_revision_suggestion(
        user_id,
        suggestion_id,
        reviewer_user_id=user_id,
        decision_note="内部审核备注不得出现在普通端结果回执",
    )
    growth = server.persona_growth(persona_id, {"id": user_id})["growth"]
    receipt = next(item for item in growth["preference_requests"] if int(item["id"]) == int(request["id"]))
    assert receipt["status"] == "confirmed"
    assert receipt["result"]["version"] == 2
    assert receipt["result"]["highlights"] == ["回应方式更贴近你的偏好"]
    assert "内部审核备注" not in json.dumps(receipt, ensure_ascii=False)


def verify_stale_preference_request_retry(server, sculptor, user_id: int) -> None:
    original_forge = server.forge_persona
    server.forge_persona = lambda **kwargs: {**forged_persona(), "name": "待续"}
    try:
        persona = server.create_persona(server.PersonaCreateRequest(description="retry"), {"id": user_id})["persona"]
    finally:
        server.forge_persona = original_forge
    persona_id = int(persona["id"])
    request = server.submit_persona_preference_request(
        persona_id,
        server.PersonaPreferenceRequest(detail="我安静的时候先陪我待一会儿，不要立刻追问"),
        {"id": user_id},
    )["request"]
    original_suggestion_id = int(
        server.admin_persona_growth({"id": user_id}, user_id, persona_id)["preference_requests"][0]["suggestion_id"]
    )
    card = next(item for item in server.personas({"id": user_id})["personas"] if int(item["id"]) == persona_id)
    assert card["growth_action"] is None
    server.update_persona(
        persona_id,
        server.PersonaUpdateRequest(summary="最近更愿意慢慢交流"),
        {"id": user_id},
    )
    stale_item = server.persona_growth(persona_id, {"id": user_id})["growth"]["preference_requests"][0]
    assert stale_item["status"] == "needs_review_again"
    assert stale_item["can_retry"] is True and stale_item["can_withdraw"] is True
    card = next(item for item in server.personas({"id": user_id})["personas"] if int(item["id"]) == persona_id)
    assert card["growth_action"] == {
        "kind": "preference_retry",
        "title": "有偏好需要重新确认",
        "count": 1,
    }
    queue = next(item for item in server.admin_personas({"id": user_id}, user_id)["personas"] if int(item["id"]) == persona_id)
    assert int(queue["stale_revision_count"]) == 1
    assert int(queue["cleanable_stale_revision_count"]) == 0
    protected = next(
        item for item in server.admin_persona_revisions({"id": user_id}, user_id, persona_id)["suggestions"]
        if int(item["id"]) == original_suggestion_id
    )
    assert protected["protected_by_active_request"] is True
    skipped = server.admin_dismiss_stale_persona_revisions({"id": user_id}, user_id, persona_id)
    assert skipped["dismissed_count"] == 0
    assert next(
        item for item in sculptor.list_revision_suggestions(user_id, persona_id)
        if int(item["id"]) == original_suggestion_id
    )["status"] == "pending"

    retried = server.retry_persona_preference_request(persona_id, request["id"], {"id": user_id})["request"]
    assert retried["id"] == request["id"] and retried["status"] == "waiting_review"
    growth = server.persona_growth(persona_id, {"id": user_id})["growth"]
    refreshed = next(item for item in growth["preference_requests"] if int(item["id"]) == int(request["id"]))
    assert refreshed["status"] == "waiting_review" and refreshed["can_retry"] is False
    card = next(item for item in server.personas({"id": user_id})["personas"] if int(item["id"]) == persona_id)
    assert card["growth_action"] is None
    admin_request = server.admin_persona_growth({"id": user_id}, user_id, persona_id)["preference_requests"][0]
    active_suggestion_id = int(admin_request["suggestion_id"])
    assert active_suggestion_id != original_suggestion_id
    revisions = sculptor.list_revision_suggestions(user_id, persona_id)
    original = next(item for item in revisions if int(item["id"]) == original_suggestion_id)
    active = next(item for item in revisions if int(item["id"]) == active_suggestion_id)
    assert original["status"] == "pending" and original["stale"] is True
    assert active["status"] == "pending" and active["stale"] is False and int(active["base_version"]) == 2
    queue = next(item for item in server.admin_personas({"id": user_id}, user_id)["personas"] if int(item["id"]) == persona_id)
    assert int(queue["stale_revision_count"]) == 1
    assert int(queue["cleanable_stale_revision_count"]) == 1
    old_in_admin = next(
        item for item in server.admin_persona_revisions({"id": user_id}, user_id, persona_id)["suggestions"]
        if int(item["id"]) == original_suggestion_id
    )
    assert old_in_admin["protected_by_active_request"] is False
    cleaned = server.admin_dismiss_stale_persona_revisions({"id": user_id}, user_id, persona_id)
    assert cleaned["dismissed_count"] == 1
    assert cleaned["suggestions"][0]["decision_note"] == "人格版本已更新，批量关闭不再可执行的过期建议"
    revisions = sculptor.list_revision_suggestions(user_id, persona_id)
    original = next(item for item in revisions if int(item["id"]) == original_suggestion_id)
    active = next(item for item in revisions if int(item["id"]) == active_suggestion_id)
    assert original["status"] == "dismissed"
    assert active["status"] == "pending" and active["stale"] is False
    with database.get_db() as db:
        facts = db.execute(
            """
            SELECT id
            FROM memory_facts
            WHERE user_id = ? AND persona_id = ? AND type = 'persona_feedback'
              AND text LIKE '用户主动提出的相处偏好：%'
            """,
            (user_id, persona_id),
        ).fetchall()
    assert len(facts) == 1


def verify_growth_demo_sandbox(server, growth_demo, admin_id: int) -> None:
    demo = server.admin_seed_growth_demo({"id": admin_id})["demo"]
    demo_user_id = int(demo["user_id"])
    persona_id = int(demo["persona_id"])
    assert demo["username"] == growth_demo.DEMO_USERNAME
    assert demo["password"] == growth_demo.DEMO_PASSWORD

    persona = next(item for item in server.admin_personas({"id": admin_id}, demo_user_id)["personas"] if int(item["id"]) == persona_id)
    assert persona["name"] == "栖夏" and int(persona["version"]) == 2
    assert int(persona["pending_revision_count"]) == 1
    assert int(persona["pending_auto_revision_count"]) == 1
    assert int(persona["pending_preference_request_count"]) == 1
    assert int(persona["adjustment_feedback_count"]) == 1

    growth = server.persona_growth(persona_id, {"id": demo_user_id})["growth"]
    assert growth["latest_reviewed_change"]["version"] == 2
    assert growth["latest_reviewed_change"]["feedback"]["reaction"] == "needs_adjustment"
    assert "不要立刻分析" in growth["latest_reviewed_change"]["feedback"]["detail_text"]
    assert growth["latest_reviewed_change"]["feedback"]["followup_status"] == "waiting"
    assert growth["reviewed_changes"][0]["version"] == 2
    assert growth["reviewed_changes"][0]["feedback"] == {
        "reaction": "needs_adjustment",
        "followup_status": "waiting",
    }
    assert growth["preference_requests"][0]["status"] == "waiting_review"
    assert growth["preference_requests"][0]["can_withdraw"] is True
    assert "不要马上替我分析" in growth["preference_requests"][0]["detail"]
    admin_growth = server.admin_persona_growth({"id": admin_id}, demo_user_id, persona_id)
    assert admin_growth["preference_requests"][0]["suggestion_status"] == "pending"
    assert "不要马上替我分析" in admin_growth["preference_requests"][0]["request_text"]
    card = next(item for item in server.personas({"id": demo_user_id})["personas"] if int(item["id"]) == persona_id)
    assert card["growth_notice"]["version"] == 2

    cleared = server.admin_clear_growth_demo({"id": admin_id})["demo"]
    assert cleared["removed"] is True
    assert not any(item["username"] == growth_demo.DEMO_USERNAME for item in server.admin_users({"id": admin_id})["users"])


def verify_sparse_profile_discovery_policy(mirror, chat, user_id: int) -> None:
    mirror.update_user_insight(
        user_id,
        profile_summary="用户明确说过喜欢饭团。",
        interaction_style=[],
        emotional_patterns=[],
        inferred_profile={},
        topic_model={"likes": ["饭团"], "dislikes": [], "avoid_topics": [], "safe_topics": ["饭团"]},
        guidance={"tone_rules": [], "topic_rules": [], "support_rules": [], "do_not": []},
    )
    prompt = mirror.discovery_prompt(user_id)
    assert "not a default conversational hook" in prompt
    assert "profile is currently sparse" in prompt
    assert "learning a different dimension" in prompt
    assert "direct or somewhat personal" in prompt and "easy to decline" in prompt
    assert "Do not push after hesitation or refusal" in prompt
    guarded = mirror.discovery_prompt(
        user_id,
        recent_assistant_messages=["你说喜欢饭团，那晚饭还会想吃饭团吗？"],
        current_user_text="我今天有点累。",
    )
    assert '["饭团"]' in guarded
    assert "Do not bring these topics back in this reply" in guarded
    relevant = mirror.discovery_prompt(
        user_id,
        recent_assistant_messages=["你说喜欢饭团。"],
        current_user_text="我现在想吃饭团。",
    )
    assert "Do not bring these topics back in this reply" not in relevant
    requested_profile = chat._profile_usage_prompt(
        "今天是个特别的日子，你看看我的信息",
        current_time=datetime(2026, 5, 25, 13, 0, tzinfo=timezone.utc),
    )
    assert "current_local_date: 2026-05-25" in requested_profile
    assert "This turn invites checking the saved user profile" in requested_profile
    assert "Do not add unrequested memories" in requested_profile
    assert "Today is the user's birthday" not in requested_profile
    ordinary_turn = chat._profile_usage_prompt(
        "今天有点累。",
        current_time=datetime(2026, 5, 26, 13, 0, tzinfo=timezone.utc),
    )
    assert "This turn does not explicitly invite profile lookup" in ordinary_turn
    assert "birthday" not in ordinary_turn.lower()


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database.DB_PATH = Path(tmp) / "phase2.db"
        import app.sculptor as sculptor
        import app.archivist as archivist
        import app.db_chat as chat
        import app.growth_demo as growth_demo
        import app.mirror as mirror
        import app.server as server

        database.init_db()
        user_id = seed_user()
        verify_growth_chain(server, sculptor, user_id)
        verify_chat_feedback_queues_candidate(server, sculptor, chat, archivist, user_id)
        verify_applied_preference_request_receipt(server, sculptor, user_id)
        verify_stale_preference_request_retry(server, sculptor, user_id)
        verify_growth_demo_sandbox(server, growth_demo, user_id)
        verify_sparse_profile_discovery_policy(mirror, chat, user_id)
    print("Phase 2 persona growth verification passed")


if __name__ == "__main__":
    main()
