"""Pure utility functions — no Discord dependency."""

import re

STATUS_EMOJI = {
    "error": "\u274c",
}

_STATUS_EMOJI_PATTERN = re.compile(r'^(?:\u274c)\s*')


def set_thread_status_prefix(name: str, status: str | None) -> str:
    """Add or replace status emoji prefix on thread name."""
    cleaned = _STATUS_EMOJI_PATTERN.sub('', name)
    if status and status in STATUS_EMOJI:
        return f"{STATUS_EMOJI[status]} {cleaned}"
    return cleaned


def strip_status_prefix(name: str) -> str:
    """Remove status emoji prefix from thread name."""
    return _STATUS_EMOJI_PATTERN.sub('', name)
