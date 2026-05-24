from __future__ import annotations

import json
import re
from typing import Any

from .database import dict_from_row, get_db, now_ts
from .identity import scrub_identity_obj, scrub_identity_text
from .layered_memory import refresh_memory_state, refresh_memory_summaries
from .llm_client import call_llm_api
from .persona_forge import build_prompt


SCULPTOR_SYSTEM = """You are Sculptor, a persona revision planner.
Your job is to propose a safer, more coherent next version of one long-term chat persona.
Use the user's explicit feedback, boundaries, relationship state, and memory summaries.
Do not invent a real human identity. Do not remove safety boundaries.
Output strict JSON only:
{
  "name": "...",
  "summary": "...",
  "traits": ["..."],
  "relationship": "...",
  "speaking_style": "...",
  "appearance_description": "...",
  "desired_image": "...",
  "psychological_fit_notes": "...",
  "psychological_profile": {
    "primary_needs": ["..."],
    "comfort_strategy": ["..."],
    "avoid_patterns": ["..."],
    "growth_direction": ["..."]
  },
  "growth_notes": "...",
  "boundaries": ["..."],
  "change_notes": ["..."],
  "prompt": "..."
}
"""

DIFF_FIELDS = (
    ("name", "名字"),
    ("summary", "摘要"),
    ("relationship", "关系定位"),
    ("speaking_style", "说话方式"),
    ("traits", "人格特征"),
    ("boundaries", "边界"),
    ("psychological_fit_notes", "心理适配"),
    ("growth_notes", "成长备注"),
    ("appearance_description", "外貌参考"),
    ("desired_image", "期望形象"),
)


def list_revision_suggestions(user_id: int, persona_id: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    params: list[Any] = [user_id]
    persona_clause = ""
    if persona_id is not None:
        persona_clause = "AND persona_id = ?"
        params.append(persona_id)
    params.append(limit)
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT *
            FROM persona_revision_suggestions
            WHERE user_id = ? {persona_clause}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_present_suggestion(dict_from_row(row) or {}) for row in rows]


def generate_revision_suggestion(
    user_id: int,
    persona_id: int,
    reason: str = "",
    *,
    use_llm: bool = True,
    origin: str = "manual",
    trigger_message_id: int | None = None,
    trigger_memory_uids: list[str] | None = None,
) -> dict[str, Any]:
    persona = _persona_for_user(user_id, persona_id)
    origin_value = (origin[:40] or "manual")
    source_context = scrub_identity_obj(_source_context(
        user_id,
        persona_id,
        strong_feedback_only=origin_value == "explicit_feedback",
    ))
    suggestion = (
        _llm_suggestion(persona, source_context, reason) if use_llm else None
    ) or _fallback_suggestion(persona, source_context, reason)
    suggestion = _normalize_suggestion(suggestion, persona)
    base_version = int(persona.get("version", 1) or 1)
    ts = now_ts()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute(
            "SELECT version FROM personas WHERE id = ? AND user_id = ? AND status = 'active'",
            (persona_id, user_id),
        ).fetchone()
        if not current or int(current["version"] or 1) != base_version:
            raise ValueError("人格在建议生成期间已更新，请重新生成建议")
        existing = db.execute(
            """
            SELECT *
            FROM persona_revision_suggestions
            WHERE user_id = ? AND persona_id = ? AND base_version = ? AND origin = ? AND status = 'pending'
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id, persona_id, base_version, origin_value),
        ).fetchone()
        if existing:
            existing_item = dict_from_row(existing) or {}
            try:
                existing_uids = json.loads(existing_item.get("trigger_memory_uids_json") or "[]")
            except Exception:
                existing_uids = []
            merged_uids = list(dict.fromkeys([*existing_uids, *(trigger_memory_uids or [])]))
            db.execute(
                """
                UPDATE persona_revision_suggestions
                SET trigger_message_id = COALESCE(?, trigger_message_id),
                    trigger_memory_uids_json = ?, reason = ?, suggestion_json = ?,
                    source_context_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    trigger_message_id,
                    json.dumps(merged_uids, ensure_ascii=False),
                    scrub_identity_text(reason.strip()),
                    json.dumps(suggestion, ensure_ascii=False),
                    json.dumps(source_context, ensure_ascii=False),
                    ts,
                    int(existing_item["id"]),
                ),
            )
            existing = db.execute(
                "SELECT * FROM persona_revision_suggestions WHERE id = ?",
                (int(existing_item["id"]),),
            ).fetchone()
            return _present_suggestion(dict_from_row(existing) or {})
        cursor = db.execute(
            """
            INSERT INTO persona_revision_suggestions (
                user_id, persona_id, status, base_version, origin,
                trigger_message_id, trigger_memory_uids_json, reason, suggestion_json,
                source_context_json, created_at, updated_at
            )
            VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                persona_id,
                base_version,
                origin_value,
                trigger_message_id,
                json.dumps(trigger_memory_uids or [], ensure_ascii=False),
                scrub_identity_text(reason.strip()),
                json.dumps(suggestion, ensure_ascii=False),
                json.dumps(source_context, ensure_ascii=False),
                ts,
                ts,
            ),
        )
        row = db.execute(
            "SELECT * FROM persona_revision_suggestions WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
    return _present_suggestion(dict_from_row(row) or {})


def maybe_queue_revision_from_feedback(user_id: int, persona_id: int, source_message_id: int) -> dict[str, Any] | None:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT uid, text
            FROM memory_facts
            WHERE user_id = ? AND persona_id = ? AND source_message_id = ?
              AND type = 'persona_feedback'
              AND priority IN ('high', 'critical')
              AND archived = 0 AND valid_to IS NULL
            ORDER BY importance DESC, confidence DESC
            LIMIT 6
            """,
            (user_id, persona_id, source_message_id),
        ).fetchall()
    triggers = [dict_from_row(row) or {} for row in rows]
    if not triggers:
        return None
    reason = "自动候选：用户在聊天中明确提出了相处方式调整"
    return generate_revision_suggestion(
        user_id,
        persona_id,
        reason,
        use_llm=False,
        origin="explicit_feedback",
        trigger_message_id=source_message_id,
        trigger_memory_uids=[str(item.get("uid") or "") for item in triggers if item.get("uid")],
    )


def apply_revision_suggestion(
    user_id: int,
    suggestion_id: int,
    *,
    reviewer_user_id: int | None = None,
    decision_note: str = "",
) -> dict[str, Any]:
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        suggestion_row = dict_from_row(
            db.execute(
                "SELECT * FROM persona_revision_suggestions WHERE id = ? AND user_id = ?",
                (suggestion_id, user_id),
            ).fetchone()
        )
        if not suggestion_row:
            raise ValueError("suggestion not found")
        if suggestion_row["status"] != "pending":
            raise ValueError("suggestion is not pending")

        persona_id = int(suggestion_row["persona_id"])
        persona = dict_from_row(
            db.execute(
                "SELECT * FROM personas WHERE id = ? AND user_id = ? AND status = 'active'",
                (persona_id, user_id),
            ).fetchone()
        )
        if not persona:
            raise ValueError("persona not found")
        base_version = suggestion_row.get("base_version")
        current_version = int(persona.get("version", 1) or 1)
        if base_version is None:
            raise ValueError("建议缺少生成时的基线版本，请重新生成后再应用")
        if int(base_version) != current_version:
            raise ValueError(f"建议基于 v{int(base_version)}，当前人格已为 v{current_version}，请重新生成建议")
        suggestion = json.loads(suggestion_row["suggestion_json"] or "{}")
        suggestion = _normalize_suggestion(suggestion, persona)
        ts = now_ts()
        new_version = current_version + 1
        clean_decision_note = scrub_identity_text(decision_note.strip())[:1000]

        db.execute(
            """
            UPDATE personas
            SET name = ?, summary = ?, prompt = ?, traits_json = ?,
                relationship = ?, speaking_style = ?, boundaries_json = ?,
                psychological_profile_json = ?, psychological_fit_notes = ?,
                appearance_description = ?, desired_image = ?, growth_notes = ?,
                version = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                suggestion["name"],
                suggestion["summary"],
                suggestion["prompt"],
                json.dumps(suggestion["traits"], ensure_ascii=False),
                suggestion["relationship"],
                suggestion["speaking_style"],
                json.dumps(suggestion["boundaries"], ensure_ascii=False),
                json.dumps(suggestion["psychological_profile"], ensure_ascii=False),
                suggestion["psychological_fit_notes"],
                suggestion["appearance_description"],
                suggestion["desired_image"],
                suggestion["growth_notes"],
                new_version,
                ts,
                persona_id,
                user_id,
            ),
        )
        db.execute(
            """
            INSERT INTO persona_versions (
                persona_id, version, name, summary, prompt, traits_json,
                relationship, speaking_style, boundaries_json,
                psychological_profile_json, psychological_fit_notes,
                appearance_description, desired_image, growth_notes,
                reason, change_type, source_suggestion_id, change_notes_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                persona_id,
                new_version,
                suggestion["name"],
                suggestion["summary"],
                suggestion["prompt"],
                json.dumps(suggestion["traits"], ensure_ascii=False),
                suggestion["relationship"],
                suggestion["speaking_style"],
                json.dumps(suggestion["boundaries"], ensure_ascii=False),
                json.dumps(suggestion["psychological_profile"], ensure_ascii=False),
                suggestion["psychological_fit_notes"],
                suggestion["appearance_description"],
                suggestion["desired_image"],
                suggestion["growth_notes"],
                suggestion_row.get("reason") or "sculptor suggestion",
                "sculptor_review",
                suggestion_id,
                json.dumps(suggestion.get("change_notes", []), ensure_ascii=False),
                ts,
            ),
        )
        db.execute(
            """
            UPDATE persona_revision_suggestions
            SET status = 'applied', applied_at = ?, applied_version = ?,
                decided_at = ?, decided_by_user_id = ?, decision_note = ?, updated_at = ?
            WHERE id = ?
            """,
            (ts, new_version, ts, reviewer_user_id, clean_decision_note, ts, suggestion_id),
        )
        persona_row = db.execute("SELECT * FROM personas WHERE id = ?", (persona_id,)).fetchone()
    return {"persona": dict_from_row(persona_row), "suggestion_id": suggestion_id, "version": new_version}


def dismiss_revision_suggestion(
    user_id: int,
    suggestion_id: int,
    *,
    reviewer_user_id: int | None = None,
    decision_note: str = "",
) -> dict[str, Any]:
    clean_decision_note = scrub_identity_text(decision_note.strip())[:1000]
    ts = now_ts()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        suggestion = dict_from_row(
            db.execute(
                "SELECT * FROM persona_revision_suggestions WHERE id = ? AND user_id = ?",
                (suggestion_id, user_id),
            ).fetchone()
        )
        if not suggestion:
            raise ValueError("suggestion not found")
        if suggestion["status"] != "pending":
            return _present_suggestion(suggestion)
        db.execute(
            """
            UPDATE persona_revision_suggestions
            SET status = 'dismissed', decided_at = ?, decided_by_user_id = ?,
                decision_note = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (ts, reviewer_user_id, clean_decision_note, ts, suggestion_id, user_id),
        )
        row = db.execute("SELECT * FROM persona_revision_suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    return _present_suggestion(dict_from_row(row) or {})


def _source_context(user_id: int, persona_id: int, *, strong_feedback_only: bool = False) -> dict[str, Any]:
    state = refresh_memory_state(user_id, persona_id)
    summaries = refresh_memory_summaries(user_id, persona_id)
    with get_db() as db:
        feedback = db.execute(
            """
            SELECT uid, type, text, importance, confidence, priority, updated_at
            FROM memory_facts
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL)
              AND type IN ('persona_feedback', 'boundary', 'relationship')
              AND (? = 0 OR type != 'persona_feedback' OR priority IN ('high', 'critical'))
              AND archived = 0 AND valid_to IS NULL
            ORDER BY priority DESC, importance DESC, updated_at DESC
            LIMIT 30
            """,
            (user_id, persona_id, 1 if strong_feedback_only else 0),
        ).fetchall()
        relations = db.execute(
            """
            SELECT uid, type, subject, predicate, object, text, importance, confidence, priority, updated_at
            FROM memory_relations
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL)
              AND predicate IN ('persona_feedback', 'boundary', 'relationship_expectation')
              AND (? = 0 OR predicate != 'persona_feedback' OR priority IN ('high', 'critical'))
              AND archived = 0 AND valid_to IS NULL
            ORDER BY priority DESC, importance DESC, updated_at DESC
            LIMIT 30
            """,
            (user_id, persona_id, 1 if strong_feedback_only else 0),
        ).fetchall()
        traces = db.execute(
            """
            SELECT id, query_text, status, error_text, prompt_chars, created_at
            FROM chat_context_traces
            WHERE user_id = ? AND persona_id = ?
            ORDER BY id DESC
            LIMIT 8
            """,
            (user_id, persona_id),
        ).fetchall()
    return {
        "state": state,
        "summaries": summaries,
        "feedback_facts": [dict_from_row(row) for row in feedback],
        "feedback_relations": [dict_from_row(row) for row in relations],
        "recent_traces": [dict_from_row(row) for row in traces],
    }


def _llm_suggestion(persona: dict[str, Any], source_context: dict[str, Any], reason: str) -> dict[str, Any] | None:
    payload = {
        "current_persona": _public_persona(persona),
        "revision_reason": reason,
        "source_context": source_context,
    }
    try:
        raw = call_llm_api(
            [
                {"role": "system", "content": SCULPTOR_SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
            ],
            task="sculptor",
        )
    except Exception as exc:
        print("[Sculptor] LLM suggestion skipped:", exc)
        return None
    return _extract_json(raw)


def _fallback_suggestion(persona: dict[str, Any], source_context: dict[str, Any], reason: str) -> dict[str, Any]:
    current = _public_persona(persona)
    feedback_texts = []
    for item in [*source_context.get("feedback_facts", []), *source_context.get("feedback_relations", [])]:
        text = scrub_identity_text(str((item or {}).get("text") or "").strip())
        if text and text not in feedback_texts:
            feedback_texts.append(text)

    change_notes = feedback_texts[:8]
    if reason.strip():
        change_notes.insert(0, scrub_identity_text(reason.strip()))
    if not change_notes:
        change_notes.append("No strong feedback yet; keep the current persona stable.")

    speaking_style = scrub_identity_text(current.get("speaking_style") or "")
    new_feedback_texts = [text for text in feedback_texts if text not in speaking_style]
    if new_feedback_texts:
        separator = "\n适配备注：" if "适配备注：" not in speaking_style else " / "
        speaking_style = f"{speaking_style}{separator}" + " / ".join(new_feedback_texts[:5])

    suggestion = {
        "name": current.get("name") or "New Persona",
        "summary": scrub_identity_text(current.get("summary") or ""),
        "traits": scrub_identity_obj(current.get("traits") or []),
        "relationship": scrub_identity_text(current.get("relationship") or ""),
        "speaking_style": speaking_style.strip(),
        "appearance_description": scrub_identity_text(current.get("appearance_description") or ""),
        "desired_image": scrub_identity_text(current.get("desired_image") or ""),
        "psychological_fit_notes": scrub_identity_text(current.get("psychological_fit_notes") or ""),
        "psychological_profile": scrub_identity_obj(current.get("psychological_profile") or {}),
        "growth_notes": scrub_identity_text(" / ".join(change_notes[:5])),
        "boundaries": scrub_identity_obj(current.get("boundaries") or []),
        "change_notes": scrub_identity_obj(change_notes),
    }
    suggestion["prompt"] = build_prompt(suggestion)
    return suggestion


def _normalize_suggestion(data: dict[str, Any], current_persona: dict[str, Any]) -> dict[str, Any]:
    current = _public_persona(current_persona)
    traits = data.get("traits") if isinstance(data.get("traits"), list) else current.get("traits", [])
    boundaries = data.get("boundaries") if isinstance(data.get("boundaries"), list) else current.get("boundaries", [])
    change_notes = data.get("change_notes") if isinstance(data.get("change_notes"), list) else []
    psychological_profile = data.get("psychological_profile")
    if not isinstance(psychological_profile, dict):
        psychological_profile = current.get("psychological_profile", {})

    result = {
        "name": scrub_identity_text(str(data.get("name") or current.get("name") or "New Persona").strip())[:80],
        "summary": scrub_identity_text(str(data.get("summary") or current.get("summary") or "").strip())[:2000],
        "traits": [scrub_identity_text(str(item).strip()) for item in traits if str(item).strip()][:20],
        "relationship": scrub_identity_text(str(data.get("relationship") or current.get("relationship") or "").strip())[:500],
        "speaking_style": scrub_identity_text(str(data.get("speaking_style") or current.get("speaking_style") or "").strip())[:2000],
        "appearance_description": scrub_identity_text(str(data.get("appearance_description") or current.get("appearance_description") or "").strip())[:2000],
        "desired_image": scrub_identity_text(str(data.get("desired_image") or current.get("desired_image") or "").strip())[:2000],
        "psychological_fit_notes": scrub_identity_text(str(data.get("psychological_fit_notes") or current.get("psychological_fit_notes") or "").strip())[:2000],
        "psychological_profile": scrub_identity_obj(_normalize_psychological_profile(psychological_profile)),
        "growth_notes": scrub_identity_text(str(data.get("growth_notes") or current.get("growth_notes") or "").strip())[:2000],
        "boundaries": [scrub_identity_text(str(item).strip()) for item in boundaries if str(item).strip()][:30],
        "change_notes": [scrub_identity_text(str(item).strip()) for item in change_notes if str(item).strip()][:20],
        "prompt": "",
    }
    for boundary in current.get("boundaries", []):
        if boundary and boundary not in result["boundaries"]:
            result["boundaries"].append(boundary)
    result["prompt"] = build_prompt(result)
    return result


def _public_persona(persona: dict[str, Any]) -> dict[str, Any]:
    result = dict(persona)
    for key, default in (
        ("traits_json", "[]"),
        ("boundaries_json", "[]"),
        ("psychological_profile_json", "{}"),
        ("memory_profile_json", "{}"),
    ):
        value = result.pop(key, default)
        try:
            result[key.removesuffix("_json")] = json.loads(value or default)
        except Exception:
            result[key.removesuffix("_json")] = json.loads(default)
    return result


def _normalize_psychological_profile(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        value = {}
    return {
        "primary_needs": _string_list(value.get("primary_needs"), 8),
        "comfort_strategy": _string_list(value.get("comfort_strategy"), 10),
        "avoid_patterns": _string_list(value.get("avoid_patterns"), 10),
        "growth_direction": _string_list(value.get("growth_direction"), 10),
    }


def _string_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()][:limit]


def _persona_for_user(user_id: int, persona_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM personas WHERE id = ? AND user_id = ? AND status = 'active'",
            (persona_id, user_id),
        ).fetchone()
    persona = dict_from_row(row)
    if not persona:
        raise ValueError("persona not found")
    return persona


def _suggestion_for_user(user_id: int, suggestion_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM persona_revision_suggestions WHERE id = ? AND user_id = ?",
            (suggestion_id, user_id),
        ).fetchone()
    suggestion = dict_from_row(row)
    if not suggestion:
        raise ValueError("suggestion not found")
    return suggestion


def _decode_suggestion(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("suggestion_json", "source_context_json", "trigger_memory_uids_json"):
        target = key.removesuffix("_json")
        try:
            default = "[]" if key == "trigger_memory_uids_json" else "{}"
            row[target] = json.loads(row.pop(key) or default)
        except Exception:
            row[target] = [] if key == "trigger_memory_uids_json" else {}
    return row


def _present_suggestion(row: dict[str, Any]) -> dict[str, Any]:
    item = _decode_suggestion(row)
    base_version = item.get("base_version")
    base_persona = _persona_version(int(item["persona_id"]), int(base_version)) if base_version is not None else None
    current_version = _current_persona_version(int(item["persona_id"]))
    item["base_persona"] = _public_persona(base_persona) if base_persona else None
    item["changes"] = _persona_changes(item["base_persona"], item.get("suggestion") or {})
    item["evidence_summary"] = _evidence_summary(item.get("source_context") or {})
    item["stale"] = item.get("status") == "pending" and (
        base_version is None or current_version is None or int(base_version) != current_version
    )
    if item["stale"]:
        if base_version is None:
            item["stale_reason"] = "旧建议没有保存生成时的基线版本，请重新生成。"
        else:
            item["stale_reason"] = f"建议基于 v{int(base_version)}，当前人格已为 v{int(current_version or 0)}。"
    return item


def _current_persona_version(persona_id: int) -> int | None:
    with get_db() as db:
        row = db.execute("SELECT version FROM personas WHERE id = ?", (persona_id,)).fetchone()
    return int(row["version"]) if row else None


def _persona_version(persona_id: int, version: int) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM persona_versions WHERE persona_id = ? AND version = ? ORDER BY id DESC LIMIT 1",
            (persona_id, version),
        ).fetchone()
    return dict_from_row(row)


def _persona_changes(base: dict[str, Any] | None, suggestion: dict[str, Any]) -> list[dict[str, str]]:
    if not base:
        return []
    changes = []
    for key, label in DIFF_FIELDS:
        before = _visible_value(base.get(key))
        after = _visible_value(suggestion.get(key))
        if before != after:
            changes.append({"field": key, "label": label, "before": before, "after": after})
    return changes


def _visible_value(value: Any) -> str:
    if isinstance(value, list):
        return "、".join(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value or "").strip()


def _evidence_summary(source: dict[str, Any]) -> dict[str, Any]:
    state = source.get("state") if isinstance(source.get("state"), dict) else {}
    state_keys = [key for key, value in state.items() if value not in (None, "", [], {})]
    return {
        "memory_count": len(source.get("feedback_facts") or []) + len(source.get("feedback_relations") or []),
        "state_keys": state_keys[:12],
        "summary_count": len(source.get("summaries") or []),
        "trace_count": len(source.get("recent_traces") or []),
    }


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except Exception:
        return None
