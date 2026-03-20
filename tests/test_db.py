"""Tests for SQLite mapping and channel config persistence."""

import asyncio
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
