"""
Thread-Conversation mapping database.
Maps Discord thread IDs to Zo conversation IDs for session persistence.
"""

import aiosqlite
import os
from datetime import datetime
from pathlib import Path

from zo_discord import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "data" / "threads.db"


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
        try:
            await db.execute("ALTER TABLE thread_mappings ADD COLUMN status TEXT DEFAULT NULL")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE thread_mappings ADD COLUMN watched INTEGER DEFAULT 1")
        except Exception:
            pass
        try:
            await db.execute("UPDATE thread_mappings SET watched = 0 WHERE manually_archived = 1")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE channel_config ADD COLUMN model TEXT DEFAULT NULL")
        except Exception:
            pass
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
        import json
        config["memory_paths"] = json.loads(config.get("memory_paths") or "[]")
        return config


async def set_channel_config(channel_id: str, **kwargs) -> None:
    import json
    now = datetime.utcnow().isoformat()
    
    existing = await get_channel_config(channel_id)
    
    if "memory_paths" in kwargs and isinstance(kwargs["memory_paths"], list):
        kwargs["memory_paths"] = json.dumps(kwargs["memory_paths"])
    
    async with aiosqlite.connect(DB_PATH) as db:
        if existing:
            sets = []
            vals = []
            for key in ("instructions", "memory_paths", "persona_id", "model"):
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
                INSERT INTO channel_config (channel_id, instructions, memory_paths, persona_id, model, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                channel_id,
                kwargs.get("instructions"),
                kwargs.get("memory_paths", "[]"),
                kwargs.get("persona_id"),
                kwargs.get("model"),
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
