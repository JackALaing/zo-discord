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
        self._thread_digest_needed = set()
        self.mark_thread_cancelled = lambda _thread_id: None

    def slash_command(self, name, description):
        def decorator(func):
            self.commands[name] = func
            return func
        return decorator


class FakeFollowup:
    def __init__(self):
        self.calls = []

    async def send(self, content, **kwargs):
        self.calls.append({"content": content, **kwargs})


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


class FakeThread:
    def __init__(self, thread_id=99, name="Hermes Thread", parent=None, history_messages=None):
        self.id = thread_id
        self.name = name
        self.parent = parent or SimpleNamespace(id=10, name="general")
        self._history_messages = history_messages or []

    async def history(self, limit=50):
        for msg in self._history_messages[:limit]:
            yield msg

    async def send(self, *args, **kwargs):
        return None


def register_commands():
    from zo_discord.commands import setup_commands

    bot = FakeBot()
    setup_commands(bot)
    return bot.commands


def register_commands_with_bot():
    from zo_discord.commands import setup_commands

    bot = FakeBot()
    bot.retry_in_thread = AsyncMock()
    setup_commands(bot)
    return bot, bot.commands


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

        assert "**#general default:** opus (`byok:test-opus-id`)" in ctx.followup.calls[0]["content"]
        assert "**#general default:** pirate (`per_test_pirate`)" in ctx.followup.calls[1]["content"]

    def test_reasoning_command_is_gated_in_non_hermes_channel(self):
        commands = register_commands()
        ctx = FakeCtx(SimpleNamespace(id=10, name="general"))

        with patch("zo_discord.commands.get_channel_config", AsyncMock(return_value={"backend": "zo"})), patch(
            "zo_discord.commands.set_channel_config", AsyncMock()
        ) as set_cfg:
            run(commands["reasoning"](ctx, level="low"))

        set_cfg.assert_not_awaited()
        assert ctx.responses[-1]["content"] == "This command is only available in Hermes channels."

    def test_cli_command_mentions_conv_id_usage(self):
        commands = register_commands()
        ctx = FakeCtx(SimpleNamespace(id=10, name="general"))

        run(commands["cli"](ctx))

        content = ctx.responses[-1]["content"]
        assert "prefer explicit `--conv-id`" in content
        assert "zo-discord --conv-id <id> rename" in content

    def test_hermes_config_commands_show_and_update_values(self):
        commands = register_commands()
        parent = SimpleNamespace(id=10, name="general")
        ctx = FakeCtx(FakeThread(parent=parent))

        async def fake_get_config(_channel_id):
            return {
                "backend": "hermes",
                "enabled_toolsets": ["web", "file"],
                "disabled_toolsets": ["rl"],
                "max_iterations": 9,
                "skip_memory": False,
                "skip_context": True,
            }

        with patch("zo_discord.commands.discord.Thread", FakeThread), patch(
            "zo_discord.commands.get_channel_config", AsyncMock(side_effect=fake_get_config)
        ), patch("zo_discord.commands.set_channel_config", AsyncMock()) as set_cfg, patch(
            "zo_discord.commands._read_hermes_config", return_value={"compression": {"threshold": 0.6}, "agent": {"max_turns": 200}}
        ), patch("zo_discord.commands._write_hermes_config") as write_cfg:
            run(commands["tools"](ctx))
            run(commands["max-iterations"](ctx))
            run(commands["max-iterations"](ctx, value=12))
            run(commands["skip-memory"](ctx))
            run(commands["skip-context"](ctx))
            run(commands["compression-threshold"](ctx))
            run(commands["compression-threshold"](ctx, value=0.8))
            run(commands["queue"](ctx))
            run(commands["interrupt"](ctx))

        assert "Enabled: `web, file`" in ctx.responses[0]["content"]
        assert "Disabled: `rl`" in ctx.responses[0]["content"]
        assert "**Max iterations:** 9" in ctx.responses[1]["content"]
        assert "Max iterations set to **12**" in ctx.responses[2]["content"]
        assert "**Skip memory:** on" in ctx.responses[3]["content"]
        assert "**Skip context:** off" in ctx.responses[4]["content"]
        assert "**Compression threshold:** 0.6" in ctx.responses[5]["content"]
        assert "Compression threshold set to **0.8**." in ctx.responses[6]["content"]
        assert "Queue mode" in ctx.followup.calls[0]["content"]
        assert "Interrupt mode" in ctx.followup.calls[1]["content"]
        set_calls = [call.kwargs for call in set_cfg.await_args_list]
        assert {"max_iterations": 12} in set_calls
        assert {"skip_memory": True} in set_calls
        assert {"skip_context": False} in set_calls
        assert {"message_mode": "queue"} in set_calls
        assert {"message_mode": "interrupt"} in set_calls
        write_cfg.assert_called_once()

    def test_session_commands_success_paths(self):
        fake_bot, commands = register_commands_with_bot()
        parent = SimpleNamespace(id=10, name="general")
        bot_user = object()
        bot_message = SimpleNamespace(author=bot_user, add_reaction=AsyncMock())
        user_message = SimpleNamespace(author=object(), add_reaction=AsyncMock())
        thread = FakeThread(parent=parent, history_messages=[bot_message, user_message])
        ctx = FakeCtx(thread)
        fake_bot._last_user_messages[str(thread.id)] = "retry this"
        fake_bot.user = bot_user

        async def fake_post(path, payload):
            if path == "/cancel":
                return 200, {"status": "cancelled"}
            if path == "/undo":
                return 200, {"removed_count": 2}
            if path == "/compress":
                return 200, {
                    "status": "compressed",
                    "session_id": "sess-2",
                    "previous_session_id": "sess-1",
                    "before": {"messages": 8, "tokens": 400},
                    "after": {"messages": 3, "tokens": 150},
                }
            raise AssertionError(path)

        async def fake_get(path, params=None):
            if path == "/status":
                return 200, {
                    "state": "running",
                    "model": "gpt-5.4",
                    "iterations_used": 2,
                    "iterations_max": 9,
                    "input_tokens": 120,
                    "output_tokens": 45,
                    "api_calls": 3,
                }
            if path == "/usage":
                return 200, {
                    "model": "gpt-5.4",
                    "input_tokens": 120,
                    "output_tokens": 45,
                    "cache_read_tokens": 10,
                    "cache_write_tokens": 5,
                    "total_tokens": 180,
                    "api_calls": 3,
                    "context_used_pct": 37.5,
                    "cost_usd": 0.1234,
                    "compression_count": 1,
                    "note": "No active agent — showing estimates only",
                }
            raise AssertionError(path)

        with patch("zo_discord.commands.discord.Thread", FakeThread), patch(
            "zo_discord.commands.get_channel_config", AsyncMock(return_value={"backend": "hermes"})
        ), patch(
            "zo_discord.commands.get_conversation_id", AsyncMock(return_value="sess-1")
        ), patch("zo_discord.commands._hermes_post", AsyncMock(side_effect=fake_post)), patch(
            "zo_discord.commands._hermes_get", AsyncMock(side_effect=fake_get)
        ), patch("zo_discord.commands.update_conversation_id", AsyncMock()) as update_conv, patch(
            "asyncio.create_task"
        ) as create_task, patch.object(fake_bot, "mark_thread_cancelled") as mark_cancelled:
            create_task.side_effect = lambda coro: coro.close() or None
            run(commands["stop"](ctx))
            run(commands["undo"](ctx))
            run(commands["retry"](ctx))
            run(commands["status"](ctx))
            run(commands["usage"](ctx))
            run(commands["compress"](ctx))

        assert ctx.responses[0]["content"] == "⏹️ Cancelled."
        mark_cancelled.assert_called_once_with(str(thread.id))
        assert "↩️ Last exchange undone. (2 messages removed)" == ctx.responses[1]["content"]
        assert bot_message.add_reaction.await_count == 2
        assert user_message.add_reaction.await_count == 2
        assert ctx.responses[2]["content"] == "🔄 Retrying last message..."
        create_task.assert_called_once()
        assert "🟢 **State:** running" in ctx.responses[3]["content"]
        assert "**Iterations:** 2/9" in ctx.responses[3]["content"]
        assert "Context: ████░░░░░░ 37.5%" in ctx.responses[4]["content"]
        assert "Cost: $0.1234" in ctx.responses[4]["content"]
        assert ctx.deferred == [{"ephemeral": True}]
        assert "🗜️ Context compressed." in ctx.followup.calls[0]["content"]
        assert "Session ID changed: `sess-2`" in ctx.followup.calls[0]["content"]
        update_conv.assert_awaited_once_with(str(thread.id), "sess-2")
        assert str(thread.id) in fake_bot._thread_digest_needed
