from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from fastapi import Cookie, Depends, FastAPI, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import (
    GUEST_SECONDS,
    SESSION_COOKIE,
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
    set_session_cookie,
)
from .archivist import recall_memories
from .config import load_config
from .database import dict_from_row, get_db, init_db, now_ts
from .db_chat import db_chat, normalize_existing_assistant_messages
from .identity import is_identity_polluted_boundary, scrub_identity_text
from .layered_memory import apply_memory_decay, recall_layered_memory, refresh_memory_state, refresh_memory_summaries
from .llm_client import LLMProviderError
from .memory_review import context_traces, get_memory_item, memory_review, update_memory_item
from .memory_judge import update_judgement_status
from .memory_conflicts import update_conflict_status
from .memory_eval import (
    list_memory_eval_runs,
    run_chat_context_evaluation,
    run_live_answer_evaluation,
    run_memory_evaluation,
    run_memory_policy_evaluation,
    run_state_expiry_evaluation,
    run_state_resolution_evaluation,
    seed_memory_eval_data,
)
from .memory_rag import semantic_memory_recall, sync_memory_embeddings
from .memory_policy import policy_snapshot
from .mirror import get_user_insight, update_user_insight
from .persona_forge import build_prompt, forge_persona
from .sculptor import (
    apply_revision_suggestion,
    dismiss_revision_suggestion,
    generate_revision_suggestion,
    list_revision_suggestions,
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


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=40)
    password: str = Field(..., min_length=8, max_length=200)


class GuestConvertRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=40)
    password: str = Field(..., min_length=8, max_length=200)
    nickname: str | None = Field(default=None, max_length=60)


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


class PersonaAvatarGenerateRequest(BaseModel):
    desired_image: str | None = Field(default=None, max_length=2000)


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


class MemoryUpdateRequest(BaseModel):
    priority: str | None = None
    locked: bool | None = None
    archived: bool | None = None


class PersonaRevisionRequest(BaseModel):
    reason: str = Field(default="", max_length=1000)


class PersonaRevisionDecisionRequest(BaseModel):
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


@app.post("/api/auth/register")
def register(req: RegisterRequest, response: Response):
    user = create_user(req.username, req.password, req.nickname)
    token = create_session(int(user["id"]))
    set_session_cookie(response, token)
    return {"user": public_user(user), "profile": _get_profile(int(user["id"]))}


@app.post("/api/auth/login")
def login(req: LoginRequest, response: Response):
    user = authenticate_user(req.username, req.password)
    if not user:
        raise HTTPException(status_code=401, detail="invalid username or password")

    token = create_session(int(user["id"]))
    set_session_cookie(response, token)
    return {"user": public_user(user), "profile": _get_profile(int(user["id"]))}


@app.post("/api/auth/guest")
def guest_login(response: Response):
    user = create_guest_user()
    token = create_session(int(user["id"]), max_age=GUEST_SECONDS)
    set_session_cookie(response, token, max_age=GUEST_SECONDS)
    return {"user": public_user(user), "profile": _get_profile(int(user["id"]))}


@app.post("/api/auth/guest/convert")
def convert_guest(req: GuestConvertRequest, response: Response, user: dict = Depends(current_user)):
    converted = convert_guest_user(
        user_id=int(user["id"]),
        username=req.username,
        password=req.password,
        nickname=req.nickname,
    )
    token = create_session(int(converted["id"]))
    set_session_cookie(response, token)
    return {"user": public_user(converted), "profile": _get_profile(int(converted["id"]))}


@app.post("/api/auth/logout")
def logout(
    response: Response,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE),
):
    clear_session(response, session_token)
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
    allowed = ("provider", "provider_name", "model", "base_url", "api_key_env", "temperature")
    return {key: config.get(key) for key in allowed if config.get(key) not in (None, "")}


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
                   user_profiles.nickname
            FROM users
            LEFT JOIN user_profiles ON user_profiles.user_id = users.id
            ORDER BY users.created_at DESC
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
                   version, status, created_at, updated_at
            FROM personas
            WHERE user_id = ?
            ORDER BY updated_at DESC
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
    return {"suggestions": list_revision_suggestions(owner_id, persona_id, limit)}


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
    return {
        "persona": _public_persona(persona),
        "growth_memories": {
            "facts": [dict_from_row(row) for row in feedback_facts],
            "relations": [dict_from_row(row) for row in feedback_relations],
        },
        "versions": [_public_persona(dict_from_row(row)) for row in versions],
    }


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
                   COALESCE(persona_growth_views.seen_reviewed_version, 0) AS seen_reviewed_version
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
        reviewed_version = dict_from_row(db.execute(
            """
            SELECT *
            FROM persona_versions
            WHERE persona_id = ? AND change_type = 'sculptor_review'
            ORDER BY version DESC, id DESC
            LIMIT 1
            """,
            (persona_id,),
        ).fetchone())
        previous_version = None
        if reviewed_version:
            previous_version = dict_from_row(db.execute(
                """
                SELECT *
                FROM persona_versions
                WHERE persona_id = ? AND version < ?
                ORDER BY version DESC, id DESC
                LIMIT 1
                """,
                (persona_id, int(reviewed_version["version"])),
            ).fetchone())
        growth_view = db.execute(
            """
            SELECT seen_reviewed_version
            FROM persona_growth_views
            WHERE user_id = ? AND persona_id = ?
            """,
            (user_id, persona_id),
        ).fetchone()

    remembered_count = fact_count + relation_count
    seen_reviewed_version = int(growth_view["seen_reviewed_version"]) if growth_view else 0
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
        }
    }


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
    return {"persona": _public_persona(persona)}


@app.post("/api/personas/{persona_id}/avatar/generate")
def generate_persona_avatar_placeholder(
    persona_id: int,
    req: PersonaAvatarGenerateRequest,
    user: dict = Depends(current_user),
):
    user_id = int(user["id"])
    with get_db() as db:
        row = db.execute(
            "SELECT id, desired_image FROM personas WHERE id = ? AND user_id = ? AND status = 'active'",
            (persona_id, user_id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="persona not found")
        if req.desired_image is not None:
            db.execute(
                """
                UPDATE personas
                SET desired_image = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (req.desired_image, now_ts(), persona_id, user_id),
            )
    return {
        "ok": False,
        "status": "reserved",
        "message": "avatar generation endpoint is reserved; image generation is not connected yet",
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


@app.post("/api/chat")
def chat(req: ChatRequest, user: dict = Depends(current_user)):
    try:
        return db_chat(
            user_id=int(user["id"]),
            persona_id=int(req.persona_id),
            conversation_id=req.conversation_id,
            message=req.message,
            retry_user_message_id=req.retry_user_message_id,
            client_message_id=req.client_message_id,
        )
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
def admin_index(admin: dict = Depends(current_admin)):
    return FileResponse(ADMIN_WEB_DIR / "index.html")


@app.get("/admin/admin.js")
def admin_script(admin: dict = Depends(current_admin)):
    return FileResponse(ADMIN_WEB_DIR / "admin.js")


@app.get("/admin/admin.css")
def admin_style(admin: dict = Depends(current_admin)):
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


def _persona_list_item(persona: dict) -> dict:
    result = dict(persona)
    latest_version = int(result.pop("latest_reviewed_version", 0) or 0)
    latest_at = int(result.pop("latest_reviewed_at", 0) or 0)
    seen_version = int(result.pop("seen_reviewed_version", 0) or 0)
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
    return result


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
