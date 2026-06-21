"""Discord bot that bans accounts for spamming"""

import os
import time
import logging
from collections import defaultdict, deque

import discord
from discord.ext import tasks
from dotenv import load_dotenv

# Load environment variables from a .env file, if present.
load_dotenv()

# --- Logging ----------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("spam-ban-bot")

# --- Configuration ----------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    """Read an integer setting from the environment, falling back to default."""
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning(
            "Invalid value for %s=%r; using default %d", name, value, default
        )
        return default


# Monitoring window (seconds). Messages within this window are counted.
# Override with the SPAM_WINDOW_SECONDS environment variable.
SPAM_WINDOW_SECONDS = _env_int("SPAM_WINDOW_SECONDS", 10)

# Message count threshold for spam detection. Triggers at this many within the window.
# Override with the SPAM_MESSAGE_THRESHOLD environment variable.
SPAM_MESSAGE_THRESHOLD = _env_int("SPAM_MESSAGE_THRESHOLD", 4)

# Period of messages to delete on ban (seconds). 1 day = 86400 seconds.
BAN_DELETE_MESSAGE_SECONDS = _env_int("BAN_DELETE_MESSAGE_SECONDS", 10 * 60)

# --- Bot --------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True  # Required to read message content
intents.members = True          # Required to access member information

client = discord.Client(intents=intents)

# Recent post timestamps per (guild_id, user_id).
# A deque is used so out-of-window timestamps can be dropped as we go.
message_history: dict[tuple[int, int], deque[float]] = defaultdict(deque)


async def notify_system_channel(guild: discord.Guild, message: str) -> None:
    """Notify the guild's system message channel.

    Does nothing if the system channel is unset or we lack send permission.
    """
    channel = guild.system_channel
    if channel is None:
        logger.warning("No system channel configured: guild=%s", guild.name)
        return

    if not channel.permissions_for(guild.me).send_messages:
        logger.warning("No permission to send to system channel: guild=%s", guild.name)
        return

    try:
        await channel.send(message)
    except discord.DiscordException:
        logger.exception("Failed to notify the system channel")


@tasks.loop(hours=24)
async def cleanup_message_history() -> None:
    """Remove stale message_history entries once a day.

    Entries that only hold out-of-window timestamps (or are empty) are no
    longer needed, so they are dropped from the dict to avoid unbounded
    memory growth. on_message only prunes an entry when that user posts
    again, so this prevents entries from one-off posters from piling up.
    """
    now = time.time()
    stale_keys = []
    for key, history in message_history.items():
        # Drop out-of-window timestamps.
        while history and now - history[0] > SPAM_WINDOW_SECONDS:
            history.popleft()
        if not history:
            stale_keys.append(key)

    for key in stale_keys:
        del message_history[key]

    if stale_keys:
        logger.info("Cleaned up message_history: removed %d entries", len(stale_keys))


@client.event
async def on_ready() -> None:
    logger.info("Logged in: %s (id=%s)", client.user, client.user.id)
    # Guard against duplicate starts on reconnect.
    if not cleanup_message_history.is_running():
        cleanup_message_history.start()


@client.event
async def on_message(message: discord.Message) -> None:
    # Ignore the bot's own messages, other bots, and DMs.
    if message.author.bot or message.guild is None:
        return

    guild = message.guild
    author = message.author
    key = (guild.id, author.id)

    now = message.created_at.timestamp()
    history = message_history[key]
    history.append(now)

    # Drop out-of-window timestamps.
    while history and now - history[0] > SPAM_WINDOW_SECONDS:
        history.popleft()

    if len(history) < SPAM_MESSAGE_THRESHOLD:
        return

    # Spam detected. Clear the history to avoid repeated triggers.
    history.clear()

    logger.info(
        "Spam detected: user=%s guild=%s (%d msgs/%d s)",
        author, guild.name, SPAM_MESSAGE_THRESHOLD, SPAM_WINDOW_SECONDS,
    )

    try:
        await guild.ban(
            author,
            reason="Spam detected",
            delete_message_seconds=BAN_DELETE_MESSAGE_SECONDS,
        )
    except discord.Forbidden:
        logger.warning("No permission to ban: user=%s", author)
        await notify_system_channel(
            guild,
            f"⚠️ Detected spam from {author.mention}, but I lack permission to ban.",
        )
        return
    except discord.DiscordException:
        logger.exception("Failed to ban: user=%s", author)
        return

    await notify_system_channel(
        guild,
        f"🔨 Banned {author} ({author.mention}) for spamming "
        f"({SPAM_MESSAGE_THRESHOLD}+ messages in {SPAM_WINDOW_SECONDS}s); "
    )


def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("Environment variable DISCORD_BOT_TOKEN is not set.")
    client.run(token)


if __name__ == "__main__":
    main()
