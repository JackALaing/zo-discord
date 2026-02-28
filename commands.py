"""
Slash commands for zo-discord.
"""

import json
from pathlib import Path

import discord
from discord import ui
from db import get_channel_config, set_channel_config, get_conversation_id
from zo_client import load_config

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "config.json"


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
    "Use zo-discord as a **notification channel** for scheduled tasks instead of SMS/email/Telegram. See the `agents/` folder for examples.",
    "zo-discord can override Discord's **auto-archive** behavior to keep threads open until you manually archive them.",
    "**Archive threads** by reacting with :white_check_mark: to any message in the thread. Set this as your double-tap reaction on mobile for quick archiving.",
    "Set a **per-channel model** by configuring it in your `config.json`. All new conversations in that channel will use that model.",
    "zo-discord automatically recognizes **new channels**. Just create a channel and send a message to initialize it.",
    "zo-discord supports **per-channel instructions and memory paths**, but has no built-in memory system. Plug in your own memory system to maintain these file paths.",
    "**Reply to specific messages** in a thread — Zo will see which message you're responding to and include it as context.",
]


class ModelSelectView(ui.View):
    def __init__(self, bot, current_model: str | None):
        super().__init__(timeout=120)
        self.bot = bot
        self.current_model = current_model

    @ui.button(label="Change Model", style=discord.ButtonStyle.primary)
    async def change_model(self, button: ui.Button, interaction: discord.Interaction):
        modal = ModelInputModal(self.bot, self.current_model)
        await interaction.response.send_modal(modal)


class ModelInputModal(ui.Modal):
    def __init__(self, bot, current_model: str | None):
        super().__init__(title="Set Default Model")
        self.bot = bot
        self.model_input = ui.InputText(
            label="Model name (e.g. claude-sonnet-4-5)",
            placeholder="Leave empty to use Zo's default",
            value=current_model or "",
            required=False,
        )
        self.add_item(self.model_input)

    async def callback(self, interaction: discord.Interaction):
        new_model = self.model_input.value.strip() or None
        _save_config_key("model", new_model)
        self.bot.zo.model = new_model
        display = new_model or "Zo's default"
        await interaction.response.send_message(
            f"Default model updated to **{display}**.",
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
        model = config.get("model") or "Zo's default"
        conv_id = None
        if isinstance(ctx.channel, discord.Thread):
            conv_id = await get_conversation_id(str(ctx.channel.id))

        lines = [
            "**zo-discord**",
            f"Model: `{model}`",
        ]
        if conv_id:
            lines.append(f"Conversation: `{conv_id}`")
        lines.append("")
        lines.append("**Commands**")
        lines.append("`/help` — This message")
        lines.append("`/tips` — Tips and tricks")
        lines.append("`/link` — Open conversation in Zo")
        lines.append("`/model` — View/change default model")
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

        await ctx.respond(
            f"Open in Zo: https://zo.computer/?c={conv_id}",
            ephemeral=True,
        )

    @bot.slash_command(name="model", description="View or change the default AI model")
    async def model_cmd(ctx: discord.ApplicationContext):
        config = load_config()
        current = config.get("model") or "Not set (using Zo's default)"
        view = ModelSelectView(bot, config.get("model"))
        await ctx.respond(
            f"**Current model:** `{current}`",
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
                f"No memory paths set for #{channel.name}. Ask Zo to set them.",
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
            f"**Allowed users:**\n{user_list}",
            view=view,
            ephemeral=True,
        )

    @bot.slash_command(name="cli", description="Show zo-discord CLI commands available to Zo")
    async def cli_cmd(ctx: discord.ApplicationContext):
        await ctx.respond(
            "**zo-discord CLI** (used by Zo, auto-detects conversation ID)\n\n"
            "```\n"
            'zo-discord rename "Title"              — Rename the thread\n'
            "zo-discord error                       — Set thread status to error\n"
            'zo-discord notify "Title" "content"    — Post to a new thread\n'
            "  --channel-name NAME                    (target channel by name)\n"
            "  --file /path/to/file                   (post file contents)\n"
            'zo-discord buttons "Prompt?" "Yes:success" "No:danger"\n'
            "                                       — Send interactive buttons\n"
            "  --preset yes_no|approve_reject         (use a preset)\n"
            'zo-discord new-thread "Title" "prompt" — Spawn new thread\n'
            "```",
            ephemeral=True,
        )
