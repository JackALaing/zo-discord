"""Tests for Discord formatting, message chunking, and title generation."""

import os
import json
import pytest
from unittest.mock import patch
from pathlib import Path

try:
    import discord
    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False

# Create a minimal config for ZoClient instantiation
MOCK_CONFIG = {
    "model": None,
    "max_message_length": 1900,
    "model_aliases": {
        "opus": "byok:test-opus-id",
        "sonnet": "byok:test-sonnet-id",
    },
    "persona_aliases": {
        "pirate": "per_test_pirate",
        "formal": "per_test_formal",
    },
}


@pytest.fixture(autouse=True)
def mock_env_and_config(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "config.json"
    config_path.write_text(json.dumps(MOCK_CONFIG))

    with patch("zo_discord.PROJECT_ROOT", tmp_path), \
         patch.dict(os.environ, {"DISCORD_ZO_API_KEY": "test-key"}):
        yield


def make_client():
    from zo_discord.zo_client import ZoClient
    return ZoClient()


# ── format_for_discord ────────────────────────────────────────────────


class TestFootnotes:
    def test_converts_url_footnotes_to_inline_links(self):
        client = make_client()
        text = "Some claim. [^1]\n\n[^1]: https://example.com/article"
        result = client.format_for_discord(text)
        assert "([example.com](https://example.com/article))" in result
        assert "[^1]:" not in result

    def test_converts_non_url_footnotes_to_inline_text(self):
        client = make_client()
        text = "See note. [^1]\n\n[^1]: Author, Book Title, p.42"
        result = client.format_for_discord(text)
        assert "(Author, Book Title, p.42)" in result

    def test_multiple_footnotes(self):
        client = make_client()
        text = "First. [^1] Second. [^2]\n\n[^1]: https://a.com\n[^2]: https://b.com"
        result = client.format_for_discord(text)
        assert "([a.com](https://a.com))" in result
        assert "([b.com](https://b.com))" in result

    def test_strips_www_from_domain(self):
        client = make_client()
        text = "Claim. [^1]\n\n[^1]: https://www.example.com/page"
        result = client.format_for_discord(text)
        assert "([example.com]" in result


class TestTables:
    def test_narrow_table_becomes_code_block(self):
        client = make_client()
        text = "| A | B |\n| --- | --- |\n| 1 | 2 |"
        result = client.format_for_discord(text)
        assert "```" in result
        assert "A" in result
        assert "1" in result

    def test_wide_table_becomes_bullet_list(self):
        client = make_client()
        text = (
            "| Name | Description | Status | Notes |\n"
            "| --- | --- | --- | --- |\n"
            "| Feature A | A very long description here | Active | Some notes |"
        )
        result = client.format_for_discord(text)
        assert "**Feature A**" in result
        assert "- Description:" in result

    def test_single_row_table_unchanged(self):
        client = make_client()
        text = "| just one row |"
        result = client.format_for_discord(text)
        assert "just one row" in result


class TestHorizontalRules:
    def test_removes_dashes(self):
        client = make_client()
        result = client.format_for_discord("Above\n\n---\n\nBelow")
        assert "---" not in result
        assert "Above" in result
        assert "Below" in result

    def test_removes_asterisks(self):
        client = make_client()
        result = client.format_for_discord("Above\n\n***\n\nBelow")
        assert "***" not in result


class TestTaskLists:
    def test_checked_items(self):
        client = make_client()
        result = client.format_for_discord("- [x] Done task")
        assert "- \u2713 Done task" in result

    def test_unchecked_items(self):
        client = make_client()
        result = client.format_for_discord("- [ ] Todo task")
        assert "- Todo task" in result
        assert "[ ]" not in result


class TestUrlHandling:
    def test_collapses_url_as_link_text(self):
        client = make_client()
        result = client.format_for_discord("[https://example.com](https://example.com)")
        assert result == "<https://example.com>"

    def test_wraps_bare_urls_in_angle_brackets(self):
        client = make_client()
        result = client.format_for_discord("Check https://example.com for details")
        assert "<https://example.com>" in result

    def test_preserves_masked_links(self):
        client = make_client()
        result = client.format_for_discord("[click here](https://example.com)")
        assert "[click here](https://example.com)" in result

    def test_does_not_double_wrap(self):
        client = make_client()
        result = client.format_for_discord("<https://example.com>")
        assert result.count("<https://example.com>") == 1


class TestExcessBlankLines:
    def test_collapses_triple_newlines(self):
        client = make_client()
        result = client.format_for_discord("A\n\n\n\nB")
        assert "\n\n\n" not in result
        assert "A\n\nB" in result


# ── chunk_response ────────────────────────────────────────────────────


class TestChunking:
    def test_short_message_no_split(self):
        client = make_client()
        chunks = client.chunk_response("Hello world")
        assert len(chunks) == 1
        assert chunks[0] == "Hello world"

    def test_splits_long_message(self):
        client = make_client()
        client.max_length = 100
        text = "A" * 50 + "\n\n" + "B" * 50 + "\n\n" + "C" * 50
        chunks = client.chunk_response(text)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 100 + 2  # +2 for zero-width space prefix

    def test_no_chunk_exceeds_limit(self):
        client = make_client()
        client.max_length = 200
        text = "\n\n".join(f"Section {i}: " + "x" * 80 for i in range(10))
        chunks = client.chunk_response(text)
        for chunk in chunks:
            assert len(chunk) <= 200 + 2

    def test_continuation_chunks_have_spacer(self):
        client = make_client()
        client.max_length = 50
        text = "First paragraph here.\n\nSecond paragraph here.\n\nThird paragraph here."
        chunks = client.chunk_response(text)
        if len(chunks) > 1:
            for chunk in chunks[1:]:
                assert chunk.startswith("\u200b\n")

    def test_splits_at_topic_boundaries(self):
        client = make_client()
        client.max_length = 100
        text = "## Section One\n\n" + "Content for section one. " * 5 + "\n\n## Section Two\n\n" + "Content for section two. " * 5
        chunks = client.chunk_response(text)
        assert len(chunks) >= 2

    def test_very_long_word_still_fits(self):
        client = make_client()
        client.max_length = 50
        text = "a" * 200
        chunks = client.chunk_response(text)
        assert all(len(c) <= 50 + 2 for c in chunks)
        joined = "".join(c.lstrip("\u200b\n") for c in chunks)
        assert "a" * 200 in joined


# ── generate_thread_title_simple ──────────────────────────────────────


class TestThreadTitle:
    def test_basic_message(self):
        client = make_client()
        assert client.generate_thread_title_simple("What is Python?") == "What is Python?"

    def test_empty_message(self):
        client = make_client()
        assert client.generate_thread_title_simple("") == "New conversation"

    def test_strips_mentions(self):
        client = make_client()
        result = client.generate_thread_title_simple("<@123456> help me")
        assert "<@" not in result
        assert "help me" in result

    def test_strips_channel_mentions(self):
        client = make_client()
        result = client.generate_thread_title_simple("<#999> check this")
        assert "<#" not in result

    def test_strips_urls(self):
        client = make_client()
        result = client.generate_thread_title_simple("look at https://example.com please")
        assert "https://" not in result
        assert "look at" in result

    def test_strips_spoilers(self):
        client = make_client()
        result = client.generate_thread_title_simple("this is ||secret|| stuff")
        assert "||" not in result

    def test_strips_inline_code(self):
        client = make_client()
        result = client.generate_thread_title_simple("run `npm install` now")
        assert "`" not in result

    def test_strips_code_blocks(self):
        client = make_client()
        result = client.generate_thread_title_simple("here:\n```python\nprint('hi')\n```\nok")
        assert "```" not in result

    def test_strips_custom_emoji(self):
        client = make_client()
        result = client.generate_thread_title_simple("<:smile:123456> hello")
        assert "<:" not in result

    def test_truncates_long_messages(self):
        client = make_client()
        result = client.generate_thread_title_simple("a " * 100)
        assert len(result) <= 80
        assert result.endswith("...")

    def test_only_formatting_returns_fallback(self):
        client = make_client()
        result = client.generate_thread_title_simple("<@123> <#456>")
        assert result == "New conversation"


# ── extract_model_prefix / extract_persona_prefix ─────────────────────


@pytest.mark.skipif(not HAS_DISCORD, reason="py-cord not installed")
class TestPrefixExtraction:
    def test_model_alias_extracted(self):
        from zo_discord.bot import ZoDiscordBot
        with patch.object(ZoDiscordBot, '__init__', lambda self: None):
            bot = ZoDiscordBot.__new__(ZoDiscordBot)
            bot.extract_model_prefix = ZoDiscordBot.extract_model_prefix.__get__(bot)
            model_id, text = bot.extract_model_prefix("/opus explain this code")
            assert model_id == "byok:test-opus-id"
            assert text == "explain this code"

    def test_unknown_prefix_not_extracted(self):
        from zo_discord.bot import ZoDiscordBot
        with patch.object(ZoDiscordBot, '__init__', lambda self: None):
            bot = ZoDiscordBot.__new__(ZoDiscordBot)
            bot.extract_model_prefix = ZoDiscordBot.extract_model_prefix.__get__(bot)
            model_id, text = bot.extract_model_prefix("/unknown do something")
            assert model_id is None
            assert text == "/unknown do something"

    def test_no_prefix(self):
        from zo_discord.bot import ZoDiscordBot
        with patch.object(ZoDiscordBot, '__init__', lambda self: None):
            bot = ZoDiscordBot.__new__(ZoDiscordBot)
            bot.extract_model_prefix = ZoDiscordBot.extract_model_prefix.__get__(bot)
            model_id, text = bot.extract_model_prefix("just a normal message")
            assert model_id is None
            assert text == "just a normal message"

    def test_persona_alias_extracted(self):
        from zo_discord.bot import ZoDiscordBot
        with patch.object(ZoDiscordBot, '__init__', lambda self: None):
            bot = ZoDiscordBot.__new__(ZoDiscordBot)
            bot.extract_persona_prefix = ZoDiscordBot.extract_persona_prefix.__get__(bot)
            persona_id, text = bot.extract_persona_prefix("@pirate tell me about the weather")
            assert persona_id == "per_test_pirate"
            assert text == "tell me about the weather"


# ── status prefix helpers ─────────────────────────────────────────────


class TestStatusPrefix:
    def test_set_error_prefix(self):
        from zo_discord.utils import set_thread_status_prefix
        result = set_thread_status_prefix("My Thread", "error")
        assert result == "\u274c My Thread"

    def test_clear_status(self):
        from zo_discord.utils import set_thread_status_prefix
        result = set_thread_status_prefix("\u274c My Thread", None)
        assert result == "My Thread"

    def test_replace_status(self):
        from zo_discord.utils import set_thread_status_prefix
        result = set_thread_status_prefix("\u274c Old Thread", "error")
        assert result == "\u274c Old Thread"

    def test_strip_prefix(self):
        from zo_discord.utils import strip_status_prefix
        assert strip_status_prefix("\u274c My Thread") == "My Thread"

    def test_strip_no_prefix(self):
        from zo_discord.utils import strip_status_prefix
        assert strip_status_prefix("Normal Thread") == "Normal Thread"
