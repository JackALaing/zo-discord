---
name: zo-discord
description: Send notifications and interact with Discord — threads, embeds, buttons, files, reactions, and channel config. Use when a scheduled agent needs to deliver results to Discord, when you need to send interactive UI elements, or when managing per-channel context.
compatibility: Created for Zo Computer
metadata:
  author: JackALaing
---
# Discord Notify & CLI

Send notifications and control Discord interactions via the bot's HTTP API at `localhost:8787`.

## CLI (recommended)

The `zo-discord` CLI routes by conversation ID. Prefer `--conv-id <id>` when available. Env-var fallback exists, but explicit `--conv-id` is the reliable path. No thread ID needed.

```bash
zo-discord <command> [args]
```

| Command | Description |
| --- | --- |
| `zo-discord rename "New title"` | Rename the thread (queued if thread not yet created) |
| `zo-discord error` | Set status to error |
| `zo-discord notify "Title" "content" --channel-name general` | Post short content to a new thread |
| `zo-discord notify "Title" --file /tmp/out.md --channel-name pulse` | Post file contents to a new thread |
| `zo-discord buttons "Prompt?" "Label:style" ...` | Send interactive buttons (user's click is injected back) |
| `zo-discord buttons "Prompt?" --preset yes_no` | Send preset buttons (presets: `yes_no`, `approve_reject`) |
| `zo-discord files /path/to/file "caption"` | Send a file attachment to the thread (max 25MB) |
| `zo-discord new-thread "Title" "prompt" --channel-name general` | Spawn a new thread with a fresh Zo session |

Channel targeting: use `--channel-name <name>` (e.g. `general`, `pulse`) or `--channel <id>`. For `new-thread`, channel targeting is required. Prefer `--channel-name` — no need to look up IDs.

Your normal replies are automatically sent to the Discord thread — no need to use a send command.

For scheduled agents posting long-form results, use `--file` to avoid shell escaping issues:
```bash
# Write results to a temp file, then notify
zo-discord notify "Daily Briefing — Feb 14" --file /tmp/briefing.md --channel-name general
```

## Buttons (via CLI)

```bash
zo-discord buttons "Do you approve?" "Approve:success" "Reject:danger"
zo-discord buttons "Do you approve?" --preset yes_no
```

## Notifications (create a thread — HTTP API)

```bash
curl -sS -X POST "http://localhost:8787/notify" -H "Content-Type: application/json" \
  -d '{"channel_name": "general", "title": "Your Title", "content": "Message body", "conversation_id": "con_xxx"}'
```

| Parameter | Required | Description |
| --- | --- | --- |
| `channel_name` | Yes* | Channel name (e.g. `general`, `pulse`) |
| `channel_id` | Yes* | Discord channel ID (alternative to `channel_name`) |
| `title` | Yes | Thread title |
| `content` | Yes | Message content (supports markdown) |
| `conversation_id` | No | Conversation ID for session continuity |

*Provide either `channel_name` or `channel_id`.

## HTTP API Reference

All endpoints are at `http://localhost:8787`. Use curl from within Zo. The CLI above is preferred for common operations.

### Conversation-based actions (auto-resolves thread)

```bash
curl -sS -X POST "http://localhost:8787/conversations/CONV_ID/action" \
  -H "Content-Type: application/json" \
  -d '{"action": "rename", "name": "New title"}'
```

Actions: `rename` (requires `name`), `error`, `complete` (archives thread)

### Buttons (interactive, injects choice into conversation)

```bash
curl -sS -X POST "http://localhost:8787/buttons" -H "Content-Type: application/json" \
  -d '{"thread_id": "THREAD_ID", "prompt": "Do you approve?", "buttons": [{"label": "Approve", "id": "approve", "style": "success"}, {"label": "Reject", "id": "reject", "style": "danger"}]}'
```

Presets: `"preset": "approve_reject"` or `"preset": "yes_no"`

Button styles: `primary`, `secondary`, `success`, `danger`

When the user presses a button, their choice is automatically injected into the thread's Zo conversation.

### File Attachments

```bash
curl -sS -X POST "http://localhost:8787/files" -H "Content-Type: application/json" \
  -d '{"thread_id": "THREAD_ID", "file_path": "/home/workspace/path/to/file.pdf", "message": "Optional text"}'
```

Max 25MB per file.

### Rich Embeds

```bash
curl -sS -X POST "http://localhost:8787/embeds" -H "Content-Type: application/json" \
  -d '{"thread_id": "THREAD_ID", "title": "Title", "description": "Body", "color": "blue", "fields": [{"name": "Key", "value": "Value", "inline": true}], "footer": "Footer text"}'
```

Colors: `blue`, `green`, `red`, `yellow`, `purple`, `orange`, `gray`

### Send Message

```bash
curl -sS -X POST "http://localhost:8787/messages/send" -H "Content-Type: application/json" \
  -d '{"channel_id": "CHANNEL_OR_THREAD_ID", "content": "Hello"}'
```

### Edit / Delete / React

```bash
# Edit
curl -sS -X POST "http://localhost:8787/messages/edit" -H "Content-Type: application/json" \
  -d '{"channel_id": "CHANNEL_ID", "message_id": "MSG_ID", "content": "Updated text"}'

# Delete
curl -sS -X DELETE "http://localhost:8787/messages" -H "Content-Type: application/json" \
  -d '{"channel_id": "CHANNEL_ID", "message_id": "MSG_ID"}'

# React
curl -sS -X POST "http://localhost:8787/react" -H "Content-Type: application/json" \
  -d '{"channel_id": "CHANNEL_ID", "message_id": "MSG_ID", "emoji": "\u2705"}'
```

### Channel Config

Set per-channel instructions and memory paths:

```bash
curl -sS -X POST "http://localhost:8787/channels/CHANNEL_ID/config" -H "Content-Type: application/json" \
  -d '{"instructions": "Focus on project X", "memory_paths": ["Knowledge/memory/projects/x.md", "Knowledge/memory/channels/general.md"]}'
```

Get config: `GET /channels/CHANNEL_ID/config`
Delete config: `DELETE /channels/CHANNEL_ID/config`

Fields:
- `instructions` (text) — injected into every thread's context as "Channel Instructions". Replaces the channel topic as context when set.
- `memory_paths` (array of workspace-relative paths) — each path is passed to Zo as a file to read at the start of every conversation in that channel. zo-discord has no built-in memory system — these paths should point to files maintained by an external memory system.
- `model` (string) — model ID override for this channel. Overrides the global default.
- `persona_id` (string) — persona ID override for this channel. Overrides the global default.
- `backend` (`zo` or `hermes`) — route the channel to Zo or the local Hermes bridge. On Hermes, Discord context and referenced file paths are sent as request-time overlay context rather than merged into the user's message text.
- `buffer_seconds` (number) — seconds to wait after the last message before processing (0 = disabled, null = use global default). See README for details on typing detection and behavior.

### Health Check

```bash
curl -sS "http://localhost:8787/health"
```

## Status Visibility

Thread titles are prefixed with status emojis only for exceptional states:
- ❌ Errored — something failed

No emoji = normal state (new, working, or has a response). The typing indicator shows when Zo is actively processing.

## Thread Completion

React with ✅ on any Zo message in a thread to archive it (removes from sidebar).

## Spawn New Thread

Start a separate conversation in a new Discord thread from within an existing thread:

```bash
zo-discord new-thread "Thread Title" "Context or question for the new thread" --channel-name general
zo-discord new-thread "Title" "Prompt" --channel-name pulse
zo-discord new-thread "Title" "Prompt" --channel CHANNEL_ID
zo-discord new-thread "Title" --prompt-file /tmp/prompt.md --channel-name hermes
cat /tmp/prompt.md | zo-discord new-thread "Title" --channel-name hermes
```

The new thread gets its own Zo session with full channel context (instructions, memory paths). The prompt is sent as the first message to the new Zo session, and Zo's response appears in the thread. For `new-thread`, always pass an explicit `--channel-name` or `--channel`; there is no implicit default.

## Message Chunking

Messages over 2000 characters are automatically split at topic boundaries: `**bold titles**`, `---` dividers, and numbered list boundaries. Each chunk is sent as a separate message so you can reply to individual sections.

## Discord Formatting

The bot automatically reformats markdown before sending to Discord:
- **Tables** → wide tables become bullet outlines with bold headers; narrow tables become monospaced code blocks
- **Footnotes** (`[^1]` + definitions) → inline masked links `[domain](url)`
- **Horizontal rules** (`---`, `***`) → removed
- **Task lists** (`- [ ]`, `- [x]`) → plain lists (`-`, `- ✓`)
This runs before chunking, so no special handling is needed by the agent.

## How the CLI Works

The `zo-discord` CLI accepts the conversation ID from:
1. An explicit `--conv-id <id>` flag (parsed first, before the command word)
2. The `CONVERSATION_ID` environment variable (falls back to `ZO_CONVERSATION_ID`) as a fallback only

It calls `POST /conversations/{conv_id}/action`, which resolves the Discord thread ID internally. No thread ID needed.

Install with `ln -sf /path/to/zo-discord/skill/scripts/discord-cli.sh /usr/local/bin/zo-discord` for global access (see README).

## User Customization

Users can customize zo-discord via Discord slash commands — model, persona, message buffering, thinking mode, auto-archive, channel instructions, memory paths, and allowed users. Tell users to type `/help` in Discord to see all available commands.

## How the Bot Works

1. Messages in a channel call the Zo API first, then create a thread with Zo's response
2. Thread <-> conversation_id mapping is stored in SQLite
3. If Zo calls `zo-discord rename` during its response, the title is queued and used when the thread is created
4. If no rename is queued, the bot generates a fallback title from the user's message
5. Follow-up messages in threads continue the same Zo session
6. Channel config (instructions, memory) is injected into every thread's context
7. Buttons inject user choices back into the Zo conversation
8. File attachments from user messages are saved and paths passed to Zo
9. First message gets full context (source, channel instructions, memory paths, channel topic as fallback, pinned messages, thread naming instructions, tools)
10. Follow-up messages get compact context (thread name, reply reminder, rename hints, skill link)
11. Agent replies normally — responses are automatically piped into the Discord thread

When the backend is Hermes, that context is delivered via the `ephemeral_system_prompt` overlay and any referenced files are listed there too. The raw `input` field stays equal to the user's actual message.
