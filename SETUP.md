# AoO Alliance Stats Discord Bot — Setup Guide

## Overview

This bot responds to @mentions in Discord with natural language answers about your alliance stats, pulling live data from your Google Sheet.

---

## Step 1 — Create the Discord Bot

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application** → name it (e.g. `AoO Stats Bot`)
3. Go to **Bot** in the left sidebar
4. Click **Reset Token** → copy and save the token (this goes in `.env` as `DISCORD_TOKEN`)
5. Under **Privileged Gateway Intents**, enable:
   - ✅ **Message Content Intent**
6. Go to **OAuth2 → URL Generator**:
   - Scopes: ✅ `bot`
   - Bot Permissions: ✅ `Send Messages`, ✅ `Read Message History`, ✅ `View Channels`
7. Copy the generated URL, open it in your browser, and add the bot to your Discord server
8. **Optional but recommended:** In Discord, right-click your stats channel → **Copy Channel ID** → paste into `.env` as `STATS_CHANNEL_ID`
   - *(Enable Developer Mode first: Discord Settings → Advanced → Developer Mode)*

---

## Step 2 — Create the Google Service Account

This gives the bot read-only access to your Sheet without your personal login.

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (or use an existing one) — name it e.g. `AoO Bot`
3. Enable these APIs (**APIs & Services → Enable APIs**):
   - **Google Sheets API**
   - **Google Drive API**
4. Go to **APIs & Services → Credentials → Create Credentials → Service Account**
   - Name: `aoo-bot-reader`
   - Role: **Viewer** (read-only)
5. Click the service account → **Keys tab → Add Key → JSON**
   - Download the JSON file
   - **Local hosting:** rename it `service_account.json` and place it in the `aoo_discord_bot/` folder
   - **Cloud hosting:** open the file, copy the entire contents as one line into `.env` as `GOOGLE_SERVICE_ACCOUNT_JSON`
6. Open the JSON file and find the `client_email` field (looks like `aoo-bot-reader@your-project.iam.gserviceaccount.com`)
7. **Share your Google Sheet** with that email address (View access only)

---

## Step 3 — Get Your Sheet ID

Your Google Sheet URL looks like:
```
https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUv/edit
```
The Sheet ID is the long string between `/d/` and `/edit` — copy it into `.env` as `GOOGLE_SHEET_ID`.

**Verify your tab names match exactly** what's in `sheets.py`:
- `Roster`
- `Event Log`
- `Member Summary`
- `Alliance Summary`

If any tab names differ, edit the `TABS` dict in `sheets.py`.

---

## Step 4 — Install & Run Locally (Bluefin/Linux)

```bash
# Clone or copy the project folder, then:
cd ~/aoo_discord_bot

# Create a virtual environment (venv only — no global installs)
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy and fill in your .env
cp .env.example .env
nano .env   # or open in your editor

# Test run
python bot.py
# You should see: ✅ Bot online as AoO Stats Bot#XXXX
```

**To make it run 24/7 as a system service:**
```bash
# Edit aoo-bot.service — replace YOUR_USERNAME with your actual Linux username
nano aoo-bot.service

# Install the service
sudo cp aoo-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable aoo-bot    # auto-start on boot
sudo systemctl start aoo-bot

# Check status / view logs
sudo systemctl status aoo-bot
journalctl -u aoo-bot -f
```

---

## Step 5 — Deploy to Railway (Cloud Option)

If you don't want to leave your machine running:

1. Push the project to a GitHub repo (make sure `.env` is in `.gitignore`!)
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Add all your environment variables in Railway's **Variables** tab
   - Use `GOOGLE_SERVICE_ACCOUNT_JSON` (not the file) for credentials
4. Railway will auto-deploy — the bot stays online even when your PC is off

---

## Usage Examples

Once running, members mention the bot in Discord:

```
@AoO Stats Bot Who has the lowest attendance this month?
@AoO Stats Bot Top 10 performers in Battle Frenzy
@AoO Stats Bot Has PlayerName participated in Elite War recently?
@AoO Stats Bot Which members haven't logged any events this week?
@AoO Stats Bot Show me the Alliance Summary
@AoO Stats Bot Who are our most improved members?
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| Bot doesn't respond | Check Message Content Intent is enabled in Discord Developer Portal |
| `WorksheetNotFound` error | Tab names in `sheets.py` must match your Sheet exactly (case-sensitive) |
| `PERMISSION_DENIED` from Google | Make sure you shared the Sheet with the service account email |
| Bot responds in wrong channel | Set `STATS_CHANNEL_ID` in `.env` |
| Answers seem outdated | Data is fetched live on every question — check your Sheet is up to date |
| Response too long | The bot auto-splits messages over 1900 chars — this is normal |

---

## File Structure

```
aoo_discord_bot/
├── bot.py                  # Main bot — Discord event handling + Claude API calls
├── sheets.py               # Google Sheets fetcher
├── requirements.txt        # Python dependencies
├── .env.example            # Template — copy to .env and fill in
├── .env                    # Your secrets — NEVER commit this
├── service_account.json    # Google credentials — NEVER commit this
└── aoo-bot.service         # systemd service file (for Bluefin/Linux autostart)
```
