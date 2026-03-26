"""
IPL Cricket Auction Telegram Bot — v2.0
Full-featured with RTM buttons, queue management, custom purse/squad,
dot-commands, auto-sell, auto-next, pause/resume, and more.
"""

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import threading
from dataclasses import dataclass, field
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
class Config:
    BOT_TOKEN: str      = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
    SUPER_ADMIN_ID: int = int(os.getenv("SUPER_ADMIN_ID", "0"))
    WEBHOOK_URL: str    = os.getenv("WEBHOOK_URL", "")
    PORT: int           = int(os.getenv("PORT", "8080"))
    DATABASE_PATH: str  = os.getenv("DATABASE_PATH", "auction.db")

    # Defaults (overridable via DB settings)
    DEFAULT_PURSE: int        = 12500   # Lakhs = 125 Crore
    DEFAULT_SQUAD_LIMIT: int  = 25
    DEFAULT_MAX_OVERSEAS: int = 8
    DEFAULT_CURRENCY: str     = "Rs."
    DEFAULT_INCREMENT: int    = 10      # Lakhs
    BID_TIMER: int            = 30
    RTM_TIMER: int            = 15
    ANTI_SNIPE_SECS: int      = 10


# ─────────────────────────────────────────────
# DATACLASSES
# ─────────────────────────────────────────────
@dataclass
class Player:
    player_id: int
    name: str
    base_price: int
    role: str
    nationality: str
    ipl_team: str
    tier: str
    status: str = "available"
    sold_to: Optional[int] = None
    sold_price: Optional[int] = None
    rtm_eligible: bool = True


@dataclass
class Team:
    team_id: int
    name: str
    purse: int
    total_spent: int = 0
    players: list = field(default_factory=list)
    is_admin: bool = False
    rtm_cards: int = 0


@dataclass
class AuctionState:
    active: bool              = False
    paused: bool              = False
    name: str                 = "IPL Auction 2026"
    current_player: Optional[Player]   = None
    current_bid: int          = 0
    highest_bidder_id: Optional[int]   = None
    highest_bidder_name: str  = ""
    last_message_id: Optional[int]     = None
    chat_id: Optional[int]             = None
    timer_task: Optional[asyncio.Task] = None
    timer_ends_at: Optional[float]     = None
    rtm_phase: bool           = False
    rtm_team_id: Optional[int]         = None
    rtm_message_id: Optional[int]      = None
    player_queue: list        = field(default_factory=list)
    set_number: int           = 1
    sold_count: int           = 0
    unsold_count: int         = 0
    auto_sell_secs: Optional[int]      = None
    auto_next_secs: Optional[int]      = None
    auto_next_enabled: bool   = False


auction = AuctionState()

flask_app = Flask(__name__)
_ptb_app: Optional[Application] = None


# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
class Database:
    _local = threading.local()

    def __init__(self, path: str = Config.DATABASE_PATH):
        self.path = path
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "c") or self._local.c is None:
            self._local.c = sqlite3.connect(self.path, check_same_thread=False)
            self._local.c.row_factory = sqlite3.Row
        return self._local.c

    @property
    def conn(self):
        return self._conn()

    def _init_db(self):
        c = sqlite3.connect(self.path)
        c.executescript("""
            CREATE TABLE IF NOT EXISTS teams (
                team_id     INTEGER PRIMARY KEY,
                name        TEXT NOT NULL,
                purse       INTEGER DEFAULT 12500,
                total_spent INTEGER DEFAULT 0,
                players     TEXT DEFAULT '[]',
                is_admin    INTEGER DEFAULT 0,
                rtm_cards   INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS players (
                player_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                base_price   INTEGER NOT NULL,
                role         TEXT NOT NULL,
                nationality  TEXT NOT NULL,
                ipl_team     TEXT DEFAULT '',
                tier         TEXT DEFAULT 'C',
                status       TEXT DEFAULT 'available',
                sold_to      INTEGER,
                sold_price   INTEGER,
                rtm_eligible INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS auction_history (
                history_id  INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id   INTEGER,
                team_id     INTEGER,
                final_price INTEGER,
                rtm_used    INTEGER DEFAULT 0,
                timestamp   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        c.commit()
        c.close()

    # ── SETTINGS ─────────────────────────────
    def get_setting(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value):
        self.conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value))
        )
        self.conn.commit()

    # ── TEAMS ────────────────────────────────
    def register_team(self, user_id: int, name: str) -> bool:
        purse = int(self.get_setting("purse", Config.DEFAULT_PURSE))
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO teams (team_id, name, purse) VALUES (?, ?, ?)",
                (user_id, name, purse),
            )
            self.conn.commit()
            return self.conn.execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(e)
            return False

    def get_team(self, user_id: int) -> Optional[Team]:
        r = self.conn.execute("SELECT * FROM teams WHERE team_id=?", (user_id,)).fetchone()
        return self._row_team(r) if r else None

    def get_all_teams(self) -> list:
        return [self._row_team(r) for r in self.conn.execute("SELECT * FROM teams").fetchall()]

    def _row_team(self, r) -> Team:
        return Team(
            team_id=r["team_id"], name=r["name"], purse=r["purse"],
            total_spent=r["total_spent"], players=json.loads(r["players"]),
            is_admin=bool(r["is_admin"]), rtm_cards=r["rtm_cards"],
        )

    def set_admin(self, user_id: int, is_admin: bool):
        self.conn.execute("UPDATE teams SET is_admin=? WHERE team_id=?",
                          (1 if is_admin else 0, user_id))
        self.conn.commit()

    def is_admin(self, user_id: int) -> bool:
        if user_id == Config.SUPER_ADMIN_ID:
            return True
        r = self.conn.execute("SELECT is_admin FROM teams WHERE team_id=?", (user_id,)).fetchone()
        return bool(r and r["is_admin"])

    def set_team_name(self, user_id: int, name: str):
        self.conn.execute("UPDATE teams SET name=? WHERE team_id=?", (name, user_id))
        self.conn.commit()

    def set_purse(self, user_id: int, amount: int):
        self.conn.execute("UPDATE teams SET purse=? WHERE team_id=?", (amount, user_id))
        self.conn.commit()

    def add_purse(self, user_id: int, amount: int):
        self.conn.execute("UPDATE teams SET purse=purse+? WHERE team_id=?", (amount, user_id))
        self.conn.commit()

    def deduct_purse(self, user_id: int, amount: int):
        self.conn.execute(
            "UPDATE teams SET purse=purse-?, total_spent=total_spent+? WHERE team_id=?",
            (amount, amount, user_id),
        )
        self.conn.commit()

    def add_player_to_team(self, user_id: int, player_id: int):
        team = self.get_team(user_id)
        if team:
            plist = team.players
            if player_id not in plist:
                plist.append(player_id)
            self.conn.execute("UPDATE teams SET players=? WHERE team_id=?",
                              (json.dumps(plist), user_id))
            self.conn.commit()

    def remove_player_from_team(self, user_id: int, positions: list):
        team = self.get_team(user_id)
        if not team:
            return
        plist = team.players
        indices = sorted([p - 1 for p in positions if 0 < p <= len(plist)], reverse=True)
        for idx in indices:
            pid = plist[idx]
            self.conn.execute(
                "UPDATE players SET status='available', sold_to=NULL, sold_price=NULL WHERE player_id=?",
                (pid,)
            )
            plist.pop(idx)
        self.conn.execute("UPDATE teams SET players=? WHERE team_id=?",
                          (json.dumps(plist), user_id))
        self.conn.commit()

    def clear_squad(self, user_id: int):
        team = self.get_team(user_id)
        if team:
            for pid in team.players:
                self.conn.execute(
                    "UPDATE players SET status='available', sold_to=NULL, sold_price=NULL WHERE player_id=?",
                    (pid,)
                )
            self.conn.execute(
                "UPDATE teams SET players='[]', total_spent=0 WHERE team_id=?", (user_id,)
            )
            self.conn.commit()

    def swap_teams(self, uid1: int, uid2: int) -> bool:
        t1 = self.get_team(uid1)
        t2 = self.get_team(uid2)
        if not t1 or not t2:
            return False
        self.conn.execute(
            "UPDATE teams SET purse=?, total_spent=?, players=? WHERE team_id=?",
            (t2.purse, t2.total_spent, json.dumps(t2.players), uid1),
        )
        self.conn.execute(
            "UPDATE teams SET purse=?, total_spent=?, players=? WHERE team_id=?",
            (t1.purse, t1.total_spent, json.dumps(t1.players), uid2),
        )
        self.conn.commit()
        return True

    # ── PLAYERS ──────────────────────────────
    def add_player(self, p: Player) -> int:
        cur = self.conn.execute(
            "INSERT INTO players (name,base_price,role,nationality,ipl_team,tier,rtm_eligible)"
            " VALUES (?,?,?,?,?,?,?)",
            (p.name, p.base_price, p.role, p.nationality, p.ipl_team, p.tier,
             1 if p.rtm_eligible else 0),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_player(self, pid: int) -> Optional[Player]:
        r = self.conn.execute("SELECT * FROM players WHERE player_id=?", (pid,)).fetchone()
        return self._row_player(r) if r else None

    def get_available_players(self) -> list:
        return [self._row_player(r) for r in
                self.conn.execute(
                    "SELECT * FROM players WHERE status='available' ORDER BY player_id"
                ).fetchall()]

    def get_unsold_players(self) -> list:
        return [self._row_player(r) for r in
                self.conn.execute("SELECT * FROM players WHERE status='unsold'").fetchall()]

    def update_player_status(self, pid: int, status: str, sold_to=None, sold_price=None):
        self.conn.execute(
            "UPDATE players SET status=?, sold_to=?, sold_price=? WHERE player_id=?",
            (status, sold_to, sold_price, pid),
        )
        self.conn.commit()

    def _row_player(self, r) -> Player:
        return Player(
            player_id=r["player_id"], name=r["name"], base_price=r["base_price"],
            role=r["role"], nationality=r["nationality"], ipl_team=r["ipl_team"] or "",
            tier=r["tier"], status=r["status"], sold_to=r["sold_to"],
            sold_price=r["sold_price"], rtm_eligible=bool(r["rtm_eligible"]),
        )

    def record_history(self, pid: int, tid: int, price: int, rtm_used: bool = False):
        self.conn.execute(
            "INSERT INTO auction_history (player_id,team_id,final_price,rtm_used)"
            " VALUES (?,?,?,?)",
            (pid, tid, price, 1 if rtm_used else 0),
        )
        self.conn.commit()

    def clear_players(self):
        self.conn.execute("DELETE FROM players")
        self.conn.commit()


db = Database()


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def cur_symbol() -> str:
    return db.get_setting("currency", Config.DEFAULT_CURRENCY)


def fmt(lakhs: int) -> str:
    s = cur_symbol()
    if lakhs >= 100:
        c = lakhs / 100
        return f"{s}{c:.1f}Cr" if c % 1 else f"{s}{int(c)}Cr"
    return f"{s}{lakhs}L"


def parse_price(s: str) -> Optional[int]:
    s = s.strip().lower().replace(" ", "")
    try:
        if s.endswith("cr"):
            return int(float(s[:-2]) * 100)
        if s.endswith("l"):
            return int(float(s[:-1]))
        return int(s)
    except ValueError:
        return None


def flag(nat: str) -> str:
    return "🇮🇳" if nat.lower() == "indian" else "🌍"


def role_emoji(r: str) -> str:
    return {
        "batsman": "🏏", "bat": "🏏",
        "bowler": "⚡", "bowl": "⚡",
        "all-rounder": "🌟", "allrounder": "🌟", "ar": "🌟",
        "wicketkeeper": "🧤", "wk": "🧤",
    }.get(r.lower(), "🏏")


def tier_str(t: str) -> str:
    return {
        "Marquee": "⭐ Marquee", "A": "🔷 Grade A", "B": "🔹 Grade B",
        "C": "▪️ Grade C", "Uncapped": "🔸 Uncapped",
    }.get(t, t)


def normalize_role(r: str) -> str:
    r = r.lower().strip()
    if r in ("bat", "batsman", "batter"):         return "Batsman"
    if r in ("bowl", "bowler"):                    return "Bowler"
    if r in ("ar", "allrounder", "all-rounder"):   return "All-rounder"
    if r in ("wk", "wicketkeeper", "keeper"):      return "Wicketkeeper"
    return r.title()


def normalize_nat(n: str) -> str:
    return "Indian" if n.lower() in ("indian", "india", "ind", "domestic") else "Overseas"


def squad_stats(team: Team):
    sq = [db.get_player(pid) for pid in team.players]
    sq = [p for p in sq if p]
    overseas = [p for p in sq if p.nationality == "Overseas"]
    return sq, overseas


def find_team_by_ipl_name(name: str) -> Optional[Team]:
    name_l = name.lower()
    for t in db.get_all_teams():
        if name_l in t.name.lower() or t.name.lower() in name_l:
            return t
    return None


def validate_bid(team: Team, player: Player, bid_l: int) -> Optional[str]:
    if team.purse < bid_l:
        return f"Not enough purse! You have {fmt(team.purse)} left."
    limit = int(db.get_setting("squad_limit", Config.DEFAULT_SQUAD_LIMIT))
    sq, overseas = squad_stats(team)
    if len(sq) >= limit:
        return f"Squad full! (max {limit})"
    max_ov = int(db.get_setting("max_overseas", Config.DEFAULT_MAX_OVERSEAS))
    if player.nationality == "Overseas" and len(overseas) >= max_ov:
        return f"Overseas limit reached! (max {max_ov})"
    if bid_l < player.base_price:
        return f"Minimum bid is {fmt(player.base_price)}."
    if auction.current_bid > 0 and bid_l <= auction.current_bid:
        return f"Bid must be above current {fmt(auction.current_bid)}."
    return None


# ─────────────────────────────────────────────
# MESSAGE BUILDERS
# ─────────────────────────────────────────────
def player_card(player: Player) -> str:
    rtm_note = f"\n🎴 *RTM:* {player.ipl_team} can match!" if (player.ipl_team and player.rtm_eligible) else ""
    return (
        f"{'='*26}\n"
        f"{role_emoji(player.role)} *{flag(player.nationality)} {player.name}*\n"
        f"Role: {player.role}  |  {player.nationality}\n"
        f"Tier: {tier_str(player.tier)}\n"
        f"Base: *{fmt(player.base_price)}*\n"
        f"Prev Team: *{player.ipl_team or 'None'}*"
        f"{rtm_note}\n"
    )


def auction_status_message(player: Player, bid: int, bidder: str,
                            time_left: Optional[int] = None) -> str:
    timer = f"Timer: {time_left}s remaining" if time_left is not None else "Timer starts on first bid"
    bid_line = (
        f"Current Bid: *{fmt(bid)}*\nHighest Bidder: *{bidder}*"
        if bid > 0
        else f"Base Price: *{fmt(player.base_price)}*\nNo bids yet"
    )
    return (
        f"AUCTION: *{auction.name}* — Set {auction.set_number}\n"
        f"{player_card(player)}\n"
        f"{bid_line}\n"
        f"{timer}"
    )


def build_bid_keyboard(player: Player, current_bid: int) -> InlineKeyboardMarkup:
    if current_bid == 0:
        b1 = player.base_price
    else:
        b1 = current_bid + Config.DEFAULT_INCREMENT
    b2 = b1 + Config.DEFAULT_INCREMENT

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"Bid {fmt(b1)}", callback_data=f"bid_{b1}"),
            InlineKeyboardButton(f"Bid {fmt(b2)}", callback_data=f"bid_{b2}"),
        ],
        [InlineKeyboardButton("My Purse", callback_data="my_purse")],
    ])


def rtm_prompt(player: Player, bid: int, bidder: str, rtm_team_name: str) -> str:
    return (
        f"RTM OPPORTUNITY!\n{'='*26}\n"
        f"*{player.name}* - {fmt(bid)} by *{bidder}*\n\n"
        f"*{rtm_team_name}* — you have *{Config.RTM_TIMER}s* to use your RTM!\n\n"
        f"Accept RTM: You match {fmt(bid)} and keep the player.\n"
        f"Decline RTM: Player goes to {bidder}."
    )


def rtm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Accept RTM", callback_data="rtm_accept"),
        InlineKeyboardButton("Decline RTM", callback_data="rtm_decline"),
    ]])


# ─────────────────────────────────────────────
# TIMER LOGIC
# ─────────────────────────────────────────────
import time as _time


async def bid_timer(context: ContextTypes.DEFAULT_TYPE):
    """Countdown. Updates message every 5s, fires expiry logic at end."""
    duration = auction.auto_sell_secs or Config.BID_TIMER
    end      = _time.time() + duration
    auction.timer_ends_at = end

    while True:
        await asyncio.sleep(5)
        if not auction.active or auction.paused or not auction.current_player:
            return
        remaining = max(0, int(auction.timer_ends_at - _time.time()))
        if remaining <= 0:
            break
        # Update timer in last message
        if auction.last_message_id:
            try:
                p = auction.current_player
                await context.bot.edit_message_text(
                    chat_id=auction.chat_id,
                    message_id=auction.last_message_id,
                    text=auction_status_message(p, auction.current_bid,
                                                auction.highest_bidder_name, remaining),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=build_bid_keyboard(p, auction.current_bid),
                )
            except Exception:
                pass

    if not auction.active or not auction.current_player:
        return

    if auction.current_bid == 0:
        await _handle_no_bids(context)
    else:
        await _check_rtm(context)


async def _handle_no_bids(context: ContextTypes.DEFAULT_TYPE):
    player = auction.current_player
    db.update_player_status(player.player_id, "unsold")
    auction.unsold_count += 1
    auction.current_player = None
    await context.bot.send_message(
        chat_id=auction.chat_id,
        text=f"UNSOLD: *{player.name}* — No bids received.",
        parse_mode=ParseMode.MARKDOWN,
    )
    await _try_auto_next(context)


async def _check_rtm(context: ContextTypes.DEFAULT_TYPE):
    player = auction.current_player
    if player.ipl_team and player.rtm_eligible:
        rtm_team = find_team_by_ipl_name(player.ipl_team)
        if rtm_team and rtm_team.team_id != auction.highest_bidder_id:
            err = validate_bid(rtm_team, player, auction.current_bid)
            if not err:
                auction.rtm_phase   = True
                auction.rtm_team_id = rtm_team.team_id
                msg = await context.bot.send_message(
                    chat_id=auction.chat_id,
                    text=rtm_prompt(player, auction.current_bid,
                                    auction.highest_bidder_name, rtm_team.name),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=rtm_keyboard(),
                )
                auction.rtm_message_id = msg.message_id
                auction.timer_task = asyncio.create_task(_rtm_timer(context))
                return
    await _finalize_sale(context)


async def _rtm_timer(context: ContextTypes.DEFAULT_TYPE):
    end = _time.time() + Config.RTM_TIMER
    while _time.time() < end:
        await asyncio.sleep(1)
        if not auction.rtm_phase:
            return
    if auction.rtm_phase:
        auction.rtm_phase   = False
        auction.rtm_team_id = None
        await context.bot.send_message(
            chat_id=auction.chat_id,
            text="RTM window expired. Player goes to highest bidder.",
            parse_mode=ParseMode.MARKDOWN,
        )
        await _finalize_sale(context)


async def _finalize_sale(context: ContextTypes.DEFAULT_TYPE, rtm_used: bool = False):
    player = auction.current_player
    if not player or not auction.highest_bidder_id:
        return

    winner_id    = auction.highest_bidder_id
    winner_name  = auction.highest_bidder_name
    final_price  = auction.current_bid

    db.update_player_status(player.player_id, "sold", winner_id, final_price)
    db.deduct_purse(winner_id, final_price)
    db.add_player_to_team(winner_id, player.player_id)
    db.record_history(player.player_id, winner_id, final_price, rtm_used)

    auction.sold_count        += 1
    winner_team                = db.get_team(winner_id)
    auction.current_player     = None
    auction.current_bid        = 0
    auction.highest_bidder_id  = None
    auction.highest_bidder_name= ""
    auction.rtm_phase          = False
    auction.rtm_team_id        = None

    await context.bot.send_message(
        chat_id=auction.chat_id,
        text=(
            f"SOLD!\n{'='*26}\n"
            f"*{player.name}* goes to *{winner_name}*\n"
            f"Final Price: *{fmt(final_price)}*\n"
            f"{'RTM Used!' if rtm_used else ''}\n\n"
            f"Remaining purse for {winner_name}: *{fmt(winner_team.purse if winner_team else 0)}*"
        ),
        parse_mode=ParseMode.MARKDOWN,
    )
    await _try_auto_next(context)


async def _try_auto_next(context: ContextTypes.DEFAULT_TYPE):
    if auction.auto_next_enabled and auction.auto_next_secs and auction.active:
        await asyncio.sleep(auction.auto_next_secs)
        if not auction.current_player and not auction.paused:
            await _do_next(context, auction.chat_id)


# ─────────────────────────────────────────────
# CORE NEXT PLAYER
# ─────────────────────────────────────────────
async def _do_next(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    if not auction.active:
        return
    if auction.current_player:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Current player still active! Use /sold or /forcesold first.",
        )
        return
    if not auction.player_queue:
        unsold = db.get_unsold_players()
        if unsold:
            auction.player_queue = unsold
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Loading {len(unsold)} unsold players back into queue...",
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text="All players auctioned! Use /endauction for summary.",
            )
            return

    player = auction.player_queue.pop(0)
    fresh  = db.get_player(player.player_id)
    if not fresh or fresh.status != "available":
        await _do_next(context, chat_id)
        return

    auction.current_player     = fresh
    auction.current_bid        = 0
    auction.highest_bidder_id  = None
    auction.highest_bidder_name= ""
    auction.timer_task         = None
    auction.timer_ends_at      = None

    remaining = len(auction.player_queue)
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"AUCTION: *{auction.name}* — Set {auction.set_number}\n"
            f"{player_card(fresh)}\n"
            f"Timer starts on first bid\n"
            f"Players remaining: {remaining}"
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_bid_keyboard(fresh, 0),
    )
    auction.last_message_id = msg.message_id
    auction.chat_id         = chat_id


# ─────────────────────────────────────────────
# BID PROCESSING
# ─────────────────────────────────────────────
async def process_bid(update, context: ContextTypes.DEFAULT_TYPE, user_id: int, bid_l: int):
    team = db.get_team(user_id)
    if not team:
        msg = "Register first with /start."
        if update.callback_query: await update.callback_query.answer(msg, show_alert=True)
        else: await update.message.reply_text(msg)
        return

    if not auction.active or not auction.current_player:
        msg = "No active auction right now."
        if update.callback_query: await update.callback_query.answer(msg, show_alert=True)
        else: await update.message.reply_text(msg)
        return

    if auction.paused:
        msg = "Auction is paused."
        if update.callback_query: await update.callback_query.answer(msg, show_alert=True)
        else: await update.message.reply_text(msg)
        return

    # Block the current highest bidder from bidding again (unless RTM phase)
    if auction.highest_bidder_id == user_id and not auction.rtm_phase:
        msg = f"You are already the highest bidder at {fmt(auction.current_bid)}!"
        if update.callback_query: await update.callback_query.answer(msg, show_alert=True)
        else: await update.message.reply_text(msg)
        return

    err = validate_bid(team, auction.current_player, bid_l)
    if err:
        if update.callback_query: await update.callback_query.answer(err, show_alert=True)
        else: await update.message.reply_text(f"Bid failed: {err}")
        return

    # Anti-snipe: extend timer if bid in last N seconds
    if auction.timer_ends_at:
        remaining = auction.timer_ends_at - _time.time()
        if 0 < remaining < Config.ANTI_SNIPE_SECS:
            auction.timer_ends_at = _time.time() + Config.ANTI_SNIPE_SECS

    prev_name = auction.highest_bidder_name

    auction.current_bid        = bid_l
    auction.highest_bidder_id  = user_id
    auction.highest_bidder_name= team.name

    # Start timer on first bid
    if auction.timer_task is None or auction.timer_task.done():
        auction.timer_task = asyncio.create_task(bid_timer(context))

    player = auction.current_player
    duration = auction.auto_sell_secs or Config.BID_TIMER

    # Send a NEW message for every bid (not edit)
    outbid_note = f"Outbids: {prev_name}" if prev_name and prev_name != team.name else "Opening bid!"
    msg = await context.bot.send_message(
        chat_id=auction.chat_id,
        text=(
            f"NEW BID\n{'='*26}\n"
            f"Player: *{player.name}*\n"
            f"Amount: *{fmt(bid_l)}*\n"
            f"Team: *{team.name}*\n"
            f"{outbid_note}\n"
            f"Timer: {duration}s"
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_bid_keyboard(player, bid_l),
    )
    auction.last_message_id = msg.message_id

    if update.callback_query:
        await update.callback_query.answer(f"Bid of {fmt(bid_l)} placed!")


# ─────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    existing = db.get_team(user.id)
    if existing:
        await update.message.reply_text(
            f"Welcome back, *{existing.name}*!\n"
            f"Purse: {fmt(existing.purse)} | Squad: {len(existing.players)} players",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    db.register_team(user.id, user.first_name)
    if user.id == Config.SUPER_ADMIN_ID:
        db.set_admin(user.id, True)

    purse = int(db.get_setting("purse", Config.DEFAULT_PURSE))
    limit = int(db.get_setting("squad_limit", Config.DEFAULT_SQUAD_LIMIT))

    await update.message.reply_text(
        f"Welcome to *{auction.name}*!\n{'='*26}\n"
        f"Team: *{user.first_name}*\n"
        f"Purse: *{fmt(purse)}*\n"
        f"Max Squad: {limit} players\n\n"
        f"Use /setteamname to rename your team!\n"
        f"Use /help for all commands.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Join Auction", callback_data="join_auction")
        ]]),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    is_adm = db.is_admin(uid)

    text = (
        "USER COMMANDS\n"
        "/start — Register\n"
        "/setteamname <n> — Set team name\n"
        "/purse — Check purse\n"
        "/squad — View squad\n"
        "/bid <amount> — Bid (e.g. /bid 2cr)\n"
        "/leaderboard — Top teams\n"
    )
    if is_adm:
        text += (
            "\nADMIN: AUCTION\n"
            "/setauctionname <n> — Name the auction\n"
            "/startauction — Start\n"
            "/next — Next player\n"
            "/sold — Confirm sale\n"
            "/forcesold — Force sell\n"
            "/pauseauction | /resumeauction\n"
            "/endauction — End\n"
            "/autosell <secs|off>\n"
            "/autonext <enable|disable|secs>\n"
            "\nADMIN: QUEUE (.atq .rfq .sq .q)\n"
            "/addtoqueue p1,p2\n"
            "/addtoqueueunsolds\n"
            "/removefromqueue 1,2\n"
            "/shufflequeue\n"
            "/swapqueue 1 2\n"
            "/clearqueue\n"
            "/queue [page]\n"
            "\nADMIN: PURSE & SQUAD\n"
            "/setpurse @user <amt>\n"
            "/addpurse @user <amt>\n"
            "/deductpurse @user <amt>\n"
            "/setsquadlimit <n>\n"
            "/setcurrency <symbol>\n"
            "/addtosquad @user p1,p2\n"
            "/removefromsquad @user 1,2\n"
            "/clearsquad @user\n"
            "/swap @u1 @u2\n"
            "/resetname @user\n"
            "\nAll / commands also work with . prefix!"
        )
    await update.message.reply_text(text)


async def cmd_set_team_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not db.get_team(user.id):
        await update.message.reply_text("Register first with /start.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /setteamname <Your Team Name>")
        return
    name = " ".join(context.args)
    db.set_team_name(user.id, name)
    await update.message.reply_text(f"Team name set to: *{name}*", parse_mode=ParseMode.MARKDOWN)


async def cmd_reset_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    target = _resolve_user(update, context)
    if not target:
        await update.message.reply_text("Usage: /resetname <user_id>")
        return
    try:
        tg_user = await context.bot.get_chat(target)
        db.set_team_name(target, tg_user.first_name)
        await update.message.reply_text(f"Name reset to: *{tg_user.first_name}*",
                                        parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await update.message.reply_text("Could not fetch user.")


async def cmd_purse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if context.args and db.is_admin(uid):
        target = _resolve_user(update, context)
        team = db.get_team(target) if target else None
    else:
        team = db.get_team(uid)
    if not team:
        await update.message.reply_text("Not registered! Use /start.")
        return
    sq, overseas = squad_stats(team)
    limit  = int(db.get_setting("squad_limit", Config.DEFAULT_SQUAD_LIMIT))
    max_ov = int(db.get_setting("max_overseas", Config.DEFAULT_MAX_OVERSEAS))
    roles  = {}
    for p in sq:
        roles[p.role] = roles.get(p.role, 0) + 1
    lines = [
        f"PURSE: *{team.name}*\n{'='*26}\n"
        f"Remaining: *{fmt(team.purse)}*\n"
        f"Spent: {fmt(team.total_spent)}\n"
        f"Squad: {len(sq)}/{limit}\n"
        f"Indian: {len(sq)-len(overseas)}  |  Overseas: {len(overseas)}/{max_ov}\n"
        f"RTM Cards: {team.rtm_cards}\n\nBy Role:"
    ]
    for r, c in roles.items():
        lines.append(f"  {role_emoji(r)} {r}: {c}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_squad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if context.args and db.is_admin(uid):
        target = _resolve_user(update, context)
        team = db.get_team(target) if target else None
    else:
        team = db.get_team(uid)
    if not team:
        await update.message.reply_text("Not registered!")
        return
    sq, _ = squad_stats(team)
    if not sq:
        await update.message.reply_text(f"*{team.name}* squad is empty.",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    by_role: dict = {}
    for p in sq:
        by_role.setdefault(p.role, []).append(p)
    lines = [f"SQUAD: *{team.name}* ({len(sq)} players)\n{'='*26}"]
    for role, players in by_role.items():
        lines.append(f"\n{role_emoji(role)} *{role}s*")
        for i, p in enumerate(players, 1):
            lines.append(
                f"  {i}. {flag(p.nationality)} {p.name} — {fmt(p.sold_price or p.base_price)}"
            )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_set_auction_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /setauctionname <name>")
        return
    auction.name = " ".join(context.args)
    await update.message.reply_text(f"Auction name set to: *{auction.name}*",
                                    parse_mode=ParseMode.MARKDOWN)


# ── PURSE MANAGEMENT ─────────────────────────

async def cmd_set_purse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    target, amount_str = _parse_user_amount(update, context)
    if not target or not amount_str:
        await update.message.reply_text("Usage: /setpurse <user_id> <amount>")
        return
    amount = parse_price(amount_str)
    if not amount:
        await update.message.reply_text("Invalid amount.")
        return
    db.set_purse(target, amount)
    t = db.get_team(target)
    await update.message.reply_text(f"Set *{t.name}* purse to *{fmt(amount)}*",
                                    parse_mode=ParseMode.MARKDOWN)


async def cmd_add_purse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    target, amount_str = _parse_user_amount(update, context)
    if not target or not amount_str:
        await update.message.reply_text("Usage: /addpurse <user_id> <amount>")
        return
    amount = parse_price(amount_str)
    if not amount:
        await update.message.reply_text("Invalid amount.")
        return
    db.add_purse(target, amount)
    t = db.get_team(target)
    await update.message.reply_text(f"Added *{fmt(amount)}* to *{t.name}*. Purse: *{fmt(t.purse)}*",
                                    parse_mode=ParseMode.MARKDOWN)


async def cmd_deduct_purse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    target, amount_str = _parse_user_amount(update, context)
    if not target or not amount_str:
        await update.message.reply_text("Usage: /deductpurse <user_id> <amount>")
        return
    amount = parse_price(amount_str)
    if not amount:
        await update.message.reply_text("Invalid amount.")
        return
    db.deduct_purse(target, amount)
    t = db.get_team(target)
    await update.message.reply_text(
        f"Deducted *{fmt(amount)}* from *{t.name}*. Left: *{fmt(t.purse)}*",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_set_squad_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /setsquadlimit <number>")
        return
    try:
        db.set_setting("squad_limit", int(context.args[0]))
        await update.message.reply_text(f"Squad limit set to *{context.args[0]}*.",
                                        parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("Invalid number.")


async def cmd_set_currency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /setcurrency <symbol>  e.g. $ Rs. EUR")
        return
    db.set_setting("currency", context.args[0])
    await update.message.reply_text(f"Currency set to: *{context.args[0]}*",
                                    parse_mode=ParseMode.MARKDOWN)


# ── SQUAD MANAGEMENT ─────────────────────────

async def cmd_add_to_squad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addtosquad <user_id> Player1,Player2")
        return
    target = _resolve_user(update, context)
    if not target:
        await update.message.reply_text("User not found.")
        return
    names_str = " ".join(context.args[1:])
    names = [n.strip() for n in names_str.split(",") if n.strip()]
    for name in names:
        pid = db.add_player(Player(0, name, 0, "Batsman", "Indian", "", "C"))
        db.update_player_status(pid, "sold", target, 0)
        db.add_player_to_team(target, pid)
    t = db.get_team(target)
    await update.message.reply_text(f"Added {len(names)} player(s) to *{t.name}* squad.",
                                    parse_mode=ParseMode.MARKDOWN)


async def cmd_remove_from_squad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /removefromsquad <user_id> 1,2")
        return
    target = _resolve_user(update, context)
    if not target:
        await update.message.reply_text("User not found.")
        return
    try:
        positions = [int(p.strip()) for p in " ".join(context.args[1:]).split(",")]
    except ValueError:
        await update.message.reply_text("Invalid positions.")
        return
    db.remove_player_from_team(target, positions)
    t = db.get_team(target)
    await update.message.reply_text(f"Removed positions {positions} from *{t.name}* squad.",
                                    parse_mode=ParseMode.MARKDOWN)


async def cmd_clear_squad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    target = _resolve_user(update, context) or update.effective_user.id
    db.clear_squad(target)
    t = db.get_team(target)
    await update.message.reply_text(f"Cleared *{t.name}* squad.", parse_mode=ParseMode.MARKDOWN)


async def cmd_swap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /swap <user_id_1> <user_id_2>")
        return
    ids = []
    for arg in context.args[:2]:
        uid = _extract_uid(arg)
        if uid:
            ids.append(uid)
    if len(ids) < 2:
        await update.message.reply_text("Could not resolve both user IDs.")
        return
    t1, t2 = db.get_team(ids[0]), db.get_team(ids[1])
    if not t1 or not t2:
        await update.message.reply_text("One or both users not registered.")
        return
    db.swap_teams(ids[0], ids[1])
    await update.message.reply_text(
        f"Swapped purse and squad between *{t1.name}* and *{t2.name}*.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── QUEUE MANAGEMENT ─────────────────────────

async def cmd_add_to_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /addtoqueue PlayerName1,PlayerName2")
        return
    names = [n.strip() for n in " ".join(context.args).split(",") if n.strip()]
    for name in names:
        pid = db.add_player(Player(0, name, 20, "Batsman", "Indian", "", "C"))
        p = db.get_player(pid)
        if p:
            auction.player_queue.append(p)
    await update.message.reply_text(
        f"Added {len(names)} player(s) to queue. Total: {len(auction.player_queue)}"
    )


async def cmd_add_unsolds_to_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    unsold = db.get_unsold_players()
    auction.player_queue.extend(unsold)
    await update.message.reply_text(
        f"Added {len(unsold)} unsold players to queue. Total: {len(auction.player_queue)}"
    )


async def cmd_remove_from_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /removefromqueue 1,3,5")
        return
    try:
        positions = sorted(
            [int(p.strip()) for p in " ".join(context.args).split(",")], reverse=True
        )
    except ValueError:
        await update.message.reply_text("Invalid positions.")
        return
    removed = []
    for pos in positions:
        idx = pos - 1
        if 0 <= idx < len(auction.player_queue):
            removed.append(auction.player_queue.pop(idx).name)
    await update.message.reply_text(
        f"Removed: {', '.join(removed)}\nQueue size: {len(auction.player_queue)}"
    )


async def cmd_shuffle_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    random.shuffle(auction.player_queue)
    await update.message.reply_text(f"Queue shuffled! {len(auction.player_queue)} players.")


async def cmd_swap_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /swapqueue <pos1> <pos2>")
        return
    try:
        p1, p2 = int(context.args[0]) - 1, int(context.args[1]) - 1
    except ValueError:
        await update.message.reply_text("Invalid positions.")
        return
    q = auction.player_queue
    if not (0 <= p1 < len(q) and 0 <= p2 < len(q)):
        await update.message.reply_text("Positions out of range.")
        return
    q[p1], q[p2] = q[p2], q[p1]
    await update.message.reply_text(f"Swapped positions {p1+1} and {p2+1}.")


async def cmd_clear_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    auction.player_queue.clear()
    await update.message.reply_text("Queue cleared.")


async def cmd_view_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not auction.player_queue:
        await update.message.reply_text("Queue is empty.")
        return
    page = int(context.args[0]) if context.args else 1
    per  = 15
    start= (page - 1) * per
    chunk= auction.player_queue[start:start + per]
    total_pages = (len(auction.player_queue) + per - 1) // per
    lines = [f"QUEUE (Page {page}/{total_pages}, {len(auction.player_queue)} total)"]
    for i, p in enumerate(chunk, start + 1):
        lines.append(f"{i}. {flag(p.nationality)} {p.name} — {role_emoji(p.role)} | {fmt(p.base_price)}")
    if page < total_pages:
        lines.append(f"\nUse /queue {page+1} for next page.")
    await update.message.reply_text("\n".join(lines))


# ── AUCTION CONTROL ──────────────────────────

async def cmd_start_auction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if auction.active:
        await update.message.reply_text("Auction already running!")
        return
    players = db.get_available_players()
    if not players:
        await update.message.reply_text("No players added yet.")
        return
    if not auction.player_queue:
        auction.player_queue = players
    auction.active       = True
    auction.paused       = False
    auction.sold_count   = 0
    auction.unsold_count = 0
    auction.set_number   = 1
    auction.chat_id      = update.effective_chat.id

    purse = int(db.get_setting("purse", Config.DEFAULT_PURSE))
    limit = int(db.get_setting("squad_limit", Config.DEFAULT_SQUAD_LIMIT))

    await update.message.reply_text(
        f"*{auction.name}* — STARTED!\n{'='*26}\n"
        f"{len(auction.player_queue)} players in queue\n"
        f"Purse per team: {fmt(purse)}\n"
        f"Max squad: {limit}\n\n"
        f"Admin: use /next to bring up the first player!",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not auction.active:
        await update.message.reply_text("No active auction.")
        return
    await _do_next(context, update.effective_chat.id)


async def cmd_sold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not auction.current_player:
        await update.message.reply_text("No player up for auction.")
        return
    if not auction.highest_bidder_id:
        await update.message.reply_text("No bids yet. Use /next to skip.")
        return
    if auction.timer_task and not auction.timer_task.done():
        auction.timer_task.cancel()
    await _finalize_sale(context)


async def cmd_force_sold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not auction.current_player or not auction.highest_bidder_id:
        await update.message.reply_text("No active bid.")
        return
    if auction.timer_task and not auction.timer_task.done():
        auction.timer_task.cancel()
    auction.rtm_phase   = False
    auction.rtm_team_id = None
    await _finalize_sale(context)


async def cmd_pause_auction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    auction.paused = True
    await update.message.reply_text("Auction PAUSED. Use /resumeauction to continue.")


async def cmd_resume_auction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    auction.paused = False
    await update.message.reply_text("Auction RESUMED!")


async def cmd_end_auction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if auction.timer_task and not auction.timer_task.done():
        auction.timer_task.cancel()
    auction.active         = False
    auction.current_player = None

    teams = sorted(db.get_all_teams(), key=lambda t: t.total_spent, reverse=True)
    medals = ["1st", "2nd", "3rd"]
    lines  = [
        f"*{auction.name}* — FINAL SUMMARY\n{'='*26}\n"
        f"Sold: {auction.sold_count}  |  Unsold: {auction.unsold_count}\n"
    ]
    for i, team in enumerate(teams, 1):
        sq, ov = squad_stats(team)
        m = medals[i-1] if i <= 3 else f"#{i}"
        lines.append(
            f"{m} *{team.name}*\n"
            f"  Spent: {fmt(team.total_spent)}  |  Left: {fmt(team.purse)}\n"
            f"  Squad: {len(sq)} players  |  Overseas: {len(ov)}"
        )
    await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_auto_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /autosell <seconds|off>")
        return
    val = context.args[0].lower()
    if val == "off":
        auction.auto_sell_secs = None
        await update.message.reply_text("Auto-sell timer disabled (using default 30s).")
    else:
        try:
            auction.auto_sell_secs = int(val)
            await update.message.reply_text(f"Auto-sell timer set to *{val}s*.",
                                            parse_mode=ParseMode.MARKDOWN)
        except ValueError:
            await update.message.reply_text("Invalid. Use a number or 'off'.")


async def cmd_auto_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /autonext <enable|disable|seconds>")
        return
    val = context.args[0].lower()
    if val == "enable":
        auction.auto_next_enabled = True
        auction.auto_next_secs    = auction.auto_next_secs or 5
        await update.message.reply_text(f"Auto-next enabled ({auction.auto_next_secs}s delay).")
    elif val == "disable":
        auction.auto_next_enabled = False
        await update.message.reply_text("Auto-next disabled.")
    else:
        try:
            auction.auto_next_enabled = True
            auction.auto_next_secs    = int(val)
            await update.message.reply_text(f"Auto-next enabled with *{val}s* delay.",
                                            parse_mode=ParseMode.MARKDOWN)
        except ValueError:
            await update.message.reply_text("Invalid.")


# ── PLAYER MANAGEMENT ────────────────────────

async def cmd_add_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if len(context.args) < 5:
        await update.message.reply_text(
            "Usage: /addplayer <Name> <Role> <PrevTeam> <Nationality> <BasePrice> [Tier]\n"
            "Example: /addplayer ViratKohli Bat RCB Indian 2cr Marquee"
        )
        return
    name  = context.args[0].replace("_", " ")
    role  = normalize_role(context.args[1])
    team  = context.args[2]
    nat   = normalize_nat(context.args[3])
    price = parse_price(context.args[4])
    tier  = context.args[5].capitalize() if len(context.args) > 5 else "C"
    if not price:
        await update.message.reply_text("Invalid price.")
        return
    pid = db.add_player(Player(0, name, price, role, nat, team, tier))
    if auction.active:
        p = db.get_player(pid)
        if p:
            auction.player_queue.append(p)
        note = " — Added to queue!"
    else:
        note = ""
    await update.message.reply_text(
        f"Added: *{flag(nat)} {name}* | {role} | {fmt(price)} | {tier}{note}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_add_player_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    lines = update.message.text.strip().split("\n")[1:]
    added, failed = [], []
    for line in lines:
        line = re.sub(r"^\d+[\.\)]\s*", "", line.strip())
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            failed.append(line[:40])
            continue
        price = parse_price(parts[4])
        if not price:
            failed.append(line[:40])
            continue
        tier = parts[5].capitalize() if len(parts) > 5 else "C"
        p = Player(0, parts[0], price, normalize_role(parts[1]),
                   normalize_nat(parts[3]), parts[2], tier)
        pid = db.add_player(p)
        p.player_id = pid
        if auction.active:
            auction.player_queue.append(p)
        added.append(parts[0])

    msg = f"Added {len(added)} players!"
    if auction.active and added:
        msg += f" ({len(added)} added to queue)"
    if added:
        msg += "\n" + "\n".join(f"  {n}" for n in added[:20])
        if len(added) > 20:
            msg += f"\n  ...and {len(added)-20} more"
    if failed:
        msg += f"\n\nFailed ({len(failed)}):\n" + "\n".join(failed[:5])
    await update.message.reply_text(msg)


async def cmd_bid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /bid <amount>  e.g. /bid 2cr")
        return
    bid_l = parse_price(context.args[0])
    if bid_l is None:
        await update.message.reply_text("Invalid amount.")
        return
    await process_bid(update, context, update.effective_user.id, bid_l)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != Config.SUPER_ADMIN_ID:
        await update.message.reply_text("Super Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /admin <user_id>")
        return
    try:
        tid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid ID.")
        return
    t = db.get_team(tid)
    if not t:
        await update.message.reply_text(f"User {tid} not found. They must /start first.")
        return
    db.set_admin(tid, True)
    await update.message.reply_text(f"*{t.name}* is now an admin!", parse_mode=ParseMode.MARKDOWN)


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teams = sorted(db.get_all_teams(), key=lambda t: t.total_spent, reverse=True)
    lines = [f"LEADERBOARD: *{auction.name}*\n{'='*26}"]
    medals = ["1st", "2nd", "3rd"]
    for i, team in enumerate(teams, 1):
        sq, ov = squad_stats(team)
        m = medals[i-1] if i <= 3 else f"#{i}"
        lines.append(f"{m} *{team.name}*\n  {fmt(team.total_spent)} spent | {len(sq)} players")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_clear_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != Config.SUPER_ADMIN_ID:
        await update.message.reply_text("Super Admin only.")
        return
    db.clear_players()
    await update.message.reply_text("All players cleared.")


# ─────────────────────────────────────────────
# CALLBACK HANDLER
# ─────────────────────────────────────────────

async def _do_rtm_accept(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    team = db.get_team(user_id)
    if not team:
        return

    player           = auction.current_player
    rtm_bid          = auction.current_bid
    prev_bidder_id   = auction.highest_bidder_id
    prev_bidder_name = auction.highest_bidder_name

    if auction.timer_task and not auction.timer_task.done():
        auction.timer_task.cancel()

    # Deduct RTM card
    db.conn.execute("UPDATE teams SET rtm_cards=MAX(0,rtm_cards-1) WHERE team_id=?", (user_id,))
    db.conn.commit()

    auction.rtm_phase          = False
    auction.rtm_team_id        = None
    auction.highest_bidder_id  = user_id
    auction.highest_bidder_name= team.name

    if prev_bidder_id:
        await context.bot.send_message(
            chat_id=auction.chat_id,
            text=(
                f"RTM ACCEPTED by *{team.name}*!\n{'='*26}\n"
                f"*{player.name}* matched at {fmt(rtm_bid)}\n\n"
                f"*{prev_bidder_name}* — you can now RAISE your bid!\n"
                f"Use /bid <higher amount> within {Config.RTM_TIMER}s to counter.\n"
                f"If no counter, {team.name} gets the player."
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        auction.timer_task = asyncio.create_task(bid_timer(context))
    else:
        await _finalize_sale(context, rtm_used=True)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data
    uid   = query.from_user.id

    if data == "join_auction":
        if db.get_team(uid):
            await query.answer("Already registered!", show_alert=True)
        else:
            db.register_team(uid, query.from_user.first_name)
            if uid == Config.SUPER_ADMIN_ID:
                db.set_admin(uid, True)
            purse = int(db.get_setting("purse", Config.DEFAULT_PURSE))
            await query.answer(f"Registered! Purse: {fmt(purse)}", show_alert=True)
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"*{query.from_user.first_name}* has joined the auction!",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    if data == "my_purse":
        team = db.get_team(uid)
        if not team:
            await query.answer("Register with /start!", show_alert=True)
            return
        sq, ov = squad_stats(team)
        limit  = int(db.get_setting("squad_limit", Config.DEFAULT_SQUAD_LIMIT))
        await query.answer(
            f"{team.name}\nPurse: {fmt(team.purse)}\nSquad: {len(sq)}/{limit}\nOverseas: {len(ov)}",
            show_alert=True,
        )
        return

    if data.startswith("bid_"):
        try:
            bid_l = int(data.split("_")[1])
        except (ValueError, IndexError):
            await query.answer("Invalid bid.", show_alert=True)
            return
        if not db.get_team(uid):
            await query.answer("Register with /start!", show_alert=True)
            return
        await process_bid(update, context, uid, bid_l)
        return

    if data == "rtm_accept":
        if not auction.rtm_phase:
            await query.answer("RTM phase already ended.", show_alert=True)
            return
        if auction.rtm_team_id != uid:
            await query.answer("RTM is not for your team!", show_alert=True)
            return
        await query.answer("RTM Accepted!", show_alert=True)
        try:
            t = db.get_team(uid)
            await query.edit_message_text(
                f"RTM ACCEPTED by *{t.name}*! Processing...",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        await _do_rtm_accept(context, uid)
        return

    if data == "rtm_decline":
        if not auction.rtm_phase:
            await query.answer("RTM phase already ended.", show_alert=True)
            return
        if auction.rtm_team_id != uid:
            await query.answer("RTM is not for your team!", show_alert=True)
            return
        auction.rtm_phase   = False
        auction.rtm_team_id = None
        if auction.timer_task and not auction.timer_task.done():
            auction.timer_task.cancel()
        t = db.get_team(uid)
        await query.answer("RTM Declined.", show_alert=True)
        try:
            await query.edit_message_text(
                f"RTM DECLINED by *{t.name}*. Player goes to highest bidder.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        await _finalize_sale(context, rtm_used=False)
        return

    await query.answer()


# ─────────────────────────────────────────────
# DOT COMMAND ROUTER
# ─────────────────────────────────────────────
DOT_MAP = {
    "setteamname": cmd_set_team_name, "stn": cmd_set_team_name,
    "resetname": cmd_reset_name, "rtn": cmd_reset_name, "resetteamname": cmd_reset_name,
    "purse": cmd_purse, "bal": cmd_purse, "balance": cmd_purse,
    "squad": cmd_squad,
    "bid": cmd_bid,
    "setpurse": cmd_set_purse, "setbal": cmd_set_purse, "setbalance": cmd_set_purse,
    "addpurse": cmd_add_purse, "addbal": cmd_add_purse, "addbalance": cmd_add_purse,
    "deductpurse": cmd_deduct_purse, "deductbal": cmd_deduct_purse, "deductbalance": cmd_deduct_purse,
    "addtosquad": cmd_add_to_squad, "ats": cmd_add_to_squad,
    "removefromsquad": cmd_remove_from_squad, "rfs": cmd_remove_from_squad,
    "clearsquad": cmd_clear_squad,
    "swap": cmd_swap,
    "setsquadlimit": cmd_set_squad_limit,
    "setcurrency": cmd_set_currency,
    "addtoqueue": cmd_add_to_queue, "atq": cmd_add_to_queue,
    "addtoqueueunsolds": cmd_add_unsolds_to_queue, "atqu": cmd_add_unsolds_to_queue,
    "removefromqueue": cmd_remove_from_queue, "rfq": cmd_remove_from_queue,
    "shufflequeue": cmd_shuffle_queue, "sq": cmd_shuffle_queue,
    "swapqueue": cmd_swap_queue,
    "clearqueue": cmd_clear_queue,
    "queue": cmd_view_queue, "q": cmd_view_queue,
    "startauction": cmd_start_auction,
    "next": cmd_next,
    "sold": cmd_sold,
    "forcesold": cmd_force_sold,
    "pauseauction": cmd_pause_auction,
    "resumeauction": cmd_resume_auction,
    "endauction": cmd_end_auction, "endsauction": cmd_end_auction,
    "autosell": cmd_auto_sell,
    "autonext": cmd_auto_next,
    "leaderboard": cmd_leaderboard,
    "help": cmd_help,
    "setauctionname": cmd_set_auction_name,
}


async def dot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text.startswith("."):
        return
    parts = text[1:].split()
    if not parts:
        return
    handler = DOT_MAP.get(parts[0].lower())
    if not handler:
        return
    context.args = parts[1:]
    await handler(update, context)


# ─────────────────────────────────────────────
# UTILITIES
# ─────────────────────────────────────────────
def _extract_uid(arg: str) -> Optional[int]:
    try:
        return int(arg.lstrip("@"))
    except ValueError:
        return None


def _resolve_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    if update.message and update.message.entities:
        for ent in update.message.entities:
            if ent.type == "text_mention" and ent.user:
                return ent.user.id
    if context.args:
        return _extract_uid(context.args[0])
    return None


def _parse_user_amount(update, context):
    if len(context.args) < 2:
        return None, None
    uid = _resolve_user(update, context)
    return uid, context.args[-1]


# ─────────────────────────────────────────────
# FLASK
# ─────────────────────────────────────────────
@flask_app.route("/")
def root():
    return "IPL Auction Bot v2.0 is running!", 200

@flask_app.route("/health")
def health():
    return {"status": "ok", "auction_active": auction.active, "name": auction.name}, 200


# ─────────────────────────────────────────────
# APP SETUP & MAIN
# ─────────────────────────────────────────────
def build_application() -> Application:
    app = Application.builder().token(Config.BOT_TOKEN).build()

    app.add_handler(CommandHandler(["start", "registration"], cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler(["setteamname", "stn"], cmd_set_team_name))
    app.add_handler(CommandHandler(["resetname", "rtn", "resetteamname"], cmd_reset_name))
    app.add_handler(CommandHandler(["purse", "bal", "balance"], cmd_purse))
    app.add_handler(CommandHandler("squad", cmd_squad))
    app.add_handler(CommandHandler("bid", cmd_bid))
    app.add_handler(CommandHandler("rtm", cmd_rtm if False else cmd_bid))  # RTM via button; /rtm = fallback
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("setauctionname", cmd_set_auction_name))

    app.add_handler(CommandHandler(["setpurse", "setbal", "setbalance"], cmd_set_purse))
    app.add_handler(CommandHandler(["addpurse", "addbal", "addbalance"], cmd_add_purse))
    app.add_handler(CommandHandler(["deductpurse", "deductbal", "deductbalance"], cmd_deduct_purse))
    app.add_handler(CommandHandler("setsquadlimit", cmd_set_squad_limit))
    app.add_handler(CommandHandler("setcurrency", cmd_set_currency))
    app.add_handler(CommandHandler(["addtosquad", "ats"], cmd_add_to_squad))
    app.add_handler(CommandHandler(["removefromsquad", "rfs"], cmd_remove_from_squad))
    app.add_handler(CommandHandler("clearsquad", cmd_clear_squad))
    app.add_handler(CommandHandler("swap", cmd_swap))

    app.add_handler(CommandHandler(["addtoqueue", "atq"], cmd_add_to_queue))
    app.add_handler(CommandHandler(["addtoqueueunsolds", "atqu"], cmd_add_unsolds_to_queue))
    app.add_handler(CommandHandler(["removefromqueue", "rfq"], cmd_remove_from_queue))
    app.add_handler(CommandHandler(["shufflequeue", "sq"], cmd_shuffle_queue))
    app.add_handler(CommandHandler("swapqueue", cmd_swap_queue))
    app.add_handler(CommandHandler("clearqueue", cmd_clear_queue))
    app.add_handler(CommandHandler(["queue", "q"], cmd_view_queue))

    app.add_handler(CommandHandler("startauction", cmd_start_auction))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("sold", cmd_sold))
    app.add_handler(CommandHandler("forcesold", cmd_force_sold))
    app.add_handler(CommandHandler("pauseauction", cmd_pause_auction))
    app.add_handler(CommandHandler("resumeauction", cmd_resume_auction))
    app.add_handler(CommandHandler(["endauction", "endsauction"], cmd_end_auction))
    app.add_handler(CommandHandler("autosell", cmd_auto_sell))
    app.add_handler(CommandHandler("autonext", cmd_auto_next))

    app.add_handler(CommandHandler("addplayer", cmd_add_player))
    app.add_handler(CommandHandler("add_player_list", cmd_add_player_list))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("clearplayers", cmd_clear_players))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^\."), dot_handler))

    return app


async def setup_webhook(app: Application, webhook_url: str):
    await app.initialize()
    await app.bot.set_webhook(
        url=f"{webhook_url}/webhook",
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )
    await app.start()
    logger.info(f"Webhook set: {webhook_url}/webhook")


def main():
    if not Config.BOT_TOKEN or Config.BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise ValueError("BOT_TOKEN not set!")
    if not Config.SUPER_ADMIN_ID:
        raise ValueError("SUPER_ADMIN_ID not set!")

    logger.info("Starting IPL Auction Bot v2.0...")
    ptb_app = build_application()

    if Config.WEBHOOK_URL:
        global _ptb_app
        _ptb_app = ptb_app
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        @flask_app.route("/webhook", methods=["POST"])
        def webhook_handler():
            from telegram import Update as _U
            data = request.get_json(force=True)
            upd  = _U.de_json(data, _ptb_app.bot)
            asyncio.run_coroutine_threadsafe(_ptb_app.process_update(upd), loop)
            return "ok", 200

        loop.run_until_complete(setup_webhook(ptb_app, Config.WEBHOOK_URL))
        import threading
        threading.Thread(
            target=lambda: flask_app.run(host="0.0.0.0", port=Config.PORT, use_reloader=False),
            daemon=True,
        ).start()
        logger.info(f"Webhook mode, port {Config.PORT}")
        loop.run_forever()
    else:
        logger.info("Polling mode")
        ptb_app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
