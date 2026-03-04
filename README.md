# zo-discord

A Discord bot for [Zo Computer](https://zo.computer) that makes Discord a first-class Zo interface, with thread management, streaming/queuing, scheduled agent notifications, per-channel models/personas, and more.

## Features

### Thread Management
- **Auto-threading** — Messages in channels create threads with Zo responses. Each thread maps to a Zo conversation with full session persistence.
- **Thread renaming** — Zo is prompted to rename each thread via the CLI, so threads get descriptive titles automatically.
- **Auto-archive override** — Keeps threads open until you manually archive them with a :white_check_mark: reaction. Toggle with `/auto-archive`. Set :white_check_mark: as your double-tap reaction on mobile for quick archiving.

### Interactive Conversations
- **Typing indicator** — Shows when Zo is actively processing a message.
- **Streaming thoughts** — See Zo's intermediate thinking in real-time, or set quiet mode to only see the final response. Toggle with `/thinking`.
- **Message queuing** — Send multiple messages while Zo is thinking. They're batched and delivered when the current turn finishes.
- **Reply context** — Reply to a specific message in a thread and Zo sees which message you're responding to.
- **File attachments** — Attach files to your messages and Zo will receive them.
- **Discord formatting** — Markdown is automatically reformatted for Discord: tables become bullet outlines or code blocks, footnotes become inline links, link embeds are suppressed, and task lists are converted to plain lists.

### Configuration
- **Models** — Set a default model globally or per-channel with `/model`. Define aliases to make model IDs easier to remember, and prefix a new conversation with `/alias` (e.g. `/opus`) to override the model for that thread.
- **Personas** — Set a default persona globally or per-channel with `/persona`. Define aliases and prefix a new conversation with `@alias` (e.g. `@pirate`) to override for that thread.
- **Channel instructions & memory** — Set custom instructions and memory file paths per-channel — they're injected into every conversation. Channel topic and pinned messages also provide context.
- **Allowed users** — Restrict bot access to specific Discord users, or allow all users. Manage with `/allowed-users`.
- **Slash commands** — `/help`, `/model`, `/persona`, `/thinking`, `/auto-archive`, `/instructions`, `/memory`, `/allowed-users`, `/tips`, `/link`, `/cli`

### Scheduled Agents
- **Notifications** — Scheduled Zo agents can post results to new Discord threads with session continuity, so you can reply and continue the conversation. See `skill/scheduled-agent-example.md`.
- **Interactive buttons** — Agents can present choices via buttons; the user's selection is injected back into the conversation.
- **File attachments** — Agents can send files back via the HTTP API.
- **Rich embeds** — Agents can post structured embeds with fields, colors, and footers.
- **CLI & HTTP API** — Full programmatic access for agent-driven automation. See `skill/SKILL.md`.

## Requirements

- A [Zo Computer](https://zo.computer) account
- A Discord server you administer
- Python 3.10+

## Setup

### 1. Create a Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application**, name it (e.g., "Zo")
3. Go to **Bot** tab:
   - Click **Reset Token** and copy the token
   - Enable **all Privileged Gateway Intents** (Presence, Server Members, Message Content)
4. Go to **OAuth2** tab:
   - Under **Scopes**, select `bot` and `applications.commands`
   - Under **Bot Permissions**, select **Administrator** (the bot needs broad permissions for thread management, reactions, attachments, and slash commands)
   - Copy the generated URL and open it to invite the bot to your server

### 2. Get Your IDs

- **Guild ID**: Right-click your server name → Copy Server ID (enable Developer Mode in Discord settings if you don't see this)
- **User ID**: Right-click your username → Copy User ID

### 3. Configure Zo Secrets

Add these as Zo secrets (Settings → Developers → Secrets):

- `DISCORD_BOT_TOKEN` — Your bot token from step 1
- `DISCORD_ZO_API_KEY` — Your Zo API key (Settings → Developers → API Keys)

### 4. Clone and Install

```bash
git clone https://github.com/JackALaing/zo-discord.git
cd zo-discord
pip install .
```

### 5. Configure the Bot

```bash
cp config/config.example.json config/config.json
```

Edit `config/config.json` with your IDs from step 2:

```json
{
  "guild_id": "YOUR_DISCORD_SERVER_ID",
  "allowed_users": ["YOUR_DISCORD_USER_ID"],
  "model": null,
  "model_aliases": {},
  "default_persona": null,
  "persona_aliases": {},
  "notification_port": 8787,
  "max_message_length": 1900,
  "data_dir": "discord_data",
  "thinking_mode": "streaming",
  "auto_archive_override": true
}
```

| Field | Description |
| --- | --- |
| `guild_id` | Your Discord server ID |
| `allowed_users` | Discord user IDs allowed to interact with the bot. Empty array = all users allowed |
| `model` | Zo model override (e.g., `"claude-sonnet-4-5"`). `null` uses your Zo account's default |
| `model_aliases` | Map of short names to model IDs for easy switching (see [Per-Thread Model Override](#per-thread-model-override)) |
| `default_persona` | Zo persona ID override. `null` uses your Zo account's default |
| `persona_aliases` | Map of short names to persona IDs (see [Per-Thread Persona Override](#per-thread-persona-override)) |
| `notification_port` | Port for the bot's internal HTTP API, used by agents and the CLI |
| `max_message_length` | Max characters per Discord message before chunking. Don't change unless Discord change their API limits |
| `data_dir` | Path for channel data and attachments. Defaults to `discord_data/` in the bot directory |
| `thinking_mode` | `"streaming"` shows Zo's intermediate thinking; `"quiet"` shows only final responses |
| `auto_archive_override` | `true` prevents Discord from auto-archiving threads; `false` uses channel defaults |

### 6. Register as a Zo Service

Register the bot as a Zo service so it auto-starts and restarts on failure:

```
Register zo-discord as a service with entrypoint start.sh in /path/to/zo-discord
```

The `start.sh` script loads your Zo secrets and starts the bot. The Zo service system handles auto-restart.

### 7. Install the CLI

The `zo-discord` CLI lets Zo interact with Discord — renaming threads, sending notifications, presenting buttons, and more. See [CLI Reference](#cli-reference) for the full command list.

```bash
ln -sf "$(pwd)/skill/scripts/discord-cli.sh" /usr/local/bin/zo-discord
```

### 8. Install the Skill

Copy the skill to your Zo skills directory so Zo knows how to use the Discord API:

```bash
cp -r skill/ /home/workspace/Skills/zo-discord/
```

## How It Works

1. User sends a message in a Discord channel
2. Bot calls the Zo API with the message + channel context (instructions, memory paths, pins, Discord tool instructions)
3. Bot creates a thread with Zo's response
4. Thread-to-conversation mapping is stored in SQLite
5. Follow-up messages in the thread continue the same Zo session
6. Scheduled Zo agents can post to Discord via the CLI or HTTP API, preserving session continuity

## CLI Reference

The `zo-discord` CLI auto-detects the conversation ID from the workspace path. No thread ID needed.

```
zo-discord rename "Title"                              — Rename the thread
zo-discord error                                       — Set thread status to error
zo-discord notify "Title" "content" --channel-name general  — Post to a new thread
zo-discord notify "Title" --file /tmp/out.md --channel-name pulse  — Post file contents
zo-discord buttons "Prompt?" "Yes:success" "No:danger" — Send interactive buttons
zo-discord buttons "Prompt?" --preset yes_no           — Use a button preset
zo-discord new-thread "Title" "prompt"                 — Spawn a new thread
```

See `skill/SKILL.md` for full HTTP API documentation.

## Agent Notifications

Scheduled Zo agents can spawn new Discord threads linked to their active session, so the user can reply and continue the same conversation with full context of the agent's work. See `skill/scheduled-agent-example.md` for a complete example.

The key pattern: the agent does its work, builds up conversation context, then posts results to a new Discord thread via `zo-discord notify`. The thread is linked to the agent's Zo session, so replies continue seamlessly.

## Slash Commands

All settings changed via slash commands are persisted to `config.json` and survive bot restarts.

| Command | Description |
| --- | --- |
| `/help` | Bot info and command list |
| `/tips` | Tips and tricks |
| `/link` | Open conversation in Zo |
| `/model` | View/change default model |
| `/persona` | View/change default persona |
| `/thinking` | Toggle thinking mode (streaming/quiet) |
| `/auto-archive` | Configure auto-archive behavior |
| `/instructions` | View channel instructions |
| `/memory` | View channel memory paths |
| `/allowed-users` | Manage allowed users |
| `/cli` | Show CLI commands |

## Per-Thread Model Override

Start your first message in a channel with `/alias` to use a different model for that thread. The alias is stripped from the message before sending to Zo.

First, set up aliases in `config/config.json`:

```json
{
  "model_aliases": {
    "cc-opus": "byok:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "cc-sonnet": "byok:yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy",
    "sonnet": "claude-sonnet-4-5"
  }
}
```

Then use them in Discord:

```
/cc-opus explain this error in my code
/sonnet what's the weather like
```

The `/model` slash command shows all configured aliases. To find your model IDs for Claude Code or Codex, start a conversation using that model, then ask Zo: "What is my Claude Code Opus model ID?" — Zo reads the model ID from the active session's system prompt.

## Per-Thread Persona Override

Start your first message in a channel with `@alias` to use a different persona for that thread. The alias is stripped from the message before sending to Zo.

First, set up aliases in `config/config.json`:

```json
{
  "persona_aliases": {
    "pirate": "per_xxxxxxxxxxxxxxx",
    "formal": "per_yyyyyyyyyyyyyyy"
  }
}
```

Then use them in Discord:

```
@pirate tell me about the weather
@formal draft an email to the team
```

The `/persona` slash command shows all configured aliases. To find your persona IDs, start a conversation with the persona active, then ask Zo to set the alias — Zo will find the persona ID more easily when it's the one in use.

## Thread Management

- **Auto-archive prevention**: When enabled, a background routine bumps thread timers every 6 hours and auto-archives are reversed in real-time. Toggle with `/auto-archive`.
- **Archive a thread**: React with :white_check_mark: on any bot message. The thread is removed from the watch list and archived. Set :white_check_mark: as your double-tap reaction on mobile for quick archiving.
- **Un-archive**: Manually un-archiving a thread or replying to it adds it back to the watch list.

## Discord Formatting

The bot automatically reformats markdown before sending to Discord:

- **Tables** — Wide tables become bullet outlines with bold headers; narrow tables become monospaced code blocks
- **Footnotes** — `[^1]` references and definitions are converted to inline masked links
- **Link embeds** — Bare URLs are wrapped in `<>` to suppress Discord's embed previews
- **Horizontal rules** — `---`, `***` are removed (Discord doesn't render them)
- **Task lists** — `- [ ]` and `- [x]` become plain lists with checkmarks

Long messages are automatically split at topic boundaries so you can reply to individual sections.

## Project Structure

```
zo-discord/
├── zo_discord/             # Python package
│   ├── __init__.py
│   ├── bot.py              # Main bot — event handlers, HTTP API, message processing
│   ├── zo_client.py        # Zo API client — streaming, retries, title generation
│   ├── db.py               # SQLite database — thread mappings, channel config
│   ├── commands.py         # Slash commands and UI components
│   └── utils.py            # Pure utility functions (status prefixes)
├── config/
│   ├── config.json         # Your config (gitignored)
│   └── config.example.json # Config template
├── skill/
│   ├── SKILL.md                       # Full CLI and HTTP API documentation
│   ├── scheduled-agent-example.md     # Example scheduled agent
│   └── scripts/
│       └── discord-cli.sh             # CLI script
├── tests/
│   └── test_formatting.py  # Tests for formatting, chunking, and title generation
├── start.sh                # Service entrypoint (sources secrets, runs bot)
├── pyproject.toml          # Package metadata and dependencies
├── LICENSE
└── README.md
```

## License

MIT
