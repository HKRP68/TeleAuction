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

For Telegram auctions, set these values:

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

To use only the Draft Desk, leave `BOT_TOKEN` and `SUPER_ADMIN_ID` empty. Start the
app and open `https://your-service.example/draft`; no Telegram auction, bot token,
or draft URL token is required. `WEBHOOK_URL` is only for Telegram updates and
should be the public URL of this same service when Telegram integration is enabled.

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

## 🏏 Draft mode and web draft desk

Draft mode is a turn-based alternative to bidding. Open the Draft Desk directly:

```text
https://your-service.example/draft
```

Create the draft on this page, then add each **Team Name**, **Owner**, and optional
**Co-owner**. This setup is stored in the database and does not require creating an
auction or registering teams in Telegram.

The desk imports `.xlsx`, `.csv`, or `.json` player files and `.xlsx`/`.csv` draft orders. Player templates and exports use exactly these 15 public columns:

```text
Set No,Set Name,Player Id,Player Name,Category,country,Overall,bat_hand,bowl_hand,bowl_style,bat_rating,bowl_rating,version,gender,Base Price
```

Leave Player Id blank for new players; Set No defaults to 1 and Set Name defaults to the set's known label or `Set N`. Overall is the stored overall rating. Gender accepts Women/Men and Female/Male/F/M. Missing gender, country or ratings do not count toward the corresponding quota. New templates and player exports contain only the 15 columns above. Older internal tier/RTM fields remain in the database for compatibility and are preserved when updating an existing Player Id. Existing installations gain Set Name automatically at startup without removing player data.

Order sheets use:

```text
round_no,pick_no,tier,team_name,owner_name,owner_tag_id
```

`owner_tag_id` is only needed when you also use Telegram `/pick` commands. A turn may select its own tier or a lower tier: **Platinum → Gold → Silver → Bronze**. The bot posts the round/pick, updated tiered squad, and the next owner’s turn when Telegram integration is enabled.

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


### Auction desk controls

All `/a…` controls are admin commands and also accept a leading dot. Use exact
player names or numeric Player IDs wherever a command accepts a player or lot.
The player table and player cards show Player ID and Set No. Queue order follows
Set No, then Player ID.

| Command | Behavior |
| --- | --- |
| `/atimer 60 40 20 10 5` | Default staged clock: opens at 60 seconds; a bid below 40 resets to 40; warnings at 20 and 10; countdown 5–1. Earlier bids preserve remaining time. |
| `/atimer 45` | Classic clock: opens at 45 seconds and every bid resets to 45. Bare `/atimer` reads the policy. |
| `/abidgap seconds` or `/abidgap off` | Optional auction-wide spacing between accepted bids; off by default. Every bidder has a separate one-second spam throttle. Bare command reads the setting. |
| `/adirect on` or `/adirect off` | Allow arbitrary valid bid amounts, or restrict every bid to the next increment. Bare command reads the setting. `/bid` without an amount bids the next step. |
| `/asetsexport [json\|csv\|xlsx]` | Download every set with stable IDs and player metadata. |
| `/asetsimport [replace]` | Reply to an exported JSON/CSV/XLSX document to generate a review. Use `/asetsreview token` to download its full changes and `/asetsconfirm token` to apply. `replace` withdraws unlisted available players. Owned players stay intact. |
| `/awithdraw player` | Withdraw an unowned player; a standing bid prevents withdrawal. |
| `/areinstate player, …` | Return withdrawn players to the queue. |
| `/astart`, `/apause`, `/aresume` | Start the first lot, freeze its clock, or resume with the saved remaining time. Resolve RTM before pausing. |
| `/anext` | Open the next lot. |
| `/arestart confirm` | Undo sales, clear bids/squads, restore starting purses, and rebuild the queue. Withdrawn players stay withdrawn. |
| `/aextend [seconds]` | Add seconds to the current lot; defaults to 10. |
| `/asold` | Sell at the standing bid. |
| `/aunsold [67, 88, 89]` | Pass the current or listed players. Rejects squad members and players with standing bids. |
| `/aforce player` | Move the player to the front; open immediately when the block is empty. |
| `/arecall player` | Refund the buyer, remove the player from their squad, and queue the player next. |
| `/aundobid` | Remove the standing bid and restore the previous bid from this lot. |
| `/aaccel [go]` | Re-list unsold players. |
| `/afinish [go\|go quick]` | Complete squads under the rules, then play back simulated sales. |
| `/afinish quick\|slow\|mute` | During playback: five sales per message, one per message, or jump to the summary. |
| `/acall [message]` | Tag teams and DM owners/co-owners. Reply to an image to include it. Reports failed DMs (owners must have started the bot). |
| `/aremoveteam team` | Release the team's players and share its refunded budget equally among remaining teams; leftover units go in owner-ID order. Refuses removal of the standing bidder. |
| `/ateamsummary [team]`, `/asummary` | Full categorized squads, overall/batting/bowling ratings, spending, purse balance and limits. |

The `/atimer` policy is stored separately for each auction. Opening a lot starts
the clock immediately. Restored live lots are paused until `/aresume`.

`/afinish` fills each team to at least the auction minimum squad size and satisfies
configured composition minimums, without exceeding squad size, composition caps,
or purses. It honors a standing bid first. Other simulated purchases use base
price (minimum one purse unit), with deterministic allocation; leftover players
are unsold. Use optional `role` (`Bat`, `Bowl`, `AR`, `WK`), `nationality`, and
`base_price` import columns to configure composition and simulation costs.
If no complete allocation is found, or the bounded allocation search runs out,
no simulated sales are applied. Successful allocations are committed together
before playback, so a playback interruption cannot leave half-completed squads.


### Flexible mixed-gender and rating rules

Open **Squad rules** at `/draft/rules`, or use Telegram:

```text
/asquad 15_17
/arules Batsman 4, Bowler 4, Allrounder 4, Wicketkeeper 2, Women 2, 84_Rating 4
```

New rule commands always use **minimum_maximum**. A single number is a minimum;
`Batsman 4` means at least four batsmen. For an exact count use `Batsman 4_4`.
Maximum `0` means no category cap (the squad maximum still applies).
This differs from the older positional `/setlimit` command, which keeps its
original `maximum-minimum` syntax for compatibility.

Gender and rating are independent dimensions: a woman bowler with rating 82 counts
as **Bowler**, **Women**, and **Rating ≤84** at the same time. The role minimums in
the example total 14, so a 15–17 squad can accommodate them plus another player.
Gender/rating quotas do not require additional players beyond those roles.

```text
/arule Women 2_6
/arule Rating<=84 4
/arule Rating<=90.5 3_7
/arule Rating<=84 off
/arules show
/arules clear
```

The number is configurable; `84_Rating` and `Rating<=84` both mean **overall rating
84 or less, inclusive**. Unknown/invalid ratings do not qualify. Supported roles
include Batsman/Batter, Bowler, Allrounder and Wicketkeeper (or Bat/Bowl/AR/WK).
Store the role in `category`; a recognized category takes priority over the legacy
`role` field. Overseas and Indian quotas are also supported. Rules are stored per
auction and checked by bidding, draft picks, and automatic completion. Configuration
rejects contradictory nested rating quotas, role minimums that exceed squad capacity,
and caps that conflict with existing squads. Admin manual squad/transfer tools remain
explicit overrides; their database changes are audited.

**Purse protection:** `/areserve auto` is on by default. Before accepting a bid it
reserves the combined cost of the cheapest eligible remaining players needed to
reach the squad/category minimums (base price, minimum one purse unit). It also
checks remaining slots and individual quota availability. `/areserve off` disables
the reserve/pool check; caps and slot feasibility remain enforced. This is a lower
bound, not a promise that other teams will leave those players available or that
all overlapping quotas can be completed together. `/afinish` performs the complete
allocation search before changing any sales.

**Import review:** player uploads from the control room now show new/updated/unchanged
records, protected squad members, field-level before/after changes, duplicate-name
warnings, and players withdrawn by replacement. Review and apply within 30 minutes.
A changed player pool or team state invalidates the preview. Telegram set imports
use the same preview/confirmation process. The older `/draft/players` programmatic
endpoint accepts direct imports with the same schema for existing clients.

**Audit trail:** `/draft/audit` shows the latest 200 events and exports the full log.
`/aaudit` shows recent events; `/aaudit json` downloads the log. Database triggers
record player changes, bid insertion/removal, purse/squad changes and team deletion,
including changes made through legacy commands. Command entries identify the
requester; mutation entries identify affected teams/bidders or system/web actions.
Rules and confirmed imports are also recorded. Logs start when this version runs;
prior history is not reconstructed.

**Spectator board:** `/watch` is read-only and updates every two seconds. It shows
the current lot, clock, standing bid, queue size, team purses, full squads and each
quota's current count. `/watch/data` supplies its read-only JSON snapshot. Player
details are rendered as text, and owner/co-owner Telegram IDs are omitted.


### Help guide and auction admins

`/help` sends a short overview and attaches [COMMANDS.md](COMMANDS.md). `/commands`
or `/helpmd` downloads the same Markdown file directly, even before an auction
exists. The guide includes every registered slash command and its aliases,
worked setup examples, gender/rating quotas, import confirmation and access levels.

Global admins can assign operators to the currently selected auction:

```text
/auctionadmin @assistant
/auctionadmin add 123456789
/auctionadmin list
/auctionadmin remove 123456789
```

Alternatively, reply to a person's message with `/auctionadmin` to grant access,
or `/auctionadmin remove` to revoke it. Username lookup requires the person to
have sent `/start`; raw positive Telegram user IDs are accepted directly.
Grants persist in SQLite, are recorded in the audit log, and apply only to that
auction. Auction admins can operate its rules, players, clock, queue, purses and
results, but cannot assign other admins or create fresh auctions. Global admins
may create auctions and manage auction-specific grants. Only `SUPER_ADMIN_ID` can
use `/admin @user` to grant global access or `/clearplayers` to clear all players.
Removing an auction-specific grant does not revoke an existing global grant.
The website retains its existing control-room access model; Telegram role grants
apply to the bot's permission checks.


### Bulk team registration

Admins can add teams with comma-separated numeric owner IDs, one team per line:

```text
/addteams
Royal Tigers, 123456789, 234567890
Blue Hawks, 345678901, 456789012, 567890123
```

`/addusers` is an alias. The first ID is the primary owner and the remaining IDs
are co-owners. Every owner acts for the same team; the team gets one starting
purse and one squad. The command also accepts a reply to a team-list message.
Users can be added by ID before sending `/start` (but must start the bot for DMs).

The control room `/draft` offers the same bulk-entry box and supports editing
multiple comma-separated co-owners. Validation rejects the entire batch for bad
rows, duplicate names/IDs, IDs already used by another team in this auction, or
team-capacity overflow. Existing team budgets remain intact. Additions are audited.


### Consistent player imports, exports and cancellation

Use `/downloadtemplate [xlsx|csv|json]` to get the shared Player Database template.
Replace the sample row, keep its headers, and reply to the file with `/uploaddata`
(or `/asetsimport`). A command in the file caption also works. Imports show a preview:
review with `/asetsreview TOKEN`, then apply with `/asetsconfirm TOKEN`. The website
provides the same CSV/JSON/XLSX import preview, template downloads and exports.

Core columns are `Set No,Set Name,Player Id,Player Name,Category,country,Overall,bat_hand,bowl_hand,bowl_style,bat_rating,bowl_rating,version,gender,Base Price`.
Only the 15 public fields appear in new templates and exports. Older optional columns remain readable for compatibility; omitted internal fields retain their stored values when updating an existing player.
Only a name is required for a new player; blank Player IDs are generated, and Set No
is 1 by default. Existing IDs update matching players in this auction; omitted fields
keep stored values. Owned players remain protected. Foreign IDs are rejected; clear
the IDs to clone players to a different auction. All formats retain the public metadata fields. Stored auction/draft tiers are preserved for existing IDs and are not exposed in the streamlined template. Status and sale columns in full
reports are read-only during metadata imports and do not recreate ownership.

```text
/addplayer 1,Pace Bowlers,,Example Player,Bowler,India,82,Right,Right,Fast,70,87,2026,Women,20l
/exportplayers xlsx
/asetsexport csv
/downloaddata
```

`/exportplayers` defaults to XLSX; `/asetsexport` defaults to JSON. Both accept
XLSX/CSV/JSON and use identical headers. `/downloaddata` keeps its full Excel report,
with an importable Players sheet and all metadata in Squad Details. An explicit
format, e.g. `/downloaddata csv`, exports the player records. `/addplayer`,
`/add_player_list` and `/bulkplayer` accept detailed CSV/JSON entry while retaining
older short entry syntax. See [COMMANDS.md](COMMANDS.md) for worked examples.

To cancel, run `/acancel` for details, then `/acancel confirm` (alias
`/cancelauction confirm`). The clock and auto-next stop, the outstanding bid is
voided without charging it, an unresolved RTM card is refunded, registration and
bidding close, and import previews expire. Completed sales, squads, purses and
player records stay available for export. Cancellation survives restarts and is
audited. `/astart` and `/aresume` cannot reopen it; use a new auction or explicitly
reset with `/arestart confirm`. Completed auctions cannot be cancelled. The control
room also provides a cancellation review screen.

Ready-to-edit examples: [sample_players.csv](sample_players.csv), [sample_players.json](sample_players.json), and [sample_players.txt](sample_players.txt). The samples use fictional player data.


### Home country, overseas quotas and the streamlined player template

By default there is no Home/Indian or Overseas nationality quota. `/home India`
selects the home country without imposing a cap. `/overseas 7_8` then requires
7–8 overseas players per squad; `/overseas off` removes that requirement. Home
players have no nationality cap unless an explicit Home rule is added. Bare
commands read the settings. Countries are compared against Home dynamically,
not against a legacy Indian/Overseas flag. Missing countries are Unclassified.
These per-auction settings are enforced in live bids, reserve checks, picks and
simulation, and the rules screen/live board show the same classification.

The only public player columns, in order, are:

```text
Set No,Set Name,Player Id,Player Name,Category,country,Overall,bat_hand,bowl_hand,bowl_style,bat_rating,bowl_rating,version,gender,Base Price
```

`Catagory` is accepted as an alias of Category; Overall maps to the stored overall
rating. New templates, CSV/JSON/XLSX exports and direct CSV player entry share this
15-column format. Existing internal tier/RTM metadata is preserved in the
database and omitted from these player files. For full auction backups keep the
SQLite database. Set Name defaults to the set's known name or `Set N`, appears in
live lot cards, and is audited. A batch cannot give the same Set No conflicting
names. See [COMMANDS.md](COMMANDS.md) for current examples.

Base Price is the 15th public player field. It is included in all templates and player exports and controls the minimum opening bid. Use `20`, `50l`, or `2cr`; old 14-column files remain readable and preserve existing prices when the column is omitted.
