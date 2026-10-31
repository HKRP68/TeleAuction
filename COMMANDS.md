# Player Auction command guide

`/help` sends a short overview and this Markdown file. `/commands` or `/helpmd`
downloads the file again. Replace names, IDs, amounts and example tokens with your
own values. Square brackets indicate optional arguments; do not type the brackets.

## Access levels

- **Player:** ordinary owner/co-owner commands.
- **Auction admin:** controls for the currently selected auction only.
- **Global admin:** controls for all auctions, creation of new auctions, and
  assignment/removal of auction admins.
- **Super admin:** the `SUPER_ADMIN_ID` configured on the server; also grants
  global admins and may clear the entire player database for an auction.

Examples below use `@owner`, `@assistant` and Telegram ID `123456789` as placeholders.
For a username lookup, the person must first send `/start` to the bot. A numeric
Telegram user ID can be used without a username. Replying to the person's message
also works for `/auctionadmin`.

## Quick setup (global admin)

```text
/create_auction 8,100cr,15_17
/auctionadmin 123456789
/asquad 15_17
/arules Batsman 4, Bowler 4, Allrounder 4, Wicketkeeper 2, Women 2, Rating<=84 4
/atimer 60 40 20 10 5
/abidgap off
/adirect on
/areserve auto
```

Create teams and import players in the website `/draft`. Review player imports
before applying them. When owners and players are ready, run `/astart` in the
auction group to start the first lot. Use `/anext` after a lot is complete.
The website paths are relative to your deployment URL; they are not bot commands.

Amounts: `2cr` = 200 lakhs, `50l` = 50 lakhs, and bare `200` = 200 lakhs.

## Player commands

| Command | Purpose | Example |
| --- | --- | --- |
| `/start` | Register and display your Telegram ID. | `/start` |
| `/help` | Overview and attached Markdown command guide. | `/help` |
| `/commands` | Download this guide. | `/commands` |
| `/setteamname name` | Set/rename your team. | `/setteamname Royal Tigers` |
| `/purse [@user]` | Show purse and squad size; admins may inspect another owner. | `/purse` |
| `/squad [@user]` | Show your squad; admins may inspect another owner. | `/squad` |
| `/status` | Current auction, lot and bid status. | `/status` |
| `/bid [amount]` | Next increment, or a typed amount when allowed. | `/bid 2cr` |
| `/pick player` | Scheduled draft selection by the owner/co-owner (or admin). | `/pick Jasprit Bumrah` |
| `/myrtm` | RTM cards and assigned former team. | `/myrtm` |
| `/rtm` | Use an eligible RTM card during its offer window. | `/rtm` |
| `/mybidhistory` | Your recent bid results. | `/mybidhistory` |
| `/auctionhistory` | Past auction results. | `/auctionhistory` |
| `/leaderboard` | Team spending leaderboard. | `/leaderboard` |
| `/playercard player` | Player details. | `/playercard Jasprit Bumrah` |
| `/mystats` | Your bidding and squad statistics. | `/mystats` |

## Add teams and owners in bulk

Auction admins can add one team per line with `/addteams` (alias `/addusers`):

```text
/addteams
Royal Tigers, 123456789, 234567890
Blue Hawks, 345678901, 456789012, 567890123
```

Format: `TEAMNAME, PRIMARY_OWNER_ID, CO_OWNER_ID, CO_OWNER_ID, …`.
The first ID is the primary owner; every additional ID is a co-owner. A team may
have only one owner. All listed owners share one squad and purse and can bid or
make the team's scheduled draft picks. Use positive numeric Telegram user IDs;
they do not need to send `/start` before being added. They must start the bot
before it can DM them.

You can also paste the team list in a separate message and reply with `/addteams`.
The website `/draft` has an **Add teams in bulk** box for the same format. Team
editing accepts comma-separated co-owner IDs and preserves multiple co-owners.

Each new team receives the auction's starting purse once, regardless of the number
of owners. The whole batch is checked before saving: duplicate team names, IDs
repeated across teams, IDs already assigned in this auction, malformed rows and
team-capacity overflow reject the batch without adding any teams. Existing teams
and their budgets are preserved. Use the team editor to modify existing teams.

## Admin assignment

| Command | Access | Purpose / example |
| --- | --- | --- |
| `/auctionadmin [add] @user` | Global admin | Assign only for the current auction: `/auctionadmin @assistant`. |
| `/auctionadmin [add] user_id` | Global admin | Assign by ID: `/auctionadmin add 123456789`. |
| `/auctionadmin` as a reply | Global admin | Reply to a person's message to assign them. |
| `/auctionadmin remove @user` | Global admin | Revoke that auction's grant: `/auctionadmin remove @assistant`. |
| `/auctionadmin remove` as a reply | Global admin | Reply to revoke that person's auction-specific access. |
| `/auctionadmin list` | Any admin for this auction | List this auction's explicitly assigned admins. |
| `/admin @user` or `/admin user_id` | Super admin | Grant global access, e.g. `/admin 123456789`. |

An auction admin can use the normal auction controls below, including rules,
players, queue, purses and results. They cannot assign other admins, grant global
access, clear the full player list, or create another auction. Auction-specific
grants persist across restarts and do not carry over to a new auction. Removing
an auction-specific grant does not remove existing global/super-admin access.

## Auction setup and squad rules (auction admin unless noted)

| Command | Purpose | Example |
| --- | --- | --- |
| `/create_auction teams,purse,min_max` | Create a fresh auction; global admin only. | `/create_auction 8,100cr,15_17` |
| `/setauctionname name` | Rename the current auction. | `/setauctionname Premier Draft 2026` |
| `/setcurrency symbol` | Set display currency. | `/setcurrency ₹` |
| `/asquad [min_max]` | Read/change squad size. | `/asquad 15_17` |
| `/arules [rules\|show\|clear]` | Read/update several quotas, or clear them all. | `/arules Batsman 4, Bowler 4, Women 2` |
| `/arule category min_max` | Set one quota. | `/arule Women 2_6` |
| `/arule category minimum` | Set a minimum without a category cap. | `/arule Rating<=84 4` |
| `/arule category off` | Remove a quota. | `/arule Rating<=84 off` |
| `/areserve [auto\|off]` | Read/change purse reserve protection; default auto. | `/areserve auto` |
| `/setincrement amount` | Minimum raise. | `/setincrement 10l` |

New rule commands use **minimum_maximum**: `4_6` means at least 4 and at most 6;
`4_4` means exactly 4; a single `4` means at least 4. Maximum `0` means no category
cap, subject to the overall squad maximum.

Categories: Batsman/Batter/Bat, Bowler/Bowl, Allrounder/AR, Wicketkeeper/WK,
Women, Men, Overseas, Indian, and any `Rating<=number` threshold. `84_Rating`
is an alias of `Rating<=84`: overall rating 84 **or below**, inclusive. For another
threshold use `/arule Rating<=90.5 3_7`.

Gender and rating quotas overlap role quotas: a woman bowler rated 82 counts toward
Bowler, Women, and Rating<=84. The quick-setup role minimums total 14; they fit a
15–17 squad. Gender/rating minimums can be satisfied by those same players.
Missing gender/rating does not qualify for those quotas.

Reserve protection keeps a lower-bound budget for the cheapest eligible remaining
players needed to meet minimums and checks individual quota availability. It cannot
promise other teams will leave those players available. Automatic completion does
a complete allocation search before applying simulated sales.

## Home country and optional overseas quota

There is **no Home/Indian or Overseas nationality minimum/cap by default**. The
home-country setting defaults to India for classification only; it does not enable
any nationality quota. Configure the optional overseas requirement with:

```text
/home India
/overseas 7_8
```

This means Home = India, and each squad must have **at least 7 and at most 8 players
from other countries**. Home players have no separate nationality cap. Overall
squad limits and any role/gender/rating rules still apply.

```text
/home Australia
/overseas
/overseas off
```

Bare `/home` reads the country, and bare `/overseas` reads the quota. `/overseas off`
(or `0_0`) removes the quota. New auctions start with nationality quotas off. Country
classification is based on each player's `country`, not their name or a historical
Indian/Overseas flag. Blank countries are Unclassified and do not count toward
either nationality quota. Changing home recomputes classification and is rejected
if it would violate an existing squad or standing bid. The rule is stored per
auction, persists across restarts, and is used in bidding, draft picks, reserve
checks and automatic completion. `/draft/rules` has matching controls, while the
live board displays Home/Overseas/Unclassified counts.

## Clock and bidding

| Command | Purpose | Example |
| --- | --- | --- |
| `/atimer [seconds]` | Read the policy, or select classic reset-on-every-bid timing. | `/atimer 45` |
| `/atimer open reset warn warn countdown` | Staged policy; default values shown. | `/atimer 60 40 20 10 5` |
| `/abidgap [seconds\|off]` | Spacing between accepted bids from all teams; default off. | `/abidgap 2` |
| `/adirect [on\|off]` | Allow typed amounts, or restrict bids to the next step. | `/adirect off` |
| `/astart` | Start the auction and open the first lot. | `/astart` |
| `/apause` | Freeze the bidding clock (resolve RTM first). | `/apause` |
| `/aresume` | Resume with saved remaining time. | `/aresume` |
| `/anext` | Open the next queued lot. | `/anext` |
| `/aextend [seconds]` | Extend the current lot, default 10 seconds. | `/aextend 20` |

Staged timing opens at 60 seconds, preserves early remaining time, and resets a
bid below 40 seconds to 40 seconds. It warns at 20 and 10 and counts down 5–1.
Each bidder has a one-second spam throttle even with `/abidgap off`. A restored
live lot is paused until `/aresume`.

## Lots, sales and auction completion

Use an exact unique player name or stable numeric Player ID as the lot number.

| Command | Purpose | Example |
| --- | --- | --- |
| `/asold` | Sell at the standing bid. | `/asold` |
| `/aunsold [players]` | Pass the current or listed unowned players; refuses standing bids. | `/aunsold 67, 88, 89` |
| `/aforce player` | Queue next, or open now if the block is empty. | `/aforce 67` |
| `/awithdraw player` | Withdraw an unowned player without a standing bid. | `/awithdraw 67` |
| `/areinstate player, …` | Re-list withdrawn players. | `/areinstate 67, 88` |
| `/arecall player` | Refund a sold player's buyer and queue that player next. | `/arecall 67` |
| `/aundobid` | Void the standing bid and restore the previous bid in this lot. | `/aundobid` |
| `/aaccel [go]` | Re-list unsold players. | `/aaccel go` |
| `/arestart confirm` | Undo sales, clear bids/squads, restore starting purses; withdrawals remain. | `/arestart confirm` |
| `/afinish [go\|go quick]` | Complete squads under the configured rules and play back results. | `/afinish go quick` |
| `/afinish quick\|slow\|mute` | During playback: five lots per message, one per message, or summary only. | `/afinish mute` |

`/afinish` honors a standing bid, allocates other players at base price (minimum
one purse unit), and leaves remaining players unsold. Every team must reach at
least the squad minimum and all composition minimums within its purse and caps.
Impossible or search-limit-exceeded completion applies no simulated sales. Results
are committed together before playback so playback interruptions do not undo them.

## Cancellation

`/acancel` explains what cancellation does; `/acancel confirm` performs it.
`/cancelauction confirm` is an alias. Auction admins may cancel the selected auction.

Cancellation stops the clock and automatic next-lot scheduling, clears the current
bid without charging it, returns an unresolved RTM card, closes bidding/registration,
and invalidates pending import previews. Completed sales, squads, purse balances,
player metadata and audit events are preserved and remain exportable. Cancellation
is recorded in the audit log. `/astart` and `/aresume` cannot reopen a cancelled
auction. Create a new auction or explicitly run `/arestart confirm` to reset the
cancelled auction's sales and purses before reopening it. A completed auction cannot
be cancelled. The website has a **Cancel auction…** review step in `/draft`.

## Player database and reviewed imports

| Command | Purpose | Example |
| --- | --- | --- |
| `/exportplayers [json\|csv\|xlsx]` | Download all player database fields. | `/exportplayers xlsx` |
| `/asetsexport [json\|csv\|xlsx]` | Download all sets and stable player IDs. | `/asetsexport csv` |
| `/asetsimport [replace]` | Reply to a JSON/CSV/XLSX file to preview its changes. | Reply with `/asetsimport replace`. |
| `/asetsreview token` | Download field-level preview details. | `/asetsreview TOKEN_FROM_PREVIEW` |
| `/asetsconfirm token` | Apply the reviewed preview within 30 minutes. | `/asetsconfirm TOKEN_FROM_PREVIEW` |
| `/findplayer query` | Search by name, role, former team or tier. | `/findplayer Bumrah` |
| `/addplayer record` | Add a full Player Database record as the 15 database columns or JSON. | `/addplayer 1,Pace Bowlers,,Example Player,Bowler,India,82,Right,Right,Fast,70,87,2026,Women,20l` |
| `/add_player_list` | Add multiple detailed CSV rows, header-based CSV, or JSON records. | Put one 15-field player record per line after `/add_player_list`. |
| `/bulkplayer records` | Add detailed CSV/JSON records, or just names for quick entries. | `/bulkplayer [{"name":"Player One","rating":84,"gender":"Women","category":"Bowler"}]` |
| `/clearplayers` | Clear the auction's player list; super admin only. | `/clearplayers` |

### Add players using exactly these 15 fields

```text
Set No,Set Name,Player Id,Player Name,Category,country,Overall,bat_hand,bowl_hand,bowl_style,bat_rating,bowl_rating,version,gender,Base Price
```

`Category` also accepts the spelling `Catagory`. `Overall` is the overall player
rating used by `Rating<=…` rules. These are the only columns in the new player
entry template and player exports. Set names appear in the player table and live
lot cards. Blank Set No defaults to 1; a blank Set Name reuses the known name for
that set, or becomes `Set 1`, `Set 2`, etc. Conflicting names for the same Set No
within a batch are rejected.

1. Run `/downloadtemplate` (or `/downloadtemplate csv` / `json`).
2. Replace the example row with your players, keeping the 15 headers.
3. Leave Player Id empty for new players; keep existing IDs to update matching players in this auction.
4. Send the file and reply `/uploaddata` or `/asetsimport` (a command in the file caption also works).
5. Review with `/asetsreview TOKEN`, then apply with `/asetsconfirm TOKEN`.
6. Export using `/exportplayers xlsx`, `/asetsexport csv`, or `/exportplayers json`.

CSV, XLSX and JSON all use these fields. TSV copied from a spreadsheet is accepted
as text too. Header names are case-insensitive, and legacy `name` / `rating` are
accepted aliases for Player Name / Overall. Existing tier and other
internal auction data are preserved when an existing ID is imported, but those
internal fields are not part of the new player template or exports. Older files
remain readable for compatibility. Base Price accepts whole purse units (e.g. `20`), lakh suffixes (`50l`) or crore suffixes (`2cr`). It sets the minimum opening bid. Omit it on an existing Player Id to preserve its price; a blank value means an open/zero base price.

Example CSV:

```csv
Set No,Set Name,Player Id,Player Name,Category,country,Overall,bat_hand,bowl_hand,bowl_style,bat_rating,bowl_rating,version,gender,Base Price
1,Pace Bowlers,,Example Player,Bowler,India,82,Right,Right,Fast,70,87,2026,Women,20l
```

Paste the same row after `/addplayer`:

```text
/addplayer 1,Pace Bowlers,,Example Player,Bowler,India,82,Right,Right,Fast,70,87,2026,Women,20l
```

For multiple players, put one row per line after `/add_player_list`. JSON can use
explicit field names:

```text
/addplayer {"Set No":1,"Set Name":"Pace Bowlers","Player Name":"Example Player","Category":"Bowler","country":"India","Overall":82,"bat_hand":"Right","bowl_hand":"Right","bowl_style":"Fast","bat_rating":70,"bowl_rating":87,"version":"2026","gender":"Women","Base Price":"20l"}
```

Only Player Name is required to create a record. Missing gender, country or Overall
does not satisfy the corresponding quota, and import previews warn about missing
country when nationality quotas are configured. Use Women/Men (Female/Male/F/M
are accepted). Known country abbreviations such as IND/AUS/ENG are normalized.
For updates, omitted fields preserve stored values; explicit blanks clear optional
values. Sold/drafted squad members are protected. Foreign Player IDs are rejected;
clear IDs to clone player metadata into another auction. Metadata imports do not
recreate sales; full database backups remain the way to restore auction results.

The Players sheet in `/downloaddata` uses the same 15 columns, followed by read-only
sale/status columns; those extra report fields are ignored when re-imported.

## Teams, purses, communication and reporting

| Command | Purpose | Example |
| --- | --- | --- |
| `/acall [message]` | Tag teams and DM owners/co-owners; reply to an image to include it. | `/acall Next set starts now` |
| `/announce message` | Post an auction announcement. | `/announce Break for five minutes` |
| `/aremoveteam team` | Release the team's players and share its refunded budget among remaining teams. | `/aremoveteam Royal Tigers` |
| `/ateamsummary [team]` | Full categorized squads, gender, ratings, spending and limits. | `/ateamsummary Royal Tigers` |
| `/asummary` | Every team's detailed summary. | `/asummary` |
| `/aaudit [json]` | Recent audit events, or download the full log. | `/aaudit json` |
| `/auctionowners` | List owners. | `/auctionowners` |
| `/soldplayers` | Sold players and message links. | `/soldplayers` |
| `/unsoldplayers` | Unsold players. | `/unsoldplayers` |
| `/auctionsummary` | Legacy auction summary. | `/auctionsummary` |
| `/setrtm @user cards former_team` | Assign RTM cards. | `/setrtm @owner 2 MI` |
| `/mute_team @user` | Prevent a team from bidding. | `/mute_team @owner` |
| `/unmute_team @user` | Restore a team's bidding. | `/unmute_team @owner` |
| `/teamup @primary @proxy` | Link a co-owner to the primary owner. | `/teamup @owner @assistant` |
| `/setpurse @user amount` | Set purse balance. | `/setpurse @owner 80cr` |
| `/addpurse @user amount` | Add purse funds. | `/addpurse @owner 5cr` |
| `/deductpurse @user amount` | Deduct purse funds. | `/deductpurse @owner 2cr` |
| `/addtosquad @user name,name` | Legacy manual override: create zero-price players and add to a squad. | `/addtosquad @owner Reserve One,Reserve Two` |
| `/removefromsquad @user position,position` | Remove entries by their position in the squad list. | `/removefromsquad @owner 1,3` |
| `/clearsquad @user` | Legacy manual squad clear. | `/clearsquad @owner` |
| `/swap @user1 @user2` | Swap team purse/squad data. | `/swap @owner @otherowner` |
| `/transfer @from @to player` | Trade a sold player between teams. | `/transfer @owner @otherowner Virat Kohli` |

DM recipients must have started the bot; `/acall` reports failed deliveries.
Manual squad/transfer tools are admin overrides and their database changes are audited.

## Legacy queue and auction controls

Prefer the `/a…` controls above for staged clocks, Player-ID targeting and safe bid
undo/refund behavior. These older commands remain available:

| Command | Purpose / example |
| --- | --- |
| `/startauction` | Mark auction active and prepare the queue; then use `/next`. |
| `/next` | Open the next player: `/next`. |
| `/pass` | Pass the current player without a standing bid: `/pass`. |
| `/sold` | Sell at the standing bid: `/sold`. |
| `/forcesold` | Sell while skipping RTM: `/forcesold`. |
| `/forceauction player_or_#position` | Force a player immediately when the block is empty: `/forceauction #3`. |
| `/pauseauction` | Pause: `/pauseauction`; prefer `/apause` to preserve remaining time. |
| `/resumeauction` | Restore/resume state: `/resumeauction`; prefer `/aresume` to restart the saved clock. |
| `/endauction` | End through a confirmation prompt, without automatic squad completion. |
| `/undo` | Reverse the last sale using its saved snapshot: `/undo`. |
| `/addtoqueue names` | `/addtoqueue Player One,Player Two`. |
| `/addtoqueueunsolds` | Append unsold players: `/addtoqueueunsolds`. |
| `/removefromqueue positions` | `/removefromqueue 1,3`. |
| `/shufflequeue` | Shuffle pending players: `/shufflequeue`. |
| `/swapqueue position position` | `/swapqueue 1 3`. |
| `/clearqueue` | Empty the queue: `/clearqueue`. |
| `/queue [page]` | View pending players: `/queue 1`. |
| `/setsquadlimit minimum maximum` | Legacy squad-size change: `/setsquadlimit 15 17`; prefer validated `/asquad 15_17`. |
| `/setlimit OS,BAT,BOWL,AR,WK` | Legacy **maximum-minimum** pairs: `/setlimit 8-4,12-5,8-3,4-2,2-1`. Unlike `/arule`, maximum comes first. |
| `/autosell seconds_or_off` | Legacy auto-sell setting: `/autosell 30`; use `/atimer` for the active clock policy. |
| `/autonext enable_disable_or_seconds` | Schedule automatic next lots: `/autonext 5`. |
| `/dtime [seconds\|show]` | Legacy timer settings: `/dtime show`; use `/atimer` for bidding-clock behavior. |
| `/bidtimer seconds` | Legacy bid timer setting: `/bidtimer 45`; use `/atimer 45` for the active policy. |
| `/antisnipe seconds` | Legacy anti-snipe setting: `/antisnipe 10`; staged resets are controlled by `/atimer`. |
| `/rtmwindow seconds` | RTM offer window: `/rtmwindow 30`. |
| `/rtmcounter seconds` | RTM counter window: `/rtmcounter 20`. |
| `/rtmdecision seconds` | RTM acceptance window: `/rtmdecision 15`. |
| `/downloaddata [format]` | No argument: full Excel report with an importable Players sheet. With json/csv/xlsx: export all player fields. Example `/downloaddata csv`. |
| `/downloadtemplate [format]` | Download the shared database template: `/downloadtemplate xlsx`, `/downloadtemplate csv`, or `/downloadtemplate json`. |
| `/uploaddata [replace]` | Reply to a CSV/JSON/XLSX file (or put the command in its caption) to preview the full Player Database import. Then apply with `/asetsconfirm TOKEN`. Inline CSV/JSON is also supported. |

## Website pages

- `/draft` — create an auction, manage teams and review spreadsheet imports.
- `/draft/rules` — squad size, role/gender/rating rules and purse reserve.
- `/draft/audit` — latest audit events and full JSON download.
- `/watch` — read-only spectator view, refreshing every two seconds.
- `/watch/data` — read-only JSON snapshot.

Dot shortcuts exist for mapped controls, including `.help`, `.commands`,
`.auctionadmin`, and the new `/a…` family (for example `.arule Women 2_6`).
Slash commands are supported for every command in this guide.

## Slash-command aliases

| Main command | Aliases |
| --- | --- |
| `/start` | `/registration` |
| `/commands` | `/helpmd` |
| `/addteams` | `/addusers` |
| `/setteamname` | `/stn` |
| `/purse` | `/bal`, `/balance` |
| `/rtm` | `/right_to_match` |
| `/myrtm` | `/my_rtm` |
| `/mybidhistory` | `/bidhistory` |
| `/auctionhistory` | `/auction_history` |
| `/create_auction` | `/createauction` |
| `/auctionowners` | `/auction_owners` |
| `/soldplayers` | `/sold_players` |
| `/unsoldplayers` | `/unsold_players` |
| `/auctionsummary` | `/auction_summary` |
| `/bulkplayer` | `/bulk_player` |
| `/mute_team` | `/muteteam` |
| `/unmute_team` | `/unmuteteam` |
| `/setpurse` | `/setbal` |
| `/addpurse` | `/addbal` |
| `/deductpurse` | `/deductbal` |
| `/addtosquad` | `/ats` |
| `/removefromsquad` | `/rfs` |
| `/forceauction` | `/force_auction` |
| `/endauction` | `/endsauction` |
| `/downloaddata` | `/exportdata` |
| `/downloadtemplate` | `/template` |
| `/uploaddata` | `/importdata` |
| `/undo` | `/undosold` |
| `/setlimit` | `/squadlimit` |
| `/setincrement` | `/increment` |
| `/findplayer` | `/search` |
| `/playercard` | `/pc` |
| `/mystats` | `/stats` |
| `/announce` | `/broadcast` |
| `/transfer` | `/tradeplayer` |
| `/dtime` | `/timers` |
| `/bidtimer` | `/bidduration` |
| `/addtoqueue` | `/atq` |
| `/addtoqueueunsolds` | `/atqu` |
| `/removefromqueue` | `/rfq` |
| `/shufflequeue` | `/sq` |
| `/queue` | `/q` |
