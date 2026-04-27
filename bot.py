import discord
from discord.ext import commands, tasks
import os
import requests
import asyncio
import re
import json
import pytz
import aiohttp
from aiohttp import web
import aiohttp_session
from aiohttp_session.cookie_storage import EncryptedCookieStorage
import base64
from cryptography import fernet
from urllib.parse import urlencode
from dotenv import load_dotenv
from eas_audio import generate_eas_message, generate_normal_speech, get_available_voices
from datetime import datetime
import sys

# Load configuration from .env
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
BOT_VERSION = "1.0"
BOT_OWNER_ID = int(os.getenv('BOT_OWNER_ID', '1365401272798281850'))
DEFAULT_VOICE = "ScanSoft Tom_Full_22kHz"

# JSON Database Setup
DB_FILE = "servers.json"

# Define the archive directory outside of the bot folder
ARCHIVE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alerts_archive"))

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            try:
                data = json.load(f)
                if "__global__" not in data:
                    data["__global__"] = {"voice": DEFAULT_VOICE}
                return data
            except json.JSONDecodeError:
                return {"__global__": {"voice": DEFAULT_VOICE}}
    return {"__global__": {"voice": DEFAULT_VOICE}}

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

servers_db = load_db()

# Configure bot
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix='fco!', intents=intents, help_command=None, owner_id=BOT_OWNER_ID)

# --- State Variables ---
seen_alerts = set()
active_alerts_cache = {} # Dict of zone -> list of alerts
alert_history = {}       # Dict of zone -> list of alerts

# Life-Threatening Alerts that will trigger a @everyone ping
URGENT_EVENTS = [
    "Tornado Warning", "Flash Flood Warning", "Severe Thunderstorm Warning",
    "Tsunami Warning", "Civil Emergency Message", "Evacuation Immediate",
    "Shelter in Place Warning", "AMBER Alert", "Nuclear Power Plant Warning",
    "Hazardous Materials Warning", "Fire Warning"
]

# --- Web Server (ENDEC Dashboard) ---
WEB_SESSION_KEY = os.urandom(32)

async def discord_login(request):
    client_id, redirect_uri = os.getenv("DISCORD_CLIENT_ID"), os.getenv("REDIRECT_URI")
    if not client_id or not redirect_uri: return web.Response(status=500, text="OAuth2 not configured.")
    oauth_url = f"https://discord.com/api/oauth2/authorize?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code&scope=identify"
    raise web.HTTPFound(oauth_url)

async def discord_callback(request):
    code = request.query.get("code")
    if not code: return web.Response(status=400, text="Missing code.")
    client_id, client_secret, redirect_uri = os.getenv("DISCORD_CLIENT_ID"), os.getenv("DISCORD_CLIENT_SECRET"), os.getenv("REDIRECT_URI")
    token_url = "https://discord.com/api/oauth2/token"
    data = {"client_id": client_id, "client_secret": client_secret, "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
    async with aiohttp.ClientSession() as session:
        async with session.post(token_url, data=data) as resp:
            if resp.status != 200: return web.Response(status=400, text="Auth failed.")
            token_data = await resp.json()
            access_token = token_data.get("access_token")
        async with session.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {access_token}"}) as resp:
            if resp.status != 200: return web.Response(status=400, text="Fetch failed.")
            user_id = (await resp.json()).get("id")
    if int(user_id) != BOT_OWNER_ID: return web.Response(status=403, text="Unauthorized.")
    web_session = await aiohttp_session.get_session(request)
    web_session["authenticated"], web_session["user_id"] = True, user_id
    raise web.HTTPFound('/')

async def require_auth(request):
    session = await aiohttp_session.get_session(request)
    if not session.get("authenticated"): raise web.HTTPFound('/login')

async def web_index(request):
    await require_auth(request)
    archive_items = [f for f in sorted(os.listdir(ARCHIVE_DIR), reverse=True) if f.endswith(".mp3")][:20] if os.path.exists(ARCHIVE_DIR) else []
    archive_html = "".join([f'<li><strong>{i.replace("_", " ")}</strong><br><audio controls src="/archive/{i}"></audio></li><hr>' for i in archive_items]) or "<li>No alerts.</li>"
    html = f"<html><body style='font-family:monospace;background:#121212;color:#00ff00;padding:20px'><h1>EAS ENDEC v{BOT_VERSION}</h1><p>Servers: {len(servers_db)-1}</p><form action='/test' method='post'><button type='submit'>Global Test</button></form><form action='/stop' method='post'><button type='submit'>Stop All</button></form><h2>Archive</h2><ul>{archive_html}</ul></body></html>"
    return web.Response(text=html, content_type='text/html')

async def web_serve_archive(request):
    fn = request.match_info.get('filename')
    path = os.path.join(ARCHIVE_DIR, fn)
    if os.path.exists(path) and fn.endswith(".mp3"): return web.FileResponse(path)
    return web.Response(status=404)

async def trigger_global_test(trigger_source="Web UI"):
    print(f"Global test: {trigger_source}")
    pre = f"This is a test of the E A S discord bot, issued via the {trigger_source}."
    msg = "This is a test of the Emergency Alert System. This is only a test."
    voice = servers_db.get("__global__", {}).get("voice", DEFAULT_VOICE)
    try:
        import shutil
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        base_fn = f"{ARCHIVE_DIR}/global_test_alert_{ts}.mp3"
        await asyncio.to_thread(generate_eas_message, msg, base_fn, pre, voice=voice)
        for gid, cfg in servers_db.items():
            if gid.startswith("__"): continue
            guild = bot.get_guild(int(gid))
            if not guild: continue
            vc = guild.voice_client
            if not vc or not vc.is_connected():
                vcid = cfg.get("voice_channel_id")
                if vcid:
                    chan = guild.get_channel(vcid)
                    if chan:
                        try: vc = await chan.connect(self_deaf=True)
                        except: continue
            if not vc or not vc.is_connected() or vc.is_playing(): continue
            gfn = f"{ARCHIVE_DIR}/global_test_{gid}_{ts}.mp3"
            shutil.copy(base_fn, gfn)
            vc.play(discord.FFmpegPCMAudio(source=gfn))
    except Exception as e: print(f"Global test error: {e}")

async def web_trigger_test(request): bot.loop.create_task(trigger_global_test()); return web.HTTPFound('/')
async def web_stop_audio(request):
    for vc in bot.voice_clients:
        if vc.is_playing(): vc.stop()
    return web.HTTPFound('/')
async def web_force_poll(request): bot.loop.create_task(check_nws_alerts()); return web.HTTPFound('/')

async def start_web_server():
    app = web.Application()
    aiohttp_session.setup(app, EncryptedCookieStorage(WEB_SESSION_KEY))
    app.add_routes([web.get('/', web_index), web.get('/login', discord_login), web.get('/callback', discord_callback), web.get('/archive/{filename}', web_serve_archive), web.post('/test', web_trigger_test), web.post('/stop', web_stop_audio), web.post('/poll', web_force_poll)])
    runner = web.AppRunner(app)
    await runner.setup()
    import ssl
    ssl_ctx = None
    if os.path.exists("cert.pem") and os.path.exists("key.pem"):
        try:
            ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_ctx.load_cert_chain("cert.pem", "key.pem")
        except: pass
    await web.TCPSite(runner, '0.0.0.0', 2424, ssl_context=ssl_ctx).start()

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')
    if not os.path.exists(ARCHIVE_DIR): os.makedirs(ARCHIVE_DIR)
    bot.loop.create_task(start_web_server())
    for gid, cfg in servers_db.items():
        if gid.startswith("__"): continue
        vcid = cfg.get("voice_channel_id")
        if vcid:
            chan = bot.get_channel(vcid)
            if chan:
                try: await chan.connect(self_deaf=True)
                except: pass
    if not check_nws_alerts.is_running(): check_nws_alerts.start()

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.NotOwner):
        try: await ctx.message.delete(); await ctx.author.send("Owner only command.")
        except: pass
    elif isinstance(error, commands.MissingPermissions):
        try: await ctx.message.delete(); await ctx.author.send("Missing permissions.")
        except: pass
    elif isinstance(error, commands.CommandNotFound): pass
    else: print(f"Error: {error}")

@bot.command()
async def help(ctx):
    embed = discord.Embed(title="📡 AliEAS v1.0", color=discord.Color.blue())
    embed.add_field(name="🎙️ General", value="`fco!join`, `fco!leave`, `fco!active`, `fco!history`, `fco!weather [ZIP]`, `fco!stop`, `fco!status`, `fco!voices`, `fco!voice`", inline=False)
    embed.add_field(name="⚙️ Admin", value="`fco!setup <ZIP>`, `fco!test`, `fco!setvoice <Name>`", inline=False)
    if await bot.is_owner(ctx.author):
        embed.add_field(name="👑 Owner", value="`fco!testg`, `fco!pipe`, `fco!serverslist`, `fco!freshpull`, `fco!restart`, `fco!shutdown`, `fco!getlogs`, `fco!globalvoice <Name>`", inline=False)
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx, zip_code: str = None):
    if not zip_code or len(zip_code) != 5: return await ctx.send("Usage: `fco!setup 81240`")
    await ctx.send(f"🔍 Setting up for {zip_code}...")
    try:
        zres = requests.get(f"http://api.zippopotam.us/us/{zip_code}").json()
        lat, lon = zres['places'][0]['latitude'], zres['places'][0]['longitude']
        pn = f"{zres['places'][0]['place name']}, {zres['places'][0]['state abbreviation']}"
        pres = requests.get(f"https://api.weather.gov/points/{lat},{lon}", headers={"User-Agent": "EASBot"}).json()
        zone = pres['properties']['county'].split('/')[-1]
    except: return await ctx.send("❌ Setup failed.")
    if not ctx.author.voice: return await ctx.send("❌ Join a VC first.")
    servers_db[str(ctx.guild.id)] = {"zone": zone, "place_name": pn, "text_channel_id": ctx.channel.id, "voice_channel_id": ctx.author.voice.channel.id, "guild_name": ctx.guild.name, "zip_code": zip_code, "voice": DEFAULT_VOICE}
    save_db(servers_db)
    if not ctx.voice_client: await ctx.author.voice.channel.connect(self_deaf=True)
    await ctx.send(f"✅ Setup Complete for {pn}!")

@bot.command()
async def voices(ctx):
    v = get_available_voices()
    await ctx.send(f"**Available SAPI5 Voices:**\n" + "\n".join([f"- `{x}`" for x in v]))

@bot.command()
@commands.has_permissions(administrator=True)
async def setvoice(ctx, *, voice_name: str):
    v = get_available_voices()
    if voice_name not in v: return await ctx.send("❌ Voice not found. Use `fco!voices` to see names.")
    gid = str(ctx.guild.id)
    if gid not in servers_db: return await ctx.send("❌ Run `fco!setup` first.")
    servers_db[gid]["voice"] = voice_name
    save_db(servers_db)
    await ctx.send(f"✅ Voice for this server set to `{voice_name}`.")

@bot.command()
async def voice(ctx):
    gid = str(ctx.guild.id)
    v = servers_db.get(gid, {}).get("voice", DEFAULT_VOICE)
    await ctx.send(f"🎙️ Current voice for this server: `{v}`")

@bot.command()
@commands.is_owner()
async def globalvoice(ctx, *, voice_name: str):
    v = get_available_voices()
    if voice_name not in v: return await ctx.send("❌ Voice not found.")
    servers_db["__global__"]["voice"] = voice_name
    save_db(servers_db)
    await ctx.send(f"👑 Global owner voice set to `{voice_name}`.")

@bot.command()
async def join(ctx):
    if ctx.author.voice:
        if not ctx.voice_client: await ctx.author.voice.channel.connect(self_deaf=True)
        else: await ctx.voice_client.move_to(ctx.author.voice.channel)
        await ctx.send("Joined.")
@bot.command()
async def leave(ctx):
    if ctx.voice_client: await ctx.voice_client.disconnect(); await ctx.send("Left.")

@bot.command()
async def test(ctx):
    gid = str(ctx.guild.id)
    cfg = servers_db.get(gid, {})
    if not cfg: return await ctx.send("Run setup.")
    vc = ctx.voice_client
    if not vc:
        chan = ctx.guild.get_channel(cfg.get("voice_channel_id"))
        if chan: vc = await chan.connect(self_deaf=True)
    if not vc or vc.is_playing(): return await ctx.send("Cannot play.")
    await ctx.send("Generating test...")
    msg = f"This is a test of the Emergency Alert System for zone {cfg['zone']}."
    voice = cfg.get("voice", DEFAULT_VOICE)
    try:
        ts = datetime.now().strftime("%Y%M%S")
        fn = f"{ARCHIVE_DIR}/test_{gid}_{ts}.mp3"
        await asyncio.to_thread(generate_eas_message, msg, fn, "This voice channel has been interrupted for the Emergency Alert System.", voice=voice)
        vc.play(discord.FFmpegPCMAudio(source=fn))
    except Exception as e: await ctx.send(f"Error: {e}")

@bot.command()
@commands.is_owner()
async def pipe(ctx):
    if not ctx.message.attachments: return await ctx.send("Upload file.")
    att = ctx.message.attachments[0]
    await ctx.send("Processing global pipe...")
    try:
        ts = datetime.now().strftime("%Y%M%S")
        tmp = f"temp_{ts}{os.path.splitext(att.filename)[1]}"
        await att.save(tmp)
        from pydub import AudioSegment
        from EASGen import EASGen
        from eas_audio import apply_radio_filter, _generate_sapi5
        user_audio = await asyncio.to_thread(AudioSegment.from_file, tmp)
        gv = servers_db.get("__global__", {}).get("voice", DEFAULT_VOICE)
        if os.path.exists(tmp): os.remove(tmp)
        # Generate broadcast with global voice
        intro_fn = f"temp_intro_{ts}.wav"
        await asyncio.to_thread(_generate_sapi5, "This voice channel has been interrupted for the Emergency Alert System.", intro_fn, gv)
        intro = apply_radio_filter(AudioSegment.from_wav(intro_fn))
        if os.path.exists(intro_fn): os.remove(intro_fn)
        h, a, e = EASGen.genHeader("ZCZC-WXR-EAN-008043+0015-1231234-KDEN/NWS-"), EASGen.genATTN(8), EASGen.genEOM()
        def compile(): return intro + AudioSegment.silent(1000) + h + AudioSegment.silent(500) + a + AudioSegment.silent(1000) + apply_radio_filter(user_audio) + AudioSegment.silent(1000) + e
        final = await asyncio.to_thread(compile)
        base_fn = f"{ARCHIVE_DIR}/pipe_base_{ts}.mp3"
        await asyncio.to_thread(final.export, base_fn, format="mp3")
        import shutil
        for gid, cfg in servers_db.items():
            if gid.startswith("__"): continue
            guild = bot.get_guild(int(gid))
            if not guild or not guild.voice_client or not guild.voice_client.is_connected(): continue
            gfn = f"{ARCHIVE_DIR}/pipe_{gid}_{ts}.mp3"
            shutil.copy(base_fn, gfn)
            guild.voice_client.play(discord.FFmpegPCMAudio(source=gfn))
        await ctx.send("Global pipe complete.")
    except Exception as e: await ctx.send(f"Error: {e}")

@bot.command()
async def weather(ctx, target_zip: str = None):
    gid = str(ctx.guild.id)
    cfg = servers_db.get(gid, {})
    zu = target_zip or cfg.get("zip_code")
    if not zu: return await ctx.send("Usage: `fco!weather <ZIP>`")
    v = cfg.get("voice", DEFAULT_VOICE)
    await ctx.send(f"🔍 Fetching forecast for {zu}...")
    try:
        zres = requests.get(f"http://api.zippopotam.us/us/{zu}").json()
        lat, lon = zres['places'][0]['latitude'], zres['places'][0]['longitude']
        pn = f"{zres['places'][0]['place name']}, {zres['places'][0]['state abbreviation']}"
        pres = requests.get(f"https://api.weather.gov/points/{lat},{lon}", headers={"User-Agent": "EASBot"}).json()
        furl, cwa = pres['properties']['forecast'], pres['properties']['cwa']
        periods = requests.get(furl, headers={"User-Agent": "EASBot"}).json()['properties']['periods']
        txt, spoken = "", f"Detailed forecast for {pn}. "
        for p in periods:
            line = f"**{p['name']}**: {p['detailedForecast']}\n"
            if len(txt) + len(line) < 1900: txt += line
            spoken += f"{p['name']}. {p['detailedForecast']} "
        spoken += " For the latest information, go to weather.gov."
        await ctx.send(embed=discord.Embed(title=f"🌤️ {pn}", description=txt[:2000], color=discord.Color.blue()))
        if ctx.voice_client and not ctx.voice_client.is_playing():
            fn = f"{ARCHIVE_DIR}/weather_{gid}_{datetime.now().strftime('%H%M%S')}.mp3"
            await asyncio.to_thread(generate_normal_speech, spoken, fn, voice=v)
            ctx.voice_client.play(discord.FFmpegPCMAudio(source=fn))
    except Exception as e: print(e); await ctx.send("Error.")

@bot.command(aliases=['testg'])
@commands.is_owner()
async def testglobal(ctx): await trigger_global_test("Bot Owner")

@bot.command()
@commands.is_owner()
async def serverslist(ctx):
    msg = "**Servers:**\n" + "\n".join([f"- {c.get('guild_name')}: {c.get('zone')}" for k,c in servers_db.items() if not k.startswith("__")])
    await ctx.send(msg[:2000])

@bot.command()
@commands.is_owner()
async def freshpull(ctx): await check_nws_alerts(); await ctx.send("Done.")
@bot.command()
@commands.is_owner()
async def restart(ctx): await bot.close(); os._exit(0)
@bot.command()
@commands.is_owner()
async def shutdown(ctx): await bot.close()
@bot.command()
@commands.is_owner()
async def getlogs(ctx):
    files = [discord.File(f) for f in ["logs/bot.log", "logs/bot_errors.log"] if os.path.exists(f)]
    if files: await ctx.send(files=files)
    else: await ctx.send("No logs.")

last_weekly_test_date = None
@tasks.loop(minutes=2.0)
async def check_nws_alerts():
    global last_weekly_test_date
    now = datetime.now(pytz.timezone("US/Mountain"))
    if now.weekday() == 2 and now.hour == 9 and now.minute >= 30:
        if last_weekly_test_date != now.strftime("%Y-%m-%d"):
            last_weekly_test_date = now.strftime("%Y-%m-%d")
            await trigger_global_test("Automated System")
    uz = set(c["zone"] for k,c in servers_db.items() if not k.startswith("__"))
    if not uz: return
    async with aiohttp.ClientSession() as session:
        for z in uz:
            try:
                async with session.get(f"https://api.weather.gov/alerts/active?zone={z}", headers={"User-Agent": "EASBot"}, timeout=15) as r:
                    if r.status == 200:
                        data = await r.json()
                        new_alerts = []
                        for f in data.get('features', []):
                            p = f.get('properties', {})
                            aid = p.get('id')
                            if aid and aid not in seen_alerts:
                                seen_alerts.add(aid)
                                new_alerts.append(p)
                        for gid, cfg in servers_db.items():
                            if gid.startswith("__") or cfg.get("zone") != z: continue
                            guild = bot.get_guild(int(gid))
                            if not guild: continue
                            tchan = guild.get_channel(cfg.get("text_channel_id"))
                            if tchan:
                                for a in new_alerts:
                                    color = discord.Color.red() if a.get('severity') in ["Extreme", "Severe"] else discord.Color.gold()
                                    embed = discord.Embed(title=f"🚨 {a.get('event')}", description=a.get('headline'), color=color)
                                    bot.loop.create_task(tchan.send(embed=embed))
                            vc = guild.voice_client
                            if vc and vc.is_connected() and not vc.is_playing() and new_alerts:
                                a = new_alerts[0]
                                speech = f"Alert: {a.get('headline')}. {a.get('description')}"
                                fn = f"{ARCHIVE_DIR}/alert_{gid}_{datetime.now().strftime('%H%M%S')}.mp3"
                                await asyncio.to_thread(generate_eas_message, speech, fn, "Interruption for EAS.", voice=cfg.get("voice", DEFAULT_VOICE))
                                vc.play(discord.FFmpegPCMAudio(source=fn))
            except: pass
    for gid, cfg in servers_db.items():
        if gid.startswith("__"): continue
        guild = bot.get_guild(int(gid))
        if guild:
            vc, vcid = guild.voice_client, cfg.get("voice_channel_id")
            if vcid:
                chan = guild.get_channel(vcid)
                if chan and (not vc or not vc.is_connected()):
                    try: await chan.connect(self_deaf=True)
                    except: pass

@check_nws_alerts.before_loop
async def before_check_nws_alerts(): await bot.wait_until_ready()

if __name__ == '__main__':
    if TOKEN: bot.run(TOKEN)
    else: print("No TOKEN.")
