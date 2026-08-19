# Discord Spam-Ban Bot

A Discord bot that monitors messages for spam (too many messages within a short
window) and escalates moderation in two steps:

1. **First offense** — the account is muted (timed out) for 10 minutes. The
   guild's system message channel is notified, and the user receives an English
   DM telling them they were muted for posting too quickly (the exact threshold
   is not disclosed).
2. **Re-offense within 1 hour** — if the same account triggers spam detection
   again within an hour of being muted, it is banned and its messages from the
   past hour are deleted. The system channel is notified.

It also bans accounts whose **very first post** in the server is media-only:

- A message counts as media-only when it has no text and every attachment is
  an image (`image/*`) or a video (`video/*`). Sticker-only messages and
  attachments of other or unknown types are allowed.
- Whether it is the author's first post is checked with the guild message
  search endpoint (`GET /guilds/{guild_id}/messages/search`), asking for the
  author's two newest messages; any hit other than the triggering message
  proves an earlier post exists. If the search fails or the index is not
  ready, the check is skipped rather than risking a false ban.
- On a hit, the account is banned and its messages from the past 7 days (the
  Discord maximum) are deleted. Users confirmed to have posted before are
  remembered in memory so they are never re-checked.


| Environment variable | Default | Description |
| --- | --- | --- |
| `SPAM_WINDOW_SECONDS` | 10 | Monitoring window (seconds) |
| `SPAM_MESSAGE_THRESHOLD` | 4 | Message count for spam detection |
| `MUTE_DURATION_SECONDS` | 600 | Mute (timeout) duration on first offense (seconds) |
| `REOFFENSE_WINDOW_SECONDS` | 3600 | Window after a mute in which a re-offense triggers a ban (seconds) |
| `BAN_DELETE_MESSAGE_SECONDS` | 3600 | Period of messages deleted on ban (seconds) |

## Setup

1. Install dependencies. This project uses [uv](https://docs.astral.sh/uv/).

   ```bash
   uv sync
   ```

2. Create a bot in the
   [Discord Developer Portal](https://discord.com/developers/applications) and
   enable the following **Privileged Gateway Intents**:

   - `MESSAGE CONTENT INTENT`
   - `SERVER MEMBERS INTENT`

3. When inviting the bot to your server, grant the following permissions:

   - View Channels / Read Messages
   - Moderate Members (to mute / time out users)
   - Ban Members
   - Send Messages (to the notification channel)

4. Configure the bot token. The bot loads variables from a `.env` file via
   [python-dotenv](https://pypi.org/project/python-dotenv/), so copy the
   example file and fill it in:

   ```bash
   cp .env.example .env
   # then edit .env and set DISCORD_BOT_TOKEN (and any optional overrides)
   ```

   Real environment variables still work too and take precedence over `.env`.

5. Start the bot.

   ```bash
   uv run saturday_night_special.py
   ```
