"""
Zo API client for Discord integration.
Handles calling the Zo API with conversation persistence and context injection.
"""

import asyncio
import aiohttp
import logging
import os
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncGenerator, Callable, Awaitable

logger = logging.getLogger(__name__)


@dataclass
class StreamResult:
    """Result from ask_stream with diagnostic info for retry decisions."""
    output: str
    conv_id: str
    interrupted: bool  # stream broke before End event
    received_events: bool  # got any SSE events at all

CONFIG_PATH = Path(__file__).parent / "config" / "config.json"

# Streaming flush config
FLUSH_MIN_SENTENCES = 3
FLUSH_COOLDOWN_SECONDS = 30

# Session pool exhaustion retry config (longer delays — pool needs time to free up)
SESSION_POOL_RETRY_DELAYS = [15, 30, 60, 120]
SESSION_POOL_ERROR_MARKERS = ["sessions are busy", "cannot evict"]


def _is_session_pool_error(error_text: str) -> bool:
    """Check if an API error indicates session pool exhaustion (vs. conversation-specific busy)."""
    lower = error_text.lower()
    return any(marker in lower for marker in SESSION_POOL_ERROR_MARKERS)


def _count_sentences(text: str) -> int:
    """Count sentences in text. Splits on . ! ? followed by space or end."""
    return len(re.findall(r'[.!?](?:\s|$)', text))


def load_config() -> dict:
    """Load bot configuration."""
    with open(CONFIG_PATH) as f:
        return json.load(f)


class ZoClient:
    """Async client for the Zo API."""

    BASE_URL = "https://api.zo.computer"

    def __init__(self):
        self.api_key = os.environ.get("DISCORD_ZO_API_KEY")
        if not self.api_key:
            raise ValueError("DISCORD_ZO_API_KEY environment variable not set")

        config = load_config()
        self.model = config.get("model")
        self.max_length = config.get("max_message_length", 1900)

    async def ask(
        self,
        input_text: str,
        conversation_id: str = None,
        context_parts: list[dict] = None,
        context_paths: list[str] = None,
    ) -> tuple[str, str]:
        """
        Send a message to Zo (non-streaming) via /zo/ask.

        Returns:
            Tuple of (response_text, conversation_id)
        """
        payload = {
            "input": input_text,
            "stream": False,
        }

        if self.model:
            payload["model_name"] = self.model
        if conversation_id:
            payload["conversation_id"] = conversation_id

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        timeout = aiohttp.ClientTimeout(total=1800)

        # Retry loop for session pool exhaustion (all sessions busy).
        # This is distinct from conversation-specific 409s — the API couldn't
        # allocate any session at all. Wait with longer delays and re-POST.
        for pool_attempt, pool_delay in enumerate(SESSION_POOL_RETRY_DELAYS):
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    f"{self.BASE_URL}/zo/ask",
                    headers=headers,
                    json=payload
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        if _is_session_pool_error(error_text) and pool_attempt < len(SESSION_POOL_RETRY_DELAYS) - 1:
                            logger.warning(
                                f"Session pool full ({resp.status}), retry {pool_attempt + 1}/{len(SESSION_POOL_RETRY_DELAYS)} in {pool_delay}s"
                            )
                            await asyncio.sleep(pool_delay)
                            continue
                        raise Exception(f"Zo API error {resp.status}: {error_text}")

                    data = await resp.json()
                    output = data["output"]
                    conv_id = data["conversation_id"]

                if output and output.strip():
                    return output, conv_id
                break  # Got a 200 but empty — fall through to existing empty-response handling

            logger.warning(f"Empty response from non-streaming ask (conv {conv_id}), trying wait_for_idle")
            idle_result = await self.wait_for_idle(conv_id)
            if idle_result and idle_result.output and idle_result.output.strip():
                return idle_result.output, idle_result.conv_id

            retry_delays = [15, 30, 60]
            for attempt, delay in enumerate(retry_delays, 1):
                logger.warning(f"Empty response (conv {conv_id}), continue attempt {attempt}/{len(retry_delays)} in {delay}s")
                await asyncio.sleep(delay)
                retry_payload = {
                    "input": "Please continue.",
                    "stream": False,
                    "conversation_id": conv_id,
                }
                if self.model:
                    retry_payload["model_name"] = self.model
                try:
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.post(
                            f"{self.BASE_URL}/zo/ask",
                            headers=headers,
                            json=retry_payload
                        ) as resp:
                            if resp.status == 409:
                                logger.info(f"Conv {conv_id} busy on continue attempt {attempt}, waiting")
                                idle_result = await self.wait_for_idle(conv_id)
                                if idle_result and idle_result.output and idle_result.output.strip():
                                    return idle_result.output, idle_result.conv_id
                                continue
                            if resp.status != 200:
                                error_text = await resp.text()
                                raise Exception(f"Zo API error {resp.status} on continue attempt {attempt}: {error_text}")
                            data = await resp.json()
                            output = data["output"]
                            conv_id = data["conversation_id"]
                        if output and output.strip():
                            logger.info(f"Continue attempt {attempt} succeeded for conv {conv_id}")
                            return output, conv_id
                except Exception as e:
                    logger.error(f"Continue attempt {attempt} failed for conv {conv_id}: {e}")

            logger.error(f"All retries exhausted for conv {conv_id}")
            return "", conv_id

    async def ask_stream(
        self,
        input_text: str,
        conversation_id: str = None,
        context: str = None,
        file_paths: list[str] = None,
        on_thinking: Callable[[str], Awaitable[None]] = None,
        on_conv_id: Callable[[str], Awaitable[None]] = None,
    ) -> StreamResult:
        """
        Send a message to Zo via the /zo/ask streaming endpoint.

        Args:
            input_text: The user's message
            conversation_id: Optional existing conversation ID
            context: Optional context string appended after the user message
            file_paths: Optional list of file paths referenced in the context
            on_thinking: Async callback for thinking previews (receives text to post)
            on_conv_id: Async callback when conversation ID is received

        Returns:
            StreamResult with output, conv_id, and diagnostic info
        """
        full_input = input_text
        if context:
            full_input = f"{input_text}\n\n{context}"
        if file_paths:
            paths_str = "\n".join(f"- `{p}`" for p in file_paths)
            full_input = f"{full_input}\n\n## Referenced Files\n{paths_str}"

        payload = {
            "input": full_input,
            "stream": True,
        }

        if self.model:
            payload["model_name"] = self.model
        if conversation_id:
            payload["conversation_id"] = conversation_id

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        timeout = aiohttp.ClientTimeout(total=1800)
        conv_id = conversation_id or ""
        final_output = ""
        stream_interrupted = False
        received_any_events = False

        # Outer retry loop for session pool exhaustion (all sessions busy).
        # Inner loop handles conversation-specific 409s (shorter delays).
        for pool_attempt, pool_delay in enumerate(SESSION_POOL_RETRY_DELAYS):
            pool_exhausted = False

            # Retry loop for 409 (conversation busy). This is a safety net for
            # edge cases like bot restarts while a session is still locked.
            # Normal flow uses message queuing so 409 should be rare.
            retry_delays_409 = [5, 10, 20]
            max_409_attempts = len(retry_delays_409)
            for attempt in range(max_409_attempts):
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        f"{self.BASE_URL}/zo/ask",
                        headers=headers,
                        json=payload,
                    ) as resp:
                        if resp.status == 409 and attempt < max_409_attempts - 1:
                            delay = retry_delays_409[attempt]
                            logger.warning(f"Conversation busy (409), retry {attempt + 1}/{max_409_attempts} in {delay}s")
                            await asyncio.sleep(delay)
                            continue
                        if resp.status != 200:
                            error_text = await resp.text()
                            if _is_session_pool_error(error_text) and pool_attempt < len(SESSION_POOL_RETRY_DELAYS) - 1:
                                logger.warning(
                                    f"Session pool full ({resp.status}), retry {pool_attempt + 1}/{len(SESSION_POOL_RETRY_DELAYS)} in {pool_delay}s"
                                )
                                pool_exhausted = True
                                break  # break inner 409 loop to hit outer pool retry
                            raise Exception(f"Zo API error {resp.status}: {error_text}")

                        conv_id = resp.headers.get("X-Conversation-Id", conversation_id or "")

                        if conv_id and on_conv_id:
                            await on_conv_id(conv_id)

                        text_buffer = ""
                        flushed_buffer = ""  # tracks what was already sent as thinking
                        last_flush_time = time.monotonic()
                        in_text_part = False
                        current_event_type = ""

                        try:
                            remainder = b""
                            stream_done = False
                            async for chunk in resp.content.iter_any():
                                remainder += chunk
                                while b"\n" in remainder:
                                    raw_line, remainder = remainder.split(b"\n", 1)
                                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r")

                                    if line.startswith("event: "):
                                        current_event_type = line[7:]
                                        received_any_events = True
                                        continue

                                    if not line.startswith("data: "):
                                        continue

                                    data_str = line[6:]
                                    if not data_str or data_str == "[DONE]":
                                        continue

                                    try:
                                        event = json.loads(data_str)
                                    except json.JSONDecodeError:
                                        continue

                                    received_any_events = True

                                    if current_event_type == "PartStartEvent":
                                        part = event.get("part", {})
                                        part_kind = part.get("part_kind", "")
                                        if part_kind == "text":
                                            in_text_part = True
                                            initial_content = part.get("content", "")
                                            if initial_content:
                                                if text_buffer and not text_buffer.endswith("\n"):
                                                    text_buffer += "\n\n"
                                                text_buffer += initial_content
                                        elif part_kind == "builtin-tool-call":
                                            in_text_part = False
                                            if on_thinking and text_buffer.strip():
                                                now = time.monotonic()
                                                elapsed = now - last_flush_time
                                                new_text = re.sub(r'\n{3,}', '\n\n', text_buffer[len(flushed_buffer):].strip())
                                                if (new_text
                                                        and _count_sentences(new_text) >= FLUSH_MIN_SENTENCES
                                                        and elapsed >= FLUSH_COOLDOWN_SECONDS):
                                                    await on_thinking(new_text)
                                                    flushed_buffer = text_buffer
                                                    last_flush_time = now

                                    elif current_event_type == "PartDeltaEvent":
                                        delta = event.get("delta", {})
                                        if delta.get("part_delta_kind") == "text":
                                            text = delta.get("content_delta", "")
                                            if in_text_part:
                                                text_buffer += text

                                    elif current_event_type == "PartEndEvent":
                                        in_text_part = False

                                    elif current_event_type == "End":
                                        final_output = event.get("data", {}).get("output", "")
                                        stream_done = True
                                        break
                                if stream_done:
                                    break
                        except (aiohttp.ClientPayloadError, aiohttp.ClientConnectionError, aiohttp.ClientOSError) as e:
                            stream_interrupted = True
                            logger.warning(f"Stream interrupted for conv {conv_id}: {e}")

                        if not stream_done and not stream_interrupted:
                            stream_interrupted = True
                            logger.warning(f"Stream ended without End event for conv {conv_id} (received_events={received_any_events})")

                        # Streaming completed (or was interrupted), exit inner retry loop
                        break

            if pool_exhausted:
                await asyncio.sleep(pool_delay)
                continue  # retry outer pool loop

            break  # success or non-pool error — exit outer loop

        return StreamResult(
            output=final_output,
            conv_id=conv_id,
            interrupted=stream_interrupted,
            received_events=received_any_events,
        )

    async def wait_for_idle(self, conversation_id: str, max_wait: int = 300) -> "StreamResult | None":
        """Poll the conversation state until the agent is no longer running.

        Uses the read-only GET /conversations/{id} endpoint to check state
        without sending any messages that could interrupt a working agent.

        If the agent finished while our stream was broken, extracts the last
        assistant output from the conversation history.

        Args:
            conversation_id: The conversation to poll.
            max_wait: Maximum total seconds to wait before giving up.

        Returns:
            StreamResult if the agent became idle (output extracted from history),
            or None if we timed out waiting.
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
        }
        timeout = aiohttp.ClientTimeout(total=30)

        poll_delays = [5, 10, 15, 20, 30, 30, 30, 30, 30, 30]
        elapsed = 0

        for delay in poll_delays:
            if elapsed >= max_wait:
                break

            logger.info(f"Waiting {delay}s for conv {conversation_id} to become idle (elapsed {elapsed}s)")
            await asyncio.sleep(delay)
            elapsed += delay

            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(
                        f"{self.BASE_URL}/conversations/{conversation_id}",
                        headers=headers,
                    ) as resp:
                        if resp.status != 200:
                            error_text = await resp.text()
                            logger.warning(f"Failed to get conv {conversation_id} state: {resp.status} {error_text}")
                            continue

                        data = await resp.json()
                        state = data.get("state", "idle")

                        if state == "running":
                            logger.info(f"Conv {conversation_id} still running, continuing to wait")
                            continue

                        # State is "idle" or "stopping" — agent is done
                        output = self._extract_last_assistant_output(data)
                        logger.info(f"Conv {conversation_id} is {state}, extracted {len(output)} chars from history")
                        return StreamResult(
                            output=output,
                            conv_id=conversation_id,
                            interrupted=False,
                            received_events=True,
                        )
            except Exception as e:
                logger.warning(f"Error polling conv {conversation_id}: {e}")

        logger.error(f"Timed out waiting for conv {conversation_id} to become idle after {elapsed}s")
        return None

    @staticmethod
    def _extract_last_assistant_output(conv_data: dict) -> str:
        """Extract the last assistant text output from a conversation response.

        Walks the message history backwards to find the last model-response
        with a text part, which is the agent's final answer.
        """
        messages = conv_data.get("messages", [])
        for msg in reversed(messages):
            if msg.get("kind") != "response":
                continue
            parts = msg.get("parts", [])
            for part in reversed(parts):
                if part.get("part_kind") == "text":
                    content = part.get("content", "")
                    if content and content.strip():
                        return content
        return ""

    async def send_continue(
        self,
        input_text: str,
        conversation_id: str,
        on_thinking: Callable[[str], Awaitable[None]] = None,
        on_conv_id: Callable[[str], Awaitable[None]] = None,
    ) -> StreamResult:
        """Send a follow-up message to an idle conversation via streaming.

        Used after wait_for_idle confirms the agent is no longer busy.
        """
        return await self.ask_stream(
            input_text,
            conversation_id=conversation_id,
            on_thinking=on_thinking,
            on_conv_id=on_conv_id,
        )

    async def stop_conversation(self, conversation_id: str) -> bool:
        """
        Stop an in-flight conversation.

        Returns True if the stop was accepted, False otherwise.

        BUG: This endpoint returns 401 with all available auth tokens.
        The /stop/{conversation_id} endpoint exists but requires host-level
        auth (not the API key or client identity token available to us).
        See: /home/workspace/Knowledge/bugs/zo-stop-endpoint-auth.md

        Once this is fixed, enable the code below and replace the message
        queuing in bot.py with true interrupt-then-redirect behavior.
        """
        # TODO: Uncomment once Zo API exposes stop with API key auth
        #
        # headers = {
        #     "Authorization": f"Bearer {self.api_key}",
        #     "Content-Type": "application/json",
        # }
        # timeout = aiohttp.ClientTimeout(total=30)
        # try:
        #     async with aiohttp.ClientSession(timeout=timeout) as session:
        #         async with session.post(
        #             f"{self.BASE_URL}/stop/{conversation_id}",
        #             headers=headers,
        #         ) as resp:
        #             if resp.status == 200:
        #                 logger.info(f"Stopped conversation {conversation_id}")
        #                 return True
        #             else:
        #                 error_text = await resp.text()
        #                 logger.warning(f"Failed to stop {conversation_id}: {resp.status} {error_text}")
        #                 return False
        # except Exception as e:
        #     logger.error(f"Error stopping conversation {conversation_id}: {e}")
        #     return False

        logger.debug(f"stop_conversation({conversation_id}) is a no-op — endpoint requires host auth")
        return False

    def generate_thread_title_simple(self, user_message: str) -> str:
        """
        Generate a clean thread title from the user message.
        Cleans Discord formatting, URLs, mentions, etc. similar to thread-it bot.
        """
        import re

        title = user_message.strip()
        if not title:
            return "New conversation"

        # Remove Discord mentions (<@123>, <@!123>, <#123>, <@&123>)
        title = re.sub(r'<[@#][!&]?\d+>', '', title)

        # Remove URLs
        title = re.sub(r'https?://\S+', '', title)

        # Remove Discord spoilers (||text||)
        title = re.sub(r'\|\|[^|]+\|\|', '', title)

        # Remove inline code (`code`)
        title = re.sub(r'`[^`]+`', '', title)

        # Remove code blocks (```code```)
        title = re.sub(r'```[\s\S]*?```', '', title)

        # Remove custom emoji (<:name:id> or <a:name:id>)
        title = re.sub(r'<a?:\w+:\d+>', '', title)

        # Clean up whitespace
        title = title.replace('\n', ' ').replace('\r', '')
        title = re.sub(r'\s+', ' ', title).strip()

        # Truncate to Discord's thread name limit (100 chars, but keep it readable)
        if len(title) > 80:
            title = title[:77] + "..."

        return title or "New conversation"

    def generate_thread_title(self, user_message: str, zo_response: str = None) -> str:
        """
        Generate thread title - just uses simple truncation.
        Smart naming via Claude is done separately in the bot via background task.
        """
        return self.generate_thread_title_simple(user_message)

    def chunk_response(self, text: str) -> list[str]:
        """
        Split a response into chunks that fit Discord's message limit.
        Applies Discord formatting fixes first, then splits at topic boundaries.
        """
        text = self.format_for_discord(text)

        if len(text) <= self.max_length:
            return [text]

        # Try topic-based splitting first
        sections = self._split_by_topics(text)

        # Now pack sections into chunks that fit the limit
        chunks = []
        current = ""

        for section in sections:
            if len(section) > self.max_length:
                if current:
                    chunks.append(current)
                    current = ""
                sub_chunks = self._split_long_section(section)
                if sub_chunks:
                    chunks.extend(sub_chunks[:-1])
                    current = sub_chunks[-1]
            elif len(current) + len(section) + 2 <= self.max_length:
                current = current + "\n\n" + section if current else section
            else:
                if current:
                    chunks.append(current)
                current = section

        if current:
            chunks.append(current)

        # Safety net: hard-split any chunk that still exceeds the limit
        safe_chunks = []
        for chunk in chunks:
            while len(chunk) > self.max_length:
                safe_chunks.append(chunk[:self.max_length])
                chunk = chunk[self.max_length:]
            if chunk:
                safe_chunks.append(chunk)

        # Add a blank-line spacer at the start of continuation chunks so
        # Discord's inter-message gap looks like a paragraph break.
        # Discord trims leading whitespace, but a zero-width space line works.
        for i in range(1, len(safe_chunks)):
            safe_chunks[i] = "\u200b\n" + safe_chunks[i]

        return safe_chunks

    def format_for_discord(self, text: str) -> str:
        """Transform markdown that doesn't render well in Discord."""
        import re

        # 1. Convert footnote references to inline links
        # Collect footnote definitions first
        footnotes = {}
        for match in re.finditer(r'^\[\^(\d+)\]:\s*(.+)$', text, re.MULTILINE):
            footnotes[match.group(1)] = match.group(2).strip()

        # Remove footnote definition lines
        text = re.sub(r'^\[\^(\d+)\]:\s*.+$\n?', '', text, flags=re.MULTILINE)

        # Replace [^n] references with inline links
        for num, url in footnotes.items():
            if url.startswith('http'):
                # Try to use domain as link text
                domain = re.sub(r'https?://(www\.)?', '', url).split('/')[0]
                text = text.replace(f'[^{num}]', f' ([{domain}]({url}))')
            else:
                text = text.replace(f'[^{num}]', f' ({url})')

        # 2. Convert markdown tables to code blocks
        table_pattern = re.compile(
            r'((?:^\|.+\|[ \t]*\n)*^\|.+\|[ \t]*(?:\n|$))',
            re.MULTILINE
        )
        def table_to_codeblock(match):
            table_text = match.group(1)
            lines = [l.strip() for l in table_text.strip().split('\n') if l.strip()]
            if len(lines) < 2:
                return table_text

            # Parse rows
            rows = []
            separator_idx = None
            for i, line in enumerate(lines):
                cells = [c.strip() for c in line.strip('|').split('|')]
                # Detect separator row (----, :---:, etc.)
                if all(re.match(r'^:?-+:?$', c.strip()) for c in cells if c.strip()):
                    separator_idx = i
                    continue
                rows.append(cells)

            if not rows:
                return table_text

            # Calculate column widths
            max_cols = max(len(r) for r in rows)
            col_widths = [0] * max_cols
            for row in rows:
                for j, cell in enumerate(row):
                    if j < max_cols:
                        col_widths[j] = max(col_widths[j], len(cell))

            total_width = sum(col_widths) + (max_cols - 1) * 2

            # Wide tables -> bullet list format (readable on mobile)
            if total_width > 40:
                header = rows[0] if rows else []
                out_lines = []
                for row in rows[1:]:
                    parts = []
                    for j, cell in enumerate(row):
                        if j < len(header) and header[j]:
                            parts.append(f"**{header[j]}**: {cell}")
                        else:
                            parts.append(cell)
                    out_lines.append('- ' + ' \u2014 '.join(parts))
                return '\n'.join(out_lines)

            # Narrow tables -> code block (looks good everywhere)
            out_lines = []
            for i, row in enumerate(rows):
                padded = []
                for j in range(max_cols):
                    cell = row[j] if j < len(row) else ''
                    padded.append(cell.ljust(col_widths[j]))
                out_lines.append('  '.join(padded))
                # Add separator after header
                if i == 0 and separator_idx is not None:
                    out_lines.append('  '.join('\u2500' * w for w in col_widths))

            return '```\n' + '\n'.join(out_lines) + '\n```'

        text = table_pattern.sub(table_to_codeblock, text)

        # 3. Remove horizontal rules
        text = re.sub(r'^[ \t]*[-*_]{3,}[ \t]*$\n?', '', text, flags=re.MULTILINE)

        # 4. Convert task lists to plain lists
        text = re.sub(r'^(\s*)- \[x\] ', r'\1- ✓ ', text, flags=re.MULTILINE)
        text = re.sub(r'^(\s*)- \[ \] ', r'\1- ', text, flags=re.MULTILINE)

        # 5. Suppress URL embeds by wrapping bare URLs in <>
        # Skip URLs already inside markdown links [text](url) or already wrapped <url>
        text = re.sub(
            r'(?<!\()(?<!<)(https?://[^\s>)\]]+)(?!\))',
            r'<\1>',
            text
        )

        # Clean up excess blank lines
        text = re.sub(r'\n{3,}', '\n\n', text)

        return text.strip()

    def _split_by_topics(self, text: str) -> list[str]:
        """Split text into topic sections at meaningful boundaries."""
        import re

        # Split on bold headers, markdown headers, horizontal rules, or numbered items after blank lines
        pattern = r'\n\n(?=\*\*[^*]+\*\*[:\s]|#{1,3}\s|\-{3,}|\d+\.\s)'
        parts = re.split(pattern, text)

        # Remove empty parts and strip
        sections = [p.strip() for p in parts if p.strip()]

        # If no topic splits found, fall back to paragraph splitting
        # but keep headings attached to the paragraph that follows them
        if len(sections) <= 1:
            paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
            sections = []
            for para in paragraphs:
                # If this paragraph is a heading, attach it to the next paragraph
                if re.match(r'^(?:\*\*[^*]+\*\*[:\s]?|#{1,3}\s)', para):
                    sections.append(para)
                elif sections and re.match(r'^(?:\*\*[^*]+\*\*[:\s]?|#{1,3}\s)', sections[-1]):
                    # Previous section was a bare heading — merge
                    sections[-1] = sections[-1] + "\n\n" + para
                else:
                    sections.append(para)

        return sections

    def _split_long_section(self, section: str) -> list[str]:
        """Split a section that's longer than max_length."""
        import re
        heading_re = re.compile(r'^(?:\*\*[^*]+\*\*[:\s]?|#{1,3}\s)')

        chunks = []
        paragraphs = section.split("\n\n")
        current = ""

        for para in paragraphs:
            if len(current) + len(para) + 2 <= self.max_length:
                current = current + "\n\n" + para if current else para
            else:
                if current:
                    # Don't strand a heading at the end of a chunk —
                    # move it to the next chunk with the upcoming content
                    lines = current.rsplit("\n\n", 1)
                    if len(lines) == 2 and heading_re.match(lines[1]):
                        chunks.append(lines[0])
                        current = lines[1]
                    else:
                        chunks.append(current)
                        current = ""
                if len(para) > self.max_length:
                    # Split by words
                    words = para.split()
                    for word in words:
                        if len(current) + len(word) + 1 <= self.max_length:
                            current = current + " " + word if current else word
                        else:
                            if current:
                                chunks.append(current)
                            current = word
                elif current and len(current) + len(para) + 2 > self.max_length:
                    chunks.append(current)
                    current = para
                else:
                    current = current + "\n\n" + para if current else para

        if current:
            chunks.append(current)
        return chunks
