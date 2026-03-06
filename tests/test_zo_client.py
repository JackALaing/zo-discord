"""Tests for ZoClient internals: config cache, session pool errors, sentences, StreamResult."""

import json
import time

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
