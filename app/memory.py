from __future__ import annotations

from collections import deque
from typing import Deque, Dict, List


class ConversationMemory:
    def __init__(self, max_turns: int = 8):
        self.history: Deque[Dict[str, str]] = deque(maxlen=max_turns * 2)

    def add(self, role: str, content: str) -> None:
        content = content.strip()
        if content:
            self.history.append({"role": role, "content": content})

    def get_history(self) -> List[Dict[str, str]]:
        return list(self.history)
