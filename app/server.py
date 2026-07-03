from __future__ import annotations

import hashlib
import html
import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Cookie, Depends, FastAPI, File, Header, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import (
    GUEST_SECONDS,
    SESSION_COOKIE,
    SESSION_SECONDS,
    authenticate_user,
    clear_session,
    cleanup_expired_guest_users,
    convert_guest_user,
    create_session,
    create_guest_user,
    create_user,
    current_admin,
    current_user,
    public_user,
    request_session_token,
    set_session_cookie,
)
from .archivist import recall_memories
from .config import load_config
from .database import dict_from_row, get_db, init_db, now_ts
from .db_chat import db_chat, normalize_existing_assistant_messages
from .expression_assets import active_expression_labels, expression_assets_public, update_expression_asset_setting
from .group_chat import (
    add_group_member,
    autonomous_group_turn,
    create_group_conversation,
    group_chat,
    group_messages,
    list_group_conversations,
    mark_group_conversation_read,
    remove_group_member,
    update_group_conversation,
)
from .conversation_memory import refresh_conversation_summary
from .identity import is_identity_polluted_boundary, scrub_identity_text
from .layered_memory import (
    apply_memory_decay,
    recall_layered_memory,
    refresh_memory_state,
    refresh_memory_summaries,
    store_layered_memories,
)
from .llm_client import LLMProviderError, api_key_env_present
from .memory_review import context_traces, get_memory_item, memory_review, update_memory_item
from .memory_judge import update_judgement_status
from .memory_conflicts import update_conflict_status
from .memory_eval import (
    list_memory_eval_runs,
    run_chat_context_evaluation,
    run_live_answer_evaluation,
    run_memory_evaluation,
    run_memory_policy_evaluation,
    run_profile_context_evaluation,
    run_profile_live_answer_evaluation,
    run_state_expiry_evaluation,
    run_state_resolution_evaluation,
    seed_memory_eval_data,
)
from .memory_rag import semantic_memory_recall, sync_memory_embeddings
from .memory_policy import policy_snapshot
from .mirror import get_user_insight, update_user_insight
from .persona_forge import build_prompt, forge_persona
from .growth_demo import clear_growth_demo_data, seed_growth_demo_data
from .growth_guidance import deactivate_guidance, supersede_conflicting_guidance
from .sculptor import (
    apply_revision_suggestion,
    dismiss_revision_suggestion,
    generate_revision_suggestion,
    list_revision_suggestions,
    maybe_auto_review_revision,
)


BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
ADMIN_WEB_DIR = BASE_DIR / "admin_web"
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

init_db()
try:
    cleanup_expired_guest_users()
except Exception as exc:
    print("[GuestCleanup] expired guest cleanup skipped:", exc)
try:
    normalize_existing_assistant_messages()
except Exception as exc:
    print("[MessagePresentation] legacy normalization skipped:", exc)
try:
    with get_db() as db:
        pending_chat_revisions = db.execute(
            """
            SELECT persona_revision_suggestions.id, persona_revision_suggestions.user_id
            FROM persona_revision_suggestions
            JOIN personas ON personas.id = persona_revision_suggestions.persona_id
            WHERE persona_revision_suggestions.status = 'pending'
              AND persona_revision_suggestions.origin = 'explicit_feedback'
              AND persona_revision_suggestions.base_version = personas.version
              AND personas.status = 'active'
            ORDER BY persona_revision_suggestions.id ASC
            """
        ).fetchall()
    for pending_revision in pending_chat_revisions:
        maybe_auto_review_revision(int(pending_revision["user_id"]), int(pending_revision["id"]))
except Exception as exc:
    print("[AdaptiveRuntime] pending chat preference reconciliation skipped:", exc)

app = FastAPI(title="忆界树 / Project Mnemosyne")

PERSONA_OPTIONS = {
    "atmosphere": [
        "\u6e29\u67d4\u966a\u4f34",
        "\u51b7\u9759\u514b\u5236",
        "\u6d3b\u6cfc\u5410\u69fd",
        "\u6210\u719f\u53ef\u9760",
    ],
    "relationship": [
        "\u50cf\u670b\u53cb\u4e00\u6837",
        "\u7a33\u5b9a\u966a\u4f34",
        "\u7ed9\u6211\u5f15\u5bfc",
    ],
    "style": [
        "\u77ed\u53e5",
        "\u5c11\u8ffd\u95ee",
        "\u4f1a\u4e3b\u52a8\u5173\u5fc3",
        "\u53ef\u4ee5\u5410\u69fd",
    ],
    "boundaries": [
        "\u73b0\u5b9e\u611f\u5f3a",
        "\u4e0d\u8981\u592a\u9ecf\u4eba",
        "\u4e0d\u8981\u8bf4\u6559",
        "\u5c11\u7528\u8868\u60c5",
    ],
}


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=40)
    password: str = Field(..., min_length=8, max_length=200)
    nickname: str | None = Field(default=None, max_length=60)
    tab_session: bool = False


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=40)
    password: str = Field(..., min_length=8, max_length=200)
    tab_session: bool = False


class GuestConvertRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=40)
    password: str = Field(..., min_length=8, max_length=200)
    nickname: str | None = Field(default=None, max_length=60)
    tab_session: bool = False


class ProfileUpdateRequest(BaseModel):
    nickname: str | None = Field(default=None, max_length=60)
    avatar_url: str | None = Field(default=None, max_length=500)
    gender: str | None = Field(default=None, max_length=20)
    birthday: str | None = Field(default=None, max_length=20)
    signature: str | None = Field(default=None, max_length=200)
    bio: str | None = Field(default=None, max_length=1000)
    preferences: dict[str, Any] | None = None


class PersonaCreateRequest(BaseModel):
    selections: dict[str, list[str]] = Field(default_factory=dict)
    description: str = Field(default="", max_length=2000)
    preferred_name: str | None = Field(default=None, max_length=40)


class PersonaUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=40)
    summary: str | None = Field(default=None, max_length=1000)
    relationship: str | None = Field(default=None, max_length=120)
    speaking_style: str | None = Field(default=None, max_length=300)
    avatar_url: str | None = Field(default=None, max_length=500)
    appearance_description: str | None = Field(default=None, max_length=2000)
    desired_image: str | None = Field(default=None, max_length=2000)


class ExpressionPreferenceUpdateRequest(BaseModel):
    enabled: bool | None = None
    mode: str | None = Field(default=None, max_length=20)


class ExpressionAssetUpdateRequest(BaseModel):
    enabled: bool
    cooldown_turns: int | None = Field(default=None, ge=0, le=20)
    lifecycle_status: str | None = Field(default=None, max_length=20)
    admin_note: str | None = Field(default="", max_length=500)


class ExpressionReviewBulkRequest(BaseModel):
    target_user_id: int | None = None
    persona_id: int | None = None
    limit: int = Field(default=12, ge=1, le=50)
    usage_limit: int = Field(default=80, ge=1, le=500)


class PersonaAvatarGenerateRequest(BaseModel):
    desired_image: str | None = Field(default=None, max_length=2000)


class PersonaVersionRestoreRequest(BaseModel):
    note: str | None = Field(default="", max_length=500)


class PersonaDeleteRequest(BaseModel):
    confirm_name: str = Field(..., min_length=1, max_length=40)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    persona_id: int
    conversation_id: int | None = None
    retry_user_message_id: int | None = None
    client_message_id: str | None = Field(default=None, max_length=80)


class ConversationUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=80)
    status: str | None = Field(default=None, max_length=20)
    pinned: bool | None = None


class GroupConversationCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=80)
    persona_ids: list[int] = Field(..., min_length=2, max_length=6)


class GroupConversationUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=80)
    status: str | None = Field(default=None, max_length=20)
    pinned: bool | None = None


class GroupMemberRequest(BaseModel):
    persona_id: int = Field(..., ge=1)


class GroupAutonomousTurnRequest(BaseModel):
    client_message_id: str | None = Field(default=None, max_length=80)


class GroupChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    group_conversation_id: int
    client_message_id: str | None = Field(default=None, max_length=80)


class MemoryUpdateRequest(BaseModel):
    priority: str | None = None
    locked: bool | None = None
    archived: bool | None = None


class PersonaRevisionRequest(BaseModel):
    reason: str = Field(default="", max_length=1000)


class PersonaRevisionDecisionRequest(BaseModel):
    note: str = Field(default="", max_length=1000)


class PersonaGrowthFeedbackRequest(BaseModel):
    reaction: str = Field(..., max_length=32)
    detail: str = Field(default="", max_length=500)


class PersonaPreferenceRequest(BaseModel):
    detail: str = Field(..., min_length=1, max_length=500)


class PersonaGrowthFeedbackResolutionRequest(BaseModel):
    reviewed_version: int = Field(..., ge=1)
    note: str = Field(default="", max_length=1000)


class InsightUpdateRequest(BaseModel):
    profile_summary: str | None = Field(default=None, max_length=1200)
    interaction_style: list[str] | None = None
    emotional_patterns: list[str] | None = None
    inferred_profile: dict[str, Any] | None = None
    topic_model: dict[str, Any] | None = None
    guidance: dict[str, Any] | None = None


class JudgementStatusRequest(BaseModel):
    status: str = Field(..., max_length=20)


class ConflictStatusRequest(BaseModel):
    status: str = Field(..., max_length=20)


class MemoryEvalRequest(BaseModel):
    reset_seed: bool = True
    include_semantic: bool = False


@app.get("/api/health")
def health():
    return {"ok": True, "database": "sqlite", "db_initialized": True}


@app.get("/api/memory/policy")
def memory_policy(user: dict = Depends(current_user)):
    return {"policy": policy_snapshot()}


@app.get("/api/state")
def state(user: dict = Depends(current_user)):
    return {
        "auth": {"logged_in": True, "user": public_user(user)},
        "profile": _get_profile(int(user["id"])),
    }


def _start_authenticated_session(
    response: Response,
    user: dict[str, Any],
    *,
    tab_session: bool = False,
    max_age: int = SESSION_SECONDS,
) -> dict[str, Any]:
    token = create_session(int(user["id"]), max_age=max_age)
    payload = {"user": public_user(user), "profile": _get_profile(int(user["id"]))}
    if tab_session:
        payload["tab_session_token"] = token
    else:
        set_session_cookie(response, token, max_age=max_age)
    return payload


@app.post("/api/auth/register")
def register(req: RegisterRequest, response: Response):
    user = create_user(req.username, req.password, req.nickname)
    return _start_authenticated_session(response, user, tab_session=req.tab_session)


@app.post("/api/auth/login")
def login(req: LoginRequest, response: Response):
    user = authenticate_user(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="invalid username or password")

    return _start_authenticated_session(response, user, tab_session=req.tab_session)


@app.post("/api/auth/guest")
def guest_login(response: Response, tab_session: bool = False):
    user = create_guest_user()
    return _start_authenticated_session(response, user, tab_session=tab_session, max_age=GUEST_SECONDS)


@app.post("/api/auth/guest/convert")
def convert_guest(req: GuestConvertRequest, response: Response, user: dict = Depends(current_user)):
    converted = convert_guest_user(
        user_id=int(user["id"]),
        username=req.username,
        password=req.password,
        nickname=req.nickname,
    )
    return _start_authenticated_session(response, converted, tab_session=req.tab_session)


@app.post("/api/auth/logout")
def logout(
    response: Response,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    authorization: str | None = Header(default=None),
):
    clear_session(response, request_session_token(session_token, authorization))
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return {"user": public_user(user), "profile": _get_profile(int(user["id"]))}


@app.get("/api/profile")
def profile(user: dict = Depends(current_user)):
    return {"profile": _get_profile(int(user["id"]))}


@app.put("/api/profile")
def update_profile(req: ProfileUpdateRequest, user: dict = Depends(current_user)):
    user_id = int(user["id"])
    current = _get_profile(user_id)
    preferences = req.preferences if req.preferences is not None else current.get("preferences", {})
    ts = now_ts()

    with get_db() as db:
        db.execute(
            """
            UPDATE user_profiles
            SET nickname = ?, avatar_url = ?, gender = ?, birthday = ?, signature = ?, bio = ?,
                preferences_json = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (
                req.nickname if req.nickname is not None else current.get("nickname"),
                req.avatar_url if req.avatar_url is not None else current.get("avatar_url"),
                req.gender if req.gender is not None else current.get("gender"),
                req.birthday if req.birthday is not None else current.get("birthday"),
                req.signature if req.signature is not None else current.get("signature"),
                req.bio if req.bio is not None else current.get("bio"),
                json.dumps(preferences, ensure_ascii=False),
                ts,
                user_id,
            ),
        )

    return {"profile": _get_profile(user_id)}


@app.get("/api/persona-options")
def persona_options():
    return {"options": PERSONA_OPTIONS, "max_per_group": 4}


@app.get("/api/expression-assets")
def expression_assets(user: dict = Depends(current_user)):
    return {"assets": expression_assets_public()}


@app.get("/api/admin/expression-assets")
def admin_expression_assets(admin: dict = Depends(current_admin)):
    return {"assets": expression_assets_public(include_disabled=True, include_admin_metadata=True)}


@app.patch("/api/admin/expression-assets/{expression_type}/{label}")
def admin_update_expression_asset(
    expression_type: str,
    label: str,
    req: ExpressionAssetUpdateRequest,
    admin: dict = Depends(current_admin),
):
    try:
        asset = update_expression_asset_setting(
            expression_type,
            label,
            enabled=req.enabled,
            cooldown_turns=req.cooldown_turns,
            lifecycle_status=req.lifecycle_status,
            admin_note=req.admin_note or "",
            updated_by_user_id=int(admin["id"]),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "asset": asset,
        "assets": expression_assets_public(include_disabled=True, include_admin_metadata=True),
    }


@app.post("/api/uploads/avatar")
async def upload_avatar(file: UploadFile = File(...), user: dict = Depends(current_user)):
    content_type = (file.content_type or "").lower()
    allowed = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    if content_type not in allowed:
        raise HTTPException(status_code=400, detail="only jpg, png, webp or gif images are allowed")

    data = await file.read()
    max_size = 5 * 1024 * 1024
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > max_size:
        raise HTTPException(status_code=400, detail="image must be smaller than 5MB")

    user_dir = UPLOAD_DIR / str(user["id"])
    user_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{allowed[content_type]}"
    path = user_dir / filename
    path.write_bytes(data)
    return {"url": f"/uploads/{user['id']}/{filename}"}


@app.get("/api/memories")
def memories(user: dict = Depends(current_user), persona_id: int | None = None, q: str = ""):
    return {"memories": recall_memories(int(user["id"]), persona_id, q, limit=50)}


@app.get("/api/memory/layered")
def layered_memories(
    user: dict = Depends(current_user),
    persona_id: int | None = None,
    q: str = "",
    include_history: bool = False,
):
    return {"memory": recall_layered_memory(int(user["id"]), persona_id, q, limit=50, include_history=include_history)}


@app.post("/api/memory/summaries/refresh")
def refresh_summaries(user: dict = Depends(current_user), persona_id: int | None = None):
    return {"summaries": refresh_memory_summaries(int(user["id"]), persona_id)}


@app.post("/api/memory/state/refresh")
def refresh_state(user: dict = Depends(current_user), persona_id: int | None = None):
    return {"state": refresh_memory_state(int(user["id"]), persona_id)}


@app.post("/api/memory/decay/apply")
def apply_decay(user: dict = Depends(current_user), persona_id: int | None = None):
    return {"decay": apply_memory_decay(int(user["id"]), persona_id)}


@app.get("/api/admin/memory/links")
def admin_memory_links(
    admin: dict = Depends(current_admin),
    target_user_id: int | None = None,
    uid: str | None = None,
):
    owner_id = _admin_target_user_id(admin, target_user_id)
    query = "SELECT * FROM memory_links WHERE user_id = ?"
    params: list[Any] = [owner_id]
    if uid:
        query += " AND (from_uid = ? OR to_uid = ?)"
        params.extend([uid, uid])
    query += " ORDER BY id ASC"
    with get_db() as db:
        rows = db.execute(query, params).fetchall()
    return {"links": [dict_from_row(row) for row in rows]}


@app.get("/api/admin/chat-context-traces")
def admin_context_traces(
    admin: dict = Depends(current_admin),
    target_user_id: int | None = None,
    persona_id: int | None = None,
    limit: int = 10,
):
    owner_id = _admin_target_user_id(admin, target_user_id)
    _assert_persona_owner(owner_id, persona_id)
    return {"traces": context_traces(owner_id, persona_id, limit)}


@app.get("/api/admin/expression-usage")
def admin_expression_usage(
    admin: dict = Depends(current_admin),
    target_user_id: int | None = None,
    persona_id: int | None = None,
    limit: int = 12,
    usage_limit: int = 80,
):
    owner_id = _admin_target_user_id(admin, target_user_id)
    _assert_persona_owner(owner_id, persona_id)
    limit = max(1, min(int(limit or 12), 50))
    usage_limit = max(limit, min(int(usage_limit or 80), 500))
    persona_filter = int(persona_id) if persona_id else None
    params: list[Any] = [owner_id]
    persona_clause = ""
    if persona_filter:
        persona_clause = "AND message_expressions.persona_id = ?"
        params.append(persona_filter)
    params.append(usage_limit)
    with get_db() as db:
        preference_row = None
        if persona_filter:
            preference_row = db.execute(
                """
                SELECT enabled, mode, source_message_id, updated_at
                FROM expression_preferences
                WHERE user_id = ? AND persona_id = ?
                """,
                (owner_id, persona_filter),
            ).fetchone()
        single_rows = db.execute(
            f"""
            SELECT message_expressions.*, messages.content, messages.created_at AS message_created_at,
                   conversations.title AS conversation_title,
                   personas.name AS persona_name,
                   'single' AS scope
            FROM message_expressions
            JOIN messages ON messages.id = message_expressions.message_id
            JOIN conversations ON conversations.id = message_expressions.conversation_id
            JOIN personas ON personas.id = message_expressions.persona_id
            WHERE message_expressions.user_id = ?
              {persona_clause}
            ORDER BY message_expressions.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        group_params: list[Any] = [owner_id]
        group_persona_clause = ""
        if persona_filter:
            group_persona_clause = "AND group_message_expressions.persona_id = ?"
            group_params.append(persona_filter)
        group_params.append(usage_limit)
        group_rows = db.execute(
            f"""
            SELECT group_message_expressions.*, group_messages.content,
                   group_messages.created_at AS message_created_at,
                   group_conversations.title AS conversation_title,
                   personas.name AS persona_name,
                   'group' AS scope
            FROM group_message_expressions
            JOIN group_messages ON group_messages.id = group_message_expressions.group_message_id
            JOIN group_conversations ON group_conversations.id = group_message_expressions.group_conversation_id
            JOIN personas ON personas.id = group_message_expressions.persona_id
            WHERE group_message_expressions.user_id = ?
              {group_persona_clause}
            ORDER BY group_message_expressions.id DESC
            LIMIT ?
            """,
            group_params,
        ).fetchall()
    asset_map = {
        (str(asset.get("expression_type") or ""), str(asset.get("label") or "")): asset
        for asset in expression_assets_public(include_disabled=True, include_admin_metadata=True)
    }
    usage_rows = [_with_expression_asset_metadata(dict_from_row(row), asset_map) for row in [*single_rows, *group_rows]]
    usage_rows.sort(key=lambda item: int(item.get("created_at") or 0), reverse=True)
    recent = usage_rows[:limit]
    counts: dict[str, dict[str, Any]] = {}
    summary = {
        "window": len(usage_rows),
        "single": 0,
        "group": 0,
        "disabled_asset": 0,
        "medium_risk": 0,
    }
    for item in usage_rows:
        scope = "group" if item.get("scope") == "group" else "single"
        summary[scope] += 1
        if item.get("asset_enabled") is False:
            summary["disabled_asset"] += 1
        if item.get("risk_level") == "medium":
            summary["medium_risk"] += 1
        key = f"{item.get('expression_type') or 'gesture'}:{item.get('label') or ''}"
        if key not in counts:
            counts[key] = {
                "tag": key,
                "count": 0,
                "label": item.get("label") or "",
                "expression_type": item.get("expression_type") or "gesture",
                "display_text": item.get("display_text") or item.get("label") or "",
                "asset_enabled": item.get("asset_enabled", False),
                "risk_level": item.get("risk_level") or "unknown",
                "group": item.get("group") or "unknown",
                "cooldown_turns": int(item.get("cooldown_turns") or 0),
            }
        counts[key]["count"] += 1
    sorted_counts = sorted(counts.values(), key=lambda item: (-int(item["count"]), str(item["tag"])))
    insights: list[dict[str, Any]] = []
    if summary["disabled_asset"]:
        insights.append({
            "kind": "disabled_asset_history",
            "severity": "watch",
            "text": f"{summary['disabled_asset']} 条历史记录来自当前已禁用资源，普通端已隐藏，管理端仍保留审查。",
        })
    if summary["medium_risk"]:
        insights.append({
            "kind": "medium_risk_usage",
            "severity": "watch",
            "text": f"{summary['medium_risk']} 条中风险轻表达出现在统计窗口内，可结合冷却轮数继续观察。",
        })
    if sorted_counts and summary["window"]:
        top = sorted_counts[0]
        share = int(top["count"]) / max(1, int(summary["window"]))
        if int(top["count"]) >= 3 and share >= 0.5:
            insights.append({
                "kind": "concentrated_label",
                "severity": "tune",
                "text": f"{top['display_text']} 占最近统计窗口 {share:.0%}，可考虑提高冷却或改写用途说明。",
                "tag": top["tag"],
            })
    review_items = _expression_review_items(sorted_counts, summary)
    preference = {"enabled": True, "mode": "normal", "explicit": False}
    if preference_row:
        row = dict_from_row(preference_row) or {}
        mode = _normalize_expression_mode(row.get("mode"), bool(int(row.get("enabled", 1) or 0)))
        preference = {
            "enabled": mode != "off",
            "mode": mode,
            "explicit": True,
            "updated_at": int(row.get("updated_at") or 0),
            "source_message_id": row.get("source_message_id"),
        }
    return {
        "preference": preference,
        "summary": summary,
        "insights": insights,
        "recent": recent,
        "counts": sorted_counts,
        "review_items": review_items,
        "counted": len(usage_rows),
    }


def _expression_review_items(counts: list[dict[str, Any]], summary: dict[str, Any]) -> list[dict[str, Any]]:
    window = max(1, int(summary.get("window") or 0))
    items: list[dict[str, Any]] = []
    for item in counts:
        count = int(item.get("count") or 0)
        if count <= 0:
            continue
        share = count / window
        base = {
            "tag": item.get("tag") or "",
            "label": item.get("label") or "",
            "display_text": item.get("display_text") or item.get("label") or item.get("tag") or "",
            "expression_type": item.get("expression_type") or "gesture",
            "count": count,
            "share": round(share, 4),
            "asset_enabled": item.get("asset_enabled", False),
            "risk_level": item.get("risk_level") or "unknown",
            "group": item.get("group") or "unknown",
            "cooldown_turns": int(item.get("cooldown_turns") or 0),
        }
        if item.get("asset_enabled") is False:
            items.append({
                **base,
                "kind": "disabled_asset_history",
                "severity": "watch",
                "text": f"{base['display_text']} 有 {count} 条历史来自当前已禁用资源，普通端已隐藏。",
            })
        if item.get("risk_level") == "medium":
            items.append({
                **base,
                "kind": "medium_risk_tag",
                "severity": "watch",
                "suggested_cooldown_turns": min(20, max(int(base["cooldown_turns"]), 8)),
                "text": f"{base['display_text']} 是中风险表达，最近出现 {count} 次，建议保持较长冷却并继续观察。",
            })
        if count >= 3 and share >= 0.5:
            items.append({
                **base,
                "kind": "concentrated_label",
                "severity": "tune",
                "suggested_cooldown_turns": min(20, int(base["cooldown_turns"]) + 2),
                "text": f"{base['display_text']} 占最近统计窗口 {share:.0%}，可提高冷却或改写用途说明。",
            })
    severity_order = {"tune": 0, "watch": 1}
    return sorted(items, key=lambda item: (severity_order.get(str(item.get("severity")), 9), -int(item.get("count") or 0), str(item.get("tag") or "")))[:8]


@app.post("/api/admin/expression-review/apply-cooldowns")
def admin_apply_expression_review_cooldowns(
    req: ExpressionReviewBulkRequest,
    admin: dict = Depends(current_admin),
):
    owner_id = _admin_target_user_id(admin, req.target_user_id)
    _assert_persona_owner(owner_id, req.persona_id)
    usage = admin_expression_usage(
        admin,
        target_user_id=owner_id,
        persona_id=req.persona_id,
        limit=req.limit,
        usage_limit=req.usage_limit,
    )
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for item in usage.get("review_items") or []:
        suggested = item.get("suggested_cooldown_turns")
        if suggested is None or item.get("asset_enabled") is False:
            continue
        key = (str(item.get("expression_type") or ""), str(item.get("label") or ""))
        if not key[0] or not key[1]:
            continue
        cooldown = max(0, min(int(suggested), 20))
        current = int(item.get("cooldown_turns") or 0)
        if cooldown == current:
            continue
        previous = candidates.get(key)
        if previous is None or cooldown > int(previous["cooldown_turns"]):
            candidates[key] = {
                "expression_type": key[0],
                "label": key[1],
                "cooldown_turns": cooldown,
                "previous_cooldown_turns": current,
                "reason": item.get("kind") or "review_item",
                "text": item.get("text") or "",
            }
    applied = []
    for item in candidates.values():
        asset = update_expression_asset_setting(
            item["expression_type"],
            item["label"],
            enabled=True,
            cooldown_turns=int(item["cooldown_turns"]),
            admin_note=f"批量审查：{item['text']}"[:500],
            updated_by_user_id=int(admin["id"]),
        )
        applied.append({**item, "asset": asset})
    refreshed_usage = admin_expression_usage(
        admin,
        target_user_id=owner_id,
        persona_id=req.persona_id,
        limit=req.limit,
        usage_limit=req.usage_limit,
    )
    return {
        "applied_count": len(applied),
        "applied": applied,
        "expression_usage": refreshed_usage,
        "assets": expression_assets_public(include_disabled=True, include_admin_metadata=True),
    }


@app.post("/api/admin/rag/sync")
def admin_rag_sync(
    admin: dict = Depends(current_admin),
    target_user_id: int | None = None,
    persona_id: int | None = None,
):
    owner_id = _admin_target_user_id(admin, target_user_id)
    _assert_persona_owner(owner_id, persona_id)
    return {"sync": sync_memory_embeddings(owner_id, persona_id)}


@app.get("/api/admin/rag/search")
def admin_rag_search(
    admin: dict = Depends(current_admin),
    target_user_id: int | None = None,
    persona_id: int | None = None,
    q: str = "",
    limit: int = 8,
):
    owner_id = _admin_target_user_id(admin, target_user_id)
    _assert_persona_owner(owner_id, persona_id)
    return {"results": semantic_memory_recall(owner_id, persona_id, q, limit)}


@app.patch("/api/admin/insight")
def admin_update_insight(
    req: InsightUpdateRequest,
    admin: dict = Depends(current_admin),
    target_user_id: int | None = None,
):
    owner_id = _admin_target_user_id(admin, target_user_id)
    return {
        "insight": update_user_insight(
            owner_id,
            profile_summary=req.profile_summary,
            interaction_style=req.interaction_style,
            emotional_patterns=req.emotional_patterns,
            inferred_profile=req.inferred_profile,
            topic_model=req.topic_model,
            guidance=req.guidance,
        )
    }


@app.patch("/api/admin/memory/judgements/{judgement_id}")
def admin_update_judgement(
    judgement_id: int,
    req: JudgementStatusRequest,
    admin: dict = Depends(current_admin),
    target_user_id: int | None = None,
):
    owner_id = _admin_target_user_id(admin, target_user_id)
    try:
        return {"judgement": update_judgement_status(owner_id, judgement_id, req.status)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.patch("/api/admin/memory/conflicts/{conflict_id}")
def admin_update_conflict(
    conflict_id: int,
    req: ConflictStatusRequest,
    admin: dict = Depends(current_admin),
    target_user_id: int | None = None,
):
    owner_id = _admin_target_user_id(admin, target_user_id)
    try:
        return {"conflict": update_conflict_status(owner_id, conflict_id, req.status)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/admin/memory/review")
def admin_review_memory(
    admin: dict = Depends(current_admin),
    target_user_id: int | None = None,
    persona_id: int | None = None,
    include_history: bool = True,
):
    owner_id = _admin_target_user_id(admin, target_user_id)
    _assert_persona_owner(owner_id, persona_id)
    return {"review": memory_review(owner_id, persona_id, include_history)}


@app.post("/api/admin/evaluations/memory/seed")
def admin_seed_memory_eval(admin: dict = Depends(current_admin)):
    return {"seed": seed_memory_eval_data(reset=True)}


@app.post("/api/admin/evaluations/memory/run")
def admin_run_memory_eval(req: MemoryEvalRequest, admin: dict = Depends(current_admin)):
    return {"run": run_memory_evaluation(reset_seed=req.reset_seed, include_semantic=req.include_semantic)}


@app.post("/api/admin/evaluations/chat-context/run")
def admin_run_chat_context_eval(req: MemoryEvalRequest, admin: dict = Depends(current_admin)):
    return {"run": run_chat_context_evaluation(reset_seed=req.reset_seed, include_semantic=req.include_semantic)}


@app.post("/api/admin/evaluations/live-answer/run")
def admin_run_live_answer_eval(req: MemoryEvalRequest, admin: dict = Depends(current_admin)):
    return {"run": run_live_answer_evaluation(reset_seed=req.reset_seed)}


@app.post("/api/admin/evaluations/profile-context/run")
def admin_run_profile_context_eval(req: MemoryEvalRequest, admin: dict = Depends(current_admin)):
    return {"run": run_profile_context_evaluation(reset_seed=req.reset_seed)}


@app.post("/api/admin/evaluations/profile-live-answer/run")
def admin_run_profile_live_answer_eval(req: MemoryEvalRequest, admin: dict = Depends(current_admin)):
    return {"run": run_profile_live_answer_evaluation(reset_seed=req.reset_seed)}


@app.post("/api/admin/evaluations/state-resolution/run")
def admin_run_state_resolution_eval(req: MemoryEvalRequest, admin: dict = Depends(current_admin)):
    return {"run": run_state_resolution_evaluation(reset_seed=req.reset_seed)}


@app.post("/api/admin/evaluations/state-expiry/run")
def admin_run_state_expiry_eval(req: MemoryEvalRequest, admin: dict = Depends(current_admin)):
    return {"run": run_state_expiry_evaluation(reset_seed=req.reset_seed)}


@app.post("/api/admin/evaluations/memory-policy/run")
def admin_run_memory_policy_eval(req: MemoryEvalRequest, admin: dict = Depends(current_admin)):
    return {"run": run_memory_policy_evaluation(reset_seed=req.reset_seed)}


@app.get("/api/admin/evaluations/memory/runs")
def admin_memory_eval_runs(admin: dict = Depends(current_admin), limit: int = 10):
    return {"runs": list_memory_eval_runs(limit)}


@app.post("/api/admin/demos/persona-growth/seed")
def admin_seed_growth_demo(admin: dict = Depends(current_admin)):
    return {"demo": seed_growth_demo_data(reset=True)}


@app.delete("/api/admin/demos/persona-growth")
def admin_clear_growth_demo(admin: dict = Depends(current_admin)):
    return {"demo": clear_growth_demo_data()}


@app.get("/api/admin/llm-calls")
def admin_llm_calls(admin: dict = Depends(current_admin), limit: int = 30, task: str | None = None):
    limit = max(1, min(int(limit), 200))
    params: list[Any] = []
    task_clause = ""
    if task:
        task_clause = "WHERE task = ?"
        params.append(task)
    params.append(limit)
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT id, task, provider, model, status, prompt_chars, response_chars,
                   duration_ms, error_text, created_at
            FROM llm_call_logs
            {task_clause}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return {"calls": [dict_from_row(row) for row in rows]}


@app.get("/api/admin/llm-routes")
def admin_llm_routes(admin: dict = Depends(current_admin)):
    config = load_config()
    base = dict(config.get("llm", {}) or {})
    routes = config.get("llm_routes", {}) or {}
    if not isinstance(routes, dict):
        routes = {}

    safe_routes: dict[str, dict[str, Any]] = {}
    effective: dict[str, dict[str, Any]] = {}
    for task, route in sorted(routes.items()):
        if not isinstance(route, dict):
            continue
        route_config = dict(base)
        route_config.update(route)
        safe_routes[str(task)] = _safe_llm_config(route)
        effective[str(task)] = _safe_llm_config(route_config)

    return {
        "default": _safe_llm_config(base),
        "routes": safe_routes,
        "effective": effective,
    }


def _safe_llm_config(config: dict[str, Any]) -> dict[str, Any]:
    allowed = ("provider", "provider_name", "model", "base_url", "api_key_env", "temperature", "max_tokens", "timeout")
    safe = {key: config.get(key) for key in allowed if config.get(key) not in (None, "")}
    env_name = str(safe.get("api_key_env") or "").strip()
    if env_name:
        safe["api_key_env_present"] = api_key_env_present(env_name)
    return safe


@app.get("/api/admin/memory/items/{uid}")
def admin_memory_item(uid: str, admin: dict = Depends(current_admin), target_user_id: int | None = None):
    try:
        owner_id = _admin_target_user_id(admin, target_user_id)
        return get_memory_item(owner_id, uid)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/api/admin/memory/items/{uid}")
def admin_update_memory(
    uid: str,
    req: MemoryUpdateRequest,
    admin: dict = Depends(current_admin),
    target_user_id: int | None = None,
):
    try:
        owner_id = _admin_target_user_id(admin, target_user_id)
        return update_memory_item(
            user_id=owner_id,
            uid=uid,
            priority=req.priority,
            locked=req.locked,
            archived=req.archived,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/admin/users")
def admin_users(admin: dict = Depends(current_admin)):
    with get_db() as db:
        rows = db.execute(
            """
            SELECT users.id, users.username, users.role, users.status, users.created_at,
                   user_profiles.nickname,
                   (
                       SELECT COUNT(*)
                       FROM persona_revision_suggestions
                       JOIN personas ON personas.id = persona_revision_suggestions.persona_id
                       WHERE persona_revision_suggestions.user_id = users.id
                         AND personas.user_id = users.id
                         AND personas.status = 'active'
                         AND persona_revision_suggestions.status = 'pending'
                         AND persona_revision_suggestions.base_version = personas.version
                   ) AS pending_revision_count,
                   (
                       SELECT COUNT(*)
                       FROM persona_revision_suggestions
                       JOIN personas ON personas.id = persona_revision_suggestions.persona_id
                       WHERE persona_revision_suggestions.user_id = users.id
                         AND personas.user_id = users.id
                         AND personas.status = 'active'
                         AND persona_revision_suggestions.status = 'pending'
                         AND persona_revision_suggestions.base_version = personas.version
                         AND persona_revision_suggestions.origin = 'explicit_feedback'
                   ) AS pending_auto_revision_count,
                   (
                       SELECT COUNT(*)
                       FROM persona_revision_suggestions
                       JOIN personas ON personas.id = persona_revision_suggestions.persona_id
                       WHERE persona_revision_suggestions.user_id = users.id
                         AND personas.user_id = users.id
                         AND personas.status = 'active'
                         AND persona_revision_suggestions.status = 'pending'
                         AND (
                             persona_revision_suggestions.base_version IS NULL
                             OR persona_revision_suggestions.base_version != personas.version
                         )
                   ) AS stale_revision_count,
                   (
                       SELECT COUNT(*)
                       FROM persona_growth_feedback
                       JOIN personas ON personas.id = persona_growth_feedback.persona_id
                       WHERE persona_growth_feedback.user_id = users.id
                         AND personas.user_id = users.id
                         AND personas.status = 'active'
                         AND persona_growth_feedback.reaction = 'needs_adjustment'
                         AND persona_growth_feedback.detail_text <> ''
                         AND persona_growth_feedback.resolved_at = 0
                         AND persona_growth_feedback.reviewed_version = (
                             SELECT COALESCE(MAX(persona_versions.version), 0)
                             FROM persona_versions
                             WHERE persona_versions.persona_id = personas.id
                               AND persona_versions.change_type = 'sculptor_review'
                         )
                   ) AS adjustment_feedback_count,
                   (
                       SELECT COUNT(*)
                       FROM persona_growth_requests
                       JOIN persona_revision_suggestions
                         ON persona_revision_suggestions.id = persona_growth_requests.suggestion_id
                       JOIN personas ON personas.id = persona_growth_requests.persona_id
                       WHERE persona_growth_requests.user_id = users.id
                         AND persona_growth_requests.withdrawn_at = 0
                         AND personas.user_id = users.id
                         AND personas.status = 'active'
                         AND persona_revision_suggestions.status = 'pending'
                         AND persona_revision_suggestions.base_version = personas.version
                   ) AS pending_preference_request_count
            FROM users
            LEFT JOIN user_profiles ON user_profiles.user_id = users.id
            ORDER BY pending_revision_count DESC, pending_preference_request_count DESC,
                     adjustment_feedback_count DESC, stale_revision_count DESC, users.created_at DESC
            LIMIT 200
            """
        ).fetchall()
    return {"users": [dict_from_row(row) for row in rows]}


@app.get("/api/admin/personas")
def admin_personas(admin: dict = Depends(current_admin), target_user_id: int | None = None):
    owner_id = _admin_target_user_id(admin, target_user_id)
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id, user_id, name, summary, relationship, speaking_style,
                   appearance_description, desired_image, psychological_fit_notes,
                   psychological_profile_json, growth_notes, avatar_url,
                   version, status, created_at, updated_at,
                   (
                       SELECT COUNT(*)
                       FROM persona_revision_suggestions
                       WHERE persona_revision_suggestions.user_id = personas.user_id
                         AND persona_revision_suggestions.persona_id = personas.id
                         AND personas.status = 'active'
                         AND persona_revision_suggestions.status = 'pending'
                         AND persona_revision_suggestions.base_version = personas.version
                   ) AS pending_revision_count,
                   (
                       SELECT COUNT(*)
                       FROM persona_revision_suggestions
                       WHERE persona_revision_suggestions.user_id = personas.user_id
                         AND persona_revision_suggestions.persona_id = personas.id
                         AND personas.status = 'active'
                         AND persona_revision_suggestions.status = 'pending'
                         AND persona_revision_suggestions.base_version = personas.version
                         AND persona_revision_suggestions.origin = 'explicit_feedback'
                   ) AS pending_auto_revision_count,
                   (
                       SELECT COUNT(*)
                       FROM persona_revision_suggestions
                       WHERE persona_revision_suggestions.user_id = personas.user_id
                         AND persona_revision_suggestions.persona_id = personas.id
                         AND personas.status = 'active'
                         AND persona_revision_suggestions.status = 'pending'
                         AND (
                             persona_revision_suggestions.base_version IS NULL
                             OR persona_revision_suggestions.base_version != personas.version
                         )
                   ) AS stale_revision_count,
                   (
                       SELECT COUNT(*)
                       FROM persona_revision_suggestions
                       WHERE persona_revision_suggestions.user_id = personas.user_id
                         AND persona_revision_suggestions.persona_id = personas.id
                         AND personas.status = 'active'
                         AND persona_revision_suggestions.status = 'pending'
                         AND (
                             persona_revision_suggestions.base_version IS NULL
                             OR persona_revision_suggestions.base_version != personas.version
                         )
                         AND NOT EXISTS (
                             SELECT 1
                             FROM persona_growth_requests
                             WHERE persona_growth_requests.suggestion_id = persona_revision_suggestions.id
                               AND persona_growth_requests.withdrawn_at = 0
                         )
                   ) AS cleanable_stale_revision_count,
                   (
                       SELECT COUNT(*)
                       FROM persona_growth_feedback
                       WHERE persona_growth_feedback.user_id = personas.user_id
                         AND persona_growth_feedback.persona_id = personas.id
                         AND personas.status = 'active'
                         AND persona_growth_feedback.reaction = 'needs_adjustment'
                         AND persona_growth_feedback.detail_text <> ''
                         AND persona_growth_feedback.resolved_at = 0
                         AND persona_growth_feedback.reviewed_version = (
                             SELECT COALESCE(MAX(persona_versions.version), 0)
                             FROM persona_versions
                             WHERE persona_versions.persona_id = personas.id
                               AND persona_versions.change_type = 'sculptor_review'
                         )
                   ) AS adjustment_feedback_count,
                   (
                       SELECT COUNT(*)
                       FROM persona_growth_requests
                       JOIN persona_revision_suggestions
                         ON persona_revision_suggestions.id = persona_growth_requests.suggestion_id
                       WHERE persona_growth_requests.user_id = personas.user_id
                         AND persona_growth_requests.persona_id = personas.id
                         AND persona_growth_requests.withdrawn_at = 0
                         AND personas.status = 'active'
                         AND persona_revision_suggestions.status = 'pending'
                         AND persona_revision_suggestions.base_version = personas.version
                   ) AS pending_preference_request_count
            FROM personas
            WHERE user_id = ?
            ORDER BY pending_revision_count DESC, pending_preference_request_count DESC,
                     adjustment_feedback_count DESC, stale_revision_count DESC, updated_at DESC
            LIMIT 200
            """,
            (owner_id,),
        ).fetchall()
    return {"personas": [dict_from_row(row) for row in rows]}


@app.get("/api/admin/persona-revisions")
def admin_persona_revisions(
    admin: dict = Depends(current_admin),
    target_user_id: int | None = None,
    persona_id: int | None = None,
    limit: int = 20,
):
    owner_id = _admin_target_user_id(admin, target_user_id)
    _assert_persona_owner(owner_id, persona_id)
    suggestions = list_revision_suggestions(owner_id, persona_id, limit)
    with get_db() as db:
        protected_ids = {
            int(row["suggestion_id"])
            for row in db.execute(
                """
                SELECT persona_growth_requests.suggestion_id
                FROM persona_growth_requests
                JOIN persona_revision_suggestions
                  ON persona_revision_suggestions.id = persona_growth_requests.suggestion_id
                WHERE persona_growth_requests.user_id = ? AND persona_growth_requests.persona_id = ?
                  AND persona_growth_requests.withdrawn_at = 0
                  AND persona_revision_suggestions.status = 'pending'
                """,
                (owner_id, persona_id),
            ).fetchall()
            if row["suggestion_id"] is not None
        }
    for suggestion in suggestions:
        suggestion["protected_by_active_request"] = int(suggestion["id"]) in protected_ids
    return {"suggestions": suggestions}


@app.get("/api/admin/persona-versions")
def admin_persona_versions(
    admin: dict = Depends(current_admin),
    target_user_id: int | None = None,
    persona_id: int | None = None,
    limit: int = 20,
):
    owner_id = _admin_target_user_id(admin, target_user_id)
    if persona_id is None:
        raise HTTPException(status_code=400, detail="persona_id is required")
    _assert_persona_owner(owner_id, persona_id)
    limit = max(1, min(int(limit), 100))
    with get_db() as db:
        rows = db.execute(
            """
            SELECT *
            FROM persona_versions
            WHERE persona_id = ?
            ORDER BY version DESC, id DESC
            LIMIT ?
            """,
            (persona_id, limit),
        ).fetchall()
    return {"versions": [_public_persona(dict_from_row(row)) for row in rows]}


@app.get("/api/admin/persona-growth")
def admin_persona_growth(
    admin: dict = Depends(current_admin),
    target_user_id: int | None = None,
    persona_id: int | None = None,
):
    owner_id = _admin_target_user_id(admin, target_user_id)
    if persona_id is None:
        raise HTTPException(status_code=400, detail="persona_id is required")
    _assert_persona_owner(owner_id, persona_id)
    with get_db() as db:
        persona = dict_from_row(
            db.execute(
                "SELECT * FROM personas WHERE id = ? AND user_id = ?",
                (persona_id, owner_id),
            ).fetchone()
        )
        feedback_facts = db.execute(
            """
            SELECT uid, type, text, importance, confidence, priority, locked, updated_at
            FROM memory_facts
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL)
              AND type IN ('persona_feedback', 'boundary', 'relationship', 'preference')
              AND archived = 0 AND valid_to IS NULL
            ORDER BY priority DESC, importance DESC, updated_at DESC
            LIMIT 30
            """,
            (owner_id, persona_id),
        ).fetchall()
        feedback_relations = db.execute(
            """
            SELECT uid, type, subject, predicate, object, text, importance, confidence, priority, locked, updated_at
            FROM memory_relations
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL)
              AND predicate IN ('persona_feedback', 'boundary', 'relationship_expectation', 'preference')
              AND archived = 0 AND valid_to IS NULL
            ORDER BY priority DESC, importance DESC, updated_at DESC
            LIMIT 30
            """,
            (owner_id, persona_id),
        ).fetchall()
        versions = db.execute(
            """
            SELECT *
            FROM persona_versions
            WHERE persona_id = ?
            ORDER BY version DESC, id DESC
            LIMIT 8
            """,
            (persona_id,),
        ).fetchall()
        user_feedback = db.execute(
            """
            SELECT reviewed_version, reaction, detail_text, resolved_at, resolved_by_user_id,
                   resolution_note, created_at, updated_at
            FROM persona_growth_feedback
            WHERE user_id = ? AND persona_id = ?
            ORDER BY reviewed_version DESC
            LIMIT 8
            """,
            (owner_id, persona_id),
        ).fetchall()
        preference_requests = db.execute(
            """
            SELECT persona_growth_requests.id, persona_growth_requests.request_text,
                   persona_growth_requests.created_at, persona_growth_requests.updated_at,
                   persona_growth_requests.withdrawn_at,
                   persona_growth_requests.request_origin,
                   persona_growth_requests.source_reviewed_version,
                   persona_growth_requests.deactivation_actor,
                   persona_growth_requests.deactivation_reason,
                   persona_revision_suggestions.id AS suggestion_id,
                   persona_revision_suggestions.status AS suggestion_status,
                   persona_revision_suggestions.origin AS suggestion_origin,
                   persona_revision_suggestions.base_version,
                   persona_revision_suggestions.applied_version,
                   persona_revision_suggestions.decided_at,
                   persona_revision_suggestions.decided_by_user_id,
                   persona_revision_suggestions.decision_note
            FROM persona_growth_requests
            LEFT JOIN persona_revision_suggestions
              ON persona_revision_suggestions.id = persona_growth_requests.suggestion_id
            WHERE persona_growth_requests.user_id = ? AND persona_growth_requests.persona_id = ?
            ORDER BY persona_growth_requests.id DESC
            LIMIT 12
            """,
            (owner_id, persona_id),
        ).fetchall()
    return {
        "persona": _public_persona(persona),
        "growth_memories": {
            "facts": [dict_from_row(row) for row in feedback_facts],
            "relations": [dict_from_row(row) for row in feedback_relations],
        },
        "user_feedback": [dict_from_row(row) for row in user_feedback],
        "preference_requests": [dict_from_row(row) for row in preference_requests],
        "versions": [_public_persona(dict_from_row(row)) for row in versions],
    }


@app.post("/api/admin/persona-versions/{version}/restore")
def admin_restore_persona_version(
    version: int,
    req: PersonaVersionRestoreRequest,
    admin: dict = Depends(current_admin),
    target_user_id: int | None = None,
    persona_id: int | None = None,
):
    owner_id = _admin_target_user_id(admin, target_user_id)
    if persona_id is None:
        raise HTTPException(status_code=400, detail="persona_id is required")
    _assert_persona_owner(owner_id, persona_id)
    return restore_persona_version(persona_id, version, req, {"id": owner_id})


@app.post("/api/admin/persona-growth/feedback/resolve")
def admin_resolve_persona_growth_feedback(
    req: PersonaGrowthFeedbackResolutionRequest,
    admin: dict = Depends(current_admin),
    target_user_id: int | None = None,
    persona_id: int | None = None,
):
    owner_id = _admin_target_user_id(admin, target_user_id)
    if persona_id is None:
        raise HTTPException(status_code=400, detail="persona_id is required")
    _assert_persona_owner(owner_id, persona_id)
    ts = now_ts()
    note = scrub_identity_text(req.note.strip())[:1000]
    with get_db() as db:
        cursor = db.execute(
            """
            UPDATE persona_growth_feedback
            SET resolved_at = ?, resolved_by_user_id = ?, resolution_note = ?
            WHERE user_id = ? AND persona_id = ? AND reviewed_version = ?
              AND reaction = 'needs_adjustment' AND resolved_at = 0
            """,
            (ts, int(admin["id"]), note, owner_id, persona_id, req.reviewed_version),
        )
        if not cursor.rowcount:
            raise HTTPException(status_code=404, detail="open adjustment feedback not found")
        feedback = dict_from_row(db.execute(
            """
            SELECT reviewed_version, reaction, detail_text, resolved_at, resolved_by_user_id,
                   resolution_note, created_at, updated_at
            FROM persona_growth_feedback
            WHERE user_id = ? AND persona_id = ? AND reviewed_version = ?
            """,
            (owner_id, persona_id, req.reviewed_version),
        ).fetchone())
    return {"feedback": feedback}


@app.post("/api/admin/persona-revisions")
def admin_generate_persona_revision(
    req: PersonaRevisionRequest,
    admin: dict = Depends(current_admin),
    target_user_id: int | None = None,
    persona_id: int | None = None,
):
    owner_id = _admin_target_user_id(admin, target_user_id)
    if persona_id is None:
        raise HTTPException(status_code=400, detail="persona_id is required")
    _assert_persona_owner(owner_id, persona_id)
    try:
        return {"suggestion": generate_revision_suggestion(owner_id, persona_id, req.reason)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/admin/persona-revisions/auto-review")
def admin_auto_review_persona_revisions(
    admin: dict = Depends(current_admin),
    target_user_id: int | None = None,
    persona_id: int | None = None,
):
    owner_id = _admin_target_user_id(admin, target_user_id)
    if persona_id is None:
        raise HTTPException(status_code=400, detail="persona_id is required")
    _assert_persona_owner(owner_id, persona_id)
    with get_db() as db:
        rows = db.execute(
            """
            SELECT persona_revision_suggestions.id
            FROM persona_revision_suggestions
            JOIN personas ON personas.id = persona_revision_suggestions.persona_id
            WHERE persona_revision_suggestions.user_id = ? AND persona_revision_suggestions.persona_id = ?
              AND personas.user_id = ? AND personas.status = 'active'
              AND persona_revision_suggestions.status = 'pending'
              AND persona_revision_suggestions.origin = 'explicit_feedback'
              AND persona_revision_suggestions.base_version = personas.version
            ORDER BY persona_revision_suggestions.id ASC
            """,
            (owner_id, persona_id, owner_id),
        ).fetchall()
    applied = []
    dismissed = []
    for row in rows:
        decision = maybe_auto_review_revision(owner_id, int(row["id"]))
        if decision and decision.get("status") == "dismissed":
            dismissed.append(decision)
        elif decision:
            applied.append(decision)
    return {
        "attempted_count": len(rows),
        "applied_count": len(applied),
        "dismissed_count": len(dismissed),
        "applied": applied,
        "dismissed": dismissed,
    }


@app.post("/api/admin/persona-revisions/{suggestion_id}/apply")
def admin_apply_persona_revision(
    suggestion_id: int,
    req: PersonaRevisionDecisionRequest | None = None,
    admin: dict = Depends(current_admin),
    target_user_id: int | None = None,
):
    owner_id = _admin_target_user_id(admin, target_user_id)
    try:
        return apply_revision_suggestion(
            owner_id,
            suggestion_id,
            reviewer_user_id=int(admin["id"]),
            decision_note=req.note if req else "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/admin/persona-revisions/stale/dismiss")
def admin_dismiss_stale_persona_revisions(
    admin: dict = Depends(current_admin),
    target_user_id: int | None = None,
    persona_id: int | None = None,
):
    owner_id = _admin_target_user_id(admin, target_user_id)
    if persona_id is None:
        raise HTTPException(status_code=400, detail="persona_id is required")
    _assert_persona_owner(owner_id, persona_id)
    with get_db() as db:
        rows = db.execute(
            """
            SELECT persona_revision_suggestions.id
            FROM persona_revision_suggestions
            JOIN personas ON personas.id = persona_revision_suggestions.persona_id
            WHERE persona_revision_suggestions.user_id = ? AND persona_revision_suggestions.persona_id = ?
              AND personas.user_id = ? AND personas.status = 'active'
              AND persona_revision_suggestions.status = 'pending'
              AND (
                  persona_revision_suggestions.base_version IS NULL
                  OR persona_revision_suggestions.base_version != personas.version
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM persona_growth_requests
                  WHERE persona_growth_requests.suggestion_id = persona_revision_suggestions.id
                    AND persona_growth_requests.withdrawn_at = 0
              )
            ORDER BY persona_revision_suggestions.id ASC
            """,
            (owner_id, persona_id, owner_id),
        ).fetchall()
    dismissed = [
        dismiss_revision_suggestion(
            owner_id,
            int(row["id"]),
            reviewer_user_id=int(admin["id"]),
            decision_note="人格版本已更新，批量关闭不再可执行的过期建议",
        )
        for row in rows
    ]
    return {"dismissed_count": len(dismissed), "suggestions": dismissed}


@app.post("/api/admin/persona-revisions/{suggestion_id}/dismiss")
def admin_dismiss_persona_revision(
    suggestion_id: int,
    req: PersonaRevisionDecisionRequest | None = None,
    admin: dict = Depends(current_admin),
    target_user_id: int | None = None,
):
    owner_id = _admin_target_user_id(admin, target_user_id)
    try:
        return {
            "suggestion": dismiss_revision_suggestion(
                owner_id,
                suggestion_id,
                reviewer_user_id=int(admin["id"]),
                decision_note=req.note if req else "",
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _admin_target_user_id(admin: dict, target_user_id: int | None) -> int:
    owner_id = target_user_id or int(admin["id"])
    with get_db() as db:
        row = db.execute("SELECT id FROM users WHERE id = ?", (owner_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="target user not found")
    return owner_id


def _assert_persona_owner(owner_id: int, persona_id: int | None) -> None:
    if persona_id is None:
        return
    with get_db() as db:
        row = db.execute("SELECT id FROM personas WHERE id = ? AND user_id = ?", (persona_id, owner_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="persona not found for target user")


@app.get("/api/insights")
def insights(user: dict = Depends(current_user)):
    return {"insight": get_user_insight(int(user["id"]))}


@app.get("/api/personas")
def personas(user: dict = Depends(current_user)):
    with get_db() as db:
        rows = db.execute(
            """
            SELECT personas.id, personas.name, personas.summary, personas.relationship,
                   personas.speaking_style, personas.appearance_description, personas.desired_image,
                   personas.psychological_fit_notes, personas.psychological_profile_json,
                   personas.growth_notes, personas.avatar_url, personas.version,
                   personas.created_at, personas.updated_at,
                   (
                       SELECT version
                       FROM persona_versions
                       WHERE persona_versions.persona_id = personas.id
                         AND persona_versions.change_type = 'sculptor_review'
                       ORDER BY version DESC, id DESC
                       LIMIT 1
                   ) AS latest_reviewed_version,
                   (
                       SELECT created_at
                       FROM persona_versions
                       WHERE persona_versions.persona_id = personas.id
                         AND persona_versions.change_type = 'sculptor_review'
                       ORDER BY version DESC, id DESC
                       LIMIT 1
                   ) AS latest_reviewed_at,
                   COALESCE(persona_growth_views.seen_reviewed_version, 0) AS seen_reviewed_version,
                   (
                       SELECT COUNT(*)
                       FROM persona_growth_requests
                       JOIN persona_revision_suggestions
                         ON persona_revision_suggestions.id = persona_growth_requests.suggestion_id
                       WHERE persona_growth_requests.user_id = personas.user_id
                         AND persona_growth_requests.persona_id = personas.id
                         AND persona_growth_requests.withdrawn_at = 0
                         AND persona_revision_suggestions.status = 'pending'
                         AND persona_revision_suggestions.origin = 'profile_request'
                         AND (
                             persona_revision_suggestions.base_version IS NULL
                             OR persona_revision_suggestions.base_version != personas.version
                         )
                   ) AS retry_preference_request_count,
                    (
                        SELECT enabled
                        FROM expression_preferences
                        WHERE expression_preferences.user_id = personas.user_id
                          AND expression_preferences.persona_id = personas.id
                        LIMIT 1
                    ) AS expression_enabled,
                    (
                        SELECT mode
                        FROM expression_preferences
                        WHERE expression_preferences.user_id = personas.user_id
                          AND expression_preferences.persona_id = personas.id
                        LIMIT 1
                    ) AS expression_mode
            FROM personas
            LEFT JOIN persona_growth_views
              ON persona_growth_views.persona_id = personas.id
             AND persona_growth_views.user_id = personas.user_id
            WHERE personas.user_id = ? AND personas.status = 'active'
            ORDER BY personas.updated_at DESC
            """,
            (user["id"],),
        ).fetchall()
    return {"personas": [_persona_list_item(dict_from_row(row) or {}) for row in rows]}


@app.get("/api/personas/deleted")
def deleted_personas(user: dict = Depends(current_user)):
    with get_db() as db:
        rows = db.execute(
            """
            SELECT id, name, summary, relationship, speaking_style,
                   appearance_description, desired_image, psychological_fit_notes,
                   psychological_profile_json, growth_notes, avatar_url,
                   version, created_at, updated_at
            FROM personas
            WHERE user_id = ? AND status = 'deleted'
            ORDER BY updated_at DESC
            """,
            (user["id"],),
        ).fetchall()
    return {"personas": [dict_from_row(row) for row in rows]}


@app.get("/api/personas/{persona_id}")
def persona_detail(persona_id: int, user: dict = Depends(current_user)):
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM personas WHERE id = ? AND user_id = ? AND status = 'active'",
            (persona_id, user["id"]),
        ).fetchone()
    persona = _public_persona(dict_from_row(row))
    if not persona:
        raise HTTPException(status_code=404, detail="persona not found")
    persona["expression_preference"] = _expression_preference_public(int(user["id"]), persona_id)
    return {"persona": persona}


@app.get("/api/personas/{persona_id}/growth")
def persona_growth(persona_id: int, user: dict = Depends(current_user)):
    user_id = int(user["id"])
    with get_db() as db:
        persona = db.execute(
            "SELECT id, version FROM personas WHERE id = ? AND user_id = ? AND status = 'active'",
            (persona_id, user_id),
        ).fetchone()
        if not persona:
            raise HTTPException(status_code=404, detail="persona not found")
        fact_count = int(
            db.execute(
                """
                SELECT COUNT(*)
                FROM memory_facts
                WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL)
                  AND type IN ('persona_feedback', 'boundary', 'relationship', 'preference')
                  AND archived = 0 AND valid_to IS NULL
                """,
                (user_id, persona_id),
            ).fetchone()[0]
        )
        relation_count = int(
            db.execute(
                """
                SELECT COUNT(*)
                FROM memory_relations
                WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL)
                  AND predicate IN ('persona_feedback', 'boundary', 'relationship_expectation', 'preference')
                  AND archived = 0 AND valid_to IS NULL
                """,
                (user_id, persona_id),
            ).fetchone()[0]
        )
        state_count = int(
            db.execute(
                """
                SELECT COUNT(*)
                FROM memory_state
                WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL)
                """,
                (user_id, persona_id),
            ).fetchone()[0]
        )
        reviewed_versions = [
            dict_from_row(row)
            for row in db.execute(
            """
            SELECT *
            FROM persona_versions
            WHERE persona_id = ? AND change_type = 'sculptor_review'
            ORDER BY version DESC, id DESC
            LIMIT 5
            """,
            (persona_id,),
        ).fetchall()
        ]
        reviewed_version = reviewed_versions[0] if reviewed_versions else None
        reviewed_history: list[dict[str, Any]] = []
        for reviewed_item in reviewed_versions:
            previous_item = dict_from_row(db.execute(
                """
                SELECT *
                FROM persona_versions
                WHERE persona_id = ? AND version < ?
                ORDER BY version DESC, id DESC
                LIMIT 1
                """,
                (persona_id, int(reviewed_item["version"])),
            ).fetchone())
            feedback_item = dict_from_row(db.execute(
                """
                SELECT reviewed_version, reaction, detail_text, resolved_at, created_at, updated_at
                FROM persona_growth_feedback
                WHERE user_id = ? AND persona_id = ? AND reviewed_version = ?
                """,
                (user_id, persona_id, int(reviewed_item["version"])),
            ).fetchone())
            reviewed_history.append(
                {
                    "reviewed_version": reviewed_item,
                    "previous_version": previous_item,
                    "feedback": feedback_item,
                }
            )
        growth_view = db.execute(
            """
            SELECT seen_reviewed_version
            FROM persona_growth_views
            WHERE user_id = ? AND persona_id = ?
            """,
            (user_id, persona_id),
        ).fetchone()
        previous_version = reviewed_history[0]["previous_version"] if reviewed_history else None
        reviewed_feedback = (
            _public_growth_feedback(reviewed_history[0]["feedback"], include_detail=True)
            if reviewed_history
            else None
        )
        request_rows = db.execute(
            """
            SELECT persona_growth_requests.id, persona_growth_requests.request_text,
                   persona_growth_requests.created_at, persona_growth_requests.updated_at,
                   persona_growth_requests.withdrawn_at,
                   persona_growth_requests.request_origin,
                   persona_growth_requests.source_reviewed_version,
                   persona_growth_requests.deactivation_actor,
                   persona_growth_requests.deactivation_reason,
                   persona_growth_requests.suggestion_id,
                   persona_revision_suggestions.status AS suggestion_status,
                   persona_revision_suggestions.origin AS suggestion_origin,
                   persona_revision_suggestions.base_version,
                   persona_revision_suggestions.applied_version
            FROM persona_growth_requests
            LEFT JOIN persona_revision_suggestions
              ON persona_revision_suggestions.id = persona_growth_requests.suggestion_id
            WHERE persona_growth_requests.user_id = ? AND persona_growth_requests.persona_id = ?
            ORDER BY persona_growth_requests.id DESC
            LIMIT 5
            """,
            (user_id, persona_id),
        ).fetchall()
        request_history: list[dict[str, Any]] = []
        for request_row in request_rows:
            request_item = dict_from_row(request_row) or {}
            public_result = None
            if request_item.get("suggestion_status") == "applied" and request_item.get("applied_version"):
                applied_review = dict_from_row(db.execute(
                    """
                    SELECT *
                    FROM persona_versions
                    WHERE persona_id = ? AND version = ? AND source_suggestion_id = ?
                      AND change_type = 'sculptor_review'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (persona_id, int(request_item["applied_version"]), int(request_item["suggestion_id"])),
                ).fetchone())
                if applied_review:
                    before_review = dict_from_row(db.execute(
                        """
                        SELECT *
                        FROM persona_versions
                        WHERE persona_id = ? AND version < ?
                        ORDER BY version DESC, id DESC
                        LIMIT 1
                        """,
                        (persona_id, int(applied_review["version"])),
                    ).fetchone())
                    public_result = {
                        "version": int(applied_review["version"]),
                        "created_at": int(applied_review["created_at"]),
                        "highlights": _public_review_highlights(before_review, applied_review),
                    }
            request_item["public_result"] = public_result
            request_history.append(request_item)

    remembered_count = fact_count + relation_count
    seen_reviewed_version = int(growth_view["seen_reviewed_version"]) if growth_view else 0
    reviewed_changes = [
        {
            "version": int(item["reviewed_version"]["version"]),
            "previous_version": int(item["previous_version"]["version"]) if item.get("previous_version") else None,
            "created_at": int(item["reviewed_version"]["created_at"]),
            "highlights": _public_review_highlights(item["previous_version"], item["reviewed_version"]),
            "feedback": _public_growth_feedback(item["feedback"]),
        }
        for item in reviewed_history
    ]
    preference_requests = []
    for item in request_history:
        suggestion_status = str(item.get("suggestion_status") or "")
        public_status = "recorded"
        if int(item.get("withdrawn_at") or 0):
            if item.get("deactivation_actor") == "adaptive_runtime":
                public_status = "superseded"
            elif item.get("deactivation_actor") == "chat_runtime":
                public_status = "stopped_in_chat"
            else:
                public_status = "withdrawn"
        else:
            public_status = "active_guidance"
        preference_requests.append(
            {
                "id": int(item["id"]),
                "detail": item["request_text"],
                "created_at": int(item["created_at"]),
                "updated_at": int(item.get("updated_at") or item["created_at"]),
                "status": public_status,
                "origin": item.get("request_origin") or "direct_entry",
                "source_reviewed_version": item.get("source_reviewed_version"),
                "deactivation_reason": item.get("deactivation_reason") or "",
                "applied_version": item.get("applied_version"),
                "result": item.get("public_result"),
                "can_withdraw": not int(item.get("withdrawn_at") or 0),
                "can_retry": False,
            }
        )
    signals: list[dict[str, Any]] = []
    if remembered_count:
        signals.append(
            {
                "kind": "memory",
                "title": "记住了相处中的重点",
                "text": f"已经留下 {remembered_count} 条与你们相处有关的记忆线索。",
            }
        )
    if state_count:
        signals.append(
            {
                "kind": "attention",
                "title": "正在留意你的节奏",
                "text": f"有 {state_count} 项当前状态或边界正在帮助 TA 回应你。",
            }
        )
    latest_reviewed_change = None
    if reviewed_version:
        highlights = _public_review_highlights(previous_version, reviewed_version)
        latest_reviewed_change = {
            "version": int(reviewed_version["version"]),
            "created_at": int(reviewed_version["created_at"]),
            "unseen": int(reviewed_version["version"]) > seen_reviewed_version,
            "highlights": highlights,
            "feedback": reviewed_feedback,
        }
        signals.append(
            {
                "kind": "adaptation",
                "title": "最近确认的变化",
                "text": f"经确认后，{'；'.join(highlights)}。",
                "created_at": int(reviewed_version["created_at"]),
            }
        )
    if signals:
        headline = "你们的相处正在留下痕迹"
    else:
        headline = "你们的相处还在慢慢形成"
        signals.append(
            {
                "kind": "starting",
                "title": "从自然聊天开始",
                "text": "重要的偏好和相处方式会在之后逐渐被记住。",
            }
        )
    return {
        "growth": {
            "headline": headline,
            "signals": signals,
            "version": int(persona["version"]),
            "latest_reviewed_change": latest_reviewed_change,
            "reviewed_changes": reviewed_changes,
            "preference_requests": preference_requests,
        }
    }


@app.post("/api/personas/{persona_id}/growth/requests")
def submit_persona_preference_request(
    persona_id: int,
    req: PersonaPreferenceRequest,
    user: dict = Depends(current_user),
):
    detail_text = scrub_identity_text(req.detail.strip())[:500]
    if not detail_text:
        raise HTTPException(status_code=400, detail="请写下希望调整的相处方式")
    return _store_persona_preference_guidance(persona_id, detail_text, user)


def _store_persona_preference_guidance(
    persona_id: int,
    detail_text: str,
    user: dict,
    *,
    request_origin: str = "direct_entry",
    source_reviewed_version: int | None = None,
):
    user_id = int(user["id"])
    with get_db() as db:
        persona = db.execute(
            "SELECT id, version FROM personas WHERE id = ? AND user_id = ? AND status = 'active'",
            (persona_id, user_id),
        ).fetchone()
        if not persona:
            raise HTTPException(status_code=404, detail="persona not found")
        existing_request = dict_from_row(db.execute(
            """
            SELECT persona_growth_requests.id, persona_growth_requests.created_at,
                   persona_growth_requests.memory_uids_json,
                   persona_revision_suggestions.id AS suggestion_id,
                   persona_revision_suggestions.status AS suggestion_status,
                   persona_revision_suggestions.trigger_memory_uids_json
            FROM persona_growth_requests
            LEFT JOIN persona_revision_suggestions
              ON persona_revision_suggestions.id = persona_growth_requests.suggestion_id
            WHERE persona_growth_requests.user_id = ? AND persona_growth_requests.persona_id = ?
              AND persona_growth_requests.withdrawn_at = 0
              AND persona_growth_requests.request_origin = ?
              AND (
                  (? IS NULL AND persona_growth_requests.source_reviewed_version IS NULL)
                  OR persona_growth_requests.source_reviewed_version = ?
              )
            ORDER BY persona_growth_requests.id DESC
            LIMIT 1
            """,
            (user_id, persona_id, request_origin, source_reviewed_version, source_reviewed_version),
        ).fetchone())
    request_memory_text = (
        f"用户对已确认变化提出的补充偏好：{detail_text}"
        if request_origin == "growth_feedback"
        else f"用户主动提出的相处偏好：{detail_text}"
    )
    memory_uids: list[str] = []
    if existing_request:
        try:
            memory_uids = [
                str(uid) for uid in json.loads(
                    existing_request.get("memory_uids_json")
                    or existing_request.get("trigger_memory_uids_json")
                    or "[]"
                )
                if uid
            ]
        except Exception:
            memory_uids = []
        ts = now_ts()
        with get_db() as db:
            for uid in memory_uids:
                db.execute(
                    """
                    UPDATE memory_facts
                    SET text = ?, importance = 0.92, confidence = 1.0,
                        priority = 'high', locked = 1, updated_at = ?
                    WHERE uid = ? AND user_id = ? AND persona_id = ? AND type = 'persona_feedback'
                    """,
                    (request_memory_text, ts, uid, user_id, persona_id),
                )
                db.execute(
                    """
                    UPDATE memory_relations
                    SET text = ?, object = ?, importance = 0.92, confidence = 1.0,
                        priority = 'high', locked = 1, updated_at = ?
                    WHERE uid = ? AND user_id = ? AND persona_id = ? AND type = 'persona_feedback'
                    """,
                        (request_memory_text, request_memory_text[:120], ts, uid, user_id, persona_id),
                )
        if existing_request.get("suggestion_status") == "pending" and existing_request.get("suggestion_id"):
            dismiss_revision_suggestion(
                user_id,
                int(existing_request["suggestion_id"]),
                decision_actor="adaptive_runtime",
                decision_note="已切换为运行时自动适配：用户相处偏好直接进入聊天指导，不再等待人工审核。",
            )
    if not memory_uids:
        stored = store_layered_memories(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=None,
            source_message_id=None,
            event_uid=None,
            episode_uid=None,
            memories=[
                {
                    "type": "persona_feedback",
                    "text": request_memory_text,
                    "importance": 0.92,
                    "confidence": 1.0,
                }
            ],
        )
        memory_uids = [
            str(item["uid"])
            for item in stored
            if item.get("uid") and item.get("layer") in {"L2", "L3"}
        ]
    ts = now_ts()
    with get_db() as db:
        if existing_request:
            request_id = int(existing_request["id"])
            created_at = int(existing_request["created_at"])
            db.execute(
                "UPDATE persona_growth_requests SET request_text = ?, memory_uids_json = ?, updated_at = ? WHERE id = ?",
                (detail_text, json.dumps(memory_uids, ensure_ascii=False), ts, request_id),
            )
            updated = True
        else:
            cursor = db.execute(
                """
                INSERT INTO persona_growth_requests (
                    user_id, persona_id, request_text, suggestion_id, memory_uids_json,
                    request_origin, source_reviewed_version, created_at, updated_at
                )
                VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    user_id, persona_id, detail_text, json.dumps(memory_uids, ensure_ascii=False),
                    request_origin, source_reviewed_version, ts, ts,
                ),
            )
            request_id = int(cursor.lastrowid)
            created_at = ts
            updated = False
    superseded_request_ids = supersede_conflicting_guidance(
        user_id,
        persona_id,
        detail_text,
        exclude_request_id=request_id,
    )
    refresh_memory_state(user_id, persona_id)
    refresh_memory_summaries(user_id, persona_id)
    return {
        "request": {
            "id": request_id,
            "detail": detail_text,
            "created_at": created_at,
            "updated_at": ts,
            "status": "active_guidance",
            "origin": request_origin,
            "source_reviewed_version": source_reviewed_version,
            "applied_version": None,
        },
        "updated": updated,
        "superseded_request_ids": superseded_request_ids,
    }


@app.post("/api/personas/{persona_id}/growth/requests/{request_id}/retry")
def retry_persona_preference_request(
    persona_id: int,
    request_id: int,
    user: dict = Depends(current_user),
):
    user_id = int(user["id"])
    with get_db() as db:
        row = dict_from_row(db.execute(
            """
            SELECT persona_growth_requests.*, persona_revision_suggestions.status AS suggestion_status,
                   persona_revision_suggestions.origin AS suggestion_origin,
                   persona_revision_suggestions.base_version,
                   persona_revision_suggestions.trigger_memory_uids_json,
                   personas.version AS persona_version
            FROM persona_growth_requests
            LEFT JOIN persona_revision_suggestions
              ON persona_revision_suggestions.id = persona_growth_requests.suggestion_id
            JOIN personas
              ON personas.id = persona_growth_requests.persona_id
             AND personas.user_id = persona_growth_requests.user_id
             AND personas.status = 'active'
            WHERE persona_growth_requests.id = ? AND persona_growth_requests.user_id = ?
              AND persona_growth_requests.persona_id = ?
            """,
            (request_id, user_id, persona_id),
        ).fetchone())
    if not row:
        raise HTTPException(status_code=404, detail="request not found")
    if int(row.get("withdrawn_at") or 0):
        raise HTTPException(status_code=400, detail="已撤回的偏好不能重新提交")
    return submit_persona_preference_request(
        persona_id,
        PersonaPreferenceRequest(detail=str(row["request_text"] or "")),
        user,
    )


@app.post("/api/personas/{persona_id}/growth/requests/{request_id}/withdraw")
def withdraw_persona_preference_request(
    persona_id: int,
    request_id: int,
    user: dict = Depends(current_user),
):
    user_id = int(user["id"])
    with get_db() as db:
        row = dict_from_row(db.execute(
            """
            SELECT persona_growth_requests.*, persona_revision_suggestions.status AS suggestion_status,
                   persona_revision_suggestions.origin AS suggestion_origin,
                   persona_growth_requests.memory_uids_json,
                   persona_revision_suggestions.trigger_memory_uids_json
            FROM persona_growth_requests
            LEFT JOIN persona_revision_suggestions
              ON persona_revision_suggestions.id = persona_growth_requests.suggestion_id
            WHERE persona_growth_requests.id = ? AND persona_growth_requests.user_id = ?
              AND persona_growth_requests.persona_id = ?
            """,
            (request_id, user_id, persona_id),
        ).fetchone())
    if not row:
        raise HTTPException(status_code=404, detail="request not found")
    if int(row.get("withdrawn_at") or 0):
        return {"request": {"id": request_id, "status": "withdrawn"}}
    if row.get("suggestion_status") == "pending" and row.get("suggestion_id"):
        dismiss_revision_suggestion(
            user_id,
            int(row["suggestion_id"]),
            decision_actor="user",
            decision_note="用户停止了这条自动相处指导",
        )
    ts = now_ts()
    if not row.get("memory_uids_json") and row.get("trigger_memory_uids_json"):
        row["memory_uids_json"] = row["trigger_memory_uids_json"]
    deactivate_guidance(
        user_id,
        persona_id,
        row,
        actor="user",
        reason="用户停止了这条相处指导",
        ts=ts,
    )
    refresh_memory_state(user_id, persona_id)
    refresh_memory_summaries(user_id, persona_id)
    return {"request": {"id": request_id, "status": "withdrawn", "updated_at": ts}}


@app.post("/api/personas/{persona_id}/growth/feedback")
def set_persona_growth_feedback(
    persona_id: int,
    req: PersonaGrowthFeedbackRequest,
    user: dict = Depends(current_user),
):
    reaction = req.reaction.strip()
    if reaction not in {"helpful", "needs_adjustment"}:
        raise HTTPException(status_code=400, detail="invalid growth feedback reaction")
    detail_text = scrub_identity_text(req.detail.strip())[:500] if reaction == "needs_adjustment" else ""
    user_id = int(user["id"])
    with get_db() as db:
        persona = db.execute(
            "SELECT id FROM personas WHERE id = ? AND user_id = ? AND status = 'active'",
            (persona_id, user_id),
        ).fetchone()
        if not persona:
            raise HTTPException(status_code=404, detail="persona not found")
        row = db.execute(
            """
            SELECT COALESCE(MAX(version), 0) AS version
            FROM persona_versions
            WHERE persona_id = ? AND change_type = 'sculptor_review'
            """,
            (persona_id,),
        ).fetchone()
        reviewed_version = int(row["version"] or 0)
        if not reviewed_version:
            raise HTTPException(status_code=400, detail="no confirmed growth change to review")
        ts = now_ts()
        db.execute(
            """
            INSERT INTO persona_growth_feedback (
                user_id, persona_id, reviewed_version, reaction, detail_text, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, persona_id, reviewed_version) DO UPDATE SET
                reaction = excluded.reaction,
                detail_text = excluded.detail_text,
                resolved_at = 0,
                resolved_by_user_id = NULL,
                resolution_note = '',
                updated_at = excluded.updated_at
            """,
            (user_id, persona_id, reviewed_version, reaction, detail_text, ts, ts),
        )
        feedback = dict_from_row(db.execute(
            """
            SELECT reviewed_version, reaction, detail_text, resolved_at, created_at, updated_at
            FROM persona_growth_feedback
            WHERE user_id = ? AND persona_id = ? AND reviewed_version = ?
            """,
            (user_id, persona_id, reviewed_version),
        ).fetchone())
    if reaction == "needs_adjustment" and detail_text:
        _store_persona_preference_guidance(
            persona_id,
            detail_text,
            user,
            request_origin="growth_feedback",
            source_reviewed_version=reviewed_version,
        )
        ts = now_ts()
        with get_db() as db:
            db.execute(
                """
                UPDATE persona_growth_feedback
                SET resolved_at = ?, resolved_by_user_id = NULL,
                    resolution_note = '已自动加入当前回应指导', updated_at = ?
                WHERE user_id = ? AND persona_id = ? AND reviewed_version = ?
                """,
                (ts, ts, user_id, persona_id, reviewed_version),
            )
            feedback = dict_from_row(db.execute(
                """
                SELECT reviewed_version, reaction, detail_text, resolved_at, created_at, updated_at
                FROM persona_growth_feedback
                WHERE user_id = ? AND persona_id = ? AND reviewed_version = ?
                """,
                (user_id, persona_id, reviewed_version),
            ).fetchone())
    return {"feedback": _public_growth_feedback(feedback, include_detail=True, include_version=True)}


@app.post("/api/personas/{persona_id}/growth/viewed")
def mark_persona_growth_viewed(persona_id: int, user: dict = Depends(current_user)):
    user_id = int(user["id"])
    with get_db() as db:
        persona = db.execute(
            "SELECT id FROM personas WHERE id = ? AND user_id = ? AND status = 'active'",
            (persona_id, user_id),
        ).fetchone()
        if not persona:
            raise HTTPException(status_code=404, detail="persona not found")
        row = db.execute(
            """
            SELECT COALESCE(MAX(version), 0) AS version
            FROM persona_versions
            WHERE persona_id = ? AND change_type = 'sculptor_review'
            """,
            (persona_id,),
        ).fetchone()
        reviewed_version = int(row["version"] or 0)
        ts = now_ts()
        db.execute(
            """
            INSERT INTO persona_growth_views (user_id, persona_id, seen_reviewed_version, viewed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, persona_id) DO UPDATE SET
                seen_reviewed_version = MAX(persona_growth_views.seen_reviewed_version, excluded.seen_reviewed_version),
                viewed_at = excluded.viewed_at
            """,
            (user_id, persona_id, reviewed_version, ts),
        )
    return {"ok": True, "persona_id": persona_id, "seen_reviewed_version": reviewed_version}


@app.patch("/api/personas/{persona_id}")
def update_persona(persona_id: int, req: PersonaUpdateRequest, user: dict = Depends(current_user)):
    user_id = int(user["id"])
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM personas WHERE id = ? AND user_id = ? AND status = 'active'",
            (persona_id, user_id),
        ).fetchone()
    current = _public_persona(dict_from_row(row))
    if not current:
        raise HTTPException(status_code=404, detail="persona not found")

    name = scrub_identity_text(req.name.strip()) if req.name is not None else current.get("name", "")
    if req.name is not None and not name:
        raise HTTPException(status_code=400, detail="persona name cannot be empty")
    summary = scrub_identity_text(req.summary.strip()) if req.summary is not None else current.get("summary", "")
    relationship = scrub_identity_text(req.relationship.strip()) if req.relationship is not None else current.get("relationship", "")
    speaking_style = scrub_identity_text(req.speaking_style.strip()) if req.speaking_style is not None else current.get("speaking_style", "")
    appearance = scrub_identity_text(req.appearance_description) if req.appearance_description is not None else current.get("appearance_description", "")
    desired = scrub_identity_text(req.desired_image) if req.desired_image is not None else current.get("desired_image", "")
    avatar_url = req.avatar_url if req.avatar_url is not None else current.get("avatar_url")
    content_changed = (
        name != current.get("name", "")
        or summary != current.get("summary", "")
        or relationship != current.get("relationship", "")
        or speaking_style != current.get("speaking_style", "")
        or appearance != current.get("appearance_description", "")
        or desired != current.get("desired_image", "")
    )
    ts = now_ts()
    next_version = int(current.get("version", 1) or 1) + 1 if content_changed else int(current.get("version", 1) or 1)

    updated = dict(current)
    updated["name"] = name
    updated["summary"] = summary
    updated["relationship"] = relationship
    updated["speaking_style"] = speaking_style
    updated["appearance_description"] = appearance or ""
    updated["desired_image"] = desired or ""
    updated["avatar_url"] = avatar_url
    if content_changed:
        updated["prompt"] = build_prompt(updated)

    with get_db() as db:
        db.execute(
            """
            UPDATE personas
            SET name = ?, summary = ?, relationship = ?, speaking_style = ?,
                avatar_url = ?, appearance_description = ?, desired_image = ?,
                prompt = ?, version = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                updated["name"],
                updated["summary"],
                updated["relationship"],
                updated["speaking_style"],
                avatar_url,
                updated["appearance_description"],
                updated["desired_image"],
                updated.get("prompt") or current.get("prompt") or "",
                next_version,
                ts,
                persona_id,
                user_id,
            ),
        )
        if content_changed:
            db.execute(
                """
                INSERT INTO persona_versions (
                    persona_id, version, name, summary, prompt, traits_json,
                    relationship, speaking_style, boundaries_json,
                    psychological_profile_json, psychological_fit_notes,
                    appearance_description, desired_image, growth_notes,
                    reason, change_type, change_notes_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    persona_id,
                    next_version,
                    updated["name"],
                    updated["summary"],
                    updated["prompt"],
                    json.dumps(updated.get("traits", []), ensure_ascii=False),
                    updated.get("relationship", ""),
                    updated.get("speaking_style", ""),
                    json.dumps(updated.get("boundaries", []), ensure_ascii=False),
                    json.dumps(updated.get("psychological_profile", {}), ensure_ascii=False),
                    updated.get("psychological_fit_notes", ""),
                    updated.get("appearance_description", ""),
                    updated.get("desired_image", ""),
                    updated.get("growth_notes", ""),
                    "persona profile update",
                    "user_profile_update",
                    json.dumps(["用户编辑了联系人资料。"], ensure_ascii=False),
                    ts,
                ),
            )
        persona = dict_from_row(db.execute("SELECT * FROM personas WHERE id = ?", (persona_id,)).fetchone())
    public_persona = _public_persona(persona)
    public_persona["expression_preference"] = _expression_preference_public(user_id, persona_id)
    return {"persona": public_persona}


@app.post("/api/personas/{persona_id}/versions/{version}/restore")
def restore_persona_version(
    persona_id: int,
    version: int,
    req: PersonaVersionRestoreRequest,
    user: dict = Depends(current_user),
):
    user_id = int(user["id"])
    if version < 1:
        raise HTTPException(status_code=400, detail="invalid persona version")
    note = scrub_identity_text((req.note or "").strip())[:500]
    ts = now_ts()
    with get_db() as db:
        current = dict_from_row(
            db.execute(
                "SELECT * FROM personas WHERE id = ? AND user_id = ? AND status = 'active'",
                (persona_id, user_id),
            ).fetchone()
        )
        if not current:
            raise HTTPException(status_code=404, detail="persona not found")
        target = dict_from_row(
            db.execute(
                """
                SELECT *
                FROM persona_versions
                WHERE persona_id = ? AND version = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (persona_id, version),
            ).fetchone()
        )
        if not target:
            raise HTTPException(status_code=404, detail="persona version not found")
        current_version = int(current.get("version", 1) or 1)
        if int(target.get("version", 0) or 0) == current_version:
            raise HTTPException(status_code=400, detail="persona is already at that version")
        restored = _public_persona(target)
        restored["id"] = persona_id
        restored["user_id"] = user_id
        restored["avatar_url"] = current.get("avatar_url")
        restored["prompt"] = build_prompt(restored)
        next_version = current_version + 1
        db.execute(
            """
            UPDATE personas
            SET name = ?, summary = ?, prompt = ?, traits_json = ?, relationship = ?,
                speaking_style = ?, boundaries_json = ?, psychological_profile_json = ?,
                psychological_fit_notes = ?, appearance_description = ?, desired_image = ?,
                growth_notes = ?, version = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                restored.get("name", ""),
                restored.get("summary", ""),
                restored.get("prompt", ""),
                json.dumps(restored.get("traits", []), ensure_ascii=False),
                restored.get("relationship", ""),
                restored.get("speaking_style", ""),
                json.dumps(restored.get("boundaries", []), ensure_ascii=False),
                json.dumps(restored.get("psychological_profile", {}), ensure_ascii=False),
                restored.get("psychological_fit_notes", ""),
                restored.get("appearance_description", ""),
                restored.get("desired_image", ""),
                restored.get("growth_notes", ""),
                next_version,
                ts,
                persona_id,
                user_id,
            ),
        )
        change_note = f"恢复到 v{version}"
        if note:
            change_note = f"{change_note}：{note}"
        db.execute(
            """
            INSERT INTO persona_versions (
                persona_id, version, name, summary, prompt, traits_json,
                relationship, speaking_style, boundaries_json,
                psychological_profile_json, psychological_fit_notes,
                appearance_description, desired_image, growth_notes,
                reason, change_type, change_notes_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                persona_id,
                next_version,
                restored.get("name", ""),
                restored.get("summary", ""),
                restored.get("prompt", ""),
                json.dumps(restored.get("traits", []), ensure_ascii=False),
                restored.get("relationship", ""),
                restored.get("speaking_style", ""),
                json.dumps(restored.get("boundaries", []), ensure_ascii=False),
                json.dumps(restored.get("psychological_profile", {}), ensure_ascii=False),
                restored.get("psychological_fit_notes", ""),
                restored.get("appearance_description", ""),
                restored.get("desired_image", ""),
                restored.get("growth_notes", ""),
                "persona version restore",
                "user_version_restore",
                json.dumps([change_note], ensure_ascii=False),
                ts,
            ),
        )
        persona = _public_persona(dict_from_row(db.execute("SELECT * FROM personas WHERE id = ?", (persona_id,)).fetchone()))
    persona["expression_preference"] = _expression_preference_public(user_id, persona_id)
    return {"persona": persona, "restored_from_version": version, "version": next_version}


@app.patch("/api/personas/{persona_id}/expression-preference")
def update_persona_expression_preference(
    persona_id: int,
    req: ExpressionPreferenceUpdateRequest,
    user: dict = Depends(current_user),
):
    user_id = int(user["id"])
    with get_db() as db:
        row = db.execute(
            "SELECT id FROM personas WHERE id = ? AND user_id = ? AND status = 'active'",
            (persona_id, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="persona not found")
        ts = now_ts()
        mode = _normalize_expression_mode(req.mode, req.enabled)
        enabled = 0 if mode == "off" else 1
        db.execute(
            """
            INSERT INTO expression_preferences (user_id, persona_id, enabled, mode, source_message_id, updated_at)
            VALUES (?, ?, ?, ?, NULL, ?)
            ON CONFLICT(user_id, persona_id) DO UPDATE SET
                enabled = excluded.enabled,
                mode = excluded.mode,
                source_message_id = NULL,
                updated_at = excluded.updated_at
            """,
            (user_id, persona_id, enabled, mode, ts),
        )
    return {
        "persona_id": persona_id,
        "expression_preference": _expression_preference_public(user_id, persona_id),
    }


@app.post("/api/personas/{persona_id}/avatar/generate")
def generate_persona_avatar_placeholder(
    persona_id: int,
    req: PersonaAvatarGenerateRequest,
    user: dict = Depends(current_user),
):
    user_id = int(user["id"])
    desired_image = scrub_identity_text(req.desired_image or "") if req.desired_image is not None else None
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM personas WHERE id = ? AND user_id = ? AND status = 'active'",
            (persona_id, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="persona not found")
        persona = _public_persona(dict_from_row(row))
        if desired_image is not None:
            persona["desired_image"] = desired_image
            db.execute(
                """
                UPDATE personas
                SET desired_image = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (desired_image, now_ts(), persona_id, user_id),
            )
        avatar_url = _generate_local_persona_avatar(user_id, persona)
        db.execute(
            """
            UPDATE personas
            SET avatar_url = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (avatar_url, now_ts(), persona_id, user_id),
        )
        persona = _public_persona(dict_from_row(db.execute("SELECT * FROM personas WHERE id = ?", (persona_id,)).fetchone()))
    persona["expression_preference"] = _expression_preference_public(user_id, persona_id)
    return {
        "ok": True,
        "status": "generated",
        "url": avatar_url,
        "persona": persona,
    }


@app.post("/api/personas/{persona_id}/delete")
def delete_persona(persona_id: int, req: PersonaDeleteRequest, user: dict = Depends(current_user)):
    user_id = int(user["id"])
    with get_db() as db:
        row = db.execute(
            "SELECT id, name FROM personas WHERE id = ? AND user_id = ? AND status = 'active'",
            (persona_id, user_id),
        ).fetchone()
        persona = dict_from_row(row)
        if not persona:
            raise HTTPException(status_code=404, detail="persona not found")
        if req.confirm_name.strip() != str(persona["name"]).strip():
            raise HTTPException(status_code=400, detail="请输入完整的人格名字以确认删除")
        db.execute(
            """
            UPDATE personas
            SET status = 'deleted', updated_at = ?
            WHERE id = ? AND user_id = ? AND status = 'active'
            """,
            (now_ts(), persona_id, user_id),
        )
    return {"ok": True, "persona_id": persona_id, "status": "deleted"}


@app.delete("/api/personas/{persona_id}/purge")
def purge_deleted_persona(persona_id: int, req: PersonaDeleteRequest, user: dict = Depends(current_user)):
    user_id = int(user["id"])
    with get_db() as db:
        row = db.execute(
            "SELECT id, name FROM personas WHERE id = ? AND user_id = ? AND status = 'deleted'",
            (persona_id, user_id),
        ).fetchone()
        persona = dict_from_row(row)
        if not persona:
            raise HTTPException(status_code=404, detail="deleted persona not found")
        if req.confirm_name.strip() != str(persona["name"]).strip():
            raise HTTPException(status_code=400, detail="请输入完整的人格名字以确认彻底清除")
        db.execute(
            """
            UPDATE group_messages
            SET speaker_persona_id = NULL,
                content = '这条来自已清除人格的群聊消息已移除。'
            WHERE user_id = ? AND speaker_persona_id = ?
            """,
            (user_id, persona_id),
        )
        db.execute(
            "DELETE FROM personas WHERE id = ? AND user_id = ? AND status = 'deleted'",
            (persona_id, user_id),
        )
    return {"ok": True, "persona_id": persona_id, "status": "purged"}


@app.get("/api/personas/{persona_id}/export")
def export_persona_data(persona_id: int, user: dict = Depends(current_user)):
    user_id = int(user["id"])
    with get_db() as db:
        persona = dict_from_row(
            db.execute(
                "SELECT * FROM personas WHERE id = ? AND user_id = ? AND status IN ('active', 'deleted')",
                (persona_id, user_id),
            ).fetchone()
        )
        if not persona:
            raise HTTPException(status_code=404, detail="persona not found")
        conversations = [
            dict_from_row(row)
            for row in db.execute(
                """
                SELECT *
                FROM conversations
                WHERE user_id = ? AND persona_id = ?
                ORDER BY id ASC
                """,
                (user_id, persona_id),
            ).fetchall()
        ]
        messages = [
            dict_from_row(row)
            for row in db.execute(
                """
                SELECT messages.*
                FROM messages
                JOIN conversations ON conversations.id = messages.conversation_id
                WHERE messages.user_id = ? AND conversations.persona_id = ?
                ORDER BY messages.conversation_id ASC, messages.id ASC
                """,
                (user_id, persona_id),
            ).fetchall()
        ]
        versions = [
            _public_persona(dict_from_row(row))
            for row in db.execute(
                """
                SELECT *
                FROM persona_versions
                WHERE persona_id = ?
                ORDER BY version ASC, id ASC
                """,
                (persona_id,),
            ).fetchall()
        ]
        expression_preference = dict_from_row(
            db.execute(
                "SELECT * FROM expression_preferences WHERE user_id = ? AND persona_id = ?",
                (user_id, persona_id),
            ).fetchone()
        )
        expressions = [
            dict_from_row(row)
            for row in db.execute(
                """
                SELECT *
                FROM message_expressions
                WHERE user_id = ? AND persona_id = ?
                ORDER BY id ASC
                """,
                (user_id, persona_id),
            ).fetchall()
        ]
        group_memberships = [
            dict_from_row(row)
            for row in db.execute(
                """
                SELECT group_members.*, group_conversations.title, group_conversations.status
                FROM group_members
                JOIN group_conversations ON group_conversations.id = group_members.group_conversation_id
                WHERE group_members.user_id = ? AND group_members.persona_id = ?
                ORDER BY group_members.group_conversation_id ASC
                """,
                (user_id, persona_id),
            ).fetchall()
        ]
        group_ids = [int(item["group_conversation_id"]) for item in group_memberships]
        group_messages = []
        if group_ids:
            placeholders = ",".join("?" for _ in group_ids)
            group_messages = [
                dict_from_row(row)
                for row in db.execute(
                    f"""
                    SELECT *
                    FROM group_messages
                    WHERE user_id = ? AND group_conversation_id IN ({placeholders})
                    ORDER BY group_conversation_id ASC, id ASC
                    """,
                    [user_id, *group_ids],
                ).fetchall()
            ]
        memories = {
            "facts": [
                dict_from_row(row)
                for row in db.execute(
                    """
                    SELECT *
                    FROM memory_facts
                    WHERE user_id = ? AND persona_id = ?
                    ORDER BY id ASC
                    """,
                    (user_id, persona_id),
                ).fetchall()
            ],
            "relations": [
                dict_from_row(row)
                for row in db.execute(
                    """
                    SELECT *
                    FROM memory_relations
                    WHERE user_id = ? AND persona_id = ?
                    ORDER BY id ASC
                    """,
                    (user_id, persona_id),
                ).fetchall()
            ],
        }
    payload = {
        "exported_at": now_ts(),
        "schema": "persona_export_v1",
        "persona": _public_persona(persona),
        "versions": versions,
        "conversations": conversations,
        "messages": messages,
        "expression_preference": expression_preference,
        "message_expressions": expressions,
        "group_memberships": group_memberships,
        "group_messages": group_messages,
        "memories": memories,
    }
    filename = f"mnemosyne-persona-{persona_id}-export.json"
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/personas/{persona_id}/restore")
def restore_persona(persona_id: int, user: dict = Depends(current_user)):
    user_id = int(user["id"])
    with get_db() as db:
        cursor = db.execute(
            """
            UPDATE personas
            SET status = 'active', updated_at = ?
            WHERE id = ? AND user_id = ? AND status = 'deleted'
            """,
            (now_ts(), persona_id, user_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="deleted persona not found")
        persona = dict_from_row(db.execute("SELECT * FROM personas WHERE id = ?", (persona_id,)).fetchone())
    return {"persona": _public_persona(persona)}


@app.post("/api/personas")
def create_persona(req: PersonaCreateRequest, user: dict = Depends(current_user)):
    user_id = int(user["id"])
    preferred_name = scrub_identity_text(str(req.preferred_name or "").strip())
    with get_db() as db:
        existing_names = [
            str(row["name"])
            for row in db.execute(
                "SELECT name FROM personas WHERE user_id = ? AND status = 'active'",
                (user_id,),
            ).fetchall()
        ]
    forged = forge_persona(
        selections=_clean_selections(req.selections),
        description=req.description,
        user_profile=_get_profile(user_id),
        existing_names=existing_names,
        preferred_name=preferred_name,
    )
    if not preferred_name and str(forged.get("name") or "").strip() == "未命名":
        raise HTTPException(
            status_code=503,
            detail="这次没有取得合适的名字。可以重新生成，或者亲自给 TA 写下一个名字。",
        )

    ts = now_ts()
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO personas (
                user_id, name, summary, prompt, traits_json, relationship,
                speaking_style, boundaries_json, memory_profile_json,
                psychological_profile_json, psychological_fit_notes,
                appearance_description, desired_image, growth_notes,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                forged["name"],
                forged["summary"],
                forged["prompt"],
                json.dumps(forged["traits"], ensure_ascii=False),
                forged["relationship"],
                forged["speaking_style"],
                json.dumps(forged["boundaries"], ensure_ascii=False),
                json.dumps(forged.get("memory_profile", {}), ensure_ascii=False),
                json.dumps(forged.get("psychological_profile", {}), ensure_ascii=False),
                forged.get("psychological_fit_notes", ""),
                forged.get("appearance_description", ""),
                forged.get("desired_image", ""),
                forged.get("growth_notes", ""),
                ts,
                ts,
            ),
        )
        persona_id = int(cursor.lastrowid)
        db.execute(
            """
            INSERT INTO persona_versions (
                persona_id, version, name, summary, prompt, traits_json,
                relationship, speaking_style, boundaries_json,
                psychological_profile_json, psychological_fit_notes,
                appearance_description, desired_image, growth_notes,
                reason, change_type, change_notes_json, created_at
            )
            VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                persona_id,
                forged["name"],
                forged["summary"],
                forged["prompt"],
                json.dumps(forged["traits"], ensure_ascii=False),
                forged["relationship"],
                forged["speaking_style"],
                json.dumps(forged["boundaries"], ensure_ascii=False),
                json.dumps(forged.get("psychological_profile", {}), ensure_ascii=False),
                forged.get("psychological_fit_notes", ""),
                forged.get("appearance_description", ""),
                forged.get("desired_image", ""),
                forged.get("growth_notes", ""),
                "initial forge",
                "initial_forge",
                json.dumps(["初始人格创建。"], ensure_ascii=False),
                ts,
            ),
        )
        persona = dict_from_row(db.execute("SELECT * FROM personas WHERE id = ?", (persona_id,)).fetchone())

    return {"persona": _public_persona(persona)}


@app.get("/api/conversations")
def conversations(user: dict = Depends(current_user), status: str = "active"):
    if status not in {"active", "archived"}:
        raise HTTPException(status_code=400, detail="invalid conversation status")
    with get_db() as db:
        rows = db.execute(
            """
            SELECT conversations.*, personas.name AS persona_name, personas.avatar_url AS persona_avatar_url,
                   (
                       SELECT messages.content
                       FROM messages
                       WHERE messages.conversation_id = conversations.id
                       ORDER BY messages.id DESC
                       LIMIT 1
                   ) AS last_message,
                   (
                       SELECT messages.role
                       FROM messages
                       WHERE messages.conversation_id = conversations.id
                       ORDER BY messages.id DESC
                       LIMIT 1
                   ) AS last_message_role,
                   (
                       SELECT messages.reply_status
                       FROM messages
                       WHERE messages.conversation_id = conversations.id
                       ORDER BY messages.id DESC
                       LIMIT 1
                   ) AS last_message_reply_status,
                   (
                       SELECT COUNT(*)
                       FROM messages
                       WHERE messages.conversation_id = conversations.id
                   ) AS message_count,
                   (
                       SELECT COUNT(*)
                       FROM messages
                       WHERE messages.conversation_id = conversations.id
                         AND messages.role = 'assistant'
                         AND messages.id > conversations.last_read_message_id
                   ) AS unread_count
            FROM conversations
            JOIN personas ON personas.id = conversations.persona_id
            WHERE conversations.user_id = ?
              AND conversations.status = ?
              AND personas.status = 'active'
            ORDER BY conversations.pinned_at DESC, conversations.updated_at DESC
            """,
            (user["id"], status),
        ).fetchall()
    return {"conversations": [dict_from_row(row) for row in rows]}


@app.get("/api/conversations/{conversation_id}/messages")
def conversation_messages(conversation_id: int, user: dict = Depends(current_user)):
    with get_db() as db:
        conversation = db.execute(
            """
            SELECT conversations.id
            FROM conversations
            JOIN personas ON personas.id = conversations.persona_id
            WHERE conversations.id = ?
              AND conversations.user_id = ?
              AND personas.status = 'active'
            """,
            (conversation_id, user["id"]),
        ).fetchone()
        if not conversation:
            raise HTTPException(status_code=404, detail="conversation not found")

        rows = db.execute(
            """
            SELECT id, role, content, reply_status, reply_error, client_message_id, created_at
            FROM messages
            WHERE conversation_id = ?
            ORDER BY id ASC
            """,
            (conversation_id,),
        ).fetchall()
        messages = [dict_from_row(row) for row in rows]
        message_ids = [int(item["id"]) for item in messages]
        expressions_by_message: dict[int, list[dict[str, Any]]] = {message_id: [] for message_id in message_ids}
        if message_ids:
            active_labels = active_expression_labels()
            placeholders = ",".join("?" for _ in message_ids)
            expression_rows = db.execute(
                f"""
                SELECT message_id, expression_type, label, source_text, created_at
                FROM message_expressions
                WHERE message_id IN ({placeholders}) AND user_id = ?
                ORDER BY id ASC
                """,
                [*message_ids, user["id"]],
            ).fetchall()
            for row in expression_rows:
                expression = dict_from_row(row)
                expression_type = str(expression.get("expression_type") or "")
                label = str(expression.get("label") or "")
                if label not in active_labels.get(expression_type, set()):
                    continue
                message_id = int(expression.pop("message_id"))
                expressions_by_message.setdefault(message_id, []).append(expression)
        for item in messages:
            item["expressions"] = expressions_by_message.get(int(item["id"]), [])
        if message_ids:
            db.execute(
                """
                UPDATE conversations
                SET last_read_message_id = ?, last_read_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (max(message_ids), now_ts(), conversation_id, user["id"]),
            )
    return {"messages": messages}


@app.post("/api/conversations/{conversation_id}/read")
def mark_conversation_read(conversation_id: int, user: dict = Depends(current_user)):
    ts = now_ts()
    with get_db() as db:
        row = db.execute(
            """
            SELECT conversations.id,
                   COALESCE(MAX(messages.id), 0) AS latest_message_id
            FROM conversations
            JOIN personas ON personas.id = conversations.persona_id
            LEFT JOIN messages ON messages.conversation_id = conversations.id
            WHERE conversations.id = ?
              AND conversations.user_id = ?
              AND personas.status = 'active'
            GROUP BY conversations.id
            """,
            (conversation_id, user["id"]),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="conversation not found")
        latest_message_id = int(row["latest_message_id"] or 0)
        db.execute(
            """
            UPDATE conversations
            SET last_read_message_id = ?, last_read_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (latest_message_id, ts, conversation_id, user["id"]),
        )
    return {"ok": True, "conversation_id": conversation_id, "last_read_message_id": latest_message_id}


@app.patch("/api/conversations/{conversation_id}")
def update_conversation(conversation_id: int, req: ConversationUpdateRequest, user: dict = Depends(current_user)):
    allowed_status = {"active", "archived"}
    title = req.title.strip() if req.title is not None else None
    status = req.status.strip() if req.status is not None else None
    if status is not None and status not in allowed_status:
        raise HTTPException(status_code=400, detail="invalid conversation status")
    if title is not None and not title:
        raise HTTPException(status_code=400, detail="title cannot be empty")
    if title is None and status is None and req.pinned is None:
        raise HTTPException(status_code=400, detail="nothing to update")

    fields: list[str] = []
    params: list[Any] = []
    if title is not None:
        fields.append("title = ?")
        params.append(title)
    if status is not None:
        fields.append("status = ?")
        params.append(status)
    if req.pinned is not None:
        fields.append("pinned_at = ?")
        params.append(now_ts() if req.pinned else 0)
    fields.append("updated_at = ?")
    params.append(now_ts())
    params.extend([conversation_id, user["id"]])

    with get_db() as db:
        cursor = db.execute(
            f"""
            UPDATE conversations
            SET {", ".join(fields)}
            WHERE id = ? AND user_id = ?
              AND EXISTS (
                  SELECT 1
                  FROM personas
                  WHERE personas.id = conversations.persona_id
                    AND personas.status = 'active'
              )
            """,
            params,
        )
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="conversation not found")
        row = db.execute(
            """
            SELECT conversations.*, personas.name AS persona_name, personas.avatar_url AS persona_avatar_url
            FROM conversations
            JOIN personas ON personas.id = conversations.persona_id
            WHERE conversations.id = ? AND personas.status = 'active'
            """,
            (conversation_id,),
        ).fetchone()
    return {"conversation": dict_from_row(row)}


@app.get("/api/group-conversations")
def group_conversations(user: dict = Depends(current_user), status: str = "active"):
    try:
        return {"group_conversations": list_group_conversations(int(user["id"]), status)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/group-conversations")
def create_group_conversation_endpoint(req: GroupConversationCreateRequest, user: dict = Depends(current_user)):
    try:
        return {
            "group_conversation": create_group_conversation(
                int(user["id"]),
                req.persona_ids,
                req.title or "",
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/group-conversations/{group_conversation_id}/messages")
def group_conversation_messages(group_conversation_id: int, user: dict = Depends(current_user)):
    try:
        return {"messages": group_messages(int(user["id"]), group_conversation_id)}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/group-conversations/{group_conversation_id}/read")
def mark_group_read(group_conversation_id: int, user: dict = Depends(current_user)):
    try:
        return mark_group_conversation_read(int(user["id"]), group_conversation_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.patch("/api/group-conversations/{group_conversation_id}")
def patch_group_conversation(
    group_conversation_id: int,
    req: GroupConversationUpdateRequest,
    user: dict = Depends(current_user),
):
    try:
        return {
            "group_conversation": update_group_conversation(
                int(user["id"]),
                group_conversation_id,
                title=req.title,
                status=req.status,
                pinned=req.pinned,
            )
        }
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.post("/api/group-conversations/{group_conversation_id}/members")
def add_group_conversation_member(
    group_conversation_id: int,
    req: GroupMemberRequest,
    user: dict = Depends(current_user),
):
    try:
        return {"group_conversation": add_group_member(int(user["id"]), group_conversation_id, int(req.persona_id))}
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.delete("/api/group-conversations/{group_conversation_id}/members/{persona_id}")
def remove_group_conversation_member(
    group_conversation_id: int,
    persona_id: int,
    user: dict = Depends(current_user),
):
    try:
        return {"group_conversation": remove_group_member(int(user["id"]), group_conversation_id, int(persona_id))}
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.post("/api/group-chat")
def group_chat_endpoint(req: GroupChatRequest, user: dict = Depends(current_user)):
    try:
        return group_chat(
            user_id=int(user["id"]),
            group_conversation_id=int(req.group_conversation_id),
            message=req.message,
            client_message_id=req.client_message_id,
        )
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except LLMProviderError as exc:
        raise HTTPException(status_code=503, detail=exc.user_message) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="group chat service unavailable") from exc


@app.post("/api/group-conversations/{group_conversation_id}/autonomous-turn")
def group_autonomous_turn_endpoint(
    group_conversation_id: int,
    req: GroupAutonomousTurnRequest,
    user: dict = Depends(current_user),
):
    try:
        return autonomous_group_turn(
            user_id=int(user["id"]),
            group_conversation_id=int(group_conversation_id),
            client_message_id=req.client_message_id,
        )
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except LLMProviderError as exc:
        raise HTTPException(status_code=503, detail=exc.user_message) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="group chat service unavailable") from exc


@app.post("/api/chat")
def chat(req: ChatRequest, background_tasks: BackgroundTasks, user: dict = Depends(current_user)):
    try:
        result = db_chat(
            user_id=int(user["id"]),
            persona_id=int(req.persona_id),
            conversation_id=req.conversation_id,
            message=req.message,
            retry_user_message_id=req.retry_user_message_id,
            client_message_id=req.client_message_id,
            defer_summary_refresh=True,
        )
        if (
            result.get("assistant_message_id")
            and (result.get("conversation_summary") or {}).get("scheduled")
        ):
            background_tasks.add_task(
                refresh_conversation_summary,
                user_id=int(user["id"]),
                persona_id=int(req.persona_id),
                conversation_id=int(result["conversation_id"]),
                latest_message_id=int(result["assistant_message_id"]),
            )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LLMProviderError as exc:
        raise HTTPException(status_code=503, detail=exc.user_message) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="服务暂时不可用，请稍后再试。") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="聊天服务暂时不可用，请稍后再试。") from exc


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/admin")
def admin_index():
    return FileResponse(ADMIN_WEB_DIR / "index.html")


@app.get("/admin/admin.js")
def admin_script():
    return FileResponse(ADMIN_WEB_DIR / "admin.js")


@app.get("/admin/admin.css")
def admin_style():
    return FileResponse(ADMIN_WEB_DIR / "admin.css")


def _get_profile(user_id: int) -> dict:
    with get_db() as db:
        row = db.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone()
    profile = dict_from_row(row) or {}
    preferences_json = profile.pop("preferences_json", "{}")
    try:
        profile["preferences"] = json.loads(preferences_json or "{}")
    except Exception:
        profile["preferences"] = {}
    return profile


def _clean_selections(selections: dict[str, list[str]]) -> dict[str, list[str]]:
    cleaned: dict[str, list[str]] = {}
    for key, allowed in PERSONA_OPTIONS.items():
        chosen = selections.get(key, [])
        if not isinstance(chosen, list):
            chosen = []
        cleaned[key] = [item for item in chosen if item in allowed][:4]
    return cleaned


def _public_persona(persona: dict | None) -> dict:
    if not persona:
        return {}
    result = dict(persona)
    for key, default in (
        ("traits_json", "[]"),
        ("boundaries_json", "[]"),
        ("psychological_profile_json", "{}"),
        ("memory_profile_json", "{}"),
    ):
        raw = result.pop(key, default)
        try:
            result[key.removesuffix("_json")] = json.loads(raw or default)
        except Exception:
            result[key.removesuffix("_json")] = json.loads(default)
    if "change_notes_json" in result:
        try:
            result["change_notes"] = json.loads(result.pop("change_notes_json") or "[]")
        except Exception:
            result["change_notes"] = []
    return result


def _generate_local_persona_avatar(user_id: int, persona: dict) -> str:
    persona_id = int(persona.get("id") or 0)
    name = str(persona.get("name") or "TA").strip() or "TA"
    image_hint = str(persona.get("desired_image") or persona.get("appearance_description") or persona.get("summary") or "").strip()
    seed = f"{persona_id}:{name}:{image_hint}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    hue = int(digest[:2], 16) % 360
    accent_hue = (hue + 42 + int(digest[2:4], 16) % 84) % 360
    initial = _avatar_initial(name)
    tag = _avatar_tag(image_hint or str(persona.get("relationship") or persona.get("speaking_style") or ""))
    svg = _persona_avatar_svg(
        initial=initial,
        tag=tag,
        hue=hue,
        accent_hue=accent_hue,
        shape_seed=int(digest[4:8], 16),
    )
    user_dir = UPLOAD_DIR / str(user_id) / "generated"
    user_dir.mkdir(parents=True, exist_ok=True)
    filename = f"persona-{persona_id}-{digest[:12]}.svg"
    path = user_dir / filename
    path.write_text(svg, encoding="utf-8")
    return f"/uploads/{user_id}/generated/{filename}"


def _avatar_initial(name: str) -> str:
    compact = "".join(ch for ch in str(name or "TA").strip() if not ch.isspace())
    return compact[:2] or "TA"


def _avatar_tag(text: str) -> str:
    compact = "".join(ch for ch in str(text or "").strip() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    return compact[:8]


def _persona_avatar_svg(*, initial: str, tag: str, hue: int, accent_hue: int, shape_seed: int) -> str:
    escaped_initial = html.escape(initial)
    escaped_tag = html.escape(tag)
    radius = 18 + shape_seed % 18
    cx = 68 + shape_seed % 34
    cy = 54 + (shape_seed // 7) % 42
    tag_text = (
        f'<text x="80" y="120" text-anchor="middle" font-size="10" '
        f'font-family="Arial, sans-serif" fill="rgba(255,255,255,.76)">{escaped_tag}</text>'
        if escaped_tag
        else ""
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="160" height="160" viewBox="0 0 160 160">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="hsl({hue}, 46%, 38%)"/>
    <stop offset="1" stop-color="hsl({accent_hue}, 42%, 56%)"/>
  </linearGradient>
</defs>
<rect width="160" height="160" rx="34" fill="url(#bg)"/>
<circle cx="{cx}" cy="{cy}" r="{radius}" fill="rgba(255,255,255,.18)"/>
<circle cx="{126 - cx // 3}" cy="{118 - cy // 5}" r="{max(12, radius - 6)}" fill="rgba(255,255,255,.1)"/>
<text x="80" y="90" text-anchor="middle" font-size="48" font-weight="700" font-family="Arial, sans-serif" fill="white">{escaped_initial}</text>
{tag_text}
</svg>
"""


def _expression_preference_public(user_id: int, persona_id: int) -> dict[str, Any]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT enabled, mode, source_message_id, updated_at
            FROM expression_preferences
            WHERE user_id = ? AND persona_id = ?
            """,
            (user_id, persona_id),
        ).fetchone()
    if not row:
        return {"enabled": True, "mode": "normal", "explicit": False, "updated_at": 0, "source_message_id": None}
    item = dict_from_row(row) or {}
    mode = _normalize_expression_mode(item.get("mode"), bool(int(item.get("enabled", 1) or 0)))
    return {
        "enabled": mode != "off",
        "mode": mode,
        "explicit": True,
        "updated_at": int(item.get("updated_at") or 0),
        "source_message_id": item.get("source_message_id"),
    }


def _with_expression_asset_metadata(item: dict[str, Any], asset_map: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    expression_type = str(item.get("expression_type") or "")
    label = str(item.get("label") or "")
    asset = asset_map.get((expression_type, label)) or {}
    item["asset_enabled"] = bool(asset.get("enabled", False))
    item["asset_known"] = bool(asset)
    item["asset_kind"] = asset.get("asset_kind") or "unknown"
    item["display_text"] = asset.get("display_text") or label
    item["icon"] = asset.get("icon") or ""
    item["group"] = asset.get("group") or "unknown"
    item["risk_level"] = asset.get("risk_level") or "unknown"
    item["intensity"] = int(asset.get("intensity") or 0)
    item["cooldown_turns"] = int(asset.get("cooldown_turns") or 0)
    return item


def _persona_list_item(persona: dict) -> dict:
    result = dict(persona)
    latest_version = int(result.pop("latest_reviewed_version", 0) or 0)
    latest_at = int(result.pop("latest_reviewed_at", 0) or 0)
    seen_version = int(result.pop("seen_reviewed_version", 0) or 0)
    result.pop("retry_preference_request_count", None)
    expression_enabled = result.pop("expression_enabled", None)
    expression_mode = result.pop("expression_mode", None)
    mode = _normalize_expression_mode(expression_mode, None if expression_enabled is None else bool(int(expression_enabled)))
    result["expression_preference"] = {
        "enabled": mode != "off",
        "mode": mode,
        "explicit": expression_enabled is not None,
    }
    result["growth_notice"] = (
        {
            "kind": "adaptation",
            "title": "相处方式有变化",
            "version": latest_version,
            "created_at": latest_at,
        }
        if latest_version > seen_version
        else None
    )
    result["growth_action"] = None
    return result


def _normalize_expression_mode(mode: Any, enabled: bool | None = None) -> str:
    value = str(mode or "").strip().lower()
    aliases = {
        "off": "off",
        "disabled": "off",
        "disable": "off",
        "false": "off",
        "subtle": "subtle",
        "low": "subtle",
        "less": "subtle",
        "quiet": "subtle",
        "normal": "normal",
        "on": "normal",
        "enabled": "normal",
        "true": "normal",
    }
    if value in aliases:
        return aliases[value]
    if enabled is False:
        return "off"
    return "normal"


def _public_review_highlights(previous_version: dict | None, reviewed_version: dict | None) -> list[str]:
    before = _public_persona(previous_version)
    after = _public_persona(reviewed_version)
    if not before or not after:
        return ["相处方式完成了一次轻微调整"]
    highlights: list[str] = []
    field_labels = (
        ("speaking_style", "回应方式更贴近你的偏好"),
        ("boundaries", "相处边界更加明确"),
        ("relationship", "相处定位经过确认"),
        ("psychological_fit_notes", "支持你的方式做了细微调整"),
        ("summary", "整体相处感觉做了轻微调整"),
        ("traits", "表达气质做了细微调整"),
    )
    for key, label in field_labels:
        if before.get(key) != after.get(key):
            highlights.append(label)
    return highlights[:3] or ["相处方式完成了一次轻微调整"]


def _public_growth_feedback(
    feedback: dict | None,
    *,
    include_detail: bool = False,
    include_version: bool = False,
) -> dict | None:
    if not feedback:
        return None
    public_feedback: dict[str, Any] = {"reaction": feedback.get("reaction", "")}
    if include_version:
        public_feedback["reviewed_version"] = int(feedback.get("reviewed_version") or 0)
    if include_detail:
        public_feedback["detail_text"] = feedback.get("detail_text", "")
    if feedback.get("reaction") == "needs_adjustment":
        followed_up_at = int(feedback.get("resolved_at") or 0)
        public_feedback["followup_status"] = "completed" if followed_up_at else "waiting"
        if followed_up_at:
            public_feedback["followed_up_at"] = followed_up_at
    return public_feedback


def _normalize_existing_persona_identity() -> dict[str, int]:
    changed = {"personas": 0, "persona_versions": 0}

    def scrub_list(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        return [
            scrub_identity_text(str(item).strip())
            for item in values
            if str(item).strip() and not is_identity_polluted_boundary(item)
        ]

    with get_db() as db:
        rows = db.execute("SELECT * FROM personas").fetchall()
        for row in rows:
            persona = _public_persona(dict_from_row(row))
            for key in (
                "name",
                "summary",
                "relationship",
                "speaking_style",
                "psychological_fit_notes",
                "appearance_description",
                "desired_image",
                "growth_notes",
            ):
                persona[key] = scrub_identity_text(persona.get(key))
            persona["traits"] = scrub_list(persona.get("traits"))
            persona["boundaries"] = scrub_list(persona.get("boundaries"))
            prompt = build_prompt(persona)
            db.execute(
                """
                UPDATE personas
                SET name = ?, summary = ?, relationship = ?, speaking_style = ?,
                    traits_json = ?, boundaries_json = ?, psychological_fit_notes = ?,
                    appearance_description = ?, desired_image = ?, growth_notes = ?, prompt = ?
                WHERE id = ?
                """,
                (
                    persona.get("name", ""),
                    persona.get("summary", ""),
                    persona.get("relationship", ""),
                    persona.get("speaking_style", ""),
                    json.dumps(persona.get("traits", []), ensure_ascii=False),
                    json.dumps(persona.get("boundaries", []), ensure_ascii=False),
                    persona.get("psychological_fit_notes", ""),
                    persona.get("appearance_description", ""),
                    persona.get("desired_image", ""),
                    persona.get("growth_notes", ""),
                    prompt,
                    persona["id"],
                ),
            )
            changed["personas"] += 1

        rows = db.execute("SELECT * FROM persona_versions").fetchall()
        for row in rows:
            version = _public_persona(dict_from_row(row))
            for key in (
                "name",
                "summary",
                "relationship",
                "speaking_style",
                "psychological_fit_notes",
                "appearance_description",
                "desired_image",
                "growth_notes",
            ):
                version[key] = scrub_identity_text(version.get(key))
            version["traits"] = scrub_list(version.get("traits"))
            version["boundaries"] = scrub_list(version.get("boundaries"))
            prompt = build_prompt(version)
            db.execute(
                """
                UPDATE persona_versions
                SET name = ?, summary = ?, relationship = ?, speaking_style = ?,
                    traits_json = ?, boundaries_json = ?, psychological_fit_notes = ?,
                    appearance_description = ?, desired_image = ?, growth_notes = ?, prompt = ?
                WHERE id = ?
                """,
                (
                    version.get("name", ""),
                    version.get("summary", ""),
                    version.get("relationship", ""),
                    version.get("speaking_style", ""),
                    json.dumps(version.get("traits", []), ensure_ascii=False),
                    json.dumps(version.get("boundaries", []), ensure_ascii=False),
                    version.get("psychological_fit_notes", ""),
                    version.get("appearance_description", ""),
                    version.get("desired_image", ""),
                    version.get("growth_notes", ""),
                    prompt,
                    version["id"],
                ),
            )
            changed["persona_versions"] += 1

        for table, columns in {
            "user_insights": [
                "profile_summary",
                "interaction_style",
                "emotional_patterns_json",
                "inferred_profile_json",
                "topic_model_json",
                "guidance_json",
            ],
            "conversation_summaries": ["summary_text", "key_points_json"],
            "conversations": ["summary"],
            "memories": ["text"],
            "memory_facts": ["text"],
            "memory_relations": ["subject", "predicate", "object", "text"],
            "memory_summaries": ["text", "source_uids_json"],
            "persona_revision_suggestions": ["reason", "suggestion_json", "source_context_json", "decision_note"],
            "chat_context_traces": ["context_json", "error_text"],
        }.items():
            try:
                rows = db.execute(f"SELECT rowid AS _rowid, * FROM {table}").fetchall()
            except Exception:
                continue
            table_changed = 0
            for row in rows:
                item = dict_from_row(row) or {}
                updates = {}
                for column in columns:
                    if column in item and item.get(column) is not None:
                        cleaned = scrub_identity_text(item.get(column))
                        if cleaned != item.get(column):
                            updates[column] = cleaned
                if updates:
                    set_sql = ", ".join(f"{column} = ?" for column in updates)
                    db.execute(
                        f"UPDATE {table} SET {set_sql} WHERE rowid = ?",
                        [*updates.values(), item["_rowid"]],
                    )
                    table_changed += 1
            if table_changed:
                changed[table] = table_changed

    return changed


try:
    _normalize_existing_persona_identity()
except Exception as exc:
    print("[IdentitySanitizer] persona normalization skipped:", exc)


app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
app.mount("/", StaticFiles(directory=WEB_DIR), name="web")
