from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from .llm_client import call_llm_api


ALLOWED_TYPES = {"identity", "preference", "plan", "relationship", "constraint"}

MEMORY_SYSTEM = """你是长期记忆提取器。

任务：
- 只从用户刚才说的话里提取未来仍然有用、相对稳定的信息。
- 不记录临时闲聊、短暂情绪、无关细节。
- 不编造用户没有说过的信息。
- 不记录敏感隐私：证件号、精确住址、银行卡、密码、精确定位等。

允许的 type：
- identity：名字、昵称、称呼偏好、基本身份偏好
- preference：长期兴趣、喜欢、讨厌、常玩的类型
- plan：明确的长期计划或目标
- relationship：关系定义、称呼边界
- constraint：硬约束，例如不要叫某个称呼、不要提某个话题

importance 取值 0~1：
- 0.9~1.0：名字、固定称呼、强约束
- 0.6~0.8：长期偏好、长期计划
- 0.3~0.5：不确定是否稳定的一般信息

只输出 JSON，格式必须是：
{"memories":[{"text":"...","type":"...","importance":0.0}]}

没有值得记录的内容时输出：
{"memories":[]}
"""

MEMORY_USER_TEMPLATE = """用户刚才说：
<<<
{user_text}
>>>
请输出 JSON。"""


def _safe_extract_json(text: str) -> Dict[str, Any]:
    text = text.strip()

    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass

    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    return {"memories": []}


def extract_memories_with_llm(user_text: str) -> List[Dict[str, Any]]:
    messages = [
        {"role": "system", "content": MEMORY_SYSTEM},
        {"role": "user", "content": MEMORY_USER_TEMPLATE.format(user_text=user_text.strip())},
    ]

    raw = call_llm_api(messages, task="memory")
    obj = _safe_extract_json(raw)
    memories = obj.get("memories", [])
    if not isinstance(memories, list):
        return []

    cleaned: List[Dict[str, Any]] = []
    for item in memories:
        if not isinstance(item, dict):
            continue

        text = str(item.get("text", "")).strip()
        memory_type = str(item.get("type", "")).strip()
        try:
            importance = float(item.get("importance", 0))
        except Exception:
            importance = 0.0

        if not text or memory_type not in ALLOWED_TYPES:
            continue

        cleaned.append(
            {
                "text": text,
                "type": memory_type,
                "importance": max(0.0, min(1.0, importance)),
            }
        )

    return cleaned
