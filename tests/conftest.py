"""Shared test fixtures for zo-discord."""

import os
import json
import pytest
from unittest.mock import patch

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

    import zo_discord.zo_client as zc
    zc._config_cache = None
    zc._config_cache_time = 0.0

    with patch("zo_discord.PROJECT_ROOT", tmp_path), \
         patch("zo_discord.zo_client.CONFIG_PATH", config_path), \
         patch.dict(os.environ, {"DISCORD_ZO_API_KEY": "test-key"}):
        yield tmp_path
