from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List


class LongTermMemory:
    def __init__(self, persona_name: str, base_dir: Path):
        self.persona_name = persona_name
        self.base_dir = base_dir
        self.path = base_dir / f"{persona_name}.json"
        self.data: Dict[str, Any] = {
            "persona": persona_name,
            "created_at": int(time.time()),
            "items": [],
        }
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return

        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("memory file root must be an object")
            data.setdefault("persona", self.persona_name)
            data.setdefault("created_at", int(time.time()))
            data.setdefault("items", [])
            if not isinstance(data["items"], list):
                data["items"] = []
            self.data = data
        except Exception as exc:
            backup = self.path.with_suffix(f".broken-{int(time.time())}.json")
            try:
                self.path.replace(backup)
            except Exception:
                pass
            print("[LongTermMemory] reset broken memory file:", exc)

    def save(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add(self, text: str, tags: List[str], meta: dict | None = None) -> None:
        text = text.strip()
        if not text:
            return

        normalized_tags = sorted({str(tag).strip() for tag in tags if str(tag).strip()})
        meta = meta or {}

        for item in self.data["items"]:
            if item.get("text") != text:
                continue

            old_importance = float(item.get("meta", {}).get("importance", 0) or 0)
            new_importance = float(meta.get("importance", 0) or 0)
            if new_importance > old_importance:
                item["meta"] = meta
                item["tags"] = normalized_tags
                item["archived"] = False
                item["ts"] = int(time.time())
                self.save()
            return

        self.data["items"].append(
            {
                "text": text,
                "tags": normalized_tags,
                "ts": int(time.time()),
                "meta": meta,
                "archived": False,
            }
        )
        self.save()

    def search(self, query: str, limit: int = 6) -> List[Dict[str, Any]]:
        q = query.strip()
        if not q:
            return []

        keywords = self._keywords(q)
        scored: list[tuple[float, Dict[str, Any]]] = []

        for item in self.active_items():
            text = str(item.get("text", ""))
            score = self._score(text, q, keywords)
            if score <= 0:
                continue

            importance = float(item.get("meta", {}).get("importance", 0) or 0)
            scored.append((score + importance, item))

        scored.sort(key=lambda pair: (-pair[0], -pair[1].get("ts", 0)))
        return [item for _, item in scored[:limit]]

    def recent(self, limit: int = 6) -> List[Dict[str, Any]]:
        items = self.active_items()
        items.sort(key=lambda item: item.get("ts", 0), reverse=True)
        return items[:limit]

    def active_items(self) -> List[Dict[str, Any]]:
        return [item for item in self.data.get("items", []) if not item.get("archived")]

    def archive_items(self, items: Iterable[Dict[str, Any]]) -> None:
        item_ids = {id(item) for item in items}
        for item in self.data.get("items", []):
            if id(item) in item_ids:
                item["archived"] = True
        self.save()

    @staticmethod
    def _keywords(query: str) -> list[str]:
        words = re.findall(r"[\w\u4e00-\u9fff]{2,}", query.lower())
        return words or [query.lower()]

    @staticmethod
    def _score(text: str, query: str, keywords: list[str]) -> float:
        text_lower = text.lower()
        query_lower = query.lower()

        if query_lower in text_lower:
            return 3

        score = sum(1 for keyword in keywords if keyword in text_lower)
        if score:
            return float(score)

        shared_chars = set(query_lower) & set(text_lower)
        meaningful = {ch for ch in shared_chars if ch.strip() and ch not in "，。！？,.!?的了呢啊"}
        return min(len(meaningful) / 6, 1.5)
