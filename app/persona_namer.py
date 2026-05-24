from __future__ import annotations

import json
import re
from typing import Any

from .identity import scrub_identity_text
from .llm_client import call_llm_api


NAME_SYSTEM = """你是 Name Crafter，只负责为新创建的长期对话对象起一个初始名字。

你会收到用户对于交谈对象的期待、已生成的人格轮廓，以及该用户已有的人格名字。

规则：
- 名字必须体现用户这一次的期待或人格轮廓，不能从固定名称池随便抽取。
- 如果用户明确指定名字，应原样采用用户指定的名字。
- 未明确指定时，优先生成自然、易称呼、有辨识度的名字；中文名字通常为 2 到 4 个字，但用户明确需要其他语言或形式时应服从用户。
- 不要用“新生人格”“助手”“AI”“未命名”“陪伴者”等功能标签充当名字。
- 不要与已有名字重复，除非用户明确要求同名。
- 名字不应默认暗示恋人关系、性别或亲属关系，除非用户明确表达了这个期待。
- 只输出 JSON，不要输出解释文字。

JSON 格式：
{
  "name": "...",
  "reason": "一句内部命名依据"
}
"""

INVALID_AUTOMATIC_NAMES = {
    "新生人格",
    "新的对话对象",
    "未命名",
    "待命名",
    "助手",
    "助理",
    "陪伴者",
    "ai",
    "AI",
}


def craft_persona_name(
    *,
    selections: dict[str, list[str]],
    description: str,
    persona: dict[str, Any],
    user_profile: dict[str, Any] | None = None,
    existing_names: list[str] | None = None,
) -> str:
    explicit_name = _explicit_name_from_text(description)
    if explicit_name:
        return explicit_name

    used_names = {
        _clean_name(name)
        for name in existing_names or []
        if _clean_name(name)
    }
    payload = {
        "selections": selections,
        "free_description": description.strip(),
        "user_profile": {
            "nickname": (user_profile or {}).get("nickname"),
        },
        "persona_outline": {
            "summary": persona.get("summary", ""),
            "traits": persona.get("traits", []),
            "relationship": persona.get("relationship", ""),
            "speaking_style": persona.get("speaking_style", ""),
            "appearance_description": persona.get("appearance_description", ""),
            "desired_image": persona.get("desired_image", ""),
        },
        "existing_names_to_avoid": sorted(used_names),
        "rejected_candidates": [],
    }

    for _ in range(2):
        try:
            raw = call_llm_api(
                [
                    {"role": "system", "content": NAME_SYSTEM},
                    {
                        "role": "user",
                        "content": "请为这个新对象生成初始名字：\n"
                        + json.dumps(payload, ensure_ascii=False, indent=2),
                    },
                ],
                task="namer",
            )
        except Exception as exc:
            print("[NameCrafter] LLM name generation skipped:", exc)
            break

        candidate = _candidate_from_response(raw)
        if candidate and candidate not in INVALID_AUTOMATIC_NAMES and candidate not in used_names:
            return candidate
        if candidate:
            payload["rejected_candidates"].append(candidate)

    return "未命名"


def _candidate_from_response(text: str) -> str:
    text = str(text or "").strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return _clean_name(obj.get("name", ""))
    except Exception:
        pass

    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                return _clean_name(obj.get("name", ""))
        except Exception:
            pass
    return _clean_name(text)


def _explicit_name_from_text(description: str) -> str:
    text = str(description or "").strip()
    patterns = (
        r"(?:名字(?:叫|是|用)?|取名为|命名为|称为)\s*[:：]?\s*[“\"'「『]?([^”\"'」』，。,；;！!\s]{1,40})",
        r"(?:就叫|叫她|叫他|叫它|叫TA|叫ta|她叫|他叫|它叫|TA叫|ta叫)\s*[:：]?\s*[“\"'「『]?([^”\"'」』，。,；;！!\s]{1,40})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            candidate = _clean_name(match.group(1))
            if candidate:
                return candidate
    return ""


def _clean_name(value: Any) -> str:
    name = scrub_identity_text(str(value or "").strip())
    name = re.sub(r"^(?:名字|name)\s*[:：]\s*", "", name, flags=re.I)
    name = name.strip(" \t\r\n“”\"'「」『』《》<>")
    name = re.split(r"[\r\n，。,；;！!?？]", name, maxsplit=1)[0].strip()
    return name[:40]
