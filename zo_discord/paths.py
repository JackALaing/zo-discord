import json
import os
from pathlib import Path

from zo_discord import PROJECT_ROOT


def get_config_path() -> Path:
    return Path(
        os.getenv("ZO_DISCORD_CONFIG_PATH", str(PROJECT_ROOT / "config" / "config.json"))
    ).expanduser()


def load_config_file() -> dict:
    config_path = get_config_path()
    if not config_path.exists():
        return {}
    with open(config_path) as f:
        return json.load(f)


def get_data_dir(config: dict | None = None) -> Path:
    config = config or load_config_file()
    return Path(config.get("data_dir", "discord_data")).expanduser().resolve()


def get_db_path(config: dict | None = None) -> Path:
    config = config or load_config_file()
    raw_path = config.get("db_path")
    if raw_path:
        return Path(raw_path).expanduser().resolve()
    return get_data_dir(config) / "threads.db"


def get_hermes_home() -> Path:
    return Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
