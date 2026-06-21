# Discord Spam-Ban Bot

A Discord bot that monitors messages and automatically bans any account that
posts **5 or more messages within 10 seconds**, then notifies the account name to the guild's system message channel.


| Environment variable | Default | Description |
| --- | --- | --- |
| `SPAM_WINDOW_SECONDS` | 10 | Monitoring window (seconds) |
| `SPAM_MESSAGE_THRESHOLD` | 5 | Message count for spam detection |

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
