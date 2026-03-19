#!/usr/bin/env python3
"""
Discord-Zo Bridge Bot

A Discord bot that creates threaded conversations with Zo, maintaining
persistent sessions, channel-specific context, interactive buttons,
status tracking, and file attachments.
"""

import asyncio
import discord
from discord.ext import commands
from discord import ui
import aiohttp
from aiohttp import web
import json
import os
import re
import sys
import logging
import uuid
from pathlib import Path

from zo_discord.db import (
    init_db, save_mapping, get_conversation_id, update_activity,
    get_active_threads, update_thread_name, update_conversation_id,
    get_channel_config, set_channel_config, delete_channel_config,
    update_thread_status, get_thread_status, get_mapping_by_conversation,
    set_watched, is_watched, get_all_watched_threads,
)
from zo_discord.zo_client import ZoClient, load_config
from zo_discord.commands import setup_commands
from zo_discord.utils import (
    STATUS_EMOJI, set_thread_status_prefix, strip_status_prefix,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

CONFIG = load_config()


DISCORD_BASE_DIR = Path(CONFIG.get("data_dir", "discord_data")).resolve()


def get_channel_dir(channel_name: str) -> Path:
    """Get or create the Discord/{channel}/ directory."""
    safe_name = re.sub(r'[^\w\-]', '_', channel_name.lower()).strip('_')
    channel_dir = DISCORD_BASE_DIR / safe_name
    channel_dir.mkdir(parents=True, exist_ok=True)
    return channel_dir


def get_attachments_dir(channel_name: str) -> Path:
    """Get or create the Discord/{channel}/attachments/ directory."""
    att_dir = get_channel_dir(channel_name) / "attachments"
    att_dir.mkdir(parents=True, exist_ok=True)
    return att_dir


async def send_suppressed(channel, **kwargs):
    """Send a message then immediately edit it to suppress link embeds.

    Enforces Discord's 2000-char limit: if content exceeds the limit,
    it's split into multiple messages automatically.
    """
    DISCORD_LIMIT = 2000
    content = kwargs.get("content", "")

    async def _send_chunk(chunk_content, is_first, ref_kwargs):
        send_kwargs = {**ref_kwargs, "content": chunk_content}
        if not is_first:
            send_kwargs.pop("reference", None)
            send_kwargs.pop("mention_author", None)
        try:
            msg = await channel.send(**send_kwargs)
        except discord.HTTPException as e:
            if e.code == 50035 and "Must be 2000 or fewer" in str(e):
                logger.warning(f"send_suppressed: Discord rejected {len(chunk_content)} chars, hard-splitting")
                msgs = []
                remaining = chunk_content
                first = is_first
                while remaining:
                    part = remaining[:DISCORD_LIMIT]
                    remaining = remaining[DISCORD_LIMIT:]
                    m = await _send_chunk(part, first, ref_kwargs)
                    if m:
                        msgs.append(m)
                    first = False
                return msgs[-1] if msgs else None
            raise
        try:
            await msg.edit(suppress=True)
        except Exception as edit_err:
            logger.warning(f"Failed to suppress embeds on message {msg.id}: {edit_err}")
        return msg

    if len(content) <= DISCORD_LIMIT:
        return await _send_chunk(content, True, kwargs)

    logger.warning(f"send_suppressed: content is {len(content)} chars, splitting at {DISCORD_LIMIT}")
    last_msg = None
    is_first = True
    while content:
        chunk = content[:DISCORD_LIMIT]
        content = content[DISCORD_LIMIT:]
        last_msg = await _send_chunk(chunk, is_first, kwargs)
        is_first = False
    return last_msg


class ButtonCallbackView(ui.View):
    """A view with buttons that posts the user's choice back to the Zo conversation."""

    def __init__(self, bot, thread_id: str, buttons: list[dict], timeout_seconds: int = 3600):
        super().__init__(timeout=timeout_seconds)
        self.bot = bot
        self.thread_id = thread_id

        for btn in buttons:
            button = ui.Button(
                label=btn["label"],
                custom_id=btn.get("id", btn["label"].lower().replace(" ", "_")),
                style=self._parse_style(btn.get("style", "primary")),
            )
            button.callback = self._make_callback(btn["label"], btn.get("id", btn["label"]))
            self.add_item(button)

    def _parse_style(self, style: str) -> discord.ButtonStyle:
        styles = {
            "primary": discord.ButtonStyle.primary,
            "secondary": discord.ButtonStyle.secondary,
            "success": discord.ButtonStyle.success,
            "danger": discord.ButtonStyle.danger,
        }
        return styles.get(style, discord.ButtonStyle.primary)

    def _make_callback(self, label: str, button_id: str):
        async def callback(interaction: discord.Interaction):
            await interaction.response.edit_message(
                content=f"**{interaction.user.display_name}** selected: **{label}**",
                view=None
            )
            conv_id = await get_conversation_id(self.thread_id)
            if conv_id:
                thread = self.bot.get_channel(int(self.thread_id))
                if thread:
                    on_thinking = self.bot.make_on_thinking(thread)

                    async def on_conv_id(cid: str):
                        if cid != conv_id:
                            await update_conversation_id(self.thread_id, cid)
                        if self.thread_id in self.bot._inflight:
                            self.bot._inflight[self.thread_id]["conv_id"] = cid

                    task = asyncio.current_task()
                    self.bot._inflight[self.thread_id] = {"conv_id": conv_id, "task": task}

                    stop_event = asyncio.Event()
                    typing_task = asyncio.create_task(self.bot.typing_loop(thread, stop_event))
                    try:
                        choice_msg = f"[Button pressed: {label} (id: {button_id})]"
                        btn_backend = None
                        btn_hermes_params = {}
                        if thread.parent:
                            _, _, btn_backend, btn_hermes_params = await self.bot.resolve_channel_defaults(str(thread.parent.id))
                        context, file_paths = await self.bot.build_thread_context(thread, include_source=False, conv_id=conv_id, backend=btn_backend or "")
                        result = await self.bot.zo.ask_stream(
                            choice_msg,
                            conversation_id=conv_id,
                            context=context or None,
                            file_paths=file_paths or None,
                            on_thinking=on_thinking,
                            on_conv_id=on_conv_id,
                            **btn_hermes_params,
                        )
                        response, new_conv_id = result.output, result.conv_id
                        if new_conv_id != conv_id:
                            await update_conversation_id(self.thread_id, new_conv_id)
                        await update_activity(self.thread_id)

                        if not response or not response.strip():
                            logger.warning(f"Empty response for button callback conv {conv_id} (interrupted={result.interrupted})")
                            response, new_conv_id = await self.bot._retry_empty_response(
                                self.thread_id, new_conv_id or conv_id, thread, on_thinking, on_conv_id,
                            )

                        chunks = self.bot.zo.chunk_response(response)
                        chunks = [c for c in chunks if c.strip()]
                        if not chunks:
                            logger.error(f"All retries exhausted for button callback conv {conv_id}")
                            chunks = [f"\u26a0\ufe0f **Zo didn't respond after multiple retries.**\n\nConversation: `{new_conv_id or conv_id}`\n\nSend another message to try again."]
                        for chunk in chunks:
                            await send_suppressed(thread, content=chunk)
                    except asyncio.CancelledError:
                        logger.info(f"Button callback cancelled (interrupted) for thread {self.thread_id}")
                    except Exception as e:
                        logger.error(f"Button callback error: {e}")
                        await self.bot.set_status(thread, "error")
                    finally:
                        self.bot._inflight.pop(self.thread_id, None)
                        stop_event.set()
                        await typing_task
        return callback


class ZoDiscordBot(commands.Bot):

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.members = True
        intents.reactions = True
        intents.typing = True

        super().__init__(
            intents=intents,
            command_prefix="!zo "
        )

        self.zo = ZoClient()
        self.config = CONFIG
        self.http_app = None
        self.http_runner = None
        self._initialized = False
        self.queued_renames = {}
        self._inflight = {}  # thread_id -> {"conv_id": str, "task": asyncio.Task}
        self._message_queues = {}  # thread_id -> asyncio.Queue of discord.Message
        self._bundled_prefixes = {}  # message.id -> str, for passing context to handle_thread_message
        self._presaved_attachments = {}  # message.id -> list[str], pre-saved attachment paths
        self._last_user_messages = {}  # thread_id -> last user message text (for /retry)
        self._thinking_mode = self.config.get("thinking_mode", "streaming")
        self._auto_archive_override = self.config.get("auto_archive_override", True)

        # Buffer state: groups rapid-fire messages before processing
        self._buffer = {}          # channel/thread key -> list[discord.Message]
        self._buffer_tasks = {}    # channel/thread key -> asyncio.Task (countdown)
        self._buffer_typing = {}   # channel/thread key -> float (last on_typing timestamp)
        self._buffer_paused = {}   # channel/thread key -> bool (paused by user typing)
        self._buffer_remaining = {}  # channel/thread key -> float (seconds left when paused)
        self._buffer_typing_tasks = {}  # channel/thread key -> asyncio.Task (typing indicator)
        self._TYPING_TIMEOUT = 10.0  # seconds to wait after last on_typing before unpausing

        setup_commands(self)

    def extract_overrides(self, text: str) -> tuple[str | None, str | None, str]:
        """Extract /model-alias and @persona-alias prefixes in either order.

        Returns (model_id_or_none, persona_id_or_none, remaining_text).
        Supports both "/opus @pirate hello" and "@pirate /opus hello".
        """
        config = load_config()
        model_aliases = config.get("model_aliases", {})
        persona_aliases = config.get("persona_aliases", {})

        model_id = None
        persona_id = None
        remaining = text

        for _ in range(2):
            parts = remaining.split(None, 1)
            if not parts:
                break
            token = parts[0]
            rest = parts[1] if len(parts) > 1 else ""

            if not model_id and token.startswith("/") and token[1:] in model_aliases:
                model_id = model_aliases[token[1:]]
                logger.info(f"Resolved model alias '{token[1:]}' to {model_id}")
                remaining = rest
            elif not persona_id and token.startswith("@") and token[1:] in persona_aliases:
                persona_id = persona_aliases[token[1:]]
                logger.info(f"Resolved persona alias '{token[1:]}' to {persona_id}")
                remaining = rest
            else:
                break

        return model_id, persona_id, remaining

    async def get_buffer_seconds(self, channel_id: str) -> float:
        """Get effective buffer_seconds for a channel (channel override > global config)."""
        ch_config = await get_channel_config(channel_id)
        if ch_config and ch_config.get("buffer_seconds") is not None:
            return float(ch_config["buffer_seconds"])
        return float(self.config.get("buffer_seconds", 0))

    async def resolve_channel_defaults(self, channel_id: str) -> tuple[str | None, str | None, str | None, dict]:
        """Get the effective model, persona, backend, and hermes params for a channel.

        Returns (model_id_or_none, persona_id_or_none, backend_or_none, hermes_params_dict) from channel_config.
        """
        ch_config = await get_channel_config(channel_id)
        if not ch_config:
            return None, None, None, {}
        hermes_params = {}
        if ch_config.get("reasoning"):
            hermes_params["reasoning_effort"] = ch_config["reasoning"]
        if ch_config.get("max_iterations"):
            hermes_params["max_iterations"] = ch_config["max_iterations"]
        if ch_config.get("skip_memory"):
            hermes_params["skip_memory"] = True
        if ch_config.get("skip_context"):
            hermes_params["skip_context"] = True
        if ch_config.get("enabled_toolsets"):
            hermes_params["enabled_toolsets"] = ch_config["enabled_toolsets"]
        if ch_config.get("disabled_toolsets"):
            hermes_params["disabled_toolsets"] = ch_config["disabled_toolsets"]
        return ch_config.get("model"), ch_config.get("persona_id"), ch_config.get("backend"), hermes_params

    async def on_ready(self):
        if not self._initialized:
            self._initialized = True
            await init_db()
            logger.info("Database initialized")
            await self.start_http_server()
            self._start_thread_watcher()

        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        logger.info(f"Connected to {len(self.guilds)} guild(s)")
        for guild in self.guilds:
            logger.info(f"  - {guild.name} (ID: {guild.id})")

    def _start_thread_watcher(self):
        from discord.ext import tasks

        @tasks.loop(hours=6)
        async def bump_threads():
            await self._bump_threads_routine()

        self._thread_bump_task = bump_threads
        bump_threads.start()
        logger.info("Thread watcher bump routine started (every 6 hours)")

    async def _bump_threads_routine(self):
        if not self._auto_archive_override:
            logger.info("Thread watcher: auto-archive override disabled, skipping bump")
            return

        watched = await get_all_watched_threads()
        if not watched:
            logger.info("Thread watcher: no threads to bump")
            return

        bumped = 0
        failed = 0
        skipped = 0

        for t in watched:
            try:
                thread = self.get_channel(int(t["thread_id"]))
                if not thread or not isinstance(thread, discord.Thread):
                    skipped += 1
                    continue
                if thread.archived or thread.locked:
                    skipped += 1
                    continue
                if not thread.permissions_for(thread.guild.me).manage_threads:
                    skipped += 1
                    continue

                new_duration = 4320 if thread.auto_archive_duration == 10080 else 10080
                await thread.edit(auto_archive_duration=new_duration)
                bumped += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(f"Thread watcher: failed to bump {t['thread_id']}: {e}")
                failed += 1

        logger.info(f"Thread watcher bump complete: {bumped} bumped, {skipped} skipped, {failed} failed")

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Handle reactions for thread completion."""
        if payload.user_id == self.user.id:
            return

        if str(payload.emoji) != "\u2705":
            return

        channel = self.get_channel(payload.channel_id)
        if not isinstance(channel, discord.Thread):
            return

        conv_id = await get_conversation_id(str(channel.id))
        if conv_id is None:
            return

        try:
            await channel.fetch_message(payload.message_id)
        except Exception:
            return

        logger.info(f"Checkmark reaction in thread '{channel.name}' - archiving")
        try:
            await set_watched(str(channel.id), False)
            await channel.edit(archived=True)
            logger.info(f"Archived thread {channel.id}")
        except Exception as e:
            logger.error(f"Failed to archive thread: {e}")

    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        """Thread watcher: reverse auto-archives, respect manual archives."""
        try:
            conv_id = await get_conversation_id(str(after.id))
            if conv_id is None:
                return

            if not before.archived and after.archived and not after.locked:
                if not self._auto_archive_override:
                    return

                watched = await is_watched(str(after.id))
                if not watched:
                    logger.info(f"Thread watcher: thread {after.id} not watched, leaving archived")
                    return

                logger.info(f"Thread watcher: auto-archive detected on '{after.name}' ({after.id}), unarchiving")
                try:
                    await after.edit(archived=False, auto_archive_duration=10080)
                except Exception as e:
                    logger.error(f"Thread watcher: failed to unarchive {after.id}: {e}")

            elif before.archived and not after.archived:
                watched = await is_watched(str(after.id))
                if not watched:
                    logger.info(f"Thread watcher: thread {after.id} was un-archived, adding to watch list")
                    await set_watched(str(after.id), True)
        except Exception as e:
            logger.error(f"Thread watcher on_thread_update error: {e}")

    def _buffer_key(self, message: discord.Message) -> str:
        """Return a buffer key for grouping messages.

        For threads: use the thread ID so follow-ups are batched.
        For channels: use "channel:{channel_id}:{author_id}" so each user's
        rapid-fire messages are grouped into one thread.
        """
        if isinstance(message.channel, discord.Thread):
            return f"thread:{message.channel.id}"
        return f"channel:{message.channel.id}:{message.author.id}"

    async def _start_buffer_typing(self, key: str, channel):
        """Start a typing indicator loop for the buffer countdown period."""
        if key in self._buffer_typing_tasks:
            return  # already running
        stop = asyncio.Event()

        async def _loop():
            while not stop.is_set():
                try:
                    await channel.trigger_typing()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass

        task = asyncio.create_task(_loop())
        task._stop_event = stop
        self._buffer_typing_tasks[key] = task

    def _stop_buffer_typing(self, key: str):
        """Stop the buffer typing indicator."""
        task = self._buffer_typing_tasks.pop(key, None)
        if task:
            task._stop_event.set()
            task.cancel()

    async def _buffer_countdown(self, key: str, delay: float):
        """Wait for the buffer delay, then flush. Supports pausing via _buffer_paused."""
        remaining = delay
        while remaining > 0:
            if self._buffer_paused.get(key):
                # Paused by user typing — store remaining time and wait
                self._buffer_remaining[key] = remaining
                self._stop_buffer_typing(key)
                while self._buffer_paused.get(key):
                    await asyncio.sleep(0.1)
                remaining = self._buffer_remaining.pop(key, remaining)
                # Resume typing indicator
                msgs = self._buffer.get(key, [])
                if msgs:
                    await self._start_buffer_typing(key, msgs[0].channel)
                continue
            wait = min(remaining, 0.2)
            await asyncio.sleep(wait)
            remaining -= wait
        await self._flush_buffer(key)

    async def _flush_buffer(self, key: str):
        """Flush buffered messages and dispatch to the appropriate handler."""
        messages = self._buffer.pop(key, [])
        self._buffer_tasks.pop(key, None)
        self._buffer_typing.pop(key, None)
        self._buffer_paused.pop(key, None)
        self._buffer_remaining.pop(key, None)
        self._stop_buffer_typing(key)

        if not messages:
            return

        if key.startswith("thread:"):
            if len(messages) == 1:
                await self.handle_thread_message(messages[0])
            else:
                primary = messages[-1]
                earlier_parts = []
                for msg in messages[:-1]:
                    earlier_parts.append(f"[{msg.author.display_name}]: {msg.content}")
                self._bundled_prefixes[primary.id] = "\n".join(earlier_parts)
                await self.handle_thread_message(primary)
        elif key.startswith("channel:"):
            if len(messages) == 1:
                await self.handle_channel_message(messages[0])
            else:
                await self.handle_channel_message_batched(messages)

    async def _add_to_buffer(self, message: discord.Message, buffer_seconds: float):
        """Add a message to the buffer and reset the countdown timer."""
        key = self._buffer_key(message)

        # Pre-save attachments (CDN URLs expire)
        if message.attachments:
            channel_name = message.channel.name if hasattr(message.channel, 'name') else "unknown"
            if isinstance(message.channel, discord.Thread) and message.channel.parent:
                channel_name = message.channel.parent.name
            att_dir = get_attachments_dir(channel_name)
            saved = []
            for att in message.attachments:
                try:
                    att_path = att_dir / f"{uuid.uuid4().hex[:8]}_{att.filename}"
                    await att.save(att_path)
                    saved.append(str(att_path))
                except Exception as e:
                    logger.error(f"Failed to save buffered attachment: {e}")
            if saved:
                self._presaved_attachments[message.id] = saved

        if key not in self._buffer:
            self._buffer[key] = []
        self._buffer[key].append(message)

        # Cancel existing countdown and start a new one
        existing_task = self._buffer_tasks.get(key)
        if existing_task:
            existing_task.cancel()
            try:
                await existing_task
            except (asyncio.CancelledError, Exception):
                pass

        # Ensure typing indicator is running
        await self._start_buffer_typing(key, message.channel)

        # Unpause if user just sent a message (they finished typing)
        self._buffer_paused[key] = False

        # Start new countdown
        task = asyncio.create_task(self._buffer_countdown(key, buffer_seconds))
        self._buffer_tasks[key] = task

    async def on_typing(self, channel, user, when):
        """Pause the buffer countdown while the user is typing."""
        if user.bot:
            return

        # Determine which buffer keys this typing event could affect
        possible_keys = []
        if isinstance(channel, discord.Thread):
            possible_keys.append(f"thread:{channel.id}")
        elif isinstance(channel, discord.TextChannel):
            possible_keys.append(f"channel:{channel.id}:{user.id}")

        for key in possible_keys:
            if key not in self._buffer or key not in self._buffer_tasks:
                continue
            # Mark as paused and record typing timestamp
            self._buffer_typing[key] = asyncio.get_event_loop().time()
            self._buffer_paused[key] = True
            # Schedule unpause after typing timeout
            asyncio.ensure_future(self._typing_timeout(key))

    async def _typing_timeout(self, key: str):
        """After TYPING_TIMEOUT seconds with no new on_typing event, unpause the buffer."""
        await asyncio.sleep(self._TYPING_TIMEOUT)
        last_typing = self._buffer_typing.get(key, 0)
        elapsed = asyncio.get_event_loop().time() - last_typing
        if elapsed >= self._TYPING_TIMEOUT - 0.1 and self._buffer_paused.get(key):
            logger.info(f"Buffer typing timeout for {key}, unpausing")
            self._buffer_paused[key] = False

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return

        allowed_users = self.config.get("allowed_users", [])
        if allowed_users and str(message.author.id) not in allowed_users:
            return

        # For thread messages with an inflight request:
        # - Queue mode: bypass buffer, queue immediately via handle_thread_message
        # - Interrupt mode: let the buffer run (so rapid-fire messages batch),
        #   then handle_thread_message will cancel the inflight turn
        if isinstance(message.channel, discord.Thread):
            thread_id = str(message.channel.id)
            if self._inflight.get(thread_id):
                parent_channel_id = str(message.channel.parent.id) if message.channel.parent else None
                message_mode = "queue"
                if parent_channel_id:
                    ch_config = await get_channel_config(parent_channel_id)
                    if ch_config:
                        message_mode = ch_config.get("message_mode", "queue") or "queue"
                if message_mode == "interrupt":
                    pass  # fall through to buffer/immediate handling below
                else:
                    await self.handle_thread_message(message)
                    return

        # Determine buffer delay
        if isinstance(message.channel, discord.Thread) and message.channel.parent:
            channel_id = str(message.channel.parent.id)
        elif isinstance(message.channel, discord.TextChannel):
            channel_id = str(message.channel.id)
        else:
            channel_id = None

        buffer_seconds = 0.0
        if channel_id:
            buffer_seconds = await self.get_buffer_seconds(channel_id)

        if buffer_seconds > 0:
            await self._add_to_buffer(message, buffer_seconds)
            return

        # No buffer — process immediately (original behavior)
        if isinstance(message.channel, discord.Thread):
            await self.handle_thread_message(message)
            return

        if isinstance(message.channel, discord.TextChannel):
            await self.handle_channel_message(message)
            return

    async def set_status(self, thread: discord.Thread, status: str):
        """Update thread title with status emoji and save to DB."""
        new_name = set_thread_status_prefix(thread.name, status)
        try:
            await asyncio.wait_for(thread.edit(name=new_name), timeout=10.0)
            await update_thread_name(str(thread.id), new_name)
            await update_thread_status(str(thread.id), status)
        except (asyncio.TimeoutError, discord.HTTPException) as e:
            is_rate_limit = isinstance(e, discord.HTTPException) and e.status == 429
            reason = "Rate limited" if is_rate_limit else "Timeout"
            logger.warning(f"{reason} setting status '{status}' on thread {thread.id} - will retry in background")
            await update_thread_status(str(thread.id), status)
            asyncio.create_task(self._retry_rename(thread, new_name, status))
        except Exception as e:
            logger.error(f"Failed to update status: {e}")

    async def _retry_rename(self, thread: discord.Thread, new_name: str, status: str):
        """Retry a thread rename after rate limit clears."""
        await asyncio.sleep(60)
        try:
            current = await thread.guild.fetch_channel(thread.id)
            expected_name = set_thread_status_prefix(current.name, status)
            await asyncio.wait_for(current.edit(name=expected_name), timeout=30.0)
            await update_thread_name(str(thread.id), expected_name)
            logger.info(f"Retry rename succeeded for thread {thread.id}: {expected_name}")
        except Exception as e:
            logger.warning(f"Retry rename failed for thread {thread.id}: {e}")

    async def handle_channel_message(self, message: discord.Message):
        logger.info(f"New message in #{message.channel.name} from {message.author}")

        # Extract per-message model and persona overrides (supports either order)
        model_override, persona_override, user_text = self.extract_overrides(message.content)
        if model_override:
            logger.info(f"Model override detected: {model_override}")
        if persona_override:
            logger.info(f"Persona override detected: {persona_override}")

        # Resolve channel defaults (model, persona, backend, hermes params)
        channel_model, channel_persona, channel_backend, channel_hermes_params = await self.resolve_channel_defaults(str(message.channel.id))

        # Priority: message prefix > channel default > global default
        effective_model = model_override or channel_model  # global default handled by ZoClient
        config = load_config()
        effective_persona = persona_override or channel_persona or config.get("default_persona")

        attachment_paths = []
        if message.attachments:
            att_dir = get_attachments_dir(message.channel.name)
            for att in message.attachments:
                try:
                    att_path = att_dir / f"{uuid.uuid4().hex[:8]}_{att.filename}"
                    await att.save(att_path)
                    attachment_paths.append(str(att_path))
                except Exception as e:
                    logger.error(f"Failed to save attachment: {e}")

        # Create thread BEFORE calling Zo so thinking previews have somewhere to go
        simple_title = self.zo.generate_thread_title_simple(user_text)
        thread = await message.create_thread(
            name=simple_title[:100],
        )
        thread_id = str(thread.id)

        # Save mapping with empty conv_id — will be updated once streaming starts
        await save_mapping(
            thread_id=thread_id,
            conversation_id="",
            channel_id=str(message.channel.id),
            guild_id=str(message.guild.id),
            thread_name=simple_title[:100]
        )

        on_thinking = self.make_on_thinking(thread)

        async def on_conv_id(cid: str):
            await update_conversation_id(thread_id, cid)
            if thread_id in self._inflight:
                self._inflight[thread_id]["conv_id"] = cid

        task = asyncio.current_task()
        self._inflight[thread_id] = {"conv_id": "", "task": task}

        stop_event = asyncio.Event()
        typing_task = asyncio.create_task(self.typing_loop(thread, stop_event))
        try:
            context, file_paths = await self.build_channel_context(message.channel, backend=channel_backend)
            if attachment_paths:
                file_paths.extend(attachment_paths)

            result = await self.zo.ask_stream(
                user_text,
                context=context or None,
                file_paths=file_paths or None,
                on_thinking=on_thinking,
                on_conv_id=on_conv_id,
                model_name=effective_model,
                persona_id=effective_persona,
                backend=channel_backend,
                **channel_hermes_params,
            )
            response, conv_id = result.output, result.conv_id

            # Apply queued rename from Zo if available
            # (Rename may have been queued mid-stream before DB mapping existed)
            rename = self.queued_renames.pop(conv_id, None)
            if rename:
                try:
                    await asyncio.wait_for(thread.edit(name=rename[:100]), timeout=10.0)
                    await update_thread_name(thread_id, rename[:100])
                except (asyncio.TimeoutError, discord.HTTPException) as e:
                    logger.warning(f"Failed to apply queued rename: {e}")
                    await update_thread_name(thread_id, rename[:100])

            if not response or not response.strip():
                logger.warning(f"Empty response for conv {conv_id} (interrupted={result.interrupted}, events={result.received_events}, error={result.error_message!r})")
                if result.error_message:
                    response = f"\u26a0\ufe0f **Request failed.**\n\n`{result.error_message[:200]}`\n\nSend another message to try again."
                else:
                    response, conv_id = await self._retry_empty_response(
                        thread_id, conv_id, thread, on_thinking, on_conv_id,
                        backend=channel_backend,
                    )

            chunks = self.zo.chunk_response(response)
            chunks = [c for c in chunks if c.strip()]
            if not chunks:
                # All retries exhausted — show fallback
                logger.error(f"All retries exhausted for conv {conv_id}")
                chunks = [f"\u26a0\ufe0f **Zo didn't respond after multiple retries.**\n\nConversation: `{conv_id}`\n\nSend another message to try again."]
            for chunk in chunks:
                await send_suppressed(thread, content=chunk)
            logger.info(f"Sent response in thread {thread.id}, conv_id {conv_id}")

        except asyncio.CancelledError:
            logger.info(f"Channel message request cancelled (interrupted) for thread {thread_id}")
        except Exception as e:
            logger.error(f"Error handling message in #{message.channel.name} from {message.author}: {e}", exc_info=True)
            try:
                await self.set_status(thread, "error")
            except Exception:
                pass
        finally:
            self._inflight.pop(thread_id, None)
            stop_event.set()
            try:
                await typing_task
            except Exception:
                pass

            # Drain queued messages (user may have sent follow-ups in the
            # thread while this channel-message turn was still running).
            await self._drain_queue(thread_id)

    async def handle_channel_message_batched(self, messages: list[discord.Message]):
        """Handle multiple buffered channel messages as a single thread.

        Creates a thread on the LAST message and combines all message texts.
        Model/persona overrides are taken from the first message that has them.
        """
        last_msg = messages[-1]
        channel = last_msg.channel
        logger.info(f"Processing {len(messages)} buffered messages in #{channel.name} from {last_msg.author}")

        # Combine text and collect overrides/attachments
        combined_parts = []
        effective_model = None
        effective_persona = None
        all_attachment_paths = []

        for msg in messages:
            model_override, persona_override, text = self.extract_overrides(msg.content)
            if model_override and not effective_model:
                effective_model = model_override
            if persona_override and not effective_persona:
                effective_persona = persona_override
            combined_parts.append(text)

            # Use pre-saved attachments from buffer or save now
            att_paths = self._presaved_attachments.pop(msg.id, None) or []
            if not att_paths and msg.attachments:
                att_dir = get_attachments_dir(channel.name)
                for att in msg.attachments:
                    try:
                        att_path = att_dir / f"{uuid.uuid4().hex[:8]}_{att.filename}"
                        await att.save(att_path)
                        att_paths.append(str(att_path))
                    except Exception as e:
                        logger.error(f"Failed to save attachment: {e}")
            all_attachment_paths.extend(att_paths)

        user_text = "\n".join(combined_parts)

        # Resolve channel defaults
        channel_model, channel_persona, channel_backend, channel_hermes_params = await self.resolve_channel_defaults(str(channel.id))
        effective_model = effective_model or channel_model
        config = load_config()
        effective_persona = effective_persona or channel_persona or config.get("default_persona")

        # Create thread on the last message
        simple_title = self.zo.generate_thread_title_simple(user_text)
        thread = await last_msg.create_thread(name=simple_title[:100])
        thread_id = str(thread.id)

        await save_mapping(
            thread_id=thread_id,
            conversation_id="",
            channel_id=str(channel.id),
            guild_id=str(last_msg.guild.id),
            thread_name=simple_title[:100]
        )

        on_thinking = self.make_on_thinking(thread)

        async def on_conv_id(cid: str):
            await update_conversation_id(thread_id, cid)
            if thread_id in self._inflight:
                self._inflight[thread_id]["conv_id"] = cid

        task = asyncio.current_task()
        self._inflight[thread_id] = {"conv_id": "", "task": task}

        stop_event = asyncio.Event()
        typing_task = asyncio.create_task(self.typing_loop(thread, stop_event))
        try:
            context, file_paths = await self.build_channel_context(channel, backend=channel_backend)
            file_paths.extend(all_attachment_paths)

            result = await self.zo.ask_stream(
                user_text,
                context=context or None,
                file_paths=file_paths or None,
                on_thinking=on_thinking,
                on_conv_id=on_conv_id,
                model_name=effective_model,
                persona_id=effective_persona,
                backend=channel_backend,
                **channel_hermes_params,
            )
            response, conv_id = result.output, result.conv_id

            rename = self.queued_renames.pop(conv_id, None)
            if rename:
                try:
                    await asyncio.wait_for(thread.edit(name=rename[:100]), timeout=10.0)
                    await update_thread_name(thread_id, rename[:100])
                except (asyncio.TimeoutError, discord.HTTPException) as e:
                    logger.warning(f"Failed to apply queued rename: {e}")
                    await update_thread_name(thread_id, rename[:100])

            if not response or not response.strip():
                logger.warning(f"Empty response for conv {conv_id} (interrupted={result.interrupted}, events={result.received_events}, error={result.error_message!r})")
                if result.error_message:
                    response = f"\u26a0\ufe0f **Request failed.**\n\n`{result.error_message[:200]}`\n\nSend another message to try again."
                else:
                    response, conv_id = await self._retry_empty_response(
                        thread_id, conv_id, thread, on_thinking, on_conv_id,
                        backend=channel_backend,
                    )

            chunks = self.zo.chunk_response(response)
            chunks = [c for c in chunks if c.strip()]
            if not chunks:
                logger.error(f"All retries exhausted for conv {conv_id}")
                chunks = [f"\u26a0\ufe0f **Zo didn't respond after multiple retries.**\n\nConversation: `{conv_id}`\n\nSend another message to try again."]
            for chunk in chunks:
                await send_suppressed(thread, content=chunk)
            logger.info(f"Sent response in thread {thread.id}, conv_id {conv_id}")

        except asyncio.CancelledError:
            logger.info(f"Batched channel message request cancelled for thread {thread_id}")
        except Exception as e:
            logger.error(f"Error handling batched messages in #{channel.name}: {e}", exc_info=True)
            try:
                await self.set_status(thread, "error")
            except Exception:
                pass
        finally:
            self._inflight.pop(thread_id, None)
            stop_event.set()
            try:
                await typing_task
            except Exception:
                pass
            await self._drain_queue(thread_id)

    def _get_channel_name_for_thread(self, thread: discord.Thread) -> str:
        """Get the parent channel name for attachment saving."""
        if thread.parent:
            return thread.parent.name
        return "unknown"

    async def handle_thread_message(self, message: discord.Message):
        thread = message.channel
        thread_id = str(thread.id)

        conv_id = await get_conversation_id(thread_id)
        if conv_id is None:
            return

        logger.info(f"Message in thread '{thread.name}' from {message.author}")

        # If there's an in-flight request, behavior depends on message_mode:
        # - queue (default): collect messages and drain after current turn
        # - interrupt: cancel current turn, then process this message immediately
        inflight = self._inflight.get(thread_id)
        if inflight:
            parent_channel_id = str(thread.parent.id) if thread.parent else None
            message_mode = "queue"
            if parent_channel_id:
                ch_config = await get_channel_config(parent_channel_id)
                if ch_config:
                    message_mode = ch_config.get("message_mode", "queue") or "queue"

            if message_mode == "interrupt":
                logger.info(f"Interrupt mode: cancelling inflight for thread {thread_id}")
                session_id = inflight.get("conv_id")
                inflight_task = inflight.get("task")

                # Cancel the Hermes session
                if session_id:
                    try:
                        async with aiohttp.ClientSession() as http_session:
                            async with http_session.post(
                                "http://127.0.0.1:8788/cancel",
                                json={"session_id": session_id},
                                timeout=aiohttp.ClientTimeout(total=10),
                            ) as resp:
                                cancel_status = resp.status
                                logger.info(f"Cancel response for session {session_id}: {cancel_status}")
                    except Exception as e:
                        logger.error(f"Failed to cancel session {session_id}: {e}")

                # Wait for the inflight task to finish (it should exit after cancel)
                if inflight_task and not inflight_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(inflight_task), timeout=15)
                    except (asyncio.TimeoutError, Exception) as e:
                        logger.warning(f"Inflight task didn't finish cleanly after cancel: {e}")

                # Clear inflight state so this message processes as a fresh turn
                self._inflight.pop(thread_id, None)
                # Discard any queued messages — the interrupt supersedes them
                self._message_queues.pop(thread_id, None)
                await send_suppressed(thread, content=f"*Interrupting — processing your new message.*")
                # Fall through to process this message normally

            else:
                # Queue mode (default behavior)
                logger.info(f"Queue mode: queuing message from {message.author} in thread {thread_id}")
                if message.attachments:
                    saved_paths = []
                    channel_name = self._get_channel_name_for_thread(thread)
                    att_dir = get_attachments_dir(channel_name)
                    for att in message.attachments:
                        try:
                            att_path = att_dir / f"{uuid.uuid4().hex[:8]}_{att.filename}"
                            await att.save(att_path)
                            saved_paths.append(str(att_path))
                        except Exception as e:
                            logger.error(f"Failed to save queued attachment: {e}")
                    if saved_paths:
                        self._presaved_attachments[message.id] = saved_paths
                if thread_id not in self._message_queues:
                    self._message_queues[thread_id] = asyncio.Queue()
                await self._message_queues[thread_id].put(message)
                await send_suppressed(thread, content=f"*Queued — will process after current turn finishes.*")
                return

        # Extract per-message model and persona overrides (supports either order)
        model_override, persona_override, user_text = self.extract_overrides(message.content)
        if model_override:
            logger.info(f"Model override detected in thread: {model_override}")
        if persona_override:
            logger.info(f"Persona override detected in thread: {persona_override}")

        # Apply channel defaults (model, persona, backend) from parent channel.
        # Backend must ALWAYS be resolved (even for existing conversations) so that
        # threads with Hermes session IDs route to zo-hermes, not Zo.
        parent_channel_id = str(thread.parent.id) if thread.parent else None
        effective_model = model_override
        effective_persona = persona_override
        channel_backend = None
        channel_hermes_params = {}
        if parent_channel_id:
            channel_model, channel_persona, channel_backend, channel_hermes_params = await self.resolve_channel_defaults(parent_channel_id)
            if not conv_id:
                if not effective_model:
                    effective_model = channel_model
                if not effective_persona:
                    effective_persona = channel_persona
        if not effective_persona:
            config = load_config()
            effective_persona = config.get("default_persona")

        is_first_reply = conv_id == ""
        context, file_paths = await self.build_thread_context(thread, include_source=is_first_reply, conv_id=conv_id, backend=channel_backend)

        if conv_id == "":
            notification_texts = []
            messages = []
            async for msg in thread.history(limit=10, oldest_first=True):
                messages.append(msg)

            for msg in messages[1:] if len(messages) > 1 else messages:
                if msg.author.bot:
                    notification_texts.append(msg.content)

            if notification_texts:
                notification_context = "\n\n".join(notification_texts)
                context = (context + "\n\n" if context else "") + "## Notification content:\n" + notification_context

        # Use pre-saved attachments if this message was queued (CDN URLs may
        # have expired), otherwise download them now.
        attachment_paths = self._presaved_attachments.pop(message.id, None) or []
        if not attachment_paths and message.attachments:
            channel_name = self._get_channel_name_for_thread(thread)
            att_dir = get_attachments_dir(channel_name)
            for att in message.attachments:
                try:
                    att_path = att_dir / f"{uuid.uuid4().hex[:8]}_{att.filename}"
                    await att.save(att_path)
                    attachment_paths.append(str(att_path))
                except Exception as e:
                    logger.error(f"Failed to save attachment: {e}")

        if attachment_paths:
            file_paths.extend(attachment_paths)

        reply_context = ""
        if message.reference and message.reference.message_id:
            try:
                ref_msg = await thread.fetch_message(message.reference.message_id)
                ref_author = "Zo" if ref_msg.author.bot else ref_msg.author.display_name
                ref_text = ref_msg.content[:500]
                if len(ref_msg.content) > 500:
                    ref_text += "..."
                reply_context = f"[Replying to {ref_author}: \"{ref_text}\"]\n\n"
            except Exception as e:
                logger.warning(f"Failed to resolve reply reference: {e}")

        user_input = reply_context + user_text

        # If this message was bundled with earlier queued messages, prepend them
        bundled_prefix = self._bundled_prefixes.pop(message.id, None)
        if bundled_prefix:
            user_input = f"{bundled_prefix}\n[{message.author.display_name}]: {user_input}"

        on_thinking = self.make_on_thinking(thread)

        async def on_conv_id(cid: str):
            if cid != conv_id:
                await update_conversation_id(thread_id, cid)
            if thread_id in self._inflight:
                self._inflight[thread_id]["conv_id"] = cid

        task = asyncio.current_task()
        self._inflight[thread_id] = {"conv_id": conv_id or "", "task": task}

        # Cache last user message for /retry
        self._last_user_messages[thread_id] = user_input

        stop_event = asyncio.Event()
        typing_task = asyncio.create_task(self.typing_loop(thread, stop_event))
        try:
            result = await self.zo.ask_stream(
                user_input,
                conversation_id=conv_id if conv_id else None,
                context=context or None,
                file_paths=file_paths or None,
                on_thinking=on_thinking,
                on_conv_id=on_conv_id,
                model_name=effective_model,
                persona_id=effective_persona,
                backend=channel_backend,
                **channel_hermes_params,
            )
            response, new_conv_id = result.output, result.conv_id

            if new_conv_id != conv_id:
                await update_conversation_id(thread_id, new_conv_id)
                if not conv_id:
                    logger.info(f"Started new conversation {new_conv_id} for notification thread")
                else:
                    logger.info(f"Conversation ID changed from {conv_id} to {new_conv_id}")

            await update_activity(thread_id)

            if not response or not response.strip():
                logger.warning(f"Empty response for thread {thread.id} (interrupted={result.interrupted}, events={result.received_events}, error={result.error_message!r})")
                if result.error_message:
                    response = f"\u26a0\ufe0f **Request failed.**\n\n`{result.error_message[:200]}`\n\nSend another message to try again."
                else:
                    response, new_conv_id = await self._retry_empty_response(
                        thread_id, new_conv_id or conv_id, thread, on_thinking, on_conv_id,
                        backend=channel_backend,
                    )

            chunks = self.zo.chunk_response(response)
            chunks = [c for c in chunks if c.strip()]
            if not chunks:
                # All retries exhausted — show fallback
                logger.error(f"All retries exhausted for thread {thread.id}")
                chunks = [f"\u26a0\ufe0f **Zo didn't respond after multiple retries.**\n\nConversation: `{new_conv_id or conv_id}`\n\nSend another message to try again."]
            for i, chunk in enumerate(chunks):
                kwargs = {"content": chunk}
                if i == 0:
                    ref = discord.MessageReference(
                        message_id=message.id,
                        channel_id=message.channel.id,
                        fail_if_not_exists=False,
                    )
                    kwargs["reference"] = ref
                    kwargs["mention_author"] = False
                    logger.info(f"Replying to message {message.id} in thread {thread.id}")
                await send_suppressed(thread, **kwargs)

        except asyncio.CancelledError:
            logger.info(f"Thread {thread_id} request was cancelled")
        except Exception as e:
            logger.error(f"Error in thread: {e}", exc_info=True)
            # Try to recover by sending a continuation message.
            # The Zo agent may have responded, but the Discord connection
            # dropped before we could relay it (e.g. "Connection closed").
            recovery_conv_id = (self._inflight.get(thread_id, {}).get("conv_id") or conv_id)
            recovered = False
            if recovery_conv_id:
                try:
                    logger.info(f"Attempting response recovery for conv {recovery_conv_id}")
                    # Wait for Discord websocket to reconnect before trying to post
                    for wait_secs in [5, 10, 15, 15, 15]:
                        if not self.is_closed() and self.ws and self.ws.open:
                            break
                        logger.info(f"Waiting {wait_secs}s for Discord reconnect...")
                        await asyncio.sleep(wait_secs)
                    recovery_input = (
                        "Your previous response was empty. If you were interrupted, "
                        "please continue where you left off. If you finished the work, "
                        "please respond with your results."
                    )
                    recovery_result = await self.zo.ask_stream(
                        recovery_input,
                        conversation_id=recovery_conv_id,
                        backend=channel_backend,
                    )
                    if recovery_result.conv_id != recovery_conv_id:
                        await update_conversation_id(thread_id, recovery_result.conv_id)
                        recovery_conv_id = recovery_result.conv_id
                    if recovery_result.output and recovery_result.output.strip():
                        logger.info(f"Recovered {len(recovery_result.output)} chars from conv {recovery_conv_id}")
                        chunks = self.zo.chunk_response(recovery_result.output)
                        chunks = [c for c in chunks if c.strip()]
                        for i, chunk in enumerate(chunks):
                            kwargs = {"content": chunk}
                            if i == 0:
                                ref = discord.MessageReference(
                                    message_id=message.id,
                                    channel_id=message.channel.id,
                                    fail_if_not_exists=False,
                                )
                                kwargs["reference"] = ref
                                kwargs["mention_author"] = False
                            await send_suppressed(thread, **kwargs)
                        recovered = True
                except Exception as recovery_err:
                    logger.error(f"Recovery failed for conv {recovery_conv_id} in thread {thread_id}: {recovery_err}")
            if not recovered:
                error_msg = str(e)
                conv_label = f"\nConversation: `{recovery_conv_id}`" if recovery_conv_id else ""
                full_error = f"\u274c **Error:** {error_msg}{conv_label}"
                try:
                    for chunk in self.zo.chunk_response(full_error):
                        await send_suppressed(thread, content=chunk)
                except Exception:
                    pass  # Discord may still be disconnected
                await self.set_status(thread, "error")
        finally:
            self._inflight.pop(thread_id, None)
            stop_event.set()
            try:
                await typing_task
            except Exception:
                pass

            await self._drain_queue(thread_id)

    async def retry_in_thread(self, thread: discord.Thread):
        """Re-send the last cached user message through the normal streaming pipeline."""
        thread_id = str(thread.id)
        user_input = self._last_user_messages.get(thread_id)
        if not user_input:
            return

        conv_id = await get_conversation_id(thread_id)
        if not conv_id:
            return

        parent_channel_id = str(thread.parent.id) if thread.parent else None
        channel_backend = None
        channel_hermes_params = {}
        effective_model = None
        effective_persona = None
        if parent_channel_id:
            effective_model, effective_persona, channel_backend, channel_hermes_params = await self.resolve_channel_defaults(parent_channel_id)
        if not effective_persona:
            config = load_config()
            effective_persona = config.get("default_persona")

        on_thinking = self.make_on_thinking(thread)

        async def on_conv_id(cid: str):
            if cid != conv_id:
                await update_conversation_id(thread_id, cid)
            if thread_id in self._inflight:
                self._inflight[thread_id]["conv_id"] = cid

        task = asyncio.current_task()
        self._inflight[thread_id] = {"conv_id": conv_id, "task": task}

        stop_event = asyncio.Event()
        typing_task = asyncio.create_task(self.typing_loop(thread, stop_event))
        try:
            result = await self.zo.ask_stream(
                user_input,
                conversation_id=conv_id,
                context=None,
                file_paths=None,
                on_thinking=on_thinking,
                on_conv_id=on_conv_id,
                model_name=effective_model,
                persona_id=effective_persona,
                backend=channel_backend,
                **channel_hermes_params,
            )
            response = result.output

            if result.conv_id and result.conv_id != conv_id:
                await update_conversation_id(thread_id, result.conv_id)

            await update_activity(thread_id)

            if not response or not response.strip():
                if result.error_message:
                    response = f"\u26a0\ufe0f **Retry failed.**\n\n`{result.error_message[:200]}`"

            if response and response.strip():
                chunks = self.zo.chunk_response(response)
                for chunk in chunks:
                    if chunk.strip():
                        await send_suppressed(thread, content=chunk)
        except asyncio.CancelledError:
            logger.info(f"Retry cancelled for thread {thread_id}")
        except Exception as e:
            logger.error(f"Retry error in thread {thread_id}: {e}", exc_info=True)
            try:
                await send_suppressed(thread, content=f"\u274c **Retry failed:** {e}")
            except Exception:
                pass
        finally:
            self._inflight.pop(thread_id, None)
            stop_event.set()
            try:
                await typing_task
            except Exception:
                pass

    def _collect_queued_text(self, thread_id: str) -> list[str]:
        """Consume all queued messages for a thread, returning their text.

        Returns a list of "[Author]: content" strings. The queue is emptied
        so these messages won't be double-processed by _drain_queue.
        """
        queue = self._message_queues.get(thread_id)
        if not queue or queue.empty():
            return []

        parts = []
        while not queue.empty():
            try:
                msg = queue.get_nowait()
                parts.append(f"[{msg.author.display_name}]: {msg.content}")
            except asyncio.QueueEmpty:
                break
        self._message_queues.pop(thread_id, None)
        return parts

    async def _retry_empty_response(
        self,
        thread_id: str,
        conv_id: str,
        thread: discord.Thread,
        on_thinking,
        on_conv_id,
        backend: str = None,
    ) -> tuple[str, str]:
        """Recover from an empty or interrupted response.

        Sends a continuation message (bundling any queued user messages)
        via streaming to nudge the agent into producing output.
        """
        queued_parts = self._collect_queued_text(thread_id)
        if queued_parts:
            retry_input = " ".join(queued_parts)
            logger.info(f"Sending continue with {len(queued_parts)} queued message(s) for conv {conv_id}")
        else:
            retry_input = (
                "Your previous response was empty. If you were interrupted, "
                "please continue where you left off. If you finished the work, "
                "please respond with your results."
            )

        retry_delays = [30, 60, 120]
        for attempt, delay in enumerate(retry_delays, 1):
            logger.warning(f"Empty response (conv {conv_id}), continue attempt {attempt}/{len(retry_delays)} in {delay}s")
            await asyncio.sleep(delay)

            try:
                result = await self.zo.ask_stream(
                    retry_input,
                    conversation_id=conv_id,
                    on_thinking=on_thinking,
                    on_conv_id=on_conv_id,
                    backend=backend,
                )
                if result.conv_id != conv_id:
                    await update_conversation_id(thread_id, result.conv_id)
                    conv_id = result.conv_id

                if result.output and result.output.strip():
                    logger.info(f"Continue attempt {attempt} succeeded for conv {conv_id}")
                    return result.output, conv_id
            except Exception as e:
                logger.error(f"Continue attempt {attempt} failed for conv {conv_id}: {e}")

        logger.error(f"All retries exhausted for conv {conv_id}")
        return "", conv_id

    async def _drain_queue(self, thread_id: str):
        """Drain queued messages for a thread, bundling them into one turn.

        The agent sees all queued messages and decides how to interpret them
        (e.g. "do X" then "no actually Y" → agent does Y;
        "do X" then "also Y" → agent does X+Y).
        """
        try:
            queue = self._message_queues.get(thread_id)
            if not queue or queue.empty():
                return

            queued_msgs = []
            while not queue.empty():
                queued_msgs.append(await queue.get())
            self._message_queues.pop(thread_id, None)

            if len(queued_msgs) == 1:
                logger.info(f"Processing 1 queued message from {queued_msgs[0].author} in thread {thread_id}")
                await self.handle_thread_message(queued_msgs[0])
            else:
                logger.info(f"Processing {len(queued_msgs)} bundled queued messages in thread {thread_id}")
                # Use the last message as the "primary" (for attachments, reply context, etc.)
                # and prepend the earlier messages as context
                primary = queued_msgs[-1]
                earlier_parts = []
                for msg in queued_msgs[:-1]:
                    earlier_parts.append(f"[{msg.author.display_name}]: {msg.content}")
                bundle_prefix = "[Messages sent while you were working:]\n" + "\n".join(earlier_parts)
                self._bundled_prefixes[primary.id] = bundle_prefix
                await self.handle_thread_message(primary)
        except Exception as e:
            logger.error(f"Error draining message queue for thread {thread_id}: {e}", exc_info=True)

    async def build_channel_context(self, channel: discord.TextChannel, include_source: bool = True, thread: discord.Thread = None, conv_id: str = "", backend: str = "") -> tuple[str, list[str]]:
        """Build context string and file paths for the /zo/ask endpoint.

        Returns:
            (context, file_paths) — context is appended after the user message,
            file_paths lists paths Zo should read.
        """
        sections = []
        file_paths = []
        ch_config = await get_channel_config(str(channel.id))

        channel_dir = get_channel_dir(channel.name)
        channel_dir_str = str(channel_dir)
        channel_mention = "<#" + str(channel.id) + ">"

        # Hermes agents have CONVERSATION_ID set as an env var by zo-hermes/server.py,
        # so the CLI auto-resolves without --conv-id. Only Zo agents need the flag.
        is_hermes = backend == "hermes"
        conv_flag = f" --conv-id {conv_id}" if conv_id and not is_hermes else ""
        if conv_id or is_hermes:
            conv_hint = ""
        else:
            conv_hint = " (find your conversation ID in the <conversation_workspace> section of your system prompt)"

        if include_source:
            # === FIRST MESSAGE: full context ===

            sections.append(
                "## Message Source\n"
                "This message is from Discord (channel: " + channel_mention + "). "
                "Reply normally \u2014 your response will appear in the thread. "
                'Before replying, rename the thread with `zo-discord' + conv_flag + ' rename "Descriptive Title"`'
                + conv_hint + " "
                "(3-6 words, specific and scannable)."
            )

            if ch_config:
                if ch_config.get("instructions"):
                    sections.append("## Channel Instructions\n" + ch_config["instructions"])
                if ch_config.get("memory_paths"):
                    for mp in ch_config["memory_paths"]:
                        file_paths.append("/home/workspace/" + mp)

            if channel.topic and not (ch_config and ch_config.get("instructions")):
                sections.append("## Channel Topic\n" + channel.topic)

            try:
                pins = await channel.pins()
                if pins:
                    pin_texts = []
                    for pin in reversed(pins):
                        content = pin.content[:500] if pin.content else "[attachment/embed]"
                        pin_texts.append("- **" + pin.author.display_name + "**: " + content)
                    sections.append("## Pinned Context\n" + "\n".join(pin_texts))
            except discord.Forbidden:
                pass

            sections.append(
                "## Discord Tools\n"
                "For more tools to interact with Discord, like spawning new threads and "
                "presenting the user with button forms, see the zo-discord skill.\n\n"
                "When you generate files (images, documents, etc.), send them into the thread with:\n"
                '`zo-discord' + conv_flag + ' files /path/to/file.png "Optional caption"`\n\n'
                "Run `zo-discord help` for the full command list."
            )

        else:
            # === FOLLOW-UP MESSAGE: compact context ===
            if thread:
                clean_name = strip_status_prefix(thread.name)
                sections.append(
                    "## Message Source\n"
                    "This message is from Discord (channel: " + channel_mention + "; "
                    'thread: "' + clean_name + '"). '
                    "Reply normally. "
                    'If the topic has shifted from the thread name, rename first: `zo-discord' + conv_flag + ' rename "New Title"`\n'
                    'Send generated files: `zo-discord' + conv_flag + ' files /path/to/file "caption"` | '
                    "Full CLI help: `zo-discord help`"
                )

        return "\n\n".join(sections), file_paths

    async def build_thread_context(self, thread: discord.Thread, include_source: bool = False, conv_id: str = "", backend: str = "") -> tuple[str, list[str]]:
        """Build context string and file paths for a thread message.

        Returns:
            (context, file_paths) — merged with parent channel context.
        """
        sections = []
        file_paths = []

        if thread.parent:
            parent_ctx, parent_paths = await self.build_channel_context(
                thread.parent, include_source=include_source, thread=thread, conv_id=conv_id, backend=backend
            )
            if parent_ctx:
                sections.append(parent_ctx)
            file_paths.extend(parent_paths)

        try:
            pins = await thread.pins()
            if pins:
                pin_texts = []
                for pin in reversed(pins):
                    content = pin.content[:500] if pin.content else "[attachment/embed]"
                    pin_texts.append(f"- **{pin.author.display_name}**: {content}")
                sections.append("## Thread Pins\n" + "\n".join(pin_texts))
        except discord.Forbidden:
            pass

        return "\n\n".join(sections), file_paths

    def make_on_thinking(self, channel):
        """Create an on_thinking callback that respects the thinking mode setting."""
        async def on_thinking(text: str):
            if self._thinking_mode == "streaming":
                await send_suppressed(channel, content=f"*{text}*")
        return on_thinking

    async def typing_loop(self, channel, stop_event: asyncio.Event):
        while not stop_event.is_set():
            try:
                await channel.trigger_typing()
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    # ─── Helpers ───────────────────────────────────────────────────────────

    async def resolve_thread(self, thread_id: str) -> discord.Thread | None:
        """Get a thread by ID, falling back to API if not in cache."""
        thread = self.get_channel(int(thread_id))
        if thread is None:
            try:
                thread = await self.fetch_channel(int(thread_id))
            except (discord.NotFound, discord.Forbidden):
                return None
        return thread if isinstance(thread, discord.Thread) else None

    # ─── HTTP Server ──────────────────────────────────────────────────────

    async def start_http_server(self):
        self.http_app = web.Application()
        self.http_app.router.add_post("/notify", self.handle_notify)
        self.http_app.router.add_get("/threads", self.handle_list_threads)
        self.http_app.router.add_post("/threads/{thread_id}/rename", self.handle_rename_thread)
        self.http_app.router.add_get("/health", self.handle_health)
        # New endpoints
        self.http_app.router.add_post("/buttons", self.handle_buttons)
        self.http_app.router.add_post("/files", self.handle_files)
        self.http_app.router.add_post("/embeds", self.handle_embeds)
        self.http_app.router.add_post("/react", self.handle_react)
        self.http_app.router.add_post("/messages/edit", self.handle_edit_message)
        self.http_app.router.add_delete("/messages", self.handle_delete_message)
        self.http_app.router.add_post("/messages/send", self.handle_send_message)
        self.http_app.router.add_post("/conversations/{conv_id}/buttons", self.handle_conversation_buttons)
        self.http_app.router.add_get("/channels/{channel_id}/config", self.handle_get_channel_config)
        self.http_app.router.add_post("/channels/{channel_id}/config", self.handle_set_channel_config)
        self.http_app.router.add_delete("/channels/{channel_id}/config", self.handle_delete_channel_config)
        self.http_app.router.add_post("/threads/{thread_id}/status", self.handle_set_status)
        self.http_app.router.add_post("/conversations/{conv_id}/action", self.handle_conversation_action)
        self.http_app.router.add_post("/conversations/{conv_id}/files", self.handle_conversation_files)
        self.http_app.router.add_post("/conversations/{conv_id}/new-thread", self.handle_new_thread)
        self.http_app.router.add_post("/config", self.handle_config)

        port = self.config.get("notification_port", 8787)

        self.http_runner = web.AppRunner(self.http_app)
        await self.http_runner.setup()

        site = web.TCPSite(self.http_runner, "0.0.0.0", port)
        await site.start()

        logger.info(f"HTTP server started on port {port}")

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "bot_user": str(self.user) if self.user else None,
            "guilds": len(self.guilds)
        })

    def resolve_channel_by_name(self, name: str):
        """Resolve a channel name to a discord.TextChannel using the bot's guild cache."""
        for guild in self.guilds:
            for ch in guild.text_channels:
                if ch.name == name:
                    return ch
        return None

    def resolve_channel(self, identifier: str):
        """Resolve a channel by numeric ID or name. Returns (channel, error_msg) tuple.
        error_msg is a string if resolution failed, None on success."""
        if not identifier:
            return None, "channel_id or channel_name is required"
        if identifier.isdigit():
            channel = self.get_channel(int(identifier))
            if not channel:
                return None, f"Channel not found: {identifier}"
            return channel, None
        channel = self.resolve_channel_by_name(identifier)
        if not channel:
            return None, f"No channel found with name '{identifier}'"
        return channel, None

    async def handle_notify(self, request: web.Request) -> web.Response:
        """Create a notification thread in a channel.

        POST /notify
        {
            "channel_id": "123" | "channel_name": "pulse",
            "title": "Thread Title",
            "content": "Message body",
            "conversation_id": "con_xxx"
        }
        """
        try:
            data = await request.json()
            channel_id = data.get("channel_id")
            channel_name = data.get("channel_name")
            title = data.get("title", "Notification")[:100]
            content = data.get("content", "")
            conversation_id = data.get("conversation_id", "")

            # Reject if this conversation already has a linked Discord thread
            if conversation_id:
                existing = await get_mapping_by_conversation(conversation_id)
                if existing:
                    thread_id = existing.get("thread_id", "unknown")
                    thread_name = existing.get("thread_name", "")
                    return web.json_response({
                        "error": (
                            f"Conversation {conversation_id} already has a linked Discord thread "
                            f"(thread_id: {thread_id}, name: '{thread_name}'). "
                            f"You are already in that thread — your normal text responses will appear there. "
                            f"Do NOT use zo-discord notify; just respond directly."
                        ),
                        "existing_thread_id": thread_id,
                        "conversation_id": conversation_id
                    }, status=409)

            if channel_name and not channel_id:
                channel = self.resolve_channel_by_name(channel_name)
                if not channel:
                    return web.json_response({"error": f"No channel found with name '{channel_name}'"}, status=404)
            elif channel_id:
                channel = self.get_channel(int(channel_id))
                if not channel:
                    return web.json_response({"error": "Channel not found"}, status=404)
            else:
                return web.json_response({"error": "channel_id or channel_name is required"}, status=400)

            # Starter message: title + mention (creates the thread, one notification)
            starter_parts = [f"**{title}**"]
            allowed_users = self.config.get("allowed_users", [])
            if allowed_users:
                mentions = " ".join(f"<@{uid}>" for uid in allowed_users)
                starter_parts.append(mentions)
            msg = await channel.send(" ".join(starter_parts))
            thread = await msg.create_thread(
                name=title,
            )

            await save_mapping(
                thread_id=str(thread.id),
                conversation_id=conversation_id,
                channel_id=str(channel.id),
                guild_id=str(channel.guild.id),
                thread_name=title
            )

            # Body goes inside the thread (reactable for dismiss)
            if content:
                chunks = self.zo.chunk_response(content)
                for chunk in chunks:
                    await send_suppressed(thread, content=chunk)

            logger.info(f"Notification thread '{title}' created (id: {thread.id})")

            return web.json_response({
                "success": True,
                "thread_id": str(thread.id),
                "conversation_id": conversation_id
            })

        except Exception as e:
            logger.error(f"Notify error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def handle_list_threads(self, request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", 50))
        guild_id = request.query.get("guild_id")

        threads = await get_active_threads(guild_id, limit)

        result = []
        for t in threads:
            thread_data = {
                "thread_id": t["thread_id"],
                "conversation_id": t["conversation_id"],
                "channel_id": t["channel_id"],
                "current_name": t["thread_name"],
                "last_activity": t["last_activity"],
                "recent_messages": []
            }

            try:
                thread = self.get_channel(int(t["thread_id"]))
                if thread:
                    messages = []
                    async for msg in thread.history(limit=10):
                        messages.append({
                            "author": msg.author.display_name,
                            "content": msg.content[:200],
                            "is_bot": msg.author.bot
                        })
                    thread_data["recent_messages"] = list(reversed(messages))
            except Exception:
                pass

            result.append(thread_data)

        return web.json_response(result)

    async def handle_rename_thread(self, request: web.Request) -> web.Response:
        thread_id = request.match_info["thread_id"]

        try:
            data = await request.json()
            new_name = data["name"][:100]

            thread = await self.resolve_thread(thread_id)
            if not thread:
                return web.json_response({"error": "Thread not found"}, status=404)

            # Preserve current status prefix
            current_status = await get_thread_status(thread_id)
            display_name = set_thread_status_prefix(new_name, current_status)

            try:
                await asyncio.wait_for(thread.edit(name=display_name), timeout=10.0)
            except (asyncio.TimeoutError, discord.HTTPException) as e:
                logger.warning(f"Rename Discord API call failed ({e}), saving to DB only")
            await update_thread_name(thread_id, display_name)

            return web.json_response({"success": True, "name": display_name})

        except Exception as e:
            logger.error(f"Rename error for thread {thread_id}: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_buttons(self, request: web.Request) -> web.Response:
        """Send interactive buttons to a thread.

        POST /buttons
        {
            "thread_id": "123",
            "prompt": "Do you approve?",
            "buttons": [{"label": "Yes", "id": "yes", "style": "success"}, ...],
            "preset": "approve_reject"  // optional, overrides buttons
        }
        """
        try:
            data = await request.json()
            thread_id = data["thread_id"]
            prompt = data.get("prompt", "Choose an option:")

            presets = {
                "approve_reject": [
                    {"label": "Approve", "id": "approve", "style": "success"},
                    {"label": "Reject", "id": "reject", "style": "danger"},
                ],
                "yes_no": [
                    {"label": "Yes", "id": "yes", "style": "success"},
                    {"label": "No", "id": "no", "style": "danger"},
                ],
            }

            buttons = data.get("buttons") or presets.get(data.get("preset"), [])
            if not buttons:
                return web.json_response({"error": "No buttons specified"}, status=400)

            thread = await self.resolve_thread(thread_id)
            if not thread:
                return web.json_response({"error": "Thread not found"}, status=404)

            view = ButtonCallbackView(self, thread_id, buttons)
            msg = await thread.send(prompt, view=view)

            return web.json_response({
                "success": True,
                "message_id": str(msg.id)
            })

        except Exception as e:
            logger.error(f"Buttons error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def handle_files(self, request: web.Request) -> web.Response:
        """Send a file attachment to a thread.

        POST /files
        {
            "thread_id": "123",
            "file_path": "/home/workspace/report.pdf",
            "message": "Here's the report"
        }
        """
        try:
            data = await request.json()
            thread_id = data["thread_id"]
            file_path = Path(data["file_path"])
            message = data.get("message", "")

            if not file_path.exists():
                return web.json_response({"error": f"File not found: {file_path}"}, status=404)

            if file_path.stat().st_size > 25 * 1024 * 1024:
                return web.json_response({"error": "File too large (max 25MB)"}, status=400)

            thread = await self.resolve_thread(thread_id)
            if not thread:
                return web.json_response({"error": "Thread not found"}, status=404)

            msg = await thread.send(
                content=message or None,
                file=discord.File(str(file_path))
            )

            return web.json_response({
                "success": True,
                "message_id": str(msg.id)
            })

        except Exception as e:
            logger.error(f"File send error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def handle_embeds(self, request: web.Request) -> web.Response:
        """Send a rich embed to a thread.

        POST /embeds
        {
            "thread_id": "123",
            "title": "Report",
            "description": "Summary...",
            "color": "blue",
            "fields": [{"name": "Key", "value": "Value", "inline": true}],
            "footer": "Optional footer text"
        }
        """
        try:
            data = await request.json()
            thread_id = data["thread_id"]

            colors = {
                "blue": 0x3498db,
                "green": 0x2ecc71,
                "red": 0xe74c3c,
                "yellow": 0xf1c40f,
                "purple": 0x9b59b6,
                "orange": 0xe67e22,
                "gray": 0x95a5a6,
            }
            color = colors.get(data.get("color", "blue"), 0x3498db)

            embed = discord.Embed(
                title=data.get("title"),
                description=data.get("description"),
                color=color
            )

            for field in data.get("fields", []):
                embed.add_field(
                    name=field["name"],
                    value=field["value"],
                    inline=field.get("inline", False)
                )

            if data.get("footer"):
                embed.set_footer(text=data["footer"])

            thread = await self.resolve_thread(thread_id)
            if not thread:
                return web.json_response({"error": "Thread not found"}, status=404)

            msg = await thread.send(embed=embed)

            return web.json_response({
                "success": True,
                "message_id": str(msg.id)
            })

        except Exception as e:
            logger.error(f"Embed error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def handle_react(self, request: web.Request) -> web.Response:
        """Add a reaction to a message.

        POST /react
        {"channel_id": "123", "message_id": "456", "emoji": "\u2705"}
        Accepts channel_name instead of channel_id.
        """
        try:
            data = await request.json()
            channel, err = self.resolve_channel(data.get("channel_name") or data.get("channel_id", ""))
            if err:
                return web.json_response({"error": err}, status=404)

            message = await channel.fetch_message(int(data["message_id"]))
            await message.add_reaction(data["emoji"])

            return web.json_response({"success": True})

        except Exception as e:
            logger.error(f"React error (channel={data.get('channel_name') or data.get('channel_id')}, msg={data.get('message_id')}): {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_edit_message(self, request: web.Request) -> web.Response:
        """Edit a bot message.

        POST /messages/edit
        {"channel_id": "123", "message_id": "456", "content": "New content"}
        Accepts channel_name instead of channel_id.
        """
        try:
            data = await request.json()
            channel, err = self.resolve_channel(data.get("channel_name") or data.get("channel_id", ""))
            if err:
                return web.json_response({"error": err}, status=404)

            message = await channel.fetch_message(int(data["message_id"]))
            if message.author.id != self.user.id:
                return web.json_response({"error": "Can only edit own messages"}, status=403)

            await message.edit(content=data["content"])

            return web.json_response({"success": True})

        except Exception as e:
            logger.error(f"Edit error (channel={data.get('channel_name') or data.get('channel_id')}, msg={data.get('message_id')}): {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_delete_message(self, request: web.Request) -> web.Response:
        """Delete a bot message.

        DELETE /messages
        {"channel_id": "123", "message_id": "456"}
        Accepts channel_name instead of channel_id.
        """
        try:
            data = await request.json()
            channel, err = self.resolve_channel(data.get("channel_name") or data.get("channel_id", ""))
            if err:
                return web.json_response({"error": err}, status=404)

            message = await channel.fetch_message(int(data["message_id"]))
            if message.author.id != self.user.id:
                return web.json_response({"error": "Can only delete own messages"}, status=403)

            await message.delete()

            return web.json_response({"success": True})

        except Exception as e:
            logger.error(f"Delete error (channel={data.get('channel_name') or data.get('channel_id')}, msg={data.get('message_id')}): {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_send_message(self, request: web.Request) -> web.Response:
        """Send a message to a channel or thread.

        POST /messages/send
        {"channel_id": "123", "content": "Hello"}
        Accepts channel_name instead of channel_id.
        """
        try:
            data = await request.json()
            channel, err = self.resolve_channel(data.get("channel_name") or data.get("channel_id", ""))
            if err:
                return web.json_response({"error": err}, status=404)

            chunks = self.zo.chunk_response(data["content"])
            msg = None
            for chunk in chunks:
                msg = await send_suppressed(channel, content=chunk)

            return web.json_response({
                "success": True,
                "message_id": str(msg.id) if msg else None
            })

        except Exception as e:
            logger.error(f"Send error (channel={data.get('channel_name') or data.get('channel_id')}): {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_set_status(self, request: web.Request) -> web.Response:
        """Set thread status.

        POST /threads/{thread_id}/status
        {"status": "error|complete"}
        """
        thread_id = request.match_info["thread_id"]
        try:
            data = await request.json()
            status = data.get("status")
            if status == "complete":
                try:
                    thread = await self.resolve_thread(thread_id)
                    if thread:
                        await set_watched(thread_id, False)
                        await thread.edit(archived=True)
                except Exception as e:
                    logger.error(f"Failed to archive: {e}")
                return web.json_response({"success": True, "status": status})

            if status not in STATUS_EMOJI:
                return web.json_response({"error": f"Invalid status. Valid: {list(STATUS_EMOJI.keys()) + ['complete']}"}, status=400)

            thread = await self.resolve_thread(thread_id)
            if not thread:
                return web.json_response({"error": "Thread not found in Discord"}, status=404)

            await self.set_status(thread, status)

            return web.json_response({"success": True, "status": status})

        except Exception as e:
            logger.error(f"Status error for thread {thread_id}: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_conversation_action(self, request: web.Request) -> web.Response:
        """Perform a thread action using conversation ID instead of thread ID.

        POST /conversations/{conv_id}/action
        {"action": "rename", "name": "New title"}
        {"action": "error|complete"}
        {"action": "send", "content": "message"}
        """
        conv_id = request.match_info["conv_id"]
        try:
            data = await request.json()
            action = data.get("action")

            # Rename handles its own mapping check (queues for unmapped conv_ids)
            if action == "rename":
                name = data.get("name", "")[:100]
                mapping = await get_mapping_by_conversation(conv_id)
                if not mapping:
                    self.queued_renames[conv_id] = name
                    logger.info(f"Queued rename '{name}' for conv {conv_id}")
                    return web.json_response({"success": True, "queued": True, "name": name})
                thread_id = mapping["thread_id"]
                thread = await self.resolve_thread(thread_id)
                if not thread:
                    return web.json_response({"error": "Thread not found in Discord"}, status=404)
                current_status = await get_thread_status(thread_id)
                display_name = set_thread_status_prefix(name, current_status)
                try:
                    await asyncio.wait_for(thread.edit(name=display_name), timeout=10.0)
                except (asyncio.TimeoutError, discord.HTTPException) as e:
                    logger.warning(f"Rename Discord API call failed ({e}), saving to DB only")
                await update_thread_name(thread_id, display_name)
                return web.json_response({"success": True, "name": display_name})

            # All other actions require an existing mapping
            mapping = await get_mapping_by_conversation(conv_id)
            if not mapping:
                return web.json_response({"error": f"No thread found for conversation {conv_id}"}, status=404)

            thread_id = mapping["thread_id"]
            thread = await self.resolve_thread(thread_id)
            if not thread:
                return web.json_response({"error": "Thread not found in Discord"}, status=404)

            if action == "complete":
                try:
                    await set_watched(thread_id, False)
                    await thread.edit(archived=True)
                except Exception as e:
                    logger.error(f"Failed to archive: {e}")
                return web.json_response({"success": True, "status": action})

            elif action in STATUS_EMOJI:
                await self.set_status(thread, action)
                return web.json_response({"success": True, "status": action})

            elif action == "send":
                content = data.get("content", "")
                if not content:
                    return web.json_response({"error": "content required"}, status=400)
                chunks = self.zo.chunk_response(content)
                msg = None
                for chunk in chunks:
                    msg = await send_suppressed(thread, content=chunk)
                return web.json_response({"success": True, "message_id": str(msg.id)})

            else:
                return web.json_response({"error": f"Unknown action: {action}. Valid: rename, send, {', '.join(STATUS_EMOJI.keys())}"}, status=400)

        except Exception as e:
            logger.error(f"Conversation action error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def handle_conversation_files(self, request: web.Request) -> web.Response:
        """Send a file attachment to the thread for a conversation.

        POST /conversations/{conv_id}/files
        {
            "file_path": "/home/workspace/Images/generated.png",
            "message": "Optional text"
        }
        """
        conv_id = request.match_info["conv_id"]
        try:
            data = await request.json()
            file_path = Path(data.get("file_path", ""))
            message = data.get("message", "")

            if not file_path or not str(file_path).strip():
                return web.json_response({"error": "file_path is required"}, status=400)

            if not file_path.exists():
                return web.json_response({"error": f"File not found: {file_path}"}, status=404)

            if file_path.stat().st_size > 25 * 1024 * 1024:
                return web.json_response({"error": "File too large (max 25MB)"}, status=400)

            mapping = await get_mapping_by_conversation(conv_id)
            if not mapping:
                return web.json_response({"error": f"No thread found for conversation {conv_id}"}, status=404)

            thread_id = mapping["thread_id"]
            thread = await self.resolve_thread(thread_id)
            if not thread:
                return web.json_response({"error": "Thread not found in Discord"}, status=404)

            msg = await thread.send(
                content=message or None,
                file=discord.File(str(file_path))
            )

            return web.json_response({
                "success": True,
                "message_id": str(msg.id)
            })

        except Exception as e:
            logger.error(f"Conversation file send error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def handle_new_thread(self, request: web.Request) -> web.Response:
        """Spawn a new Discord thread with a fresh Zo session.

        POST /conversations/{conv_id}/new-thread
        {
            "title": "Thread Title",
            "prompt": "Context or question for the new thread",
            "channel_id": "optional — defaults to current thread's channel",
            "channel_name": "optional — resolve by name instead of ID"
        }
        """
        conv_id = request.match_info["conv_id"]
        try:
            data = await request.json()
            title = data.get("title", "New Thread")[:100]
            prompt = data.get("prompt", "")
            target_channel_id = data.get("channel_id")
            target_channel_name = data.get("channel_name")

            if not prompt:
                return web.json_response({"error": "prompt is required"}, status=400)

            if target_channel_name and not target_channel_id:
                channel = self.resolve_channel_by_name(target_channel_name)
                if not channel:
                    return web.json_response({"error": f"No channel found with name '{target_channel_name}'"}, status=404)
            elif target_channel_id:
                channel = self.get_channel(int(target_channel_id))
            else:
                mapping = await get_mapping_by_conversation(conv_id)
                if mapping:
                    channel = self.get_channel(int(mapping["channel_id"]))
                else:
                    return web.json_response({"error": "No thread found for conversation and no channel_id/channel_name provided"}, status=400)

            if not channel:
                return web.json_response({"error": "Channel not found"}, status=404)

            # Combine title + mention into single starter message
            starter_parts = [title]
            allowed_users = self.config.get("allowed_users", [])
            if allowed_users:
                mentions = " ".join(f"<@{uid}>" for uid in allowed_users)
                starter_parts.append(mentions)
            msg = await channel.send("\n".join(starter_parts))
            thread = await msg.create_thread(
                name=title,
            )

            await save_mapping(
                thread_id=str(thread.id),
                conversation_id="",
                channel_id=str(channel.id),
                guild_id=str(channel.guild.id),
                thread_name=title
            )

            # Resolve channel model/persona defaults
            channel_model, channel_persona, channel_backend, channel_hermes_params = await self.resolve_channel_defaults(str(channel.id))

            context, file_paths = await self.build_channel_context(channel, include_source=True, thread=thread, backend=channel_backend or "")
            config = load_config()
            effective_persona = channel_persona or config.get("default_persona")

            thread_id_str = str(thread.id)

            on_thinking = self.make_on_thinking(thread)

            async def on_conv_id(cid: str):
                await update_conversation_id(thread_id_str, cid)

            result = await self.zo.ask_stream(
                prompt,
                context=context or None,
                file_paths=file_paths or None,
                on_thinking=on_thinking,
                on_conv_id=on_conv_id,
                model_name=channel_model,
                persona_id=effective_persona,
                backend=channel_backend,
                **channel_hermes_params,
            )
            response, new_conv_id = result.output, result.conv_id

            if not response or not response.strip():
                logger.warning(f"Empty response for new thread {thread.id} (interrupted={result.interrupted}, events={result.received_events})")
                response, new_conv_id = await self._retry_empty_response(
                    thread_id_str, new_conv_id, thread, on_thinking, on_conv_id,
                    backend=channel_backend,
                )

            chunks = self.zo.chunk_response(response)
            chunks = [c for c in chunks if c.strip()]
            if not chunks:
                logger.error(f"All retries exhausted for new thread {thread.id}")
                chunks = [f"\u26a0\ufe0f **Zo didn't respond after multiple retries.**\n\nConversation: `{new_conv_id}`\n\nSend another message to try again."]
            for chunk in chunks:
                await send_suppressed(thread, content=chunk)

            logger.info(f"Created new thread '{title}' (id: {thread.id}) with conv {new_conv_id}")

            return web.json_response({
                "success": True,
                "thread_id": str(thread.id),
                "conversation_id": new_conv_id
            })

        except Exception as e:
            logger.error(f"New thread error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def handle_conversation_buttons(self, request: web.Request) -> web.Response:
        """Send interactive buttons to a thread using conversation ID.

        POST /conversations/{conv_id}/buttons
        {
            "prompt": "Do you approve?",
            "buttons": [{"label": "Yes", "id": "yes", "style": "success"}, ...],
            "preset": "approve_reject"  // optional, overrides buttons
        }
        """
        conv_id = request.match_info["conv_id"]
        try:
            data = await request.json()

            mapping = await get_mapping_by_conversation(conv_id)
            if not mapping:
                return web.json_response({"error": f"No thread found for conversation {conv_id}"}, status=404)

            thread_id = mapping["thread_id"]
            data["thread_id"] = thread_id
            # Delegate to existing handler by constructing a modified request
            prompt = data.get("prompt", "Choose an option:")

            presets = {
                "approve_reject": [
                    {"label": "Approve", "id": "approve", "style": "success"},
                    {"label": "Reject", "id": "reject", "style": "danger"},
                ],
                "yes_no": [
                    {"label": "Yes", "id": "yes", "style": "success"},
                    {"label": "No", "id": "no", "style": "danger"},
                ],
            }

            buttons = data.get("buttons") or presets.get(data.get("preset"), [])
            if not buttons:
                return web.json_response({"error": "No buttons specified"}, status=400)

            thread = await self.resolve_thread(thread_id)
            if not thread:
                return web.json_response({"error": "Thread not found"}, status=404)

            view = ButtonCallbackView(self, thread_id, buttons)
            msg = await thread.send(prompt, view=view)

            return web.json_response({
                "success": True,
                "message_id": str(msg.id)
            })

        except Exception as e:
            logger.error(f"Conversation buttons error: {e}", exc_info=True)
            return web.json_response({"error": str(e)}, status=500)

    async def handle_config(self, request: web.Request) -> web.Response:
        """Simple config endpoint for agents. Accepts channel_id in body.

        POST /config
        {"channel_id": "123", "reasoning": "high", "skip_memory": true, ...}
        """
        try:
            data = await request.json()
            channel_id = data.pop("channel_id", None)
            if not channel_id:
                return web.json_response({"error": "channel_id is required"}, status=400)
            await set_channel_config(str(channel_id), **data)
            config = await get_channel_config(str(channel_id))
            return web.json_response({"success": True, "config": config})
        except Exception as e:
            logger.error(f"Config endpoint error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_get_channel_config(self, request: web.Request) -> web.Response:
        channel, err = self.resolve_channel(request.match_info["channel_id"])
        if err:
            return web.json_response({"error": err}, status=404)
        channel_id = str(channel.id)
        config = await get_channel_config(channel_id)
        if not config:
            return web.json_response({"error": "No config for this channel"}, status=404)
        return web.json_response(config)

    async def handle_set_channel_config(self, request: web.Request) -> web.Response:
        channel, err = self.resolve_channel(request.match_info["channel_id"])
        if err:
            return web.json_response({"error": err}, status=404)
        channel_id = str(channel.id)
        try:
            data = await request.json()
            await set_channel_config(channel_id, **data)
            config = await get_channel_config(channel_id)
            return web.json_response({"success": True, "config": config})
        except Exception as e:
            logger.error(f"Set channel config error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def handle_delete_channel_config(self, request: web.Request) -> web.Response:
        channel, err = self.resolve_channel(request.match_info["channel_id"])
        if err:
            return web.json_response({"error": err}, status=404)
        channel_id = str(channel.id)
        try:
            await delete_channel_config(channel_id)
            return web.json_response({"success": True})
        except Exception as e:
            logger.error(f"Delete channel config error: {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def close(self):
        if self.http_runner:
            await self.http_runner.cleanup()
        await super().close()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Discord-Zo Bridge Bot")
    parser.add_argument("--status", action="store_true", help="Check bot status")
    args = parser.parse_args()

    if args.status:
        token = os.environ.get("DISCORD_BOT_TOKEN")
        api_key = os.environ.get("DISCORD_ZO_API_KEY")

        print("Discord-Zo Bot Status")
        print("=" * 40)
        token_status = "\u2713 Set" if token else "\u2717 Not set"
        key_status = "\u2713 Set" if api_key else "\u2717 Not set"
        print(f"DISCORD_BOT_TOKEN: {token_status}")
        print(f"DISCORD_ZO_API_KEY: {key_status}")
        from zo_discord import PROJECT_ROOT
        print(f"Config: {PROJECT_ROOT / 'config' / 'config.json'}")

        if not token or not api_key:
            print("\nAdd missing secrets at: Settings > Developers")
            sys.exit(1)

        sys.exit(0)

    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        logger.error("DISCORD_BOT_TOKEN not set. Add it in Settings > Developers")
        sys.exit(1)

    bot = ZoDiscordBot()
    bot.run(token)


if __name__ == "__main__":
    main()
