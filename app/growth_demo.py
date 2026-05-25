from __future__ import annotations

import json
from typing import Any

from .auth import hash_password
from .database import dict_from_row, get_db, now_ts
from .layered_memory import refresh_memory_state, refresh_memory_summaries
from .persona_forge import build_prompt


DEMO_USERNAME = "__mnemosyne_growth_demo__"
DEMO_PASSWORD = "mnemosyne-demo-password"


def seed_growth_demo_data(*, reset: bool = True) -> dict[str, Any]:
    """Create a disposable account that exercises the persona-growth review loop."""
    if reset:
        clear_growth_demo_data()
    existing = _demo_identity()
    if existing:
        return {"created": False, **existing, "username": DEMO_USERNAME, "password": DEMO_PASSWORD}

    ts = now_ts()
    original = _persona_payload(
        speaking_style="温和地回应，允许对方慢慢组织语言。",
        growth_notes="还在熟悉用户的表达节奏。",
    )
    applied = _persona_payload(
        speaking_style="更简短地回应，先接住情绪，避免连续追问。",
        growth_notes="根据用户偏好，减少连续追问并留出表达空间。",
    )
    pending = _persona_payload(
        speaking_style="回应保持简短；安慰时避免替用户下结论，最多提出一个可选问题。",
        growth_notes="继续减少追问，并避免把安慰说得过满。",
    )

    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO users (username, password_hash, role, status, created_at, updated_at)
            VALUES (?, ?, 'user', 'active', ?, ?)
            """,
            (DEMO_USERNAME, hash_password(DEMO_PASSWORD), ts, ts),
        )
        user_id = int(cursor.lastrowid)
        db.execute(
            """
            INSERT INTO user_profiles (user_id, nickname, signature, bio, created_at, updated_at)
            VALUES (?, '成长演示用户', '用于试用人格成长反馈', '可随时从管理台清除的演示账号。', ?, ?)
            """,
            (user_id, ts, ts),
        )
        cursor = db.execute(
            """
            INSERT INTO personas (
                user_id, name, summary, prompt, traits_json, relationship, speaking_style,
                boundaries_json, memory_profile_json, psychological_profile_json,
                psychological_fit_notes, appearance_description, desired_image, growth_notes,
                status, version, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 2, ?, ?)
            """,
            _persona_values(user_id, applied, ts),
        )
        persona_id = int(cursor.lastrowid)
        cursor = db.execute(
            """
            INSERT INTO conversations (user_id, persona_id, title, summary, created_at, updated_at)
            VALUES (?, ?, '和栖夏的演示对话', '用于体验相处方式变化的演示记录。', ?, ?)
            """,
            (user_id, persona_id, ts - 420, ts - 120),
        )
        conversation_id = int(cursor.lastrowid)
        db.execute(
            """
            INSERT INTO messages (conversation_id, user_id, persona_id, role, content, created_at)
            VALUES (?, ?, ?, 'user', '我其实有点累，安慰我的时候别一直追问。', ?),
                   (?, ?, ?, 'assistant', '好，那我先陪你缓一缓。不急着讲清楚。', ?)
            """,
            (
                conversation_id, user_id, persona_id, ts - 300,
                conversation_id, user_id, persona_id, ts - 260,
            ),
        )
        db.execute(
            """
            INSERT INTO memory_facts (
                uid, user_id, persona_id, conversation_id, type, text,
                importance, confidence, valid_from, created_at, updated_at, priority, locked
            )
            VALUES ('DEMO-GROWTH-FEEDBACK-1', ?, ?, ?, 'persona_feedback',
                    '用户希望安慰时少追问，留一点自己整理情绪的空间。',
                    0.92, 0.96, ?, ?, ?, 'high', 1)
            """,
            (user_id, persona_id, conversation_id, ts - 300, ts - 300, ts - 300),
        )
        db.execute(
            """
            INSERT INTO persona_versions (
                persona_id, version, name, summary, prompt, traits_json, relationship,
                speaking_style, boundaries_json, psychological_profile_json,
                psychological_fit_notes, appearance_description, desired_image, growth_notes,
                reason, change_type, change_notes_json, created_at
            )
            VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    'initial forge', 'initial_forge', '[]', ?)
            """,
            (persona_id, *_version_values(original), ts - 720),
        )
        applied_cursor = db.execute(
            """
            INSERT INTO persona_revision_suggestions (
                user_id, persona_id, status, base_version, origin, trigger_memory_uids_json,
                reason, suggestion_json, source_context_json, created_at, updated_at,
                applied_at, applied_version, decided_at, decision_note
            )
            VALUES (?, ?, 'applied', 1, 'explicit_feedback', ?,
                    '演示：根据用户希望少追问的反馈进行轻微调整', ?, ?, ?, ?, ?, 2, ?,
                    '演示数据：已审核应用。')
            """,
            (
                user_id, persona_id, json.dumps(["DEMO-GROWTH-FEEDBACK-1"], ensure_ascii=False),
                json.dumps(applied, ensure_ascii=False), json.dumps(_source_context(), ensure_ascii=False),
                ts - 600, ts - 520, ts - 500, ts - 500,
            ),
        )
        applied_suggestion_id = int(applied_cursor.lastrowid)
        db.execute(
            """
            INSERT INTO persona_versions (
                persona_id, version, name, summary, prompt, traits_json, relationship,
                speaking_style, boundaries_json, psychological_profile_json,
                psychological_fit_notes, appearance_description, desired_image, growth_notes,
                reason, change_type, source_suggestion_id, change_notes_json, created_at
            )
            VALUES (?, 2, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    '演示：减少连续追问', 'sculptor_review', ?, ?, ?)
            """,
            (
                persona_id, *_version_values(applied), applied_suggestion_id,
                json.dumps(["减少连续追问", "给用户更多表达空间"], ensure_ascii=False), ts - 500,
            ),
        )
        db.execute(
            """
            INSERT INTO persona_growth_feedback (
                user_id, persona_id, reviewed_version, reaction, detail_text, created_at, updated_at
            )
            VALUES (?, ?, 2, 'needs_adjustment', '还是有点像在替我做结论，希望只是陪着我，不要立刻分析。', ?, ?)
            """,
            (user_id, persona_id, ts - 240, ts - 240),
        )
        pending_cursor = db.execute(
            """
            INSERT INTO persona_revision_suggestions (
                user_id, persona_id, status, base_version, origin, trigger_memory_uids_json,
                reason, suggestion_json, source_context_json, created_at, updated_at
            )
            VALUES (?, ?, 'pending', 2, 'profile_request', ?,
                    '演示：继续减少追问与替用户总结', ?, ?, ?, ?)
            """,
            (
                user_id, persona_id, json.dumps(["DEMO-GROWTH-FEEDBACK-1"], ensure_ascii=False),
                json.dumps(pending, ensure_ascii=False), json.dumps(_source_context(), ensure_ascii=False),
                ts - 180, ts - 180,
            ),
        )
        db.execute(
            """
            INSERT INTO persona_growth_requests (
                user_id, persona_id, request_text, suggestion_id, created_at, updated_at
            )
            VALUES (?, ?, '安慰我的时候先陪着我，不要马上替我分析或下结论。', ?, ?, ?)
            """,
            (user_id, persona_id, int(pending_cursor.lastrowid), ts - 180, ts - 180),
        )

    refresh_memory_state(user_id, persona_id)
    refresh_memory_summaries(user_id, persona_id)
    return {
        "created": True,
        "user_id": user_id,
        "persona_id": persona_id,
        "conversation_id": conversation_id,
        "username": DEMO_USERNAME,
        "password": DEMO_PASSWORD,
    }


def clear_growth_demo_data() -> dict[str, Any]:
    with get_db() as db:
        row = db.execute("SELECT id FROM users WHERE username = ?", (DEMO_USERNAME,)).fetchone()
        if not row:
            return {"removed": False, "username": DEMO_USERNAME}
        user_id = int(row["id"])
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return {"removed": True, "user_id": user_id, "username": DEMO_USERNAME}


def _demo_identity() -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute(
            """
            SELECT users.id AS user_id, personas.id AS persona_id,
                   conversations.id AS conversation_id
            FROM users
            JOIN personas ON personas.user_id = users.id
            LEFT JOIN conversations ON conversations.persona_id = personas.id
            WHERE users.username = ?
            LIMIT 1
            """,
            (DEMO_USERNAME,),
        ).fetchone()
    return dict_from_row(row)


def _persona_payload(*, speaking_style: str, growth_notes: str) -> dict[str, Any]:
    persona = {
        "name": "栖夏",
        "summary": "安静、可靠，愿意给情绪留出空间。",
        "traits": ["温和", "克制", "耐心"],
        "relationship": "逐渐熟悉的朋友",
        "speaking_style": speaking_style,
        "boundaries": ["不说教", "不强行分析情绪"],
        "memory_profile": {},
        "psychological_profile": {
            "primary_needs": ["被接住而不是被分析"],
            "comfort_strategy": ["先陪伴，再按需要回应"],
            "avoid_patterns": ["连续追问", "替用户下结论"],
            "growth_direction": ["在安慰和空间感之间慢慢校准"],
        },
        "psychological_fit_notes": "用户疲惫时优先给空间，不急于推进对话。",
        "appearance_description": "",
        "desired_image": "",
        "growth_notes": growth_notes,
    }
    persona["prompt"] = build_prompt(persona)
    return persona


def _persona_values(user_id: int, persona: dict[str, Any], ts: int) -> tuple[Any, ...]:
    return (
        user_id,
        persona["name"],
        persona["summary"],
        persona["prompt"],
        json.dumps(persona["traits"], ensure_ascii=False),
        persona["relationship"],
        persona["speaking_style"],
        json.dumps(persona["boundaries"], ensure_ascii=False),
        json.dumps(persona["memory_profile"], ensure_ascii=False),
        json.dumps(persona["psychological_profile"], ensure_ascii=False),
        persona["psychological_fit_notes"],
        persona["appearance_description"],
        persona["desired_image"],
        persona["growth_notes"],
        ts - 720,
        ts - 120,
    )


def _version_values(persona: dict[str, Any]) -> tuple[Any, ...]:
    return (
        persona["name"],
        persona["summary"],
        persona["prompt"],
        json.dumps(persona["traits"], ensure_ascii=False),
        persona["relationship"],
        persona["speaking_style"],
        json.dumps(persona["boundaries"], ensure_ascii=False),
        json.dumps(persona["psychological_profile"], ensure_ascii=False),
        persona["psychological_fit_notes"],
        persona["appearance_description"],
        persona["desired_image"],
        persona["growth_notes"],
    )


def _source_context() -> dict[str, Any]:
    return {
        "state": {"interaction_style": ["少追问", "给空间"]},
        "summaries": [{"text": "用户在疲惫时希望得到安静陪伴。"}],
        "feedback_facts": [
            {
                "uid": "DEMO-GROWTH-FEEDBACK-1",
                "type": "persona_feedback",
                "text": "用户希望安慰时少追问，留一点自己整理情绪的空间。",
                "priority": "high",
            }
        ],
        "feedback_relations": [],
        "recent_traces": [],
    }
