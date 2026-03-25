"""Tests for zo-discord bot helpers and Hermes-specific behavior."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

try:
    import discord
    from aiohttp.test_utils import make_mocked_request

    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False


def run(coro):
    return asyncio.run(coro)


def make_bot():
    from zo_discord.bot import ZoDiscordBot

    with patch.object(ZoDiscordBot, "__init__", lambda self: None):
        bot = ZoDiscordBot.__new__(ZoDiscordBot)
    bot.config = {}
    bot.zo = SimpleNamespace(
        backend="hermes",
        ask_stream=AsyncMock(),
        chunk_response=lambda text: [text],
    )
    bot._thinking_mode = "streaming"
    bot._auto_archive_override = True
    bot._inflight = {}
    bot._message_queues = {}
    bot._bundled_prefixes = {}
    bot._queue_drain_suppressed = set()
    bot._presaved_attachments = {}
    bot._last_user_messages = {}
    bot._pending_clarify = {}
    bot._thread_digest_needed = set()
    bot._cancelled_threads = set()
    bot.queued_renames = {}

    def mark_thread_cancelled(thread_id):
        bot._cancelled_threads.add(str(thread_id))

    def consume_thread_cancelled(thread_id):
        thread_id = str(thread_id)
        if thread_id in bot._cancelled_threads:
            bot._cancelled_threads.discard(thread_id)
            return True
        return False

    bot.mark_thread_cancelled = mark_thread_cancelled
    bot.consume_thread_cancelled = consume_thread_cancelled
    return bot


class FakeAuthor:
    def __init__(self, name="Jack", bot=False):
        self.display_name = name
        self.bot = bot
        self.id = 123 if not bot else 999


class FakeMessage:
    def __init__(self, content, author=None, attachments=None, message_id=1):
        self.content = content
        self.author = author or FakeAuthor()
        self.attachments = attachments or []
        self.id = message_id
        self.reference = None
        self.channel = None


class FakeParentChannel:
    def __init__(self, channel_id=456, name="hermes", topic="Topic text", pins=None):
        self.id = channel_id
        self.name = name
        self.topic = topic
        self._pins = pins or []

    async def pins(self):
        return list(self._pins)


class FakeThread:
    def __init__(self, thread_id=789, name="Current Thread", parent=None, pins=None, history_messages=None):
        self.id = thread_id
        self.name = name
        self.parent = parent or FakeParentChannel()
        self._pins = pins or []
        self._history_messages = history_messages or []
        self.sent = []

    async def pins(self):
        return list(self._pins)

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return SimpleNamespace(id=len(self.sent))

    async def history(self, limit=50, oldest_first=False):
        messages = list(self._history_messages[:limit])
        if not oldest_first:
            messages = list(reversed(messages))
        for msg in messages:
            yield msg


@pytest.mark.skipif(not HAS_DISCORD, reason="py-cord not installed")
class TestBotHelpers:
    def test_resolve_channel_defaults_exposes_hermes_params(self):
        bot = make_bot()

        async def fake_get_config(_channel_id):
            return {
                "model": "byok:test",
                "persona_id": "per_123",
                "backend": "hermes",
                "reasoning": "high",
                "max_iterations": 9,
                "skip_memory": 1,
                "skip_context": 1,
                "enabled_toolsets": ["web", "file"],
                "disabled_toolsets": ["rl"],
            }

        with patch("zo_discord.bot.get_channel_config", fake_get_config):
            model, persona, backend, hermes_params = run(bot.resolve_channel_defaults("123"))

        assert model == "byok:test"
        assert persona == "per_123"
        assert backend == "hermes"
        assert hermes_params == {
            "reasoning_effort": "high",
            "max_iterations": 9,
            "skip_memory": True,
            "skip_context": True,
            "enabled_toolsets": ["web", "file"],
            "disabled_toolsets": ["rl"],
        }

    def test_build_channel_context_includes_message_source_and_memory_paths(self):
        bot = make_bot()
        channel = FakeParentChannel(
            channel_id=111,
            name="ai-lab",
            topic="Fallback topic",
            pins=[SimpleNamespace(author=FakeAuthor("Pinned User"), content="Pinned note")],
        )

        async def fake_get_config(_channel_id):
            return {
                "instructions": "Use Hermes carefully.",
                "memory_paths": ["Knowledge/memory/projects/hermes.md"],
            }

        with patch("zo_discord.bot.get_channel_config", fake_get_config), patch(
            "zo_discord.bot.get_channel_dir", lambda _name: SimpleNamespace()
        ):
            context, file_paths = run(
                bot.build_channel_context(channel, include_source=True, conv_id="", backend="hermes")
            )

        assert "## Message Source" in context
        assert "This message is from Discord (channel: <#111>)" in context
        assert "## Channel Instructions" in context
        assert "Use Hermes carefully." in context
        assert "## Pinned Context" in context
        assert "Pinned note" in context
        assert "zo-discord --conv-id <session_id> rename" in context
        assert file_paths == ["/home/workspace/Knowledge/memory/projects/hermes.md"]

    def test_build_thread_context_uses_follow_up_message_source(self):
        bot = make_bot()
        parent = FakeParentChannel(channel_id=222, name="ops")
        thread = FakeThread(thread_id=333, name="❌ Current Thread", parent=parent)

        with patch("zo_discord.bot.get_channel_config", AsyncMock(return_value=None)), patch(
            "zo_discord.bot.get_channel_dir", lambda _name: SimpleNamespace()
        ):
            context, file_paths = run(
                bot.build_thread_context(thread, include_source=False, conv_id="conv-123", backend="hermes")
            )

        assert 'thread: "Current Thread"' in context
        assert "rename" in context
        assert "files /path/to/file" in context
        assert file_paths == []

    def test_build_thread_context_injects_one_shot_digest_after_compression(self):
        bot = make_bot()
        parent = FakeParentChannel(channel_id=222, name="ops")
        history_messages = [
            SimpleNamespace(author=FakeAuthor("Jack"), content="penguin"),
            SimpleNamespace(author=FakeAuthor("Zo", bot=True), content="penguin"),
            SimpleNamespace(author=FakeAuthor("Jack"), content="volcano"),
            SimpleNamespace(author=FakeAuthor("Zo", bot=True), content="volcano"),
            SimpleNamespace(author=FakeAuthor("Jack"), content="whale"),
            SimpleNamespace(author=FakeAuthor("Zo", bot=True), content="shark"),
        ]
        thread = FakeThread(thread_id=333, name="Current Thread", parent=parent, history_messages=history_messages)
        bot._thread_digest_needed.add(str(thread.id))

        with patch("zo_discord.bot.get_channel_config", AsyncMock(return_value=None)), patch(
            "zo_discord.bot.get_channel_dir", lambda _name: SimpleNamespace()
        ):
            first_context, _ = run(
                bot.build_thread_context(thread, include_source=False, conv_id="conv-123", backend="hermes")
            )
            second_context, _ = run(
                bot.build_thread_context(thread, include_source=False, conv_id="conv-123", backend="hermes")
            )

        assert "## Earlier Thread Summary" in first_context
        assert "- Jack: penguin" in first_context
        assert "- Zo: volcano" in first_context
        assert "## Earlier Thread Summary" not in second_context

    def test_handle_config_rejects_missing_channel_id(self):
        bot = make_bot()
        request = make_mocked_request("POST", "/config")
        request._post = None
        request.json = AsyncMock(return_value={"reasoning": "high"})

        response = run(bot.handle_config(request))
        assert response.status == 400
        assert b"channel_id is required" in response.body

    def test_handle_config_rejects_invalid_tool_list(self):
        bot = make_bot()
        request = make_mocked_request("POST", "/config")
        request._post = None
        request.json = AsyncMock(
            return_value={"channel_id": "123", "enabled_toolsets": '["web"'}
        )

        response = run(bot.handle_config(request))
        assert response.status == 400
        assert b"enabled_toolsets" in response.body

    def test_handle_config_accepts_valid_payload(self):
        bot = make_bot()
        request = make_mocked_request("POST", "/config")
        request._post = None
        request.json = AsyncMock(
            return_value={
                "channel_id": "123",
                "reasoning": "high",
                "enabled_toolsets": ["web", "terminal"],
                "message_mode": "interrupt",
            }
        )

        with patch("zo_discord.bot.set_channel_config", AsyncMock()) as set_cfg, patch(
            "zo_discord.bot.get_channel_config",
            AsyncMock(
                return_value={
                    "channel_id": "123",
                    "reasoning": "high",
                    "enabled_toolsets": ["web", "terminal"],
                    "message_mode": "interrupt",
                }
            ),
        ):
            response = run(bot.handle_config(request))

        assert response.status == 200
        set_cfg.assert_awaited_once()
        assert b'"success": true' in response.body.lower()

    def test_make_on_clarify_typed_response(self):
        bot = make_bot()
        thread = FakeThread()

        with patch("zo_discord.bot.send_suppressed", AsyncMock()):
            on_clarify = bot.make_on_clarify(thread)

            async def exercise():
                task = asyncio.create_task(on_clarify("Which option?", [], "conv-1"))
                await asyncio.sleep(0)
                bot._pending_clarify[str(thread.id)].set_result("Typed answer")
                return await task

            response = run(exercise())

        assert response == "Typed answer"
        assert str(thread.id) not in bot._pending_clarify

    def test_make_on_clarify_timeout(self):
        bot = make_bot()
        thread = FakeThread()

        async def fake_wait_for(_future, timeout):
            raise asyncio.TimeoutError

        with patch("zo_discord.bot.send_suppressed", AsyncMock()) as send_msg, patch(
            "zo_discord.bot.asyncio.wait_for", side_effect=fake_wait_for
        ):
            on_clarify = bot.make_on_clarify(thread)
            response = run(on_clarify("Which option?", ["A", "B"], "conv-1"))

        assert "best judgement" in response
        assert send_msg.await_count == 2

    def test_clarify_button_view_choice_sets_future(self):
        from zo_discord.bot import ClarifyButtonView

        class FakeResponse:
            def __init__(self):
                self.calls = []

            async def edit_message(self, **kwargs):
                self.calls.append(kwargs)

        async def exercise():
            future = asyncio.get_running_loop().create_future()
            view = ClarifyButtonView(["A", "B"], future)
            button = next(child for child in view.children if getattr(child, "label", None) == "A")
            interaction = SimpleNamespace(
                user=SimpleNamespace(display_name="Jack"),
                response=FakeResponse(),
            )
            await button.callback(interaction)
            return future, interaction

        future, interaction = run(exercise())

        assert future.done() is True
        assert future.result() == "A"
        assert interaction.response.calls == [
            {"content": "**Jack** selected: **A**", "view": None}
        ]

    def test_clarify_button_view_other_prompts_for_typed_answer(self):
        from zo_discord.bot import ClarifyButtonView

        class FakeResponse:
            def __init__(self):
                self.calls = []

            async def edit_message(self, **kwargs):
                self.calls.append(kwargs)

        async def exercise():
            future = asyncio.get_running_loop().create_future()
            view = ClarifyButtonView(["A", "B"], future)
            button = next(child for child in view.children if getattr(child, "label", None) == "Other")
            interaction = SimpleNamespace(
                user=SimpleNamespace(display_name="Jack"),
                response=FakeResponse(),
            )
            await button.callback(interaction)
            return future, interaction

        future, interaction = run(exercise())

        assert future.done() is False
        assert interaction.response.calls == [
            {"content": "*Type your answer below:*", "view": None}
        ]

    def test_retry_with_status_gate_waits_for_running_then_retries_when_idle(self):
        bot = make_bot()
        bot.zo.ask_stream = AsyncMock(
            return_value=SimpleNamespace(output="Recovered response", conv_id="conv-1", model_fallback="")
        )

        statuses = [
            {"state": "running", "iterations_used": 1, "iterations_max": 10},
            {"state": "idle", "iterations_used": 1, "iterations_max": 10},
        ]

        async def fake_status(_conv_id):
            return statuses.pop(0)

        with patch("zo_discord.bot.check_hermes_status", fake_status), patch(
            "zo_discord.bot.asyncio.sleep", AsyncMock()
        ), patch("zo_discord.bot.update_conversation_id", AsyncMock()):
            output, conv_id = run(
                bot._retry_with_status_gate(
                    "conv-1", "thread-1", "continue", AsyncMock(), AsyncMock(), "hermes"
                )
            )

        assert output == "Recovered response"
        assert conv_id == "conv-1"
        bot.zo.ask_stream.assert_awaited_once()
        sent_prompt = bot.zo.ask_stream.await_args.args[0]
        sent_context = bot.zo.ask_stream.await_args.kwargs["context"]
        assert "Original user message for this turn" in sent_prompt
        assert "continue" in sent_prompt
        assert "transport-recovery resend" in sent_context

    def test_send_model_fallback_notice_skips_duplicate_hermes_byok_notice(self):
        bot = make_bot()

        with patch("zo_discord.bot.send_suppressed", AsyncMock()) as send_msg:
            run(
                bot._send_model_fallback_notice(
                    FakeThread(),
                    "Hermes cannot use requested model byok:test; falling back to gpt-5.4.",
                )
            )

        send_msg.assert_not_awaited()

    def test_retry_with_status_gate_returns_error_if_hermes_unreachable(self):
        bot = make_bot()

        with patch("zo_discord.bot.check_hermes_status", AsyncMock(return_value=None)), patch(
            "zo_discord.bot.check_hermes_health", AsyncMock(return_value=False)
        ), patch("zo_discord.bot.asyncio.sleep", AsyncMock()):
            output, conv_id = run(
                bot._retry_with_status_gate(
                    "conv-1", "thread-1", "continue", AsyncMock(), AsyncMock(), "hermes"
                )
            )

        assert "zo-hermes is not responding" in output
        assert conv_id == "conv-1"

    def test_retry_with_status_gate_rechecks_queue_before_recovery_send(self):
        bot = make_bot()
        bot.zo.ask_stream = AsyncMock(
            return_value=SimpleNamespace(output="Recovered response", conv_id="conv-1", model_fallback="")
        )
        bot._message_queues["thread-1"] = asyncio.Queue()

        statuses = [
            {"state": "running", "iterations_used": 1, "iterations_max": 10},
            {"state": "idle", "iterations_used": 1, "iterations_max": 10},
        ]

        async def fake_status(_conv_id):
            if len(statuses) == 1:
                bot._message_queues["thread-1"].put_nowait(FakeMessage("follow up", author=FakeAuthor("Jill"), message_id=2))
            return statuses.pop(0)

        with patch("zo_discord.bot.check_hermes_status", fake_status), patch(
            "zo_discord.bot.asyncio.sleep", AsyncMock()
        ), patch("zo_discord.bot.update_conversation_id", AsyncMock()):
            output, conv_id = run(
                bot._retry_with_status_gate(
                    "conv-1", "thread-1", "original message", AsyncMock(), AsyncMock(), "hermes"
                )
            )

        assert output == "Recovered response"
        assert conv_id == "conv-1"
        sent_prompt = bot.zo.ask_stream.await_args.args[0]
        assert "original message" in sent_prompt
        assert "Queued user messages received while the previous turn was in progress" in sent_prompt
        assert "[Jill]: follow up" in sent_prompt

    def test_drain_queue_bundles_multiple_messages(self):
        bot = make_bot()
        bot._message_queues["thread-1"] = asyncio.Queue()
        primary = FakeMessage("second", author=FakeAuthor("Jack"), message_id=2)
        first = FakeMessage("first", author=FakeAuthor("Jill"), message_id=1)
        run(bot._message_queues["thread-1"].put(first))
        run(bot._message_queues["thread-1"].put(primary))
        bot.handle_thread_message = AsyncMock()

        run(bot._drain_queue("thread-1"))

        assert bot._bundled_prefixes[primary.id].startswith("[Messages sent while you were working:]")
        assert "[Jill]: first" in bot._bundled_prefixes[primary.id]
        bot.handle_thread_message.assert_awaited_once_with(primary)

    def test_handle_thread_message_queue_mode_enqueues_and_returns(self):
        bot = make_bot()
        thread = FakeThread()
        message = FakeMessage("queued message")
        message.channel = thread
        bot._inflight[str(thread.id)] = {"conv_id": "conv-1", "task": SimpleNamespace(done=lambda: False)}

        with patch("zo_discord.bot.get_conversation_id", AsyncMock(return_value="conv-1")), patch(
            "zo_discord.bot.get_channel_config", AsyncMock(return_value={"message_mode": "queue"})
        ), patch("zo_discord.bot.resolve_honcho_session_key", AsyncMock(return_value=None)), patch(
            "zo_discord.bot.send_suppressed", AsyncMock()
        ) as send_msg:
            run(bot.handle_thread_message(message))

        assert str(thread.id) in bot._message_queues
        assert bot._message_queues[str(thread.id)].qsize() == 1
        assert "Queued" in send_msg.await_args.kwargs["content"]

    def test_handle_thread_message_interrupt_mode_polls_status_before_reprocessing(self):
        bot = make_bot()
        thread = FakeThread()
        message = FakeMessage("interrupt me")
        message.channel = thread
        inflight_task = SimpleNamespace(done=lambda: True)
        bot._inflight[str(thread.id)] = {"conv_id": "conv-1", "task": inflight_task}
        bot.extract_overrides = lambda text: (None, None, text)
        bot.resolve_channel_defaults = AsyncMock(return_value=(None, None, "hermes", {}))
        bot.build_thread_context = AsyncMock(return_value=("", []))
        bot.make_on_thinking = lambda _thread: AsyncMock()
        bot.make_on_clarify = lambda _thread: AsyncMock()
        bot.typing_loop = AsyncMock()

        bot.zo.ask_stream = AsyncMock(
            return_value=SimpleNamespace(
                output="Handled after interrupt",
                conv_id="conv-1",
                interrupted=False,
                received_events=True,
                error_message="",
                model_fallback="",
            )
        )

        class FakePostResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class FakeClientSession:
            def post(self, *args, **kwargs):
                return FakePostResponse()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        statuses = [{"state": "running"}, {"state": "idle"}]

        async def fake_status(_session_id):
            return statuses.pop(0)

        with patch("zo_discord.bot.get_conversation_id", AsyncMock(return_value="conv-1")), patch(
            "zo_discord.bot.get_channel_config", AsyncMock(return_value={"message_mode": "interrupt"})
        ), patch("zo_discord.bot.resolve_honcho_session_key", AsyncMock(return_value=None)), patch(
            "zo_discord.bot.aiohttp.ClientSession", FakeClientSession
        ), patch("zo_discord.bot.check_hermes_status", fake_status), patch(
            "zo_discord.bot.send_suppressed", AsyncMock()
        ) as send_msg, patch("zo_discord.bot.asyncio.sleep", AsyncMock()), patch(
            "zo_discord.bot.update_conversation_id", AsyncMock()
        ), patch("zo_discord.bot.update_activity", AsyncMock()):
            run(bot.handle_thread_message(message))

        bot.zo.ask_stream.assert_awaited_once()
        assert send_msg.await_args_list[0].kwargs["content"].startswith("*Interrupting")
        assert str(thread.id) in bot._cancelled_threads

    def test_handle_thread_message_interrupt_mode_preserves_queued_messages(self):
        bot = make_bot()
        thread = FakeThread()
        queued = FakeMessage("queued earlier", author=FakeAuthor("Jill"), message_id=2)
        message = FakeMessage("interrupt me", message_id=3)
        message.channel = thread
        inflight_task = SimpleNamespace(done=lambda: True)
        bot._inflight[str(thread.id)] = {"conv_id": "conv-1", "task": inflight_task}
        bot._message_queues[str(thread.id)] = asyncio.Queue()
        run(bot._message_queues[str(thread.id)].put(queued))
        bot.extract_overrides = lambda text: (None, None, text)
        bot.resolve_channel_defaults = AsyncMock(return_value=(None, None, "hermes", {}))
        bot.build_thread_context = AsyncMock(return_value=("", []))
        bot.make_on_thinking = lambda _thread: AsyncMock()
        bot.make_on_clarify = lambda _thread: AsyncMock()
        bot.typing_loop = AsyncMock()

        bot.zo.ask_stream = AsyncMock(
            return_value=SimpleNamespace(
                output="Handled after interrupt",
                conv_id="conv-1",
                interrupted=False,
                received_events=True,
                error_message="",
                model_fallback="",
            )
        )

        class FakePostResponse:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class FakeClientSession:
            def post(self, *args, **kwargs):
                return FakePostResponse()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        statuses = [{"state": "running"}, {"state": "idle"}]

        async def fake_status(_session_id):
            return statuses.pop(0)

        with patch("zo_discord.bot.get_conversation_id", AsyncMock(return_value="conv-1")), patch(
            "zo_discord.bot.get_channel_config", AsyncMock(return_value={"message_mode": "interrupt"})
        ), patch("zo_discord.bot.resolve_honcho_session_key", AsyncMock(return_value=None)), patch(
            "zo_discord.bot.aiohttp.ClientSession", FakeClientSession
        ), patch("zo_discord.bot.check_hermes_status", fake_status), patch(
            "zo_discord.bot.send_suppressed", AsyncMock()
        ), patch("zo_discord.bot.asyncio.sleep", AsyncMock()), patch(
            "zo_discord.bot.update_conversation_id", AsyncMock()
        ), patch("zo_discord.bot.update_activity", AsyncMock()):
            run(bot.handle_thread_message(message))

        assert str(thread.id) not in bot._message_queues
        sent_prompt = bot.zo.ask_stream.await_args.args[0]
        assert "[Messages sent while you were working:]" in sent_prompt
        assert "[Jill]: queued earlier" in sent_prompt
        assert "[Jack]: interrupt me" in sent_prompt

    def test_handle_thread_message_uses_channel_model_for_existing_conversation(self):
        bot = make_bot()
        thread = FakeThread(parent=FakeParentChannel(channel_id=222, name="zo"))
        message = FakeMessage("follow up")
        message.channel = thread
        bot.extract_overrides = lambda text: (None, None, text)
        bot.resolve_channel_defaults = AsyncMock(
            return_value=("byok:test-model", "per_123", "zo", {})
        )
        bot.build_thread_context = AsyncMock(return_value=("", []))
        bot.make_on_thinking = lambda _thread: AsyncMock()
        bot.typing_loop = AsyncMock()
        bot._send_hermes_model_fallback_notice = AsyncMock()
        bot.zo.ask_stream = AsyncMock(
            return_value=SimpleNamespace(
                output="Follow-up handled",
                conv_id="conv-1",
                interrupted=False,
                received_events=True,
                error_message="",
                model_fallback="",
            )
        )

        with patch("zo_discord.bot.get_conversation_id", AsyncMock(return_value="conv-1")), patch(
            "zo_discord.bot.resolve_honcho_session_key", AsyncMock(return_value="stable-key")), patch(
            "zo_discord.bot.get_channel_config", AsyncMock(return_value={"message_mode": "queue"})
        ), patch("zo_discord.bot.update_activity", AsyncMock()), patch(
            "zo_discord.bot.send_suppressed", AsyncMock()
        ):
            run(bot.handle_thread_message(message))

        assert bot.zo.ask_stream.await_args.kwargs["model_name"] == "byok:test-model"
        assert bot.zo.ask_stream.await_args.kwargs["persona_id"] == "per_123"
        assert bot.zo.ask_stream.await_args.kwargs["honcho_session_key"] == "stable-key"

    def test_handle_notify_stores_explicit_honcho_session_key(self):
        bot = make_bot()

        class NotifyThread:
            def __init__(self):
                self.id = 789
                self.guild = SimpleNamespace(id=999)

        class StarterMessage:
            async def create_thread(self, name):
                return NotifyThread()

        class NotifyChannel:
            id = 456
            guild = SimpleNamespace(id=999)

            async def send(self, _content):
                return StarterMessage()

        request = make_mocked_request("POST", "/notify")
        request._post = None
        request.json = AsyncMock(
            return_value={
                "channel_name": "hermes",
                "title": "Thread Title",
                "content": "",
                "conversation_id": "conv-1",
                "honcho_session_key": "stable-key",
            }
        )
        bot.resolve_channel_by_name = lambda _name: NotifyChannel()

        with patch("zo_discord.bot.get_mapping_by_conversation", AsyncMock(return_value=None)), patch(
            "zo_discord.bot.save_mapping", AsyncMock()
        ) as save_mapping:
            response = run(bot.handle_notify(request))

        assert response.status == 200
        assert save_mapping.await_args.kwargs["honcho_session_key"] == "stable-key"

    def test_handle_notify_falls_back_to_conversation_id_for_honcho_session_key(self):
        bot = make_bot()

        class NotifyThread:
            def __init__(self):
                self.id = 789
                self.guild = SimpleNamespace(id=999)

        class StarterMessage:
            async def create_thread(self, name):
                return NotifyThread()

        class NotifyChannel:
            id = 456
            guild = SimpleNamespace(id=999)

            async def send(self, _content):
                return StarterMessage()

        request = make_mocked_request("POST", "/notify")
        request._post = None
        request.json = AsyncMock(
            return_value={
                "channel_name": "hermes",
                "title": "Thread Title",
                "content": "",
                "conversation_id": "conv-1",
            }
        )
        bot.resolve_channel_by_name = lambda _name: NotifyChannel()

        with patch("zo_discord.bot.get_mapping_by_conversation", AsyncMock(return_value=None)), patch(
            "zo_discord.bot.save_mapping", AsyncMock()
        ) as save_mapping:
            response = run(bot.handle_notify(request))

        assert response.status == 200
        assert save_mapping.await_args.kwargs["honcho_session_key"] == "conv-1"

    def test_handle_channel_message_seeds_thread_honcho_session_key(self):
        bot = make_bot()
        channel = FakeParentChannel(channel_id=222, name="hermes")
        thread = FakeThread(thread_id=333, name="Thread Title", parent=channel)
        message = FakeMessage("kick off")
        message.channel = channel
        message.guild = SimpleNamespace(id=999)
        message.create_thread = AsyncMock(return_value=thread)
        bot.extract_overrides = lambda text: (None, None, text)
        bot.resolve_channel_defaults = AsyncMock(return_value=(None, None, "hermes", {}))
        bot.build_channel_context = AsyncMock(return_value=("", []))
        bot.make_on_thinking = lambda _thread: AsyncMock()
        bot.make_on_clarify = lambda _thread: AsyncMock()
        bot.typing_loop = AsyncMock()
        bot._send_hermes_model_fallback_notice = AsyncMock()
        bot._send_hermes_persona_ignored_notice = AsyncMock()
        bot.zo.generate_thread_title_simple = lambda _text: "Thread Title"
        bot.zo.ask_stream = AsyncMock(
            return_value=SimpleNamespace(
                output="Started",
                conv_id="sess-1",
                interrupted=False,
                received_events=True,
                error_message="",
                model_fallback="",
            )
        )

        with patch("zo_discord.bot.save_mapping", AsyncMock()) as save_mapping, patch(
            "zo_discord.bot.send_suppressed", AsyncMock()
        ), patch("zo_discord.bot.update_thread_name", AsyncMock()), patch(
            "zo_discord.bot.update_conversation_id", AsyncMock()
        ):
            run(bot.handle_channel_message(message))

        assert save_mapping.await_args.kwargs["honcho_session_key"] == "discord-thread-333"
        assert bot.zo.ask_stream.await_args.kwargs["honcho_session_key"] == "discord-thread-333"

    def test_drain_queue_skips_during_interrupt_handoff(self):
        bot = make_bot()
        thread = FakeThread()
        queued = FakeMessage("queued earlier", author=FakeAuthor("Jill"), message_id=2)
        bot._message_queues[str(thread.id)] = asyncio.Queue()
        run(bot._message_queues[str(thread.id)].put(queued))
        bot._queue_drain_suppressed.add(str(thread.id))
        bot.handle_thread_message = AsyncMock()

        run(bot._drain_queue(str(thread.id)))

        bot.handle_thread_message.assert_not_awaited()
        assert bot._message_queues[str(thread.id)].qsize() == 1

    def test_retry_empty_response_skips_retry_for_intentional_cancel(self):
        bot = make_bot()
        thread = FakeThread()
        bot.mark_thread_cancelled(str(thread.id))

        with patch("zo_discord.bot.check_hermes_status", AsyncMock()) as status_mock:
            response, conv_id = run(
                bot._retry_empty_response(
                    str(thread.id),
                    "conv-1",
                    thread,
                    AsyncMock(),
                    AsyncMock(),
                    backend="hermes",
                )
            )

        assert response is None
        assert conv_id == "conv-1"
        status_mock.assert_not_awaited()
        assert str(thread.id) not in bot._cancelled_threads
