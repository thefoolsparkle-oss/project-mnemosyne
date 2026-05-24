from __future__ import annotations

import json
from pathlib import Path
import time

from .emotion import EmotionState


class EmotionStore:
    def __init__(self, persona_name: str):
        self.base_dir = Path(__file__).resolve().parent.parent / "data" / "emotion"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / f"{persona_name}.json"

    def load(self) -> EmotionState:
        if not self.path.exists():
            return EmotionState()

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            state = EmotionState.from_dict(data)
            self._recover_after_rest(state)
            return state
        except Exception as exc:
            print("[EmotionStore] load failed:", exc)
            return EmotionState()

    def save(self, state: EmotionState) -> None:
        self.path.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _recover_after_rest(state: EmotionState) -> None:
        last_updated = state.last_updated or int(time.time())
        hours = max(0, (time.time() - last_updated) / 3600)

        if hours < 6:
            return

        if state.mood in {"guarded", "concerned"}:
            state.mood = "neutral"
        state.energy = min(100, state.energy + int(hours * 4))
        state.clamp()
