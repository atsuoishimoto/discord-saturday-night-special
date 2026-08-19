"""Discord bot that mutes, then bans, accounts for spamming"""

import os
import time
import logging
import datetime
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

# How long to mute (timeout) a user on their first violation (seconds).
# Override with the MUTE_DURATION_SECONDS environment variable.
MUTE_DURATION_SECONDS = _env_int("MUTE_DURATION_SECONDS", 10 * 60)

# Re-offense window (seconds). A user who triggers spam detection again within
# this period after being muted is banned instead of muted again.
# Override with the REOFFENSE_WINDOW_SECONDS environment variable.
REOFFENSE_WINDOW_SECONDS = _env_int("REOFFENSE_WINDOW_SECONDS", 60 * 60)

# Period of messages to delete on ban (seconds). Defaults to the past hour.
BAN_DELETE_MESSAGE_SECONDS = _env_int("BAN_DELETE_MESSAGE_SECONDS", 60 * 60)

# Period of messages to delete when banning a media-only first post (seconds).
# 7 days is the maximum Discord allows for delete_message_seconds.
FIRST_POST_BAN_DELETE_SECONDS = 7 * 24 * 60 * 60

# --- Bot --------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True  # Required to read message content
intents.members = True          # Required to access member information

client = discord.Client(intents=intents)

# Recent post timestamps per (guild_id, user_id).
# A deque is used so out-of-window timestamps can be dropped as we go.
message_history: dict[tuple[int, int], deque[float]] = defaultdict(deque)

# Timestamp (epoch seconds) of the most recent mute per (guild_id, user_id).
# Used to escalate to a ban when a user re-offends within the re-offense window.
muted_at: dict[tuple[int, int], float] = {}

# (guild_id, user_id) pairs confirmed to have posted before. Members in this
# set skip the media-only first-post check entirely.
known_posters: set[tuple[int, int]] = set()

# Message shown to a muted user. The exact threshold is intentionally withheld.
MUTE_DM_MESSAGE = (
    "You have been temporarily muted in **{guild}** for sending messages too "
    "quickly. The mute lasts about {minutes} minutes. Please slow down — if it "
    "happens again shortly after, you may be banned from the server."
)


def is_media_only(message: discord.Message) -> bool:
    """Return True if the message has no text and only image/video attachments.

    Sticker-only messages are not media-only: they have no attachments.
    Attachments whose content_type is unknown (None) or of any other kind
    (e.g. application/pdf) are given the benefit of the doubt.
    """
    if message.content.strip():
        return False
    if not message.attachments:
        return False
    return all(
        attachment.content_type is not None
        and attachment.content_type.startswith(("image/", "video/"))
        for attachment in message.attachments
    )


async def has_prior_message(guild: discord.Guild, message: discord.Message) -> bool | None:
    """Check whether the author has an earlier message indexed in the guild.

    Uses GET /guilds/{guild_id}/messages/search, which discord.py has no
    wrapper for. Asks for the 2 newest messages so the answer does not depend
    on whether the triggering message itself is already in the search index:
    any hit with a different id proves an earlier post exists.

    Returns True if an earlier post exists, False if this is the author's
    first post, or None if the search failed or returned an unexpected shape
    (e.g. the index is still being built) — callers should skip the check.
    """
    route = discord.http.Route(
        "GET", "/guilds/{guild_id}/messages/search", guild_id=guild.id
    )
    params = {
        "author_id": message.author.id,
        "sort_by": "timestamp",
        "sort_order": "desc",
        "limit": 2,
    }
    try:
        data = await client.http.request(route, params=params)
    except discord.HTTPException:
        logger.exception(
            "Message search failed: guild=%s user=%s", guild.name, message.author
        )
        return None

    # A 202 "index not ready" response has no "messages" key; treat anything
    # without one as inconclusive rather than as "no prior posts".
    if not isinstance(data, dict) or "messages" not in data:
        logger.warning("Inconclusive message search response: %r", data)
        return None

    try:
        for entry in data["messages"]:
            # Each entry is a list of messages with the hit first.
            hit = entry[0] if isinstance(entry, list) else entry
            if int(hit["id"]) != message.id:
                return True
        return False
    except (KeyError, TypeError, ValueError, IndexError):
        logger.warning("Unexpected message search response shape: %r", data)
        return None


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

    # Drop mute records that are past the re-offense window; they can no longer
    # escalate to a ban, so there is no reason to keep them.
    expired_mutes = [
        key
        for key, ts in muted_at.items()
        if now - ts > REOFFENSE_WINDOW_SECONDS
    ]
    for key in expired_mutes:
        del muted_at[key]

    if expired_mutes:
        logger.info("Cleaned up muted_at: removed %d entries", len(expired_mutes))


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

    # Ban accounts whose very first post in the guild is media-only.
    if key not in known_posters:
        if is_media_only(message):
            prior = await has_prior_message(guild, message)
            if prior is False:
                logger.info(
                    "Media-only first post detected: user=%s guild=%s",
                    author, guild.name,
                )
                await ban_user(
                    guild,
                    author,
                    reason="Media-only first post",
                    delete_message_seconds=FIRST_POST_BAN_DELETE_SECONDS,
                    notice=(
                        f"🔨 Banned {author} ({author.mention}) for posting only "
                        "media as their first message."
                    ),
                )
                return
            if prior is True:
                known_posters.add(key)
            # prior is None: the search was inconclusive; leave the user
            # unchecked so the next message tries again.
        else:
            # A regular post is now on record; skip future first-post checks.
            known_posters.add(key)

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

    # Escalate to a ban if the user re-offends within the re-offense window
    # after being muted; otherwise mute them for MUTE_DURATION_SECONDS.
    previous_mute = muted_at.get(key)
    if previous_mute is not None and now - previous_mute <= REOFFENSE_WINDOW_SECONDS:
        await ban_user(guild, author)
    else:
        await mute_user(guild, author, key)


async def mute_user(
    guild: discord.Guild,
    member: discord.Member,
    key: tuple[int, int],
) -> None:
    """Time the member out, notify the system channel, and DM them.

    The DM is sent in English and deliberately does not reveal the exact
    spam threshold. The mute is recorded so a re-offense escalates to a ban.
    """
    try:
        await member.timeout(
            datetime.timedelta(seconds=MUTE_DURATION_SECONDS),
            reason="Spam detected",
        )
    except discord.Forbidden:
        logger.warning("No permission to mute: user=%s", member)
        await notify_system_channel(
            guild,
            f"⚠️ Detected spam from {member.mention}, but I lack permission to mute.",
        )
        return
    except discord.DiscordException:
        logger.exception("Failed to mute: user=%s", member)
        return

    # Record the mute so a quick re-offense escalates to a ban.
    muted_at[key] = time.time()

    minutes = MUTE_DURATION_SECONDS // 60
    await notify_system_channel(
        guild,
        f"🔇 Muted {member} ({member.mention}) for {minutes} minutes for spamming.",
    )

    # DM the user. They may have DMs disabled, so failures are non-fatal.
    try:
        await member.send(MUTE_DM_MESSAGE.format(guild=guild.name, minutes=minutes))
    except discord.Forbidden:
        logger.info("Could not DM muted user (DMs closed?): user=%s", member)
    except discord.DiscordException:
        logger.exception("Failed to DM muted user: user=%s", member)


async def ban_user(
    guild: discord.Guild,
    member: discord.Member,
    reason: str = "Repeated spam after mute",
    delete_message_seconds: int = BAN_DELETE_MESSAGE_SECONDS,
    notice: str | None = None,
) -> None:
    """Ban a member, deleting their recent messages."""
    try:
        await guild.ban(
            member,
            reason=reason,
            delete_message_seconds=delete_message_seconds,
        )
    except discord.Forbidden:
        logger.warning("No permission to ban: user=%s", member)
        await notify_system_channel(
            guild,
            f"⚠️ Tried to ban {member.mention} ({reason}), but I lack permission to ban.",
        )
        return
    except discord.DiscordException:
        logger.exception("Failed to ban: user=%s", member)
        return

    # The user is gone; drop any mute record so it cannot linger.
    muted_at.pop((guild.id, member.id), None)

    if notice is None:
        notice = f"🔨 Banned {member} ({member.mention}) for repeated spamming after a mute."
    await notify_system_channel(guild, notice)


def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("Environment variable DISCORD_BOT_TOKEN is not set.")
    client.run(token)


if __name__ == "__main__":
    main()
