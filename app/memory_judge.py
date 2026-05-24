from __future__ import annotations

import json
import re
from typing import Any

from .database import dict_from_row, get_db, now_ts
from .llm_client import call_llm_api


JUDGE_SYSTEM = """You are Memory Judge for a long-term AI chat system.
Review extracted memories for quality and risk.
Do not judge the user. Judge whether the memory item is useful, safe, and well formed.

Return strict JSON only:
{
  "quality_score": 0.0,
  "risk_score": 0.0,
  "action": "keep|revise|archive|lock",
  "reasons": ["short reason"],
  "flags": ["duplicate|too_vague|sensitive|unsupported|conflict|important"]
}
"""


ALLOWED_ACTIONS = {"keep", "revise", "archive", "lock"}


def judge_stored_memories(
    *,
    user_id: int,
    persona_id: int | None,
    source_message_id: int | None,
    user_text: str,
    stored_memories: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    judgements = []
    seen = set()
    for memory in stored_memories:
        uid = memory.get("uid") or f"legacy-{memory.get('id')}"
        if not uid or uid in seen:
            continue
        seen.add(uid)
        if memory.get("layer") == "legacy":
            continue
        text = str(memory.get("text") or memory.get("summary") or "").strip()
        if not text:
            continue
        judgement = _judge_one(user_text, memory)
        judgements.append(
            save_judgement(
                user_id=user_id,
                persona_id=persona_id,
                memory_uid=str(uid),
                memory_layer=str(memory.get("layer") or ""),
                memory_type=str(memory.get("type") or ""),
                memory_text=text,
                source_message_id=source_message_id,
                judgement=judgement,
            )
        )
    return judgements


def list_judgements(
    user_id: int,
    persona_id: int | None = None,
    status: str | None = "open",
    limit: int = 50,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    params: list[Any] = [user_id]
    persona_clause = ""
    status_clause = ""
    if persona_id is not None:
        persona_clause = "AND persona_id = ?"
        params.append(persona_id)
    if status:
        status_clause = "AND status = ?"
        params.append(status)
    params.append(limit)
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT *
            FROM memory_judgements
            WHERE user_id = ? {persona_clause} {status_clause}
            ORDER BY risk_score DESC, quality_score ASC, updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
    return [_decode(dict_from_row(row) or {}) for row in rows]


def update_judgement_status(user_id: int, judgement_id: int, status: str) -> dict[str, Any]:
    if status not in {"open", "accepted", "dismissed"}:
        raise ValueError("invalid judgement status")
    with get_db() as db:
        db.execute(
            """
            UPDATE memory_judgements
            SET status = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (status, now_ts(), judgement_id, user_id),
        )
        row = db.execute("SELECT * FROM memory_judgements WHERE id = ? AND user_id = ?", (judgement_id, user_id)).fetchone()
    item = dict_from_row(row)
    if not item:
        raise ValueError("judgement not found")
    return _decode(item)


def save_judgement(
    *,
    user_id: int,
    persona_id: int | None,
    memory_uid: str,
    memory_layer: str,
    memory_type: str,
    memory_text: str,
    source_message_id: int | None,
    judgement: dict[str, Any],
) -> dict[str, Any]:
    quality = _float(judgement.get("quality_score"), 0.5)
    risk = _float(judgement.get("risk_score"), 0.0)
    action = str(judgement.get("action") or "keep")
    if action not in ALLOWED_ACTIONS:
        action = "keep"
    reasons = _as_list(judgement.get("reasons"))
    flags = _as_list(judgement.get("flags"))
    ts = now_ts()
    with get_db() as db:
        db.execute(
            """
            INSERT INTO memory_judgements (
                user_id, persona_id, memory_uid, memory_layer, memory_type,
                memory_text, source_message_id, quality_score, risk_score,
                action, reasons_json, flags_json, status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
            ON CONFLICT(memory_uid)
            DO UPDATE SET memory_text = excluded.memory_text,
                          quality_score = excluded.quality_score,
                          risk_score = excluded.risk_score,
                          action = excluded.action,
                          reasons_json = excluded.reasons_json,
                          flags_json = excluded.flags_json,
                          status = CASE
                              WHEN memory_judgements.status = 'open' THEN 'open'
                              ELSE memory_judgements.status
                          END,
                          updated_at = excluded.updated_at
            """,
            (
                user_id,
                persona_id,
                memory_uid,
                memory_layer,
                memory_type,
                memory_text,
                source_message_id,
                quality,
                risk,
                action,
                json.dumps(reasons, ensure_ascii=False),
                json.dumps(flags, ensure_ascii=False),
                ts,
                ts,
            ),
        )
        row = db.execute("SELECT * FROM memory_judgements WHERE memory_uid = ?", (memory_uid,)).fetchone()
    return _decode(dict_from_row(row) or {})


def _judge_one(user_text: str, memory: dict[str, Any]) -> dict[str, Any]:
    rule = _rule_judge(user_text, memory)
    try:
        raw = call_llm_api(
            [
                {"role": "system", "content": JUDGE_SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"user_message": user_text, "memory": memory, "rule_baseline": rule},
                        ensure_ascii=False,
                        indent=2,
                    ),
                },
            ],
            task="judge",
        )
        obj = _extract_json(raw)
        if obj:
            return _merge_rule_and_llm(rule, obj)
    except Exception as exc:
        print("[MemoryJudge] LLM skipped:", exc)
    return rule


def _rule_judge(user_text: str, memory: dict[str, Any]) -> dict[str, Any]:
    text = str(memory.get("text") or memory.get("summary") or "")
    memory_type = str(memory.get("type") or "")
    reasons = []
    flags = []
    quality = 0.72
    risk = 0.0
    action = "keep"

    if len(text.strip()) < 6:
        quality -= 0.25
        flags.append("too_vague")
        reasons.append("Memory text is very short.")
        action = "revise"
    if memory_type in {"identity", "boundary"}:
        quality += 0.12
        flags.append("important")
        action = "lock"
    if memory_type == "persona_feedback":
        quality += 0.06
        flags.append("important")
    if _contains_sensitive(text):
        risk += 0.8
        quality -= 0.35
        flags.append("sensitive")
        reasons.append("Memory may contain sensitive personal data.")
        action = "archive"
    if text and text not in user_text and memory_type not in {"emotional_pattern"}:
        quality -= 0.08
        flags.append("unsupported")
        reasons.append("Memory is not a direct substring of the source message; check extraction.")
    if not reasons:
        reasons.append("Memory appears usable.")

    return {
        "quality_score": max(0.0, min(1.0, quality)),
        "risk_score": max(0.0, min(1.0, risk)),
        "action": action,
        "reasons": reasons,
        "flags": flags,
    }


def _merge_rule_and_llm(rule: dict[str, Any], llm: dict[str, Any]) -> dict[str, Any]:
    risk = max(_float(rule.get("risk_score"), 0), _float(llm.get("risk_score"), 0))
    quality = min(_float(rule.get("quality_score"), 0.5), _float(llm.get("quality_score"), 0.5)) if risk >= 0.6 else _float(llm.get("quality_score"), rule.get("quality_score", 0.5))
    action = str(llm.get("action") or rule.get("action") or "keep")
    if rule.get("action") == "archive":
        action = "archive"
    if rule.get("action") == "lock" and action == "keep":
        action = "lock"
    if action not in ALLOWED_ACTIONS:
        action = str(rule.get("action") or "keep")
    return {
        "quality_score": max(0.0, min(1.0, quality)),
        "risk_score": max(0.0, min(1.0, risk)),
        "action": action,
        "reasons": [*_as_list(rule.get("reasons")), *_as_list(llm.get("reasons"))][:12],
        "flags": sorted(set([*_as_list(rule.get("flags")), *_as_list(llm.get("flags"))]))[:12],
    }


def _contains_sensitive(text: str) -> bool:
    patterns = [
        r"\b\d{16,19}\b",
        r"\b\d{3}-\d{2}-\d{4}\b",
        r"\b\d{11,18}\b",
        r"password|api[_ -]?key|secret|token",
        r"银行卡|身份证|密码|住址|地址",
    ]
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in patterns)


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("reasons_json", "flags_json"):
        out = key.removesuffix("_json")
        try:
            row[out] = json.loads(row.pop(key) or "[]")
        except Exception:
            row[out] = []
    return row


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _float(value: Any, default: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return default


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []
