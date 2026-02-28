# zo-discord

A Discord bot for [Zo Computer](https://zo.computer) that turns your Discord server into a conversational interface with Zo. Every message creates a threaded conversation with persistent context, and Zo agents can deliver results back to Discord.

## Features

- **Auto-threading** — Messages in channels create threads with Zo responses. Each thread maps to a Zo conversation with full session persistence.
- **Message queuing** — Send multiple messages while Zo is thinking. They're batched and delivered when the current turn finishes.
- **Channel context** — Per-channel instructions and memory paths are injected into every conversation. Channel topic and pinned messages provide additional context.
- **Thread management** — Auto-archive prevention keeps threads open. Archive with a :white_check_mark: reaction on any bot message.
- **Agent notifications** — Scheduled Zo agents can post results to Discord threads with session continuity, so you can reply and continue the conversation.
- **Slash commands** — `/help`, `/model`, `/thinking`, `/auto-archive`, `/instructions`, `/memory`, `/allowed-users`, `/tips`, `/link`, `/cli`
- **Interactive buttons** — Agents can present choices via buttons; the user's selection is injected back into the conversation.
- **File attachments** — Send files to Zo by attaching them to messages. Zo agents can send files back via the HTTP API.
- **Rich embeds** — Agents can post structured embeds with fields, colors, and footers.
- **Reply context** — Reply to a specific message in a thread and Zo sees which message you're responding to.

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
   - Enable **Message Content Intent** under Privileged Gateway Intents
   - Enable **Server Members Intent** under Privileged Gateway Intents
4. Go to **OAuth2** tab:
   - Under **Scopes**, select `bot` and `applications.commands`
   - Under **Bot Permissions**, select: Send Messages, Create Public Threads, Send Messages in Threads, Manage Threads, Read Message History, Add Reactions, Attach Files, Use Slash Commands
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
git clone https://github.com/jacklaing/zo-discord.git
cd zo-discord
pip install -r requirements.txt
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
  "notification_port": 8787,
  "max_message_length": 1900,
  "thinking_mode": "streaming",
  "auto_archive_override": true
}
```

| Field | Description |
| --- | --- |
| `guild_id` | Your Discord server ID |
| `allowed_users` | Discord user IDs allowed to interact with the bot. Empty array = all users allowed |
| `model` | Zo model override (e.g., `"claude-sonnet-4-5"`). `null` uses your Zo account's default |
| `notification_port` | Port for the bot's internal HTTP API, used by agents and the CLI |
| `max_message_length` | Max characters per Discord message before chunking |
| `data_dir` | Path for channel data and attachments. Defaults to `discord_data/` in the bot directory |
| `thinking_mode` | `"streaming"` shows Zo's intermediate thinking; `"quiet"` shows only final responses |
| `auto_archive_override` | `true` prevents Discord from auto-archiving threads; `false` uses channel defaults |

### 6. Register as a Zo Service

Register the bot as a Zo service so it auto-starts and restarts on failure:

```
Register zo-discord as a service with entrypoint start.sh in /path/to/zo-discord
```

The `start.sh` script loads your Zo secrets and starts the bot. The Zo service system handles auto-restart.

### 7. Install the CLI (Optional)

The `zo-discord` CLI lets Zo agents send notifications to Discord:

```bash
ln -sf "$(pwd)/skill/scripts/discord-cli.sh" /usr/local/bin/zo-discord
```

### 8. Install the Skill (Optional)

Copy the skill to your Zo skills directory so Zo knows how to use the Discord API:

```bash
cp -r skill/ /home/workspace/Skills/zo-discord/
```

## How It Works

1. User sends a message in a Discord channel
2. Bot calls the Zo API with the message + channel context (instructions, memory paths, pins)
3. Bot creates a thread with Zo's response
4. Thread-to-conversation mapping is stored in SQLite
5. Follow-up messages in the thread continue the same Zo session
6. Zo agents can post to Discord via the CLI or HTTP API, preserving session continuity

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

Zo agents can deliver results to Discord with session continuity. See `agents/research-agent-example.md` for a complete example.

The key pattern: the agent does its work, builds up conversation context, then sends results via `zo-discord notify`. When the user replies in the Discord thread, they continue the same Zo session with full context of the agent's work.

## Slash Commands

All settings changed via slash commands are persisted to `config.json` and survive bot restarts.

| Command | Description |
| --- | --- |
| `/help` | Bot info and command list |
| `/tips` | Tips and tricks |
| `/link` | Open conversation in Zo |
| `/model` | View/change default model |
| `/thinking` | Toggle thinking mode (streaming/quiet) |
| `/auto-archive` | Configure auto-archive behavior |
| `/instructions` | View channel instructions |
| `/memory` | View channel memory paths |
| `/allowed-users` | Manage allowed users |
| `/cli` | Show CLI commands |

## Thread Management

- **Auto-archive prevention**: When enabled, a background routine bumps thread timers every 6 hours and auto-archives are reversed in real-time.
- **Archive a thread**: React with :white_check_mark: on any bot message. The thread is removed from the watch list and archived.
- **Un-archive**: Manually un-archiving a thread adds it back to the watch list.
- **Toggle**: Use `/auto-archive` to switch between preventing auto-archive and using Discord's channel defaults.

## Project Structure

```
zo-discord/
├── bot.py              # Main bot — event handlers, HTTP API, message processing
├── zo_client.py        # Zo API client — streaming, retries, title generation
├── db.py               # SQLite database — thread mappings, channel config
├── commands.py         # Slash commands and UI components
├── start.sh            # Service entrypoint (sources secrets, runs bot)
├── requirements.txt    # Python dependencies
├── config/
│   ├── config.json         # Your config (gitignored)
│   └── config.example.json # Config template
├── skill/
│   ├── SKILL.md            # Full CLI and HTTP API documentation
│   └── scripts/
│       └── discord-cli.sh  # CLI script
└── agents/
    └── research-agent-example.md  # Example scheduled agent
```

## License

MIT
