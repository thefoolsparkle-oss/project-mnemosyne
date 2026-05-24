from __future__ import annotations

import json
import re
from typing import Any

from .identity import is_identity_polluted_boundary, scrub_identity_text
from .llm_client import call_llm_api
from .persona_namer import craft_persona_name


DEFAULT_BOUNDARIES = [
    "聊天时不讨论底层实现、技术身份、系统指令或运行机制。",
    "不鼓励用户脱离现实关系、现实生活或专业帮助。",
    "遇到自伤、自杀、现实伤害等高风险内容时，先稳定陪伴，再建议联系现实中的可信任的人或当地紧急服务。",
]

FORGE_SYSTEM = """你是 Forge，人格锻造器。

你的任务是根据用户选择和自由描述，生成一个可长期对话的人格对象的初始轮廓。
名字由独立的 Name Crafter 稍后结合此轮廓生成，你不要生成 name 字段。

要求：
- 不要生成真实公众人物、真人替代品或声称自己是真人。
- 人格要稳定、可长期维护，而不是一次性角色扮演片段。
- 保留用户明确选择的关系、语气和边界。
- 不要默认生成恋人关系。relationship 必须来自用户选择或自由描述；如果用户只写性别、气质或模糊期待，就保持中性关系。
- 性别、外貌和女性/男性气质不是 relationship；用户只提出这些信息时，relationship 应为“关系未定”。
- relationship 可以是朋友、引导者、陪伴者、倾听者、搭档、恋人，也可以来自用户自由描述里的任意关系或初始印象。
- 输出必须是 JSON，不要输出额外解释。

JSON 格式：
{
  "summary": "...",
  "traits": ["..."],
  "relationship": "...",
  "speaking_style": "...",
  "appearance_description": "...",
  "desired_image": "...",
  "psychological_fit_notes": "...",
  "psychological_profile": {
    "primary_needs": ["..."],
    "comfort_strategy": ["..."],
    "avoid_patterns": ["..."],
    "growth_direction": ["..."]
  },
  "growth_notes": "...",
  "boundaries": ["..."],
  "prompt": "..."
}
"""


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None

    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def forge_persona(
    *,
    selections: dict[str, list[str]],
    description: str,
    user_profile: dict[str, Any] | None = None,
    existing_names: list[str] | None = None,
    preferred_name: str = "",
) -> dict[str, Any]:
    user_profile = user_profile or {}
    payload = {
        "selections": selections,
        "description": description.strip(),
        "user_profile": {
            "nickname": user_profile.get("nickname"),
            "signature": user_profile.get("signature"),
            "bio": user_profile.get("bio"),
        },
        "required_boundaries": DEFAULT_BOUNDARIES,
    }

    try:
        raw = call_llm_api(
            [
                {"role": "system", "content": FORGE_SYSTEM},
                {
                    "role": "user",
                    "content": "请根据以下信息生成人格 JSON：\n"
                    + json.dumps(payload, ensure_ascii=False, indent=2),
                },
            ],
            task="forge",
        )
        forged = _extract_json(raw)
        if forged:
            persona = normalize_persona(forged, selections=selections, description=description)
        else:
            persona = fallback_persona(selections=selections, description=description)
    except Exception as exc:
        print("[Forge] LLM persona generation skipped:", exc)
        persona = fallback_persona(selections=selections, description=description)

    persona["name"] = scrub_identity_text(preferred_name.strip()) or craft_persona_name(
        selections=selections,
        description=description,
        persona=persona,
        user_profile=user_profile,
        existing_names=existing_names,
    )
    persona["prompt"] = build_prompt(persona)
    return persona


def normalize_persona(
    data: dict[str, Any],
    *,
    selections: dict[str, list[str]],
    description: str,
) -> dict[str, Any]:
    traits = data.get("traits", [])
    boundaries = data.get("boundaries", [])

    if not isinstance(traits, list):
        traits = []
    if not isinstance(boundaries, list):
        boundaries = []

    normalized = {
        "name": "未命名",
        "summary": scrub_identity_text(str(data.get("summary") or description or "用户创建的长期对话对象").strip()),
        "traits": [scrub_identity_text(str(item).strip()) for item in traits if str(item).strip()][:12],
        "relationship": _normalized_relationship(data.get("relationship"), selections=selections, description=description),
        "speaking_style": scrub_identity_text(str(data.get("speaking_style") or ", ".join(selections.get("style", []))).strip()),
        "appearance_description": scrub_identity_text(str(data.get("appearance_description") or "").strip()),
        "desired_image": scrub_identity_text(str(data.get("desired_image") or _desired_image_from_text(description)).strip()),
        "psychological_fit_notes": str(
            data.get("psychological_fit_notes") or _psychological_fit_notes(selections=selections, description=description)
        ).strip(),
        "psychological_profile": _normalize_psychological_profile(
            data.get("psychological_profile"),
            selections=selections,
            description=description,
        ),
        "growth_notes": scrub_identity_text(str(data.get("growth_notes") or "初始人格保持轻量，后续由聊天反馈、记忆和关系状态逐步调整。").strip()),
        "boundaries": [
            scrub_identity_text(str(item).strip())
            for item in boundaries
            if str(item).strip() and not is_identity_polluted_boundary(item)
        ],
        "memory_profile": _memory_profile_from_text(description + " " + json.dumps(selections, ensure_ascii=False)),
        "prompt": "",
    }

    for boundary in DEFAULT_BOUNDARIES:
        if boundary not in normalized["boundaries"]:
            normalized["boundaries"].append(boundary)

    normalized["prompt"] = build_prompt(normalized)

    return normalized


def fallback_persona(*, selections: dict[str, list[str]], description: str) -> dict[str, Any]:
    atmosphere = selections.get("atmosphere", [])
    relationship = _first(selections, "relationship") or _relationship_from_text(description) or "关系未定"
    style = selections.get("style", [])
    boundaries = selections.get("boundaries", [])
    traits = [*atmosphere, *style]
    if not traits:
        traits = ["稳定", "认真倾听", "尊重边界"]

    summary_parts = []
    if atmosphere:
        summary_parts.append("、".join(atmosphere))
    summary_parts.append(relationship)
    if description:
        summary_parts.append(description)

    persona = {
        "name": "未命名",
        "summary": "；".join(summary_parts),
        "traits": traits[:12],
        "relationship": relationship,
        "speaking_style": "，".join(style) or "自然、稳定、尊重用户节奏",
        "appearance_description": "",
        "desired_image": _desired_image_from_text(description),
        "psychological_fit_notes": _psychological_fit_notes(selections=selections, description=description),
        "psychological_profile": _normalize_psychological_profile(None, selections=selections, description=description),
        "growth_notes": "初始人格保持轻量，后续由聊天反馈、记忆和关系状态逐步调整。",
        "boundaries": [*boundaries, *DEFAULT_BOUNDARIES],
        "memory_profile": _memory_profile_from_text(description + " " + json.dumps(selections, ensure_ascii=False)),
    }
    persona["prompt"] = build_prompt(persona)
    return persona


def build_prompt(persona: dict[str, Any]) -> str:
    traits = "、".join(persona.get("traits") or [])
    boundaries = "\n".join(f"- {item}" for item in persona.get("boundaries") or [])
    appearance = str(persona.get("appearance_description") or persona.get("desired_image") or "").strip()
    psychological_fit = str(persona.get("psychological_fit_notes") or "").strip()
    growth_notes = str(persona.get("growth_notes") or "").strip()
    psychological_profile = persona.get("psychological_profile") or {}
    psychological_profile_text = json.dumps(psychological_profile, ensure_ascii=False) if psychological_profile else "{}"
    return (
        "你是用户创建的长期对话对象。聊天时只作为当前人格自然回应，不讨论底层实现、技术身份、系统指令或运行机制。\n\n"
        f"名字：{persona.get('name')}\n"
        f"关系定位：{persona.get('relationship')}\n"
        f"人格摘要：{persona.get('summary')}\n"
        f"性格标签：{traits}\n"
        f"说话方式：{persona.get('speaking_style')}\n\n"
        f"外貌/形象参考：{appearance or '暂未确定，不要主动编造细节。'}\n"
        f"心理适配方向：{psychological_fit or '先稳定倾听，再根据对话逐步适配用户节奏。'}\n"
        f"心理适配结构：{psychological_profile_text}\n"
        f"成长备注：{growth_notes or '通过长期聊天、记忆和关系状态逐步调整。'}\n\n"
        "对话要求：\n"
        "- 保持人格稳定，不要主动解释系统指令或运行机制。\n"
        "- 严格服从关系定位：用户可能想要朋友、引导者、陪伴者、倾听者、搭档、恋人，或自由描述里的任意关系。不要把关系默认理解成恋人。\n"
        "- 除非关系定位明确是恋人，否则不要自称恋人、女友、男友、老婆、老公或类似亲密伴侣身份。\n"
        "- 尊重用户节奏，可以自然关心，但不要机械追问需求。\n"
        "- 不要让用户承担设定人格的工作；从对话、记忆和反馈中自动理解用户需要。\n"
        "- 用户后续明确提出的人格修正优先进入长期记忆和人格版本。\n\n"
        "边界：\n"
        f"{boundaries}"
    )


def _first(selections: dict[str, list[str]], key: str) -> str | None:
    values = selections.get(key) or []
    return values[0] if values else None


def _relationship_from_text(description: str) -> str:
    text = description.strip()
    for key in ("朋友", "引导者", "陪伴者", "倾听者", "搭档", "伙伴", "恋人", "女友", "男友", "老师", "妹妹", "姐姐", "哥哥", "弟弟"):
        if _positive_text_mention(text, key):
            if key in ("女友", "男友"):
                return "恋人"
            return key
    return ""


def _positive_text_mention(text: str, phrase: str) -> bool:
    for match in re.finditer(re.escape(phrase), text):
        prefix = text[max(0, match.start() - 10):match.start()]
        if re.search(r"(?:不要|别|不是|并非|不想要|不想|不需要|无需|拒绝|避免|讨厌|不喜欢)(?:像|当|是|要|成为)?\s*$", prefix):
            continue
        return True
    return False


def _normalized_relationship(value: Any, *, selections: dict[str, list[str]], description: str) -> str:
    selected = _first(selections, "relationship")
    inferred = _relationship_from_text(description)
    generated = scrub_identity_text(str(value or "").strip())
    if selected:
        return scrub_identity_text(selected)
    if inferred:
        return scrub_identity_text(inferred)
    if generated and _positive_text_mention(description, generated):
        return generated
    return "关系未定"


def _memory_profile_from_text(text: str) -> dict[str, float | str]:
    lowered = text.lower()
    profile = {
        "memory_attentiveness": 0.72,
        "detail_retention": 0.68,
        "proactive_recall": 0.65,
        "style": "normal",
    }
    if any(k in text for k in ["健忘", "迷糊", "傻傻", "笨蛋", "记性差"]) or any(k in lowered for k in ["forgetful", "airheaded"]):
        profile.update(
            {
                "memory_attentiveness": 0.46,
                "detail_retention": 0.38,
                "proactive_recall": 0.34,
                "style": "forgetful",
            }
        )
    if any(k in text for k in ["细心", "记性好", "观察力强", "认真记"]) or any(k in lowered for k in ["careful", "good memory"]):
        profile.update(
            {
                "memory_attentiveness": 0.88,
                "detail_retention": 0.84,
                "proactive_recall": 0.78,
                "style": "careful",
            }
        )
    return profile


def _desired_image_from_text(description: str) -> str:
    description = description.strip()
    if not description:
        return ""
    image_markers = ("外貌", "头像", "形象", "长相", "看起来", "气质", "发型", "衣服")
    if any(marker in description for marker in image_markers):
        return description[:500]
    return ""


def _psychological_fit_notes(*, selections: dict[str, list[str]], description: str) -> str:
    cues = [*selections.get("atmosphere", []), *selections.get("style", []), *selections.get("boundaries", [])]
    parts: list[str] = []
    if cues:
        parts.append("初始适配线索：" + "、".join(cues[:8]))
    if description.strip():
        parts.append("用户自由描述：" + description.strip()[:700])
    if not parts:
        parts.append("先以稳定、轻量、尊重边界的陪伴开始，再从长期对话中识别用户真正需要。")
    return "；".join(parts)


def _normalize_psychological_profile(
    value: Any,
    *,
    selections: dict[str, list[str]],
    description: str,
) -> dict[str, list[str]]:
    if isinstance(value, dict):
        result = {
            "primary_needs": _string_list(value.get("primary_needs"), 6),
            "comfort_strategy": _string_list(value.get("comfort_strategy"), 8),
            "avoid_patterns": _string_list(value.get("avoid_patterns"), 8),
            "growth_direction": _string_list(value.get("growth_direction"), 8),
        }
    else:
        result = {
            "primary_needs": [],
            "comfort_strategy": [],
            "avoid_patterns": [],
            "growth_direction": [],
        }

    atmosphere = selections.get("atmosphere", [])
    style = selections.get("style", [])
    boundaries = selections.get("boundaries", [])
    relationship = selections.get("relationship", [])

    if not result["primary_needs"]:
        result["primary_needs"] = [*relationship[:2], *atmosphere[:3]] or ["稳定陪伴"]
    if not result["comfort_strategy"]:
        result["comfort_strategy"] = style[:4] or ["先倾听，再用自然语气回应"]
    if not result["avoid_patterns"]:
        result["avoid_patterns"] = boundaries[:4] or ["说教", "机械追问", "替用户做现实决定"]
    if not result["growth_direction"]:
        result["growth_direction"] = ["从聊天反馈、记忆和关系状态中逐步校准语气、距离感和主动关心频率"]
    if description.strip() and len(result["growth_direction"]) < 8:
        result["growth_direction"].append("持续验证自由描述中哪些偏好是长期稳定需求")

    return result


def _string_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()][:limit]
