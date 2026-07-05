# AliEAS: Software-Defined EAS Discord Relay
Version: 1.0 (Production)

AliEAS is a boutique, highly authentic Software-Defined ENDEC terminal built for Discord. It was engineered to capture the exact aesthetic and functionality of a real analog NOAA Weather Radio system, complete with legacy 32-bit voice engines, mathematically generated SAME bursts, and a professional web-based dashboard.

Official Repository: [https://github.com/averlice/AliEAS](https://github.com/averlice/AliEAS)

## License
Distributed under the **MIT License**. See `LICENSE` for more information.

## Features
*   **Authentic Audio:** Uses `EASGen` to dynamically generate authentic SAME AFSK data bursts (Headers and EOM).
*   **Legacy Voice Engine:** Features a custom PowerShell bridge to utilize the classic 32-bit SAPI5 **ScanSoft Tom** voice (the legendary 2000s NOAA Weather Radio voice) inside a modern 64-bit Python environment.
*   **Radio Atmosphere:** Layers a dynamic 60Hz electronic hum, white noise, and mic key-up/key-down clicks underneath the voice to simulate a real analog radio broadcast.
*   **Intelligent Parsing:** Automatically expands NWS shorthand (like `MDT`, `HI`, `CO`) and strips out formatting asterisks so the TTS voice sounds natural.
*   **Triple-Stage Outlook Fetching:** Robustly finds Hazardous Weather Outlooks by checking the API, scraping local WFO graphical web pages, or falling back to Area Forecast Discussions.
*   **Zero-Trust Web Dashboard:** A built-in `aiohttp` web server (Port 2424) that provides a screen-reader friendly ENDEC control panel. Secured via **Discord OAuth2** identity verification (only the Bot Owner can access the controls).
*   **Permanent Audio Archive:** Automatically logs and permanently saves every generated broadcast to a dedicated archive folder with a built-in web playback library.
*   **Smart Pinging:** Automatically upgrades Discord pings to `@everyone` when life-threatening alerts (like Tornado Warnings or Civil Emergencies) are issued.
*   **Global Manual Overrides:** The `fco!pipe` command allows administrators to upload any `.mp3` or `.m4a` file and instantly broadcast it globally with full EAS tones across all configured servers.

## Installation Requirements

### 1. Python Dependencies
Ensure you have Python installed, then run:
```bash
pip install -r requirements.txt
```

### 2. External Dependencies
*   **FFmpeg:** You must have `ffmpeg` installed and added to your system PATH for `discord.py` and `pydub` to process audio.
*   **ScanSoft Tom (Optional but Recommended):** The bot is hardcoded to look for the 32-bit `ScanSoft Tom_Full_22kHz` SAPI5 voice in the Windows Registry. If you do not have this voice, you will need to modify `eas_audio.py` to point to a different TTS engine or a 64-bit voice.
*   **CMU Flite (Optional):** To use `flite_kal`, `flite_kal16`, `flite_awb`, `flite_rms`, or `flite_slt`, install the native CMU Flite executable and either add `flite.exe` to PATH or place it at `flite.exe` / `flite/flite.exe` in the repo root. The PyPI package named `flite` is not CMU Flite TTS and should not be installed for this bot.

### 3. Environment Variables (.env)
Create a `.env` file in the root directory with the following variables:
```env
DISCORD_TOKEN=your_bot_token_here
DISCORD_CLIENT_ID=your_oauth2_client_id
DISCORD_CLIENT_SECRET=your_oauth2_client_secret
REDIRECT_URI=http://localhost:2424/callback
```
*(Note: Change the `REDIRECT_URI` if you are hosting the dashboard behind a reverse proxy like Cloudflare).*

## Running the Bot
Run the bot manually from your terminal:
```bash
python bot.py
```
The bot will start its Discord connection and launch the Web ENDEC on `http://localhost:2424`. 

If you place valid `cert.pem` and `key.pem` files in the root directory, the Web ENDEC will automatically detect them and launch in secure `HTTPS` mode.

## Discord Commands

### General Commands
*   `fco!join` - Joins your current voice channel.
*   `fco!leave` - Leaves the voice channel.
*   `fco!active` - Shows all currently active alerts for this server's zone.
*   `fco!history` - Shows the last 5 alerts broadcasted in this zone.
*   `fco!weather [ZIP]` - Reads the full 7-day forecast and HWO (defaults to server zone).
*   `fco!stop` or `fco!silence` - Instantly stops audio playback.
*   `fco!status` - Shows the bot's health, latency, and monitoring zones.

### Admin Commands (Server Admins)
*   `fco!setup <ZIP>` - Sets the bot's alert location and default channels.
*   `fco!test` - Broadcasts a test EAS alert in this server only.

### Owner Commands
*   `fco!testglobal` or `fco!testg` - Tests all configured servers globally.
*   `fco!pipe` - Broadcasts an uploaded audio file (.mp3, .wav, .m4a) with full EAS tones.
*   `fco!serverslist` - Lists all configured servers.
*   `fco!freshpull` - Forces an immediate API pull.
*   `fco!restart` - Restarts the bot process.
*   `fco!shutdown` - Shuts down the bot.
*   `fco!getlogs` - Uploads the background log files to Discord.

## Credits & Thanks
This project leverages `EASGen` for SAME burst generation and `pydub` for audio manipulation. The 32-bit PowerShell bridge is a custom workaround for modern Windows environments. 
