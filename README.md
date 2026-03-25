# 🏏 IPL Cricket Auction Telegram Bot

A complete, production-ready Telegram bot for conducting IPL-style cricket auctions with real-time bidding, RTM system, and full IPL 2026 rules.

---

## 🚀 QUICK SETUP (Android with Termux OR PC)

### Step 1 — Create Your Bot
1. Open Telegram → search **@BotFather**
2. Send `/newbot`
3. Choose a name (e.g. `IPL Auction 2026`)
4. Choose a username (e.g. `my_ipl_auction_bot`)
5. **Copy your BOT TOKEN** (looks like `123456789:ABC-DEF...`)

### Step 2 — Get Your Telegram User ID
1. Open Telegram → search **@userinfobot**
2. Send `/start`
3. **Copy your ID** (a number like `987654321`)

### Step 3 — Configure the Bot
1. Copy `.env.example` → rename to `.env`
2. Fill in:
   ```
   BOT_TOKEN=your_token_from_step_1
   SUPER_ADMIN_ID=your_id_from_step_2
   ```

---

## 📱 RUNNING ON ANDROID (Termux)

```bash
# Install Termux from F-Droid (NOT Play Store)
# Then inside Termux:

pkg update && pkg upgrade
pkg install python git

# Clone or copy your bot files to Termux
cd ~
mkdir ipl-bot && cd ipl-bot

# Copy all files here, then:
pip install -r requirements.txt

# Create .env file
nano .env
# Paste your BOT_TOKEN and SUPER_ADMIN_ID, save with Ctrl+X

# Run the bot
python bot.py
```

Keep Termux open (or use Termux:Boot to auto-start).

---

## 💻 RUNNING ON PC / VPS

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your token and admin ID
python bot.py
```

---

## ☁️ DEPLOYING TO RAILWAY (Free Cloud Hosting)

1. Push code to GitHub
2. Go to [railway.app](https://railway.app) → New Project
3. **Deploy from GitHub** → select your repo
4. Go to **Variables** → Add:
   - `BOT_TOKEN` = your token
   - `SUPER_ADMIN_ID` = your ID
5. That's it! Bot runs 24/7 for free.

---

## 🎮 HOW TO USE THE BOT

### First Time Setup
1. Start the bot → everyone sends `/start` to register
2. Admin adds players:
   ```
   /add_player_list
   1 Virat Kohli, Bat, RCB, Indian, 2cr, Marquee
   2 Rohit Sharma, Bat, MI, Indian, 2cr, Marquee
   3 Jasprit Bumrah, Bowl, MI, Indian, 2cr, Marquee
   ```
3. Admin starts auction: `/startauction`
4. Admin brings first player: `/next`
5. Everyone bids using buttons or `/bid 2.5cr`
6. Admin confirms sale: `/sold`
7. Repeat!

### All Commands

**Everyone:**
- `/start` — Register & get ₹125Cr purse
- `/purse` — Check your balance & squad
- `/squad` — View your players
- `/bid 2cr` — Place a bid
- `/rtm` — Use Right to Match card
- `/leaderboard` — See all teams

**Admin Only:**
- `/startauction` — Begin the auction
- `/next` — Next player up
- `/sold` — Confirm sale
- `/forcesold` — Force sell (skip RTM)
- `/endauction` — End & show summary
- `/add_player_list` — Bulk add players
- `/addplayer` — Add single player
- `/admin 123456` — Make someone admin
- `/queue` — View upcoming players
- `/unsoldqueue` — View unsold players

---

## 📋 IPL 2026 RULES IMPLEMENTED

- 💰 ₹125 Crore starting purse per team
- 👥 Maximum 25 players in squad
- 🌍 Maximum 8 overseas players in squad
- 💵 Maximum ₹18 Crore on overseas players
- ⏱️ 30-second bidding timer
- 🛡️ Anti-snipe: Timer extends if bid in last 10 seconds
- 🎴 RTM (Right to Match) system
- 📈 Post-RTM counter-bid allowed (IPL 2025 rule)

---

## 📁 File Structure

```
ipl-auction-bot/
├── bot.py              ← Main bot (all logic)
├── requirements.txt    ← Python packages
├── .env.example        ← Config template
├── .env                ← Your actual config (DO NOT share!)
├── railway.json        ← Railway deployment config
├── render.yaml         ← Render.com deployment config
├── Dockerfile          ← Docker container config
├── docker-compose.yml  ← Docker Compose config
├── sample_players.txt  ← Sample player list to paste
└── auction.db          ← SQLite database (auto-created)
```
