"""
IPL Cricket Auction Telegram Bot
Production-ready implementation with full IPL 2026 rules,
RTM system, real-time bidding, and SQLite persistence.
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from flask import Flask, request
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ─────────────────────────────────────────────
# LOGGING SETUP
# ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
    SUPER_ADMIN_ID: int = int(os.getenv("SUPER_ADMIN_ID", "0"))
    WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "")
    PORT: int = int(os.getenv("PORT", "8080"))
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "auction.db")

    # IPL 2026 Rules
    INITIAL_PURSE: int = 125_00_00_000        # 125 Crore in paise (stored as lakhs: 12500)
    MAX_SQUAD: int = 25
    MIN_SQUAD: int = 18
    MAX_OVERSEAS_SQUAD: int = 8
    MAX_OVERSEAS_XI: int = 4
    OVERSEAS_SALARY_CAP: int = 18_00_00_000   # 18 Crore
    MIN_INCREMENT: int = 10_00_000            # 10 Lakhs

    # Auction timers (seconds)
    BID_TIMER: int = 30
    RTM_TIMER: int = 15
    ANTI_SNIPE_THRESHOLD: int = 10
    ANTI_SNIPE_EXTENSION: int = 10


# ─────────────────────────────────────────────
# DATACLASSES
# ─────────────────────────────────────────────
@dataclass
class Player:
    player_id: int
    name: str
    base_price: int          # in Lakhs (e.g. 200 = 2 Crore)
    role: str                # Batsman/Bowler/All-rounder/Wicketkeeper
    nationality: str         # Indian/Overseas
    ipl_team: str            # Previous IPL team
    tier: str                # Marquee/A/B/C/Uncapped
    status: str = "available"
    sold_to: Optional[int] = None
    sold_price: Optional[int] = None
    rtm_eligible: bool = True


@dataclass
class Team:
    team_id: int             # Telegram user ID
    name: str
    purse: int               # in Lakhs
    total_spent: int = 0
    players: list = field(default_factory=list)
    is_admin: bool = False
    rtm_cards: int = 0


@dataclass
class AuctionState:
    active: bool = False
    current_player: Optional[Player] = None
    current_bid: int = 0
    highest_bidder_id: Optional[int] = None
    highest_bidder_name: str = ""
    message_id: Optional[int] = None
    chat_id: Optional[int] = None
    timer_task: Optional[asyncio.Task] = None
    timer_ends_at: Optional[float] = None
    rtm_phase: bool = False
    rtm_team_id: Optional[int] = None
    player_queue: list = field(default_factory=list)
    set_number: int = 1
    sold_count: int = 0
    unsold_count: int = 0


# Singleton auction state
auction = AuctionState()
auction_lock = threading.Lock()

# Flask app for health checks / webhooks
flask_app = Flask(__name__)

# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
class Database:
    """Thread-safe SQLite database wrapper."""
    _local = threading.local()

    def __init__(self, db_path: str = Config.DATABASE_PATH):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._get_conn()

    def _init_db(self):
        """Create tables if they don't exist."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS teams (
                team_id     INTEGER PRIMARY KEY,
                name        TEXT    NOT NULL,
                purse       INTEGER DEFAULT 12500,
                total_spent INTEGER DEFAULT 0,
                players     TEXT    DEFAULT '[]',
                is_admin    INTEGER DEFAULT 0,
                rtm_cards   INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS players (
                player_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT    NOT NULL,
                base_price   INTEGER NOT NULL,
                role         TEXT    NOT NULL,
                nationality  TEXT    NOT NULL,
                ipl_team     TEXT    DEFAULT '',
                tier         TEXT    DEFAULT 'C',
                status       TEXT    DEFAULT 'available',
                sold_to      INTEGER,
                sold_price   INTEGER,
                rtm_eligible INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS auction_history (
                history_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id   INTEGER NOT NULL,
                team_id     INTEGER NOT NULL,
                bid_amount  INTEGER NOT NULL,
                final_price INTEGER NOT NULL,
                rtm_used    INTEGER DEFAULT 0,
                timestamp   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (player_id) REFERENCES players(player_id),
                FOREIGN KEY (team_id)   REFERENCES teams(team_id)
            );
        """)
        conn.commit()
        conn.close()

    # ── TEAM OPERATIONS ──────────────────────

    def register_team(self, user_id: int, name: str) -> bool:
        """Register a new team. Returns True if created, False if exists."""
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO teams (team_id, name) VALUES (?, ?)",
                (user_id, name),
            )
            self.conn.commit()
            return self.conn.execute(
                "SELECT changes()"
            ).fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(f"register_team error: {e}")
            return False

    def get_team(self, user_id: int) -> Optional[Team]:
        row = self.conn.execute(
            "SELECT * FROM teams WHERE team_id=?", (user_id,)
        ).fetchone()
        if not row:
            return None
        return Team(
            team_id=row["team_id"],
            name=row["name"],
            purse=row["purse"],
            total_spent=row["total_spent"],
            players=json.loads(row["players"]),
            is_admin=bool(row["is_admin"]),
            rtm_cards=row["rtm_cards"],
        )

    def get_all_teams(self) -> list[Team]:
        rows = self.conn.execute("SELECT * FROM teams").fetchall()
        return [
            Team(
                team_id=r["team_id"],
                name=r["name"],
                purse=r["purse"],
                total_spent=r["total_spent"],
                players=json.loads(r["players"]),
                is_admin=bool(r["is_admin"]),
                rtm_cards=r["rtm_cards"],
            )
            for r in rows
        ]

    def set_admin(self, user_id: int, is_admin: bool = True) -> bool:
        try:
            self.conn.execute(
                "UPDATE teams SET is_admin=? WHERE team_id=?",
                (1 if is_admin else 0, user_id),
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error(f"set_admin error: {e}")
            return False

    def is_admin(self, user_id: int) -> bool:
        if user_id == Config.SUPER_ADMIN_ID:
            return True
        row = self.conn.execute(
            "SELECT is_admin FROM teams WHERE team_id=?", (user_id,)
        ).fetchone()
        return bool(row and row["is_admin"])

    def update_purse(self, user_id: int, amount: int):
        """Subtract amount from purse and add to total_spent."""
        self.conn.execute(
            "UPDATE teams SET purse=purse-?, total_spent=total_spent+? WHERE team_id=?",
            (amount, amount, user_id),
        )
        self.conn.commit()

    def add_player_to_team(self, user_id: int, player_id: int):
        team = self.get_team(user_id)
        if team:
            players = team.players
            if player_id not in players:
                players.append(player_id)
            self.conn.execute(
                "UPDATE teams SET players=? WHERE team_id=?",
                (json.dumps(players), user_id),
            )
            self.conn.commit()

    # ── PLAYER OPERATIONS ────────────────────

    def add_player(self, player: Player) -> int:
        """Insert a player; returns new player_id."""
        cursor = self.conn.execute(
            """INSERT INTO players
               (name, base_price, role, nationality, ipl_team, tier, status, rtm_eligible)
               VALUES (?, ?, ?, ?, ?, ?, 'available', ?)""",
            (
                player.name,
                player.base_price,
                player.role,
                player.nationality,
                player.ipl_team,
                player.tier,
                1 if player.rtm_eligible else 0,
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_player(self, player_id: int) -> Optional[Player]:
        row = self.conn.execute(
            "SELECT * FROM players WHERE player_id=?", (player_id,)
        ).fetchone()
        return self._row_to_player(row) if row else None

    def get_available_players(self) -> list[Player]:
        rows = self.conn.execute(
            "SELECT * FROM players WHERE status='available' ORDER BY player_id"
        ).fetchall()
        return [self._row_to_player(r) for r in rows]

    def get_unsold_players(self) -> list[Player]:
        rows = self.conn.execute(
            "SELECT * FROM players WHERE status='unsold'"
        ).fetchall()
        return [self._row_to_player(r) for r in rows]

    def update_player_status(
        self,
        player_id: int,
        status: str,
        sold_to: Optional[int] = None,
        sold_price: Optional[int] = None,
    ):
        self.conn.execute(
            "UPDATE players SET status=?, sold_to=?, sold_price=? WHERE player_id=?",
            (status, sold_to, sold_price, player_id),
        )
        self.conn.commit()

    def _row_to_player(self, row) -> Player:
        return Player(
            player_id=row["player_id"],
            name=row["name"],
            base_price=row["base_price"],
            role=row["role"],
            nationality=row["nationality"],
            ipl_team=row["ipl_team"] or "",
            tier=row["tier"],
            status=row["status"],
            sold_to=row["sold_to"],
            sold_price=row["sold_price"],
            rtm_eligible=bool(row["rtm_eligible"]),
        )

    def record_history(
        self,
        player_id: int,
        team_id: int,
        bid_amount: int,
        final_price: int,
        rtm_used: bool = False,
    ):
        self.conn.execute(
            """INSERT INTO auction_history
               (player_id, team_id, bid_amount, final_price, rtm_used)
               VALUES (?, ?, ?, ?, ?)""",
            (player_id, team_id, bid_amount, final_price, 1 if rtm_used else 0),
        )
        self.conn.commit()

    def clear_players(self):
        self.conn.execute("DELETE FROM players")
        self.conn.commit()


# Global DB instance
db = Database()


# ─────────────────────────────────────────────
# HELPERS / FORMATTERS
# ─────────────────────────────────────────────

def lakhs_to_str(amount_l: int) -> str:
    """Convert Lakhs integer to human-readable Crore/Lakh string."""
    if amount_l >= 100:
        crores = amount_l / 100
        return f"₹{crores:.1f}Cr" if crores % 1 else f"₹{int(crores)}Cr"
    return f"₹{amount_l}L"


def parse_price(price_str: str) -> Optional[int]:
    """
    Parse price strings like '2cr', '50L', '200', '2.5cr' → Lakhs integer.
    Returns None on failure.
    """
    price_str = price_str.strip().lower().replace(" ", "")
    try:
        if price_str.endswith("cr"):
            val = float(price_str[:-2])
            return int(val * 100)
        elif price_str.endswith("l"):
            return int(float(price_str[:-1]))
        else:
            return int(price_str)
    except ValueError:
        return None


def flag(nationality: str) -> str:
    return "🇮🇳" if nationality.lower() == "indian" else "🌍"


def role_emoji(role: str) -> str:
    mapping = {
        "batsman": "🏏",
        "bat": "🏏",
        "bowler": "⚡",
        "bowl": "⚡",
        "all-rounder": "🌟",
        "allrounder": "🌟",
        "ar": "🌟",
        "wicketkeeper": "🧤",
        "wk": "🧤",
        "keeper": "🧤",
    }
    return mapping.get(role.lower(), "🏏")


def tier_stars(tier: str) -> str:
    return {"Marquee": "⭐⭐⭐", "A": "⭐⭐", "B": "⭐", "C": "🔹", "Uncapped": "🔸"}.get(
        tier, "🔹"
    )


def normalize_role(role_str: str) -> str:
    """Normalize role input to standard format."""
    r = role_str.lower().strip()
    if r in ("bat", "batsman", "batter"):
        return "Batsman"
    if r in ("bowl", "bowler"):
        return "Bowler"
    if r in ("ar", "allrounder", "all-rounder", "all_rounder"):
        return "All-rounder"
    if r in ("wk", "wicketkeeper", "keeper", "wicket-keeper"):
        return "Wicketkeeper"
    return role_str.title()


def normalize_nationality(nat: str) -> str:
    n = nat.lower().strip()
    if n in ("indian", "india", "ind", "domestic"):
        return "Indian"
    return "Overseas"


def build_auction_message(player: Player, current_bid: int, bidder_name: str) -> str:
    """Build the live auction display message."""
    nationality_flag = flag(player.nationality)
    r_emoji = role_emoji(player.role)
    t_stars = tier_stars(player.tier)

    bid_line = (
        f"💵 *Current Bid:* {lakhs_to_str(current_bid)}\n"
        f"👤 *Highest Bidder:* {bidder_name or 'None'}"
        if current_bid > 0
        else f"💵 *Base Price:* {lakhs_to_str(player.base_price)}\n👤 *No bids yet*"
    )

    rtm_line = ""
    if player.ipl_team:
        rtm_line = f"\n🎴 *RTM Available:* {player.ipl_team} can match!\n"

    return (
        f"🔨 *NOW AUCTIONING* — Set {auction.set_number}\n"
        f"{'═' * 24}\n"
        f"{r_emoji} *{nationality_flag} {player.name}*\n"
        f"🎯 {player.role} | {player.nationality}\n"
        f"🏆 Tier: {t_stars} {player.tier}\n"
        f"🔴 Base: {lakhs_to_str(player.base_price)}\n"
        f"🏟️ Previous Team: {player.ipl_team or 'None'}\n"
        f"{rtm_line}\n"
        f"{bid_line}\n"
        f"⏱️ *Timer:* Starts on first bid"
    )


def build_bid_buttons(player: Player, current_bid: int) -> InlineKeyboardMarkup:
    """Build inline keyboard for bidding."""
    bid_amount = max(player.base_price, current_bid)
    next_bid = bid_amount if current_bid == 0 else current_bid + Config.MIN_INCREMENT // 1_00_000  # in Lakhs

    # Keep increments in Lakhs
    base_l = player.base_price
    next_l = base_l if current_bid == 0 else current_bid + 10  # +10 Lakhs

    buttons = [
        [
            InlineKeyboardButton(
                f"💰 Bid {lakhs_to_str(next_l)}", callback_data=f"bid_{next_l}"
            ),
            InlineKeyboardButton(
                f"➕ +₹10L ({lakhs_to_str(next_l + 10)})",
                callback_data=f"bid_{next_l + 10}",
            ),
        ],
        [
            InlineKeyboardButton("📊 My Purse", callback_data="check_purse"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def validate_bid(team: Team, player: Player, bid_amount_l: int) -> Optional[str]:
    """
    Validate a bid. Returns error message string if invalid, None if valid.
    bid_amount_l is in Lakhs.
    """
    # Purse check
    if team.purse < bid_amount_l:
        return f"❌ Insufficient purse! You have {lakhs_to_str(team.purse)} left."

    # Squad size check
    squad_ids: list[int] = team.players
    squad_players = [db.get_player(pid) for pid in squad_ids if db.get_player(pid)]

    if len(squad_players) >= Config.MAX_SQUAD:
        return f"❌ Squad full! Max {Config.MAX_SQUAD} players allowed."

    # Overseas limit
    if player.nationality == "Overseas":
        overseas_count = sum(1 for p in squad_players if p and p.nationality == "Overseas")
        if overseas_count >= Config.MAX_OVERSEAS_SQUAD:
            return f"❌ Overseas limit reached! Max {Config.MAX_OVERSEAS_SQUAD} overseas players."

        overseas_spent = sum(
            p.sold_price for p in squad_players
            if p and p.nationality == "Overseas" and p.sold_price
        )
        overseas_salary_cap_l = 1800  # 18 Crore in Lakhs
        if (overseas_spent or 0) + bid_amount_l > overseas_salary_cap_l:
            return f"❌ Overseas salary cap exceeded! Max ₹18Cr on overseas players."

    # Minimum bid check
    if bid_amount_l < player.base_price:
        return f"❌ Bid must be at least {lakhs_to_str(player.base_price)}."

    if auction.current_bid > 0 and bid_amount_l <= auction.current_bid:
        return f"❌ Bid must be higher than current bid of {lakhs_to_str(auction.current_bid)}."

    return None  # Valid


# ─────────────────────────────────────────────
# TIMER LOGIC
# ─────────────────────────────────────────────

async def auction_timer(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    message_id: int,
    duration: int,
    is_rtm: bool = False,
):
    """Countdown timer for bidding. Edits message with time remaining."""
    import time

    end_time = time.time() + duration
    auction.timer_ends_at = end_time

    # Update loop every 5 seconds
    remaining = duration
    while remaining > 0:
        await asyncio.sleep(min(5, remaining))
        remaining = max(0, int(end_time - time.time()))

        if not auction.active:
            return

        # Refresh the message with time update
        player = auction.current_player
        if not player:
            return

        bidder_name = auction.highest_bidder_name
        current_bid = auction.current_bid
        phase_label = "🎴 RTM Phase" if is_rtm else "⏱️"

        try:
            text = build_auction_message(player, current_bid, bidder_name)
            text = text.replace(
                "⏱️ *Timer:* Starts on first bid",
                f"{phase_label} *Time left:* {remaining}s",
            )
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_bid_buttons(player, current_bid) if not is_rtm else None,
            )
        except Exception:
            pass  # Message may not have changed

    # Timer expired
    if is_rtm:
        await handle_rtm_expiry(context, chat_id)
    else:
        await handle_timer_expiry(context, chat_id, message_id)


async def handle_timer_expiry(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int
):
    """Called when main bid timer expires."""
    if not auction.active or not auction.current_player:
        return

    player = auction.current_player

    if auction.current_bid == 0:
        # No bids — mark unsold
        db.update_player_status(player.player_id, "unsold")
        auction.unsold_count += 1
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ *{player.name}* goes UNSOLD! No bids received.",
            parse_mode=ParseMode.MARKDOWN,
        )
        auction.current_player = None
        return

    # Check RTM eligibility
    if player.ipl_team and player.rtm_eligible:
        rtm_team = find_team_by_ipl_name(player.ipl_team)
        if rtm_team and rtm_team.team_id != auction.highest_bidder_id:
            # Validate RTM team can afford it
            error = validate_bid(rtm_team, player, auction.current_bid)
            if not error:
                auction.rtm_phase = True
                auction.rtm_team_id = rtm_team.team_id
                rtm_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🎴 *RTM OPPORTUNITY!*\n"
                        f"{'═' * 24}\n"
                        f"*{player.name}* has {auction.current_bid}Cr bid by "
                        f"*{auction.highest_bidder_name}*\n\n"
                        f"🏟️ *{player.ipl_team}* has RIGHT TO MATCH!\n"
                        f"You have *{Config.RTM_TIMER} seconds* to match "
                        f"{lakhs_to_str(auction.current_bid)}\n\n"
                        f"Reply with /rtm to use your RTM card!"
                    ),
                    parse_mode=ParseMode.MARKDOWN,
                )
                # Start RTM timer
                auction.timer_task = asyncio.create_task(
                    auction_timer(
                        context,
                        chat_id,
                        rtm_msg.message_id,
                        Config.RTM_TIMER,
                        is_rtm=True,
                    )
                )
                return

    # No RTM or RTM team can't afford — finalize sale
    await finalize_sale(context, chat_id, rtm_used=False)


async def handle_rtm_expiry(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Called when RTM timer expires without response."""
    auction.rtm_phase = False
    auction.rtm_team_id = None
    await context.bot.send_message(
        chat_id=chat_id,
        text="⏰ RTM window expired! Proceeding with highest bidder.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await finalize_sale(context, chat_id, rtm_used=False)


async def finalize_sale(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, rtm_used: bool = False
):
    """Transfer player to winning team and update DB."""
    player = auction.current_player
    if not player or not auction.highest_bidder_id:
        return

    winner_id = auction.highest_bidder_id
    final_price = auction.current_bid
    winner_name = auction.highest_bidder_name

    # Update DB
    db.update_player_status(player.player_id, "sold", winner_id, final_price)
    db.update_purse(winner_id, final_price)
    db.add_player_to_team(winner_id, player.player_id)
    db.record_history(player.player_id, winner_id, final_price, final_price, rtm_used)

    auction.sold_count += 1
    auction.current_player = None
    auction.current_bid = 0
    auction.highest_bidder_id = None
    auction.highest_bidder_name = ""
    auction.rtm_phase = False
    auction.rtm_team_id = None

    winner_team = db.get_team(winner_id)
    remaining_purse = winner_team.purse if winner_team else 0

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🔨 *SOLD!*\n"
            f"{'═' * 24}\n"
            f"🏏 *{player.name}* → 🏆 *{winner_name}*\n"
            f"💰 Final Price: *{lakhs_to_str(final_price)}*\n"
            f"{'🎴 RTM Used!' if rtm_used else ''}\n\n"
            f"💼 {winner_name}'s remaining purse: {lakhs_to_str(remaining_purse)}\n\n"
            f"Admin: use /next for next player or /endauction to finish."
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


def find_team_by_ipl_name(ipl_name: str) -> Optional[Team]:
    """Find a registered team whose name matches a previous IPL team."""
    teams = db.get_all_teams()
    ipl_name_lower = ipl_name.lower()
    for team in teams:
        if ipl_name_lower in team.name.lower() or team.name.lower() in ipl_name_lower:
            return team
    return None


# ─────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Register user and show welcome message."""
    user = update.effective_user
    existing = db.get_team(user.id)

    if existing:
        await update.message.reply_text(
            f"✅ Welcome back, *{existing.name}*!\n"
            f"💼 Your purse: {lakhs_to_str(existing.purse)}\n"
            f"👥 Squad: {len(existing.players)} players\n\n"
            f"Use /purse for details or /help for commands.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    db.register_team(user.id, user.first_name)

    # Auto-promote SUPER_ADMIN
    if user.id == Config.SUPER_ADMIN_ID:
        db.set_admin(user.id, True)
        admin_note = "\n🔑 *You are the Super Admin!*"
    else:
        admin_note = ""

    await update.message.reply_text(
        f"🏏 *Welcome to IPL Auction 2026!*\n"
        f"{'═' * 24}\n"
        f"👤 Team: *{user.first_name}*\n"
        f"💰 Starting Purse: *₹125 Crore*\n"
        f"📋 Max Squad: 25 players\n"
        f"🌍 Max Overseas: 8 in squad\n"
        f"{admin_note}\n\n"
        f"Commands:\n"
        f"/purse — Check your purse & squad\n"
        f"/squad — View your squad\n"
        f"/leaderboard — See all teams\n"
        f"/help — All commands",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_adm = db.is_admin(user_id)

    admin_cmds = ""
    if is_adm:
        admin_cmds = (
            "\n\n🔑 *Admin Commands:*\n"
            "/startauction — Begin auction\n"
            "/next — Next player\n"
            "/sold — Confirm sale\n"
            "/forcesold — Force sell (no RTM)\n"
            "/endauction — End & show summary\n"
            "/addplayer — Add single player\n"
            "/add\\_player\\_list — Bulk add players\n"
            "/admin <user\\_id> — Promote admin\n"
            "/removeadmin <user\\_id> — Remove admin\n"
            "/queue — View player queue\n"
            "/unsoldqueue — View unsold players\n"
            "/clearplayers — Clear all players\n"
            "/squad <user\\_id> — View anyone's squad"
        )

    await update.message.reply_text(
        f"🏏 *IPL Auction Bot — Help*\n"
        f"{'═' * 24}\n"
        f"👤 *User Commands:*\n"
        f"/start — Register / Welcome\n"
        f"/purse — Check purse & squad info\n"
        f"/squad — View your players\n"
        f"/bid <amount> — Manual bid (e.g. /bid 2cr)\n"
        f"/rtm — Use RTM card\n"
        f"/leaderboard — Top spenders\n"
        f"{admin_cmds}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_purse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    team = db.get_team(user.id)
    if not team:
        await update.message.reply_text("❌ Not registered! Use /start first.")
        return

    squad_ids: list[int] = team.players
    squad = [db.get_player(pid) for pid in squad_ids if db.get_player(pid)]
    indian_count = sum(1 for p in squad if p and p.nationality == "Indian")
    overseas_count = sum(1 for p in squad if p and p.nationality == "Overseas")
    roles = {r: sum(1 for p in squad if p and p.role == r)
             for r in ["Batsman", "Bowler", "All-rounder", "Wicketkeeper"]}

    await update.message.reply_text(
        f"💼 *{team.name}'s Purse*\n"
        f"{'═' * 24}\n"
        f"💰 Remaining: *{lakhs_to_str(team.purse)}*\n"
        f"💸 Spent: {lakhs_to_str(team.total_spent)}\n"
        f"👥 Squad: {len(squad)} / {Config.MAX_SQUAD}\n"
        f"🇮🇳 Indian: {indian_count}\n"
        f"🌍 Overseas: {overseas_count} / {Config.MAX_OVERSEAS_SQUAD}\n\n"
        f"📋 *By Role:*\n"
        + "\n".join(f"  {role_emoji(r)} {r}: {c}" for r, c in roles.items())
        + f"\n\n🎴 RTM Cards: {team.rtm_cards}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_squad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args

    # Admins can check anyone's squad
    if args and db.is_admin(user.id):
        try:
            target_id = int(args[0])
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID.")
            return
        team = db.get_team(target_id)
        if not team:
            await update.message.reply_text("❌ Team not found.")
            return
    else:
        team = db.get_team(user.id)
        if not team:
            await update.message.reply_text("❌ Not registered! Use /start first.")
            return

    squad_ids: list[int] = team.players
    squad = [db.get_player(pid) for pid in squad_ids if db.get_player(pid)]

    if not squad:
        await update.message.reply_text(
            f"🏏 *{team.name}'s Squad* — Empty\nNo players purchased yet."
        )
        return

    # Group by role
    by_role: dict[str, list[Player]] = {}
    for p in squad:
        by_role.setdefault(p.role, []).append(p)

    lines = [f"🏏 *{team.name}'s Squad* ({len(squad)} players)\n{'═' * 24}"]
    for role, players in by_role.items():
        lines.append(f"\n{role_emoji(role)} *{role}s*")
        for p in players:
            lines.append(
                f"  {flag(p.nationality)} {p.name} — {lakhs_to_str(p.sold_price or p.base_price)}"
            )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_add_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add single player: /addplayer <name> <role> <team> <nationality> <price>"""
    user_id = update.effective_user.id
    if not db.is_admin(user_id):
        await update.message.reply_text("❌ Admin only.")
        return

    args = context.args
    if len(args) < 5:
        await update.message.reply_text(
            "❌ Usage: /addplayer <name> <role> <prev_team> <nationality> <base_price>\n"
            "Example: /addplayer ViratKohli Bat RCB Indian 2cr"
        )
        return

    name = args[0].replace("_", " ")
    role = normalize_role(args[1])
    ipl_team = args[2].replace("_", " ")
    nationality = normalize_nationality(args[3])
    price = parse_price(args[4])

    if price is None:
        await update.message.reply_text("❌ Invalid price format. Use: 2cr, 50L, 200")
        return

    tier = args[5].capitalize() if len(args) > 5 else "C"

    player = Player(
        player_id=0,
        name=name,
        base_price=price,
        role=role,
        nationality=nationality,
        ipl_team=ipl_team,
        tier=tier,
    )
    new_id = db.add_player(player)

    await update.message.reply_text(
        f"✅ Player added!\n"
        f"ID: {new_id} | {flag(nationality)} *{name}*\n"
        f"{role_emoji(role)} {role} | {nationality} | {lakhs_to_str(price)}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_add_player_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Bulk add players from multiline message:
    /add_player_list
    1 Virat Kohli, Bat, RCB, Indian, 2cr
    2 David Warner, Bat, DC, Overseas, 3cr
    """
    user_id = update.effective_user.id
    if not db.is_admin(user_id):
        await update.message.reply_text("❌ Admin only.")
        return

    text = update.message.text
    lines = text.strip().split("\n")[1:]  # Skip command line

    added, failed = [], []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Remove leading number
        line = re.sub(r"^\d+[\.\)]\s*", "", line)

        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            failed.append(f"Bad format: {line[:40]}")
            continue

        name = parts[0]
        role = normalize_role(parts[1])
        ipl_team = parts[2]
        nationality = normalize_nationality(parts[3])
        price = parse_price(parts[4])
        tier = parts[5].capitalize() if len(parts) > 5 else "C"

        if not price:
            failed.append(f"Bad price: {line[:40]}")
            continue

        player = Player(
            player_id=0,
            name=name,
            base_price=price,
            role=role,
            nationality=nationality,
            ipl_team=ipl_team,
            tier=tier,
        )
        db.add_player(player)
        added.append(name)

    msg = f"✅ Added {len(added)} players!\n"
    if added:
        msg += "\n".join(f"  • {n}" for n in added[:20])
        if len(added) > 20:
            msg += f"\n  ...and {len(added) - 20} more"
    if failed:
        msg += f"\n\n❌ Failed ({len(failed)}):\n" + "\n".join(failed[:5])

    await update.message.reply_text(msg)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Promote a user to admin: /admin <user_id>"""
    user_id = update.effective_user.id
    if user_id != Config.SUPER_ADMIN_ID:
        await update.message.reply_text("❌ Only Super Admin can promote admins.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /admin <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    target = db.get_team(target_id)
    if not target:
        await update.message.reply_text(
            f"❌ User {target_id} not registered. They must /start first."
        )
        return

    db.set_admin(target_id, True)
    await update.message.reply_text(
        f"✅ *{target.name}* is now an admin!", parse_mode=ParseMode.MARKDOWN
    )


async def cmd_remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != Config.SUPER_ADMIN_ID:
        await update.message.reply_text("❌ Only Super Admin can remove admins.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /removeadmin <user_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")
        return

    db.set_admin(target_id, False)
    await update.message.reply_text(f"✅ Removed admin from user {target_id}.")


async def cmd_start_auction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the auction session."""
    user_id = update.effective_user.id
    if not db.is_admin(user_id):
        await update.message.reply_text("❌ Admin only.")
        return

    if auction.active:
        await update.message.reply_text("⚠️ Auction already in progress!")
        return

    players = db.get_available_players()
    if not players:
        await update.message.reply_text(
            "❌ No available players. Add players first with /addplayer or /add_player_list"
        )
        return

    auction.active = True
    auction.player_queue = players
    auction.set_number = 1
    auction.sold_count = 0
    auction.unsold_count = 0
    auction.chat_id = update.effective_chat.id

    await update.message.reply_text(
        f"🏏 *IPL AUCTION 2026 STARTED!*\n"
        f"{'═' * 24}\n"
        f"📋 {len(players)} players in queue\n"
        f"💰 Each team starts with ₹125Cr\n\n"
        f"Admin: use /next to bring the first player!",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bring next player to auction block."""
    user_id = update.effective_user.id
    if not db.is_admin(user_id):
        await update.message.reply_text("❌ Admin only.")
        return

    if not auction.active:
        await update.message.reply_text("❌ No active auction. Use /startauction first.")
        return

    if auction.current_player:
        await update.message.reply_text(
            "⚠️ Current player still up for auction!\n"
            "Use /sold or /forcesold to finalize, or wait for the timer."
        )
        return

    if not auction.player_queue:
        # Reload unsold players
        unsold = db.get_unsold_players()
        if unsold:
            auction.player_queue = unsold
            await update.message.reply_text(
                f"♻️ Loading {len(unsold)} unsold players back into queue..."
            )
        else:
            await update.message.reply_text(
                "✅ All players have been auctioned!\nUse /endauction for summary."
            )
            return

    # Dequeue next player
    player = auction.player_queue.pop(0)
    # Re-fetch fresh from DB
    fresh = db.get_player(player.player_id)
    if not fresh or fresh.status != "available":
        # Skip already-sold/removed
        await cmd_next(update, context)
        return

    auction.current_player = fresh
    auction.current_bid = 0
    auction.highest_bidder_id = None
    auction.highest_bidder_name = ""
    auction.rtm_phase = False
    auction.rtm_team_id = None

    chat_id = update.effective_chat.id
    msg_text = build_auction_message(fresh, 0, "")
    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=msg_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_bid_buttons(fresh, 0),
    )
    auction.message_id = sent.message_id
    auction.chat_id = chat_id


async def cmd_bid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual bid: /bid <amount>"""
    user_id = update.effective_user.id
    team = db.get_team(user_id)
    if not team:
        await update.message.reply_text("❌ Not registered! Use /start first.")
        return

    if not auction.active or not auction.current_player:
        await update.message.reply_text("❌ No player currently up for auction.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /bid <amount> (e.g. /bid 2cr or /bid 150)")
        return

    bid_l = parse_price(context.args[0])
    if bid_l is None:
        await update.message.reply_text("❌ Invalid amount. Use: /bid 2cr or /bid 200")
        return

    await process_bid(update, context, user_id, team, bid_l)


async def process_bid(
    update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    team: Team,
    bid_l: int,
):
    """Core bid processing logic used by both button and command bids."""
    player = auction.current_player
    if not player:
        return

    error = validate_bid(team, player, bid_l)
    if error:
        if update.callback_query:
            await update.callback_query.answer(error, show_alert=True)
        else:
            await update.message.reply_text(error)
        return

    import time

    # Anti-snipe: extend timer if bid in last 10 seconds
    remaining = (
        int(auction.timer_ends_at - time.time()) if auction.timer_ends_at else None
    )
    if remaining is not None and remaining < Config.ANTI_SNIPE_THRESHOLD:
        extension = Config.ANTI_SNIPE_EXTENSION
        auction.timer_ends_at = time.time() + extension
        snipe_notice = f"⚠️ Anti-snipe! Timer extended by {extension}s"
    else:
        snipe_notice = ""

    # Register bid
    auction.current_bid = bid_l
    auction.highest_bidder_id = user_id
    auction.highest_bidder_name = team.name

    # Start timer on first bid
    if auction.timer_task is None or auction.timer_task.done():
        auction.timer_task = asyncio.create_task(
            auction_timer(
                context,
                auction.chat_id,
                auction.message_id,
                Config.BID_TIMER,
            )
        )

    # Update auction message
    try:
        text = build_auction_message(player, bid_l, team.name)
        text = text.replace(
            "⏱️ *Timer:* Starts on first bid",
            f"⏱️ *Time left:* {Config.BID_TIMER}s",
        )
        if snipe_notice:
            text += f"\n{snipe_notice}"
        await context.bot.edit_message_text(
            chat_id=auction.chat_id,
            message_id=auction.message_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_bid_buttons(player, bid_l),
        )
    except Exception as e:
        logger.warning(f"Edit message error: {e}")

    if update.callback_query:
        await update.callback_query.answer(f"✅ Bid of {lakhs_to_str(bid_l)} placed!")
    else:
        await update.message.reply_text(
            f"✅ {team.name} bids *{lakhs_to_str(bid_l)}*!", parse_mode=ParseMode.MARKDOWN
        )


async def cmd_rtm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Use RTM card to match current bid."""
    user_id = update.effective_user.id
    team = db.get_team(user_id)
    if not team:
        await update.message.reply_text("❌ Not registered!")
        return

    if not auction.rtm_phase:
        await update.message.reply_text("❌ No RTM phase active right now.")
        return

    if auction.rtm_team_id != user_id:
        player = auction.current_player
        expected_team = player.ipl_team if player else "unknown"
        await update.message.reply_text(
            f"❌ Only *{expected_team}* can use RTM for this player!",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if team.rtm_cards <= 0:
        await update.message.reply_text("❌ No RTM cards remaining!")
        return

    # Cancel RTM timer
    if auction.timer_task and not auction.timer_task.done():
        auction.timer_task.cancel()

    # Deduct RTM card
    db.conn.execute(
        "UPDATE teams SET rtm_cards=rtm_cards-1 WHERE team_id=?", (user_id,)
    )
    db.conn.commit()

    rtm_match_amount = auction.current_bid
    prev_bidder_id = auction.highest_bidder_id
    prev_bidder_name = auction.highest_bidder_name

    # IPL 2025 rule: after RTM, previous highest bidder can raise
    auction.rtm_phase = False

    await context.bot.send_message(
        chat_id=auction.chat_id,
        text=(
            f"🎴 *RTM USED!*\n"
            f"*{team.name}* matches {lakhs_to_str(rtm_match_amount)}!\n\n"
            f"*{prev_bidder_name}* — you can now RAISE the bid!\n"
            f"Use /bid <higher amount> within 15 seconds to counter.\n"
            f"Otherwise {team.name} gets the player."
        ),
        parse_mode=ParseMode.MARKDOWN,
    )

    # Give previous bidder 15s to counter
    auction.highest_bidder_id = user_id
    auction.highest_bidder_name = team.name
    auction.rtm_team_id = None

    # Start a new short timer for counter-bid
    auction.timer_task = asyncio.create_task(
        auction_timer(context, auction.chat_id, auction.message_id, 15, is_rtm=False)
    )


async def cmd_sold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin confirms sale to current highest bidder."""
    user_id = update.effective_user.id
    if not db.is_admin(user_id):
        await update.message.reply_text("❌ Admin only.")
        return

    if not auction.active or not auction.current_player:
        await update.message.reply_text("❌ No active auction player.")
        return

    if not auction.highest_bidder_id:
        await update.message.reply_text("❌ No bids yet! Use /next to skip or wait.")
        return

    if auction.timer_task and not auction.timer_task.done():
        auction.timer_task.cancel()

    await finalize_sale(context, update.effective_chat.id, rtm_used=False)


async def cmd_force_sold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force sell player immediately, skipping RTM."""
    user_id = update.effective_user.id
    if not db.is_admin(user_id):
        await update.message.reply_text("❌ Admin only.")
        return

    if not auction.active or not auction.current_player:
        await update.message.reply_text("❌ No active auction player.")
        return

    if not auction.highest_bidder_id:
        await update.message.reply_text("❌ No bids yet!")
        return

    if auction.timer_task and not auction.timer_task.done():
        auction.timer_task.cancel()

    auction.rtm_phase = False
    auction.rtm_team_id = None
    await finalize_sale(context, update.effective_chat.id, rtm_used=False)


async def cmd_end_auction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """End auction and show comprehensive summary."""
    user_id = update.effective_user.id
    if not db.is_admin(user_id):
        await update.message.reply_text("❌ Admin only.")
        return

    if not auction.active:
        await update.message.reply_text("❌ No active auction.")
        return

    if auction.timer_task and not auction.timer_task.done():
        auction.timer_task.cancel()

    auction.active = False
    auction.current_player = None

    teams = db.get_all_teams()
    # Sort by total spent
    teams_sorted = sorted(teams, key=lambda t: t.total_spent, reverse=True)

    summary_lines = [
        f"🏆 *IPL AUCTION 2026 — FINAL SUMMARY*\n{'═' * 26}\n"
        f"✅ Sold: {auction.sold_count} | ❌ Unsold: {auction.unsold_count}\n"
    ]

    for i, team in enumerate(teams_sorted, 1):
        squad = [db.get_player(pid) for pid in team.players if db.get_player(pid)]
        overseas = sum(1 for p in squad if p and p.nationality == "Overseas")
        summary_lines.append(
            f"#{i} 🏏 *{team.name}*\n"
            f"   💸 Spent: {lakhs_to_str(team.total_spent)} | 💰 Left: {lakhs_to_str(team.purse)}\n"
            f"   👥 Squad: {len(squad)} | 🌍 Overseas: {overseas}"
        )

    await update.message.reply_text(
        "\n\n".join(summary_lines), parse_mode=ParseMode.MARKDOWN
    )


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show upcoming players in queue."""
    user_id = update.effective_user.id
    if not db.is_admin(user_id):
        await update.message.reply_text("❌ Admin only.")
        return

    if not auction.active:
        await update.message.reply_text("❌ No active auction.")
        return

    queue = auction.player_queue[:15]
    if not queue:
        await update.message.reply_text("📭 Queue is empty.")
        return

    lines = [f"📋 *Upcoming Players* ({len(auction.player_queue)} total)\n"]
    for i, p in enumerate(queue, 1):
        lines.append(
            f"{i}. {flag(p.nationality)} {p.name} — {role_emoji(p.role)} {p.role} | {lakhs_to_str(p.base_price)}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_unsold_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all unsold players."""
    unsold = db.get_unsold_players()
    if not unsold:
        await update.message.reply_text("✅ No unsold players.")
        return

    lines = [f"❌ *Unsold Players* ({len(unsold)} total)\n"]
    for p in unsold:
        lines.append(
            f"• {flag(p.nationality)} {p.name} — {role_emoji(p.role)} | Base: {lakhs_to_str(p.base_price)}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show spending leaderboard and top players."""
    teams = sorted(db.get_all_teams(), key=lambda t: t.total_spent, reverse=True)

    lines = [f"🏆 *IPL Auction Leaderboard*\n{'═' * 24}\n"]
    medals = ["🥇", "🥈", "🥉"]
    for i, team in enumerate(teams, 1):
        medal = medals[i - 1] if i <= 3 else f"#{i}"
        lines.append(
            f"{medal} *{team.name}*\n"
            f"   💸 {lakhs_to_str(team.total_spent)} spent | {len(team.players)} players"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_clear_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all players (Super Admin only)."""
    user_id = update.effective_user.id
    if user_id != Config.SUPER_ADMIN_ID:
        await update.message.reply_text("❌ Super Admin only.")
        return

    db.clear_players()
    await update.message.reply_text("✅ All players cleared from database.")


# ─────────────────────────────────────────────
# CALLBACK QUERY (BUTTON PRESSES)
# ─────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data == "check_purse":
        team = db.get_team(user_id)
        if not team:
            await query.answer("❌ Not registered!", show_alert=True)
            return
        squad = len(team.players)
        await query.answer(
            f"💼 {team.name}\n"
            f"💰 Purse: {lakhs_to_str(team.purse)}\n"
            f"👥 Squad: {squad}/{Config.MAX_SQUAD}",
            show_alert=True,
        )
        return

    if data.startswith("bid_"):
        team = db.get_team(user_id)
        if not team:
            await query.answer("❌ Register first with /start!", show_alert=True)
            return

        if not auction.active or not auction.current_player:
            await query.answer("❌ No active auction!", show_alert=True)
            return

        try:
            bid_l = int(data.split("_")[1])
        except (ValueError, IndexError):
            await query.answer("❌ Invalid bid.", show_alert=True)
            return

        await process_bid(update, context, user_id, team, bid_l)


# ─────────────────────────────────────────────
# FLASK ROUTES (health + webhook on ONE port)
# ─────────────────────────────────────────────

# PTB application reference (set in main)
_ptb_app: Optional[Application] = None


@flask_app.route("/health")
def health():
    return {"status": "ok", "auction_active": auction.active}, 200


@flask_app.route("/")
def root():
    return "🏏 IPL Auction Bot is running!", 200


@flask_app.route(f"/webhook", methods=["POST"])
def webhook():
    """Receive Telegram updates via webhook on the same port as Flask."""
    import json as _json
    from telegram import Update as _Update

    if _ptb_app is None:
        return "Bot not ready", 503

    data = request.get_json(force=True)
    update = _Update.de_json(data, _ptb_app.bot)

    # Process update in the bot's event loop
    asyncio.run_coroutine_threadsafe(
        _ptb_app.process_update(update),
        _ptb_app.update_queue._loop if hasattr(_ptb_app.update_queue, "_loop") else asyncio.get_event_loop(),
    )
    return "ok", 200


# ─────────────────────────────────────────────
# APPLICATION SETUP & MAIN
# ─────────────────────────────────────────────

def build_application() -> Application:
    app = Application.builder().token(Config.BOT_TOKEN).build()

    # Register command handlers
    app.add_handler(CommandHandler(["start", "registration"], cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("purse", cmd_purse))
    app.add_handler(CommandHandler("squad", cmd_squad))
    app.add_handler(CommandHandler("bid", cmd_bid))
    app.add_handler(CommandHandler("rtm", cmd_rtm))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))

    # Admin commands
    app.add_handler(CommandHandler("addplayer", cmd_add_player))
    app.add_handler(CommandHandler("add_player_list", cmd_add_player_list))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("removeadmin", cmd_remove_admin))
    app.add_handler(CommandHandler("startauction", cmd_start_auction))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("sold", cmd_sold))
    app.add_handler(CommandHandler("forcesold", cmd_force_sold))
    app.add_handler(CommandHandler("endauction", cmd_end_auction))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("unsoldqueue", cmd_unsold_queue))
    app.add_handler(CommandHandler("clearplayers", cmd_clear_players))

    # Button callbacks
    app.add_handler(CallbackQueryHandler(handle_callback))

    return app


async def setup_webhook(app: Application, webhook_url: str):
    """Register the webhook URL with Telegram and initialize the app."""
    await app.initialize()
    await app.bot.set_webhook(
        url=f"{webhook_url}/webhook",
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )
    await app.start()
    logger.info(f"Webhook set to {webhook_url}/webhook")


def main():
    """Entry point — polling (local) or webhook (Render/Railway)."""
    if not Config.BOT_TOKEN or Config.BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise ValueError("❌ BOT_TOKEN is not set! Check your .env file.")

    if not Config.SUPER_ADMIN_ID:
        raise ValueError("❌ SUPER_ADMIN_ID is not set! Check your .env file.")

    logger.info("Starting IPL Auction Bot...")

    ptb_app = build_application()

    if Config.WEBHOOK_URL:
        # ── PRODUCTION: webhook mode, single port ──────────────────
        # Set up the event loop and register webhook with Telegram
        global _ptb_app
        _ptb_app = ptb_app

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Store loop reference so webhook route can schedule coroutines
        flask_app._ptb_loop = loop

        # Monkey-patch the webhook route to use this loop
        @flask_app.route("/webhook", methods=["POST"], endpoint="webhook_v2")
        def webhook_handler():
            from telegram import Update as _Update
            data = request.get_json(force=True)
            update = _Update.de_json(data, _ptb_app.bot)
            asyncio.run_coroutine_threadsafe(
                _ptb_app.process_update(update), loop
            )
            return "ok", 200

        # Initialize PTB (register webhook) in the event loop
        loop.run_until_complete(setup_webhook(ptb_app, Config.WEBHOOK_URL))

        # Run Flask (which receives Telegram POSTs) in a background thread
        import threading
        def run_flask():
            flask_app.run(host="0.0.0.0", port=Config.PORT, use_reloader=False)

        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()

        logger.info(f"Running in WEBHOOK mode on port {Config.PORT}")

        # Keep the event loop running (for timers/async tasks)
        loop.run_forever()

    else:
        # ── DEVELOPMENT: polling mode ──────────────────────────────
        logger.info("Running in POLLING mode (development)...")
        ptb_app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
