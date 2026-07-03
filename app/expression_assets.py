from __future__ import annotations

import re
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
        "cooldown_turns": 4,
        "sort_order": 10,
        "description": "轻微放松、接住情绪时使用。",
        "prompt_hint": "轻微放松、温和接住情绪。",
        "aliases": ("笑一下", "笑了笑"),
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
        "cooldown_turns": 5,
        "sort_order": 20,
        "description": "轻松回应或温和打趣时使用。",
        "prompt_hint": "轻松回应或温和打趣，不用于严肃低落场景。",
        "aliases": ("偷笑", "轻轻笑", "轻轻笑了一下"),
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
        "cooldown_turns": 8,
        "sort_order": 30,
        "description": "用户明显低落或风险升高时使用。",
        "prompt_hint": "用户明显低落、受伤或风险升高时使用；不要用来夸张表演。",
        "aliases": ("担忧", "皱眉", "有点担心"),
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
        "cooldown_turns": 4,
        "sort_order": 40,
        "description": "放慢语气、减少压迫感时使用。",
        "prompt_hint": "放慢语气、减少压迫感，适合安慰或谨慎回应。",
        "aliases": ("小声", "压低声音", "轻轻说"),
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
        "cooldown_turns": 4,
        "sort_order": 50,
        "description": "需要一点留白，不急着推进话题时使用。",
        "prompt_hint": "需要一点留白、不急着推进话题时使用。",
        "aliases": ("沉默", "想了想", "停一下"),
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
        "cooldown_turns": 3,
        "sort_order": 60,
        "description": "确认、认同或轻轻接话时使用。",
        "prompt_hint": "确认、认同或轻轻接话时使用。",
        "aliases": ("轻轻点头", "点了点头"),
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
        lifecycle_status = _normalize_lifecycle_status(None if setting is None else setting.get("lifecycle_status"))
        media_review_status = _normalize_media_review_status(None if setting is None else setting.get("media_review_status"))
        asset["lifecycle_status"] = lifecycle_status
        asset["media_review_status"] = media_review_status
        if setting:
            can_use_media = media_review_status == "approved" or include_admin_metadata
            if can_use_media:
                configured_asset_kind = str(setting.get("asset_kind") or "").strip()
                if configured_asset_kind:
                    asset["asset_kind"] = _normalize_asset_kind(configured_asset_kind)
                for field in ("media_url", "thumbnail_url", "alt_text"):
                    configured = str(setting.get(field) or "").strip()
                    if configured:
                        asset[field] = configured
        asset["media_url"] = str(asset.get("media_url") or "")
        asset["thumbnail_url"] = str(asset.get("thumbnail_url") or asset.get("media_url") or "")
        asset["alt_text"] = str(asset.get("alt_text") or asset.get("display_text") or asset.get("label") or "")
        asset["enabled"] = (True if setting is None else bool(int(setting.get("enabled", 1) or 0))) and lifecycle_status == "active"
        configured_cooldown = -1 if setting is None else int(setting.get("cooldown_turns", -1) or -1)
        asset["cooldown_turns"] = configured_cooldown if configured_cooldown >= 0 else int(asset.get("cooldown_turns") or 0)
        if include_admin_metadata:
            asset["admin_note"] = "" if setting is None else str(setting.get("admin_note") or "")
            asset["updated_at"] = 0 if setting is None else int(setting.get("updated_at") or 0)
            asset["cooldown_turns_override"] = configured_cooldown if configured_cooldown >= 0 else None
            asset["media_review_note"] = "" if setting is None else str(setting.get("media_review_note") or "")
            asset["media_source"] = "" if setting is None else str(setting.get("media_source") or "")
            asset["media_source_detail"] = "" if setting is None else str(setting.get("media_source_detail") or "")
        if include_disabled or asset["enabled"]:
            result.append(asset)
    return result


def expression_protocol_prompt() -> str:
    lines = []
    for asset in expression_assets_public():
        expression_type = str(asset["expression_type"])
        label = str(asset["label"])
        hint = str(asset.get("prompt_hint") or asset.get("description") or "").strip()
        group = str(asset.get("group") or "general")
        risk_level = str(asset.get("risk_level") or "low")
        intensity = int(asset.get("intensity") or 1)
        cooldown_turns = int(asset.get("cooldown_turns") or 0)
        tag = f"[[expression:{expression_type}:{label}]]"
        lines.append(
            f"  - `{tag}`：{hint}（分组：{group}；风险：{risk_level}；强度：{intensity}；冷却：{cooldown_turns}轮）"
        )
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


def expression_alias_match(text: str, *, include_disabled: bool = False) -> dict[str, str] | None:
    compact = re.sub(r"\s+", "", str(text or ""))
    if not compact:
        return None
    matches: list[tuple[int, dict[str, Any]]] = []
    for asset in expression_assets_public(include_disabled=include_disabled):
        aliases = [str(asset.get("label") or "")]
        aliases.extend(str(alias or "") for alias in asset.get("aliases") or [])
        for alias in aliases:
            alias = re.sub(r"\s+", "", alias)
            if alias and alias in compact:
                matches.append((len(alias), asset))
    if not matches:
        return None
    asset = max(matches, key=lambda item: item[0])[1]
    return {
        "expression_type": str(asset.get("expression_type") or ""),
        "label": str(asset.get("label") or ""),
    }


def update_expression_asset_setting(
    expression_type: str,
    label: str,
    *,
    enabled: bool,
    admin_note: str = "",
    cooldown_turns: int | None = None,
    lifecycle_status: str | None = None,
    asset_kind: str | None = None,
    media_url: str | None = None,
    thumbnail_url: str | None = None,
    alt_text: str | None = None,
    media_source: str | None = None,
    media_source_detail: str | None = None,
    media_review_status: str | None = None,
    media_review_note: str | None = None,
    updated_by_user_id: int | None = None,
) -> dict[str, Any]:
    expression_type = str(expression_type or "").strip()
    label = str(label or "").strip()
    if (expression_type, label) not in _known_asset_keys():
        raise ValueError("expression asset not found")
    from .database import get_db, now_ts

    ts = now_ts()
    with get_db() as db:
        existing = db.execute(
            """
            SELECT cooldown_turns, lifecycle_status, asset_kind, media_url, thumbnail_url, alt_text,
                   media_source, media_source_detail, media_review_status, media_review_note
            FROM expression_asset_settings
            WHERE expression_type = ? AND label = ?
            """,
            (expression_type, label),
        ).fetchone()
        if cooldown_turns is None:
            stored_cooldown = int(existing["cooldown_turns"]) if existing else -1
        else:
            stored_cooldown = max(0, min(int(cooldown_turns), 20))
        stored_lifecycle_status = _normalize_lifecycle_status(
            lifecycle_status if lifecycle_status is not None else (existing["lifecycle_status"] if existing else "active")
        )
        stored_asset_kind = _normalize_asset_kind(
            asset_kind if asset_kind is not None else (existing["asset_kind"] if existing else "")
        )
        stored_media_url = _stored_setting_text(media_url, existing, "media_url")
        stored_thumbnail_url = _stored_setting_text(thumbnail_url, existing, "thumbnail_url")
        stored_alt_text = _stored_setting_text(alt_text, existing, "alt_text")
        stored_media_source = _stored_setting_text(media_source, existing, "media_source")
        stored_media_source_detail = _stored_setting_text(media_source_detail, existing, "media_source_detail")
        stored_media_review_status = _normalize_media_review_status(
            media_review_status if media_review_status is not None else (existing["media_review_status"] if existing else "approved")
        )
        stored_media_review_note = _stored_setting_text(media_review_note, existing, "media_review_note")
        db.execute(
            """
            INSERT INTO expression_asset_settings (
                expression_type, label, enabled, cooldown_turns, lifecycle_status,
                asset_kind, media_url, thumbnail_url, alt_text, media_source, media_source_detail,
                media_review_status, media_review_note,
                admin_note, updated_by_user_id, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(expression_type, label) DO UPDATE SET
                enabled = excluded.enabled,
                cooldown_turns = excluded.cooldown_turns,
                lifecycle_status = excluded.lifecycle_status,
                asset_kind = excluded.asset_kind,
                media_url = excluded.media_url,
                thumbnail_url = excluded.thumbnail_url,
                alt_text = excluded.alt_text,
                media_source = excluded.media_source,
                media_source_detail = excluded.media_source_detail,
                media_review_status = excluded.media_review_status,
                media_review_note = excluded.media_review_note,
                admin_note = excluded.admin_note,
                updated_by_user_id = excluded.updated_by_user_id,
                updated_at = excluded.updated_at
            """,
            (
                expression_type,
                label,
                1 if enabled else 0,
                stored_cooldown,
                stored_lifecycle_status,
                stored_asset_kind,
                stored_media_url,
                stored_thumbnail_url,
                stored_alt_text,
                stored_media_source,
                stored_media_source_detail,
                stored_media_review_status,
                stored_media_review_note,
                str(admin_note or "")[:500],
                updated_by_user_id,
                ts,
            ),
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
                SELECT expression_type, label, enabled, cooldown_turns, lifecycle_status,
                       asset_kind, media_url, thumbnail_url, alt_text, media_source,
                       media_source_detail, media_review_status, media_review_note,
                       admin_note, updated_at
                FROM expression_asset_settings
                """
            ).fetchall()
        return {
            (str(row["expression_type"]), str(row["label"])): dict_from_row(row)
            for row in rows
        }
    except Exception:
        return {}


def _normalize_lifecycle_status(value: Any) -> str:
    status = str(value or "active").strip().lower()
    if status not in {"active", "paused", "archived"}:
        return "active"
    return status


def _normalize_asset_kind(value: Any) -> str:
    kind = str(value or "").strip().lower()
    if not kind:
        return ""
    if kind not in {"text_badge", "image", "gif", "avatar_expression"}:
        return "text_badge"
    return kind


def _normalize_media_review_status(value: Any) -> str:
    status = str(value or "approved").strip().lower()
    if status not in {"pending", "approved", "rejected"}:
        return "pending"
    return status


def _stored_setting_text(value: str | None, existing: Any, field: str) -> str:
    if value is None:
        return str(existing[field] or "") if existing else ""
    return str(value or "").strip()[:500]
