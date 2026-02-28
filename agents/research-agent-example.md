# Research Agent Example

Example scheduled task that does work first, then notifies via Discord with session continuity.

## How It Works

1. Agent does work (research, analysis, code review, etc.)
2. Agent sends results to Discord using the `zo-discord` CLI
3. User receives notification in Discord thread
4. User replies to thread, continuing the same Zo session with full context

## Instruction

```markdown
You are a weekly research assistant. Your job is to research a topic and send a summary to Discord.

## Task

1. Research the latest developments in AI agents and tool use (web search, recent papers, blog posts)

2. Synthesize your findings into a concise summary with:
   - Key trends
   - Notable projects or papers
   - Practical implications

3. Write your findings to a temp file and send to Discord:

   ```bash
   cat > /tmp/research-summary.md << 'SUMMARY'
   YOUR_SUMMARY_HERE
   SUMMARY

   zo-discord notify "Weekly AI Agents Research" --file /tmp/research-summary.md --channel-name general
```

4. The notification will create a Discord thread. When the user replies, they continue this same conversation with full context of your research.

## Output

After sending the notification, confirm:

- Research completed
- Notification sent to Discord

Do not send via email/SMS — Discord only.

```markdown

## RRULE

Run weekly on Mondays at 9 AM Eastern:
```

RRULE:FREQ=WEEKLY;BYDAY=MO;BYHOUR=14;BYMINUTE=0

```markdown


(BYHOUR=14 because rrules use UTC; 14:00 UTC = 9:00 AM ET during EST)

## Key Pattern: Session Continuity

The `zo-discord notify` command automatically discovers the agent's conversation ID from the workspace path and passes it to the bot. This links the Discord thread to the agent's session:
- All the agent's prior reasoning is preserved
- User replies continue the same conversation
- No context is lost between agent work and user interaction

## Flow
```

Scheduled Agent

 1. Does research/analysis/work
 2. Builds up conversation context
 3. Sends results via zo-discord CLI\
    |\
    v\
    Discord Bot
 4. Creates thread with title
 5. Posts agent's content
 6. Stores thread_id → conv_id mapping\
    |\
    v\
    User
 7. Sees notification thread in Discord
 8. Replies with follow-up question
 9. Bot looks up conv_id, calls Zo API
10. Agent has full context of prior work

```markdown

## Notes

- Agents discover their conversation ID automatically from the workspace path
- The `zo-discord` CLI handles conversation ID detection — no manual substitution needed
- If you don't use `zo-discord notify`, a new session starts when the user replies
- You can also use the HTTP API directly: `POST http://localhost:8787/notify`
```