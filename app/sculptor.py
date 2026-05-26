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

AUTO_REVIEW_ALLOWED_FIELDS = {"speaking_style", "growth_notes"}
AUTO_REVIEW_BLOCKED_MARKERS = (
    "恋人",
    "男朋友",
    "女朋友",
    "老婆",
    "老公",
    "主人",
    "改名",
    "名字",
    "身份",
    "关系",
    "只属于",
    "专属",
)
AUTO_REVIEW_NOTE = "自动审核代理：用户在聊天中明确提出低风险相处要求，仅调整回应方式。"
AUTO_REVIEW_NO_CHANGE_NOTE = "自动审核代理：当前回应方式已覆盖这项低风险要求，无需新增人格版本。"
AUTO_REVIEW_CORE_CHANGE_NOTE = "自动适配边界：含糊或推断性的关系、身份或核心偏好不会静默改写；用户可在聊天中明确设置或编辑资料。"
AUTO_CORE_UPDATE_NOTE = "自动适配：用户在聊天中明确设置了人格核心资料，已直接应用并保留版本记录。"
AUTO_GUIDANCE_RECONCILE_NOTE = "自动适配：用户已替代或停止聊天中的相处指导，已移除人格中失效的回应方式依据。"
EXPLICIT_RELATIONSHIP_LABELS = {
    "关系未定": "关系未定",
    "朋友": "朋友",
    "好朋友": "好朋友",
    "恋人": "恋人",
    "男朋友": "男朋友",
    "女朋友": "女朋友",
    "搭档": "搭档",
    "伙伴": "伙伴",
    "倾听者": "倾听者",
    "陪伴者": "陪伴者",
    "老师": "老师",
    "姐姐": "姐姐",
    "哥哥": "哥哥",
    "妹妹": "妹妹",
    "弟弟": "弟弟",
}
EXPLICIT_RELATIONSHIP_VALUE = r"(关系未定|好朋友|朋友|恋人|男朋友|女朋友|搭档|伙伴|倾听者|陪伴者|老师|姐姐|哥哥|妹妹|弟弟)"
EXPLICIT_RELATIONSHIP_PATTERNS = (
    re.compile(
        r"(?:把|将)?\s*(?:你和我的|我们的|咱们的)\s*关系\s*(?:改成|改为|设成|设为|设置为|定义为|变成)\s*"
        + EXPLICIT_RELATIONSHIP_VALUE
    ),
    re.compile(
        r"(?:从现在开始|以后|今后)\s*(?:你|你就)\s*(?:是|做|当|成为)\s*(?:我的|我)?\s*"
        + EXPLICIT_RELATIONSHIP_VALUE
        + r"(?:吧|了)?(?=[，。！？,.!?；;\s]|$)"
    ),
    re.compile(
        r"(?:从现在开始|以后|今后)\s*(?:我们|咱们)\s*(?:就是|是|成为)\s*"
        + EXPLICIT_RELATIONSHIP_VALUE
        + r"(?:吧|了)?(?=[，。！？,.!?；;\s]|$)"
    ),
)
EXPLICIT_RELATIONSHIP_EXIT_PATTERNS = (
    re.compile(
        r"(?:^|[，。！？,.!?；;])\s*(?:从现在开始|以后|今后|现在)?\s*"
        r"(?:你|我们|咱们)\s*(?:先)?(?:不要|别|别再|不再|不想再)\s*(?:继续)?"
        r"(?:当|做|是)\s*(?:我的|我)?\s*(?:恋人|情侣|男朋友|女朋友)"
        r"(?:了|吧)?(?=[，。！？,.!?；;\s]|$)"
    ),
    re.compile(
        r"(?:^|[，。！？,.!?；;])\s*(?:我们|咱们)\s*(?:先)?"
        r"(?:不要|别|别再|不再|不想再)\s*(?:定义|确定)\s*关系"
        r"(?:了|吧)?(?=[，。！？,.!?；;\s]|$)"
    ),
    re.compile(r"(?:^|[，。！？,.!?；;])\s*(?:我们|咱们)\s*分手(?:了|吧)?(?=[，。！？,.!?；;\s]|$)"),
)
EXPLICIT_NAME_VALUE = r"[“\"']?([^，。！？,.!?；;\s“”\"']{1,40}?)[”\"']?(?:吧)?(?=[，。！？,.!?；;\s]|$)"
EXPLICIT_NAME_PATTERNS = (
    re.compile(r"(?:把|将)\s*你的名字\s*(?:改成|改为|设成|设为)\s*" + EXPLICIT_NAME_VALUE),
    re.compile(r"(?:从现在开始|以后|今后)\s*你(?:就)?\s*叫(?:做)?\s*" + EXPLICIT_NAME_VALUE),
    re.compile(r"(?:从现在开始|以后|今后)\s*(?:我(?:就)?\s*)?叫你\s*" + EXPLICIT_NAME_VALUE),
)
EXPLICIT_CORE_NEGATION_PREFIX_RE = re.compile(r"(?:不要|别|别再|不想|不希望|不能|不许|不用|无需)\s*$")


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
    source_context = _source_context(
        user_id,
        persona_id,
        strong_feedback_only=origin_value in {"explicit_feedback", "profile_request"},
    )
    if origin_value in {"explicit_feedback", "profile_request"} and trigger_memory_uids:
        focus_uids = {str(uid) for uid in trigger_memory_uids if uid}
        source_context["feedback_facts"] = [
            item for item in source_context.get("feedback_facts", [])
            if str((item or {}).get("uid") or "") in focus_uids
        ]
        source_context["feedback_relations"] = [
            item for item in source_context.get("feedback_relations", [])
            if str((item or {}).get("uid") or "") in focus_uids
        ]
    source_context = scrub_identity_obj(source_context)
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
        # Separate chat instructions keep a sensitive pending request from being
        # overwritten by a later harmless style request on the same base version.
        if origin_value == "explicit_feedback":
            existing = None
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


def maybe_queue_revision_from_feedback(
    user_id: int,
    persona_id: int,
    source_message_id: int,
    *,
    allow_handled_core: bool = False,
) -> dict[str, Any] | None:
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
    candidate = generate_revision_suggestion(
        user_id,
        persona_id,
        reason,
        use_llm=False,
        origin="explicit_feedback",
        trigger_message_id=source_message_id,
        trigger_memory_uids=[str(item.get("uid") or "") for item in triggers if item.get("uid")],
    )
    maybe_auto_review_revision(user_id, int(candidate["id"]), allow_handled_core=allow_handled_core)
    return _present_suggestion(_suggestion_for_user(user_id, int(candidate["id"])))


def maybe_apply_explicit_core_update_from_chat(
    user_id: int,
    persona_id: int,
    user_text: str,
    source_message_id: int,
) -> dict[str, Any] | None:
    """Apply unambiguous user commands that explicitly set persona name or relationship."""
    updates = _explicit_core_updates(user_text)
    if not updates:
        return None
    persona = _persona_for_user(user_id, persona_id)
    current = _public_persona(persona)
    suggestion = dict(current)
    change_notes: list[str] = []
    if updates.get("relationship") and updates["relationship"] != current.get("relationship"):
        suggestion["relationship"] = updates["relationship"]
        change_notes.append(f"用户在聊天中明确将关系设为{updates['relationship']}。")
    if updates.get("name") and updates["name"] != current.get("name"):
        suggestion["name"] = updates["name"]
        change_notes.append(f"用户在聊天中明确将名字设为{updates['name']}。")
    if not change_notes:
        return None
    suggestion["change_notes"] = change_notes
    suggestion["prompt"] = build_prompt(suggestion)
    suggestion = _normalize_suggestion(suggestion, persona)
    ts = now_ts()
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO persona_revision_suggestions (
                user_id, persona_id, status, base_version, origin, trigger_message_id,
                reason, suggestion_json, source_context_json, created_at, updated_at
            )
            VALUES (?, ?, 'pending', ?, 'explicit_core_update', ?,
                    '用户在聊天中明确设置核心资料', ?, ?, ?, ?)
            """,
            (
                user_id,
                persona_id,
                int(persona.get("version", 1) or 1),
                source_message_id,
                json.dumps(suggestion, ensure_ascii=False),
                json.dumps({"explicit_user_message": scrub_identity_text(str(user_text or "").strip())}, ensure_ascii=False),
                ts,
                ts,
            ),
        )
        suggestion_id = int(cursor.lastrowid)
    return apply_revision_suggestion(
        user_id,
        suggestion_id,
        reviewer_user_id=None,
        decision_actor="adaptive_runtime",
        decision_note=AUTO_CORE_UPDATE_NOTE,
    )


def maybe_reconcile_inactive_chat_guidance_style(user_id: int, persona_id: int) -> dict[str, Any] | None:
    persona = _persona_for_user(user_id, persona_id)
    current = _public_persona(persona)
    with get_db() as db:
        rows = db.execute(
            """
            SELECT persona_revision_suggestions.source_context_json
            FROM persona_growth_requests
            JOIN persona_revision_suggestions
              ON persona_revision_suggestions.user_id = persona_growth_requests.user_id
             AND persona_revision_suggestions.persona_id = persona_growth_requests.persona_id
             AND persona_revision_suggestions.trigger_message_id = persona_growth_requests.source_message_id
            WHERE persona_growth_requests.user_id = ? AND persona_growth_requests.persona_id = ?
              AND persona_growth_requests.request_origin = 'chat_feedback'
              AND persona_growth_requests.withdrawn_at > 0
              AND persona_growth_requests.source_message_id IS NOT NULL
              AND persona_revision_suggestions.origin = 'explicit_feedback'
              AND persona_revision_suggestions.status = 'applied'
            ORDER BY persona_revision_suggestions.id ASC
            """,
            (user_id, persona_id),
        ).fetchall()
    inactive_feedback_texts: list[str] = []
    for row in rows:
        try:
            source_context = json.loads(row["source_context_json"] or "{}")
        except Exception:
            source_context = {}
        for item in [*(source_context.get("feedback_facts") or []), *(source_context.get("feedback_relations") or [])]:
            text = scrub_identity_text(str((item or {}).get("text") or "").strip())
            if text and text not in inactive_feedback_texts:
                inactive_feedback_texts.append(text)
    if not inactive_feedback_texts:
        return None
    speaking_style = _strip_inactive_feedback_notes(str(current.get("speaking_style") or ""), inactive_feedback_texts)
    current_growth_notes = str(current.get("growth_notes") or "")
    growth_notes = _strip_inactive_feedback_notes(current_growth_notes, inactive_feedback_texts)
    if growth_notes != current_growth_notes:
        growth_notes = _strip_inactive_feedback_notes(
            growth_notes,
            ["自动候选：用户在聊天中明确提出了相处方式调整"],
        ) or "以用户当前仍有效的相处要求为准。"
    if speaking_style == current.get("speaking_style", "") and growth_notes == current.get("growth_notes", ""):
        return None
    suggestion = dict(current)
    suggestion["speaking_style"] = speaking_style
    suggestion["growth_notes"] = growth_notes
    suggestion["change_notes"] = ["用户已替代或停止聊天中的相处指导，移除失效的回应方式依据。"]
    suggestion["prompt"] = build_prompt(suggestion)
    suggestion = _normalize_suggestion(suggestion, persona)
    ts = now_ts()
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO persona_revision_suggestions (
                user_id, persona_id, status, base_version, origin, reason,
                suggestion_json, source_context_json, created_at, updated_at
            )
            VALUES (?, ?, 'pending', ?, 'guidance_reconcile',
                    '自动同步已失效的聊天相处指导', ?, ?, ?, ?)
            """,
            (
                user_id,
                persona_id,
                int(persona.get("version", 1) or 1),
                json.dumps(suggestion, ensure_ascii=False),
                json.dumps({"inactive_feedback_texts": inactive_feedback_texts}, ensure_ascii=False),
                ts,
                ts,
            ),
        )
        suggestion_id = int(cursor.lastrowid)
    return apply_revision_suggestion(
        user_id,
        suggestion_id,
        reviewer_user_id=None,
        decision_actor="adaptive_runtime",
        decision_note=AUTO_GUIDANCE_RECONCILE_NOTE,
    )


def maybe_auto_review_revision(
    user_id: int,
    suggestion_id: int,
    *,
    allow_handled_core: bool = False,
) -> dict[str, Any] | None:
    """Apply only narrow, explicit chat-style preferences without waiting for an admin."""
    row = _suggestion_for_user(user_id, suggestion_id)
    if row.get("status") != "pending" or row.get("origin") != "explicit_feedback":
        return None
    presented = _present_suggestion(dict(row))
    if presented.get("stale"):
        return None
    changed_fields = {str(item.get("field") or "") for item in presented.get("changes") or []}
    if "speaking_style" not in changed_fields:
        return dismiss_revision_suggestion(
            user_id,
            suggestion_id,
            reviewer_user_id=None,
            decision_actor="review_agent",
            decision_note=AUTO_REVIEW_NO_CHANGE_NOTE,
        )
    if not changed_fields.issubset(AUTO_REVIEW_ALLOWED_FIELDS):
        return dismiss_revision_suggestion(
            user_id,
            suggestion_id,
            reviewer_user_id=None,
            decision_actor="adaptive_runtime",
            decision_note=AUTO_REVIEW_CORE_CHANGE_NOTE,
        )
    evidence = presented.get("source_context") or {}
    feedback_text = "\n".join(
        str(item.get("text") or "")
        for item in [*(evidence.get("feedback_facts") or []), *(evidence.get("feedback_relations") or [])]
        if item
    )
    if not allow_handled_core and any(marker in feedback_text for marker in AUTO_REVIEW_BLOCKED_MARKERS):
        return dismiss_revision_suggestion(
            user_id,
            suggestion_id,
            reviewer_user_id=None,
            decision_actor="adaptive_runtime",
            decision_note=AUTO_REVIEW_CORE_CHANGE_NOTE,
        )
    return apply_revision_suggestion(
        user_id,
        suggestion_id,
        reviewer_user_id=None,
        decision_actor="review_agent",
        decision_note=AUTO_REVIEW_NOTE,
    )


def _strip_inactive_feedback_notes(value: str, inactive_feedback_texts: list[str]) -> str:
    cleaned = scrub_identity_text(value)
    for text in sorted(inactive_feedback_texts, key=len, reverse=True):
        cleaned = cleaned.replace(text, "")
    cleaned = re.sub(r"\s*/\s*(?=(?:/|$))", "", cleaned)
    cleaned = re.sub(r"\n适配备注：\s*(?:/+\s*)?$", "", cleaned)
    cleaned = re.sub(r"适配备注：\s*/\s*", "适配备注：", cleaned)
    cleaned = re.sub(r"\s*/\s*/\s*", " / ", cleaned)
    cleaned = re.sub(r"^\s*/\s*|\s*/\s*$", "", cleaned)
    return cleaned.strip()


def _explicit_core_updates(user_text: str) -> dict[str, str]:
    text = scrub_identity_text(str(user_text or "").strip())
    updates: dict[str, str] = {}
    for pattern in EXPLICIT_RELATIONSHIP_PATTERNS:
        relationship_match = pattern.search(text)
        if relationship_match and not _has_negated_core_prefix(text, relationship_match.start()):
            updates["relationship"] = EXPLICIT_RELATIONSHIP_LABELS[relationship_match.group(1)]
            break
    if "relationship" not in updates:
        for pattern in EXPLICIT_RELATIONSHIP_EXIT_PATTERNS:
            if pattern.search(text):
                updates["relationship"] = "关系未定"
                break
    for pattern in EXPLICIT_NAME_PATTERNS:
        match = pattern.search(text)
        if not match or _has_negated_core_prefix(text, match.start()):
            continue
        raw_name = str(match.group(1) or "").strip()
        clean_name = scrub_identity_text(raw_name).strip()
        if clean_name and clean_name == raw_name and len(clean_name) <= 40:
            updates["name"] = clean_name
        break
    return updates


def _has_negated_core_prefix(text: str, match_start: int) -> bool:
    prefix = text[max(0, match_start - 12):match_start]
    return bool(EXPLICIT_CORE_NEGATION_PREFIX_RE.search(prefix))


def apply_revision_suggestion(
    user_id: int,
    suggestion_id: int,
    *,
    reviewer_user_id: int | None = None,
    decision_actor: str = "admin",
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
        clean_decision_actor = str(decision_actor or "admin").strip()[:40] or "admin"

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
                decided_at = ?, decided_by_user_id = ?, decision_actor = ?,
                decision_note = ?, updated_at = ?
            WHERE id = ?
            """,
            (ts, new_version, ts, reviewer_user_id, clean_decision_actor, clean_decision_note, ts, suggestion_id),
        )
        persona_row = db.execute("SELECT * FROM personas WHERE id = ?", (persona_id,)).fetchone()
    return {"persona": dict_from_row(persona_row), "suggestion_id": suggestion_id, "version": new_version}


def dismiss_revision_suggestion(
    user_id: int,
    suggestion_id: int,
    *,
    reviewer_user_id: int | None = None,
    decision_actor: str = "admin",
    decision_note: str = "",
) -> dict[str, Any]:
    clean_decision_note = scrub_identity_text(decision_note.strip())[:1000]
    clean_decision_actor = str(decision_actor or "admin").strip()[:40] or "admin"
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
                decision_actor = ?, decision_note = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (ts, reviewer_user_id, clean_decision_actor, clean_decision_note, ts, suggestion_id, user_id),
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
