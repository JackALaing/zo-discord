"""
Thread-Conversation mapping database.
Maps Discord thread IDs to Zo conversation IDs for session persistence.
"""

import json
import aiosqlite
import os
from datetime import datetime
from pathlib import Path

from zo_discord import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "data" / "threads.db"
VALID_REASONING_LEVELS = {"off", "low", "medium", "high"}
VALID_BACKENDS = {"zo", "hermes"}
VALID_MESSAGE_MODES = {"queue", "interrupt"}
JSON_LIST_FIELDS = ("memory_paths", "enabled_toolsets", "disabled_toolsets")
CHANNEL_CONFIG_FIELDS = (
    "instructions",
    "memory_paths",
    "persona_id",
    "model",
    "buffer_seconds",
    "backend",
    "reasoning",
    "max_iterations",
    "skip_memory",
    "skip_context",
    "enabled_toolsets",
    "disabled_toolsets",
    "message_mode",
)
CHANNEL_CONFIG_MIGRATIONS = (
    "ALTER TABLE thread_mappings ADD COLUMN status TEXT DEFAULT NULL",
    "ALTER TABLE thread_mappings ADD COLUMN watched INTEGER DEFAULT 1",
    "ALTER TABLE channel_config ADD COLUMN model TEXT DEFAULT NULL",
    "ALTER TABLE channel_config ADD COLUMN buffer_seconds REAL DEFAULT NULL",
    "ALTER TABLE channel_config ADD COLUMN backend TEXT DEFAULT NULL",
    "ALTER TABLE channel_config ADD COLUMN reasoning TEXT DEFAULT NULL",
    "ALTER TABLE channel_config ADD COLUMN max_iterations INTEGER DEFAULT NULL",
    "ALTER TABLE channel_config ADD COLUMN skip_memory BOOLEAN DEFAULT FALSE",
    "ALTER TABLE channel_config ADD COLUMN skip_context BOOLEAN DEFAULT FALSE",
    "ALTER TABLE channel_config ADD COLUMN enabled_toolsets TEXT DEFAULT NULL",
    "ALTER TABLE channel_config ADD COLUMN disabled_toolsets TEXT DEFAULT NULL",
    "ALTER TABLE channel_config ADD COLUMN message_mode TEXT DEFAULT 'queue'",
)


def _parse_json_list(value, field_name: str):
    if value is None:
        return None
    if isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            raise ValueError(f"{field_name} must be a list of strings")
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as e:
            raise ValueError(f"{field_name} must be valid JSON: {e.msg}") from e
        if not isinstance(parsed, list):
            raise ValueError(f"{field_name} must decode to a JSON list")
        if not all(isinstance(item, str) for item in parsed):
            raise ValueError(f"{field_name} must be a list of strings")
        return parsed
    raise ValueError(f"{field_name} must be a list or JSON-encoded list")


def _validate_choice(value, field_name: str, valid_values: set[str], *, allow_none: bool = True) -> None:
    if value is None and allow_none:
        return
    if value not in valid_values:
        raise ValueError(f"{field_name} must be one of {sorted(valid_values)}")


def _validate_bool(value, field_name: str) -> None:
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")


def _serialize_json_list_fields(values: dict) -> dict:
    serialized = dict(values)
    for field_name in JSON_LIST_FIELDS:
        if field_name in serialized and isinstance(serialized[field_name], list):
            serialized[field_name] = json.dumps(serialized[field_name])
    return serialized


def _validate_channel_config_kwargs(kwargs: dict) -> dict:
    normalized = dict(kwargs)

    if "reasoning" in normalized:
        _validate_choice(normalized["reasoning"], "reasoning", VALID_REASONING_LEVELS)

    if "backend" in normalized:
        _validate_choice(normalized["backend"], "backend", VALID_BACKENDS)

    if "message_mode" in normalized:
        _validate_choice(
            normalized["message_mode"],
            "message_mode",
            VALID_MESSAGE_MODES,
            allow_none=False,
        )

    if "max_iterations" in normalized:
        value = normalized["max_iterations"]
        if value is not None:
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError("max_iterations must be an integer >= 1")

    if "skip_memory" in normalized:
        _validate_bool(normalized["skip_memory"], "skip_memory")

    if "skip_context" in normalized:
        _validate_bool(normalized["skip_context"], "skip_context")

    for field_name in JSON_LIST_FIELDS:
        if field_name in normalized:
            normalized[field_name] = _parse_json_list(normalized[field_name], field_name)

    return normalized


async def _apply_migrations(db) -> None:
    for statement in CHANNEL_CONFIG_MIGRATIONS:
        try:
            await db.execute(statement)
        except Exception:
            pass


async def init_db():
    """Initialize the database schema."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS thread_mappings (
                thread_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                guild_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_activity TEXT NOT NULL,
                thread_name TEXT
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_conversation_id 
            ON thread_mappings(conversation_id)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_channel_id 
            ON thread_mappings(channel_id)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channel_config (
                channel_id TEXT PRIMARY KEY,
                instructions TEXT,
                persona_id TEXT,
                memory_paths TEXT DEFAULT '[]',
                updated_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_channel_config
            ON channel_config(channel_id)
        """)
        await _apply_migrations(db)
        await db.commit()


async def save_mapping(
    thread_id: str,
    conversation_id: str,
    channel_id: str,
    guild_id: str,
    thread_name: str = None
):
    """Save a thread-to-conversation mapping."""
    now = datetime.utcnow().isoformat()
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO thread_mappings 
            (thread_id, conversation_id, channel_id, guild_id, created_at, last_activity, thread_name)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (thread_id, conversation_id, channel_id, guild_id, now, now, thread_name))
        await db.commit()


async def get_conversation_id(thread_id: str) -> str | None:
    """Get the Zo conversation ID for a Discord thread."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT conversation_id FROM thread_mappings WHERE thread_id = ?",
            (thread_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def update_activity(thread_id: str):
    """Update the last activity timestamp for a thread."""
    now = datetime.utcnow().isoformat()
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE thread_mappings SET last_activity = ? WHERE thread_id = ?",
            (now, thread_id)
        )
        await db.commit()


async def update_thread_name(thread_id: str, name: str):
    """Update the stored thread name."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE thread_mappings SET thread_name = ? WHERE thread_id = ?",
            (name, thread_id)
        )
        await db.commit()


async def update_conversation_id(thread_id: str, conversation_id: str):
    """Update the conversation ID for a thread (used when starting a session on first reply)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE thread_mappings SET conversation_id = ? WHERE thread_id = ?",
            (conversation_id, thread_id)
        )
        await db.commit()


async def get_active_threads(guild_id: str = None, limit: int = 50) -> list[dict]:
    """Get recently active threads for renaming."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        
        query = """
            SELECT thread_id, conversation_id, channel_id, guild_id, 
                   created_at, last_activity, thread_name
            FROM thread_mappings 
        """
        params = []
        
        if guild_id:
            query += " WHERE guild_id = ?"
            params.append(guild_id)
        
        query += " ORDER BY last_activity DESC LIMIT ?"
        params.append(limit)
        
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        
        return [dict(row) for row in rows]


async def get_mapping_by_conversation(conversation_id: str) -> dict | None:
    """Get thread mapping by Zo conversation ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM thread_mappings WHERE conversation_id = ?",
            (conversation_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_channel_config(channel_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM channel_config WHERE channel_id = ?",
            (channel_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        config = dict(row)
        config["memory_paths"] = json.loads(config.get("memory_paths") or "[]")
        config["enabled_toolsets"] = json.loads(config.get("enabled_toolsets") or "null")
        config["disabled_toolsets"] = json.loads(config.get("disabled_toolsets") or "null")
        return config


async def set_channel_config(channel_id: str, **kwargs) -> None:
    now = datetime.utcnow().isoformat()
    kwargs = _serialize_json_list_fields(_validate_channel_config_kwargs(kwargs))
    existing = await get_channel_config(channel_id)

    async with aiosqlite.connect(DB_PATH) as db:
        if existing:
            sets = []
            vals = []
            for key in CHANNEL_CONFIG_FIELDS:
                if key in kwargs:
                    sets.append(f"{key} = ?")
                    vals.append(kwargs[key])
            if sets:
                sets.append("updated_at = ?")
                vals.append(now)
                vals.append(channel_id)
                await db.execute(
                    f"UPDATE channel_config SET {', '.join(sets)} WHERE channel_id = ?",
                    vals
                )
        else:
            await db.execute("""
                INSERT INTO channel_config (channel_id, instructions, memory_paths, persona_id, model, buffer_seconds, backend,
                    reasoning, max_iterations, skip_memory, skip_context, enabled_toolsets, disabled_toolsets, message_mode, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                channel_id,
                kwargs.get("instructions"),
                kwargs.get("memory_paths", "[]"),
                kwargs.get("persona_id"),
                kwargs.get("model"),
                kwargs.get("buffer_seconds"),
                kwargs.get("backend"),
                kwargs.get("reasoning"),
                kwargs.get("max_iterations"),
                kwargs.get("skip_memory", False),
                kwargs.get("skip_context", False),
                kwargs.get("enabled_toolsets"),
                kwargs.get("disabled_toolsets"),
                kwargs.get("message_mode", "queue"),
                now
            ))
        await db.commit()


async def delete_channel_config(channel_id: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM channel_config WHERE channel_id = ?", (channel_id,))
        await db.commit()


async def update_thread_status(thread_id: str, status: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE thread_mappings SET status = ? WHERE thread_id = ?",
            (status, thread_id)
        )
        await db.commit()


async def get_thread_status(thread_id: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT status FROM thread_mappings WHERE thread_id = ?",
            (thread_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def set_watched(thread_id: str, value: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE thread_mappings SET watched = ? WHERE thread_id = ?",
            (1 if value else 0, thread_id)
        )
        await db.commit()


async def is_watched(thread_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT watched FROM thread_mappings WHERE thread_id = ?",
            (thread_id,)
        )
        row = await cursor.fetchone()
        return bool(row[0]) if row else False


async def get_all_watched_threads() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT thread_id, conversation_id, channel_id, guild_id, last_activity "
            "FROM thread_mappings WHERE watched = 1"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
