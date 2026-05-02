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
import shutil
import logging
import socket

# Setup Logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger("AliEAS")

# --- Single Instance Lock ---
lock_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    lock_socket.bind(('127.0.0.1', 2425))
except socket.error:
    logger.error("Another instance of AliEAS is already running. Exiting.")
    sys.exit(1)

# Load configuration from .env
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
BOT_VERSION = "1.0"
BOT_OWNER_ID = int(os.getenv('BOT_OWNER_ID', '1365401272798281850'))
DEFAULT_VOICE = "ScanSoft Tom_Full_22kHz"

# JSON Database Setup
DB_FILE = "servers.json"
ARCHIVE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "alerts_archive"))

def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            try:
                data = json.load(f)
                if "__global__" not in data:
                    data["__global__"] = {"voice": DEFAULT_VOICE, "archive_channel_id": None}
                return data
            except json.JSONDecodeError:
                return {"__global__": {"voice": DEFAULT_VOICE, "archive_channel_id": None}}
    return {"__global__": {"voice": DEFAULT_VOICE, "archive_channel_id": None}}

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

servers_db = load_db()

# --- Central Repository Helper ---
async def archive_to_central_repo(title, description, file_path):
    """Uploads an alert and its audio to the central Discord repository."""
    channel_id = servers_db.get("__global__", {}).get("archive_channel_id")
    if not channel_id: return
    try:
        channel = await bot.fetch_channel(int(channel_id))
        clean_desc = description[:4000] + ("..." if len(description) > 4000 else "")
        embed = discord.Embed(title=f"📦 ARCHIVE: {title}", description=clean_desc, color=discord.Color.dark_grey(), timestamp=datetime.now())
        await channel.send(embed=embed, file=discord.File(file_path))
        logger.info(f"Successfully archived {title} to Discord.")
    except Exception as e: 
        logger.error(f"Central Repo Error: {e}")

# Configure bot
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix='fco!', intents=intents, help_command=None, owner_id=BOT_OWNER_ID)

# --- State Variables ---
seen_alerts = set()
URGENT_EVENTS = ["Tornado Warning", "Flash Flood Warning", "Severe Thunderstorm Warning", "Tsunami Warning", "Civil Emergency Message", "Evacuation Immediate", "Shelter in Place Warning", "AMBER Alert", "Nuclear Power Plant Warning", "Hazardous Materials Warning", "Fire Warning"]

# --- Web Server (ENDEC Dashboard) ---
WEB_SESSION_KEY = os.urandom(32)

async def discord_login(request):
    client_id, redirect_uri = os.getenv("DISCORD_CLIENT_ID"), os.getenv("REDIRECT_URI")
    oauth_url = f"https://discord.com/api/oauth2/authorize?client_id={client_id}&redirect_uri={redirect_uri}&response_type=code&scope=identify"
    raise web.HTTPFound(oauth_url)

async def discord_callback(request):
    code = request.query.get("code")
    client_id, client_secret, redirect_uri = os.getenv("DISCORD_CLIENT_ID"), os.getenv("DISCORD_CLIENT_SECRET"), os.getenv("REDIRECT_URI")
    async with aiohttp.ClientSession() as session:
        async with session.post("https://discord.com/api/oauth2/token", data={"client_id": client_id, "client_secret": client_secret, "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}) as resp: 
            token_data = await resp.json()
            access_token = token_data.get("access_token")
        async with session.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {access_token}"}) as resp:
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
    archive_items = [f for f in sorted(os.listdir(ARCHIVE_DIR), reverse=True) if f.endswith(".wav")] if os.path.exists(ARCHIVE_DIR) else []
    archive_html = "".join([f'<li><strong>{i.replace("_", " ")}</strong><br><audio controls src="/archive/{i}"></audio></li><hr>' for i in archive_items]) or "<li>No alerts.</li>"
    html = f"<html><body style='font-family:monospace;background:#121212;color:#00ff00;padding:20px'><h1>EAS ENDEC v{BOT_VERSION}</h1><p>Servers: {len(servers_db)-1}</p><form action='/test' method='post'><button type='submit'>Global Test</button></form><form action='/stop' method='post'><button type='submit'>Stop All</button></form><h2>Archive</h2><ul>{archive_html}</ul></body></html>"
    return web.Response(text=html, content_type='text/html')

async def web_serve_archive(request):
    fn = request.match_info.get('filename')
    path = os.path.join(ARCHIVE_DIR, fn)
    if os.path.exists(path) and fn.endswith(".wav"): return web.FileResponse(path)
    return web.Response(status=404)

async def trigger_global_test(trigger_source="Web UI"):
    logger.info(f"Global test triggered by: {trigger_source}")
    pre = f"This is a test of the E A S discord bot, issued via the {trigger_source}."
    msg = "This is a test of the Emergency Alert System. This is only a test."
    voice = servers_db.get("__global__", {}).get("voice", DEFAULT_VOICE)
    try:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        base_fn = os.path.abspath(f"{ARCHIVE_DIR}/global_test_alert_{ts}.wav")
        await asyncio.to_thread(generate_eas_message, msg, base_fn, pre, voice=voice)
        await archive_to_central_repo(f"Global Test Alert", msg, base_fn)
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
                        except Exception as e:
                            logger.error(f"Failed to connect to VC in {guild.name}: {e}")
                            continue
            if not vc or not vc.is_connected() or vc.is_playing(): continue
            gfn = os.path.abspath(f"{ARCHIVE_DIR}/global_test_{gid}_{ts}.wav")
            shutil.copy(base_fn, gfn)
            vc.play(discord.FFmpegPCMAudio(source=gfn))      
    except Exception as e:
        logger.error(f"Global test error: {e}")

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
    logger.info(f'Logged in as {bot.user.name}')
    if not os.path.exists(ARCHIVE_DIR): os.makedirs(ARCHIVE_DIR)
    if not os.path.exists("sounds"): os.makedirs("sounds")
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
    else: logger.error(f"Command Error: {error}")

@bot.command()
@commands.has_permissions(administrator=True)
async def introsound(ctx):
    """(Admin Only) Sets a server-specific intro sound for all alerts (attach a file)."""
    gid = str(ctx.guild.id)
    if gid not in servers_db: return await ctx.send("❌ Run `fco!setup` first.")
    if not ctx.message.attachments:
        servers_db[gid]["intro_path"] = None
        save_db(servers_db)
        await ctx.send("✅ Server intro sound cleared.")
        return
    att = ctx.message.attachments[0]
    ext = os.path.splitext(att.filename)[1].lower()
    if ext not in ['.wav', '.mp3', '.m4a', '.ogg']: return await ctx.send("❌ Unsupported format.")
    path = os.path.abspath(os.path.join("sounds", f"intro_{gid}{ext}"))
    await att.save(path)
    servers_db[gid]["intro_path"] = path
    save_db(servers_db)
    await ctx.send(f"✅ Server intro sound set to `{att.filename}`.")

@bot.command()
@commands.has_permissions(administrator=True)
async def outrosound(ctx):
    """(Admin Only) Sets a server-specific outro sound for all alerts (attach a file)."""
    gid = str(ctx.guild.id)
    if gid not in servers_db: return await ctx.send("❌ Run `fco!setup` first.")
    if not ctx.message.attachments:
        servers_db[gid]["outro_path"] = None
        save_db(servers_db)
        await ctx.send("✅ Server outro sound cleared.")
        return
    att = ctx.message.attachments[0]
    ext = os.path.splitext(att.filename)[1].lower()
    if ext not in ['.wav', '.mp3', '.m4a', '.ogg']: return await ctx.send("❌ Unsupported format.")
    path = os.path.abspath(os.path.join("sounds", f"outro_{gid}{ext}"))
    await att.save(path)
    servers_db[gid]["outro_path"] = path
    save_db(servers_db)
    await ctx.send(f"✅ Server outro sound set to `{att.filename}`.")

@bot.command()
@commands.is_owner()
async def archive(ctx, channel_id: str = None):
    if not channel_id: return await ctx.send("Usage: `fco!archive <ID>`")
    servers_db["__global__"]["archive_channel_id"] = channel_id
    save_db(servers_db)
    await ctx.send(f"✅ Archive set to <#{channel_id}>")

@bot.command()
async def help(ctx):
    embed = discord.Embed(title="📡 AliEAS v1.0", color=discord.Color.blue())
    embed.add_field(name="🎙️ General", value="`fco!join`, `fco!leave`, `fco!active`, `fco!history`, `fco!weather`, `fco!stop`, `fco!status`, `fco!voices`, `fco!voice`", inline=False)
    embed.add_field(name="⚙️ Admin", value="`fco!setup <ZIP>`, `fco!test`, `fco!setvoice <Name>`, `fco!introsound`, `fco!outrosound` ", inline=False)
    if await bot.is_owner(ctx.author):
        embed.add_field(name="👑 Owner", value="`fco!testg`, `fco!pipe`, `fco!archive <ID>`, `fco!serverslist`, `fco!freshpull`, `fco!restart`, `fco!shutdown`, `fco!getlogs`, `fco!globalvoice` ", inline=False)
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx, zip_code: str = None):
    if not zip_code or len(zip_code) != 5: return await ctx.send("Usage: `fco!setup 81240`")
    await ctx.send(f"🔍 Setting up for {zip_code}...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://api.zippopotam.us/us/{zip_code}") as r:
                zres = await r.json()
            lat, lon = zres['places'][0]['latitude'], zres['places'][0]['longitude']
            pn = f"{zres['places'][0]['place name']}, {zres['places'][0]['state abbreviation']}"
            async with session.get(f"https://api.weather.gov/points/{lat},{lon}", headers={"User-Agent": "EASBot"}) as r:
                pres = await r.json()
            zone = pres['properties']['county'].split('/')[-1]
    except: return await ctx.send("❌ Setup failed.")
    if not ctx.author.voice: return await ctx.send("❌ Join a VC first.")
    servers_db[str(ctx.guild.id)] = {"zone": zone, "place_name": pn, "text_channel_id": ctx.channel.id, "voice_channel_id": ctx.author.voice.channel.id, "guild_name": ctx.guild.name, "zip_code": zip_code, "voice": DEFAULT_VOICE, "intro_path": None, "outro_path": None}
    save_db(servers_db)
    if not ctx.voice_client: await ctx.author.voice.channel.connect(self_deaf=True)
    await ctx.send(f"✅ Setup Complete!")

@bot.command()
async def voices(ctx):
    await ctx.send(f"**Available SAPI5 Voices:**\n- " + "\n- ".join(get_available_voices()))

@bot.command()
@commands.has_permissions(administrator=True)
async def setvoice(ctx, *, voice_name: str):
    if voice_name not in get_available_voices(): return await ctx.send("❌ Voice not found.")
    gid = str(ctx.guild.id)
    if gid in servers_db:
        servers_db[gid]["voice"] = voice_name
        save_db(servers_db)
        await ctx.send(f"✅ Voice set to `{voice_name}`.")

@bot.command()
async def voice(ctx):
    gid = str(ctx.guild.id)
    v = servers_db.get(gid, {}).get("voice", DEFAULT_VOICE)
    await ctx.send(f"🎙️ Server voice: `{v}`")

@bot.command()
@commands.is_owner()
async def globalvoice(ctx, *, voice_name: str):
    if voice_name not in get_available_voices(): return await ctx.send("❌ Voice not found.")
    servers_db["__global__"]["voice"] = voice_name
    save_db(servers_db)
    await ctx.send(f"👑 Global voice set to `{voice_name}`.")

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
@commands.has_permissions(administrator=True)
async def test(ctx):
    gid = str(ctx.guild.id)
    cfg = servers_db.get(gid, {})
    if not cfg: return
    vc = ctx.voice_client
    if not vc:
        chan = ctx.guild.get_channel(cfg.get("voice_channel_id"))
        if chan: vc = await chan.connect(self_deaf=True)
    if not vc or vc.is_playing(): return await ctx.send("Voice client busy.")
    await ctx.send("Generating test...")
    msg = f"This is a test of the Emergency Alert System for zone {cfg['zone']}."
    try:
        ts = datetime.now().strftime("%Y%M%S")
        fn = os.path.abspath(f"{ARCHIVE_DIR}/test_{gid}_{ts}.wav")
        v = cfg.get("voice", DEFAULT_VOICE)
        logger.info(f"Generating test for {ctx.guild.name} using voice: {v}")
        await asyncio.to_thread(generate_eas_message, msg, fn, "This voice channel has been interrupted for the Emergency Alert System.", voice=v)
        await archive_to_central_repo(f"Test Alert ({ctx.guild.name})", msg, fn)
        vc.play(discord.FFmpegPCMAudio(source=fn))
        await ctx.send(f"Now playing the test message using `{v}`.")
    except Exception as e:
        logger.error(f"Test command error: {e}")
        await ctx.send(f"Error: {e}")

@bot.command()
@commands.is_owner()
async def pipe(ctx):
    if not ctx.message.attachments: return await ctx.send("Upload file.")
    att = ctx.message.attachments[0]
    logger.info(f"Manual pipe broadcast requested by {ctx.author}.")
    await ctx.send("Processing global pipe...")
    try:
        ts = datetime.now().strftime("%Y%M%S")
        tmp = os.path.abspath(f"temp_{ts}{os.path.splitext(att.filename)[1]}")
        await att.save(tmp)
        
        from pydub import AudioSegment
        from EASGen import EASGen
        from eas_audio import _generate_sapi5
        
        # Load user audio (Clean)
        user_audio = await asyncio.to_thread(AudioSegment.from_file, tmp)
        user_audio = user_audio.set_frame_rate(44100) if user_audio.frame_rate != 44100 else user_audio
        
        gv = servers_db.get("__global__", {}).get("voice", DEFAULT_VOICE)
        if os.path.exists(tmp): os.remove(tmp)
        
        # Generate intro with global voice
        intro_fn = os.path.abspath(f"temp_intro_{ts}.wav")
        await asyncio.to_thread(_generate_sapi5, "This voice channel has been interrupted for the Emergency Alert System.", intro_fn, gv)
        intro_spoken = AudioSegment.from_wav(intro_fn).set_frame_rate(44100)
        if os.path.exists(intro_fn): os.remove(intro_fn)
        
        # Generate EAS Tones
        h = EASGen.genHeader("ZCZC-WXR-EAN-008043+0015-1231234-KDEN/NWS-").set_frame_rate(44100)
        a = EASGen.genATTN(8).set_frame_rate(44100)
        e = EASGen.genEOM().set_frame_rate(44100)
        
        def compile():
            silence = AudioSegment.silent(duration=1000, frame_rate=44100)
            return intro_spoken + silence + h + AudioSegment.silent(500, frame_rate=44100) + a + silence + user_audio + silence + e
            
        final = await asyncio.to_thread(compile)
        base_fn = os.path.abspath(f"{ARCHIVE_DIR}/pipe_base_{ts}.wav")
        await asyncio.to_thread(final.export, base_fn, format="wav")
        await archive_to_central_repo(f"Manual Pipe Broadcast", "Manual audio broadcast.", base_fn)
        
        for gid, cfg in servers_db.items():
            if gid.startswith("__"): continue
            guild = bot.get_guild(int(gid))
            if not guild or not guild.voice_client or not guild.voice_client.is_connected(): continue
            
            gfn = os.path.abspath(f"{ARCHIVE_DIR}/pipe_{gid}_{ts}.wav")
            shutil.copy(base_fn, gfn)
            guild.voice_client.play(discord.FFmpegPCMAudio(source=gfn))
            logger.info(f"📢 Piped audio to {guild.name}")
            
        await ctx.send("Global pipe complete.")
    except Exception as e:
        logger.error(f"Pipe command failure: {e}")
        await ctx.send(f"❌ Pipe failed: {e}")

@bot.command()
async def weather(ctx, target_zip: str = None):
    gid = str(ctx.guild.id)
    cfg = servers_db.get(gid, {})
    zu = target_zip or cfg.get("zip_code")
    if not zu: return
    # Use server-specific intro and outro
    v, ip, op = cfg.get("voice", DEFAULT_VOICE), cfg.get("intro_path"), cfg.get("outro_path")
    await ctx.send(f"🔍 Fetching forecast for {zu}...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://api.zippopotam.us/us/{zu}") as r:
                zdata = await r.json()
            pn = f"{zdata['places'][0]['place name']}, {zdata['places'][0]['state abbreviation']}"
            lat, lon = zdata['places'][0]['latitude'], zdata['places'][0]['longitude']
            async with session.get(f"https://api.weather.gov/points/{lat},{lon}", headers={"User-Agent": "EASBot"}) as r:
                pdata = await r.json()
            furl, cwa = pdata['properties']['forecast'], pdata['properties']['cwa']
            async with session.get(furl, headers={"User-Agent": "EASBot"}) as r:
                fdata = await r.json()
            periods = fdata['properties']['periods']
            txt, spoken_text = "", f"Detailed forecast for {pn}. "
            forecast_text_blocks = []
            for p in periods:
                line = f"**{p['name']}**: {p['detailedForecast']}\n"
                if not forecast_text_blocks or len(forecast_text_blocks[-1]) + len(line) > 1900:
                    forecast_text_blocks.append(line)
                else:
                    forecast_text_blocks[-1] += line
                spoken_text += f"{p['name']}. {p['detailedForecast']} "
            
            hwo_url = f"https://api.weather.gov/products/types/HWO/locations/{cwa}"
            def parse_hwo_text(t):
                m = re.search(r'(This hazardous weather outlook|\.DAY ONE.*?|DISCUSSION\.\.\.)', t, re.I)       
                if m: return re.sub(r'\s+', ' ', re.split(r'\.SPOTTER|\$\$|\&\&', t[m.start():], flags=re.I)[0].strip().replace('\n', ' ').replace('*', ''))
                return None
            hwo_found, hwo_summary = False, ""
            async with session.get(hwo_url, headers={"User-Agent": "EASBot"}) as r:
                hlist = await r.json()
            if hlist.get('@graph'):
                async with session.get(hlist['@graph'][0]['@id'], headers={"User-Agent": "EASBot"}) as r:       
                    hraw = await r.json()
                parsed = parse_hwo_text(hraw.get('productText', ''))
                if parsed: hwo_summary = parsed; spoken_text += " Hazardous Weather Outlook. " + parsed; hwo_found = True
            if not hwo_found:
                try:
                    from bs4 import BeautifulSoup
                    async with session.get(f"https://www.weather.gov/{cwa.lower()}/ghwo", headers={"User-Agent": "EASBot"}) as r:
                        if r.status == 200:
                            soup = BeautifulSoup(await r.text(), 'html.parser')
                            for table in soup.find_all('table'):
                                parsed = parse_hwo_text(table.get_text())
                                if parsed: hwo_summary = parsed; spoken_text += " Hazardous Weather Outlook. " + parsed; hwo_found = True; break
                except: pass
            if not hwo_found:
                async with session.get(f"https://api.weather.gov/products/types/AFD/locations/{cwa}", headers={"User-Agent": "EASBot"}) as r:
                    alist = await r.json()
                if alist.get('@graph'):
                    async with session.get(alist['@graph'][0]['@id'], headers={"User-Agent": "EASBot"}) as r:   
                        araw = await r.json()
                    m = re.search(r'\.(?:KEY MESSAGES|SYNOPSIS)\.\.\.(.*?)\&\&', araw.get('productText', ''), re.S | re.I)
                    if m:
                        hwo_summary = re.sub(r'\s+', ' ', m.group(1).replace('\n', ' ').replace('*', '').replace('-', ''))
                        spoken_text += " Regional Weather Summary. " + hwo_summary; hwo_found = True

        spoken_text += " For the latest information, go to weather.gov."

        async def generate_and_play_weather():
            try:
                # 1. Generate audio for EVERY request (so it can be archived)
                fn = os.path.abspath(f"{ARCHIVE_DIR}/weather_{gid}_{datetime.now().strftime('%H%M%S')}.wav")
                await asyncio.to_thread(generate_normal_speech, spoken_text, fn, voice=v, intro_path=ip, outro_path=op)
                
                # 2. Archive to central repo (Always do this)
                await archive_to_central_repo(f"Weather Forecast ({pn})", spoken_text, fn)
                
                # 3. Play if in VC
                if ctx.voice_client and not ctx.voice_client.is_playing():
                    ctx.voice_client.play(discord.FFmpegPCMAudio(source=fn))
            except Exception as e:
                logger.error(f"Weather audio error: {e}")

        # Start the background task for generation, archiving, and playback
        bot.loop.create_task(generate_and_play_weather())

        for i, block in enumerate(forecast_text_blocks):
            embed = discord.Embed(title=f"🌤️ {pn} ({i+1}/{len(forecast_text_blocks)})", description=block, color=discord.Color.blue())
            if i == len(forecast_text_blocks) - 1: embed.add_field(name="⚠️ Outlook", value=hwo_summary[:1024], inline=False)
            await ctx.send(embed=embed)
            if i < len(forecast_text_blocks) - 1: await asyncio.sleep(10)
    except Exception as e: 
        logger.error(f"Weather command failure: {e}")
        await ctx.send("Error.")

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
async def freshpull(ctx): 
    await ctx.send("🔄 Manual Pull Requested...")
    await check_nws_alerts()
    await ctx.send("✅ Done.")

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
                        
                        # Process each new alert
                        for a in new_alerts:
                            speech = f"Alert: {a.get('headline')}. {a.get('description')}"
                            ts_str = datetime.now().strftime('%Y%M%S')
                            
                            # Sanitize Alert ID for Windows Filename
                            safe_id = re.sub(r'[^a-zA-Z0-9]', '_', a.get('id', 'unknown').split('/')[-1])
                            
                            # Determine which voices are needed
                            target_guilds = []
                            needed_voices = set()
                            for gid, cfg in servers_db.items():
                                if gid.startswith("__") or cfg.get("zone") != z: continue
                                guild = bot.get_guild(int(gid))
                                if guild:
                                    target_guilds.append((guild, cfg))
                                    needed_voices.add(cfg.get("voice", DEFAULT_VOICE))
                            
                            if not needed_voices:
                                # Archive with default voice if no one is tracking
                                archive_fn = os.path.abspath(f"{ARCHIVE_DIR}/alert_{safe_id}_{ts_str}.wav")
                                await asyncio.to_thread(generate_eas_message, speech, archive_fn, "This voice channel has been interrupted for the Emergency Alert System.", voice=DEFAULT_VOICE)
                                await archive_to_central_repo(f"REAL ALERT: {a.get('event')} (Log Only)", speech, archive_fn)
                                continue
                                
                            # Generate one file per required voice
                            voice_files = {}
                            for v_name in needed_voices:
                                v_fn = os.path.abspath(f"{ARCHIVE_DIR}/alert_{safe_id}_{v_name.replace(' ', '_')}_{ts_str}.wav")
                                await asyncio.to_thread(generate_eas_message, speech, v_fn, "This voice channel has been interrupted for the Emergency Alert System.", voice=v_name)
                                await archive_to_central_repo(f"REAL ALERT: {a.get('event')} (Voice: {v_name})", speech, v_fn)
                                voice_files[v_name] = v_fn
                            
                            # Dispatch to each server using its chosen voice
                            for guild, cfg in target_guilds:
                                v_choice = cfg.get("voice", DEFAULT_VOICE)
                                logger.info(f"Dispatching alert to {guild.name} using voice: {v_choice}")
                                
                                # Text Embed
                                tchan = guild.get_channel(cfg.get("text_channel_id"))
                                if tchan:
                                    color = discord.Color.red() if a.get('severity') in ["Extreme", "Severe"] else discord.Color.gold()
                                    embed = discord.Embed(title=f"🚨 {a.get('event')}", description=a.get('headline'), color=color)
                                    bot.loop.create_task(tchan.send(embed=embed))
                                    
                                # Play in Voice
                                vc = guild.voice_client
                                if vc and vc.is_connected() and not vc.is_playing():
                                    play_fn = os.path.abspath(f"{ARCHIVE_DIR}/play_{guild.id}_{ts_str}.wav")
                                    shutil.copy(voice_files[v_choice], play_fn)
                                    vc.play(discord.FFmpegPCMAudio(source=play_fn))
            except Exception as e:
                logger.error(f"API/Alert loop error for zone {z}: {e}")
                
    # Watchdog for dead connections
    for gid, cfg in servers_db.items():
        if gid.startswith("__"): continue
        guild = bot.get_guild(int(gid))
        if guild:
            vc, vcid = guild.voice_client, cfg.get("voice_channel_id")
            if vcid:
                chan = guild.get_channel(vcid)
                if chan and (not vc or not vc.is_connected()):
                    try: await chan.connect(self_deaf=True)
                    except Exception as e:
                        logger.error(f"Watchdog connect error in {guild.name}: {e}")

@check_nws_alerts.before_loop
async def before_check_nws_alerts(): await bot.wait_until_ready()

if __name__ == '__main__':
    if TOKEN: bot.run(TOKEN)
    else: logger.error("No TOKEN.")
