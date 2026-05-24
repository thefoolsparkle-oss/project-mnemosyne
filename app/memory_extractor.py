from __future__ import annotations

import re
from typing import List, Tuple


def extract_memories(user_text: str) -> List[Tuple[str, List[str]]]:
    """Lightweight fallback extractor kept for offline/debug use."""
    text = user_text.strip()
    if not text:
        return []

    out: List[Tuple[str, List[str]]] = []

    name_match = re.search(r"(?:我叫|叫我|你可以叫我)\s*([^\s，。！？,.!?]{1,20})", text)
    if name_match:
        out.append((f"用户希望被称为{name_match.group(1)}", ["identity"]))

    like_match = re.search(r"我(?:最)?喜欢\s*([^\n\r，。！？,.!?]{1,30})", text)
    if like_match:
        out.append((f"用户喜欢{like_match.group(1).strip()}", ["preference"]))

    dislike_match = re.search(r"我讨厌\s*([^\n\r，。！？,.!?]{1,30})", text)
    if dislike_match:
        out.append((f"用户讨厌{dislike_match.group(1).strip()}", ["preference"]))

    if any(keyword in text for keyword in ["考试", "作业", "项目", "deadline", "due"]):
        out.append((f"用户提到近期安排：{text}", ["plan"]))

    constraint_match = re.search(r"(?:不要|别)\s*叫我\s*([^\s，。！？,.!?]{1,20})", text)
    if constraint_match:
        out.append((f"不要称呼用户为{constraint_match.group(1)}", ["constraint"]))

    return out
