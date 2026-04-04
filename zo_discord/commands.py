"""
Slash commands for zo-discord.
"""

import json
import logging
import os
from pathlib import Path

import aiohttp
import discord
import yaml
from discord import ui
from zo_discord import PROJECT_ROOT
from zo_discord.db import get_channel_config, set_channel_config, get_conversation_id, update_conversation_id
from zo_discord.hermes import is_hermes
from zo_discord.zo_client import load_config

logger = logging.getLogger("zo_discord.commands")

HERMES_BASE = "http://127.0.0.1:8788"

HERMES_CONFIG_PATH = Path.home() / ".hermes" / "config.yaml"


async def _hermes_post(path: str, payload: dict) -> tuple[int, dict]:
    """POST to a zo-hermes endpoint. Returns (status_code, json_body)."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{HERMES_BASE}{path}",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = await resp.json()
                return resp.status, body
    except Exception as e:
        logger.error("Hermes POST %s failed: %s", path, e)
        return 0, {"error": f"zo-hermes unreachable: {e}"}


async def _hermes_get(path: str, params: dict | None = None) -> tuple[int, dict]:
    """GET from a zo-hermes endpoint. Returns (status_code, json_body)."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{HERMES_BASE}{path}",
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = await resp.json()
                return resp.status, body
    except Exception as e:
        logger.error("Hermes GET %s failed: %s", path, e)
        return 0, {"error": f"zo-hermes unreachable: {e}"}

AVAILABLE_TOOLSETS = [
    "web", "terminal", "file", "browser", "vision", "image_gen",
    "skills", "skills_hub", "moa", "todo", "tts", "cronjob", "rl",
    "all", "debugging", "safe",
]

CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"


async def _get_channel_backend(ctx: discord.ApplicationContext) -> str | None:
    """Get the backend configured for the channel where the command was invoked."""
    channel = ctx.channel
    if isinstance(channel, discord.Thread) and channel.parent:
        channel = channel.parent
    ch_config = await get_channel_config(str(channel.id))
    return ch_config.get("backend") if ch_config else None


def _is_hermes_ctx(backend: str | None) -> bool:
    """Check if the channel backend is Hermes."""
    config = load_config()
    return is_hermes(backend, config.get("backend", "zo"))


def _backend_label(backend: str | None) -> str:
    """Human-readable backend name."""
    config = load_config()
    return "Hermes" if is_hermes(backend, config.get("backend", "zo")) else "Zo"


def _save_config_key(key: str, value):
    """Update a single key in config.json and write back."""
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    config[key] = value
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


def _get_parent_channel(ctx: discord.ApplicationContext):
    """Get the parent channel (resolves threads to their parent)."""
    channel = ctx.channel
    if isinstance(channel, discord.Thread) and channel.parent:
        channel = channel.parent
    return channel


def _read_hermes_config() -> dict:
    """Read ~/.hermes/config.yaml."""
    if not HERMES_CONFIG_PATH.exists():
        return {}
    with open(HERMES_CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def _write_hermes_config(data: dict):
    """Write ~/.hermes/config.yaml."""
    with open(HERMES_CONFIG_PATH, "w") as f:
        yaml.dump(data, f, default_flow_style=False)

TIPS = [
    "zo-discord supports **message queuing**. Send multiple messages while Zo is thinking — they'll be batched into one message when the current turn finishes.",
    "zo-discord supports **message buffering** (`/buffer`). Set a delay (e.g. 2s) so rapid-fire messages are combined into a single request. The timer pauses while you're typing, so you won't feel rushed. Great for composing multi-part prompts.",
    "Use zo-discord as a **notification channel** for scheduled tasks instead of SMS/email/Telegram. See the zo-discord skill for more details.",
    "zo-discord can override Discord's **auto-archive** behavior to keep threads open until you manually archive them. React with :white_check_mark: to any message in the thread to archive it. Set this as your double-tap reaction on mobile for quick archiving.",
    "Set **model and persona per-channel** with `/model` and `/persona`. Ask Zo to set aliases, then prefix prompts with `/alias` (e.g. `/opus`) or `@alias` (e.g. `@pirate`) to override the channel default for one conversation.",
    "zo-discord automatically recognizes **new channels**. Just create a channel and send a message to initialize it. Scheduled agents can target channels by name. See the zo-discord skill for more details.",
    "zo-discord supports **per-channel instructions and memory paths**, but has no built-in memory system. Plug in your own memory system to maintain these file paths.",
    "**Reply to specific messages** in a thread — Zo will see which message you're responding to and include it as context.",
    "zo-discord supports **file attachments**. Attach files to your messages and Zo will receive them. Uploaded files are saved to the attachments subfolder within each channel's data directory (configured via `data_dir` in config).",
]


def _resolve_model_alias(value: str | None) -> str | None:
    """Resolve a model alias to a model ID. Returns the ID if alias found, else the value as-is."""
    if not value:
        return None
    config = load_config()
    aliases = config.get("model_aliases", {})
    return aliases.get(value, value)


def _display_model(model_id: str | None) -> str:
    """Format a model ID for display, showing alias if one exists."""
    if not model_id:
        return "Not set"
    config = load_config()
    aliases = config.get("model_aliases", {})
    for alias, mid in aliases.items():
        if mid == model_id:
            return f"{alias} (`{model_id}`)"
    return f"`{model_id}`"


def _resolve_persona_alias(value: str | None) -> str | None:
    """Resolve a persona alias to a persona ID. Returns the ID if alias found, else the value as-is."""
    if not value:
        return None
    config = load_config()
    aliases = config.get("persona_aliases", {})
    return aliases.get(value, value)


def _display_persona(persona_id: str | None) -> str:
    """Format a persona ID for display, showing alias if one exists."""
    if not persona_id:
        return "Not set"
    config = load_config()
    aliases = config.get("persona_aliases", {})
    for alias, pid in aliases.items():
        if pid == persona_id:
            return f"{alias} (`{persona_id}`)"
    return f"`{persona_id}`"


class GlobalModelModal(ui.Modal):
    def __init__(self, bot, current_model: str | None):
        super().__init__(title="Set Global Default Model")
        self.bot = bot
        self.model_input = ui.InputText(
            label="Model name or alias",
            placeholder="e.g. opus, sonnet, or blank for Zo's default",
            value=current_model or "",
            required=False,
        )
        self.add_item(self.model_input)

    async def callback(self, interaction: discord.Interaction):
        raw = self.model_input.value.strip() or None
        resolved = _resolve_model_alias(raw)
        _save_config_key("model", resolved)
        self.bot.zo.model = resolved
        display = _display_model(resolved)
        await interaction.response.send_message(
            f"Global model updated to **{display}**.",
            ephemeral=True,
        )


class ChannelModelModal(ui.Modal):
    def __init__(self, bot, current_model: str | None, channel_id: str | None):
        super().__init__(title="Set Channel Default Model")
        self.bot = bot
        self.channel_id = channel_id
        self.model_input = ui.InputText(
            label="Model name or alias (blank to clear)",
            placeholder="e.g. opus, sonnet, or blank to use global default",
            value=current_model or "",
            required=False,
        )
        self.add_item(self.model_input)

    async def callback(self, interaction: discord.Interaction):
        raw = self.model_input.value.strip() or None
        resolved = _resolve_model_alias(raw)
        if self.channel_id:
            await set_channel_config(self.channel_id, model=resolved)
        display = _display_model(resolved) if resolved else "Cleared (using global default)"
        await interaction.response.send_message(
            f"Channel model updated to **{display}**.",
            ephemeral=True,
        )


# --- Model Views/Modals ---

class ModelSelectView(ui.View):
    def __init__(self, bot, current_global: str | None, current_channel: str | None, channel_id: str | None):
        super().__init__(timeout=120)
        self.bot = bot
        self.current_global = current_global
        self.current_channel = current_channel
        self.channel_id = channel_id

    @ui.button(label="Change Global", style=discord.ButtonStyle.primary)
    async def change_global(self, button: ui.Button, interaction: discord.Interaction):
        modal = GlobalModelModal(self.bot, self.current_global)
        await interaction.response.send_modal(modal)

    @ui.button(label="Change Channel", style=discord.ButtonStyle.secondary)
    async def change_channel(self, button: ui.Button, interaction: discord.Interaction):
        modal = ChannelModelModal(self.bot, self.current_channel, self.channel_id)
        await interaction.response.send_modal(modal)


class GlobalPersonaModal(ui.Modal):
    def __init__(self, bot, current_persona: str | None):
        super().__init__(title="Set Global Default Persona")
        self.bot = bot
        self.persona_input = ui.InputText(
            label="Persona name or alias (blank to clear)",
            placeholder="e.g. sassy, formal, or blank for Zo's default",
            value=current_persona or "",
            required=False,
        )
        self.add_item(self.persona_input)

    async def callback(self, interaction: discord.Interaction):
        raw = self.persona_input.value.strip() or None
        resolved = _resolve_persona_alias(raw)
        _save_config_key("default_persona", resolved)
        display = _display_persona(resolved) if resolved else "Cleared (using Zo's default)"
        await interaction.response.send_message(
            f"Global persona updated to **{display}**.",
            ephemeral=True,
        )


class ChannelPersonaModal(ui.Modal):
    def __init__(self, bot, current_persona: str | None, channel_id: str | None):
        super().__init__(title="Set Channel Default Persona")
        self.bot = bot
        self.channel_id = channel_id
        self.persona_input = ui.InputText(
            label="Persona name or alias (blank to clear)",
            placeholder="e.g. sassy, formal, or blank to use global default",
            value=current_persona or "",
            required=False,
        )
        self.add_item(self.persona_input)

    async def callback(self, interaction: discord.Interaction):
        raw = self.persona_input.value.strip() or None
        resolved = _resolve_persona_alias(raw)
        if self.channel_id:
            await set_channel_config(self.channel_id, persona_id=resolved)
        display = _display_persona(resolved) if resolved else "Cleared (using global default)"
        await interaction.response.send_message(
            f"Channel persona updated to **{display}**.",
            ephemeral=True,
        )


# --- Persona Views/Modals ---

class PersonaSelectView(ui.View):
    def __init__(self, bot, current_global: str | None, current_channel: str | None, channel_id: str | None):
        super().__init__(timeout=120)
        self.bot = bot
        self.current_global = current_global
        self.current_channel = current_channel
        self.channel_id = channel_id

    @ui.button(label="Change Global", style=discord.ButtonStyle.primary)
    async def change_global(self, button: ui.Button, interaction: discord.Interaction):
        modal = GlobalPersonaModal(self.bot, self.current_global)
        await interaction.response.send_modal(modal)

    @ui.button(label="Change Channel", style=discord.ButtonStyle.secondary)
    async def change_channel(self, button: ui.Button, interaction: discord.Interaction):
        modal = ChannelPersonaModal(self.bot, self.current_channel, self.channel_id)
        await interaction.response.send_modal(modal)


class ThinkingSelectView(ui.View):
    def __init__(self, bot):
        super().__init__(timeout=60)
        self.bot = bot

    @ui.button(label="Streaming", style=discord.ButtonStyle.primary)
    async def streaming(self, button: ui.Button, interaction: discord.Interaction):
        self.bot._thinking_mode = "streaming"
        _save_config_key("thinking_mode", "streaming")
        await interaction.response.edit_message(
            content="Thinking mode set to **Streaming** — Zo's intermediate thinking will be posted as italicized messages.",
            view=None,
        )

    @ui.button(label="Quiet", style=discord.ButtonStyle.secondary)
    async def quiet(self, button: ui.Button, interaction: discord.Interaction):
        self.bot._thinking_mode = "quiet"
        _save_config_key("thinking_mode", "quiet")
        await interaction.response.edit_message(
            content="Thinking mode set to **Quiet** — only Zo's final response will be shown.",
            view=None,
        )


class AutoArchiveSelectView(ui.View):
    def __init__(self, bot):
        super().__init__(timeout=60)
        self.bot = bot

    @ui.button(label="Prevent Auto-Archive", style=discord.ButtonStyle.success)
    async def prevent(self, button: ui.Button, interaction: discord.Interaction):
        self.bot._auto_archive_override = True
        _save_config_key("auto_archive_override", True)
        await interaction.response.edit_message(
            content="Auto-archive **disabled**. Threads will stay open until you archive them with :white_check_mark:.",
            view=None,
        )

    @ui.button(label="Allow Auto-Archive", style=discord.ButtonStyle.secondary)
    async def allow(self, button: ui.Button, interaction: discord.Interaction):
        self.bot._auto_archive_override = False
        _save_config_key("auto_archive_override", False)
        await interaction.response.edit_message(
            content="Auto-archive **enabled**. Discord will auto-archive inactive threads.",
            view=None,
        )


class GlobalBufferModal(ui.Modal):
    def __init__(self, bot, current_value: float):
        super().__init__(title="Set Global Buffer")
        self.bot = bot
        self.buffer_input = ui.InputText(
            label="Buffer seconds (0 to disable)",
            placeholder="e.g. 2, 5, 0",
            value=str(current_value),
            required=True,
        )
        self.add_item(self.buffer_input)

    async def callback(self, interaction: discord.Interaction):
        raw = self.buffer_input.value.strip()
        try:
            value = float(raw)
            if value < 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Invalid value. Enter a number >= 0.", ephemeral=True)
            return
        _save_config_key("buffer_seconds", value)
        self.bot.config["buffer_seconds"] = value
        status = f"**{value}s**" if value > 0 else "**Disabled** (0s)"
        await interaction.response.send_message(f"Global buffer updated to {status}.", ephemeral=True)


class ChannelBufferModal(ui.Modal):
    def __init__(self, bot, current_value: float | None, channel_id: str | None):
        super().__init__(title="Set Channel Buffer")
        self.bot = bot
        self.channel_id = channel_id
        self.buffer_input = ui.InputText(
            label="Buffer seconds (blank to use global default)",
            placeholder="e.g. 2, 5, 0, or blank for global default",
            value=str(current_value) if current_value is not None else "",
            required=False,
        )
        self.add_item(self.buffer_input)

    async def callback(self, interaction: discord.Interaction):
        raw = self.buffer_input.value.strip()
        if not raw:
            value = None
        else:
            try:
                value = float(raw)
                if value < 0:
                    raise ValueError
            except ValueError:
                await interaction.response.send_message("Invalid value. Enter a number >= 0, or leave blank.", ephemeral=True)
                return
        if self.channel_id:
            await set_channel_config(self.channel_id, buffer_seconds=value)
        if value is not None:
            status = f"**{value}s**" if value > 0 else "**Disabled** (0s)"
        else:
            status = "**Cleared** (using global default)"
        await interaction.response.send_message(f"Channel buffer updated to {status}.", ephemeral=True)


class BufferSelectView(ui.View):
    def __init__(self, bot, current_global: float, current_channel: float | None, channel_id: str | None):
        super().__init__(timeout=120)
        self.bot = bot
        self.current_global = current_global
        self.current_channel = current_channel
        self.channel_id = channel_id

    @ui.button(label="Change Global", style=discord.ButtonStyle.primary)
    async def change_global(self, button: ui.Button, interaction: discord.Interaction):
        modal = GlobalBufferModal(self.bot, self.current_global)
        await interaction.response.send_modal(modal)

    @ui.button(label="Change Channel", style=discord.ButtonStyle.secondary)
    async def change_channel(self, button: ui.Button, interaction: discord.Interaction):
        modal = ChannelBufferModal(self.bot, self.current_channel, self.channel_id)
        await interaction.response.send_modal(modal)


class AllowedUserModal(ui.Modal):
    def __init__(self, bot):
        super().__init__(title="Add/Remove Allowed User")
        self.bot = bot
        self.user_input = ui.InputText(
            label="Discord User ID",
            placeholder="Right-click user → Copy User ID",
            required=True,
        )
        self.add_item(self.user_input)

    async def callback(self, interaction: discord.Interaction):
        user_id = self.user_input.value.strip()
        config = load_config()
        allowed = config.get("allowed_users", [])
        if user_id in allowed:
            allowed.remove(user_id)
            action = "removed from"
        else:
            allowed.append(user_id)
            action = "added to"
        _save_config_key("allowed_users", allowed)
        self.bot.config["allowed_users"] = allowed

        await interaction.response.send_message(
            f"User `{user_id}` {action} allowed users.\n\nCurrent list: {', '.join(f'`{u}`' for u in allowed) or '(empty — all users allowed)'}",
            ephemeral=True,
        )


class AllowedUsersView(ui.View):
    def __init__(self, bot):
        super().__init__(timeout=120)
        self.bot = bot

    @ui.button(label="Add/Remove User", style=discord.ButtonStyle.primary)
    async def toggle_user(self, button: ui.Button, interaction: discord.Interaction):
        modal = AllowedUserModal(self.bot)
        await interaction.response.send_modal(modal)


class BackendSelectView(ui.View):
    def __init__(self, bot, current_global: str, current_channel: str | None, channel_id: str):
        super().__init__(timeout=120)
        self.bot = bot
        self.current_global = current_global
        self.current_channel = current_channel
        self.channel_id = channel_id

    @ui.button(label="Set Channel → Zo", style=discord.ButtonStyle.primary)
    async def set_zo(self, button: ui.Button, interaction: discord.Interaction):
        await set_channel_config(self.channel_id, backend="zo")
        await interaction.response.edit_message(
            content="Channel backend set to **Zo**. New threads will use the Zo API.",
            view=None,
        )

    @ui.button(label="Set Channel → Hermes", style=discord.ButtonStyle.success)
    async def set_hermes(self, button: ui.Button, interaction: discord.Interaction):
        await set_channel_config(self.channel_id, backend="hermes")
        await interaction.response.edit_message(
            content="Channel backend set to **Hermes**. New threads will use the local Hermes agent.",
            view=None,
        )

    @ui.button(label="Clear Channel Override", style=discord.ButtonStyle.secondary)
    async def clear(self, button: ui.Button, interaction: discord.Interaction):
        await set_channel_config(self.channel_id, backend=None)
        await interaction.response.edit_message(
            content=f"Channel backend cleared. Using global default (**{self.current_global}**).",
            view=None,
        )


async def _require_hermes(ctx: discord.ApplicationContext) -> bool:
    """Check if channel is Hermes. If not, respond with error and return False."""
    backend = await _get_channel_backend(ctx)
    if not _is_hermes_ctx(backend):
        await ctx.respond("This command is only available in Hermes channels.", ephemeral=True)
        return False
    return True


async def _defer_then_followup(ctx: discord.ApplicationContext, content: str, **kwargs):
    """Acknowledge first, then send via follow-up to avoid expired slash interactions."""
    await ctx.defer(ephemeral=kwargs.get("ephemeral", False))
    await ctx.followup.send(content, **kwargs)


async def _mark_last_exchange_undone(thread: discord.Thread, bot_user: discord.abc.User, removed_count: int) -> None:
    """Best-effort visual marker for the last undone exchange in Discord history."""
    if removed_count <= 0:
        return
    try:
        def has_block_reaction(msg) -> bool:
            for reaction in getattr(msg, "reactions", []) or []:
                emoji = getattr(reaction, "emoji", None)
                if emoji == "🚫":
                    return True
            return False

        reacted = 0
        async for msg in thread.history(limit=50):
            if msg.author == bot_user and reacted < removed_count:
                if not has_block_reaction(msg):
                    await msg.add_reaction("🚫")
                reacted += 1
            elif msg.author != bot_user and reacted > 0:
                if not has_block_reaction(msg):
                    await msg.add_reaction("🚫")
                break
    except Exception as e:
        logger.warning("Failed to add undo reactions: %s", e)


def setup_commands(bot):
    """Register all slash commands on the bot."""

    @bot.slash_command(name="help", description="Show bot info and available commands")
    async def help_cmd(ctx: discord.ApplicationContext):
        config = load_config()
        model_display = _display_model(config.get("model"))
        backend = await _get_channel_backend(ctx)
        hermes = _is_hermes_ctx(backend)
        backend_name = _backend_label(backend)
        conv_id = None
        if isinstance(ctx.channel, discord.Thread):
            conv_id = await get_conversation_id(str(ctx.channel.id))

        lines = [
            "**zo-discord**",
            f"Backend: {backend_name}",
            f"Model: {model_display}",
        ]
        if conv_id:
            label = "Session" if hermes else "Conversation"
            lines.append(f"{label}: `{conv_id}`")
        lines.append("")
        lines.append("**Commands**")
        lines.append("`/help` — This message")
        lines.append("`/tips` — Tips and tricks")
        lines.append("`/link` — Open conversation in Zo" if not hermes else "`/link` — Show session ID")
        lines.append("`/model` — View/change default model")
        lines.append("`/persona` — View/change default persona")
        lines.append("`/backend` — View/change backend (Zo/Hermes)")
        lines.append("`/thinking` — Toggle thinking mode")
        lines.append("`/buffer` — Configure message buffering")
        lines.append("`/auto-archive` — Configure auto-archive")
        lines.append("`/instructions` — View channel instructions")
        lines.append("`/memory` — View channel memory paths")
        lines.append("`/allowed-users` — Manage allowed users")
        lines.append("`/cli` — Show CLI commands")

        if hermes:
            lines.append("")
            lines.append("**Hermes Config**")
            lines.append("`/reasoning` — Set reasoning effort (off/low/medium/high)")
            lines.append("`/tools` — View enabled/disabled toolsets")
            lines.append("`/max-iterations` — Set max agent iterations")
            lines.append("`/skip-memory` — Toggle memory skip")
            lines.append("`/skip-context` — Toggle context skip")
            lines.append("`/compression-threshold` — Set compression threshold")

            help_channel = _get_parent_channel(ctx)
            help_ch_config = await get_channel_config(str(help_channel.id))
            msg_mode = help_ch_config.get("message_mode", "queue") if help_ch_config else "queue"
            lines.append(f"`/queue` / `/interrupt` — message mode (currently: {msg_mode})")
            lines.append("")
            lines.append("**Session Management**")
            lines.append("`/stop` — Cancel the current agent turn")
            lines.append("`/undo` — Undo the last exchange")
            lines.append("`/retry` — Undo and re-send the last message")
            lines.append("`/status` — Show session state")
            lines.append("`/usage` — Show token usage")
            lines.append("`/compress` — Compress session context")

        await ctx.respond("\n".join(lines), ephemeral=True)

    @bot.slash_command(name="tips", description="Tips and tricks for using zo-discord")
    async def tips_cmd(ctx: discord.ApplicationContext):
        formatted = "\n\n".join(f"{i+1}. {tip}" for i, tip in enumerate(TIPS))
        await ctx.respond(f"**Tips & Tricks**\n\n{formatted}", ephemeral=True)

    @bot.slash_command(name="link", description="Open this conversation in Zo")
    async def link_cmd(ctx: discord.ApplicationContext):
        if not isinstance(ctx.channel, discord.Thread):
            await ctx.respond("Use this command inside a thread.", ephemeral=True)
            return

        conv_id = await get_conversation_id(str(ctx.channel.id))
        if not conv_id or conv_id == "":
            await ctx.respond("No conversation linked to this thread yet.", ephemeral=True)
            return

        backend = await _get_channel_backend(ctx)
        if _is_hermes_ctx(backend):
            await ctx.respond(
                f"**Hermes session:** `{conv_id}`\n\n"
                "*Hermes sessions don't have a web UI link. "
                "Use the CLI to inspect session state.*",
                ephemeral=True,
            )
            return

        handle = os.environ.get("ZO_USER", "")
        if handle:
            url = f"https://{handle}.zo.computer/?chat={conv_id}"
        else:
            url = f"https://zo.computer/?chat={conv_id}"

        await ctx.respond(
            f"Open in Zo: {url}",
            ephemeral=True,
        )

    @bot.slash_command(name="model", description="View or change the default AI model")
    async def model_cmd(ctx: discord.ApplicationContext):
        config = load_config()
        global_model = config.get("model")
        aliases = config.get("model_aliases", {})

        channel = ctx.channel
        if isinstance(channel, discord.Thread) and channel.parent:
            channel = channel.parent
        channel_id = str(channel.id)

        ch_config = await get_channel_config(channel_id)
        channel_model = ch_config.get("model") if ch_config else None

        lines = [
            f"**Global default:** {_display_model(global_model)}",
            f"**#{channel.name} default:** {_display_model(channel_model) if channel_model else 'Not set (using global)'}",
        ]

        lines.append("")
        lines.append("**Aliases** (use in the form below or use `/alias`, e.g. `/opus`, as a prompt prefix to set the model for a new conversation):")
        if aliases:
            for alias, model_id in aliases.items():
                lines.append(f"- `{alias}` → `{model_id}`")
        else:
            lines.append("Not set")

        lines.append("")
        lines.append(
            "*Don't know your model ID? Ask Zo to set aliases "
            "(e.g. \"set /cc-opus to my Claude Code Opus model\"). "
            "For BYOK models (Claude Code / Codex), Zo will find your key more easily if you use the model itself for the conversation.*"
        )

        view = ModelSelectView(bot, global_model, channel_model, channel_id)
        await _defer_then_followup(
            ctx,
            "\n".join(lines),
            view=view,
            ephemeral=True,
        )

    @bot.slash_command(name="persona", description="View or change the default persona")
    async def persona_cmd(ctx: discord.ApplicationContext):
        config = load_config()
        global_persona = config.get("default_persona")
        aliases = config.get("persona_aliases", {})

        channel = ctx.channel
        if isinstance(channel, discord.Thread) and channel.parent:
            channel = channel.parent
        channel_id = str(channel.id)

        ch_config = await get_channel_config(channel_id)
        channel_persona = ch_config.get("persona_id") if ch_config else None

        lines = [
            f"**Global default:** {_display_persona(global_persona) if global_persona else 'Not set (using Zo default)'}",
            f"**#{channel.name} default:** {_display_persona(channel_persona) if channel_persona else 'Not set (using global)'}",
        ]

        lines.append("")
        lines.append("**Aliases** (use in the form below or use `@alias`, e.g. `@pirate`, as a prompt prefix to set the persona for a new conversation):")
        if aliases:
            for alias, persona_id in aliases.items():
                lines.append(f"- `{alias}` → `{persona_id}`")
        else:
            lines.append("Not set")

        lines.append("")
        lines.append(
            "*Don't know your persona ID? Ask Zo to list your personas and set aliases "
            "(e.g. \"set @pirate to my Pirate persona\"). "
            "Zo will find your persona ID more easily if you use the persona itself for the conversation.*"
        )

        view = PersonaSelectView(bot, global_persona, channel_persona, channel_id)
        await _defer_then_followup(
            ctx,
            "\n".join(lines),
            view=view,
            ephemeral=True,
        )

    @bot.slash_command(name="thinking", description="Toggle thinking mode (streaming/quiet)")
    async def thinking_cmd(ctx: discord.ApplicationContext):
        current = getattr(bot, "_thinking_mode", "streaming")
        view = ThinkingSelectView(bot)
        await ctx.respond(
            f"**Current mode:** {current}\n\n"
            "**Streaming** — Zo's intermediate thinking posted as italicized messages\n"
            "**Quiet** — Only the final response is shown",
            view=view,
            ephemeral=True,
        )

    @bot.slash_command(name="auto-archive", description="Configure thread auto-archive behavior")
    async def auto_archive_cmd(ctx: discord.ApplicationContext):
        current = getattr(bot, "_auto_archive_override", True)
        status = "disabled (threads stay open)" if current else "enabled (Discord manages)"
        view = AutoArchiveSelectView(bot)
        await ctx.respond(
            f"**Auto-archive:** {status}",
            view=view,
            ephemeral=True,
        )

    @bot.slash_command(name="buffer", description="Configure message buffering (debounce)")
    async def buffer_cmd(ctx: discord.ApplicationContext):
        config = load_config()
        global_buffer = config.get("buffer_seconds", 0)

        channel = ctx.channel
        if isinstance(channel, discord.Thread) and channel.parent:
            channel = channel.parent
        channel_id = str(channel.id)

        ch_config = await get_channel_config(channel_id)
        channel_buffer = ch_config.get("buffer_seconds") if ch_config else None

        # Compute effective value
        effective = channel_buffer if channel_buffer is not None else global_buffer
        effective_str = f"{effective}s" if effective > 0 else "Disabled"

        global_str = f"{global_buffer}s" if global_buffer > 0 else "Disabled (0s)"
        if channel_buffer is not None:
            channel_str = f"{channel_buffer}s" if channel_buffer > 0 else "Disabled (0s)"
        else:
            channel_str = "Not set (using global)"

        lines = [
            f"**Global default:** {global_str}",
            f"**#{channel.name} default:** {channel_str}",
            f"**Effective:** {effective_str}",
            "",
            "When enabled, rapid-fire messages are combined into a single "
            "request before Zo starts processing. Each new message resets "
            "the countdown. The buffer pauses while you're typing, so "
            "you won't feel rushed.",
            "",
            "*Set to 0 to disable.*",
        ]

        view = BufferSelectView(bot, global_buffer, channel_buffer, channel_id)
        await ctx.respond("\n".join(lines), view=view, ephemeral=True)

    @bot.slash_command(name="instructions", description="View this channel's custom instructions")
    async def instructions_cmd(ctx: discord.ApplicationContext):
        channel = ctx.channel
        if isinstance(channel, discord.Thread) and channel.parent:
            channel = channel.parent

        config = await get_channel_config(str(channel.id))
        if config and config.get("instructions"):
            await ctx.respond(
                f"**Instructions for #{channel.name}:**\n\n{config['instructions']}",
                ephemeral=True,
            )
        else:
            await ctx.respond(
                f"No custom instructions set for #{channel.name}. Ask Zo to set them.",
                ephemeral=True,
            )

    @bot.slash_command(name="memory", description="View this channel's memory paths")
    async def memory_cmd(ctx: discord.ApplicationContext):
        channel = ctx.channel
        if isinstance(channel, discord.Thread) and channel.parent:
            channel = channel.parent

        config = await get_channel_config(str(channel.id))
        if config and config.get("memory_paths"):
            paths = "\n".join(f"- `{p}`" for p in config["memory_paths"])
            await ctx.respond(
                f"**Memory paths for #{channel.name}:**\n{paths}",
                ephemeral=True,
            )
        else:
            await ctx.respond(
                f"No memory paths set for #{channel.name}. zo-discord has no built-in memory system — plug in your own and ask Zo to set the file paths for this channel.",
                ephemeral=True,
            )

    @bot.slash_command(name="allowed-users", description="Manage allowed Discord users")
    async def allowed_users_cmd(ctx: discord.ApplicationContext):
        config = load_config()
        allowed = config.get("allowed_users", [])
        if allowed:
            user_list = "\n".join(f"- `{uid}`" for uid in allowed)
        else:
            user_list = "(empty — all users allowed)"
        view = AllowedUsersView(bot)
        await ctx.respond(
            f"**Allowed users:**\n{user_list}\n\n"
            "*To get a user ID, enable Developer Mode in Discord settings (App Settings → Advanced → Developer Mode), "
            "then right-click a user and select Copy User ID.*",
            view=view,
            ephemeral=True,
        )

    @bot.slash_command(name="backend", description="View or change the AI backend (Zo or Hermes)")
    async def backend_cmd(ctx: discord.ApplicationContext):
        config = load_config()
        global_backend = config.get("backend", "zo")

        channel = ctx.channel
        if isinstance(channel, discord.Thread) and channel.parent:
            channel = channel.parent
        channel_id = str(channel.id)

        ch_config = await get_channel_config(channel_id)
        channel_backend = ch_config.get("backend") if ch_config else None

        effective = channel_backend or global_backend

        lines = [
            f"**Global default:** {global_backend}",
            f"**#{channel.name}:** {channel_backend or 'Not set (using global)'}",
            f"**Effective:** {effective}",
            "",
            "**Zo** — Zo Cloud API (remote, full Zo tool suite)\n"
            "**Hermes** — Local Hermes agent (localhost:8788, extended thinking, cancel support)",
        ]

        view = BackendSelectView(bot, global_backend, channel_backend, channel_id)
        await ctx.respond("\n".join(lines), view=view, ephemeral=True)

    @bot.slash_command(name="cli", description="Show zo-discord CLI commands available to Zo")
    async def cli_cmd(ctx: discord.ApplicationContext):
        await ctx.respond(
            "**zo-discord CLI** (prefer explicit `--conv-id`)\n\n"
            '- `zo-discord --conv-id <id> rename "Title"` — Rename the thread\n'
            "- `zo-discord --conv-id <id> error` — Set thread status to error\n"
            '- `zo-discord notify "Title" "content" --channel-name NAME` — Post to a new thread\n'
            "  - `--file /path/to/file` — Post file contents instead\n"
            '- `zo-discord --conv-id <id> buttons "Prompt?" "Yes:success" "No:danger"` — Send interactive buttons\n'
            "  - `--preset yes_no|approve_reject` — Use a preset\n"
            '- `zo-discord --conv-id <id> new-thread "Title" "prompt" --channel-name NAME` — Spawn a new thread\n'
            "  - `--prompt-file /path/to/file` or piped stdin — Safer prompt input for long or quoted text",
            ephemeral=True,
        )

    # --- Channel config slash commands ---

    @bot.slash_command(name="reasoning", description="Set reasoning effort for this channel")
    async def reasoning_cmd(
        ctx: discord.ApplicationContext,
        level: discord.Option(
            str,
            description="Reasoning effort level",
            choices=["off", "low", "medium", "high"],
            required=False,
        ) = None,
    ):
        if not await _require_hermes(ctx):
            return
        channel = _get_parent_channel(ctx)
        channel_id = str(channel.id)
        ch_config = await get_channel_config(channel_id)
        current = ch_config.get("reasoning") if ch_config else None

        if level is None:
            display = current or "Not set (using default)"
            await ctx.respond(f"**Reasoning effort:** {display}", ephemeral=True)
            return

        await set_channel_config(channel_id, reasoning=level)
        await ctx.respond(f"Reasoning effort set to **{level}** for #{channel.name}.", ephemeral=True)

    @bot.slash_command(name="tools", description="View enabled/disabled toolsets for this channel")
    async def tools_cmd(ctx: discord.ApplicationContext):
        if not await _require_hermes(ctx):
            return
        channel = _get_parent_channel(ctx)
        channel_id = str(channel.id)
        ch_config = await get_channel_config(channel_id)

        enabled = ch_config.get("enabled_toolsets") if ch_config else None
        disabled = ch_config.get("disabled_toolsets") if ch_config else None

        lines = [f"**Toolsets for #{channel.name}:**"]
        if enabled:
            lines.append(f"Enabled: `{', '.join(enabled)}`")
        else:
            lines.append("Enabled: all (default)")
        if disabled:
            lines.append(f"Disabled: `{', '.join(disabled)}`")
        else:
            lines.append("Disabled: none")

        lines.append("")
        lines.append(f"Available: `{', '.join(AVAILABLE_TOOLSETS)}`")
        lines.append("")
        lines.append("*Ask the agent to change tools conversationally — it can update them via the config API.*")

        await ctx.respond("\n".join(lines), ephemeral=True)

    @bot.slash_command(name="max-iterations", description="Set max iterations for this channel")
    async def max_iterations_cmd(
        ctx: discord.ApplicationContext,
        value: discord.Option(int, description="Max iterations (blank to view current)", required=False) = None,
    ):
        if not await _require_hermes(ctx):
            return
        channel = _get_parent_channel(ctx)
        channel_id = str(channel.id)
        ch_config = await get_channel_config(channel_id)
        current = ch_config.get("max_iterations") if ch_config else None

        hermes_cfg = _read_hermes_config()
        global_default = hermes_cfg.get("agent", {}).get("max_turns", 200)

        if value is None:
            display = str(current) if current is not None else f"Not set (global default: {global_default})"
            await ctx.respond(f"**Max iterations:** {display}", ephemeral=True)
            return

        if value < 1:
            await ctx.respond("Value must be at least 1.", ephemeral=True)
            return

        await set_channel_config(channel_id, max_iterations=value)
        await ctx.respond(f"Max iterations set to **{value}** for #{channel.name}. (Global default: {global_default})", ephemeral=True)

    @bot.slash_command(name="skip-memory", description="Toggle memory skip for this channel")
    async def skip_memory_cmd(ctx: discord.ApplicationContext):
        if not await _require_hermes(ctx):
            return
        channel = _get_parent_channel(ctx)
        channel_id = str(channel.id)
        ch_config = await get_channel_config(channel_id)
        current = bool(ch_config.get("skip_memory")) if ch_config else False

        new_value = not current
        await set_channel_config(channel_id, skip_memory=new_value)
        state = "on" if new_value else "off"
        await ctx.respond(f"**Skip memory:** {state} for #{channel.name}.", ephemeral=True)

    @bot.slash_command(name="skip-context", description="Toggle context skip for this channel")
    async def skip_context_cmd(ctx: discord.ApplicationContext):
        if not await _require_hermes(ctx):
            return
        channel = _get_parent_channel(ctx)
        channel_id = str(channel.id)
        ch_config = await get_channel_config(channel_id)
        current = bool(ch_config.get("skip_context")) if ch_config else False

        new_value = not current
        await set_channel_config(channel_id, skip_context=new_value)
        state = "on" if new_value else "off"
        await ctx.respond(f"**Skip context:** {state} for #{channel.name}.", ephemeral=True)

    # --- Global config command ---

    @bot.slash_command(name="compression-threshold", description="View/set Hermes compression threshold")
    async def compression_threshold_cmd(
        ctx: discord.ApplicationContext,
        value: discord.Option(float, description="Threshold (0.0-1.0, blank to view)", required=False) = None,
    ):
        if not await _require_hermes(ctx):
            return
        hermes_cfg = _read_hermes_config()
        current = hermes_cfg.get("compression", {}).get("threshold")

        if value is None:
            display = str(current) if current is not None else "Not set"
            await ctx.respond(f"**Compression threshold:** {display}", ephemeral=True)
            return

        if not (0.0 <= value <= 1.0):
            await ctx.respond("Value must be between 0.0 and 1.0.", ephemeral=True)
            return

        hermes_cfg.setdefault("compression", {})["threshold"] = value
        _write_hermes_config(hermes_cfg)
        await ctx.respond(f"Compression threshold set to **{value}**.", ephemeral=True)

    # --- Message mode commands ---

    @bot.slash_command(name="queue", description="Set message mode to queue (batch messages)")
    async def queue_cmd(ctx: discord.ApplicationContext):
        if not await _require_hermes(ctx):
            return
        channel = _get_parent_channel(ctx)
        await set_channel_config(str(channel.id), message_mode="queue")
        await _defer_then_followup(
            ctx,
            "📥 **Queue mode** — messages will be batched and sent after the agent finishes. Use `/interrupt` to switch.",
            ephemeral=True,
        )

    @bot.slash_command(name="interrupt", description="Set message mode to interrupt (cancel current turn)")
    async def interrupt_cmd(ctx: discord.ApplicationContext):
        if not await _require_hermes(ctx):
            return
        channel = _get_parent_channel(ctx)
        await set_channel_config(str(channel.id), message_mode="interrupt")
        await _defer_then_followup(
            ctx,
            "⚡ **Interrupt mode** — messages will cancel the current turn and be injected immediately. Use `/queue` to switch.",
            ephemeral=True,
        )

    # --- Session management commands (call zo-hermes endpoints) ---

    async def _get_session_id(ctx: discord.ApplicationContext) -> str | None:
        """Get the hermes session_id for the current thread."""
        if not isinstance(ctx.channel, discord.Thread):
            return None
        return await get_conversation_id(str(ctx.channel.id))

    @bot.slash_command(name="stop", description="Cancel the current agent turn")
    async def stop_cmd(ctx: discord.ApplicationContext):
        if not await _require_hermes(ctx):
            return
        session_id = await _get_session_id(ctx)
        if not session_id:
            await ctx.respond("Nothing running.", ephemeral=True)
            return

        status_code, body = await _hermes_post("/cancel", {"session_id": session_id})
        if status_code == 200:
            if isinstance(ctx.channel, discord.Thread) and hasattr(bot, "mark_thread_cancelled"):
                bot.mark_thread_cancelled(str(ctx.channel.id))
            await ctx.respond("⏹️ Cancelled.", ephemeral=True)
        elif status_code == 404:
            await ctx.respond("Nothing running.", ephemeral=True)
        else:
            await ctx.respond(f"Error: {body.get('error', 'Unknown error')}", ephemeral=True)

    @bot.slash_command(name="undo", description="Undo the last exchange")
    async def undo_cmd(ctx: discord.ApplicationContext):
        if not await _require_hermes(ctx):
            return
        session_id = await _get_session_id(ctx)
        if not session_id:
            await ctx.respond("No session active in this thread.", ephemeral=True)
            return

        status_code, body = await _hermes_post("/undo", {"session_id": session_id})
        if status_code != 200:
            await ctx.respond(f"Error: {body.get('error', 'Unknown error')}", ephemeral=True)
            return

        # Try to react to recent bot messages in this thread
        removed_count = body.get("removed_count", 0)
        if isinstance(ctx.channel, discord.Thread) and removed_count > 0:
            await _mark_last_exchange_undone(ctx.channel, bot.user, removed_count)

        await ctx.respond(
            f"↩️ Last exchange undone. ({body.get('removed_count', 0)} messages removed)",
            ephemeral=True,
        )

    @bot.slash_command(name="retry", description="Undo and re-send the last message")
    async def retry_cmd(ctx: discord.ApplicationContext):
        if not await _require_hermes(ctx):
            return
        session_id = await _get_session_id(ctx)
        if not session_id:
            await ctx.respond("No session active in this thread.", ephemeral=True)
            return

        thread_id = str(ctx.channel.id) if isinstance(ctx.channel, discord.Thread) else None
        last_msg = bot._last_user_messages.get(thread_id) if thread_id else None
        if not last_msg:
            await ctx.respond("No cached user message to retry.", ephemeral=True)
            return

        # Undo first
        status_code, body = await _hermes_post("/undo", {"session_id": session_id})
        if status_code != 200:
            await ctx.respond(f"Undo failed: {body.get('error', 'Unknown error')}", ephemeral=True)
            return

        if isinstance(ctx.channel, discord.Thread):
            await _mark_last_exchange_undone(ctx.channel, bot.user, body.get("removed_count", 0))
            try:
                await ctx.channel.send("# Retried Message")
            except Exception as e:
                logger.warning("Failed to send retry separator message: %s", e)

        await ctx.respond("🔄 Retrying last message...", ephemeral=True)

        # Re-send through the bot's normal streaming pipeline
        import asyncio
        asyncio.create_task(bot.retry_in_thread(ctx.channel))

    @bot.slash_command(name="status", description="Show session status")
    async def status_cmd(ctx: discord.ApplicationContext):
        if not await _require_hermes(ctx):
            return
        session_id = await _get_session_id(ctx)
        if not session_id:
            await ctx.respond("No session active in this thread.", ephemeral=True)
            return

        status_code, body = await _hermes_get("/status", {"session_id": session_id})
        if status_code != 200:
            await ctx.respond(f"Error: {body.get('error', 'Unknown error')}", ephemeral=True)
            return

        state = body.get("state", "unknown")
        state_emoji = "🟢" if state == "running" else "⚪"
        lines = [f"{state_emoji} **State:** {state}"]

        if body.get("model"):
            lines.append(f"**Model:** `{body['model']}`")
        if body.get("iterations_used") is not None:
            lines.append(f"**Iterations:** {body['iterations_used']}/{body.get('iterations_max', '?')}")
        if body.get("input_tokens"):
            lines.append(f"**Tokens:** {body['input_tokens']:,} in / {body.get('output_tokens', 0):,} out")
        if body.get("api_calls"):
            lines.append(f"**API calls:** {body['api_calls']}")
        if body.get("message_count"):
            lines.append(f"**Messages:** {body['message_count']}")

        await ctx.respond("\n".join(lines), ephemeral=True)

    @bot.slash_command(name="usage", description="Show token usage for this session")
    async def usage_cmd(ctx: discord.ApplicationContext):
        if not await _require_hermes(ctx):
            return
        session_id = await _get_session_id(ctx)
        if not session_id:
            await ctx.respond("No session active in this thread.", ephemeral=True)
            return

        status_code, body = await _hermes_get("/usage", {"session_id": session_id})
        if status_code != 200:
            await ctx.respond(f"Error: {body.get('error', 'Unknown error')}", ephemeral=True)
            return

        lines = ["**Session Usage**"]

        if body.get("model"):
            lines.append(f"Model: `{body['model']}`")

        if body.get("input_tokens") is not None:
            lines.append(f"Input: {body['input_tokens']:,} tokens")
            lines.append(f"Output: {body.get('output_tokens', 0):,} tokens")
            if body.get("cache_read_tokens"):
                lines.append(f"Cache read: {body['cache_read_tokens']:,}")
            if body.get("cache_write_tokens"):
                lines.append(f"Cache write: {body['cache_write_tokens']:,}")

        if body.get("total_tokens"):
            lines.append(f"Total: {body['total_tokens']:,} tokens")
        if body.get("api_calls"):
            lines.append(f"API calls: {body['api_calls']}")

        if body.get("context_used_pct") is not None:
            pct = body["context_used_pct"]
            bar_filled = round(pct / 10)
            bar = "█" * bar_filled + "░" * (10 - bar_filled)
            lines.append(f"Context: {bar} {pct}%")

        if body.get("cost_usd") is not None:
            lines.append(f"Cost: ${body['cost_usd']:.4f}")

        if body.get("compression_count"):
            lines.append(f"Compressions: {body['compression_count']}")

        if body.get("note"):
            lines.append(f"*{body['note']}*")

        await ctx.respond("\n".join(lines), ephemeral=True)

    @bot.slash_command(name="compress", description="Compress session context")
    async def compress_cmd(ctx: discord.ApplicationContext):
        if not await _require_hermes(ctx):
            return
        session_id = await _get_session_id(ctx)
        if not session_id:
            await ctx.respond("No session active in this thread.", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)

        status_code, body = await _hermes_post("/compress", {"session_id": session_id})
        if status_code != 200:
            await ctx.followup.send(f"Error: {body.get('error', 'Unknown error')}", ephemeral=True)
            return

        lines = ["🗜️ Context compressed."]
        before = body.get("before", {})
        after = body.get("after", {})
        if before and after:
            lines.append(
                f"Messages: {before.get('messages', '?')} → {after.get('messages', '?')}"
            )
            if before.get("tokens") and after.get("tokens"):
                saved = before["tokens"] - after["tokens"]
                lines.append(
                    f"Tokens: {before['tokens']:,} → {after['tokens']:,} (saved {saved:,})"
                )

        if body.get("previous_session_id"):
            new_sid = body.get("session_id")
            lines.append(f"Session ID changed: `{new_sid}`")
            # Update the DB with new session ID
            if isinstance(ctx.channel, discord.Thread):
                await update_conversation_id(str(ctx.channel.id), new_sid)
                if hasattr(bot, "_thread_digest_needed"):
                    bot._thread_digest_needed.add(str(ctx.channel.id))

        await ctx.followup.send("\n".join(lines), ephemeral=True)
