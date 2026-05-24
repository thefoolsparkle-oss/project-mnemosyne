from __future__ import annotations

import re
from typing import Any

from .config import load_config


MODES = {"eco", "balanced", "deep"}


def memory_mode() -> str:
    memory = load_config().get("memory", {})
    mode = str(memory.get("mode") or "balanced").lower().strip()
    return mode if mode in MODES else "balanced"


def message_signal(text: str) -> dict[str, Any]:
    text = str(text or "").strip()
    markers = {
        "identity": ("叫我", "称呼", "我叫", "名字", "nickname"),
        "preference": ("喜欢", "讨厌", "不喜欢", "爱喝", "爱吃", "感兴趣"),
        "plan": ("计划", "明天", "今天", "下周", "准备", "提醒", "别忘", "目标"),
        "boundary": ("不要", "别", "避免", "不许", "不能", "不想聊"),
        "feedback": ("短一点", "别说教", "不要说教", "少追问", "回复", "语气"),
        "relationship": ("关系", "朋友", "恋人", "陪伴", "亲密", "距离感"),
        "resolution": ("完成了", "已经完成", "展示完", "不用提醒", "取消提醒", "搞定了"),
    }
    hits = {name: [marker for marker in values if marker in text] for name, values in markers.items()}
    hits = {name: values for name, values in hits.items() if values}
    informational = bool(hits) or len(re.findall(r"[\w\u4e00-\u9fff]{2,}", text)) >= 8
    strong = bool(hits)
    return {
        "informational": informational,
        "strong": strong,
        "categories": sorted(hits.keys()),
        "markers": hits,
        "length": len(text),
    }


def should_use_llm_for_extraction(text: str) -> bool:
    mode = memory_mode()
    signal = message_signal(text)
    if mode == "eco":
        return False
    if mode == "balanced":
        return signal["strong"] and signal["length"] >= 12
    return signal["informational"]


def should_use_llm_for_mirror(text: str, stored_memories: list[dict]) -> bool:
    mode = memory_mode()
    signal = message_signal(text)
    if mode == "eco":
        return False
    if mode == "balanced":
        return bool(stored_memories) and signal["strong"] and signal["length"] >= 16
    return signal["informational"] or bool(stored_memories)


def should_use_llm_for_judge(memory_count: int) -> bool:
    mode = memory_mode()
    if mode == "eco":
        return False
    if mode == "balanced":
        return False
    return memory_count > 0


def should_use_semantic_recall() -> bool:
    return memory_mode() == "deep"


def should_refresh_summary(message_count: int) -> bool:
    mode = memory_mode()
    if mode == "eco":
        return message_count >= 10
    if mode == "balanced":
        return message_count >= 6
    return message_count >= 2


def policy_snapshot() -> dict[str, Any]:
    mode = memory_mode()
    return {
        "mode": mode,
        "llm_extraction": mode in {"balanced", "deep"},
        "llm_mirror": mode in {"balanced", "deep"},
        "llm_judge": mode == "deep",
        "semantic_recall": mode == "deep",
        "summary_frequency": {"eco": "every ~10 messages", "balanced": "every ~6 messages", "deep": "every ~2 messages"}[mode],
        "state_curator": "always rule-first",
    }
