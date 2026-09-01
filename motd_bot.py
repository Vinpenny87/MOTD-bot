import os
import json
import datetime as dt
from zoneinfo import ZoneInfo
from io import BytesIO

import discord
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN")

SUBMISSIONS_CHANNEL_ID = 1197939941372608532
RESULTS_CHANNEL_ID = 983134844492079154

MODEL_OF_THE_DAY_ROLE_ID = 1393634269544579082

WINNER_OF_THE_DAY_ROLE_ID = 1425135652231577620
WINNER_OF_THE_WEEK_ROLE_ID = 1541095899340607548
WINNER_OF_THE_MONTH_ROLE_ID = 1544185477823729684

WINNER_MONTH_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "winner_of_the_month_state.json",
)

WINNER_WEEK_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "winner_of_the_week_state.json",
)

WINNER_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "winner_of_the_day_state.json",
)

VOTE_EMOJI = "<:upvote:962050161771696148>"

TIMEZONE = ZoneInfo("Europe/Vienna")

DAILY_POST_HOUR = 16
DAILY_POST_MINUTE = 59

WEEKLY_POST_HOUR = 17
WEEKLY_POST_MINUTE = 0

MONTHLY_POST_HOUR = 17
MONTHLY_POST_MINUTE = 1

TOP_N = 3

EXCLUDE_BOT_VOTE = False

DEV_USER_IDS = {
    1097539138959462471,
    582786439763329024,
}

NON_PRO_WEEKDAYS = {0, 3}

NON_PRO_MESSAGE = (
    "# Today is a NON-PRO day! Any minis made using pro features will be deleted!"
)

def is_image_attachment(att: discord.Attachment) -> bool:
    if att.content_type:
        return att.content_type.startswith("image/")
    return att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))

def score_message(msg: discord.Message) -> int:
    for r in msg.reactions:
        if str(r.emoji) == VOTE_EMOJI:
            count = r.count
            if EXCLUDE_BOT_VOTE:
                count = max(0, count - 1)
            return count
    return 0

def get_window_daily(now: dt.datetime):
    end = now.replace(
        hour=DAILY_POST_HOUR,
        minute=DAILY_POST_MINUTE,
        second=0,
        microsecond=0,
    )
    start = end - dt.timedelta(days=1)
    return start, end

def get_window_last24h(now: dt.datetime):
    end = now
    start = end - dt.timedelta(days=1)
    return start, end

def get_window_weekly(now: dt.datetime):
    end = now.replace(
        hour=WEEKLY_POST_HOUR,
        minute=WEEKLY_POST_MINUTE,
        second=0,
        microsecond=0,
    )
    start = end - dt.timedelta(days=7)
    return start, end

def get_window_monthly(now: dt.datetime):
    end = now.replace(
        day=1,
        hour=WEEKLY_POST_HOUR,
        minute=WEEKLY_POST_MINUTE,
        second=0,
        microsecond=0,
    )

    if end.month == 1:
        start = end.replace(year=end.year - 1, month=12)
    else:
        start = end.replace(month=end.month - 1)

    return start, end

def load_previous_winner_ids(state_file: str = WINNER_STATE_FILE) -> set[int]:
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        return {int(user_id) for user_id in data.get("winner_ids", [])}
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return set()

def save_current_winner_ids(winner_ids: set[int], state_file: str = WINNER_STATE_FILE) -> None:
    try:
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(
                {"winner_ids": sorted(winner_ids)},
                f,
                indent=2,
            )
    except OSError as exc:
        print(f"Failed to save Winner of the Day state: {exc}")

async def update_winner_roles(
    guild: discord.Guild,
    role_id: int,
    current_winner_ids: set[int],
    state_file: str = WINNER_STATE_FILE,
) -> None:
    winner_role = guild.get_role(role_id)
    if winner_role is None:
        print(f"Winner of the Day role {role_id} was not found.")
        return

    previous_winner_ids = load_previous_winner_ids(state_file)

    for user_id in previous_winner_ids - current_winner_ids:
        try:
            member = guild.get_member(user_id)
            if member is None:
                member = await guild.fetch_member(user_id)

            if winner_role in member.roles:
                await member.remove_roles(
                    winner_role,
                    reason="No longer included in today's Model of the Day winners",
                )
        except discord.NotFound:

            pass
        except Exception as exc:
            print(
                f"Failed to remove Winner of the Day role "
                f"from user {user_id}: {exc}"
            )

    for user_id in current_winner_ids:
        try:
            member = guild.get_member(user_id)
            if member is None:
                member = await guild.fetch_member(user_id)

            if winner_role not in member.roles:
                await member.add_roles(
                    winner_role,
                    reason="Included in today's Model of the Day winners",
                )
        except discord.NotFound:

            pass
        except Exception as exc:
            print(
                f"Failed to give Winner of the Day role "
                f"to user {user_id}: {exc}"
            )

    save_current_winner_ids(current_winner_ids, state_file)

class MotDBot(commands.Bot):

    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.reactions = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        announce_daily.start()
        announce_weekly.start()
        announce_monthly.start()

bot = MotDBot()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.webhook_id:
        return

    if message.channel.id == SUBMISSIONS_CHANNEL_ID:
        has_image = any(is_image_attachment(att) for att in message.attachments)

        if has_image:
            try:
                if not any(str(r.emoji) == VOTE_EMOJI for r in message.reactions):
                    await message.add_reaction(VOTE_EMOJI)
            except Exception as exc:
                print(f"Failed to add vote reaction to message {message.id}: {exc}")

    await bot.process_commands(message)

async def run_ranked_announcement(
    bot_obj: commands.Bot,
    start: dt.datetime,
    end: dt.datetime,
    title_text: str,
    no_winners_text: str,
    post_non_pro_notice: bool = False,
    ping_role_id: int | None = None,
    winner_role_id: int | None = None,
    winner_state_file: str = WINNER_STATE_FILE,
):
    sub_ch = bot_obj.get_channel(SUBMISSIONS_CHANNEL_ID)
    res_ch = bot_obj.get_channel(RESULTS_CHANNEL_ID)

    if not sub_ch or not res_ch:
        return

    now = dt.datetime.now(TIMEZONE)

    best_by_author: dict[int, discord.Message] = {}

    async for msg in sub_ch.history(
        after=start,
        before=end,
        limit=None,
        oldest_first=True,
    ):

        if msg.author.bot or msg.webhook_id:
            continue

        votes = score_message(msg)
        if votes <= 0:
            continue

        author_id = msg.author.id
        current_best = best_by_author.get(author_id)

        if (
            current_best is None
            or votes > score_message(current_best)
            or (votes == score_message(current_best) and msg.created_at < current_best.created_at)
        ):
            best_by_author[author_id] = msg

    entries = list(best_by_author.values())

    if not entries:

        if winner_role_id is not None:
            await update_winner_roles(
                guild=sub_ch.guild,
                role_id=winner_role_id,
                current_winner_ids=set(),
                state_file=winner_state_file,
            )

        await res_ch.send(no_winners_text)
        await sub_ch.send(no_winners_text)

        if post_non_pro_notice and now.weekday() in NON_PRO_WEEKDAYS:
            await sub_ch.send(NON_PRO_MESSAGE)
        return

    entries.sort(key=lambda m: (-score_message(m), m.created_at))

    ranked: list[tuple[int, discord.Message]] = []
    place = 0
    last_votes = None

    for msg in entries:
        votes = score_message(msg)

        if last_votes is None or votes < last_votes:
            place += 1

        if place > TOP_N:
            break

        ranked.append((place, msg))
        last_votes = votes

    if winner_role_id is not None:
        announced_winner_ids = {
            msg.author.id
            for _, msg in ranked
        }

        await update_winner_roles(
            guild=sub_ch.guild,
            role_id=winner_role_id,
            current_winner_ids=announced_winner_ids,
            state_file=winner_state_file,
        )

    lines = []

    if ping_role_id is not None:
        lines.append(f"<@&{ping_role_id}>")
        lines.append("")

    lines.append(title_text)

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
            lines.append(f"By **{m.author.mention}**")

    text = "\n".join(lines)

    await res_ch.send(text)
    await sub_ch.send(text)

    image_blobs: list[tuple[str, bytes]] = []

    for i, (_, msg) in enumerate(ranked):

        att = next((a for a in msg.attachments if is_image_attachment(a)), None)
        if att:
            try:
                data = await att.read()
                if data:
                    image_blobs.append((f"{i+1}_{att.filename}", data))
            except Exception:
                pass

    if image_blobs:

        files_res = [discord.File(BytesIO(d), filename=n) for n, d in image_blobs]
        files_sub = [discord.File(BytesIO(d), filename=n) for n, d in image_blobs]

        await res_ch.send(files=files_res)
        await sub_ch.send(files=files_sub)

    if post_non_pro_notice and now.weekday() in NON_PRO_WEEKDAYS:
        await sub_ch.send(NON_PRO_MESSAGE)

async def run_motd_announcement(bot_obj: commands.Bot, use_last24h: bool = False):
    now = dt.datetime.now(TIMEZONE)
    start, end = (get_window_last24h(now) if use_last24h else get_window_daily(now))

    await run_ranked_announcement(
        bot_obj=bot_obj,
        start=start,
        end=end,
        title_text="# Congratulations to our :medal: MODEL OF THE DAY :medal: winners!",
        no_winners_text="# No winners today",
        post_non_pro_notice=True,
        ping_role_id=MODEL_OF_THE_DAY_ROLE_ID,
        winner_role_id=(None if use_last24h else WINNER_OF_THE_DAY_ROLE_ID),
    )

async def run_motw_announcement(bot_obj: commands.Bot):
    now = dt.datetime.now(TIMEZONE)
    start, end = get_window_weekly(now)

    await run_ranked_announcement(
        bot_obj=bot_obj,
        start=start,
        end=end,
        title_text="# Congratulations to our :medal: MODEL OF THE WEEK :medal: winners!",
        no_winners_text="# No weekly winners",
        post_non_pro_notice=False,
        winner_role_id=WINNER_OF_THE_WEEK_ROLE_ID,
        winner_state_file=WINNER_WEEK_STATE_FILE,
    )

async def run_motm_announcement(bot_obj: commands.Bot):
    now = dt.datetime.now(TIMEZONE)
    start, end = get_window_monthly(now)

    await run_ranked_announcement(
        bot_obj=bot_obj,
        start=start,
        end=end,
        title_text="# Congratulations to our :medal: MODEL OF THE MONTH :medal: winners!",
        no_winners_text="# No monthly winners",
        post_non_pro_notice=False,
        winner_role_id=(WINNER_OF_THE_MONTH_ROLE_ID or None),
        winner_state_file=WINNER_MONTH_STATE_FILE,
    )

@tasks.loop(time=dt.time(hour=DAILY_POST_HOUR, minute=DAILY_POST_MINUTE, tzinfo=TIMEZONE))
async def announce_daily():
    await bot.wait_until_ready()
    await run_motd_announcement(bot, use_last24h=False)

@tasks.loop(time=dt.time(hour=WEEKLY_POST_HOUR, minute=WEEKLY_POST_MINUTE, tzinfo=TIMEZONE))
async def announce_weekly():
    await bot.wait_until_ready()

    now = dt.datetime.now(TIMEZONE)

    if now.weekday() == 6:
        await run_motw_announcement(bot)

@tasks.loop(time=dt.time(hour=MONTHLY_POST_HOUR, minute=MONTHLY_POST_MINUTE, tzinfo=TIMEZONE))
async def announce_monthly():
    await bot.wait_until_ready()

    now = dt.datetime.now(TIMEZONE)

    if now.day == 1:
        await run_motm_announcement(bot)

@bot.command(name="motdtest")
async def motdtest(ctx: commands.Context):
    if ctx.author.id not in DEV_USER_IDS:
        return

    await run_motd_announcement(bot, use_last24h=True)

@bot.command(name="motdroletest")
async def motdroletest(ctx: commands.Context):
    if ctx.author.id not in DEV_USER_IDS:
        return

    sub_ch = bot.get_channel(SUBMISSIONS_CHANNEL_ID)
    if not sub_ch:
        return

    now = dt.datetime.now(TIMEZONE)
    start, end = get_window_last24h(now)

    best_by_author: dict[int, discord.Message] = {}

    async for msg in sub_ch.history(
        after=start,
        before=end,
        limit=None,
        oldest_first=True,
    ):
        if msg.author.bot or msg.webhook_id:
            continue

        votes = score_message(msg)
        if votes <= 0:
            continue

        author_id = msg.author.id
        current_best = best_by_author.get(author_id)

        if (
            current_best is None
            or votes > score_message(current_best)
            or (
                votes == score_message(current_best)
                and msg.created_at < current_best.created_at
            )
        ):
            best_by_author[author_id] = msg

    entries = list(best_by_author.values())
    entries.sort(key=lambda m: (-score_message(m), m.created_at))

    ranked: list[tuple[int, discord.Message]] = []
    place = 0
    last_votes = None

    for msg in entries:
        votes = score_message(msg)

        if last_votes is None or votes < last_votes:
            place += 1

        if place > TOP_N:
            break

        ranked.append((place, msg))
        last_votes = votes

    winner_ids = {
        msg.author.id
        for _, msg in ranked
    }

    await update_winner_roles(
        guild=sub_ch.guild,
        role_id=WINNER_OF_THE_DAY_ROLE_ID,
        current_winner_ids=winner_ids,
        state_file=WINNER_STATE_FILE,
    )

@bot.command(name="motwtest")
async def motwtest(ctx: commands.Context):
    if ctx.author.id not in DEV_USER_IDS:
        return

    now = dt.datetime.now(TIMEZONE)
    start, end = get_window_weekly(now)
    await run_ranked_announcement(
        bot_obj=bot, start=start, end=end,
        title_text="# Congratulations to our :medal: MODEL OF THE WEEK :medal: winners!",
        no_winners_text="# No weekly winners",
        post_non_pro_notice=False,
    )

@bot.command(name="motwroletest")
async def motwroletest(ctx: commands.Context):
    if ctx.author.id not in DEV_USER_IDS:
        return

    sub_ch = bot.get_channel(SUBMISSIONS_CHANNEL_ID)
    if not sub_ch:
        return

    now = dt.datetime.now(TIMEZONE)
    start, end = get_window_weekly(now)

    best_by_author: dict[int, discord.Message] = {}

    async for msg in sub_ch.history(
        after=start,
        before=end,
        limit=None,
        oldest_first=True,
    ):
        if msg.author.bot or msg.webhook_id:
            continue

        votes = score_message(msg)
        if votes <= 0:
            continue

        author_id = msg.author.id
        current_best = best_by_author.get(author_id)

        if (
            current_best is None
            or votes > score_message(current_best)
            or (
                votes == score_message(current_best)
                and msg.created_at < current_best.created_at
            )
        ):
            best_by_author[author_id] = msg

    entries = list(best_by_author.values())
    entries.sort(key=lambda m: (-score_message(m), m.created_at))

    ranked: list[tuple[int, discord.Message]] = []
    place = 0
    last_votes = None

    for msg in entries:
        votes = score_message(msg)

        if last_votes is None or votes < last_votes:
            place += 1

        if place > TOP_N:
            break

        ranked.append((place, msg))
        last_votes = votes

    winner_ids = {msg.author.id for _, msg in ranked}

    await update_winner_roles(
        guild=sub_ch.guild,
        role_id=WINNER_OF_THE_WEEK_ROLE_ID,
        current_winner_ids=winner_ids,
        state_file=WINNER_WEEK_STATE_FILE,
    )

@bot.command(name="motmtest")
async def motmtest(ctx: commands.Context):
    if ctx.author.id not in DEV_USER_IDS:
        return

    await run_motm_announcement(bot)

@bot.command(name="motmroletest")
async def motmroletest(ctx: commands.Context):
    if ctx.author.id not in DEV_USER_IDS:
        return

    sub_ch = bot.get_channel(SUBMISSIONS_CHANNEL_ID)
    if not sub_ch:
        return

    if not WINNER_OF_THE_MONTH_ROLE_ID:
        await ctx.send("Set WINNER_OF_THE_MONTH_ROLE_ID in motd_bot.py first.")
        return

    now = dt.datetime.now(TIMEZONE)
    start, end = get_window_monthly(now)

    best_by_author: dict[int, discord.Message] = {}

    async for msg in sub_ch.history(
        after=start,
        before=end,
        limit=None,
        oldest_first=True,
    ):
        if msg.author.bot or msg.webhook_id:
            continue

        votes = score_message(msg)
        if votes <= 0:
            continue

        author_id = msg.author.id
        current_best = best_by_author.get(author_id)

        if (
            current_best is None
            or votes > score_message(current_best)
            or (
                votes == score_message(current_best)
                and msg.created_at < current_best.created_at
            )
        ):
            best_by_author[author_id] = msg

    entries = list(best_by_author.values())
    entries.sort(key=lambda m: (-score_message(m), m.created_at))

    ranked: list[tuple[int, discord.Message]] = []
    place = 0
    last_votes = None

    for msg in entries:
        votes = score_message(msg)

        if last_votes is None or votes < last_votes:
            place += 1

        if place > TOP_N:
            break

        ranked.append((place, msg))
        last_votes = votes

    winner_ids = {msg.author.id for _, msg in ranked}

    await update_winner_roles(
        guild=sub_ch.guild,
        role_id=WINNER_OF_THE_MONTH_ROLE_ID,
        current_winner_ids=winner_ids,
        state_file=WINNER_MONTH_STATE_FILE,
    )

if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN environment variable first")

bot.run(TOKEN)