from __future__ import annotations

import json
from typing import Any

from .conversation_memory import list_conversation_summaries
from .database import dict_from_row, get_db, now_ts
from .layered_memory import refresh_memory_state, refresh_memory_summaries
from .memory_conflicts import list_conflicts
from .memory_judge import list_judgements
from .mirror import get_user_insight


ALLOWED_PRIORITIES = {"low", "normal", "high", "critical"}


def memory_review(user_id: int, persona_id: int | None = None, include_history: bool = True) -> dict[str, Any]:
    refresh_memory_state(user_id, persona_id)
    refresh_memory_summaries(user_id, persona_id)

    current_clause = "" if include_history else "AND valid_to IS NULL"
    with get_db() as db:
        state_rows = db.execute(
            """
            SELECT key, value_json, source_uids_json, confidence, updated_at
            FROM memory_state
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL)
            ORDER BY key ASC
            """,
            (user_id, persona_id),
        ).fetchall()
        summaries = db.execute(
            """
            SELECT uid, summary_type, text, source_uids_json, importance, confidence, updated_at
            FROM memory_summaries
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL) AND archived = 0
            ORDER BY importance DESC, updated_at DESC
            """,
            (user_id, persona_id),
        ).fetchall()
        facts = db.execute(
            f"""
            SELECT uid, type, text, importance, confidence, priority, locked, decay_score,
                   valid_from, valid_to, supersedes_uid, superseded_by_uid, updated_at
            FROM memory_facts
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL) AND archived = 0
            {current_clause}
            ORDER BY locked DESC, importance DESC, updated_at DESC
            """,
            (user_id, persona_id),
        ).fetchall()
        relations = db.execute(
            f"""
            SELECT uid, type, subject, predicate, object, text, importance, confidence,
                   priority, locked, decay_score, valid_from, valid_to,
                   supersedes_uid, superseded_by_uid, updated_at
            FROM memory_relations
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL) AND archived = 0
            {current_clause}
            ORDER BY locked DESC, importance DESC, updated_at DESC
            """,
            (user_id, persona_id),
        ).fetchall()
        links = db.execute(
            """
            SELECT from_uid, to_uid, link_type, created_at
            FROM memory_links
            WHERE user_id = ?
            ORDER BY id ASC
            LIMIT 500
            """,
            (user_id,),
        ).fetchall()

    return {
        "insight": get_user_insight(user_id),
        "conversation_summaries": list_conversation_summaries(user_id, persona_id, limit=8),
        "judgements": list_judgements(user_id, persona_id, status="open", limit=30),
        "conflicts": list_conflicts(user_id, persona_id, status="open", limit=30),
        "state": [_decode_state(dict_from_row(row) or {}) for row in state_rows],
        "summaries": [_decode_sources(dict_from_row(row) or {}) for row in summaries],
        "facts": [dict_from_row(row) for row in facts],
        "relations": [dict_from_row(row) for row in relations],
        "links": [dict_from_row(row) for row in links],
    }


def context_traces(user_id: int, persona_id: int | None = None, limit: int = 10) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 50))
    params: list[Any] = [user_id]
    persona_clause = ""
    if persona_id is not None:
        persona_clause = "AND persona_id = ?"
        params.append(persona_id)
    params.append(limit)

    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT id, user_id, persona_id, conversation_id, user_message_id,
                   assistant_message_id, query_text, context_json, prompt_chars,
                   status, error_text, created_at, updated_at
            FROM chat_context_traces
            WHERE user_id = ? {persona_clause}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

    result = []
    for row in rows:
        item = dict_from_row(row) or {}
        try:
            item["context"] = json.loads(item.pop("context_json") or "{}")
        except Exception:
            item["context"] = {}
        result.append(item)
    return result


def update_memory_item(
    *,
    user_id: int,
    uid: str,
    priority: str | None = None,
    locked: bool | None = None,
    archived: bool | None = None,
) -> dict[str, Any]:
    table = _table_for_uid(uid)
    if table not in {"memory_facts", "memory_relations"}:
        raise ValueError("only fact and relation memories can be edited")
    if priority is not None and priority not in ALLOWED_PRIORITIES:
        raise ValueError("invalid priority")

    updates = []
    params: list[Any] = []
    if priority is not None:
        updates.append("priority = ?")
        params.append(priority)
    if locked is not None:
        updates.append("locked = ?")
        params.append(1 if locked else 0)
    if archived is not None:
        updates.append("archived = ?")
        params.append(1 if archived else 0)
    if not updates:
        return get_memory_item(user_id, uid)

    updates.append("updated_at = ?")
    params.append(now_ts())
    params.extend([uid, user_id])

    with get_db() as db:
        db.execute(
            f"""
            UPDATE {table}
            SET {", ".join(updates)}
            WHERE uid = ? AND user_id = ?
            """,
            params,
        )
    return get_memory_item(user_id, uid)


def get_memory_item(user_id: int, uid: str) -> dict[str, Any]:
    table = _table_for_uid(uid)
    if not table:
        raise ValueError("unknown memory uid")
    with get_db() as db:
        row = db.execute(f"SELECT * FROM {table} WHERE uid = ? AND user_id = ?", (uid, user_id)).fetchone()
        links = db.execute(
            """
            SELECT from_uid, to_uid, link_type, created_at
            FROM memory_links
            WHERE user_id = ? AND (from_uid = ? OR to_uid = ?)
            ORDER BY id ASC
            """,
            (user_id, uid, uid),
        ).fetchall()
    item = dict_from_row(row)
    if not item:
        raise ValueError("memory item not found")
    return {"item": item, "links": [dict_from_row(link) for link in links]}


def _table_for_uid(uid: str) -> str | None:
    if uid.startswith("FACT-"):
        return "memory_facts"
    if uid.startswith("REL-"):
        return "memory_relations"
    if uid.startswith("EP-"):
        return "memory_episodes"
    if uid.startswith("EVT-"):
        return "memory_events"
    if uid.startswith("SUM-"):
        return "memory_summaries"
    return None


def _decode_state(row: dict) -> dict:
    row = _decode_sources(row)
    try:
        row["value"] = json.loads(row.pop("value_json"))
    except Exception:
        row["value"] = None
    return row


def _decode_sources(row: dict) -> dict:
    raw = row.pop("source_uids_json", "[]")
    try:
        row["source_uids"] = json.loads(raw or "[]")
    except Exception:
        row["source_uids"] = []
    return row
