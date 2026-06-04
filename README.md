# 🏏 IPL Cricket Auction Telegram Bot

A Telegram bot for running IPL-style cricket auctions with live bidding, queues, RTM flows, team purses, squad management, exports, and deployment support for local machines, Docker, Railway, and Render.

---

## 🚀 Quick setup

### 1. Create your bot
1. Open Telegram and search **@BotFather**.
2. Send `/newbot`.
3. Choose a display name and username.
4. Copy the **BOT_TOKEN** that BotFather gives you.

### 2. Get your Telegram user ID
1. Open Telegram and search **@userinfobot**.
2. Send `/start`.
3. Copy your numeric Telegram user ID.

### 3. Configure environment variables
Copy the included example file and fill in your values:

```bash
cp .env.example .env
```

Required values:

```env
BOT_TOKEN=your_token_from_botfather
SUPER_ADMIN_ID=your_numeric_telegram_user_id
```

Optional values:

```env
# Leave empty for long polling. Set to your public URL for webhook mode.
WEBHOOK_URL=

# SQLite location. Use a persistent mounted path in production.
DATABASE_PATH=auction.db

# Optional Telegram channel/group ID for pinned SQLite backups.
STATE_CHANNEL_ID=0
```

---

## 📱 Running on Android with Termux

```bash
pkg update && pkg upgrade
pkg install python git

# Copy or clone the bot files, then enter the project directory.
pip install -r requirements.txt
cp .env.example .env
nano .env
python bot.py
```

Keep Termux open, or use Termux:Boot if you want to auto-start the bot after device restart.

---

## 💻 Running on PC / VPS

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
# Edit .env with BOT_TOKEN and SUPER_ADMIN_ID.
python bot.py
```

By default, the bot runs in long-polling mode when `WEBHOOK_URL` is empty.

---

## 🐳 Running with Docker Compose

```bash
cp .env.example .env
# Edit .env with BOT_TOKEN and SUPER_ADMIN_ID.
docker compose up --build
```

Docker Compose stores SQLite data under `./data` by setting `DATABASE_PATH=/app/data/auction.db` and mounting `./data:/app/data`.

---

## ☁️ Deployment notes

### Railway
1. Push this repo to GitHub.
2. Create a Railway project from GitHub.
3. Add variables:
   - `BOT_TOKEN`
   - `SUPER_ADMIN_ID`
   - `DATABASE_PATH=/app/data/auction.db` if you attach a persistent volume
4. Leave `WEBHOOK_URL` empty unless you explicitly configure a public webhook URL.

### Render
1. Create a Render Web Service from this repo.
2. Add variables:
   - `BOT_TOKEN`
   - `SUPER_ADMIN_ID`
   - `WEBHOOK_URL=https://your-render-service.onrender.com`
   - `DATABASE_PATH=/opt/render/project/src/data/auction.db` if using a persistent disk
3. Attach a persistent disk for SQLite, or set `STATE_CHANNEL_ID` to a private Telegram channel/group where the bot is admin so the bot can pin database backups.

> Cloud filesystems can be ephemeral. For serious auctions, use a persistent disk/volume or configure `STATE_CHANNEL_ID` backups.

---

## 🎮 First-time auction flow

1. Everyone sends `/start` to register.
2. Admin creates or starts an auction.
3. Admin adds players using `/addplayer`, `/add_player_list`, `/bulkplayer`, or `/uploaddata`.
4. Admin starts the auction with `/startauction`.
5. Admin brings the next player with `/next`.
6. Teams bid with buttons, `/bid 2cr`, or dot shortcuts like `.bid 2cr`.
7. Admin confirms sale with `/sold` or skips RTM with `/forcesold`.
8. Repeat until the auction ends, then run `/endauction`.

---

## 🧾 Command reference

### Everyone
- `/start` or `/registration` — Register for the current auction.
- `/help` — Show bot help.
- `/setteamname` or `/stn` — Set your team name.
- `/purse`, `/bal`, or `/balance` — Check purse and squad.
- `/squad` — View your squad.
- `/status` — View current auction/player status.
- `/bid 2cr` — Place a bid.
- `/rtm` or `/right_to_match` — Use Right to Match when eligible.
- `/myrtm` — View your RTM card status.
- `/mybidhistory` — View your bidding history.
- `/auctionhistory` — View completed auction snapshots.
- `/leaderboard` — View team standings.

### Admin: auction setup and info
- `/create_auction` or `/createauction` — Create a new auction.
- `/setauctionname` — Rename the active auction.
- `/setcurrency` — Change the currency label.
- `/admin 123456` — Grant admin rights to a known user.
- `/auctionowners` — View owners/co-owners.
- `/soldplayers` — View sold players.
- `/unsoldplayers` — View unsold players.
- `/auctionsummary` — View auction summary.

### Admin: player management
- `/addplayer` — Add one player.
- `/add_player_list` — Bulk-add comma-separated players.
- `/bulkplayer` — Bulk-add players using JSON/CSV-style input.
- `/clearplayers` — Clear players for the auction.
- `/findplayer` or `/search` — Search players.
- `/playercard` or `/pc` — Show a player card.
- `/downloadtemplate` or `/template` — Download an import template.
- `/uploaddata` or `/importdata` — Import player/team data.
- `/downloaddata` or `/exportdata` — Export auction data.

### Admin: team management
- `/setrtm` — Assign RTM cards.
- `/mute_team` or `/muteteam` — Mute a team.
- `/unmute_team` or `/unmuteteam` — Unmute a team.
- `/teamup` — Link a co-owner to a team.
- `/setpurse` or `/setbal` — Set team purse.
- `/addpurse` or `/addbal` — Add to team purse.
- `/deductpurse` or `/deductbal` — Deduct from team purse.
- `/addtosquad` or `/ats` — Manually add a player to a squad.
- `/removefromsquad` or `/rfs` — Remove a player from a squad.
- `/clearsquad` — Clear a squad.
- `/swap` — Swap players between teams.
- `/transfer` or `/tradeplayer` — Transfer/trade a player.
- `/mystats` or `/stats` — View team stats.

### Admin: live auction controls
- `/startauction` — Begin the auction.
- `/next` — Bring the next queued player.
- `/pass` — Mark current player unsold/pass.
- `/forceauction` — Force a player into auction.
- `/sold` — Confirm sale and trigger RTM logic when applicable.
- `/forcesold` — Confirm sale without RTM.
- `/undo` or `/undosold` — Undo the latest sale.
- `/pauseauction` — Pause auction.
- `/resumeauction` — Resume auction.
- `/endauction` — End auction and save summary.
- `/announce` or `/broadcast` — Send an announcement.

### Admin: timers and queue
- `/autosell` — Configure auto-sell behavior.
- `/autonext` — Configure auto-next behavior.
- `/dtime` or `/timers` — View all timers.
- `/bidtimer` or `/bidduration` — Set bid timer.
- `/antisnipe` — Set anti-snipe extension.
- `/rtmwindow` — Set RTM offer window.
- `/rtmcounter` — Set RTM counter-bid window.
- `/rtmdecision` — Set RTM final decision window.
- `/addtoqueue` or `/atq` — Add players to queue.
- `/addtoqueueunsolds` or `/atqu` — Add unsold players to queue.
- `/removefromqueue` or `/rfq` — Remove players from queue.
- `/shufflequeue` or `/sq` — Shuffle queue.
- `/swapqueue` — Swap queue positions.
- `/clearqueue` — Clear queue.
- `/queue` or `/q` — View queue.

Most slash commands also support dot shortcuts through the dot-command handler, for example `.bid 2cr`.

---

## 📋 Auction rules implemented

- Starting purse is configurable per auction; IPL-style examples use ₹125 Crore.
- Minimum and maximum squad sizes are configurable.
- Overseas-player limits are enforced by auction rules.
- Bidding timers and anti-snipe extensions are configurable.
- RTM has separate offer, counter-bid, and final-decision windows.
- Post-RTM counter-bid flow is supported.
- Sold/unsold history, bid history, and auction snapshots are stored in SQLite.

---

## 🧪 Development checks

```bash
python -m py_compile bot.py
pytest -q
```

Install test dependencies with:

```bash
pip install -r requirements.txt -r requirements-dev.txt
```

---

## 📁 File structure

```text
ipl-auction-bot/
├── bot.py                  # Main bot logic
├── requirements.txt        # Runtime packages
├── requirements-dev.txt    # Test/dev packages
├── .env.example            # Environment template
├── railway.json            # Railway deployment config
├── render.yaml             # Render deployment config
├── Dockerfile              # Docker image config
├── docker-compose.yml      # Local Docker Compose config
├── sample_players.txt      # Sample player list
├── tests/                  # Automated tests
└── auction.db              # SQLite database, created at runtime and ignored by git
```
