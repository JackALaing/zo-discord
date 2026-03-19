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

from zo_discord.hermes import get_request_config, get_backend_label, handle_session_id_change, is_hermes, HERMES_URL

logger = logging.getLogger(__name__)

HERMES_HEALTH_TIMEOUT = 2  # seconds


async def check_hermes_health() -> dict | None:
    """Check zo-hermes /health endpoint. Returns health dict or None if unreachable."""
    try:
        timeout = aiohttp.ClientTimeout(total=HERMES_HEALTH_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{HERMES_URL}/health") as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.warning(f"Hermes health check returned {resp.status}")
                return None
    except Exception as e:
        logger.warning(f"Hermes health check failed: {e}")
        return None


@dataclass
class StreamResult:
    """Result from ask_stream with diagnostic info for retry decisions."""
    output: str
    conv_id: str
    interrupted: bool  # stream broke before End event
    received_events: bool  # got any SSE events at all
    error_message: str = ""  # SSEErrorEvent message if stream errored

from zo_discord import PROJECT_ROOT

CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"

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


_config_cache = None
_config_cache_time = 0.0
_CONFIG_TTL = 5.0  # seconds


def load_config() -> dict:
    """Load bot configuration, cached for 5 seconds."""
    global _config_cache, _config_cache_time
    now = time.monotonic()
    if _config_cache is not None and (now - _config_cache_time) < _CONFIG_TTL:
        return _config_cache
    with open(CONFIG_PATH) as f:
        _config_cache = json.load(f)
    _config_cache_time = now
    return _config_cache


class ZoClient:
    """Async client for the Zo API (or Hermes via zo-hermes)."""

    BASE_URL = "https://api.zo.computer"

    def __init__(self):
        self.api_key = os.environ.get("DISCORD_ZO_API_KEY")
        if not self.api_key:
            raise ValueError("DISCORD_ZO_API_KEY environment variable not set")

        config = load_config()
        self.model = config.get("model")
        self.max_length = config.get("max_message_length", 1900)
        self.backend = config.get("backend", "zo")  # "zo" or "hermes"

    async def ask_stream(
        self,
        input_text: str,
        conversation_id: str = None,
        context: str = None,
        file_paths: list[str] = None,
        on_thinking: Callable[[str], Awaitable[None]] = None,
        on_conv_id: Callable[[str], Awaitable[None]] = None,
        on_clarify: Callable[[str, list, str], Awaitable[str]] = None,
        model_name: str = None,
        persona_id: str = None,
        backend: str = None,
        reasoning_effort: str = None,
        max_iterations: int = None,
        skip_memory: bool = False,
        skip_context: bool = False,
        enabled_toolsets: list[str] = None,
        disabled_toolsets: list[str] = None,
    ) -> StreamResult:
        """
        Send a message to Zo or Hermes via streaming endpoint.

        Args:
            input_text: The user's message
            conversation_id: Optional existing conversation ID
            context: Optional context string appended after the user message
            file_paths: Optional list of file paths referenced in the context
            on_thinking: Async callback for thinking previews (receives text to post)
            on_conv_id: Async callback when conversation ID is received
            backend: Override backend ("zo" or "hermes"), defaults to self.backend

        Returns:
            StreamResult with output, conv_id, and diagnostic info
        """
        full_input = input_text
        if context:
            full_input = f"{input_text}\n\n{context}"
        if file_paths:
            paths_str = "\n".join(f"- `{p}`" for p in file_paths)
            full_input = f"{full_input}\n\n## Referenced Files\n{paths_str}"

        effective_model = model_name or self.model
        payload = {
            "input": full_input,
            "stream": True,
        }

        if effective_model:
            payload["model_name"] = effective_model
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if persona_id:
            payload["persona_id"] = persona_id
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        if max_iterations:
            payload["max_iterations"] = max_iterations
        if skip_memory:
            payload["skip_memory"] = True
        if skip_context:
            payload["skip_context"] = True
        if enabled_toolsets:
            payload["enabled_toolsets"] = enabled_toolsets
        if disabled_toolsets:
            payload["disabled_toolsets"] = disabled_toolsets

        api_url, headers = get_request_config(self.api_key, backend, self.backend)
        backend_label = get_backend_label(backend, self.backend)

        # Pre-flight health check for Hermes backend
        if is_hermes(backend, self.backend):
            health = await check_hermes_health()
            if health is None:
                logger.error("Hermes is unreachable — skipping /ask call")
                return StreamResult(
                    output="",
                    conv_id=conversation_id or "",
                    interrupted=False,
                    received_events=False,
                    error_message="zo-hermes is unreachable. The service may be down or restarting.",
                )
            status = health.get("status", "unknown")
            if status != "ok":
                active = health.get("active_sessions", "?")
                logger.warning(f"Hermes health status: {status}, active_sessions: {active}")

        hermes_extras = {k: v for k, v in payload.items() if k in ("reasoning_effort", "max_iterations", "skip_memory", "skip_context", "enabled_toolsets", "disabled_toolsets")}
        logger.info(f"Sending to {backend_label} API - model_name: {payload.get('model_name')}, persona_id: {payload.get('persona_id')}, conv_id: {payload.get('conversation_id', 'new')}" + (f", hermes_params: {hermes_extras}" if hermes_extras else ""))

        timeout = aiohttp.ClientTimeout(total=1800)
        conv_id = conversation_id or ""
        final_output = ""
        stream_interrupted = False
        received_any_events = False
        sse_error_message = ""

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
                        api_url,
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
                        in_thinking_part = False
                        thinking_buffer = ""
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
                                        elif part_kind == "thinking":
                                            # Thinking/reasoning part — accumulate deltas,
                                            # flush on PartEndEvent. Zo streams thinking as
                                            # small deltas (first token in PartStart, rest
                                            # in PartDelta). Hermes sends full text in
                                            # PartStart with no deltas.
                                            in_thinking_part = True
                                            thinking_buffer = part.get("content", "")
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
                                        delta_kind = delta.get("part_delta_kind", "")
                                        if delta_kind == "text":
                                            text = delta.get("content_delta", "")
                                            if in_text_part:
                                                text_buffer += text
                                        elif delta_kind == "thinking":
                                            thinking_buffer += delta.get("content_delta", "")

                                    elif current_event_type == "PartEndEvent":
                                        if in_thinking_part and on_thinking and thinking_buffer.strip():
                                            await on_thinking(thinking_buffer.strip())
                                            thinking_buffer = ""
                                        in_thinking_part = False
                                        in_text_part = False

                                    elif current_event_type == "ClarifyEvent":
                                        if on_clarify:
                                            clarify_question = event.get("question", "")
                                            clarify_choices = event.get("choices")
                                            clarify_session = event.get("session_id", conv_id)
                                            try:
                                                user_answer = await on_clarify(
                                                    clarify_question, clarify_choices, clarify_session,
                                                )
                                                # Send response back to zo-hermes
                                                async with aiohttp.ClientSession() as clarify_http:
                                                    await clarify_http.post(
                                                        "http://127.0.0.1:8788/clarify-response",
                                                        json={
                                                            "session_id": clarify_session,
                                                            "response": user_answer,
                                                        },
                                                        timeout=aiohttp.ClientTimeout(total=10),
                                                    )
                                            except Exception as e:
                                                logger.error(f"Clarify callback failed: {e}")

                                    elif current_event_type == "SSEErrorEvent":
                                        error_msg = event.get("message", "")
                                        logger.warning(f"SSE error event for conv {conv_id}: {error_msg}")
                                        sse_error_message = error_msg
                                        stream_interrupted = True
                                        break

                                    elif current_event_type == "End":
                                        end_data = event.get("data", {})
                                        final_output = end_data.get("output", "")
                                        new_conv = handle_session_id_change(end_data, conv_id)
                                        if new_conv:
                                            conv_id = new_conv
                                            if on_conv_id:
                                                await on_conv_id(conv_id)
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

        # Fallback: if End event had empty output but we accumulated streamed text,
        # use the streamed text. This can happen when zo-hermes streams deltas but
        # the final_response field is empty.
        if not final_output and text_buffer.strip():
            logger.info(f"Using streamed text_buffer as output for conv {conv_id} (End event output was empty)")
            final_output = text_buffer

        return StreamResult(
            output=final_output,
            conv_id=conv_id,
            interrupted=stream_interrupted,
            received_events=received_any_events,
            error_message=sse_error_message,
        )

    def generate_thread_title_simple(self, user_message: str) -> str:
        """
        Generate a clean thread title from the user message.
        Cleans Discord formatting, URLs, mentions, etc. similar to thread-it bot.
        """
        title = user_message.strip()
        if not title:
            return "New conversation"

        # Remove Discord mentions
        title = re.sub(r'<[@#][!&]?\d+>', '', title)

        # Remove URLs
        title = re.sub(r'https?://\S+', '', title)

        # Remove Discord spoilers (||text||)
        title = re.sub(r'\|\|[^|]+\|\|', '', title)

        # Remove code blocks before inline code (inline code regex would eat inner backticks first)
        title = re.sub(r'```[\s\S]*?```', '', title)

        # Remove inline code (`code`)
        title = re.sub(r'`[^`]+`', '', title)

        # Remove custom emoji (<:name:id> or <a:name:id>)
        title = re.sub(r'<a?:\w+:\d+>', '', title)

        # Clean up whitespace
        title = title.replace('\n', ' ').replace('\r', '')
        title = re.sub(r'\s+', ' ', title).strip()

        # Truncate to Discord's thread name limit (100 chars, but keep it readable)
        if len(title) > 80:
            title = title[:77] + "..."

        return title or "New conversation"

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

        # Fix code blocks that got split across chunks
        safe_chunks = self._fix_code_block_fences(safe_chunks)

        # Add a blank-line spacer at the start of continuation chunks so
        # Discord's inter-message gap looks like a paragraph break.
        # Discord trims leading whitespace, but a zero-width space line works.
        for i in range(1, len(safe_chunks)):
            safe_chunks[i] = "\u200b\n" + safe_chunks[i]

        return safe_chunks

    def format_for_discord(self, text: str) -> str:
        """Transform markdown that doesn't render well in Discord."""
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

            # Wide tables -> structured list (readable on mobile)
            if total_width > 40:
                header = rows[0] if rows else []
                out_lines = []
                for row in rows[1:]:
                    title = row[0] if row else ''
                    out_lines.append(f'**{title}**')
                    for j, cell in enumerate(row[1:], 1):
                        if j < len(header) and header[j]:
                            out_lines.append(f'- {header[j]}: {cell}')
                        else:
                            out_lines.append(f'- {cell}')
                    out_lines.append('')
                return '\n'.join(out_lines).rstrip()

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

        # 5. Collapse [url](url) links where the text IS a URL — Discord won't render these as masked links
        text = re.sub(
            r'\[(https?://[^\]]+)\]\(https?://[^)]+\)',
            r'\1',
            text
        )

        # 6. Suppress URL embeds by wrapping bare URLs in <>
        # Skip URLs already inside markdown links [text](url) or already wrapped <url>
        text = re.sub(
            r'(?<![(\[<])(https?://[^\s>)\]]+)(?!\))',
            r'<\1>',
            text
        )

        # Clean up excess blank lines
        text = re.sub(r'\n{3,}', '\n\n', text)

        return text.strip()

    def _split_by_topics(self, text: str) -> list[str]:
        """Split text into topic sections at meaningful boundaries."""
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

    def _fix_code_block_fences(self, chunks: list[str]) -> list[str]:
        """Close/reopen code blocks that were split across chunks."""
        fence_re = re.compile(r'^(`{3,})(\w*)', re.MULTILINE)

        in_code = False
        lang = ""
        fence_char_count = 3

        fixed = []
        for chunk in chunks:
            if in_code:
                chunk = f"```{lang}\n" + chunk

            fences = fence_re.findall(chunk)
            for ticks, fence_lang in fences:
                if not in_code:
                    in_code = True
                    lang = fence_lang
                    fence_char_count = len(ticks)
                else:
                    if len(ticks) >= fence_char_count:
                        in_code = False
                        lang = ""

            if in_code:
                chunk = chunk + "\n```"

            fixed.append(chunk)

        return fixed
