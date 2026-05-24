from __future__ import annotations

from dataclasses import asdict, dataclass
import time


@dataclass
class EmotionState:
    mood: str = "neutral"
    affinity: int = 0
    trust: int = 0
    energy: int = 70
    last_updated: int = 0

    def clamp(self) -> None:
        self.affinity = max(0, min(100, self.affinity))
        self.trust = max(0, min(100, self.trust))
        self.energy = max(0, min(100, self.energy))

    def to_dict(self) -> dict:
        data = asdict(self)
        data["last_updated"] = int(time.time())
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "EmotionState":
        return cls(
            mood=str(data.get("mood", "neutral")),
            affinity=int(data.get("affinity", 0)),
            trust=int(data.get("trust", 0)),
            energy=int(data.get("energy", 70)),
            last_updated=int(data.get("last_updated", 0)),
        )


def update_emotion(state: EmotionState, user_text: str) -> None:
    text = user_text.lower()

    positive = ["喜欢你", "谢谢", "开心", "陪我", "在吗", "想你", "辛苦了"]
    distant = ["算了", "没事", "随便", "不用了"]
    negative = ["烦", "别吵", "走开", "闭嘴", "讨厌"]
    vulnerable = ["难过", "害怕", "焦虑", "睡不着", "撑不住"]

    if any(k in text for k in positive):
        state.affinity += 5
        state.trust += 3
        state.mood = "warm"

    if any(k in text for k in vulnerable):
        state.affinity += 2
        state.trust += 4
        state.mood = "concerned"

    if any(k in text for k in distant):
        state.affinity -= 2
        state.mood = "neutral"

    if any(k in text for k in negative):
        state.affinity -= 6
        state.trust -= 4
        state.mood = "guarded"

    if len(text) > 80:
        state.energy -= 2

    state.clamp()
