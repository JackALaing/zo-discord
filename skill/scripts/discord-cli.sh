#!/bin/bash
# zo-discord CLI — control Discord threads from Zo conversations
# Conversation ID: --conv-id flag (Zo agents) or CONVERSATION_ID env var (Hermes agents).
#
# Usage: zo-discord <command> [args...]
#
# Commands:
#   rename <title>                              — Rename the thread
#   error                                       — Set status to error
#   notify <title> <content> --channel-name NAME — Post content to a new thread
#   notify <title> --file <path> --channel-name NAME — Post file contents to a new thread
#   buttons "Prompt?" "Label:style" ...         — Send interactive buttons
#   buttons "Prompt?" --preset yes_no           — Send preset buttons (yes_no, approve_reject)
#   files <path> [message]                      — Send a file attachment to the thread
#   new-thread <title> <prompt> [--channel-name NAME] — Spawn a new thread
#
# Channel targeting: use --channel-name <name> (e.g. "general", "pulse") or --channel <id>

set -euo pipefail

API="http://localhost:8787"
CMD="${1:?Usage: zo-discord <command> [args...] (try 'zo-discord help')}"
shift

# Help — no conversation ID needed
if [[ "$CMD" == "help" || "$CMD" == "--help" || "$CMD" == "-h" ]]; then
  cat <<'HELP'
zo-discord — control Discord threads from Zo conversations

Usage: zo-discord <command> [args...]

Commands:
  rename <title>                                Rename the thread
  error                                         Set status to error
  notify <title> <content> --channel-name NAME  Post content to a new thread
  notify <title> --file <path> --channel-name NAME  Post file contents to a new thread
  buttons "Prompt?" "Label:style" ...           Send interactive buttons
  buttons "Prompt?" --preset yes_no             Send preset buttons (yes_no, approve_reject)
  files <path> [message]                        Send a file attachment (max 25MB)
  new-thread <title> <prompt> [--channel-name NAME]  Spawn a new thread

Channel targeting: use --channel-name <name> or --channel <id>
Conv ID: pass --conv-id <id>, or set CONVERSATION_ID / ZO_CONVERSATION_ID env var
HELP
  exit 0
fi

# Auto-detect conversation ID
CONV_ID=""

# 1. Check for --conv-id flag (can appear anywhere in remaining args)
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --conv-id) CONV_ID="$2"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
set -- "${ARGS[@]+"${ARGS[@]}"}"

# 2. CONVERSATION_ID env var (Hermes agents get this from zo-hermes/server.py)
if [[ -z "$CONV_ID" && -n "${CONVERSATION_ID:-}" ]]; then
  CONV_ID="$CONVERSATION_ID"
elif [[ -z "$CONV_ID" && -n "${ZO_CONVERSATION_ID:-}" ]]; then
  CONV_ID="$ZO_CONVERSATION_ID"
fi

# No more fallbacks — error out
if [[ -z "$CONV_ID" ]]; then
  echo "Error: Could not detect conversation ID." >&2
  echo "Pass --conv-id <id> (check your system prompt's <conversation_workspace> section for the ID)." >&2
  exit 1
fi

case "$CMD" in
  rename)
    TITLE="${1:?Usage: zo-discord rename <title>}"
    curl -sS -X POST "$API/conversations/$CONV_ID/action" \
      -H "Content-Type: application/json" \
      -d "{\"action\": \"rename\", \"name\": \"$TITLE\"}"
    ;;
  error)
    curl -sS -X POST "$API/conversations/$CONV_ID/action" \
      -H "Content-Type: application/json" \
      -d "{\"action\": \"$CMD\"}"
    ;;
  notify)
    TITLE="${1:?Usage: zo-discord notify <title> [<content>] --channel-name NAME [--file path]}"
    shift
    CONTENT=""
    CHANNEL=""
    CHANNEL_NAME=""
    FILE=""
    # First check if next arg is positional content (not a flag)
    if [[ $# -gt 0 && ! "$1" =~ ^-- ]]; then
      CONTENT="$1"
      shift
    fi
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --channel-name) CHANNEL_NAME="$2"; shift 2 ;;
        --channel) CHANNEL="$2"; shift 2 ;;
        --file) FILE="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
      esac
    done
    if [[ -z "$CHANNEL" && -z "$CHANNEL_NAME" ]]; then
      echo "Error: --channel-name or --channel is required for notify" >&2; exit 1
    fi
    if [[ -z "$CONTENT" && -z "$FILE" ]]; then
      echo "Error: provide content as argument or use --file <path>" >&2; exit 1
    fi
    PAYLOAD=$(python3 -c "
import json, sys
title, channel, channel_name, content, filepath, conv_id = sys.argv[1:7]
if filepath:
    with open(filepath) as f:
        content = f.read()
d = {'title': title, 'content': content, 'conversation_id': conv_id}
if channel_name:
    d['channel_name'] = channel_name
else:
    d['channel_id'] = channel
print(json.dumps(d))
" "$TITLE" "$CHANNEL" "$CHANNEL_NAME" "$CONTENT" "$FILE" "$CONV_ID")
    RESPONSE=$(curl -sS -w "\n%{http_code}" -X POST "$API/notify" \
      -H "Content-Type: application/json" \
      -d "$PAYLOAD")
    HTTP_CODE=$(echo "$RESPONSE" | tail -1)
    BODY=$(echo "$RESPONSE" | sed '$d')
    if [[ "$HTTP_CODE" == "409" ]]; then
      ERROR_MSG=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('error','Conversation already has a linked thread'))" 2>/dev/null || echo "Conversation already has a linked Discord thread. Do NOT use zo-discord notify; just respond directly.")
      echo "REJECTED: $ERROR_MSG" >&2
      exit 1
    fi
    echo "$BODY"
    ;;
  buttons)
    PROMPT=""
    PRESET=""
    BTN_ARGS=()
    # Parse args: first non-flag arg is prompt, rest are button specs or flags
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --preset) PRESET="$2"; shift 2 ;;
        *)
          if [[ -z "$PROMPT" ]]; then
            PROMPT="$1"
          else
            BTN_ARGS+=("$1")
          fi
          shift
          ;;
      esac
    done
    if [[ -z "$PROMPT" ]]; then
      echo "Usage: zo-discord buttons \"Prompt?\" [\"Label:style\" ...] [--preset name]" >&2
      echo "Styles: primary, secondary, success, danger" >&2
      echo "Presets: approve_reject, yes_no" >&2
      exit 1
    fi
    PAYLOAD=$(python3 -c "
import json, sys
prompt = sys.argv[1]
preset = sys.argv[2]
btn_args = sys.argv[3:]
d = {'prompt': prompt}
if preset:
    d['preset'] = preset
elif btn_args:
    buttons = []
    for b in btn_args:
        parts = b.split(':', 1)
        label = parts[0]
        style = parts[1] if len(parts) > 1 else 'primary'
        btn_id = label.lower().replace(' ', '_')
        buttons.append({'label': label, 'id': btn_id, 'style': style})
    d['buttons'] = buttons
else:
    print('Error: provide button args or --preset', file=sys.stderr)
    sys.exit(1)
print(json.dumps(d))
" "$PROMPT" "$PRESET" "${BTN_ARGS[@]+"${BTN_ARGS[@]}"}")
    curl -sS -X POST "$API/conversations/$CONV_ID/buttons" \
      -H "Content-Type: application/json" \
      -d "$PAYLOAD"
    ;;
  files)
    FILEPATH="${1:?Usage: zo-discord files <path> [message]}"
    shift
    MESSAGE="${1:-}"
    PAYLOAD=$(python3 -c "
import json, sys
d = {'file_path': sys.argv[1]}
if sys.argv[2]:
    d['message'] = sys.argv[2]
print(json.dumps(d))
" "$FILEPATH" "$MESSAGE")
    curl -sS -X POST "$API/conversations/$CONV_ID/files" \
      -H "Content-Type: application/json" \
      -d "$PAYLOAD"
    ;;
  new-thread)
    TITLE="${1:?Usage: zo-discord new-thread <title> <prompt> [--channel-name NAME]}"
    shift
    PROMPT="${1:?Usage: zo-discord new-thread <title> <prompt> [--channel-name NAME]}"
    shift
    CHANNEL=""
    CHANNEL_NAME=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --channel-name) CHANNEL_NAME="$2"; shift 2 ;;
        --channel) CHANNEL="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
      esac
    done
    PAYLOAD=$(python3 -c "
import json, sys
d = {'title': sys.argv[1], 'prompt': sys.argv[2]}
channel, channel_name = sys.argv[3], sys.argv[4]
if channel_name:
    d['channel_name'] = channel_name
elif channel:
    d['channel_id'] = channel
print(json.dumps(d))
" "$TITLE" "$PROMPT" "$CHANNEL" "$CHANNEL_NAME")
    curl -sS -X POST "$API/conversations/$CONV_ID/new-thread" \
      -H "Content-Type: application/json" \
      -d "$PAYLOAD"
    ;;
  *)
    echo "Unknown command: $CMD" >&2
    echo "Commands: rename, error, notify, buttons, files, new-thread" >&2
    exit 1
    ;;
esac
