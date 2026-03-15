# MODEL OF THE DAY BOT

# This bot does the following:
# 1. Watches a submissions channel for uploaded image posts
# 2. Automatically adds the vote emoji to valid submissions
# 3. At a scheduled time, scans the previous MotD time window
# 4. Picks each user's (single) highest-voted submission only
# 5. Ranks winners, allowing ties
# 6. Posts the results in both the results channel and submissions channel
# 7. Reposts the winning images
# 8. Posts a Non-Pro notice on Mondays and Thursdays
#
# The bot is written in Python using discord.py.
# Discord itself does NOT care what language is used.
# This script simply connects to the bot account using the token.


# IMPORTS

import os
import datetime as dt
from zoneinfo import ZoneInfo
from io import BytesIO

import discord
from discord.ext import commands, tasks


# BOT TOKEN

# Reads the bot token from the DISCORD_TOKEN environment variable.
# Input: none
# Output: string token or None
TOKEN = os.getenv("DISCORD_TOKEN")


# CONFIG

# Channel IDs:
# SUBMISSIONS_CHANNEL_ID = channel where users post entries
# RESULTS_CHANNEL_ID     = channel where winners are announced
# Note: the bot also reposts the same results in the submissions channel
SUBMISSIONS_CHANNEL_ID = 1197939941372608532
RESULTS_CHANNEL_ID = 983134844492079154

# Emoji used for voting
# Must match the actual reaction string seen by discord.py
VOTE_EMOJI = "<:upvote:962050161771696148>"

# Timezone used for the daily reset / announcement
TIMEZONE = ZoneInfo("Europe/Vienna")

# Daily scheduled posting time
POST_HOUR = 17
POST_MINUTE = 0

# Highest place number to include
# 3 = 1st, 2nd, 3rd
TOP_N = 3

# If True, subtract the bot's own reaction from vote counts
# If False, the bot's reaction counts as a vote
EXCLUDE_BOT_VOTE = False

# User IDs allowed to run dev/test commands
DEV_USER_IDS = {
    1097539138959462471,  # Vinpenny
    582786439763329024,   # s3rm0z
}

# Weekdays where the bot should post the Non-Pro reminder
# Monday = 0
# Thursday = 3
NON_PRO_WEEKDAYS = {0, 3}

# Message posted on Non-Pro days
NON_PRO_MESSAGE = (
    "# Today is a NON-PRO day! Any minis made using pro features will be deleted!"
)


# HELPERS

def is_image_attachment(att: discord.Attachment) -> bool:
    """
    Check whether a Discord attachment counts as an image submission.

    Receives:
        att (discord.Attachment)
            A single attachment from a Discord message.

    Returns:
        bool
            True if the attachment looks like an image.
            False otherwise.

    Notes:
        This currently accepts png, jpg, jpeg, webp, and gif.
        It first checks the content_type if Discord provided one.
        If not, it falls back to checking the filename extension.
    """
    if att.content_type:
        return att.content_type.startswith("image/")
    return att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))


def score_message(msg: discord.Message) -> int:
    """
    Count how many votes a message has.

    Receives:
        msg (discord.Message)
            A Discord message whose reactions should be checked.

    Returns:
        int
            The number of votes using VOTE_EMOJI.
            Returns 0 if the message has no matching vote reaction.

    Notes:
        If EXCLUDE_BOT_VOTE is True, the returned count is reduced by 1.
        This allows the bot to auto-react without granting a free vote.
    """
    for r in msg.reactions:
        if str(r.emoji) == VOTE_EMOJI:
            count = r.count
            if EXCLUDE_BOT_VOTE:
                count = max(0, count - 1)
            return count
    return 0


def get_window_scheduled(now: dt.datetime):
    """
    Build the normal scheduled MotD time window.

    Receives:
        now (datetime)
            The current datetime in the configured timezone.

    Returns:
        tuple[datetime, datetime]
            (start, end)

    Meaning:
        This creates the completed MotD day window:
        yesterday at POST_HOUR:POST_MINUTE -> today at POST_HOUR:POST_MINUTE

    Example:
        If POST_HOUR = 17 and now is 15-03-2026 17:00,
        this returns:
            start = 14-03-2026 17:00
            end   = 15-03-2026 17:00
    """
    end = now.replace(hour=POST_HOUR, minute=POST_MINUTE, second=0, microsecond=0)
    start = end - dt.timedelta(days=1)
    return start, end


def get_window_last24h(now: dt.datetime):
    """
    Build a rolling 24-hour time window for manual testing.

    Receives:
        now (datetime)
            The current datetime.

    Returns:
        tuple[datetime, datetime]
            (start, end)

    Meaning:
        This creates:
            now - 24 hours -> now

    Used by:
        The dev command !motdtest
    """
    end = now
    start = end - dt.timedelta(days=1)
    return start, end


# BOT CLASS

class MotDBot(commands.Bot):
    """
    Main bot class.

    Purpose:
        Sets up the bot client, permissions/intents,
        command prefix, and startup hooks.

    Inherits from:
        commands.Bot
    """

    def __init__(self):
        """
        Initializes the bot instance.

        Receives:
            no external arguments

        Sets:
            intents.guilds          -> allows guild/server data
            intents.messages        -> allows reading messages
            intents.reactions       -> allows reading reactions
            intents.message_content -> allows command text like !motdtest

        Returns:
            None
        """
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.reactions = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        """
        Runs during bot startup before full connection is complete.

        Receives:
            no external arguments

        Does:
            Starts the scheduled daily announcement loop.

        Returns:
            None
        """
        announce_winners.start()


# Create the bot instance
bot = MotDBot()


# EVENTS

@bot.event
async def on_ready():
    """
    Event fired once the bot has successfully connected to Discord.

    Receives:
        no explicit arguments

    Does:
        Prints the logged-in bot user to the console.

    Returns:
        None
    """
    print(f"Logged in as {bot.user}")


@bot.event
async def on_message(message: discord.Message):
    """
    Event fired every time the bot can see a message.

    Receives:
        message (discord.Message)
            The new message that was sent.

    Does:
        - Ignores bot and webhook messages
        - In the submissions channel:
            - checks whether the message contains an uploaded image
            - if valid, auto-adds the vote emoji
        - passes the message into the command system

    Returns:
        None

    Important:
        This function only reacts to uploaded image attachments.
        It ignores text-only posts and non-image attachments.
    """
    if message.author.bot or message.webhook_id:
        return

    # Only react to uploaded image submissions
    if message.channel.id == SUBMISSIONS_CHANNEL_ID:
        has_image = any(is_image_attachment(att) for att in message.attachments)

        if not has_image:
            return  # Ignore text-only messages and non-image attachments

        try:
            if not any(str(r.emoji) == VOTE_EMOJI for r in message.reactions):
                await message.add_reaction(VOTE_EMOJI)
        except Exception:
            pass

    await bot.process_commands(message)


# CORE MOTD LOGIC

async def run_motd_announcement(bot_obj: commands.Bot, use_last24h: bool = False):
    """
    Main function that performs the full MotD winner calculation and posting.

    Receives:
        bot_obj (commands.Bot)
            The bot instance, used to access channels and send messages.

        use_last24h (bool)
            If False:
                use the normal scheduled MotD window
            If True:
                use a rolling last-24-hours window for testing

    Returns:
        None

    Main steps:
        1. Get submissions and results channels
        2. Compute the active time window
        3. Scan submission history within that window
        4. Keep only the best-scoring post per author
        5. Sort all qualifying entries
        6. Rank 1st/2nd/3rd place, allowing ties
        7. Post the text announcement
        8. Repost the winning images
        9. Post the Non-Pro notice if needed
    """
    sub_ch = bot_obj.get_channel(SUBMISSIONS_CHANNEL_ID)
    res_ch = bot_obj.get_channel(RESULTS_CHANNEL_ID)

    if not sub_ch or not res_ch:
        return

    now = dt.datetime.now(TIMEZONE)
    start, end = (get_window_last24h(now) if use_last24h else get_window_scheduled(now))

    # best_by_author maps:
    #   user_id -> best message for that user in this time window
    best_by_author: dict[int, discord.Message] = {}

    async for msg in sub_ch.history(after=start, before=end):
        # Ignore bot and webhook posts
        if msg.author.bot or msg.webhook_id:
            continue

        # Ignore anything with no votes
        votes = score_message(msg)
        if votes <= 0:
            continue

        author_id = msg.author.id
        current_best = best_by_author.get(author_id)

        # Replace the stored entry if:
        # - this is the first valid post for the user
        # - OR this post has more votes
        # - OR it has the same votes but was posted earlier
        if (
            current_best is None
            or votes > score_message(current_best)
            or (votes == score_message(current_best) and msg.created_at < current_best.created_at)
        ):
            best_by_author[author_id] = msg

    entries = list(best_by_author.values())

    # No valid entries found
    if not entries:
        text = "# No winners today"
        await res_ch.send(text)
        await sub_ch.send(text)

        if now.weekday() in NON_PRO_WEEKDAYS:
            await sub_ch.send(NON_PRO_MESSAGE)
        return

    # Sort highest votes first, then earlier posts first
    entries.sort(key=lambda m: (-score_message(m), m.created_at))

    # ranked will contain:
    #   (place_number, message)
    ranked: list[tuple[int, discord.Message]] = []
    place = 0
    last_votes = None

    for msg in entries:
        votes = score_message(msg)

        # Increase place number whenever vote count drops
        if last_votes is None or votes < last_votes:
            place += 1

        # Stop once we go beyond 3rd place
        if place > TOP_N:
            break

        ranked.append((place, msg))
        last_votes = votes

    # TEXT ANNOUNCEMENT

    lines = ["# Congratulations to our :medal: MODEL OF THE DAY :medal: winners!"]

    # Group tied winners under the same placement line
    winners_by_place: dict[int, list[discord.Message]] = {}
    for place_num, msg in ranked:
        winners_by_place.setdefault(place_num, []).append(msg)

    for place_num in sorted(winners_by_place.keys()):
        msgs = winners_by_place[place_num]
        votes = score_message(msgs[0])

        if place_num == 1:
            emoji, label = ":first_place:", "1st"
        elif place_num == 2:
            emoji, label = ":second_place:", "2nd"
        else:
            emoji, label = ":third_place:", "3rd"

        lines.append(f"In {label} {emoji} place with {votes} upvotes")
        for m in msgs:
            lines.append(f"By {m.author.mention}")

    text = "\n".join(lines)

    # Post announcement text in both channels
    await res_ch.send(text)
    await sub_ch.send(text)

    # IMAGE REPOST
    # image_blobs contains:
    #   (filename, raw file bytes)
    image_blobs: list[tuple[str, bytes]] = []

    for i, (_, msg) in enumerate(ranked):
        # Only use the first valid image attachment from the winning post
        att = next((a for a in msg.attachments if is_image_attachment(a)), None)
        if att:
            try:
                data = await att.read()
                if data:
                    image_blobs.append((f"{i+1}_{att.filename}", data))
            except Exception:
                pass

    if image_blobs:
        # Create fresh file objects for each send
        files_res = [discord.File(BytesIO(d), filename=n) for n, d in image_blobs]
        files_sub = [discord.File(BytesIO(d), filename=n) for n, d in image_blobs]

        await res_ch.send(files=files_res)
        await sub_ch.send(files=files_sub)

    # NON-PRO DAY NOTICE

    if now.weekday() in NON_PRO_WEEKDAYS:
        await sub_ch.send(NON_PRO_MESSAGE)


# SCHEDULED TASK

@tasks.loop(time=dt.time(hour=POST_HOUR, minute=POST_MINUTE, tzinfo=TIMEZONE))
async def announce_winners():
    """
    Scheduled task that runs once per day at POST_HOUR:POST_MINUTE.

    Receives:
        no explicit external arguments

    Does:
        Waits until the bot is ready, then runs the normal MotD announcement.

    Returns:
        None
    """
    await bot.wait_until_ready()
    await run_motd_announcement(bot, use_last24h=False)


# DEV COMMANDS

@bot.command(name="motdtest")
async def motdtest(ctx: commands.Context):
    """
    Developer-only command to manually run the MotD logic.

    Receives:
        ctx (commands.Context)
            Command context containing the author, channel, etc.

    Does:
        If the author is in DEV_USER_IDS:
            runs the MotD logic using the last 24 hours
        Otherwise:
            silently returns

    Returns:
        None
    """
    if ctx.author.id not in DEV_USER_IDS:
        return

    await run_motd_announcement(bot, use_last24h=True)


# STARTUP SAFETY CHECK

if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN environment variable first")


# START BOT

bot.run(TOKEN)
