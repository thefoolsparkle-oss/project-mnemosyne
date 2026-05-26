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


def verify_growth_chain(server, sculptor, chat, user_id: int) -> None:
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
    assert growth["latest_reviewed_change"]["feedback"]["followup_status"] == "completed"
    assert growth["reviewed_changes"][0]["feedback"] == {
        "reaction": "needs_adjustment",
        "followup_status": "completed",
        "followed_up_at": growth["latest_reviewed_change"]["feedback"]["followed_up_at"],
    }
    feedback_queue = next(item for item in server.admin_personas({"id": user_id}, user_id)["personas"] if int(item["id"]) == persona_id)
    assert int(feedback_queue["adjustment_feedback_count"]) == 0
    user_feedback_queue = next(item for item in server.admin_users({"id": user_id})["users"] if int(item["id"]) == user_id)
    assert int(user_feedback_queue["adjustment_feedback_count"]) == 0
    server.set_persona_growth_feedback(
        persona_id,
        server.PersonaGrowthFeedbackRequest(reaction="needs_adjustment", detail="追问还是多了些"),
        {"id": user_id},
    )
    growth = server.persona_growth(persona_id, {"id": user_id})["growth"]
    assert growth["latest_reviewed_change"]["feedback"]["followup_status"] == "completed"
    assert len(growth["preference_requests"]) == 1
    assert growth["preference_requests"][0]["origin"] == "growth_feedback"
    assert int(growth["preference_requests"][0]["source_reviewed_version"]) == 2
    assert growth["preference_requests"][0]["detail"] == "追问还是多了些"
    request = server.submit_persona_preference_request(
        persona_id,
        server.PersonaPreferenceRequest(detail="难过的时候先陪我，不要马上分析原因"),
        {"id": user_id},
    )["request"]
    assert request["status"] == "active_guidance"
    assert request["id"] > 0
    growth = server.persona_growth(persona_id, {"id": user_id})["growth"]
    assert growth["preference_requests"][0]["detail"] == "难过的时候先陪我，不要马上分析原因"
    assert growth["preference_requests"][0]["status"] == "active_guidance"
    assert growth["preference_requests"][0]["origin"] == "direct_entry"
    assert len(growth["preference_requests"]) == 2
    assert growth["preference_requests"][1]["origin"] == "growth_feedback"
    admin_growth = server.admin_persona_growth({"id": user_id}, user_id, persona_id)
    assert admin_growth["preference_requests"][0]["request_text"] == "难过的时候先陪我，不要马上分析原因"
    assert admin_growth["preference_requests"][0]["suggestion_id"] is None
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
    assert "难过的时候先陪我" in direct_fact["text"] and direct_fact["priority"] == "high"
    request_queue = next(item for item in server.admin_personas({"id": user_id}, user_id)["personas"] if int(item["id"]) == persona_id)
    assert int(request_queue["pending_preference_request_count"]) == 0
    user_request_queue = next(item for item in server.admin_users({"id": user_id})["users"] if int(item["id"]) == user_id)
    assert int(user_request_queue["pending_preference_request_count"]) == 0
    revised = server.submit_persona_preference_request(
        persona_id,
        server.PersonaPreferenceRequest(detail="难过的时候只陪着我，不要分析也不要追问"),
        {"id": user_id},
    )
    assert revised["updated"] is True and revised["request"]["id"] == request["id"]
    growth = server.persona_growth(persona_id, {"id": user_id})["growth"]
    assert growth["preference_requests"][0]["detail"] == "难过的时候只陪着我，不要分析也不要追问"
    assert len(growth["preference_requests"]) == 2
    assert growth["preference_requests"][1]["detail"] == "追问还是多了些"
    active_prompt = chat._active_preference_prompt(user_id, persona_id)
    assert "难过的时候只陪着我，不要分析也不要追问" in active_prompt
    assert "追问还是多了些" in active_prompt
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
    assert growth["preference_requests"][1]["status"] == "active_guidance"
    active_prompt = chat._active_preference_prompt(user_id, persona_id)
    assert "难过的时候只陪着我，不要分析也不要追问" not in active_prompt
    assert "追问还是多了些" in active_prompt
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
    feedback_queue = next(item for item in server.admin_personas({"id": user_id}, user_id)["personas"] if int(item["id"]) == persona_id)
    assert int(feedback_queue["adjustment_feedback_count"]) == 0

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
        chat_prompts = []

        def answer_with_trace(messages, **kwargs):
            chat_prompts.append(messages)
            return "好，我会说短一点。"

        chat.call_llm_api = answer_with_trace
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
        revisions = sculptor.list_revision_suggestions(user_id, persona_id)
        assert len(revisions) == 1
        candidate = revisions[0]
        assert candidate["origin"] == "explicit_feedback"
        assert candidate["status"] == "applied"
        assert candidate["decision_actor"] == "review_agent"
        assert candidate["decision_note"] == sculptor.AUTO_REVIEW_NOTE
        assert int(candidate["applied_version"]) == 2
        assert int(candidate["trigger_message_id"]) == int(reply["user_message_id"])
        assert candidate["trigger_memory_uids"]
        assert "会不会忘记我" not in candidate["suggestion"]["speaking_style"]
        assert any("你以后回复短一点" in item["content"] for item in chat_prompts[-1] if item["role"] == "system")
        queue = next(item for item in server.admin_personas({"id": user_id}, user_id)["personas"] if int(item["id"]) == persona_id)
        assert int(queue["pending_revision_count"]) == 0
        assert int(queue["pending_auto_revision_count"]) == 0

        chat.db_chat(
            user_id,
            persona_id,
            "你回复还是短一点就好。",
            client_message_id="phase2-feedback-2",
        )
        revised = sculptor.list_revision_suggestions(user_id, persona_id)
        assert len(revised) == 2
        followup = revised[0]
        assert followup["status"] == "applied" and followup["decision_actor"] == "review_agent"
        assert int(followup["base_version"]) == 2 and int(followup["applied_version"]) == 3
        assert "你回复还是短一点" in followup["suggestion"]["speaking_style"]
        manual = sculptor.generate_revision_suggestion(
            user_id,
            persona_id,
            "管理员手动复核",
            use_llm=False,
        )
        assert manual["origin"] == "manual"
        assert len([item for item in sculptor.list_revision_suggestions(user_id, persona_id) if item["status"] == "pending"]) == 1
        queue = next(item for item in server.admin_personas({"id": user_id}, user_id)["personas"] if int(item["id"]) == persona_id)
        assert int(queue["pending_revision_count"]) == 1
        assert int(queue["pending_auto_revision_count"]) == 0
        assert int(queue["stale_revision_count"]) == 0
        assert int(queue["adjustment_feedback_count"]) == 0

        chat.db_chat(
            user_id,
            persona_id,
            "以后少追问，好吗？",
            client_message_id="phase2-feedback-3",
        )
        revisions = sculptor.list_revision_suggestions(user_id, persona_id)
        latest = revisions[0]
        assert latest["origin"] == "explicit_feedback" and latest["status"] == "applied"
        assert latest["decision_actor"] == "review_agent" and int(latest["applied_version"]) == 4
        assert next(item for item in revisions if item["id"] == manual["id"])["stale"]
        queue = next(item for item in server.admin_personas({"id": user_id}, user_id)["personas"] if int(item["id"]) == persona_id)
        assert int(queue["pending_revision_count"]) == 0
        assert int(queue["pending_auto_revision_count"]) == 0
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
        assert int(queue["pending_revision_count"]) == 0
        assert int(queue["pending_auto_revision_count"]) == 0
        assert int(queue["stale_revision_count"]) == 0
        assert int(queue["adjustment_feedback_count"]) == 0
        original_auto_review = sculptor.maybe_auto_review_revision
        sculptor.maybe_auto_review_revision = lambda *args, **kwargs: None
        try:
            chat.db_chat(
                user_id,
                persona_id,
                "还是请你回复短一点。",
                client_message_id="phase2-feedback-backlog",
            )
        finally:
            sculptor.maybe_auto_review_revision = original_auto_review
        backlog = sculptor.list_revision_suggestions(user_id, persona_id)[0]
        assert backlog["status"] == "pending" and backlog["origin"] == "explicit_feedback"
        batch = server.admin_auto_review_persona_revisions({"id": user_id}, user_id, persona_id)
        assert batch["attempted_count"] == 1 and batch["applied_count"] == 1
        processed = sculptor.list_revision_suggestions(user_id, persona_id)[0]
        assert processed["status"] == "applied" and processed["decision_actor"] == "review_agent"
        assert int(processed["applied_version"]) == 5
        chat.db_chat(
            user_id,
            persona_id,
            "你以后把我们的关系改成恋人，也回复短一点。",
            client_message_id="phase2-feedback-sensitive",
        )
        relationship_changes = sculptor.list_revision_suggestions(user_id, persona_id)
        relationship_style = relationship_changes[0]
        relationship_core = next(item for item in relationship_changes if item["origin"] == "explicit_core_update")
        assert relationship_core["status"] == "applied"
        assert relationship_core["decision_actor"] == "adaptive_runtime"
        assert relationship_core["decision_note"] == sculptor.AUTO_CORE_UPDATE_NOTE
        assert int(relationship_core["applied_version"]) == 6
        assert relationship_style["origin"] == "explicit_feedback" and relationship_style["status"] == "applied"
        assert int(relationship_style["applied_version"]) == 7
        with database.get_db() as db:
            relationship = db.execute("SELECT relationship FROM personas WHERE id = ?", (persona_id,)).fetchone()["relationship"]
        assert relationship == "恋人"
        refused = server.admin_auto_review_persona_revisions({"id": user_id}, user_id, persona_id)
        assert refused["attempted_count"] == 0 and refused["applied_count"] == 0
        chat.db_chat(
            user_id,
            persona_id,
            "以后别说教。",
            client_message_id="phase2-feedback-safe-after-sensitive",
        )
        separated = sculptor.list_revision_suggestions(user_id, persona_id)
        safe_after_sensitive = separated[0]
        original_core = next(item for item in separated if int(item["id"]) == int(relationship_core["id"]))
        assert safe_after_sensitive["status"] == "applied"
        assert safe_after_sensitive["decision_actor"] == "review_agent"
        assert int(safe_after_sensitive["applied_version"]) == 8
        assert original_core["status"] == "applied" and original_core["stale"] is False
        before_duplicate_count = len(separated)
        chat.db_chat(
            user_id,
            persona_id,
            "以后别说教。",
            client_message_id="phase2-feedback-already-covered",
        )
        assert len(sculptor.list_revision_suggestions(user_id, persona_id)) == before_duplicate_count
        chat.db_chat(
            user_id,
            persona_id,
            "把你的名字改成小舟。",
            client_message_id="phase2-core-name",
        )
        renamed = sculptor.list_revision_suggestions(user_id, persona_id)[0]
        assert renamed["origin"] == "explicit_core_update" and renamed["status"] == "applied"
        assert int(renamed["applied_version"]) == 9
        with database.get_db() as db:
            name = db.execute("SELECT name FROM personas WHERE id = ?", (persona_id,)).fetchone()["name"]
        assert name == "小舟"
        assert sculptor._explicit_core_updates("不要把我们的关系改成朋友。") == {}
        assert sculptor._explicit_core_updates("别把你的名字改成北川。") == {}
        chat.db_chat(
            user_id,
            persona_id,
            "以后你就是我的女朋友吧。",
            client_message_id="phase2-core-natural-relationship",
        )
        natural_relationship = next(
            item for item in sculptor.list_revision_suggestions(user_id, persona_id)
            if item["origin"] == "explicit_core_update" and int(item["applied_version"] or 0) == 10
        )
        assert natural_relationship["status"] == "applied"
        with database.get_db() as db:
            relationship = db.execute("SELECT relationship FROM personas WHERE id = ?", (persona_id,)).fetchone()["relationship"]
        assert relationship == "女朋友"
        chat.db_chat(
            user_id,
            persona_id,
            "我以后叫你阿澄吧。",
            client_message_id="phase2-core-natural-name",
        )
        natural_name = next(
            item for item in sculptor.list_revision_suggestions(user_id, persona_id)
            if item["origin"] == "explicit_core_update" and int(item["applied_version"] or 0) == 11
        )
        assert natural_name["status"] == "applied"
        core_change_count = len(
            [item for item in sculptor.list_revision_suggestions(user_id, persona_id) if item["origin"] == "explicit_core_update"]
        )
        chat.db_chat(
            user_id,
            persona_id,
            "不要把我们的关系改成朋友，也不要把你的名字改成北川。",
            client_message_id="phase2-core-negated-command",
        )
        assert len(
            [item for item in sculptor.list_revision_suggestions(user_id, persona_id) if item["origin"] == "explicit_core_update"]
        ) == core_change_count
        with database.get_db() as db:
            current = db.execute("SELECT name, relationship, version FROM personas WHERE id = ?", (persona_id,)).fetchone()
        assert current["name"] == "阿澄" and current["relationship"] == "女朋友"
        assert int(current["version"]) == 11
        chat.db_chat(
            user_id,
            persona_id,
            "以后别像恋人那样说，回复短一点。",
            client_message_id="phase2-core-ambiguous",
        )
        ambiguous = sculptor.list_revision_suggestions(user_id, persona_id)[0]
        assert ambiguous["origin"] == "explicit_feedback" and ambiguous["status"] == "dismissed"
        assert ambiguous["decision_actor"] == "adaptive_runtime"
        with database.get_db() as db:
            relationship = db.execute("SELECT relationship FROM personas WHERE id = ?", (persona_id,)).fetchone()["relationship"]
        assert relationship == "女朋友"
        no_change_candidate = sculptor.generate_revision_suggestion(
            user_id,
            persona_id,
            "模拟历史积压：当前已满足的低风险聊天要求",
            use_llm=False,
            origin="explicit_feedback",
            trigger_memory_uids=safe_after_sensitive["trigger_memory_uids"],
        )
        no_change = sculptor.maybe_auto_review_revision(user_id, int(no_change_candidate["id"]))
        assert no_change and no_change["status"] == "dismissed"
        assert no_change["decision_actor"] == "review_agent"
        assert no_change["decision_note"] == sculptor.AUTO_REVIEW_NO_CHANGE_NOTE
        with database.get_db() as db:
            version = int(db.execute("SELECT version FROM personas WHERE id = ?", (persona_id,)).fetchone()["version"])
        assert version == 11
        assert sculptor._explicit_core_updates("我们分手吧。") == {"relationship": "关系未定"}
        assert sculptor._explicit_core_updates("以后你别再当我的女朋友了。") == {"relationship": "关系未定"}
        assert sculptor._explicit_core_updates("我们先不要定义关系了。") == {"relationship": "关系未定"}
        assert sculptor._explicit_core_updates("以后别像恋人那样说。") == {}
        chat.db_chat(
            user_id,
            persona_id,
            "我们分手吧。",
            client_message_id="phase2-core-relationship-exit",
        )
        ended_relationship = next(
            item for item in sculptor.list_revision_suggestions(user_id, persona_id)
            if item["origin"] == "explicit_core_update" and int(item["applied_version"] or 0) == 12
        )
        assert ended_relationship["status"] == "applied"
        assert ended_relationship["decision_actor"] == "adaptive_runtime"
        with database.get_db() as db:
            current = db.execute("SELECT relationship, version FROM personas WHERE id = ?", (persona_id,)).fetchone()
        assert current["relationship"] == "关系未定" and int(current["version"]) == 12
        reset_count = len(
            [item for item in sculptor.list_revision_suggestions(user_id, persona_id) if item["origin"] == "explicit_core_update"]
        )
        chat.db_chat(
            user_id,
            persona_id,
            "我们分手吧。",
            client_message_id="phase2-core-relationship-exit-repeat",
        )
        assert len(
            [item for item in sculptor.list_revision_suggestions(user_id, persona_id) if item["origin"] == "explicit_core_update"]
        ) == reset_count
    finally:
        server.forge_persona = original_forge
        chat.call_llm_api = original_chat_llm
        chat.update_interaction_insight = original_mirror
        chat.should_use_semantic_recall = original_semantic
        archivist.should_use_llm_for_extraction = original_extraction_policy


def verify_active_preference_guidance(server, chat, user_id: int) -> None:
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
    assert request["status"] == "active_guidance"
    growth = server.persona_growth(persona_id, {"id": user_id})["growth"]
    receipt = next(item for item in growth["preference_requests"] if int(item["id"]) == int(request["id"]))
    assert receipt["status"] == "active_guidance" and receipt["can_withdraw"] is True
    assert "安慰我时说简短一点" in chat._active_preference_prompt(user_id, persona_id)
    with database.get_db() as db:
        assert int(db.execute("SELECT version FROM personas WHERE id = ?", (persona_id,)).fetchone()["version"]) == 1
        assert db.execute(
            "SELECT COUNT(*) AS count FROM persona_revision_suggestions WHERE persona_id = ? AND status = 'pending'",
            (persona_id,),
        ).fetchone()["count"] == 0


def verify_active_preference_survives_profile_update(server, chat, user_id: int) -> None:
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
    assert request["status"] == "active_guidance"
    card = next(item for item in server.personas({"id": user_id})["personas"] if int(item["id"]) == persona_id)
    assert card["growth_action"] is None
    server.update_persona(
        persona_id,
        server.PersonaUpdateRequest(summary="最近更愿意慢慢交流"),
        {"id": user_id},
    )
    active_item = server.persona_growth(persona_id, {"id": user_id})["growth"]["preference_requests"][0]
    assert active_item["status"] == "active_guidance"
    assert active_item["can_retry"] is False and active_item["can_withdraw"] is True
    assert "我安静的时候先陪我待一会儿" in chat._active_preference_prompt(user_id, persona_id)
    card = next(item for item in server.personas({"id": user_id})["personas"] if int(item["id"]) == persona_id)
    assert card["growth_action"] is None
    queue = next(item for item in server.admin_personas({"id": user_id}, user_id)["personas"] if int(item["id"]) == persona_id)
    assert int(queue["pending_revision_count"]) == 0
    assert int(queue["pending_preference_request_count"]) == 0
    stopped = server.withdraw_persona_preference_request(persona_id, request["id"], {"id": user_id})["request"]
    assert stopped["status"] == "withdrawn"
    assert "我安静的时候先陪我待一会儿" not in chat._active_preference_prompt(user_id, persona_id)
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


def verify_newer_conflicting_guidance_supersedes_old(server, chat, user_id: int) -> None:
    original_forge = server.forge_persona
    server.forge_persona = lambda **kwargs: {**forged_persona(), "name": "知雨"}
    try:
        persona = server.create_persona(server.PersonaCreateRequest(description="gentle"), {"id": user_id})["persona"]
    finally:
        server.forge_persona = original_forge
    persona_id = int(persona["id"])
    older = server.submit_persona_preference_request(
        persona_id,
        server.PersonaPreferenceRequest(detail="安慰我的时候少追问，回复短一点"),
        {"id": user_id},
    )["request"]
    newer = server._store_persona_preference_guidance(
        persona_id,
        "安慰我的时候你可以多问我一点，回复详细一点",
        {"id": user_id},
        request_origin="growth_feedback",
        source_reviewed_version=2,
    )
    assert newer["superseded_request_ids"] == [older["id"]]
    growth = server.persona_growth(persona_id, {"id": user_id})["growth"]
    active = next(item for item in growth["preference_requests"] if item["status"] == "active_guidance")
    replaced = next(item for item in growth["preference_requests"] if int(item["id"]) == int(older["id"]))
    assert active["detail"] == "安慰我的时候你可以多问我一点，回复详细一点"
    assert replaced["status"] == "superseded"
    assert replaced["deactivation_reason"] == "已被用户较新的相处指导替代"
    admin_history = server.admin_persona_growth({"id": user_id}, user_id, persona_id)["preference_requests"]
    admin_replaced = next(item for item in admin_history if int(item["id"]) == int(older["id"]))
    assert admin_replaced["deactivation_actor"] == "adaptive_runtime"
    assert admin_replaced["deactivation_reason"] == "已被用户较新的相处指导替代"
    prompt = chat._active_preference_prompt(user_id, persona_id)
    assert "你可以多问我一点" in prompt and "回复详细一点" in prompt
    assert "少追问" not in prompt and "回复短一点" not in prompt
    with database.get_db() as db:
        old_memory = db.execute(
            """
            SELECT archived, valid_to
            FROM memory_facts
            WHERE user_id = ? AND persona_id = ? AND text LIKE '用户主动提出的相处偏好：%'
            ORDER BY id ASC LIMIT 1
            """,
            (user_id, persona_id),
        ).fetchone()
    assert int(old_memory["archived"]) == 1 and int(old_memory["valid_to"]) > 0


def verify_chat_guidance_replaces_older_chat_guidance(server, sculptor, chat, archivist, user_id: int) -> None:
    original_forge = server.forge_persona
    original_chat_llm = chat.call_llm_api
    original_mirror = chat.update_interaction_insight
    original_semantic = chat.should_use_semantic_recall
    original_extraction_policy = archivist.should_use_llm_for_extraction
    server.forge_persona = lambda **kwargs: {**forged_persona(), "name": "闻溪"}
    try:
        persona = server.create_persona(server.PersonaCreateRequest(description="gentle"), {"id": user_id})["persona"]
        persona_id = int(persona["id"])
        chat_prompts = []
        chat.call_llm_api = lambda messages, task="chat": chat_prompts.append(messages) or "好。"
        chat.update_interaction_insight = lambda *args, **kwargs: {}
        chat.should_use_semantic_recall = lambda: False
        archivist.should_use_llm_for_extraction = lambda *args, **kwargs: False
        chat.db_chat(
            user_id,
            persona_id,
            "以后回复短一点，少追问。",
            client_message_id="phase2-chat-guidance-old",
        )
        chat.db_chat(
            user_id,
            persona_id,
            "我改主意了，以后你可以多问我一点，回复详细一点。",
            client_message_id="phase2-chat-guidance-new",
        )
        growth = server.persona_growth(persona_id, {"id": user_id})["growth"]
        chat_guidance = [item for item in growth["preference_requests"] if item["origin"] == "chat_feedback"]
        assert len(chat_guidance) == 2
        assert chat_guidance[0]["status"] == "active_guidance"
        assert "多问我一点" in chat_guidance[0]["detail"]
        assert chat_guidance[1]["status"] == "superseded"
        assert chat_guidance[1]["deactivation_reason"] == "已被用户较新的相处指导替代"
        active_context = next(
            item["content"] for item in chat_prompts[-1]
            if item["role"] == "system" and "Active user companionship preferences" in item["content"]
        )
        assert "你可以多问我一点" in active_context and "回复详细一点" in active_context
        assert "以后回复短一点，少追问" not in active_context
        assert "override older stored style or support guidance" in active_context
        with database.get_db() as db:
            after_replacement_style = db.execute(
                "SELECT speaking_style FROM personas WHERE id = ?",
                (persona_id,),
            ).fetchone()["speaking_style"]
        assert "以后回复短一点" not in after_replacement_style and "少追问" not in after_replacement_style
        assert "回复详细一点" in after_replacement_style and "多问我一点" in after_replacement_style
        guidance_count = len(chat_guidance)
        revision_count = len(sculptor.list_revision_suggestions(user_id, persona_id))
        replacement_reconciles = [
            item for item in sculptor.list_revision_suggestions(user_id, persona_id)
            if item["origin"] == "guidance_reconcile"
        ]
        assert len(replacement_reconciles) == 1
        assert replacement_reconciles[0]["decision_actor"] == "adaptive_runtime"
        assert replacement_reconciles[0]["decision_note"] == sculptor.AUTO_GUIDANCE_RECONCILE_NOTE
        chat.db_chat(
            user_id,
            persona_id,
            "我不想让你回复短一点，只是在举例。",
            client_message_id="phase2-chat-guidance-negated",
        )
        chat.db_chat(
            user_id,
            persona_id,
            "你为什么总说回复短一点？",
            client_message_id="phase2-chat-guidance-discussion",
        )
        after_noise = server.persona_growth(persona_id, {"id": user_id})["growth"]
        assert len([item for item in after_noise["preference_requests"] if item["origin"] == "chat_feedback"]) == guidance_count
        assert len(sculptor.list_revision_suggestions(user_id, persona_id)) == revision_count
        chat.db_chat(
            user_id,
            persona_id,
            "回复详细一点这条不用了，多问我一点照旧。",
            client_message_id="phase2-chat-guidance-partial-stop",
        )
        after_partial_stop = server.persona_growth(persona_id, {"id": user_id})["growth"]
        partially_stopped = next(
            item for item in after_partial_stop["preference_requests"]
            if item["origin"] == "chat_feedback" and "多问我一点" in item["detail"] and "回复详细一点" in item["detail"]
        )
        retained = next(
            item for item in after_partial_stop["preference_requests"]
            if item["status"] == "active_guidance" and "多问我一点" in item["detail"] and "回复详细一点" not in item["detail"]
        )
        assert partially_stopped["status"] == "stopped_in_chat"
        assert partially_stopped["deactivation_reason"] == "用户在聊天中停止了这条指导的部分内容，未取消部分继续生效"
        assert retained["origin"] == "chat_feedback"
        assert len([item for item in after_partial_stop["preference_requests"] if item["origin"] == "chat_feedback"]) == guidance_count + 1
        after_partial_revisions = sculptor.list_revision_suggestions(user_id, persona_id)
        assert len(after_partial_revisions) == revision_count + 1
        assert after_partial_revisions[0]["origin"] == "guidance_reconcile"
        assert "回复详细一点" not in chat._active_preference_prompt(user_id, persona_id)
        assert "多问我一点" in chat._active_preference_prompt(user_id, persona_id)
        with database.get_db() as db:
            after_partial_style = db.execute(
                "SELECT speaking_style, growth_notes FROM personas WHERE id = ?",
                (persona_id,),
            ).fetchone()
        assert "回复详细一点" not in after_partial_style["speaking_style"]
        assert "多问我一点" not in after_partial_style["speaking_style"]
        assert "回复详细一点" not in after_partial_style["growth_notes"]
        chat.db_chat(
            user_id,
            persona_id,
            "多问我一点也不用了。",
            client_message_id="phase2-chat-guidance-final-stop",
        )
        after_stop = server.persona_growth(persona_id, {"id": user_id})["growth"]
        stopped = next(item for item in after_stop["preference_requests"] if int(item["id"]) == int(retained["id"]))
        assert stopped["status"] == "stopped_in_chat"
        assert stopped["deactivation_reason"] == "用户在聊天中明确停止了这条指导"
        assert len([item for item in after_stop["preference_requests"] if item["origin"] == "chat_feedback"]) == guidance_count + 1
        assert len(sculptor.list_revision_suggestions(user_id, persona_id)) == revision_count + 1
        assert "多问我一点" not in chat._active_preference_prompt(user_id, persona_id)
        with database.get_db() as db:
            old_legacy = db.execute(
                """
                SELECT archived
                FROM memories
                WHERE user_id = ? AND persona_id = ? AND type = 'persona_feedback'
                  AND text LIKE '%回复短一点%' AND text LIKE '%少追问%'
                ORDER BY id ASC LIMIT 1
                """,
                (user_id, persona_id),
            ).fetchone()
            stopped_legacy = db.execute(
                """
                SELECT archived
                FROM memories
                WHERE user_id = ? AND persona_id = ? AND type = 'persona_feedback'
                  AND text LIKE '%多问我一点%'
                ORDER BY id DESC LIMIT 1
                """,
                (user_id, persona_id),
            ).fetchone()
        assert old_legacy and int(old_legacy["archived"]) == 1
        assert stopped_legacy and int(stopped_legacy["archived"]) == 1
    finally:
        server.forge_persona = original_forge
        chat.call_llm_api = original_chat_llm
        chat.update_interaction_insight = original_mirror
        chat.should_use_semantic_recall = original_semantic
        archivist.should_use_llm_for_extraction = original_extraction_policy


def verify_legacy_preference_queue_retired(server, user_id: int) -> None:
    original_forge = server.forge_persona
    server.forge_persona = lambda **kwargs: {**forged_persona(), "name": "旧队列"}
    try:
        persona = server.create_persona(server.PersonaCreateRequest(description="legacy"), {"id": user_id})["persona"]
    finally:
        server.forge_persona = original_forge
    persona_id = int(persona["id"])
    ts = database.now_ts()
    with database.get_db() as db:
        suggestion_id = int(db.execute(
            """
            INSERT INTO persona_revision_suggestions (
                user_id, persona_id, status, base_version, origin, reason,
                suggestion_json, source_context_json, created_at, updated_at
            )
            VALUES (?, ?, 'pending', 1, 'profile_request', '旧资料页审批流', ?, '{}', ?, ?)
            """,
            (user_id, persona_id, json.dumps(forged_persona(), ensure_ascii=False), ts, ts),
        ).lastrowid)
        db.execute(
            """
            INSERT INTO persona_growth_requests (
                user_id, persona_id, request_text, suggestion_id, created_at, updated_at
            )
            VALUES (?, ?, '请少追问', ?, ?, ?)
            """,
            (user_id, persona_id, suggestion_id, ts, ts),
        )
    database.init_db()
    with database.get_db() as db:
        row = db.execute(
            "SELECT status, decision_actor, decision_note FROM persona_revision_suggestions WHERE id = ?",
            (suggestion_id,),
        ).fetchone()
    assert row["status"] == "dismissed"
    assert row["decision_actor"] == "adaptive_runtime"
    assert "运行时自动适配" in row["decision_note"]
    growth = server.persona_growth(persona_id, {"id": user_id})["growth"]
    assert growth["preference_requests"][0]["status"] == "active_guidance"


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
    assert int(persona["pending_preference_request_count"]) == 0
    assert int(persona["adjustment_feedback_count"]) == 0
    suggestions = server.admin_persona_revisions({"id": admin_id}, demo_user_id, persona_id)["suggestions"]
    reviewed = next(item for item in suggestions if item["status"] == "applied")
    assert reviewed["decision_actor"] == "review_agent"
    backlog = next(
        item for item in suggestions
        if item["status"] == "pending" and item["origin"] == "explicit_feedback"
    )
    assert backlog["stale"] is False

    processed = server.admin_auto_review_persona_revisions({"id": admin_id}, demo_user_id, persona_id)
    assert processed["attempted_count"] == 1
    assert processed["applied_count"] == 0 and processed["dismissed_count"] == 1
    persona = next(item for item in server.admin_personas({"id": admin_id}, demo_user_id)["personas"] if int(item["id"]) == persona_id)
    assert int(persona["version"]) == 2
    assert int(persona["pending_revision_count"]) == 0
    assert int(persona["pending_auto_revision_count"]) == 0
    assert int(persona["pending_preference_request_count"]) == 0

    growth = server.persona_growth(persona_id, {"id": demo_user_id})["growth"]
    assert growth["latest_reviewed_change"]["version"] == 2
    assert growth["latest_reviewed_change"]["feedback"]["reaction"] == "needs_adjustment"
    assert "不要立刻分析" in growth["latest_reviewed_change"]["feedback"]["detail_text"]
    assert growth["latest_reviewed_change"]["feedback"]["followup_status"] == "completed"
    assert growth["reviewed_changes"][0]["version"] == 2
    assert growth["reviewed_changes"][0]["feedback"] == {
        "reaction": "needs_adjustment",
        "followup_status": "completed",
        "followed_up_at": growth["latest_reviewed_change"]["feedback"]["followed_up_at"],
    }
    assert growth["preference_requests"][0]["status"] == "active_guidance"
    assert growth["preference_requests"][0]["can_withdraw"] is True
    assert "不要马上替我分析" in growth["preference_requests"][0]["detail"]
    admin_growth = server.admin_persona_growth({"id": admin_id}, demo_user_id, persona_id)
    assert admin_growth["preference_requests"][0]["suggestion_id"] is None
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
    with database.get_db() as db:
        db.execute("UPDATE user_insights SET discovery_dimensions_json = '{}' WHERE user_id = ?", (user_id,))
    prompt = mirror.discovery_prompt(user_id)
    assert "not a default conversational hook" in prompt
    assert "profile is currently sparse" in prompt
    assert "learning a different dimension" in prompt
    assert '"interests and tastes"' in prompt
    assert '"daily rhythm"' in prompt
    assert "Areas not yet clearly learned" in prompt
    with database.get_db() as db:
        persisted_dimensions = json.loads(
            db.execute("SELECT discovery_dimensions_json FROM user_insights WHERE user_id = ?", (user_id,)).fetchone()[0]
        )
    assert "interests" in persisted_dimensions
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
    original_mirror_policy = mirror.should_use_llm_for_mirror
    mirror.should_use_llm_for_mirror = lambda *args, **kwargs: False
    try:
        tracked = mirror.update_interaction_insight(
            user_id,
            "我平时下班后比较累，压力大的时候先陪我缓缓就好。",
            [{"type": "persona_feedback", "text": "用户压力大时希望先陪着缓一缓"}],
        )
        tracked = mirror.update_interaction_insight(
            user_id,
            "我很看重自由，也正在准备明年的资格考试。",
            [{"type": "plan", "text": "用户正在准备明年的资格考试"}],
        )
    finally:
        mirror.should_use_llm_for_mirror = original_mirror_policy
    dimensions = tracked["discovery_dimensions"]
    assert {"interests", "daily_rhythm", "values", "comfort_style", "ambitions", "relationship_style"} <= set(dimensions)
    assert dimensions["comfort_style"]["observed_count"] == 1
    expanded = mirror.discovery_prompt(user_id)
    assert '"daily rhythm"' in expanded and '"comfort style"' in expanded and '"plans and ambitions"' in expanded
    assert '"boundaries and annoyances"' in expanded
    assert "Discovery coverage is guidance, not a checklist" in expanded
    cautious = mirror.update_interaction_insight(user_id, "别问我这些，我不喜欢被问这些。", [])
    assert cautious["curiosity_feedback"]["status"] == "cautious"
    assert cautious["curiosity_feedback"]["declined_count"] == 1
    cautious_prompt = mirror.discovery_prompt(user_id)
    assert "explicitly said they do not want exploratory or personal questions" in cautious_prompt
    assert "Do not initiate one now" in cautious_prompt
    invited = mirror.update_interaction_insight(user_id, "你可以问我，想知道什么可以问。", [])
    assert invited["curiosity_feedback"]["status"] == "invited"
    assert invited["curiosity_feedback"]["declined_count"] == 1
    assert invited["curiosity_feedback"]["invited_count"] == 1
    invited_prompt = mirror.discovery_prompt(user_id)
    assert "explicitly welcomed natural curiosity" in invited_prompt
    assert "at most one optional question" in invited_prompt
    topic_skip = mirror.update_interaction_insight(user_id, "这个我不想说，换个话题吧。", [])
    assert topic_skip["curiosity_feedback"]["status"] == "invited"
    assert topic_skip["curiosity_feedback"]["declined_count"] == 1
    assert topic_skip["curiosity_feedback"]["invited_count"] == 1
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


def verify_explicit_topic_change_of_mind(server, mirror, archivist, memory_conflicts, user_id: int) -> None:
    original_forge = server.forge_persona
    original_mirror_policy = mirror.should_use_llm_for_mirror
    original_extraction_policy = archivist.should_use_llm_for_extraction
    server.forge_persona = lambda **kwargs: {**forged_persona(), "name": "照影"}
    try:
        persona = server.create_persona(server.PersonaCreateRequest(description="gentle"), {"id": user_id})["persona"]
        persona_id = int(persona["id"])
        mirror.should_use_llm_for_mirror = lambda *args, **kwargs: False
        archivist.should_use_llm_for_extraction = lambda *args, **kwargs: False

        stored = archivist.extract_and_store(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=None,
            source_message_id=None,
            user_text="我喜欢原神。",
        )
        liked = mirror.update_interaction_insight(user_id, "我喜欢原神。", stored)
        assert "原神" in liked["topic_model"]["likes"] and "原神" not in liked["topic_model"]["dislikes"]

        stored = archivist.extract_and_store(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=None,
            source_message_id=None,
            user_text="我现在不喜欢原神了。",
        )
        disliked = mirror.update_interaction_insight(user_id, "我现在不喜欢原神了。", stored)
        assert "原神" not in disliked["topic_model"]["likes"]
        assert "原神" in disliked["topic_model"]["dislikes"] and "原神" in disliked["topic_model"]["avoid_topics"]
        assert "Do not proactively bring up 原神" in mirror.insight_prompt(user_id)

        stored = archivist.extract_and_store(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=None,
            source_message_id=None,
            user_text="我现在又喜欢原神了。",
        )
        liked_again = mirror.update_interaction_insight(user_id, "我现在又喜欢原神了。", stored)
        assert "原神" in liked_again["topic_model"]["likes"]
        assert "原神" not in liked_again["topic_model"]["dislikes"]
        assert "原神" not in liked_again["topic_model"]["avoid_topics"]
        assert "Do not proactively bring up 原神" not in mirror.insight_prompt(user_id)

        with database.get_db() as db:
            legacy = db.execute(
                """
                SELECT text, archived
                FROM memories
                WHERE user_id = ? AND persona_id = ? AND type = 'preference' AND text LIKE '%原神%'
                ORDER BY id ASC
                """,
                (user_id, persona_id),
            ).fetchall()
            facts = db.execute(
                """
                SELECT text, valid_to
                FROM memory_facts
                WHERE user_id = ? AND persona_id = ? AND type = 'preference' AND text LIKE '%原神%'
                ORDER BY id ASC
                """,
                (user_id, persona_id),
            ).fetchall()
            relations = db.execute(
                """
                SELECT text, valid_to
                FROM memory_relations
                WHERE user_id = ? AND persona_id = ? AND predicate = 'preference' AND object = '原神'
                ORDER BY id ASC
                """,
                (user_id, persona_id),
            ).fetchall()
        assert [int(row["archived"]) for row in legacy] == [1, 1, 0]
        assert facts[0]["valid_to"] and facts[1]["valid_to"] and facts[2]["valid_to"] is None
        assert relations[0]["valid_to"] and relations[1]["valid_to"] and relations[2]["valid_to"] is None
        conflicts = memory_conflicts.list_conflicts(user_id, persona_id, status=None)
        assert any(item["conflict_type"] == "preference_polarity" and item["status"] == "resolved" for item in conflicts)
    finally:
        server.forge_persona = original_forge
        mirror.should_use_llm_for_mirror = original_mirror_policy
        archivist.should_use_llm_for_extraction = original_extraction_policy


def verify_explicit_topic_boundary_release(server, mirror, archivist, layered_memory, memory_conflicts, user_id: int) -> None:
    original_forge = server.forge_persona
    original_mirror_policy = mirror.should_use_llm_for_mirror
    original_extraction_policy = archivist.should_use_llm_for_extraction
    server.forge_persona = lambda **kwargs: {**forged_persona(), "name": "照岚"}
    try:
        persona = server.create_persona(server.PersonaCreateRequest(description="gentle"), {"id": user_id})["persona"]
        persona_id = int(persona["id"])
        mirror.should_use_llm_for_mirror = lambda *args, **kwargs: False
        archivist.should_use_llm_for_extraction = lambda *args, **kwargs: False

        stored = archivist.extract_and_store(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=None,
            source_message_id=None,
            user_text="我喜欢原神，但以后别主动提原神。",
        )
        bounded = mirror.update_interaction_insight(user_id, "我喜欢原神，但以后别主动提原神。", stored)
        assert "原神" in bounded["topic_model"]["likes"]
        assert "原神" in bounded["topic_model"]["avoid_topics"]
        assert "Do not proactively bring up 原神" in mirror.insight_prompt(user_id)
        state = layered_memory.refresh_memory_state(user_id, persona_id)
        assert "原神" not in state["forbidden_addresses"]
        assert "不要主动提及：原神。" in layered_memory.summary_prompt(user_id, persona_id)

        stored = archivist.extract_and_store(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=None,
            source_message_id=None,
            user_text="我现在又喜欢原神了。",
        )
        still_bounded = mirror.update_interaction_insight(user_id, "我现在又喜欢原神了。", stored)
        assert "原神" in still_bounded["topic_model"]["likes"]
        assert "原神" in still_bounded["topic_model"]["avoid_topics"]

        stored = archivist.extract_and_store(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=None,
            source_message_id=None,
            user_text="原神可以聊了。",
        )
        released = mirror.update_interaction_insight(user_id, "原神可以聊了。", stored)
        assert "原神" in released["topic_model"]["likes"]
        assert "原神" not in released["topic_model"]["avoid_topics"]
        assert "Do not proactively bring up 原神" not in mirror.insight_prompt(user_id)

        stored = archivist.extract_and_store(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=None,
            source_message_id=None,
            user_text="我现在不喜欢原神了，以后别主动提原神。",
        )
        disliked = mirror.update_interaction_insight(user_id, "我现在不喜欢原神了，以后别主动提原神。", stored)
        assert "原神" in disliked["topic_model"]["dislikes"]
        stored = archivist.extract_and_store(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=None,
            source_message_id=None,
            user_text="以后可以主动聊原神了。",
        )
        released_boundary_only = mirror.update_interaction_insight(user_id, "以后可以主动聊原神了。", stored)
        assert "原神" in released_boundary_only["topic_model"]["dislikes"]
        assert "原神" in released_boundary_only["topic_model"]["avoid_topics"]
        assert "Do not proactively bring up 原神" in mirror.insight_prompt(user_id)

        with database.get_db() as db:
            legacy = db.execute(
                """
                SELECT archived FROM memories
                WHERE user_id = ? AND persona_id = ? AND type = 'boundary' AND text = '不要主动提原神'
                ORDER BY id ASC
                """,
                (user_id, persona_id),
            ).fetchall()
            relations = db.execute(
                """
                SELECT valid_to FROM memory_relations
                WHERE user_id = ? AND persona_id = ? AND predicate = 'boundary' AND text = '不要主动提原神'
                ORDER BY id ASC
                """,
                (user_id, persona_id),
            ).fetchall()
        assert legacy and all(int(row["archived"]) == 1 for row in legacy)
        assert relations and all(row["valid_to"] for row in relations)
        conflicts = memory_conflicts.list_conflicts(user_id, persona_id, status=None)
        assert any(item["conflict_type"] == "boundary_released" and item["status"] == "resolved" for item in conflicts)
    finally:
        server.forge_persona = original_forge
        mirror.should_use_llm_for_mirror = original_mirror_policy
        archivist.should_use_llm_for_extraction = original_extraction_policy


def verify_explicit_address_boundary_release(server, archivist, layered_memory, memory_conflicts, user_id: int) -> None:
    original_forge = server.forge_persona
    original_extraction_policy = archivist.should_use_llm_for_extraction
    server.forge_persona = lambda **kwargs: {**forged_persona(), "name": "照语"}
    try:
        persona = server.create_persona(server.PersonaCreateRequest(description="gentle"), {"id": user_id})["persona"]
        persona_id = int(persona["id"])
        archivist.should_use_llm_for_extraction = lambda *args, **kwargs: False

        archivist.extract_and_store(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=None,
            source_message_id=None,
            user_text="别叫我宝宝。",
        )
        prohibited = layered_memory.refresh_memory_state(user_id, persona_id)
        assert prohibited["preferred_address"] is None
        assert "宝宝" in prohibited["forbidden_addresses"]

        archivist.extract_and_store(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=None,
            source_message_id=None,
            user_text="现在可以叫我宝宝了。",
        )
        permitted = layered_memory.refresh_memory_state(user_id, persona_id)
        assert permitted["preferred_address"] is None
        assert "宝宝" not in permitted["forbidden_addresses"]
        assert "不要称呼用户为：宝宝。" not in layered_memory.summary_prompt(user_id, persona_id)

        archivist.extract_and_store(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=None,
            source_message_id=None,
            user_text="别叫我主人。",
        )
        prohibited_again = layered_memory.refresh_memory_state(user_id, persona_id)
        assert "主人" in prohibited_again["forbidden_addresses"]

        stored = archivist.extract_and_store(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=None,
            source_message_id=None,
            user_text="你可以叫我主人。",
        )
        assert any(item.get("type") == "identity" and item.get("text") == "用户希望被称为主人" for item in stored)
        named = layered_memory.refresh_memory_state(user_id, persona_id)
        assert named["preferred_address"] == "主人"
        assert "主人" not in named["forbidden_addresses"]

        archivist.extract_and_store(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=None,
            source_message_id=None,
            user_text="别叫我小禾。",
        )
        archivist.extract_and_store(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=None,
            source_message_id=None,
            user_text="以后叫我小禾。",
        )
        renamed = layered_memory.refresh_memory_state(user_id, persona_id)
        assert renamed["preferred_address"] == "小禾"
        assert "小禾" not in renamed["forbidden_addresses"]
        recalled = archivist.recall_memories(user_id, persona_id, "以后应该怎么称呼我", limit=20)
        active_identity_text = [
            item["text"] for item in recalled if item.get("type") == "identity" and "用户希望被称为" in str(item.get("text"))
        ]
        assert "用户希望被称为小禾" in active_identity_text
        assert "用户希望被称为主人" not in active_identity_text

        with database.get_db() as db:
            legacy = db.execute(
                """
                SELECT archived FROM memories
                WHERE user_id = ? AND persona_id = ? AND type = 'boundary'
                  AND text IN ('不要称呼用户为宝宝', '不要称呼用户为主人', '不要称呼用户为小禾')
                ORDER BY id ASC
                """,
                (user_id, persona_id),
            ).fetchall()
            relations = db.execute(
                """
                SELECT valid_to FROM memory_relations
                WHERE user_id = ? AND persona_id = ? AND predicate = 'boundary'
                  AND text IN ('不要称呼用户为宝宝', '不要称呼用户为主人', '不要称呼用户为小禾')
                ORDER BY id ASC
                """,
                (user_id, persona_id),
            ).fetchall()
            identities = db.execute(
                """
                SELECT text, archived FROM memories
                WHERE user_id = ? AND persona_id = ? AND type = 'identity'
                  AND text IN ('用户希望被称为主人', '用户希望被称为小禾')
                ORDER BY id ASC
                """,
                (user_id, persona_id),
            ).fetchall()
        assert len(legacy) == 3 and all(int(row["archived"]) == 1 for row in legacy)
        assert len(relations) == 3 and all(row["valid_to"] for row in relations)
        assert [(row["text"], int(row["archived"])) for row in identities] == [
            ("用户希望被称为主人", 1),
            ("用户希望被称为小禾", 0),
        ]
        conflicts = memory_conflicts.list_conflicts(user_id, persona_id, status=None)
        assert len([item for item in conflicts if item["conflict_type"] == "address_boundary_released" and item["status"] == "resolved"]) == 3
        assert any(item["conflict_type"] == "identity_superseded" and item["status"] == "resolved" for item in conflicts)
        assert any(item["conflict_type"] == "preferred_address_superseded" and item["status"] == "resolved" for item in conflicts)
    finally:
        server.forge_persona = original_forge
        archivist.should_use_llm_for_extraction = original_extraction_policy


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        database.DB_PATH = Path(tmp) / "phase2.db"
        import app.sculptor as sculptor
        import app.archivist as archivist
        import app.db_chat as chat
        import app.growth_demo as growth_demo
        import app.layered_memory as layered_memory
        import app.memory_conflicts as memory_conflicts
        import app.mirror as mirror
        import app.server as server

        database.init_db()
        user_id = seed_user()
        verify_growth_chain(server, sculptor, chat, user_id)
        verify_chat_feedback_queues_candidate(server, sculptor, chat, archivist, user_id)
        verify_active_preference_guidance(server, chat, user_id)
        verify_active_preference_survives_profile_update(server, chat, user_id)
        verify_newer_conflicting_guidance_supersedes_old(server, chat, user_id)
        verify_chat_guidance_replaces_older_chat_guidance(server, sculptor, chat, archivist, user_id)
        verify_legacy_preference_queue_retired(server, user_id)
        verify_growth_demo_sandbox(server, growth_demo, user_id)
        verify_sparse_profile_discovery_policy(mirror, chat, user_id)
        verify_explicit_topic_change_of_mind(server, mirror, archivist, memory_conflicts, user_id)
        verify_explicit_topic_boundary_release(server, mirror, archivist, layered_memory, memory_conflicts, user_id)
        verify_explicit_address_boundary_release(server, archivist, layered_memory, memory_conflicts, user_id)
    print("Phase 2 persona growth verification passed")


if __name__ == "__main__":
    main()
