import os
import logging
from datetime import datetime, timezone
import asyncio

import discord
from dotenv import load_dotenv
from aiohttp import web

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
RELAY_CHANNEL_ID = int(os.getenv("RELAY_CHANNEL_ID", "0"))   # Channel in your private relay server
MY_USER_ID = int(os.getenv("MY_USER_ID", "0"))               # Your numeric Discord user ID

def _parse_id_list(raw: str) -> set[int]:
    return {int(x.strip()) for x in (raw or "").split(",") if x.strip().isdigit()}

# Optional filters
WATCH_GUILD_IDS        = _parse_id_list(os.getenv("WATCH_GUILD_IDS", ""))          # guild allowlist
WATCH_TEXT_CHANNEL_IDS = _parse_id_list(os.getenv("WATCH_TEXT_CHANNEL_IDS", ""))   # “access gained” watch
WATCH_VOICE_CHANNEL_IDS= _parse_id_list(os.getenv("WATCH_VOICE_CHANNEL_IDS", ""))  # voice watch

# --- logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.FileHandler("discord.log", encoding="utf-8", mode="w")]
)
log = logging.getLogger("join-reporter")

# --- intents (no message_content needed) ---
intents = discord.Intents.default()
intents.guilds = True
intents.members = True          # REQUIRED for on_member_join
intents.voice_states = True     # only matters if you use the voice watcher below

client = discord.Client(intents=intents)

# -------- helpers --------
def now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

async def relay(text: str):
    ch = client.get_channel(RELAY_CHANNEL_ID)
    if ch is None:
        # fetch at least once if not cached
        try:
            ch = await client.fetch_channel(RELAY_CHANNEL_ID)
        except Exception:
            log.error("Relay channel not found (id=%s)", RELAY_CHANNEL_ID)
            return

    if isinstance(ch, discord.TextChannel):
        try:
            await ch.send(text)
        except discord.Forbidden:
            log.warning("No permission to send in relay channel %s", ch.id)
    else:
        log.error("Relay channel wrong type (id=%s)", RELAY_CHANNEL_ID)

async def guild_is_watched(guild: discord.Guild) -> bool:
    if WATCH_GUILD_IDS and guild.id not in WATCH_GUILD_IDS:
        return False
    # Only report if YOU are also in the guild
    me = guild.get_member(MY_USER_ID)
    if me:
        return True
    try:
        await guild.fetch_member(MY_USER_ID)
        return True
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return False

# -------- Discord events --------
@client.event
async def on_ready():
    gids = ", ".join(f"{g.name}({g.id})" for g in client.guilds)
    print(f"✅ Ready as {client.user} | Watching {len(client.guilds)} guilds: {gids}")
    try:
        ch = client.get_channel(RELAY_CHANNEL_ID) or await client.fetch_channel(RELAY_CHANNEL_ID)
        print(f"Relay channel: {getattr(ch, 'name', 'NOT FOUND')} ({RELAY_CHANNEL_ID})")
    except Exception:
        print(f"Relay channel NOT FOUND ({RELAY_CHANNEL_ID})")

@client.event
async def on_member_join(member: discord.Member):
    if not await guild_is_watched(member.guild):
        return
    await relay(f"{member.name} joined : {member.guild.name} at {now_utc_str()}")

# OPTIONAL: detect when a user gains text-channel access (role change)
@client.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if not WATCH_TEXT_CHANNEL_IDS or not await guild_is_watched(after.guild):
        return
    for ch_id in WATCH_TEXT_CHANNEL_IDS:
        ch = after.guild.get_channel(ch_id)
        if not isinstance(ch, (discord.TextChannel, discord.ForumChannel, discord.CategoryChannel)):
            continue
        try:
            before_can = ch.permissions_for(before).view_channel
            after_can  = ch.permissions_for(after).view_channel
        except Exception:
            continue
        if not before_can and after_can:
            await relay(f"{after.name} gained access to : #{ch.name} in {after.guild.name} at {now_utc_str()}")

# OPTIONAL: watch voice channel join/leave/move for selected channels
@client.event
async def on_voice_state_update(member, before, after):
    if not WATCH_VOICE_CHANNEL_IDS or not await guild_is_watched(member.guild):
        return

    def in_watch(vch): return bool(vch and vch.id in WATCH_VOICE_CHANNEL_IDS)

    if after.channel != before.channel:
        if in_watch(after.channel) and not in_watch(before.channel):
            await relay(f"{member.name} joined voice : #{after.channel.name} in {member.guild.name} at {now_utc_str()}")
        elif in_watch(before.channel) and not in_watch(after.channel):
            await relay(f"{member.name} left voice : #{before.channel.name} in {member.guild.name} at {now_utc_str()}")
        elif in_watch(before.channel) and in_watch(after.channel):
            await relay(f"{member.name} moved voice : #{before.channel.name} → #{after.channel.name} in {member.guild.name} at {now_utc_str()}")

# -------- tiny web server for Render + UptimeRobot --------
async def _health(_):
    return web.Response(text="ok")

async def run_web():
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))  # Render injects PORT
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    await asyncio.Event().wait()  # keep alive

async def main():
    if not TOKEN or not RELAY_CHANNEL_ID or not MY_USER_ID:
        raise SystemExit("Set DISCORD_TOKEN, RELAY_CHANNEL_ID, and MY_USER_ID in environment (.env on local).")
    tasks = [client.start(TOKEN)]
    # On Render Web Service, PORT is set. Locally you can export PORT=8080 to test.
    tasks.append(run_web())
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())

