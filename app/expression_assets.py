from __future__ import annotations

from typing import Any


EXPRESSION_ASSETS: tuple[dict[str, Any], ...] = (
    {
        "expression_type": "mood",
        "label": "微笑",
        "display_text": "微笑",
        "icon": "⌣",
        "asset_kind": "text_badge",
        "group": "warmth",
        "risk_level": "low",
        "intensity": 1,
        "sort_order": 10,
        "description": "轻微放松、接住情绪时使用。",
        "prompt_hint": "轻微放松、温和接住情绪。",
    },
    {
        "expression_type": "mood",
        "label": "轻笑",
        "display_text": "轻笑",
        "icon": "◡",
        "asset_kind": "text_badge",
        "group": "warmth",
        "risk_level": "low",
        "intensity": 1,
        "sort_order": 20,
        "description": "轻松回应或温和打趣时使用。",
        "prompt_hint": "轻松回应或温和打趣，不用于严肃低落场景。",
    },
    {
        "expression_type": "mood",
        "label": "担心",
        "display_text": "担心",
        "icon": "!",
        "asset_kind": "text_badge",
        "group": "care",
        "risk_level": "medium",
        "intensity": 2,
        "sort_order": 30,
        "description": "用户明显低落或风险升高时使用。",
        "prompt_hint": "用户明显低落、受伤或风险升高时使用；不要用来夸张表演。",
    },
    {
        "expression_type": "tone",
        "label": "轻声",
        "display_text": "轻声",
        "icon": "~",
        "asset_kind": "text_badge",
        "group": "support",
        "risk_level": "low",
        "intensity": 1,
        "sort_order": 40,
        "description": "放慢语气、减少压迫感时使用。",
        "prompt_hint": "放慢语气、减少压迫感，适合安慰或谨慎回应。",
    },
    {
        "expression_type": "tone",
        "label": "停顿",
        "display_text": "停顿",
        "icon": "...",
        "asset_kind": "text_badge",
        "group": "support",
        "risk_level": "low",
        "intensity": 1,
        "sort_order": 50,
        "description": "需要一点留白，不急着推进话题时使用。",
        "prompt_hint": "需要一点留白、不急着推进话题时使用。",
    },
    {
        "expression_type": "gesture",
        "label": "点头",
        "display_text": "点头",
        "icon": "✓",
        "asset_kind": "text_badge",
        "group": "acknowledgement",
        "risk_level": "low",
        "intensity": 1,
        "sort_order": 60,
        "description": "确认、认同或轻轻接话时使用。",
        "prompt_hint": "确认、认同或轻轻接话时使用。",
    },
)


def expression_assets_public(
    *,
    include_disabled: bool = False,
    include_admin_metadata: bool = False,
) -> list[dict[str, Any]]:
    settings = _expression_asset_settings()
    result = []
    for item in sorted(EXPRESSION_ASSETS, key=lambda asset: int(asset.get("sort_order") or 0)):
        asset = dict(item)
        setting = settings.get(_asset_key(asset))
        asset["enabled"] = True if setting is None else bool(int(setting.get("enabled", 1) or 0))
        if include_admin_metadata:
            asset["admin_note"] = "" if setting is None else str(setting.get("admin_note") or "")
            asset["updated_at"] = 0 if setting is None else int(setting.get("updated_at") or 0)
        if include_disabled or asset["enabled"]:
            result.append(asset)
    return result


def expression_protocol_prompt() -> str:
    lines = []
    for asset in expression_assets_public():
        expression_type = str(asset["expression_type"])
        label = str(asset["label"])
        hint = str(asset.get("prompt_hint") or asset.get("description") or "").strip()
        tag = f"[[expression:{expression_type}:{label}]]"
        lines.append(f"  - `{tag}`：{hint}")
    return "\n".join(lines)


def active_expression_labels() -> dict[str, set[str]]:
    labels: dict[str, set[str]] = {}
    for asset in expression_assets_public():
        labels.setdefault(str(asset["expression_type"]), set()).add(str(asset["label"]))
    return labels


def expression_asset(expression_type: str, label: str) -> dict[str, Any] | None:
    for asset in expression_assets_public(include_disabled=True):
        if (
            str(asset.get("expression_type") or "") == str(expression_type or "")
            and str(asset.get("label") or "") == str(label or "")
        ):
            return asset
    return None


def update_expression_asset_setting(
    expression_type: str,
    label: str,
    *,
    enabled: bool,
    admin_note: str = "",
    updated_by_user_id: int | None = None,
) -> dict[str, Any]:
    expression_type = str(expression_type or "").strip()
    label = str(label or "").strip()
    if (expression_type, label) not in _known_asset_keys():
        raise ValueError("expression asset not found")
    from .database import get_db, now_ts

    ts = now_ts()
    with get_db() as db:
        db.execute(
            """
            INSERT INTO expression_asset_settings (
                expression_type, label, enabled, admin_note, updated_by_user_id, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(expression_type, label) DO UPDATE SET
                enabled = excluded.enabled,
                admin_note = excluded.admin_note,
                updated_by_user_id = excluded.updated_by_user_id,
                updated_at = excluded.updated_at
            """,
            (expression_type, label, 1 if enabled else 0, str(admin_note or "")[:500], updated_by_user_id, ts),
        )
    return next(
        asset
        for asset in expression_assets_public(include_disabled=True, include_admin_metadata=True)
        if str(asset["expression_type"]) == expression_type and str(asset["label"]) == label
    )


def _known_asset_keys() -> set[tuple[str, str]]:
    return {_asset_key(asset) for asset in EXPRESSION_ASSETS}


def _asset_key(asset: dict[str, Any]) -> tuple[str, str]:
    return (str(asset.get("expression_type") or ""), str(asset.get("label") or ""))


def _expression_asset_settings() -> dict[tuple[str, str], dict[str, Any]]:
    try:
        from .database import dict_from_row, get_db

        with get_db() as db:
            rows = db.execute(
                """
                SELECT expression_type, label, enabled, admin_note, updated_at
                FROM expression_asset_settings
                """
            ).fetchall()
        return {
            (str(row["expression_type"]), str(row["label"])): dict_from_row(row)
            for row in rows
        }
    except Exception:
        return {}
