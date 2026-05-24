from __future__ import annotations

import hashlib
import json
import math
import os
from typing import Any

import requests

from .config import load_config
from .database import dict_from_row, get_db, now_ts


DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


def sync_memory_embeddings(user_id: int, persona_id: int | None = None, limit: int = 200) -> dict[str, Any]:
    sources = _memory_sources(user_id, persona_id, limit)
    if not sources:
        return {"ok": True, "indexed": 0, "skipped": 0, "error": ""}

    model = _embedding_model()
    pending = []
    with get_db() as db:
        for source in sources:
            row = db.execute(
                """
                SELECT text_hash
                FROM memory_embeddings
                WHERE source_table = ? AND source_uid = ? AND model = ?
                """,
                (source["source_table"], source["source_uid"], model),
            ).fetchone()
            if row and row["text_hash"] == source["text_hash"]:
                continue
            pending.append(source)

    if not pending:
        return {"ok": True, "indexed": 0, "skipped": len(sources), "error": ""}

    try:
        vectors = create_embeddings([item["source_text"] for item in pending], model=model)
    except Exception as exc:
        return {"ok": False, "indexed": 0, "skipped": len(sources), "error": str(exc)}

    ts = now_ts()
    with get_db() as db:
        for source, vector in zip(pending, vectors):
            db.execute(
                """
                INSERT INTO memory_embeddings (
                    user_id, persona_id, source_table, source_uid, source_text,
                    text_hash, model, dimensions, vector_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_table, source_uid, model)
                DO UPDATE SET source_text = excluded.source_text,
                              text_hash = excluded.text_hash,
                              dimensions = excluded.dimensions,
                              vector_json = excluded.vector_json,
                              updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    source["persona_id"],
                    source["source_table"],
                    source["source_uid"],
                    source["source_text"],
                    source["text_hash"],
                    model,
                    len(vector),
                    json.dumps(vector),
                    ts,
                    ts,
                ),
            )

    return {"ok": True, "indexed": len(pending), "skipped": len(sources) - len(pending), "error": ""}


def semantic_memory_recall(user_id: int, persona_id: int | None, query: str, limit: int = 8) -> list[dict[str, Any]]:
    query = query.strip()
    if not query:
        return []

    sync = sync_memory_embeddings(user_id, persona_id)
    if not sync.get("ok"):
        return []

    model = _embedding_model()
    try:
        query_vector = create_embeddings([query], model=model)[0]
    except Exception as exc:
        print("[MemoryRAG] query embedding skipped:", exc)
        return []

    with get_db() as db:
        rows = db.execute(
            """
            SELECT *
            FROM memory_embeddings
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL) AND model = ?
            ORDER BY updated_at DESC
            LIMIT 500
            """,
            (user_id, persona_id, model),
        ).fetchall()

    scored = []
    for row in rows:
        item = dict_from_row(row) or {}
        try:
            vector = json.loads(item.get("vector_json") or "[]")
        except Exception:
            continue
        score = _cosine_similarity(query_vector, vector)
        if score > 0:
            scored.append((score, item))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    results = []
    for score, item in scored[: max(1, min(limit, 20))]:
        results.append(
            {
                "uid": item["source_uid"],
                "source_table": item["source_table"],
                "text": item["source_text"],
                "score": round(float(score), 4),
                "model": item["model"],
            }
        )
    return results


def semantic_memory_prompt(items: list[dict[str, Any]]) -> str:
    if not items:
        return "Semantic memory retrieval: no vector matches available."
    lines = ["Semantic memory retrieval:"]
    for item in items[:8]:
        lines.append(f"- {item['uid']} score={item['score']}: {item['text']}")
    lines.append("Use these semantic matches only when relevant, and obey state variables over fuzzy matches.")
    return "\n".join(lines)


def create_embeddings(texts: list[str], *, model: str | None = None) -> list[list[float]]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set for embeddings")
    model = model or _embedding_model()
    base_url = _embedding_base_url()
    payload: dict[str, Any] = {
        "model": model,
        "input": texts,
        "encoding_format": "float",
    }
    dimensions = _embedding_dimensions()
    if dimensions:
        payload["dimensions"] = dimensions

    response = requests.post(
        f"{base_url}/embeddings",
        json=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    rows = sorted(data.get("data", []), key=lambda item: item.get("index", 0))
    return [list(map(float, row["embedding"])) for row in rows]


def _memory_sources(user_id: int, persona_id: int | None, limit: int) -> list[dict[str, Any]]:
    sources = []
    with get_db() as db:
        facts = db.execute(
            """
            SELECT uid, persona_id, type, text, priority, importance, confidence
            FROM memory_facts
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL)
              AND archived = 0 AND valid_to IS NULL
            ORDER BY locked DESC, importance DESC, updated_at DESC
            LIMIT ?
            """,
            (user_id, persona_id, limit),
        ).fetchall()
        relations = db.execute(
            """
            SELECT uid, persona_id, type, subject, predicate, object, text, priority, importance, confidence
            FROM memory_relations
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL)
              AND archived = 0 AND valid_to IS NULL
            ORDER BY locked DESC, importance DESC, updated_at DESC
            LIMIT ?
            """,
            (user_id, persona_id, limit),
        ).fetchall()
        summaries = db.execute(
            """
            SELECT uid, persona_id, summary_type, text, importance, confidence
            FROM memory_summaries
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL) AND archived = 0
            ORDER BY importance DESC, updated_at DESC
            LIMIT ?
            """,
            (user_id, persona_id, max(20, limit // 4)),
        ).fetchall()

    for row in facts:
        item = dict_from_row(row) or {}
        text = f"[fact/{item.get('type')}] {item.get('text')}"
        sources.append(_source("memory_facts", item["uid"], user_id, item.get("persona_id"), text))
    for row in relations:
        item = dict_from_row(row) or {}
        relation = f"{item.get('subject')} {item.get('predicate')} {item.get('object')}"
        text = f"[relation/{item.get('type')}] {item.get('text')} ({relation})"
        sources.append(_source("memory_relations", item["uid"], user_id, item.get("persona_id"), text))
    for row in summaries:
        item = dict_from_row(row) or {}
        text = f"[summary/{item.get('summary_type')}] {item.get('text')}"
        sources.append(_source("memory_summaries", item["uid"], user_id, item.get("persona_id"), text))
    return sources


def _source(source_table: str, source_uid: str, user_id: int, persona_id: int | None, text: str) -> dict[str, Any]:
    text = " ".join(str(text or "").split())[:2000]
    return {
        "user_id": user_id,
        "persona_id": persona_id,
        "source_table": source_table,
        "source_uid": source_uid,
        "source_text": text,
        "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def _embedding_model() -> str:
    config = load_config()
    embedding = config.get("embedding", {}) if isinstance(config.get("embedding"), dict) else {}
    return str(embedding.get("model") or DEFAULT_EMBEDDING_MODEL)


def _embedding_dimensions() -> int | None:
    config = load_config()
    embedding = config.get("embedding", {}) if isinstance(config.get("embedding"), dict) else {}
    value = embedding.get("dimensions")
    try:
        return int(value) if value else None
    except Exception:
        return None


def _embedding_base_url() -> str:
    config = load_config()
    embedding = config.get("embedding", {}) if isinstance(config.get("embedding"), dict) else {}
    llm = config.get("llm", {}) if isinstance(config.get("llm"), dict) else {}
    base_url = str(embedding.get("base_url") or llm.get("base_url") or "").rstrip("/")
    if not base_url or base_url == "http://localhost:11434":
        return "https://api.openai.com/v1"
    return base_url


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
