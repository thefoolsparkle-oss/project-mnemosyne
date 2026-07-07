from __future__ import annotations

import json
import re
from typing import Any

from .database import dict_from_row, get_db, now_ts
from .expression_assets import expression_assets_public


VALID_EXPRESSION_STYLES = {"", "restrained", "playful", "warm", "neutral"}


def persona_expression_style_context(
    user_id: int | None,
    persona_id: int | None,
    persona: dict[str, Any],
) -> dict[str, Any]:
    inferred = infer_persona_expression_style(persona)
    if not user_id or not persona_id:
        return inferred
    setting = persona_expression_style_setting(user_id, persona_id)
    if not setting.get("explicit"):
        return inferred
    style = str(setting.get("style") or inferred["expression_persona_style"])
    preferred_groups = setting.get("preferred_groups") or inferred["expression_persona_preferred_groups"]
    avoid_labels = setting.get("avoid_labels") or inferred["expression_persona_avoid_labels"]
    return {
        "expression_persona_style": style,
        "expression_persona_preferred_groups": preferred_groups,
        "expression_persona_avoid_labels": avoid_labels,
        "expression_persona_style_source": "admin",
        "expression_persona_style_note": setting.get("admin_note") or "",
    }


def infer_persona_expression_style(persona: dict[str, Any]) -> dict[str, Any]:
    text = re.sub(
        r"\s+",
        "",
        " ".join(
            str(persona.get(field) or "").lower()
            for field in ("summary", "relationship", "speaking_style", "growth_notes")
        ),
    )
    if any(marker in text for marker in ("安静", "简短", "克制", "慢", "少说", "沉稳", "可靠")):
        return {
            "expression_persona_style": "restrained",
            "expression_persona_preferred_groups": ["support", "acknowledgement"],
            "expression_persona_avoid_labels": ["轻笑"],
            "expression_persona_style_source": "inferred",
        }
    if any(marker in text for marker in ("活泼", "打趣", "俏皮", "开朗", "玩笑", "轻松")):
        return {
            "expression_persona_style": "playful",
            "expression_persona_preferred_groups": ["warmth", "acknowledgement"],
            "expression_persona_avoid_labels": ["担心"],
            "expression_persona_style_source": "inferred",
        }
    if any(marker in text for marker in ("恋人", "亲密", "陪伴", "温柔")):
        return {
            "expression_persona_style": "warm",
            "expression_persona_preferred_groups": ["warmth", "support"],
            "expression_persona_avoid_labels": [],
            "expression_persona_style_source": "inferred",
        }
    return {
        "expression_persona_style": "neutral",
        "expression_persona_preferred_groups": [],
        "expression_persona_avoid_labels": [],
        "expression_persona_style_source": "inferred",
    }


def persona_expression_style_setting(user_id: int, persona_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT style, preferred_groups_json, avoid_labels_json, admin_note, updated_by_user_id, updated_at
            FROM persona_expression_styles
            WHERE user_id = ? AND persona_id = ?
            """,
            (user_id, persona_id),
        ).fetchone()
    if not row:
        return {
            "explicit": False,
            "style": "",
            "preferred_groups": [],
            "avoid_labels": [],
            "admin_note": "",
            "updated_by_user_id": None,
            "updated_at": 0,
        }
    item = dict_from_row(row) or {}
    return {
        "explicit": True,
        "style": _normalize_style(item.get("style")),
        "preferred_groups": _load_list(item.get("preferred_groups_json")),
        "avoid_labels": _load_list(item.get("avoid_labels_json")),
        "admin_note": str(item.get("admin_note") or ""),
        "updated_by_user_id": item.get("updated_by_user_id"),
        "updated_at": int(item.get("updated_at") or 0),
    }


def update_persona_expression_style_setting(
    user_id: int,
    persona_id: int,
    *,
    style: str | None = None,
    preferred_groups: list[str] | None = None,
    avoid_labels: list[str] | None = None,
    admin_note: str = "",
    updated_by_user_id: int | None = None,
) -> dict[str, Any]:
    clean_style = _normalize_style(style)
    clean_groups = _normalize_groups(preferred_groups or [])
    clean_labels = _normalize_labels(avoid_labels or [])
    ts = now_ts()
    with get_db() as db:
        db.execute(
            """
            INSERT INTO persona_expression_styles (
                user_id, persona_id, style, preferred_groups_json, avoid_labels_json,
                admin_note, updated_by_user_id, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, persona_id) DO UPDATE SET
                style = excluded.style,
                preferred_groups_json = excluded.preferred_groups_json,
                avoid_labels_json = excluded.avoid_labels_json,
                admin_note = excluded.admin_note,
                updated_by_user_id = excluded.updated_by_user_id,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                persona_id,
                clean_style,
                json.dumps(clean_groups, ensure_ascii=False),
                json.dumps(clean_labels, ensure_ascii=False),
                str(admin_note or "")[:500],
                updated_by_user_id,
                ts,
            ),
        )
    return persona_expression_style_setting(user_id, persona_id)


def _normalize_style(value: Any) -> str:
    style = str(value or "").strip().lower()
    return style if style in VALID_EXPRESSION_STYLES else ""


def _normalize_groups(values: list[str]) -> list[str]:
    known = {str(asset.get("group") or "") for asset in expression_assets_public(include_disabled=True)}
    return _normalize_list(values, known)


def _normalize_labels(values: list[str]) -> list[str]:
    known = {str(asset.get("label") or "") for asset in expression_assets_public(include_disabled=True)}
    return _normalize_list(values, known)


def _normalize_list(values: list[str], known: set[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        clean = str(value or "").strip()
        if clean and clean in known and clean not in result:
            result.append(clean)
    return result[:8]


def _load_list(raw: Any) -> list[str]:
    try:
        data = json.loads(str(raw or "[]"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if str(item or "").strip()]
