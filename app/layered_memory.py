from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any

from .database import dict_from_row, get_db, next_memory_uid, now_ts
from .memory_conflicts import detect_preference_conflicts, record_conflict
from .state_curator import curate_dynamic_state


FACT_PREFIX_BY_TYPE = {
    "user_profile": "FACT-USER",
    "identity": "FACT-USER",
    "preference": "FACT-PREF",
    "plan": "FACT-PLAN",
    "relationship": "FACT-REL",
    "boundary": "FACT-BOUNDARY",
    "persona_feedback": "FACT-PERSONA",
    "emotional_pattern": "FACT-EMOTION",
}

RELATION_PREDICATE_BY_TYPE = {
    "identity": "preferred_address",
    "preference": "preference",
    "plan": "has_plan",
    "relationship": "relationship_expectation",
    "boundary": "boundary",
    "persona_feedback": "persona_feedback",
    "emotional_pattern": "emotional_pattern",
    "user_profile": "profile_fact",
}

REPLACEABLE_FACT_TYPES = {"identity", "relationship"}
REPLACEABLE_RELATION_PREDICATES = {"preferred_address", "relationship_expectation"}


def record_user_message_event(
    *,
    user_id: int,
    persona_id: int | None,
    conversation_id: int | None,
    message_id: int | None,
    content: str,
) -> dict:
    ts = now_ts()
    uid = next_memory_uid("EVT", ts)
    with get_db() as db:
        db.execute(
            """
            INSERT INTO memory_events (
                uid, user_id, persona_id, conversation_id, message_id,
                event_type, role, content, metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, 'message', 'user', ?, '{}', ?)
            """,
            (uid, user_id, persona_id, conversation_id, message_id, content, ts),
        )
    return {"uid": uid, "layer": "L0", "content": content, "created_at": ts}


def create_episode_from_event(
    *,
    user_id: int,
    persona_id: int | None,
    conversation_id: int | None,
    event_uid: str,
    user_text: str,
    memories: list[dict[str, Any]],
) -> dict | None:
    if not memories:
        return None

    ts = now_ts()
    uid = next_memory_uid("EP", ts)
    title = _episode_title(memories)
    summary = _episode_summary(user_text, memories)
    importance = max(float(item.get("importance", 0.5)) for item in memories)
    confidence = max(float(item.get("confidence", 0.5)) for item in memories)

    with get_db() as db:
        db.execute(
            """
            INSERT INTO memory_episodes (
                uid, user_id, persona_id, conversation_id, title, summary,
                importance, confidence, valid_from, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (uid, user_id, persona_id, conversation_id, title, summary, importance, confidence, ts, ts, ts),
        )
        db.execute(
            "INSERT INTO memory_links (user_id, from_uid, to_uid, link_type, created_at) VALUES (?, ?, ?, 'derived_from', ?)",
            (user_id, uid, event_uid, ts),
        )

    return {"uid": uid, "layer": "L1", "title": title, "summary": summary}


def store_layered_memories(
    *,
    user_id: int,
    persona_id: int | None,
    conversation_id: int | None,
    source_message_id: int | None,
    event_uid: str | None,
    episode_uid: str | None,
    memories: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    stored: list[dict[str, Any]] = []
    for memory in memories:
        fact = upsert_fact(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            event_uid=event_uid,
            episode_uid=episode_uid,
            memory=memory,
        )
        if fact:
            stored.append(fact)

        relation = upsert_relation(
            user_id=user_id,
            persona_id=persona_id,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
            event_uid=event_uid,
            episode_uid=episode_uid,
            memory=memory,
        )
        if relation:
            stored.append(relation)
    return stored


def upsert_fact(
    *,
    user_id: int,
    persona_id: int | None,
    conversation_id: int | None,
    source_message_id: int | None,
    event_uid: str | None,
    episode_uid: str | None,
    memory: dict[str, Any],
) -> dict | None:
    memory_type = str(memory.get("type", "")).strip()
    text = str(memory.get("text", "")).strip()
    if not text:
        return None

    importance = _float(memory.get("importance", 0.5))
    confidence = _float(memory.get("confidence", 0.62))
    priority = _priority_for_memory(memory_type, importance)
    locked = 1 if priority in {"critical", "high"} and memory_type in {"identity", "boundary", "relationship", "persona_feedback"} else 0
    ts = now_ts()

    existing = _find_similar_fact(user_id, persona_id, memory_type, text)
    with get_db() as db:
        if existing:
            db.execute(
                """
                UPDATE memory_facts
                SET importance = ?, confidence = ?, priority = ?, locked = ?,
                    updated_at = ?, last_used_at = ?, access_count = access_count + 1, archived = 0
                WHERE uid = ?
                """,
                (
                    max(float(existing["importance"]), importance),
                    min(1.0, max(float(existing["confidence"]), confidence + 0.05)),
                    _max_priority(str(existing.get("priority", "normal")), priority),
                    max(int(existing.get("locked", 0) or 0), locked),
                    ts,
                    ts,
                    existing["uid"],
                ),
            )
            _link_sources(db, user_id, existing["uid"], event_uid, episode_uid, ts)
            return {"uid": existing["uid"], "layer": "L2", "type": memory_type, "text": text, "updated": True}

        uid = next_memory_uid(FACT_PREFIX_BY_TYPE.get(memory_type, "FACT-MISC"), ts)
        supersedes_uid = _find_superseded_fact(user_id, persona_id, memory_type, text)
        if supersedes_uid:
            previous_text = _memory_text_by_uid("memory_facts", user_id, supersedes_uid)
            db.execute(
                """
                UPDATE memory_facts
                SET valid_to = ?, superseded_by_uid = ?, updated_at = ?
                WHERE uid = ?
                """,
                (ts, uid, ts, supersedes_uid),
            )
            _insert_link(db, user_id, uid, supersedes_uid, "supersedes", ts)
            _insert_link(db, user_id, supersedes_uid, uid, "superseded_by", ts)
            record_conflict(
                user_id=user_id,
                persona_id=persona_id,
                conflict_type=f"{memory_type}_superseded",
                current_uid=uid,
                previous_uid=supersedes_uid,
                current_text=text,
                previous_text=previous_text,
                resolution="prefer_current",
                reason=f"New {memory_type} replaced older current value.",
            )

        db.execute(
            """
            INSERT INTO memory_facts (
                uid, user_id, persona_id, conversation_id, source_message_id, type, text,
                importance, confidence, valid_from, supersedes_uid, created_at, updated_at, last_used_at
                , priority, locked, decay_score, access_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (
                uid,
                user_id,
                persona_id,
                conversation_id,
                source_message_id,
                memory_type,
                text,
                importance,
                confidence,
                ts,
                supersedes_uid,
                ts,
                ts,
                ts,
                priority,
                locked,
            ),
        )
        _link_sources(db, user_id, uid, event_uid, episode_uid, ts)
    return {"uid": uid, "layer": "L2", "type": memory_type, "text": text, "updated": False}


def upsert_relation(
    *,
    user_id: int,
    persona_id: int | None,
    conversation_id: int | None,
    source_message_id: int | None,
    event_uid: str | None,
    episode_uid: str | None,
    memory: dict[str, Any],
) -> dict | None:
    memory_type = str(memory.get("type", "")).strip()
    text = str(memory.get("text", "")).strip()
    if not text:
        return None

    subject = "user"
    predicate = RELATION_PREDICATE_BY_TYPE.get(memory_type, memory_type or "related_to")
    obj = _object_from_memory(memory_type, text)
    importance = _float(memory.get("importance", 0.5))
    confidence = _float(memory.get("confidence", 0.62))
    priority = _priority_for_memory(memory_type, importance)
    locked = 1 if priority in {"critical", "high"} and memory_type in {"identity", "boundary", "relationship", "persona_feedback"} else 0
    ts = now_ts()

    existing = _find_similar_relation(user_id, persona_id, predicate, obj, text)
    with get_db() as db:
        if existing:
            db.execute(
                """
                UPDATE memory_relations
                SET importance = ?, confidence = ?, priority = ?, locked = ?,
                    updated_at = ?, last_used_at = ?, access_count = access_count + 1, archived = 0
                WHERE uid = ?
                """,
                (
                    max(float(existing["importance"]), importance),
                    min(1.0, max(float(existing["confidence"]), confidence + 0.05)),
                    _max_priority(str(existing.get("priority", "normal")), priority),
                    max(int(existing.get("locked", 0) or 0), locked),
                    ts,
                    ts,
                    existing["uid"],
                ),
            )
            _link_sources(db, user_id, existing["uid"], event_uid, episode_uid, ts)
            return {"uid": existing["uid"], "layer": "L3", "type": memory_type, "text": text, "updated": True}

        uid = next_memory_uid(f"REL-{memory_type or 'MISC'}", ts)
        supersedes_uid = _find_superseded_relation(user_id, persona_id, predicate, obj)
        if supersedes_uid:
            previous_text = _memory_text_by_uid("memory_relations", user_id, supersedes_uid)
            db.execute(
                """
                UPDATE memory_relations
                SET valid_to = ?, superseded_by_uid = ?, updated_at = ?
                WHERE uid = ?
                """,
                (ts, uid, ts, supersedes_uid),
            )
            _insert_link(db, user_id, uid, supersedes_uid, "supersedes", ts)
            _insert_link(db, user_id, supersedes_uid, uid, "superseded_by", ts)
            record_conflict(
                user_id=user_id,
                persona_id=persona_id,
                conflict_type=f"{predicate}_superseded",
                current_uid=uid,
                previous_uid=supersedes_uid,
                current_text=text,
                previous_text=previous_text,
                resolution="prefer_current",
                reason=f"New {predicate} replaced older current value.",
            )

        db.execute(
            """
            INSERT INTO memory_relations (
                uid, user_id, persona_id, conversation_id, source_message_id, type,
                subject, predicate, object, text, importance, confidence, valid_from,
                supersedes_uid, created_at, updated_at, last_used_at,
                priority, locked, decay_score, access_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (
                uid,
                user_id,
                persona_id,
                conversation_id,
                source_message_id,
                memory_type,
                subject,
                predicate,
                obj,
                text,
                importance,
                confidence,
                ts,
                supersedes_uid,
                ts,
                ts,
                ts,
                priority,
                locked,
            ),
        )
        _link_sources(db, user_id, uid, event_uid, episode_uid, ts)
    if predicate == "preference":
        detect_preference_conflicts(
            user_id=user_id,
            persona_id=persona_id,
            current_uid=uid,
            current_text=text,
            current_object=obj,
        )
    return {"uid": uid, "layer": "L3", "type": memory_type, "text": text, "updated": False}


def recall_layered_memory(
    user_id: int,
    persona_id: int | None,
    query: str,
    limit: int = 18,
    include_history: bool = False,
) -> dict[str, list[dict]]:
    keywords = _keywords(query)
    memory_profile = _persona_memory_profile(persona_id)
    facts = _recall_table("memory_facts", user_id, persona_id, keywords, limit, include_history, memory_profile)
    relations = _recall_table("memory_relations", user_id, persona_id, keywords, limit, include_history, memory_profile)
    episodes = _recall_table("memory_episodes", user_id, persona_id, keywords, max(4, limit // 3), include_history, memory_profile)
    summaries = _recall_table("memory_summaries", user_id, persona_id, keywords, max(3, limit // 4), include_history, memory_profile)

    return {
        "summaries": summaries,
        "relations": relations,
        "facts": facts,
        "episodes": episodes,
    }


def refresh_memory_summaries(user_id: int, persona_id: int | None = None) -> list[dict]:
    apply_memory_decay(user_id, persona_id)
    ts = now_ts()
    facts = _current_rows("memory_facts", user_id, persona_id, limit=80)
    relations = _current_rows("memory_relations", user_id, persona_id, limit=80)
    refresh_memory_state(user_id, persona_id, facts=facts, relations=relations)

    summaries = []
    user_summary = _build_user_summary(facts, relations)
    if user_summary:
        summaries.append(_upsert_summary(user_id, persona_id, None, "user_profile", user_summary, _source_uids(facts, relations), "critical", ts))

    interaction_summary = _build_interaction_summary(facts, relations)
    if interaction_summary:
        summaries.append(_upsert_summary(user_id, persona_id, None, "interaction_style", interaction_summary, _source_uids(facts, relations), "high", ts))

    return summaries


def refresh_memory_state(
    user_id: int,
    persona_id: int | None = None,
    *,
    facts: list[dict] | None = None,
    relations: list[dict] | None = None,
) -> dict[str, Any]:
    facts = facts if facts is not None else _current_rows("memory_facts", user_id, persona_id, limit=120)
    relations = relations if relations is not None else _current_rows("memory_relations", user_id, persona_id, limit=120)

    preferred = [r for r in relations if r.get("predicate") == "preferred_address"]
    boundaries = [r for r in relations if r.get("predicate") == "boundary"]
    preferences = [r for r in relations if r.get("predicate") == "preference"]
    latest_preferences = _latest_preferences_by_object(preferences)
    feedback = [item for item in [*facts, *relations] if item.get("type") == "persona_feedback"]
    relationship = [r for r in relations if r.get("predicate") == "relationship_expectation"]

    state = {
        "preferred_address": preferred[0].get("object") if preferred else None,
        "forbidden_addresses": _unique([r.get("object") for r in boundaries if r.get("object")]),
        "likes": _unique([r.get("object") for r in latest_preferences if _preference_polarity(str(r.get("text"))) == "like"]),
        "dislikes": _unique([r.get("object") for r in latest_preferences if _preference_polarity(str(r.get("text"))) == "dislike"]),
        "interaction_style": _unique([item.get("text") for item in feedback if item.get("text")]),
        "relationship_state": relationship[0].get("object") if relationship else None,
    }
    dynamic_state = _build_dynamic_state(facts, relations)
    state["dynamic_state"] = dynamic_state

    source_uids = _source_uids(facts, relations)
    ts = now_ts()
    with get_db() as db:
        for key, value in state.items():
            _upsert_state(db, user_id, persona_id, key, value, source_uids, ts)
        active_dynamic_keys = [f"dynamic.{key}" for key in dynamic_state]
        _delete_stale_dynamic_state(db, user_id, persona_id, active_dynamic_keys)
        for key, value in dynamic_state.items():
            _upsert_state(db, user_id, persona_id, f"dynamic.{key}", value, _state_source_uids(value), ts)
    return state


def state_prompt(user_id: int, persona_id: int | None = None) -> str:
    state = refresh_memory_state(user_id, persona_id)
    lines = ["Memory state variables:"]
    for key in ("preferred_address", "forbidden_addresses", "likes", "dislikes", "interaction_style", "relationship_state"):
        value = state.get(key)
        if value in (None, "", []):
            continue
        lines.append(f"- {key}: {json.dumps(value, ensure_ascii=False)}")
    dynamic_state = state.get("dynamic_state") if isinstance(state.get("dynamic_state"), dict) else {}
    if dynamic_state:
        lines.append("Dynamic state variables inferred from important memories:")
        for key, value in list(dynamic_state.items())[:12]:
            if value in (None, "", []):
                continue
            visible_items = [
                item for item in (value[:4] if isinstance(value, list) else [])
                if isinstance(item, dict) and item.get("injection_policy") != "recall_only"
            ]
            if not visible_items:
                continue
            lines.append(f"- dynamic.{key}:")
            for item in visible_items:
                text = item.get("text") or item.get("value") or ""
                meta = {
                    "weight": item.get("recall_weight"),
                    "policy": item.get("injection_policy"),
                    "lifecycle": item.get("lifecycle"),
                    "expires_at": item.get("expires_at"),
                    "urgency": item.get("urgency"),
                    "stability": item.get("stability"),
                    "tags": item.get("tags", []),
                    "source": item.get("source_uid"),
                }
                lines.append(f"  - {text} {json.dumps(meta, ensure_ascii=False)}")
    if len(lines) == 1:
        lines.append("- no explicit state variables yet")
    lines.append("These variables are precise backend state. Follow them over vague memory text.")
    return "\n".join(lines)


def apply_memory_decay(user_id: int, persona_id: int | None = None) -> dict:
    ts = now_ts()
    updated = 0
    with get_db() as db:
        for table in ("memory_facts", "memory_relations"):
            rows = db.execute(
                f"""
                SELECT uid, priority, locked, importance, updated_at, last_used_at, decay_score
                FROM {table}
                WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL)
                  AND archived = 0 AND valid_to IS NULL
                """,
                (user_id, persona_id),
            ).fetchall()
            for row in rows:
                item = dict_from_row(row) or {}
                if int(item.get("locked", 0) or 0) or item.get("priority") in {"critical", "high"}:
                    new_decay = 0.0
                else:
                    last = int(item.get("last_used_at") or item.get("updated_at") or ts)
                    age_days = max(0, (ts - last) / 86400)
                    importance = float(item.get("importance", 0.5) or 0.5)
                    new_decay = min(3.0, max(0.0, age_days * (1.0 - importance) * 0.08))
                if abs(float(item.get("decay_score", 0) or 0) - new_decay) >= 0.01:
                    db.execute(f"UPDATE {table} SET decay_score = ? WHERE uid = ?", (new_decay, item["uid"]))
                    updated += 1
    return {"updated": updated}


def layered_memory_prompt(memory: dict[str, list[dict]]) -> str:
    parts = ["Layered long-term memory:"]
    for section in ("summaries", "relations", "facts", "episodes"):
        items = memory.get(section, [])
        if not items:
            continue
        parts.append(f"{section}:")
        for item in items[:8]:
            uid = item.get("uid")
            text = item.get("text") or item.get("summary") or ""
            extra = ""
            if section == "relations":
                extra = f" ({item.get('subject')} {item.get('predicate')} {item.get('object')})"
            parts.append(f"- {uid}: {text}{extra}")
    if len(parts) == 1:
        parts.append("No layered memories yet.")
    return "\n".join(parts)


def summary_prompt(user_id: int, persona_id: int | None = None) -> str:
    refresh_memory_summaries(user_id, persona_id)
    summaries = _current_rows("memory_summaries", user_id, persona_id, limit=8)
    if not summaries:
        return "Stable memory summary: no summaries yet."
    lines = ["Stable memory summary:"]
    for item in summaries:
        lines.append(f"- {item.get('uid')}: {item.get('text')}")
    return "\n".join(lines)


def _recall_table(
    table: str,
    user_id: int,
    persona_id: int | None,
    keywords: set[str],
    limit: int,
    include_history: bool,
    memory_profile: dict,
) -> list[dict]:
    current_clause = ""
    if not include_history and table in {"memory_facts", "memory_relations", "memory_episodes"}:
        current_clause = "AND valid_to IS NULL"

    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT *
            FROM {table}
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL) AND archived = 0
            {current_clause}
            ORDER BY importance DESC, confidence DESC, updated_at DESC
            LIMIT 80
            """,
            (user_id, persona_id),
        ).fetchall()

    scored = []
    for row in rows:
        item = dict_from_row(row) or {}
        score = _score_item(item, keywords, memory_profile)
        if item.get("type") in {"identity", "relationship", "boundary", "persona_feedback"}:
            score += 3
        if table == "memory_relations" and item.get("predicate") in {"boundary", "preferred_address"}:
            score += 4
        if score > 0 or not keywords:
            scored.append((score, item))
    scored.sort(key=lambda pair: (-pair[0], -float(pair[1].get("importance", 0)), -int(pair[1].get("updated_at", 0))))
    return [item for _, item in scored[:limit]]


def _current_rows(table: str, user_id: int, persona_id: int | None, limit: int) -> list[dict]:
    valid_clause = "AND valid_to IS NULL" if table in {"memory_facts", "memory_relations", "memory_episodes"} else ""
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT *
            FROM {table}
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL) AND archived = 0
            {valid_clause}
            ORDER BY importance DESC, confidence DESC, updated_at DESC
            LIMIT ?
            """,
            (user_id, persona_id, limit),
        ).fetchall()
    return [dict_from_row(row) or {} for row in rows]


def _upsert_summary(
    user_id: int,
    persona_id: int | None,
    conversation_id: int | None,
    summary_type: str,
    text: str,
    source_uids: list[str],
    priority: str,
    ts: int,
) -> dict:
    with get_db() as db:
        row = db.execute(
            """
            SELECT *
            FROM memory_summaries
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL)
              AND conversation_id IS ? AND summary_type = ? AND archived = 0
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (user_id, persona_id, conversation_id, summary_type),
        ).fetchone()
        existing = dict_from_row(row)
        if existing:
            db.execute(
                """
                UPDATE memory_summaries
                SET text = ?, source_uids_json = ?, importance = ?, confidence = ?, updated_at = ?
                WHERE uid = ?
                """,
                (text, json.dumps(source_uids, ensure_ascii=False), 0.9 if priority == "critical" else 0.75, 0.78, ts, existing["uid"]),
            )
            return {"uid": existing["uid"], "summary_type": summary_type, "text": text, "updated": True}

        uid = next_memory_uid(f"SUM-{summary_type.upper()}", ts)
        db.execute(
            """
            INSERT INTO memory_summaries (
                uid, user_id, persona_id, conversation_id, summary_type, text,
                source_uids_json, importance, confidence, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uid,
                user_id,
                persona_id,
                conversation_id,
                summary_type,
                text,
                json.dumps(source_uids, ensure_ascii=False),
                0.9 if priority == "critical" else 0.75,
                0.78,
                ts,
                ts,
            ),
        )
    return {"uid": uid, "summary_type": summary_type, "text": text, "updated": False}


def _upsert_state(db, user_id: int, persona_id: int | None, key: str, value: Any, source_uids: list[str], ts: int) -> None:
    persona_scope = str(persona_id) if persona_id is not None else "global"
    db.execute(
        """
        INSERT INTO memory_state (user_id, persona_id, persona_scope, key, value_json, source_uids_json, confidence, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 0.82, ?)
        ON CONFLICT(user_id, persona_scope, key)
        DO UPDATE SET value_json = excluded.value_json,
                      source_uids_json = excluded.source_uids_json,
                      confidence = excluded.confidence,
                      updated_at = excluded.updated_at
        """,
        (
            user_id,
            persona_id,
            persona_scope,
            key,
            json.dumps(value, ensure_ascii=False),
            json.dumps(source_uids, ensure_ascii=False),
            ts,
        ),
    )


def _build_user_summary(facts: list[dict], relations: list[dict]) -> str:
    preferred = [r for r in relations if r.get("predicate") == "preferred_address"]
    boundaries = [r for r in relations if r.get("predicate") == "boundary"]
    preferences = _latest_preferences_by_object([r for r in relations if r.get("predicate") == "preference"])
    likes = [r for r in preferences if _preference_polarity(str(r.get("text"))) == "like"]
    dislikes = [r for r in preferences if _preference_polarity(str(r.get("text"))) == "dislike"]

    lines = []
    if preferred:
        lines.append(f"用户当前希望被称为{preferred[0].get('object')}。")
    if boundaries:
        names = "、".join(str(r.get("object")) for r in boundaries[:8])
        lines.append(f"不要称呼用户为：{names}。")
    if likes:
        names = "、".join(str(r.get("object")) for r in likes[:8])
        lines.append(f"用户喜欢：{names}。")
    if dislikes:
        names = "、".join(str(r.get("object")) for r in dislikes[:8])
        lines.append(f"用户讨厌：{names}。")
    return "\n".join(lines)


def _build_interaction_summary(facts: list[dict], relations: list[dict]) -> str:
    feedback = [item for item in [*facts, *relations] if item.get("type") == "persona_feedback"]
    boundaries = [item for item in [*facts, *relations] if item.get("type") == "boundary" and any(k in str(item.get("text")) for k in ["语气", "说教", "追问", "黏"])]
    lines = []
    for item in [*feedback, *boundaries][:8]:
        text = str(item.get("text") or "")
        if text and text not in lines:
            lines.append(text)
    return "\n".join(lines)


def _build_dynamic_state(facts: list[dict], relations: list[dict]) -> dict[str, list[dict[str, Any]]]:
    return curate_dynamic_state(facts, relations)


def _state_source_uids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        str(item.get("source_uid") or item.get("uid"))
        for item in value
        if isinstance(item, dict) and (item.get("source_uid") or item.get("uid"))
    ][:80]


def _delete_stale_dynamic_state(db, user_id: int, persona_id: int | None, active_keys: list[str]) -> None:
    persona_scope = str(persona_id) if persona_id is not None else "global"
    params: list[Any] = [user_id, persona_scope]
    keep_clause = ""
    if active_keys:
        keep_clause = f"AND key NOT IN ({','.join('?' for _ in active_keys)})"
        params.extend(active_keys)
    db.execute(
        f"""
        DELETE FROM memory_state
        WHERE user_id = ? AND persona_scope = ? AND key LIKE 'dynamic.%'
        {keep_clause}
        """,
        params,
    )


def _source_uids(*groups: list[dict]) -> list[str]:
    uids = []
    for group in groups:
        for item in group:
            uid = item.get("uid")
            if uid and uid not in uids:
                uids.append(str(uid))
    return uids[:80]


def _unique(values: list[Any]) -> list[Any]:
    out = []
    for value in values:
        if value in (None, "", []):
            continue
        if value not in out:
            out.append(value)
    return out


def _link_sources(db, user_id: int, target_uid: str, event_uid: str | None, episode_uid: str | None, ts: int) -> None:
    for source_uid in (event_uid, episode_uid):
        if not source_uid:
            continue
        _insert_link(db, user_id, target_uid, source_uid, "derived_from", ts)


def _insert_link(db, user_id: int, from_uid: str, to_uid: str, link_type: str, ts: int) -> None:
    exists = db.execute(
        "SELECT id FROM memory_links WHERE user_id = ? AND from_uid = ? AND to_uid = ? AND link_type = ?",
        (user_id, from_uid, to_uid, link_type),
    ).fetchone()
    if not exists:
        db.execute(
            "INSERT INTO memory_links (user_id, from_uid, to_uid, link_type, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, from_uid, to_uid, link_type, ts),
        )


def _find_similar_fact(user_id: int, persona_id: int | None, memory_type: str, text: str) -> dict | None:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT *
            FROM memory_facts
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL) AND type = ? AND archived = 0 AND valid_to IS NULL
            ORDER BY updated_at DESC
            LIMIT 80
            """,
            (user_id, persona_id, memory_type),
        ).fetchall()
    return _find_similar(rows, text)


def _find_similar_relation(user_id: int, persona_id: int | None, predicate: str, obj: str, text: str) -> dict | None:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT *
            FROM memory_relations
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL)
              AND predicate = ? AND archived = 0 AND valid_to IS NULL
            ORDER BY updated_at DESC
            LIMIT 80
            """,
            (user_id, persona_id, predicate),
        ).fetchall()
    for row in rows:
        item = dict_from_row(row) or {}
        if predicate == "preference" and item.get("object") == obj:
            current_polarity = _preference_polarity(text)
            stored_polarity = _preference_polarity(str(item.get("text") or ""))
            if current_polarity and stored_polarity and current_polarity != stored_polarity:
                continue
        if item.get("object") == obj or SequenceMatcher(None, str(item.get("text")), text).ratio() >= 0.9:
            return item
    return None


def _find_superseded_fact(user_id: int, persona_id: int | None, memory_type: str, text: str) -> str | None:
    if memory_type not in REPLACEABLE_FACT_TYPES:
        return None
    with get_db() as db:
        row = db.execute(
            """
            SELECT uid, text
            FROM memory_facts
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL) AND type = ?
              AND valid_to IS NULL AND archived = 0
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (user_id, persona_id, memory_type),
        ).fetchone()
    item = dict_from_row(row)
    if item and item.get("text") != text:
        return str(item["uid"])
    return None


def _find_superseded_relation(user_id: int, persona_id: int | None, predicate: str, obj: str) -> str | None:
    if predicate not in REPLACEABLE_RELATION_PREDICATES:
        return None
    with get_db() as db:
        row = db.execute(
            """
            SELECT uid, object
            FROM memory_relations
            WHERE user_id = ? AND (persona_id = ? OR persona_id IS NULL) AND predicate = ?
              AND valid_to IS NULL AND archived = 0
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (user_id, persona_id, predicate),
        ).fetchone()
    item = dict_from_row(row)
    if item and item.get("object") != obj:
        return str(item["uid"])
    return None


def _memory_text_by_uid(table: str, user_id: int, uid: str) -> str:
    with get_db() as db:
        row = db.execute(f"SELECT text FROM {table} WHERE user_id = ? AND uid = ?", (user_id, uid)).fetchone()
    item = dict_from_row(row)
    return str(item.get("text") or "") if item else ""


def _find_similar(rows, text: str) -> dict | None:
    for row in rows:
        item = dict_from_row(row) or {}
        if item.get("text") == text or SequenceMatcher(None, str(item.get("text")), text).ratio() >= 0.9:
            return item
    return None


def _episode_title(memories: list[dict[str, Any]]) -> str:
    types = sorted({str(item.get("type")) for item in memories if item.get("type")})
    return " / ".join(types)[:80] or "memory episode"


def _episode_summary(user_text: str, memories: list[dict[str, Any]]) -> str:
    facts = "; ".join(str(item.get("text")) for item in memories if item.get("text"))
    return f"User message: {user_text[:180]}\nExtracted: {facts[:400]}"


def _object_from_memory(memory_type: str, text: str) -> str:
    patterns = [
        r"用户希望被称为(.+)",
        r"不要称呼用户为(.+)",
        r"用户喜欢(.+)",
        r"用户讨厌(.+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return text[:120]


def _latest_preferences_by_object(preferences: list[dict]) -> list[dict]:
    latest: dict[str, dict] = {}
    for item in sorted(
        preferences,
        key=lambda row: (
            int(row.get("updated_at") or 0),
            int(row.get("valid_from") or 0),
            int(row.get("id") or 0),
        ),
    ):
        obj = str(item.get("object") or "").strip()
        if obj:
            latest[obj] = item
    return list(latest.values())


def _preference_polarity(text: str) -> str:
    if any(word in text for word in ("讨厌", "不喜欢", "dislike", "hate")):
        return "dislike"
    if any(word in text for word in ("喜欢", "like", "love")):
        return "like"
    return ""


def _keywords(query: str) -> set[str]:
    return set(re.findall(r"[\w\u4e00-\u9fff]{2,}", query.lower()))


def _score_item(item: dict, keywords: set[str], memory_profile: dict) -> float:
    proactive = float(memory_profile.get("proactive_recall", 0.65) or 0.65)
    detail = float(memory_profile.get("detail_retention", 0.68) or 0.68)
    attentiveness = float(memory_profile.get("memory_attentiveness", 0.72) or 0.72)
    persona_bonus = (proactive + detail + attentiveness) / 3
    if not keywords:
        return 1.0 + persona_bonus
    haystack = " ".join(str(item.get(key, "")) for key in ("uid", "type", "text", "summary", "subject", "predicate", "object")).lower()
    score = sum(1 for keyword in keywords if keyword in haystack)
    priority_bonus = {"critical": 5.0, "high": 3.0, "normal": 1.0, "low": 0.0}.get(str(item.get("priority", "normal")), 1.0)
    locked_bonus = 4.0 if int(item.get("locked", 0) or 0) else 0.0
    decay_penalty = float(item.get("decay_score", 0) or 0)
    if score:
        return float(score) + priority_bonus + locked_bonus + persona_bonus - (decay_penalty * (1.15 - detail))
    shared = set("".join(keywords)) & set(haystack)
    useful = {ch for ch in shared if ch.strip() and ch not in "的了呢啊是我你他她它，。！？,.!?"}
    return min(len(useful) / 8, 1.5) + priority_bonus + locked_bonus + persona_bonus - (decay_penalty * (1.15 - detail))


def _float(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return 0.5


def _priority_for_memory(memory_type: str, importance: float) -> str:
    if memory_type in {"identity", "boundary"} and importance >= 0.85:
        return "critical"
    if memory_type in {"relationship", "persona_feedback"} and importance >= 0.65:
        return "high"
    if importance >= 0.75:
        return "high"
    if importance < 0.45:
        return "low"
    return "normal"


def _max_priority(a: str, b: str) -> str:
    order = {"low": 0, "normal": 1, "high": 2, "critical": 3}
    return a if order.get(a, 1) >= order.get(b, 1) else b


def _persona_memory_profile(persona_id: int | None) -> dict:
    default = {
        "memory_attentiveness": 0.72,
        "detail_retention": 0.68,
        "proactive_recall": 0.65,
        "style": "normal",
    }
    if not persona_id:
        return default
    try:
        with get_db() as db:
            row = db.execute("SELECT memory_profile_json FROM personas WHERE id = ?", (persona_id,)).fetchone()
        if not row:
            return default
        data = json.loads(row["memory_profile_json"] or "{}")
        if not isinstance(data, dict):
            return default
        return {**default, **data}
    except Exception:
        return default
