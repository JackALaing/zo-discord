"""
Slash commands for zo-discord.
"""

import json
import os
from pathlib import Path

import discord
from discord import ui
from zo_discord import PROJECT_ROOT
from zo_discord.db import get_channel_config, set_channel_config, get_conversation_id
from zo_discord.zo_client import load_config

CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"


def _save_config_key(key: str, value):
    """Update a single key in config.json and write back."""
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    config[key] = value
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")

TIPS = [
    "zo-discord supports **message queuing**. Send multiple messages while Zo is thinking — they'll be batched into one message when the current turn finishes.",
    "Use zo-discord as a **notification channel** for scheduled tasks instead of SMS/email/Telegram. See the zo-discord skill for more details.",
    "zo-discord can override Discord's **auto-archive** behavior to keep threads open until you manually archive them. React with :white_check_mark: to any message in the thread to archive it. Set this as your double-tap reaction on mobile for quick archiving.",
    "You can set **model and persona per-channel** using the `/model` and `/persona` commands. Models and personas use IDs that are hard to remember, so you can ask Zo to set aliases, then use the alias. You can also prefix your prompt with `/model-alias` (e.g. `/opus`) and `@persona-alias` (e.g. `@pirate`) to override the channel default. For example, you could set Sonnet as your default model for the channel, but prefix a prompt with `/opus` to use Opus for just that conversation.",
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


class AllowedUsersView(ui.View):
    def __init__(self, bot):
        super().__init__(timeout=120)
        self.bot = bot

    @ui.button(label="Add/Remove User", style=discord.ButtonStyle.primary)
    async def toggle_user(self, button: ui.Button, interaction: discord.Interaction):
        modal = AllowedUserModal(self.bot)
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


def setup_commands(bot):
    """Register all slash commands on the bot."""

    @bot.slash_command(name="help", description="Show bot info and available commands")
    async def help_cmd(ctx: discord.ApplicationContext):
        config = load_config()
        model_display = _display_model(config.get("model"))
        conv_id = None
        if isinstance(ctx.channel, discord.Thread):
            conv_id = await get_conversation_id(str(ctx.channel.id))

        lines = [
            "**zo-discord**",
            f"Model: {model_display}",
        ]
        if conv_id:
            lines.append(f"Conversation: `{conv_id}`")
        lines.append("")
        lines.append("**Commands**")
        lines.append("`/help` — This message")
        lines.append("`/tips` — Tips and tricks")
        lines.append("`/link` — Open conversation in Zo")
        lines.append("`/model` — View/change default model")
        lines.append("`/persona` — View/change default persona")
        lines.append("`/thinking` — Toggle thinking mode")
        lines.append("`/auto-archive` — Configure auto-archive")
        lines.append("`/instructions` — View channel instructions")
        lines.append("`/memory` — View channel memory paths")
        lines.append("`/allowed-users` — Manage allowed users")
        lines.append("`/cli` — Show CLI commands")

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
        await ctx.respond(
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
        await ctx.respond(
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

    @bot.slash_command(name="cli", description="Show zo-discord CLI commands available to Zo")
    async def cli_cmd(ctx: discord.ApplicationContext):
        await ctx.respond(
            "**zo-discord CLI** (used by Zo, auto-detects conversation ID)\n\n"
            '- `zo-discord rename "Title"` — Rename the thread\n'
            "- `zo-discord error` — Set thread status to error\n"
            '- `zo-discord notify "Title" "content" --channel-name NAME` — Post to a new thread\n'
            "  - `--file /path/to/file` — Post file contents instead\n"
            '- `zo-discord buttons "Prompt?" "Yes:success" "No:danger"` — Send interactive buttons\n'
            "  - `--preset yes_no|approve_reject` — Use a preset\n"
            '- `zo-discord new-thread "Title" "prompt"` — Spawn a new thread',
            ephemeral=True,
        )
