"""Tests for status prefix utility helpers."""


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
