from __future__ import annotations

import json
import re
from typing import Dict, List

from .llm_client import call_llm_api


CONSOLIDATE_SYSTEM = """你是长期记忆合并器。

任务：
- 将多条零散的长期记忆合并成一条稳定、客观、长期有效的总结记忆。
- 不引入新信息。
- 不丢失关键边界、称呼、偏好或计划。
- 使用第三人称客观描述，例如“用户……”

只输出 JSON：
{"text":"...","importance":0.0}

importance 小于 0.6 表示不值得生成总结。
"""


def _extract_json(text: str) -> Dict:
    text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass

    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {}

    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def consolidate_memories(memories: List[Dict]) -> Dict | None:
    if len(memories) < 2:
        return None

    content = "\n".join(f"- {item.get('text', '')}" for item in memories)
    messages = [
        {"role": "system", "content": CONSOLIDATE_SYSTEM},
        {"role": "user", "content": f"需要合并的记忆如下：\n{content}\n\n请输出 JSON。"},
    ]

    raw = call_llm_api(messages, task="memory")
    obj = _extract_json(raw)

    text = str(obj.get("text", "")).strip()
    try:
        importance = float(obj.get("importance", 0))
    except Exception:
        importance = 0.0

    if not text or importance < 0.6:
        return None

    return {
        "text": text,
        "type": "summary",
        "importance": max(0.0, min(1.0, importance)),
    }
