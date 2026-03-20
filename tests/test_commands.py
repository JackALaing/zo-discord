"""Tests for slash command gating and backend-aware UX."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

try:
    import discord

    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False


def run(coro):
    return asyncio.run(coro)


class FakeBot:
    def __init__(self):
        self.commands = {}
        self._last_user_messages = {}

    def slash_command(self, name, description):
        def decorator(func):
            self.commands[name] = func
            return func
        return decorator


class FakeFollowup:
    def __init__(self):
        self.calls = []

    async def send(self, content, ephemeral=False):
        self.calls.append({"content": content, "ephemeral": ephemeral})


class FakeCtx:
    def __init__(self, channel):
        self.channel = channel
        self.responses = []
        self.deferred = []
        self.followup = FakeFollowup()

    async def respond(self, content, **kwargs):
        self.responses.append({"content": content, **kwargs})

    async def defer(self, **kwargs):
        self.deferred.append(kwargs)


def register_commands():
    from zo_discord.commands import setup_commands

    bot = FakeBot()
    setup_commands(bot)
    return bot.commands


@pytest.mark.skipif(not HAS_DISCORD, reason="py-cord not installed")
class TestCommands:
    def test_hermes_only_commands_gate_in_zo_channels(self):
        commands = register_commands()
        hermes_only = [
            "stop",
            "undo",
            "retry",
            "status",
            "usage",
            "compress",
            "queue",
            "interrupt",
            "tools",
            "max-iterations",
            "skip-memory",
            "skip-context",
            "compression-threshold",
        ]
        ctx = FakeCtx(SimpleNamespace(id=10, name="general"))

        async def fake_get_config(_channel_id):
            return {"backend": "zo"}

        with patch("zo_discord.commands.get_channel_config", fake_get_config):
            for name in hermes_only:
                ctx.responses.clear()
                func = commands[name]
                kwargs = {}
                if name == "max-iterations":
                    kwargs["value"] = None
                if name == "compression-threshold":
                    kwargs["value"] = None
                run(func(ctx, **kwargs))
                assert ctx.responses[-1]["content"] == "This command is only available in Hermes channels."

    def test_help_hides_hermes_sections_in_zo_channels(self):
        commands = register_commands()
        ctx = FakeCtx(SimpleNamespace(id=10, name="general"))

        async def fake_get_config(_channel_id):
            return {"backend": "zo"}

        with patch("zo_discord.commands.get_channel_config", fake_get_config):
            run(commands["help"](ctx))

        content = ctx.responses[-1]["content"]
        assert "Backend: Zo" in content
        assert "**Session Management**" not in content
        assert "`/reasoning`" not in content

    def test_help_shows_hermes_sections_in_hermes_threads(self):
        commands = register_commands()
        parent = SimpleNamespace(id=10, name="general")
        thread_type = type("FakeThread", (), {})
        thread = thread_type()
        thread.id = 99
        thread.name = "Hermes Thread"
        thread.parent = parent
        ctx = FakeCtx(thread)

        async def fake_get_config(channel_id):
            if channel_id == "99":
                return None
            return {"backend": "hermes", "message_mode": "interrupt"}

        with patch("zo_discord.commands.discord.Thread", thread_type), patch(
            "zo_discord.commands.get_channel_config", fake_get_config
        ), patch("zo_discord.commands.get_conversation_id", AsyncMock(return_value="conv-1")):
            run(commands["help"](ctx))

        content = ctx.responses[-1]["content"]
        assert "Backend: Hermes" in content
        assert "**Hermes Config**" in content
        assert "**Session Management**" in content
        assert "currently: interrupt" in content
        assert "Session: `conv-1`" in content

    def test_link_shows_session_id_for_hermes_threads(self):
        commands = register_commands()
        parent = SimpleNamespace(id=10, name="general")
        thread_type = type("FakeThread", (), {})
        thread = thread_type()
        thread.id = 99
        thread.name = "Hermes Thread"
        thread.parent = parent
        ctx = FakeCtx(thread)

        with patch("zo_discord.commands.discord.Thread", thread_type), patch(
            "zo_discord.commands.get_conversation_id", AsyncMock(return_value="sess-123")
        ), patch("zo_discord.commands.get_channel_config", AsyncMock(return_value={"backend": "hermes"})):
            run(commands["link"](ctx))

        assert "Hermes session" in ctx.responses[-1]["content"]
        assert "sess-123" in ctx.responses[-1]["content"]

    def test_link_returns_zo_url_for_zo_threads(self):
        commands = register_commands()
        parent = SimpleNamespace(id=10, name="general")
        thread_type = type("FakeThread", (), {})
        thread = thread_type()
        thread.id = 99
        thread.name = "Zo Thread"
        thread.parent = parent
        ctx = FakeCtx(thread)

        with patch.dict(os.environ, {"ZO_USER": "jackal"}, clear=False), patch(
            "zo_discord.commands.discord.Thread", thread_type
        ), patch("zo_discord.commands.get_conversation_id", AsyncMock(return_value="con_123")), patch(
            "zo_discord.commands.get_channel_config", AsyncMock(return_value={"backend": "zo"})
        ):
            run(commands["link"](ctx))

        assert "https://jackal.zo.computer/?chat=con_123" in ctx.responses[-1]["content"]

    def test_tips_responds_with_shortened_message(self):
        commands = register_commands()
        ctx = FakeCtx(SimpleNamespace(id=10, name="general"))

        run(commands["tips"](ctx))

        content = ctx.responses[-1]["content"]
        assert content.startswith("**Tips & Tricks**")
        assert len(content) < 2000

    def test_model_and_persona_show_channel_defaults(self):
        commands = register_commands()
        ctx = FakeCtx(SimpleNamespace(id=10, name="general"))

        async def fake_get_config(_channel_id):
            return {"model": "byok:test-opus-id", "persona_id": "per_test_pirate"}

        with patch("zo_discord.commands.get_channel_config", fake_get_config):
            run(commands["model"](ctx))
            run(commands["persona"](ctx))

        assert "**#general default:** opus (`byok:test-opus-id`)" in ctx.responses[0]["content"]
        assert "**#general default:** pirate (`per_test_pirate`)" in ctx.responses[1]["content"]

    def test_reasoning_command_works_in_non_hermes_channel(self):
        commands = register_commands()
        ctx = FakeCtx(SimpleNamespace(id=10, name="general"))

        with patch("zo_discord.commands.get_channel_config", AsyncMock(return_value={"backend": "zo"})), patch(
            "zo_discord.commands.set_channel_config", AsyncMock()
        ) as set_cfg:
            run(commands["reasoning"](ctx, level="low"))

        set_cfg.assert_awaited_once_with("10", reasoning="low")
        assert "Reasoning effort set to **low**" in ctx.responses[-1]["content"]

    def test_cli_command_mentions_conv_id_usage(self):
        commands = register_commands()
        ctx = FakeCtx(SimpleNamespace(id=10, name="general"))

        run(commands["cli"](ctx))

        content = ctx.responses[-1]["content"]
        assert "prefer explicit `--conv-id`" in content
        assert "zo-discord --conv-id <id> rename" in content
