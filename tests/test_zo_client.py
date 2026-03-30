"""Tests for ZoClient internals and Hermes SSE handling."""

import json
import time
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.conftest import MOCK_CONFIG


# ── config TTL cache ─────────────────────────────────────────────────


class TestConfigCache:
    """Test that load_config uses a TTL cache and picks up changes."""

    def test_cache_returns_same_object_within_ttl(self):
        import zo_discord.zo_client as zc
        config1 = zc.load_config()
        config2 = zc.load_config()
        assert config1 is config2

    def test_cache_refreshes_after_ttl(self):
        import zo_discord.zo_client as zc
        config1 = zc.load_config()
        # Manually expire the cache
        zc._config_cache_time = time.monotonic() - zc._CONFIG_TTL - 1
        config2 = zc.load_config()
        # After expiry, should reload (new dict object)
        assert config1 is not config2
        # But content is the same
        assert config1 == config2

    def test_cache_picks_up_file_changes(self, mock_env_and_config):
        import zo_discord.zo_client as zc
        fixture_tmp = mock_env_and_config

        config1 = zc.load_config()
        assert config1.get("model") is None

        # Write a modified config to the fixture's config path
        new_config = {**MOCK_CONFIG, "model": "changed-model"}
        config_path = fixture_tmp / "config" / "config.json"
        config_path.write_text(json.dumps(new_config))

        # Expire the cache
        zc._config_cache_time = time.monotonic() - zc._CONFIG_TTL - 1
        config2 = zc.load_config()
        assert config2["model"] == "changed-model"


# ── session pool error detection ─────────────────────────────────────


class TestSessionPoolError:
    def test_detects_sessions_busy(self):
        from zo_discord.zo_client import _is_session_pool_error
        assert _is_session_pool_error("All sessions are busy, please try again later")

    def test_detects_cannot_evict(self):
        from zo_discord.zo_client import _is_session_pool_error
        assert _is_session_pool_error("Cannot evict any session from the pool")

    def test_case_insensitive(self):
        from zo_discord.zo_client import _is_session_pool_error
        assert _is_session_pool_error("SESSIONS ARE BUSY")

    def test_normal_error_not_detected(self):
        from zo_discord.zo_client import _is_session_pool_error
        assert not _is_session_pool_error("Internal server error")
        assert not _is_session_pool_error("Conversation not found")
        assert not _is_session_pool_error("")


# ── sentence counting ────────────────────────────────────────────────


class TestSentenceCounting:
    def test_basic_sentences(self):
        from zo_discord.zo_client import _count_sentences
        assert _count_sentences("Hello. World. Done.") == 3

    def test_exclamation_and_question(self):
        from zo_discord.zo_client import _count_sentences
        assert _count_sentences("What? Yes! Okay.") == 3

    def test_no_sentences(self):
        from zo_discord.zo_client import _count_sentences
        assert _count_sentences("no punctuation here") == 0

    def test_sentence_at_end(self):
        from zo_discord.zo_client import _count_sentences
        assert _count_sentences("One sentence.") == 1


class TestPathDeduplication:
    def test_dedupe_file_paths_keeps_first_alias(self):
        from zo_discord.zo_client import _dedupe_file_paths

        deduped = _dedupe_file_paths(
            [
                "/home/workspace/Skills/zo-discord",
                "/home/workspace/Services/zo-discord/skill",
                "/home/workspace/Knowledge/memory/a.md",
            ]
        )

        assert deduped == [
            "/home/workspace/Skills/zo-discord",
            "/home/workspace/Knowledge/memory/a.md",
        ]


# ── StreamResult dataclass ───────────────────────────────────────────


class TestStreamResult:
    def test_fields(self):
        from zo_discord.zo_client import StreamResult
        r = StreamResult(output="hello", conv_id="con_123", interrupted=False, received_events=True)
        assert r.output == "hello"
        assert r.conv_id == "con_123"
        assert r.interrupted is False
        assert r.received_events is True

    def test_interrupted_stream(self):
        from zo_discord.zo_client import StreamResult
        r = StreamResult(output="", conv_id="con_456", interrupted=True, received_events=True)
        assert r.interrupted is True
        assert r.output == ""


class FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_any(self):
        for chunk in self._chunks:
            yield chunk


class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=None, text=""):
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(chunks or [])
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, response, capture):
        self._response = response
        self._capture = capture

    def post(self, url, headers=None, json=None):
        self._capture["url"] = url
        self._capture["headers"] = headers
        self._capture["json"] = json
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class TestAskStream:
    def _make_client(self):
        import zo_discord.zo_client as zc

        with patch.dict("os.environ", {"DISCORD_ZO_API_KEY": "test-key"}):
            return zc.ZoClient()

    def test_payload_sends_context_via_overlay_for_hermes(self):
        client = self._make_client()
        capture = {}
        response = FakeResponse(
            headers={
                "X-Conversation-Id": "conv-1",
                "X-Model-Fallback": "Hermes cannot use requested model byok:test; falling back to gpt-5.4.",
            },
            chunks=[
                b'event: End\n',
                b'data: {"data": {"output": "hello", "conversation_id": "conv-1"}}\n',
            ],
        )

        with patch("zo_discord.zo_client.get_request_config", return_value=("http://127.0.0.1:8788/ask", {"Authorization": "Bearer test"})), patch(
            "zo_discord.zo_client.aiohttp.ClientSession", lambda timeout=None: FakeSession(response, capture)
        ):
            result = asyncio.run(
                client.ask_stream(
                    "Hi",
                    conversation_id="conv-1",
                    honcho_session_key="discord-thread-123",
                    context="Extra context",
                    file_paths=["/home/workspace/Skills/zo-discord", "/home/workspace/Services/zo-discord/skill"],
                    backend="hermes",
                    reasoning_effort="high",
                    max_iterations=7,
                    skip_memory=True,
                    skip_context=True,
                    enabled_toolsets=["web", "terminal"],
                    disabled_toolsets=["rl"],
                )
            )

        assert result.output == "hello"
        assert result.model_fallback.startswith("Hermes cannot use requested model")
        payload = capture["json"]
        assert payload["conversation_id"] == "conv-1"
        assert payload["honcho_session_key"] == "discord-thread-123"
        assert payload["reasoning_effort"] == "high"
        assert payload["max_iterations"] == 7
        assert payload["skip_memory"] is True
        assert payload["skip_context"] is True
        assert payload["enabled_toolsets"] == ["web", "terminal"]
        assert payload["disabled_toolsets"] == ["rl"]
        assert payload["input"] == "Hi"
        assert payload["ephemeral_system_prompt"] == "Extra context\n\n## Referenced Files\n- `/home/workspace/Skills/zo-discord`"

    def test_payload_keeps_context_in_input_for_non_hermes(self):
        client = self._make_client()
        capture = {}
        response = FakeResponse(
            headers={"X-Conversation-Id": "conv-1"},
            chunks=[
                b'event: End\n',
                b'data: {"data": {"output": "hello", "conversation_id": "conv-1"}}\n',
            ],
        )

        with patch("zo_discord.zo_client.get_request_config", return_value=("https://api.zo.computer/zo/ask", {"Authorization": "Bearer test"})), patch(
            "zo_discord.zo_client.aiohttp.ClientSession", lambda timeout=None: FakeSession(response, capture)
        ):
            result = asyncio.run(
                client.ask_stream(
                    "Hi",
                    conversation_id="conv-1",
                    honcho_session_key="discord-thread-123",
                    context="Extra context",
                    file_paths=["/home/workspace/a.md"],
                    backend="zo",
                )
            )

        assert result.output == "hello"
        payload = capture["json"]
        assert "Extra context" in payload["input"]
        assert "## Referenced Files" in payload["input"]
        assert "`/home/workspace/a.md`" in payload["input"]
        assert "ephemeral_system_prompt" not in payload
        assert "honcho_session_key" not in payload

    def test_clarify_event_posts_user_response_back_to_hermes(self):
        client = self._make_client()
        capture = {"clarify_posts": []}
        stream_response = FakeResponse(
            headers={"X-Conversation-Id": "conv-1"},
            chunks=[
                b"event: ClarifyEvent\n",
                b'data: {"question":"Which one?","choices":["A","B"],"session_id":"conv-1"}\n',
                b"event: End\n",
                b'data: {"data":{"output":"done","conversation_id":"conv-1"}}\n',
            ],
        )
        clarify_response = FakeResponse(headers={}, chunks=[])

        class MultiSession:
            def __init__(self, response):
                self.response = response

            def post(self, url, headers=None, json=None, timeout=None):
                if url == "http://127.0.0.1:8788/clarify-response":
                    capture["clarify_posts"].append({"url": url, "json": json})
                    return clarify_response
                capture["stream_json"] = json
                return stream_response

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        with patch("zo_discord.zo_client.get_request_config", return_value=("http://test/ask", {"Authorization": "Bearer test"})), patch(
            "zo_discord.zo_client.aiohttp.ClientSession", lambda timeout=None: MultiSession(stream_response)
        ):
            result = asyncio.run(
                client.ask_stream(
                    "Hi",
                    backend="hermes",
                    on_clarify=AsyncMock(return_value="B"),
                )
            )

        assert result.output == "done"
        assert capture["clarify_posts"] == [
            {"url": "http://127.0.0.1:8788/clarify-response", "json": {"session_id": "conv-1", "response": "B"}}
        ]

    def test_sse_error_sets_error_message_and_interrupted(self):
        client = self._make_client()
        capture = {}
        response = FakeResponse(
            headers={"X-Conversation-Id": "conv-1"},
            chunks=[
                b"event: SSEErrorEvent\n",
                b'data: {"message":"boom"}\n',
            ],
        )

        with patch("zo_discord.zo_client.get_request_config", return_value=("http://test/ask", {"Authorization": "Bearer test"})), patch(
            "zo_discord.zo_client.aiohttp.ClientSession", lambda timeout=None: FakeSession(response, capture)
        ):
            result = asyncio.run(client.ask_stream("Hi", backend="hermes"))

        assert result.interrupted is True
        assert result.error_message == "boom"
        assert result.turn_status == "error"

    def test_completed_streamed_only_uses_bridge_terminal_output_without_local_synthesis(self):
        client = self._make_client()
        capture = {}
        response = FakeResponse(
            headers={"X-Conversation-Id": "conv-2"},
            chunks=[
                b'event: PartStartEvent\n',
                b'data: {"part":{"part_kind":"text","content":"from streamed text"}}\n',
                b'event: End\n',
                b'data: {"data":{"output":"from bridge output","conversation_id":"conv-2","result":{"turn_status":"completed_streamed_only","output_source":"streamed_text","output_present":true,"hermes_final_response_present":false,"completed":true,"partial":false,"failed":false,"interrupted":false,"error":null}}}\n',
            ],
        )

        with patch("zo_discord.zo_client.get_request_config", return_value=("http://test/ask", {"Authorization": "Bearer test"})), patch(
            "zo_discord.zo_client.aiohttp.ClientSession", lambda timeout=None: FakeSession(response, capture)
        ):
            result = asyncio.run(client.ask_stream("Hi", backend="hermes"))

        assert result.output == "from bridge output"
        assert result.turn_status == "completed_streamed_only"
        assert result.terminal_result["output_source"] == "streamed_text"

    def test_old_bridge_without_result_keeps_streamed_text_compatibility_fallback(self):
        client = self._make_client()
        capture = {}
        response = FakeResponse(
            headers={"X-Conversation-Id": "conv-3"},
            chunks=[
                b'event: PartStartEvent\n',
                b'data: {"part":{"part_kind":"text","content":"old bridge streamed text"}}\n',
                b'event: End\n',
                b'data: {"data":{"output":"","conversation_id":"conv-3"}}\n',
            ],
        )

        with patch("zo_discord.zo_client.get_request_config", return_value=("http://test/ask", {"Authorization": "Bearer test"})), patch(
            "zo_discord.zo_client.aiohttp.ClientSession", lambda timeout=None: FakeSession(response, capture)
        ):
            result = asyncio.run(client.ask_stream("Hi", backend="hermes"))

        assert result.output == "old bridge streamed text"
        assert result.turn_status == "completed_streamed_only"
        assert result.terminal_result is None

    def test_old_bridge_partial_stream_then_sse_error_does_not_promote_streamed_text(self):
        client = self._make_client()
        capture = {}
        response = FakeResponse(
            headers={"X-Conversation-Id": "conv-4"},
            chunks=[
                b'event: PartStartEvent\n',
                b'data: {"part":{"part_kind":"text","content":"partial streamed text"}}\n',
                b'event: SSEErrorEvent\n',
                b'data: {"message":"boom"}\n',
            ],
        )

        with patch("zo_discord.zo_client.get_request_config", return_value=("http://test/ask", {"Authorization": "Bearer test"})), patch(
            "zo_discord.zo_client.aiohttp.ClientSession", lambda timeout=None: FakeSession(response, capture)
        ):
            result = asyncio.run(client.ask_stream("Hi", backend="hermes"))

        assert result.output == ""
        assert result.interrupted is True
        assert result.error_message == "boom"
        assert result.turn_status == "error"
        assert result.terminal_result is None

    def test_old_bridge_partial_stream_without_end_does_not_promote_streamed_text(self):
        client = self._make_client()
        capture = {}
        response = FakeResponse(
            headers={"X-Conversation-Id": "conv-5"},
            chunks=[
                b'event: PartStartEvent\n',
                b'data: {"part":{"part_kind":"text","content":"partial streamed text"}}\n',
            ],
        )

        with patch("zo_discord.zo_client.get_request_config", return_value=("http://test/ask", {"Authorization": "Bearer test"})), patch(
            "zo_discord.zo_client.aiohttp.ClientSession", lambda timeout=None: FakeSession(response, capture)
        ):
            result = asyncio.run(client.ask_stream("Hi", backend="hermes"))

        assert result.output == ""
        assert result.interrupted is True
        assert result.error_message == ""
        assert result.turn_status == ""
        assert result.terminal_result is None

    def test_end_result_preserves_conversation_rollover(self):
        client = self._make_client()
        capture = {}
        on_conv_id = AsyncMock()
        response = FakeResponse(
            headers={"X-Conversation-Id": "conv-1"},
            chunks=[
                b'event: End\n',
                b'data: {"data":{"output":"done","conversation_id":"conv-2","result":{"turn_status":"completed","output_source":"final_response","output_present":true,"hermes_final_response_present":true,"completed":true,"partial":false,"failed":false,"interrupted":false,"error":null}}}\n',
            ],
        )

        with patch("zo_discord.zo_client.get_request_config", return_value=("http://test/ask", {"Authorization": "Bearer test"})), patch(
            "zo_discord.zo_client.aiohttp.ClientSession", lambda timeout=None: FakeSession(response, capture)
        ):
            result = asyncio.run(client.ask_stream("Hi", conversation_id="conv-1", backend="hermes", on_conv_id=on_conv_id))

        assert result.conv_id == "conv-2"
        assert result.turn_status == "completed"
        assert on_conv_id.await_args_list[-1].args == ("conv-2",)
