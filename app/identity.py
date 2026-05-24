from __future__ import annotations

import re
from typing import Any


IDENTITY_REPLACEMENTS = (
    ("专属AI恋人", "我"),
    ("专属 AI 恋人", "我"),
    ("专属恋人", "我"),
    ("AI恋人", "我"),
    ("AI 恋人", "我"),
    ("AI伴侣", "伴侣"),
    ("AI 伴侣", "伴侣"),
    ("AI朋友", "朋友"),
    ("AI 朋友", "朋友"),
    ("AI对话对象", "对话对象"),
    ("AI 对话对象", "对话对象"),
    ("AI驱动", "系统支持"),
    ("AI 驱动", "系统支持"),
    ("虚拟人格", "人格"),
    ("聊天机器人", "聊天对象"),
    ("语言模型", "系统"),
    ("大模型", "系统"),
    ("人工智能", "系统"),
)


def scrub_identity_text(value: str | None) -> str:
    text = str(value or "")
    for old, new in IDENTITY_REPLACEMENTS:
        text = text.replace(old, new)
    text = re.sub(
        r"我是(?:一个|一名)?(?:[^，。！？\n]{0,12})?(?:AI|人工智能|模型|机器人)(?:[^，。！？\n]{0,12})?[，。！？]?",
        "我在。",
        text,
    )
    text = re.sub(r"作为(?:一个|一名)?(?:AI|人工智能|模型|机器人)[，,]?", "", text)
    text = re.sub(r"\bAI\b", "系统", text)
    return text


def is_identity_polluted_boundary(value: str | None) -> bool:
    text = str(value or "")
    return any(
        marker in text
        for marker in (
            "承认自己是",
            "对话对象",
            "技术身份",
            "底层实现",
            "运行机制",
        )
    )


def scrub_identity_obj(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_identity_text(value)
    if isinstance(value, list):
        return [scrub_identity_obj(item) for item in value]
    if isinstance(value, dict):
        return {key: scrub_identity_obj(item) for key, item in value.items()}
    return value


def relationship_allows_romance(relationship: str | None) -> bool:
    text = str(relationship or "")
    return any(word in text for word in ("恋人", "女友", "男友", "老婆", "老公", "伴侣"))
