from __future__ import annotations

import re
import time
from typing import Any


CORE_DYNAMIC_LIMIT = 8
RESOLVABLE_KINDS = {"plans.upcoming", "reminders.active", "projects.active", "goals.current"}


def curate_dynamic_state(facts: list[dict], relations: list[dict]) -> dict[str, list[dict[str, Any]]]:
    """Build structured dynamic state assertions from important memories.

    This is deliberately rule-first. LLM extraction can enrich it later, but the
    chat pipeline should still have useful state when the API is unavailable.
    """
    raw_assertions: list[dict[str, Any]] = []
    for source in facts:
        raw_assertions.extend(_assertions_from_memory(source, "fact"))
    for source in relations:
        raw_assertions.extend(_assertions_from_memory(source, "relation"))

    resolutions = [item for item in raw_assertions if item.get("kind") == "resolutions.completed"]
    buckets: dict[str, list[dict[str, Any]]] = {}
    for assertion in raw_assertions:
        if assertion.get("kind") in RESOLVABLE_KINDS:
            resolution = _matching_resolution(assertion, resolutions)
            if resolution:
                resolution.setdefault("resolved_sources", []).append(assertion.get("source_uid"))
                continue
        _append(buckets, assertion)

    curated = {}
    for key, items in sorted(buckets.items()):
        active = [item for item in items if item.get("lifecycle") not in {"expired"}]
        ranked = _rank_items(active)
        if ranked:
            curated[key] = ranked
    return curated


def _assertions_from_memory(memory: dict[str, Any], layer: str) -> list[dict[str, Any]]:
    if not _stateworthy(memory):
        return []

    text = _clean(str(memory.get("text") or memory.get("summary") or ""))
    if not text:
        return []

    memory_type = str(memory.get("type") or "").strip().lower()
    predicate = str(memory.get("predicate") or "").strip().lower()
    obj = str(memory.get("object") or "").strip()
    assertions: list[dict[str, Any]] = []

    if _is_completion_signal(text):
        assertions.append(_assertion("resolutions.completed", memory, layer, text, obj or _completion_value(text), "completed_state"))

    if memory_type == "plan" or predicate == "has_plan" or _has_any(text, ("计划", "明天", "今天", "下周", "之后", "准备", "提醒", "别忘")):
        if not _is_completion_signal(text):
            assertions.append(_assertion("plans.upcoming", memory, layer, text, obj or text, "current_plan"))
        if _has_any(text, ("提醒", "别忘", "记得")) and not _is_completion_signal(text):
            assertions.append(_assertion("reminders.active", memory, layer, text, obj or text, "reminder"))

    if _has_any(text, ("项目", "程序", "系统", "作品", "简历", "展示", "portfolio", "resume")) and not _is_completion_signal(text):
        assertions.append(_assertion("projects.active", memory, layer, text, _project_value(text), "active_project"))

    if memory_type == "persona_feedback" or predicate == "persona_feedback" or _has_any(text, ("短一点", "别说教", "不要说教", "少追问", "不要一直追问", "语气", "回复")):
        assertions.append(_assertion("communication.rules", memory, layer, text, text, "communication_rule"))

    if memory_type == "boundary" or predicate == "boundary" or _has_any(text, ("不要", "别", "避免", "不许", "不能")):
        assertions.append(_assertion("boundaries.active", memory, layer, text, obj or _boundary_value(text), "boundary"))

    if memory_type == "relationship" or predicate == "relationship_expectation" or _has_any(text, ("关系", "恋人", "朋友", "陪伴", "亲密", "距离感")):
        assertions.append(_assertion("relationship.expectations", memory, layer, text, obj or text, "relationship_expectation"))

    if _has_any(text, ("目标", "想要", "希望", "需要", "我要", "我想")):
        assertions.append(_assertion("goals.current", memory, layer, text, _goal_value(text), "goal"))

    if memory_type not in {"identity", "preference"} and not assertions:
        assertions.append(_assertion(_fallback_key(memory_type, predicate), memory, layer, text, obj or text, "important_memory"))

    return assertions


def _assertion(kind: str, memory: dict[str, Any], layer: str, text: str, value: str, label: str) -> dict[str, Any]:
    importance = _float(memory.get("importance", 0.5))
    confidence = _float(memory.get("confidence", 0.5))
    urgency = _urgency(text)
    stability = _stability(text)
    source_ts = int(memory.get("updated_at") or memory.get("valid_from") or time.time())
    lifecycle = _lifecycle(text, kind, label, source_ts)
    injection = _injection_policy(kind, lifecycle, urgency, stability)
    return {
        "kind": kind,
        "label": label,
        "value": str(value or text)[:260],
        "text": text[:500],
        "source_uid": memory.get("uid"),
        "source_layer": layer,
        "source_type": memory.get("type") or memory.get("predicate") or "",
        "importance": round(importance, 3),
        "confidence": round(confidence, 3),
        "urgency": urgency,
        "stability": stability,
        "lifecycle": lifecycle["status"],
        "lifecycle_reason": lifecycle["reason"],
        "expires_at": lifecycle["expires_at"],
        "injection_policy": injection,
        "recall_weight": round((importance * 0.45) + (confidence * 0.25) + (urgency * 0.2) + (stability * 0.1), 3),
        "updated_at": source_ts,
        "tags": _tags(text),
    }


def _append(buckets: dict[str, list[dict[str, Any]]], assertion: dict[str, Any]) -> None:
    key = str(assertion.get("kind") or "misc.important")
    buckets.setdefault(key, []).append(assertion)


def _rank_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = sorted(
        items,
        key=lambda item: (
            _policy_rank(str(item.get("injection_policy") or "")),
            -float(item.get("recall_weight") or 0),
            -float(item.get("importance") or 0),
            -int(item.get("updated_at") or 0),
            str(item.get("source_uid") or ""),
        ),
    )
    deduped = []
    seen = set()
    for item in items:
        key = (item.get("kind"), item.get("text"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= CORE_DYNAMIC_LIMIT:
            break
    return deduped


def _policy_rank(policy: str) -> int:
    order = {"always_inject": 0, "inject_when_relevant": 1, "recall_only": 2}
    return order.get(policy, 3)


def _stateworthy(item: dict[str, Any]) -> bool:
    text = str(item.get("text") or item.get("summary") or "").strip()
    if not text:
        return False
    priority = str(item.get("priority") or "normal")
    importance = _float(item.get("importance", 0.5))
    locked = int(item.get("locked", 0) or 0)
    return locked == 1 or priority in {"critical", "high"} or importance >= 0.58


def _fallback_key(memory_type: str, predicate: str) -> str:
    raw = predicate or memory_type or "important"
    key = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", raw.lower()).strip("_")
    return f"memory.{key[:48] or 'important'}"


def _project_value(text: str) -> str:
    match = re.search(r"([^，。！？,.!?]{0,20}(项目|程序|系统|作品|简历|展示)[^，。！？,.!?]{0,30})", text)
    return _clean(match.group(1)) if match else text[:160]


def _goal_value(text: str) -> str:
    match = re.search(r"(想要|希望|需要|我要|我想)([^，。！？,.!?]{2,80})", text)
    return _clean(match.group(0)) if match else text[:160]


def _boundary_value(text: str) -> str:
    match = re.search(r"(不要|别|避免|不许|不能)([^，。！？,.!?]{1,80})", text)
    return _clean(match.group(0)) if match else text[:160]


def _urgency(text: str) -> float:
    if _has_any(text, ("现在", "马上", "今天", "今晚", "明天", "下次", "提醒", "别忘")):
        return 0.9
    if _has_any(text, ("以后", "之后", "长期", "一直")):
        return 0.7
    return 0.45


def _stability(text: str) -> float:
    if _has_any(text, ("以后", "一直", "长期", "不要", "别", "希望", "喜欢", "讨厌")):
        return 0.85
    if _has_any(text, ("今天", "明天", "这次", "当前", "现在")):
        return 0.55
    return 0.65


def _lifecycle(text: str, kind: str, label: str, source_ts: int) -> dict[str, Any]:
    now = int(time.time())
    if kind == "resolutions.completed":
        return {"status": "resolved", "expires_at": None, "reason": "completion_record"}
    if "今天" in text or "今晚" in text:
        return _expireable("time_bound", source_ts + 36 * 3600, now, "today_or_tonight")
    if "明天" in text:
        return _expireable("time_bound", source_ts + 60 * 3600, now, "tomorrow")
    if "下周" in text:
        return _expireable("time_bound", source_ts + 10 * 24 * 3600, now, "next_week")
    if _has_any(text, ("这次", "当前", "现在")) and kind not in {"communication.rules", "boundaries.active"}:
        return _expireable("session_scoped", source_ts + 7 * 24 * 3600, now, "current_context")
    if kind in {"boundaries.active", "communication.rules", "relationship.expectations"}:
        return {"status": "long_term", "expires_at": None, "reason": "stable_rule"}
    if label in {"reminder", "current_plan"}:
        return _expireable("active_until_resolved", source_ts + 30 * 24 * 3600, now, "unresolved_plan_or_reminder")
    if _has_any(text, ("以后", "一直", "长期")):
        return {"status": "long_term", "expires_at": None, "reason": "explicit_long_term"}
    return _expireable("working", source_ts + 14 * 24 * 3600, now, "default_working_state")


def _expireable(status: str, expires_at: int, now: int, reason: str) -> dict[str, Any]:
    if expires_at <= now:
        return {"status": "expired", "expires_at": expires_at, "reason": f"{reason}_expired"}
    return {"status": status, "expires_at": expires_at, "reason": reason}


def _injection_policy(kind: str, lifecycle: dict[str, Any], urgency: float, stability: float) -> str:
    if kind == "resolutions.completed":
        return "recall_only"
    if kind in {"boundaries.active", "communication.rules"}:
        return "always_inject"
    if lifecycle.get("status") in {"time_bound", "active_until_resolved"} and urgency >= 0.7:
        return "always_inject"
    if stability >= 0.75 or urgency >= 0.65:
        return "inject_when_relevant"
    return "recall_only"


def _tags(text: str) -> list[str]:
    tags = []
    candidates = {
        "time_sensitive": ("今天", "明天", "下次", "提醒", "别忘"),
        "project": ("项目", "程序", "系统", "作品", "简历", "展示"),
        "communication": ("短一点", "说教", "追问", "回复", "语气"),
        "avoidance": ("不要", "别", "避免", "不喜欢", "讨厌"),
        "goal": ("想要", "希望", "需要", "目标"),
        "relationship": ("关系", "朋友", "恋人", "陪伴", "亲密"),
        "completed": ("完成", "结束", "搞定", "做完", "展示完", "不用提醒", "不需要提醒"),
    }
    for tag, markers in candidates.items():
        if _has_any(text, markers):
            tags.append(tag)
    return tags


def _is_completion_signal(text: str) -> bool:
    return _has_any(
        text,
        (
            "已经完成",
            "完成了",
            "已经结束",
            "结束了",
            "搞定了",
            "做完了",
            "展示完",
            "已经展示",
            "不用提醒",
            "不需要提醒",
            "取消提醒",
        ),
    )


def _completion_value(text: str) -> str:
    match = re.search(r"(已经完成|完成了|已经结束|结束了|搞定了|做完了|展示完|已经展示)([^，。！？,.!?]{0,80})", text)
    return _clean(match.group(0)) if match else text[:160]


def _matching_resolution(assertion: dict[str, Any], resolutions: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not resolutions:
        return None
    assertion_time = int(assertion.get("updated_at") or 0)
    assertion_uid = str(assertion.get("source_uid") or "")
    assertion_text = str(assertion.get("text") or assertion.get("value") or "")
    assertion_tokens = _match_tokens(assertion_text)
    if not assertion_tokens:
        return None
    for resolution in sorted(resolutions, key=lambda item: (int(item.get("updated_at") or 0), str(item.get("source_uid") or "")), reverse=True):
        resolution_time = int(resolution.get("updated_at") or 0)
        resolution_uid = str(resolution.get("source_uid") or "")
        if (resolution_time, resolution_uid) < (assertion_time, assertion_uid):
            continue
        resolution_tokens = _match_tokens(str(resolution.get("text") or resolution.get("value") or ""))
        overlap = assertion_tokens & resolution_tokens
        if len(overlap) >= 2 or (overlap and _has_any(str(resolution.get("text") or ""), ("这个", "这件事", "提醒"))):
            return resolution
    return None


def _match_tokens(text: str) -> set[str]:
    raw = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", text))
    stop = {"用户", "已经", "完成", "结束", "搞定", "做完", "提醒", "需要", "准备", "明天", "今天", "这个", "这件事"}
    tokens = {item for item in raw if item not in stop}
    semantic_markers = (
        "简历",
        "项目",
        "展示",
        "记忆系统",
        "程序",
        "作品",
        "原神",
        "青柠苏打",
        "称呼",
        "主人",
        "阿月",
    )
    for marker in semantic_markers:
        if marker in text:
            tokens.add(marker)
    return tokens


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _clean(text: str) -> str:
    return " ".join(str(text or "").split())


def _float(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return 0.5
