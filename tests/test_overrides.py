"""Tests for extract_overrides (model/persona prefix extraction)."""

import pytest
from unittest.mock import patch

try:
    import discord
    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False


@pytest.mark.skipif(not HAS_DISCORD, reason="py-cord not installed")
class TestExtractOverrides:
    """Test the unified extract_overrides method that replaced
    extract_model_prefix and extract_persona_prefix."""

    def _make_bot(self):
        from zo_discord.bot import ZoDiscordBot
        with patch.object(ZoDiscordBot, '__init__', lambda self: None):
            bot = ZoDiscordBot.__new__(ZoDiscordBot)
            bot.extract_overrides = ZoDiscordBot.extract_overrides.__get__(bot)
            return bot

    def test_model_alias_only(self):
        bot = self._make_bot()
        model, persona, text = bot.extract_overrides("/opus explain this code")
        assert model == "byok:test-opus-id"
        assert persona is None
        assert text == "explain this code"

    def test_persona_alias_only(self):
        bot = self._make_bot()
        model, persona, text = bot.extract_overrides("@pirate tell me about the weather")
        assert model is None
        assert persona == "per_test_pirate"
        assert text == "tell me about the weather"

    def test_model_then_persona(self):
        bot = self._make_bot()
        model, persona, text = bot.extract_overrides("/opus @pirate hello")
        assert model == "byok:test-opus-id"
        assert persona == "per_test_pirate"
        assert text == "hello"

    def test_persona_then_model(self):
        bot = self._make_bot()
        model, persona, text = bot.extract_overrides("@pirate /opus hello")
        assert model == "byok:test-opus-id"
        assert persona == "per_test_pirate"
        assert text == "hello"

    def test_unknown_model_not_extracted(self):
        bot = self._make_bot()
        model, persona, text = bot.extract_overrides("/unknown do something")
        assert model is None
        assert persona is None
        assert text == "/unknown do something"

    def test_unknown_persona_not_extracted(self):
        bot = self._make_bot()
        model, persona, text = bot.extract_overrides("@nobody hello there")
        assert model is None
        assert persona is None
        assert text == "@nobody hello there"

    def test_no_prefix(self):
        bot = self._make_bot()
        model, persona, text = bot.extract_overrides("just a normal message")
        assert model is None
        assert persona is None
        assert text == "just a normal message"

    def test_empty_message(self):
        bot = self._make_bot()
        model, persona, text = bot.extract_overrides("")
        assert model is None
        assert persona is None
        assert text == ""

    def test_model_only_no_remaining_text(self):
        bot = self._make_bot()
        model, persona, text = bot.extract_overrides("/opus")
        assert model == "byok:test-opus-id"
        assert persona is None
        assert text == ""

    def test_second_alias(self):
        bot = self._make_bot()
        model, persona, text = bot.extract_overrides("/sonnet @formal write something")
        assert model == "byok:test-sonnet-id"
        assert persona == "per_test_formal"
        assert text == "write something"
