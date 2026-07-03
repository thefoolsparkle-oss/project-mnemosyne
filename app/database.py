from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import BASE_DIR


DB_PATH = BASE_DIR / "data" / "app.db"


def now_ts() -> int:
    return int(time.time())


def dict_from_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                status TEXT NOT NULL DEFAULT 'active',
                is_guest INTEGER NOT NULL DEFAULT 0,
                guest_expires_at INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id INTEGER PRIMARY KEY,
                nickname TEXT NOT NULL,
                avatar_url TEXT,
                gender TEXT NOT NULL DEFAULT '',
                birthday TEXT,
                signature TEXT,
                bio TEXT,
                preferences_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS personas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                summary TEXT NOT NULL,
                prompt TEXT NOT NULL,
                traits_json TEXT NOT NULL DEFAULT '[]',
                relationship TEXT NOT NULL DEFAULT '',
                speaking_style TEXT NOT NULL DEFAULT '',
                boundaries_json TEXT NOT NULL DEFAULT '[]',
                memory_profile_json TEXT NOT NULL DEFAULT '{}',
                psychological_profile_json TEXT NOT NULL DEFAULT '{}',
                psychological_fit_notes TEXT NOT NULL DEFAULT '',
                appearance_description TEXT NOT NULL DEFAULT '',
                desired_image TEXT NOT NULL DEFAULT '',
                growth_notes TEXT NOT NULL DEFAULT '',
                avatar_url TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                version INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS persona_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                persona_id INTEGER NOT NULL,
                version INTEGER NOT NULL,
                name TEXT NOT NULL,
                summary TEXT NOT NULL,
                prompt TEXT NOT NULL,
                traits_json TEXT NOT NULL DEFAULT '[]',
                relationship TEXT NOT NULL DEFAULT '',
                speaking_style TEXT NOT NULL DEFAULT '',
                boundaries_json TEXT NOT NULL DEFAULT '[]',
                psychological_profile_json TEXT NOT NULL DEFAULT '{}',
                psychological_fit_notes TEXT NOT NULL DEFAULT '',
                appearance_description TEXT NOT NULL DEFAULT '',
                desired_image TEXT NOT NULL DEFAULT '',
                growth_notes TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                change_type TEXT NOT NULL DEFAULT '',
                source_suggestion_id INTEGER,
                change_notes_json TEXT NOT NULL DEFAULT '[]',
                created_at INTEGER NOT NULL,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS persona_growth_views (
                user_id INTEGER NOT NULL,
                persona_id INTEGER NOT NULL,
                seen_reviewed_version INTEGER NOT NULL DEFAULT 0,
                viewed_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, persona_id),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS persona_growth_feedback (
                user_id INTEGER NOT NULL,
                persona_id INTEGER NOT NULL,
                reviewed_version INTEGER NOT NULL,
                reaction TEXT NOT NULL CHECK(reaction IN ('helpful', 'needs_adjustment')),
                detail_text TEXT NOT NULL DEFAULT '',
                resolved_at INTEGER NOT NULL DEFAULT 0,
                resolved_by_user_id INTEGER,
                resolution_note TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, persona_id, reviewed_version),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS persona_growth_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                persona_id INTEGER NOT NULL,
                request_text TEXT NOT NULL,
                suggestion_id INTEGER,
                memory_uids_json TEXT NOT NULL DEFAULT '[]',
                request_origin TEXT NOT NULL DEFAULT 'direct_entry',
                source_reviewed_version INTEGER,
                source_message_id INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL DEFAULT 0,
                withdrawn_at INTEGER NOT NULL DEFAULT 0,
                deactivation_actor TEXT NOT NULL DEFAULT '',
                deactivation_reason TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE,
                FOREIGN KEY (suggestion_id) REFERENCES persona_revision_suggestions(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                persona_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                pinned_at INTEGER NOT NULL DEFAULT 0,
                last_read_message_id INTEGER NOT NULL DEFAULT 0,
                last_read_at INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                persona_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('system', 'user', 'assistant')),
                content TEXT NOT NULL,
                reply_status TEXT NOT NULL DEFAULT '',
                reply_error TEXT NOT NULL DEFAULT '',
                client_message_id TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS message_expressions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                persona_id INTEGER NOT NULL,
                conversation_id INTEGER NOT NULL,
                expression_type TEXT NOT NULL DEFAULT 'gesture',
                label TEXT NOT NULL DEFAULT '',
                source_text TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS expression_preferences (
                user_id INTEGER NOT NULL,
                persona_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                mode TEXT NOT NULL DEFAULT 'normal',
                source_message_id INTEGER,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, persona_id),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE,
                FOREIGN KEY (source_message_id) REFERENCES messages(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS expression_asset_settings (
                expression_type TEXT NOT NULL,
                label TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                cooldown_turns INTEGER NOT NULL DEFAULT -1,
                lifecycle_status TEXT NOT NULL DEFAULT 'active',
                asset_kind TEXT NOT NULL DEFAULT '',
                media_url TEXT NOT NULL DEFAULT '',
                thumbnail_url TEXT NOT NULL DEFAULT '',
                alt_text TEXT NOT NULL DEFAULT '',
                media_review_status TEXT NOT NULL DEFAULT 'approved',
                media_review_note TEXT NOT NULL DEFAULT '',
                admin_note TEXT NOT NULL DEFAULT '',
                updated_by_user_id INTEGER,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (expression_type, label),
                FOREIGN KEY (updated_by_user_id) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS group_conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                pinned_at INTEGER NOT NULL DEFAULT 0,
                last_read_group_message_id INTEGER NOT NULL DEFAULT 0,
                last_read_at INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS group_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_conversation_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                persona_id INTEGER NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                role_note TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                joined_at INTEGER NOT NULL,
                last_spoke_at INTEGER NOT NULL DEFAULT 0,
                turn_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE(group_conversation_id, persona_id),
                FOREIGN KEY (group_conversation_id) REFERENCES group_conversations(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS group_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_conversation_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                speaker_type TEXT NOT NULL CHECK(speaker_type IN ('user', 'persona', 'system')),
                speaker_persona_id INTEGER,
                content TEXT NOT NULL,
                reply_status TEXT NOT NULL DEFAULT '',
                reply_error TEXT NOT NULL DEFAULT '',
                client_message_id TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                FOREIGN KEY (group_conversation_id) REFERENCES group_conversations(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (speaker_persona_id) REFERENCES personas(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS group_member_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_conversation_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                persona_id INTEGER NOT NULL,
                other_persona_id INTEGER NOT NULL,
                affinity INTEGER NOT NULL DEFAULT 0,
                tension INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL,
                UNIQUE(group_conversation_id, persona_id, other_persona_id),
                FOREIGN KEY (group_conversation_id) REFERENCES group_conversations(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE,
                FOREIGN KEY (other_persona_id) REFERENCES personas(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS group_message_expressions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_message_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                persona_id INTEGER NOT NULL,
                group_conversation_id INTEGER NOT NULL,
                expression_type TEXT NOT NULL DEFAULT 'gesture',
                label TEXT NOT NULL DEFAULT '',
                source_text TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                FOREIGN KEY (group_message_id) REFERENCES group_messages(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE,
                FOREIGN KEY (group_conversation_id) REFERENCES group_conversations(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS conversation_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                persona_id INTEGER NOT NULL,
                conversation_id INTEGER NOT NULL UNIQUE,
                summary_text TEXT NOT NULL DEFAULT '',
                key_points_json TEXT NOT NULL DEFAULT '[]',
                covered_message_id INTEGER,
                source_message_count INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                FOREIGN KEY (covered_message_id) REFERENCES messages(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                persona_id INTEGER,
                conversation_id INTEGER,
                type TEXT NOT NULL,
                text TEXT NOT NULL,
                importance REAL NOT NULL DEFAULT 0.5,
                confidence REAL NOT NULL DEFAULT 0.5,
                source_message_id INTEGER,
                archived INTEGER NOT NULL DEFAULT 0,
                conflict_group TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_used_at INTEGER,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE SET NULL,
                FOREIGN KEY (source_message_id) REFERENCES messages(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS user_insights (
                user_id INTEGER PRIMARY KEY,
                profile_summary TEXT NOT NULL DEFAULT '',
                interaction_style TEXT NOT NULL DEFAULT '',
                emotional_patterns_json TEXT NOT NULL DEFAULT '[]',
                discovery_dimensions_json TEXT NOT NULL DEFAULT '{}',
                curiosity_feedback_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS memory_counters (
                prefix TEXT PRIMARY KEY,
                value INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS memory_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                persona_id INTEGER,
                conversation_id INTEGER,
                message_id INTEGER,
                event_type TEXT NOT NULL,
                role TEXT,
                content TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE SET NULL,
                FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS memory_episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                persona_id INTEGER,
                conversation_id INTEGER,
                title TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL,
                importance REAL NOT NULL DEFAULT 0.5,
                confidence REAL NOT NULL DEFAULT 0.5,
                valid_from INTEGER NOT NULL,
                valid_to INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS memory_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                persona_id INTEGER,
                conversation_id INTEGER,
                source_message_id INTEGER,
                type TEXT NOT NULL,
                text TEXT NOT NULL,
                importance REAL NOT NULL DEFAULT 0.5,
                confidence REAL NOT NULL DEFAULT 0.5,
                valid_from INTEGER NOT NULL,
                valid_to INTEGER,
                supersedes_uid TEXT,
                superseded_by_uid TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_used_at INTEGER,
                archived INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE SET NULL,
                FOREIGN KEY (source_message_id) REFERENCES messages(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS memory_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                persona_id INTEGER,
                conversation_id INTEGER,
                source_message_id INTEGER,
                type TEXT NOT NULL,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                text TEXT NOT NULL,
                importance REAL NOT NULL DEFAULT 0.5,
                confidence REAL NOT NULL DEFAULT 0.5,
                valid_from INTEGER NOT NULL,
                valid_to INTEGER,
                supersedes_uid TEXT,
                superseded_by_uid TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_used_at INTEGER,
                archived INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE SET NULL,
                FOREIGN KEY (source_message_id) REFERENCES messages(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS memory_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                persona_id INTEGER,
                conversation_id INTEGER,
                summary_type TEXT NOT NULL,
                text TEXT NOT NULL,
                source_uids_json TEXT NOT NULL DEFAULT '[]',
                importance REAL NOT NULL DEFAULT 0.5,
                confidence REAL NOT NULL DEFAULT 0.5,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS memory_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                from_uid TEXT NOT NULL,
                to_uid TEXT NOT NULL,
                link_type TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS memory_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                persona_id INTEGER,
                persona_scope TEXT NOT NULL DEFAULT 'global',
                key TEXT NOT NULL,
                value_json TEXT NOT NULL,
                source_uids_json TEXT NOT NULL DEFAULT '[]',
                confidence REAL NOT NULL DEFAULT 0.7,
                updated_at INTEGER NOT NULL,
                UNIQUE(user_id, persona_scope, key),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS chat_context_traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                persona_id INTEGER NOT NULL,
                conversation_id INTEGER NOT NULL,
                user_message_id INTEGER NOT NULL,
                assistant_message_id INTEGER,
                query_text TEXT NOT NULL,
                context_json TEXT NOT NULL,
                prompt_chars INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                error_text TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
                FOREIGN KEY (user_message_id) REFERENCES messages(id) ON DELETE CASCADE,
                FOREIGN KEY (assistant_message_id) REFERENCES messages(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS persona_revision_suggestions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                persona_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                base_version INTEGER,
                origin TEXT NOT NULL DEFAULT 'manual',
                trigger_message_id INTEGER,
                trigger_memory_uids_json TEXT NOT NULL DEFAULT '[]',
                reason TEXT NOT NULL DEFAULT '',
                suggestion_json TEXT NOT NULL,
                source_context_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                applied_at INTEGER,
                applied_version INTEGER,
                decided_at INTEGER,
                decided_by_user_id INTEGER,
                decision_actor TEXT NOT NULL DEFAULT 'admin',
                decision_note TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE,
                FOREIGN KEY (decided_by_user_id) REFERENCES users(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS memory_embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                persona_id INTEGER,
                source_table TEXT NOT NULL,
                source_uid TEXT NOT NULL,
                source_text TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                model TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                vector_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(source_table, source_uid, model),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS memory_judgements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                persona_id INTEGER,
                memory_uid TEXT NOT NULL,
                memory_layer TEXT NOT NULL,
                memory_type TEXT NOT NULL DEFAULT '',
                memory_text TEXT NOT NULL,
                source_message_id INTEGER,
                quality_score REAL NOT NULL DEFAULT 0.5,
                risk_score REAL NOT NULL DEFAULT 0,
                action TEXT NOT NULL DEFAULT 'keep',
                reasons_json TEXT NOT NULL DEFAULT '[]',
                flags_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'open',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(memory_uid),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE,
                FOREIGN KEY (source_message_id) REFERENCES messages(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS memory_conflicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                persona_id INTEGER,
                conflict_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                current_uid TEXT NOT NULL,
                previous_uid TEXT,
                current_text TEXT NOT NULL,
                previous_text TEXT NOT NULL DEFAULT '',
                resolution TEXT NOT NULL DEFAULT 'prefer_current',
                reason TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(current_uid, previous_uid, conflict_type),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS memory_eval_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                persona_id INTEGER,
                suite_name TEXT NOT NULL,
                status TEXT NOT NULL,
                score REAL NOT NULL DEFAULT 0,
                results_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (persona_id) REFERENCES personas(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS llm_call_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task TEXT NOT NULL DEFAULT 'default',
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                prompt_chars INTEGER NOT NULL DEFAULT 0,
                response_chars INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                error_text TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_personas_user_id ON personas(user_id);
            CREATE INDEX IF NOT EXISTS idx_persona_growth_views_user
                ON persona_growth_views(user_id, persona_id);
            CREATE INDEX IF NOT EXISTS idx_persona_growth_feedback_scope
                ON persona_growth_feedback(user_id, persona_id, reviewed_version);
            CREATE INDEX IF NOT EXISTS idx_persona_growth_requests_scope
                ON persona_growth_requests(user_id, persona_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations(user_id);
            CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);
            CREATE INDEX IF NOT EXISTS idx_message_expressions_message_id ON message_expressions(message_id);
            CREATE INDEX IF NOT EXISTS idx_expression_preferences_scope
                ON expression_preferences(user_id, persona_id);
            CREATE INDEX IF NOT EXISTS idx_expression_asset_settings_enabled
                ON expression_asset_settings(enabled);
            CREATE INDEX IF NOT EXISTS idx_group_conversations_user
                ON group_conversations(user_id, status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_group_members_group
                ON group_members(group_conversation_id, is_active);
            CREATE INDEX IF NOT EXISTS idx_group_messages_group
                ON group_messages(group_conversation_id, id);
            CREATE INDEX IF NOT EXISTS idx_group_message_expressions_message_id
                ON group_message_expressions(group_message_id);
            CREATE INDEX IF NOT EXISTS idx_conversation_summaries_scope
                ON conversation_summaries(user_id, persona_id, conversation_id);
            CREATE INDEX IF NOT EXISTS idx_memories_user_persona ON memories(user_id, persona_id, archived);
            CREATE INDEX IF NOT EXISTS idx_memories_text ON memories(user_id, type, text);
            CREATE INDEX IF NOT EXISTS idx_memory_events_scope ON memory_events(user_id, persona_id, conversation_id);
            CREATE INDEX IF NOT EXISTS idx_memory_episodes_scope ON memory_episodes(user_id, persona_id, archived);
            CREATE INDEX IF NOT EXISTS idx_memory_facts_scope ON memory_facts(user_id, persona_id, type, archived);
            CREATE INDEX IF NOT EXISTS idx_memory_relations_scope ON memory_relations(user_id, persona_id, predicate, archived);
            CREATE INDEX IF NOT EXISTS idx_memory_summaries_scope ON memory_summaries(user_id, persona_id, summary_type, archived);
            CREATE INDEX IF NOT EXISTS idx_memory_state_scope ON memory_state(user_id, persona_id, key);
            CREATE INDEX IF NOT EXISTS idx_memory_links_from ON memory_links(from_uid);
            CREATE INDEX IF NOT EXISTS idx_memory_links_to ON memory_links(to_uid);
            CREATE INDEX IF NOT EXISTS idx_chat_context_traces_scope
                ON chat_context_traces(user_id, persona_id, conversation_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_persona_revision_suggestions_scope
                ON persona_revision_suggestions(user_id, persona_id, status, created_at);
            CREATE INDEX IF NOT EXISTS idx_memory_embeddings_scope
                ON memory_embeddings(user_id, persona_id, model);
            CREATE INDEX IF NOT EXISTS idx_memory_judgements_scope
                ON memory_judgements(user_id, persona_id, status, action, created_at);
            CREATE INDEX IF NOT EXISTS idx_memory_conflicts_scope
                ON memory_conflicts(user_id, persona_id, status, conflict_type, created_at);
            CREATE INDEX IF NOT EXISTS idx_memory_eval_runs_scope
                ON memory_eval_runs(user_id, persona_id, suite_name, created_at);
            CREATE INDEX IF NOT EXISTS idx_llm_call_logs_task
                ON llm_call_logs(task, status, created_at);
            """
        )
        _ensure_column(db, "memory_links", "user_id", "INTEGER")
        for table in ("memory_facts", "memory_relations"):
            _ensure_column(db, table, "priority", "TEXT NOT NULL DEFAULT 'normal'")
            _ensure_column(db, table, "locked", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(db, table, "decay_score", "REAL NOT NULL DEFAULT 0")
            _ensure_column(db, table, "access_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "personas", "memory_profile_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(db, "conversations", "pinned_at", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "conversations", "last_read_message_id", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "conversations", "last_read_at", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "messages", "reply_status", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "messages", "reply_error", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "messages", "client_message_id", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "expression_preferences", "mode", "TEXT NOT NULL DEFAULT 'normal'")
        _ensure_column(db, "expression_asset_settings", "cooldown_turns", "INTEGER NOT NULL DEFAULT -1")
        _ensure_column(db, "expression_asset_settings", "lifecycle_status", "TEXT NOT NULL DEFAULT 'active'")
        _ensure_column(db, "expression_asset_settings", "asset_kind", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "expression_asset_settings", "media_url", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "expression_asset_settings", "thumbnail_url", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "expression_asset_settings", "alt_text", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "expression_asset_settings", "media_review_status", "TEXT NOT NULL DEFAULT 'approved'")
        _ensure_column(db, "expression_asset_settings", "media_review_note", "TEXT NOT NULL DEFAULT ''")
        for table in ("personas", "persona_versions"):
            _ensure_column(db, table, "psychological_profile_json", "TEXT NOT NULL DEFAULT '{}'")
            _ensure_column(db, table, "psychological_fit_notes", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(db, table, "appearance_description", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(db, table, "desired_image", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(db, table, "growth_notes", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "persona_versions", "change_type", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "persona_versions", "source_suggestion_id", "INTEGER")
        _ensure_column(db, "persona_versions", "change_notes_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(db, "persona_revision_suggestions", "base_version", "INTEGER")
        _ensure_column(db, "persona_revision_suggestions", "origin", "TEXT NOT NULL DEFAULT 'manual'")
        _ensure_column(db, "persona_revision_suggestions", "trigger_message_id", "INTEGER")
        _ensure_column(db, "persona_revision_suggestions", "trigger_memory_uids_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(db, "persona_revision_suggestions", "applied_version", "INTEGER")
        _ensure_column(db, "persona_revision_suggestions", "decided_at", "INTEGER")
        _ensure_column(db, "persona_revision_suggestions", "decided_by_user_id", "INTEGER")
        _ensure_column(db, "persona_revision_suggestions", "decision_actor", "TEXT NOT NULL DEFAULT 'admin'")
        _ensure_column(db, "persona_revision_suggestions", "decision_note", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "persona_growth_feedback", "resolved_at", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "persona_growth_feedback", "resolved_by_user_id", "INTEGER")
        _ensure_column(db, "persona_growth_feedback", "resolution_note", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "persona_growth_feedback", "detail_text", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "persona_growth_requests", "updated_at", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "persona_growth_requests", "withdrawn_at", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "persona_growth_requests", "memory_uids_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(db, "persona_growth_requests", "request_origin", "TEXT NOT NULL DEFAULT 'direct_entry'")
        _ensure_column(db, "persona_growth_requests", "source_reviewed_version", "INTEGER")
        _ensure_column(db, "persona_growth_requests", "source_message_id", "INTEGER")
        _ensure_column(db, "persona_growth_requests", "deactivation_actor", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "persona_growth_requests", "deactivation_reason", "TEXT NOT NULL DEFAULT ''")
        ts = now_ts()
        db.execute(
            """
            UPDATE persona_revision_suggestions
            SET status = 'dismissed', decided_at = ?, decision_actor = 'adaptive_runtime',
                decision_note = '已切换为运行时自动适配：用户相处偏好直接进入聊天指导，不再等待人工审核。',
                updated_at = ?
            WHERE origin = 'profile_request' AND status = 'pending'
            """,
            (ts, ts),
        )
        db.execute(
            """
            UPDATE persona_versions
            SET change_type = CASE reason
                WHEN 'initial forge' THEN 'initial_forge'
                WHEN 'persona profile update' THEN 'user_profile_update'
                ELSE change_type
            END
            WHERE change_type = ''
            """
        )
        _ensure_column(db, "memory_state", "persona_scope", "TEXT NOT NULL DEFAULT 'global'")
        _ensure_column(db, "users", "role", "TEXT NOT NULL DEFAULT 'user'")
        _ensure_column(db, "users", "is_guest", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "users", "guest_expires_at", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(db, "user_profiles", "gender", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "user_insights", "inferred_profile_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(db, "user_insights", "topic_model_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(db, "user_insights", "guidance_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(db, "user_insights", "discovery_dimensions_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(db, "user_insights", "curiosity_feedback_json", "TEXT NOT NULL DEFAULT '{}'")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_state_unique_scope ON memory_state(user_id, persona_scope, key)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_memory_links_user ON memory_links(user_id)")
        db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_client_message_id
            ON messages(user_id, client_message_id)
            WHERE role = 'user' AND client_message_id <> ''
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_group_messages_client_message_id
            ON group_messages(user_id, group_conversation_id, client_message_id)
            WHERE client_message_id <> ''
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_group_member_relations_group
            ON group_member_relations(user_id, group_conversation_id, persona_id)
            """
        )
        db.execute(
            """
            CREATE TRIGGER IF NOT EXISTS trg_memory_links_delete_user
            AFTER DELETE ON users
            BEGIN
                DELETE FROM memory_links WHERE user_id = OLD.id;
            END
            """
        )


def next_memory_uid(prefix: str, ts: int | None = None) -> str:
    ts = ts or now_ts()
    safe_prefix = "".join(ch for ch in prefix.upper() if ch.isalnum() or ch == "-")[:32]
    with get_db() as db:
        row = db.execute("SELECT value FROM memory_counters WHERE prefix = ?", (safe_prefix,)).fetchone()
        value = int(row["value"]) + 1 if row else 1
        if row:
            db.execute("UPDATE memory_counters SET value = ? WHERE prefix = ?", (value, safe_prefix))
        else:
            db.execute("INSERT INTO memory_counters (prefix, value) VALUES (?, ?)", (safe_prefix, value))

    if safe_prefix == "EVT" or safe_prefix == "EP":
        day = time.strftime("%Y%m%d", time.localtime(ts))
        return f"{safe_prefix}-{day}-{value:06d}"
    return f"{safe_prefix}-{value:06d}"


def _ensure_column(db: sqlite3.Connection, table: str, column: str, column_sql: str) -> None:
    existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in existing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_sql}")
