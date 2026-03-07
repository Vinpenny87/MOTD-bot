import os
import datetime as dt
from zoneinfo import ZoneInfo
from io import BytesIO

import aiohttp
import discord
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN")

# ====================
# CONFIG
# ====================
SUBMISSIONS_CHANNEL_ID = 1197939941372608532
RESULTS_CHANNEL_ID = 983134844492079154

VOTE_EMOJI = "<:upvote:962050161771696148>"
TIMEZONE = ZoneInfo("Europe/Vienna")

POST_HOUR = 17
POST_MINUTE = 0

TOP_N = 3
EXCLUDE_BOT_VOTE = False

DEV_USER_IDS = {
    1097539138959462471,
    582786439763329024,
}

# Monday + Thursday
NON_PRO_WEEKDAYS = {0, 3}

NON_PRO_MESSAGE = (
    "# Today is a NON-PRO day! Any minis made using pro features will be deleted!"
)


# ====================
# HELPERS
# ====================
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


def get_window_scheduled(now: dt.datetime):
    end = now.replace(hour=POST_HOUR, minute=POST_MINUTE, second=0, microsecond=0)
    start = end - dt.timedelta(days=1)
    return start, end


def get_window_last24h(now: dt.datetime):
    end = now
    start = end - dt.timedelta(days=1)
    return start, end


def get_embed_image(msg: discord.Message) -> str | None:
    for e in msg.embeds:
        if e.image and e.image.url:
            return e.image.url
        if e.thumbnail and e.thumbnail.url:
            return e.thumbnail.url
    return None


async def download_bytes(url: str) -> bytes | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                return await resp.read()
    except Exception:
        return None


# ====================
# BOT
# ====================
class MotDBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.reactions = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        announce_winners.start()


bot = MotDBot()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.webhook_id:
        return

    # Only react to submissions that have an image attachment or image embed preview
    if message.channel.id == SUBMISSIONS_CHANNEL_ID:
        has_image = (
            any(is_image_attachment(att) for att in message.attachments)
            or any(
                (e.image and e.image.url) or (e.thumbnail and e.thumbnail.url)
                for e in message.embeds
            )
        )

        if not has_image:
            return  # ignore text-only messages completely

        try:
            if not any(str(r.emoji) == VOTE_EMOJI for r in message.reactions):
                await message.add_reaction(VOTE_EMOJI)
        except Exception:
            pass

    await bot.process_commands(message)


async def run_motd_announcement(bot_obj: commands.Bot, use_last24h: bool = False):
    sub_ch = bot_obj.get_channel(SUBMISSIONS_CHANNEL_ID)
    res_ch = bot_obj.get_channel(RESULTS_CHANNEL_ID)

    if not sub_ch or not res_ch:
        return

    now = dt.datetime.now(TIMEZONE)
    start, end = (get_window_last24h(now) if use_last24h else get_window_scheduled(now))

    best_by_author: dict[int, discord.Message] = {}

    async for msg in sub_ch.history(after=start, before=end):
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
        text = "# No winners today"
        await res_ch.send(text)
        await sub_ch.send(text)

        # Non-pro notice AFTER the images (there are none) – still post after the main announcement
        if now.weekday() in NON_PRO_WEEKDAYS:
            await res_ch.send(NON_PRO_MESSAGE)
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

    # -------- TEXT POST (grouped so ties don't repeat the "In 2nd..." line) --------
    lines = ["# Congratulations to our :medal: MODEL OF THE DAY :medal: winners!"]

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

    await res_ch.send(text)
    await sub_ch.send(text)

    # -------- IMAGE POST --------
    image_blobs: list[tuple[str, bytes]] = []

    for i, (_, msg) in enumerate(ranked):
        # Prefer first real image attachment only
        att = next((a for a in msg.attachments if is_image_attachment(a)), None)
        if att:
            try:
                data = await att.read()
                if data:
                    image_blobs.append((f"{i+1}_{att.filename}", data))
                continue
            except Exception:
                pass

        # Fallback: embed preview image/thumbnail
        embed_img = get_embed_image(msg)
        if embed_img:
            data = await download_bytes(embed_img)
            if data:
                image_blobs.append((f"{i+1}_embed.png", data))

    if image_blobs:
        files_res = [discord.File(BytesIO(d), filename=n) for n, d in image_blobs]
        files_sub = [discord.File(BytesIO(d), filename=n) for n, d in image_blobs]
        await res_ch.send(files=files_res)
        await sub_ch.send(files=files_sub)

    # -------- NON-PRO DAY NOTICE (AFTER images) --------
    if now.weekday() in NON_PRO_WEEKDAYS:
        await res_ch.send(NON_PRO_MESSAGE)
        await sub_ch.send(NON_PRO_MESSAGE)


@tasks.loop(time=dt.time(hour=POST_HOUR, minute=POST_MINUTE, tzinfo=TIMEZONE))
async def announce_winners():
    await bot.wait_until_ready()
    await run_motd_announcement(bot, use_last24h=False)


@bot.command(name="motdtest")
async def motdtest(ctx: commands.Context):
    if ctx.author.id not in DEV_USER_IDS:
        return

    await run_motd_announcement(bot, use_last24h=True)


if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN environment variable first")

bot.run(TOKEN)