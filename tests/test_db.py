"""Tests for SQLite mapping and channel config persistence."""

import asyncio
import aiosqlite
from pathlib import Path


def run(coro):
    return asyncio.run(coro)


def configure_db(tmp_path: Path):
    import zo_discord.db as db

    db.DB_PATH = tmp_path / "threads.db"
    run(db.init_db())
    return db


class TestThreadMappings:
    def test_conversation_id_persists_and_updates(self, tmp_path):
        db = configure_db(tmp_path)

        run(
            db.save_mapping(
                thread_id="thread-1",
                conversation_id="conv-1",
                channel_id="channel-1",
                guild_id="guild-1",
                thread_name="Test Thread",
            )
        )
        assert run(db.get_conversation_id("thread-1")) == "conv-1"

        run(db.update_conversation_id("thread-1", "conv-2"))
        assert run(db.get_conversation_id("thread-1")) == "conv-2"

        mapping = run(db.get_mapping_by_conversation("conv-2"))
        assert mapping["thread_id"] == "thread-1"
        assert mapping["thread_name"] == "Test Thread"

    def test_migration_adds_honcho_session_key_column(self, tmp_path):
        db = configure_db(tmp_path)

        async def get_columns():
            async with aiosqlite.connect(db.DB_PATH) as conn:
                cursor = await conn.execute("PRAGMA table_info(thread_mappings)")
                return [row[1] for row in await cursor.fetchall()]

        columns = run(get_columns())
        assert "honcho_session_key" in columns

    def test_honcho_session_key_round_trip_and_update(self, tmp_path):
        db = configure_db(tmp_path)

        run(
            db.save_mapping(
                thread_id="thread-1",
                conversation_id="conv-1",
                honcho_session_key="discord-thread-1",
                channel_id="channel-1",
                guild_id="guild-1",
                thread_name="Test Thread",
            )
        )
        assert run(db.get_honcho_session_key("thread-1")) == "discord-thread-1"

        run(db.update_honcho_session_key("thread-1", "conv-1"))
        assert run(db.get_honcho_session_key("thread-1")) == "conv-1"

    def test_resolve_honcho_session_key_backfills_from_conversation_id(self, tmp_path):
        db = configure_db(tmp_path)

        run(
            db.save_mapping(
                thread_id="thread-1",
                conversation_id="conv-1",
                channel_id="channel-1",
                guild_id="guild-1",
                thread_name="Legacy Thread",
            )
        )

        key = run(db.resolve_honcho_session_key("thread-1"))

        assert key == "conv-1"
        assert run(db.get_honcho_session_key("thread-1")) == "conv-1"

    def test_resolve_honcho_session_key_backfills_from_thread_id_when_conversation_empty(self, tmp_path):
        db = configure_db(tmp_path)

        run(
            db.save_mapping(
                thread_id="thread-2",
                conversation_id="",
                channel_id="channel-1",
                guild_id="guild-1",
                thread_name="Legacy Thread",
            )
        )

        key = run(db.resolve_honcho_session_key("thread-2"))

        assert key == "discord-thread-thread-2"
        assert run(db.get_honcho_session_key("thread-2")) == "discord-thread-thread-2"


class TestChannelConfig:
    def test_round_trip_serializes_lists_and_flags(self, tmp_path):
        db = configure_db(tmp_path)

        run(
            db.set_channel_config(
                "channel-1",
                instructions="Focus on Hermes",
                memory_paths=["Knowledge/memory/a.md", "Knowledge/memory/b.md"],
                reasoning="high",
                max_iterations=12,
                skip_memory=True,
                skip_context=True,
                enabled_toolsets=["web", "file"],
                disabled_toolsets=["rl"],
                message_mode="interrupt",
                backend="hermes",
            )
        )

        config = run(db.get_channel_config("channel-1"))
        assert config["instructions"] == "Focus on Hermes"
        assert config["memory_paths"] == ["Knowledge/memory/a.md", "Knowledge/memory/b.md"]
        assert config["reasoning"] == "high"
        assert config["max_iterations"] == 12
        assert bool(config["skip_memory"]) is True
        assert bool(config["skip_context"]) is True
        assert config["enabled_toolsets"] == ["web", "file"]
        assert config["disabled_toolsets"] == ["rl"]
        assert config["message_mode"] == "interrupt"
        assert config["backend"] == "hermes"

    def test_accepts_json_string_lists_for_config_endpoint_payloads(self, tmp_path):
        db = configure_db(tmp_path)

        run(
            db.set_channel_config(
                "channel-1",
                memory_paths='["Knowledge/memory/x.md"]',
                enabled_toolsets='["web", "terminal"]',
                disabled_toolsets='["rl"]',
            )
        )

        config = run(db.get_channel_config("channel-1"))
        assert config["memory_paths"] == ["Knowledge/memory/x.md"]
        assert config["enabled_toolsets"] == ["web", "terminal"]
        assert config["disabled_toolsets"] == ["rl"]

    def test_rejects_bad_reasoning(self, tmp_path):
        db = configure_db(tmp_path)

        try:
            run(db.set_channel_config("channel-1", reasoning="turbo"))
        except ValueError as e:
            assert "reasoning" in str(e)
        else:
            raise AssertionError("Expected ValueError")

    def test_rejects_bad_max_iterations(self, tmp_path):
        db = configure_db(tmp_path)

        try:
            run(db.set_channel_config("channel-1", max_iterations=0))
        except ValueError as e:
            assert "max_iterations" in str(e)
        else:
            raise AssertionError("Expected ValueError")

    def test_rejects_malformed_list_json(self, tmp_path):
        db = configure_db(tmp_path)

        try:
            run(db.set_channel_config("channel-1", enabled_toolsets='["web"'))
        except ValueError as e:
            assert "enabled_toolsets" in str(e)
        else:
            raise AssertionError("Expected ValueError")

    def test_rejects_non_list_values(self, tmp_path):
        db = configure_db(tmp_path)

        try:
            run(db.set_channel_config("channel-1", disabled_toolsets={"nope": True}))
        except ValueError as e:
            assert "disabled_toolsets" in str(e)
        else:
            raise AssertionError("Expected ValueError")

    def test_rejects_bad_message_mode(self, tmp_path):
        db = configure_db(tmp_path)

        try:
            run(db.set_channel_config("channel-1", message_mode="burst"))
        except ValueError as e:
            assert "message_mode" in str(e)
        else:
            raise AssertionError("Expected ValueError")

    def test_rejects_bad_backend(self, tmp_path):
        db = configure_db(tmp_path)

        try:
            run(db.set_channel_config("channel-1", backend="local"))
        except ValueError as e:
            assert "backend" in str(e)
        else:
            raise AssertionError("Expected ValueError")

    def test_rejects_non_boolean_skip_flags(self, tmp_path):
        db = configure_db(tmp_path)

        for field_name in ("skip_memory", "skip_context"):
            try:
                run(db.set_channel_config("channel-1", **{field_name: 1}))
            except ValueError as e:
                assert field_name in str(e)
            else:
                raise AssertionError("Expected ValueError")

    def test_rejects_bad_memory_paths_shapes(self, tmp_path):
        db = configure_db(tmp_path)

        try:
            run(db.set_channel_config("channel-1", memory_paths='{"path":"nope"}'))
        except ValueError as e:
            assert "memory_paths" in str(e)
        else:
            raise AssertionError("Expected ValueError")

        try:
            run(db.set_channel_config("channel-1", memory_paths=["ok", 3]))
        except ValueError as e:
            assert "memory_paths" in str(e)
        else:
            raise AssertionError("Expected ValueError")
