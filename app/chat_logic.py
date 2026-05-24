from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from .config import load_config
from .emotion import update_emotion
from .emotion_store import EmotionStore
from .llm_client import call_llm_api
from .long_memory import LongTermMemory
from .memory import ConversationMemory
from .memory_consolidator import consolidate_memories
from .memory_llm_extractor import extract_memories_with_llm
from .persona_loader import Persona, load_active_persona


Message = Dict[str, str]


class ChatBot:
    def __init__(self):
        self.config = load_config()
        self.persona: Persona = load_active_persona()

        memory_config = self.config.get("memory", {})
        short_term_turns = int(memory_config.get("short_term_turns", 8))
        self.memory_extract_every = int(memory_config.get("extract_every", 2))
        self.consolidate_every = int(memory_config.get("consolidate_every", 10))

        self.memory = ConversationMemory(max_turns=short_term_turns)
        base_dir = Path(__file__).resolve().parent.parent / "data" / "memories"
        self.long_memory = LongTermMemory(persona_name=self.persona.name, base_dir=base_dir)

        self.turn_count = 0
        self.emotion_store = EmotionStore(self.persona.name)
        self.emotion = self.emotion_store.load()

    def build_messages(self, user_input: str) -> List[Message]:
        messages: List[Message] = [
            {"role": "system", "content": self.persona.prompt},
            {"role": "system", "content": self._state_prompt()},
        ]

        if self.persona.profile:
            messages.append(
                {
                    "role": "system",
                    "content": f"【角色结构化信息】\n{self.persona.profile}",
                }
            )

        memories = self._select_memories(user_input)
        if memories:
            messages.extend(self._memory_messages(memories))

        messages.extend(self.memory.get_history())
        messages.append({"role": "user", "content": user_input})
        return messages

    def chat(self, user_input: str) -> str:
        user_input = user_input.strip()
        if not user_input:
            return "我在。你可以慢慢说。"

        reply = call_llm_api(self.build_messages(user_input), task="chat")
        self._after_reply(user_input, reply)
        return reply

    def snapshot(self) -> dict:
        return {
            "persona": {
                "id": self.persona.name,
                "name": self.persona.display_name,
            },
            "emotion": self.emotion.to_dict(),
            "memory_count": len(self.long_memory.active_items()),
            "turn_count": self.turn_count,
        }

    def _state_prompt(self) -> str:
        return (
            "【当前内部状态】\n"
            f"- 情绪: {self.emotion.mood}\n"
            f"- 亲密度: {self.emotion.affinity}/100\n"
            f"- 信任度: {self.emotion.trust}/100\n"
            f"- 精力: {self.emotion.energy}/100\n\n"
            "这些状态只作为语气参考，不要直接向用户汇报数值。"
        )

    def _select_memories(self, user_input: str) -> list[dict]:
        hits = self.long_memory.search(user_input, limit=8)
        if hits:
            return hits
        return self.long_memory.recent(limit=6)

    @staticmethod
    def _memory_messages(memories: list[dict]) -> list[Message]:
        identity = [
            item
            for item in memories
            if {"identity", "constraint", "relationship"} & set(item.get("tags", []))
        ]
        other = [item for item in memories if item not in identity]

        messages: list[Message] = []
        if identity:
            messages.append(
                {
                    "role": "system",
                    "content": "【必须遵守的用户信息】\n"
                    + "\n".join(f"- {item['text']}" for item in identity),
                }
            )

        if other:
            messages.append(
                {
                    "role": "system",
                    "content": "【可参考的长期记忆】\n"
                    + "\n".join(f"- {item['text']}" for item in other),
                }
            )

        return messages

    def _after_reply(self, user_input: str, reply: str) -> None:
        update_emotion(self.emotion, user_input)
        self.emotion_store.save(self.emotion)

        self.memory.add("user", user_input)
        self.memory.add("assistant", reply)

        self.turn_count += 1
        if self._should_extract_memory(user_input):
            self._extract_memory(user_input)

        if self.consolidate_every > 0 and self.turn_count % self.consolidate_every == 0:
            self._consolidate_memory()

    def _should_extract_memory(self, user_input: str) -> bool:
        hotwords = [
            "我叫",
            "叫我",
            "你可以叫我",
            "我喜欢",
            "我讨厌",
            "记住",
            "别叫",
            "不要叫",
            "我们的关系",
        ]
        return (
            self.memory_extract_every > 0
            and self.turn_count % self.memory_extract_every == 0
        ) or any(word in user_input for word in hotwords)

    def _extract_memory(self, user_input: str) -> None:
        try:
            memories = extract_memories_with_llm(user_input)
        except Exception as exc:
            print("[ChatBot] memory extraction skipped:", exc)
            return

        for memory in memories:
            if memory["importance"] < 0.5:
                continue
            self.long_memory.add(
                text=memory["text"],
                tags=[memory["type"]],
                meta={"importance": memory["importance"]},
            )

    def _consolidate_memory(self) -> None:
        try:
            candidates = [
                item
                for item in self.long_memory.active_items()
                if float(item.get("meta", {}).get("importance", 0) or 0) >= 0.6
                and "summary" not in item.get("tags", [])
            ]

            groups: dict[tuple[str, ...], list[dict]] = {}
            for item in candidates:
                key = tuple(item.get("tags", []))
                groups.setdefault(key, []).append(item)

            for group_items in groups.values():
                if len(group_items) < 3:
                    continue

                summary = consolidate_memories(group_items)
                if not summary:
                    continue

                self.long_memory.add(
                    text=summary["text"],
                    tags=["summary"],
                    meta={"importance": summary["importance"]},
                )
                self.long_memory.archive_items(group_items)
        except Exception as exc:
            print("[ChatBot] memory consolidation skipped:", exc)
