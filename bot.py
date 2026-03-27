"""
IPL Cricket Auction Telegram Bot — v3.0
Multi-auction, history, mute, teamup, full RTM state machine,
/create_auction, /pass, /status, ReAuction, /auctionhistory
"""

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import threading
import time as _time
from dataclasses import dataclass, field
from typing import Optional

from flask import Flask, request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────
class Config:
    BOT_TOKEN: str      = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
    SUPER_ADMIN_ID: int = int(os.getenv("SUPER_ADMIN_ID", "0"))
    WEBHOOK_URL: str    = os.getenv("WEBHOOK_URL", "")
    PORT: int           = int(os.getenv("PORT", "8080"))
    DB_PATH: str        = os.getenv("DATABASE_PATH", "auction.db")
    BID_TIMER: int      = 30
    RTM_TIMER: int      = 15
    ANTI_SNIPE: int     = 10
    INCREMENT: int      = 10   # default Lakhs


# ──────────────────────────────────────────────────────────
# DATACLASSES
# ──────────────────────────────────────────────────────────
@dataclass
class Player:
    player_id: int
    auction_id: int
    name: str
    base_price: int      # Lakhs
    role: str
    nationality: str
    ipl_team: str
    tier: str
    status: str = "available"
    sold_to: Optional[int] = None
    sold_price: Optional[int] = None


@dataclass
class Participant:
    user_id: int
    auction_id: int
    username: str        # @handle or first_name
    team_name: str
    purse: int           # Lakhs
    total_spent: int = 0
    squad: list = field(default_factory=list)   # list of player_ids
    is_muted: bool = False
    rtm_cards: int = 0


# RTM state machine values
RTM_NONE      = "none"
RTM_OFFERED   = "offered"       # prev team sees Accept/Decline buttons
RTM_ACCEPTED  = "accepted"      # prev team accepted, waiting for orig bidder counter
RTM_COUNTER   = "counter"       # orig bidder countered, prev team must Accept/Decline price


@dataclass
class AuctionLiveState:
    """In-memory live auction state (reset per auction run)."""
    active: bool               = False
    paused: bool               = False
    auction_id: Optional[int]  = None
    auction_name: str          = ""
    chat_id: Optional[int]     = None

    current_player: Optional[Player] = None
    current_bid: int           = 0
    highest_bidder_id: Optional[int]  = None
    highest_bidder_name: str   = ""
    last_msg_id: Optional[int] = None

    timer_task: Optional[asyncio.Task] = None
    timer_ends_at: Optional[float]     = None
    auto_sell_secs: Optional[int]      = None
    auto_next_secs: Optional[int]      = None
    auto_next_on: bool         = False

    player_queue: list         = field(default_factory=list)
    set_number: int            = 1
    sold_count: int            = 0
    unsold_count: int          = 0

    # RTM state machine
    rtm_state: str             = RTM_NONE
    rtm_team_id: Optional[int] = None       # Team A (prev team)
    rtm_team_name: str         = ""
    rtm_original_bidder_id: Optional[int]   = None   # Team B
    rtm_original_bidder_name: str= ""
    rtm_original_bid: int      = 0          # Team B's bid that triggered RTM
    rtm_counter_bid: int       = 0          # Team B's counter after RTM accept
    rtm_msg_id: Optional[int]  = None

    # ReAuction: hold last sold info briefly
    last_sold_player_id: Optional[int]  = None
    last_sold_player_name: str = ""
    last_sold_buyer_id: Optional[int]   = None
    last_sold_buyer_name: str  = ""
    last_sold_price: int       = 0
    reauction_msg_id: Optional[int]     = None

    # TeamUp links: {linked_user_id: primary_user_id}
    team_links: dict           = field(default_factory=dict)


live = AuctionLiveState()
flask_app = Flask(__name__)
_ptb_app: Optional[Application] = None


# ──────────────────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────────────────
class DB:
    _local = threading.local()

    def __init__(self, path: str = Config.DB_PATH):
        self.path = path
        self._init()

    def _cx(self) -> sqlite3.Connection:
        if not hasattr(self._local, "c") or self._local.c is None:
            self._local.c = sqlite3.connect(self.path, check_same_thread=False)
            self._local.c.row_factory = sqlite3.Row
        return self._local.c

    @property
    def cx(self):
        return self._cx()

    def _init(self):
        c = sqlite3.connect(self.path)
        c.executescript("""
        CREATE TABLE IF NOT EXISTS global_users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT DEFAULT '',
            first_name  TEXT DEFAULT '',
            is_admin    INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS auctions (
            auction_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            max_teams       INTEGER NOT NULL,
            purse           INTEGER NOT NULL,
            min_players     INTEGER DEFAULT 11,
            max_players     INTEGER DEFAULT 11,
            currency        TEXT DEFAULT 'Rs.',
            status          TEXT DEFAULT 'registration',
            chat_id         INTEGER,
            reg_msg_id      INTEGER,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS participants (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id  INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            username    TEXT DEFAULT '',
            team_name   TEXT DEFAULT '',
            purse       INTEGER NOT NULL,
            total_spent INTEGER DEFAULT 0,
            squad       TEXT DEFAULT '[]',
            is_muted    INTEGER DEFAULT 0,
            rtm_cards   INTEGER DEFAULT 0,
            joined_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(auction_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS players (
            player_id   INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id  INTEGER NOT NULL,
            name        TEXT NOT NULL,
            base_price  INTEGER NOT NULL,
            role        TEXT DEFAULT 'Batsman',
            nationality TEXT DEFAULT 'Indian',
            ipl_team    TEXT DEFAULT '',
            tier        TEXT DEFAULT 'C',
            status      TEXT DEFAULT 'available',
            sold_to     INTEGER,
            sold_price  INTEGER
        );
        CREATE TABLE IF NOT EXISTS auction_history (
            history_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id  INTEGER NOT NULL,
            summary     TEXT NOT NULL,
            completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS team_links (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id      INTEGER NOT NULL,
            primary_user_id INTEGER NOT NULL,
            linked_user_id  INTEGER NOT NULL,
            UNIQUE(auction_id, linked_user_id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        c.commit()
        c.close()

    # ── GLOBAL USERS ────────────────────────────────────
    def upsert_user(self, user_id: int, username: str, first_name: str):
        self.cx.execute(
            "INSERT INTO global_users(user_id,username,first_name) VALUES(?,?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
            (user_id, username or "", first_name or ""),
        )
        self.cx.commit()

    def get_user(self, user_id: int) -> Optional[sqlite3.Row]:
        return self.cx.execute("SELECT * FROM global_users WHERE user_id=?", (user_id,)).fetchone()

    def find_user_by_username(self, username: str) -> Optional[sqlite3.Row]:
        uname = username.lstrip("@").lower()
        return self.cx.execute(
            "SELECT * FROM global_users WHERE LOWER(username)=?", (uname,)
        ).fetchone()

    def resolve_user(self, arg: str) -> Optional[int]:
        """Resolve @username or raw user_id string → user_id int."""
        arg = arg.strip()
        if arg.startswith("@"):
            row = self.find_user_by_username(arg[1:])
            return int(row["user_id"]) if row else None
        try:
            return int(arg)
        except ValueError:
            return None

    def is_global_admin(self, user_id: int) -> bool:
        if user_id == Config.SUPER_ADMIN_ID:
            return True
        r = self.cx.execute("SELECT is_admin FROM global_users WHERE user_id=?", (user_id,)).fetchone()
        return bool(r and r["is_admin"])

    def set_global_admin(self, user_id: int, val: bool):
        self.cx.execute(
            "INSERT INTO global_users(user_id,is_admin) VALUES(?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET is_admin=excluded.is_admin",
            (user_id, 1 if val else 0),
        )
        self.cx.commit()

    def display_name(self, user_id: int) -> str:
        """Return 'TeamName (@username)' for a user."""
        r = self.get_user(user_id)
        if not r:
            return str(user_id)
        uname = f" (@{r['username']})" if r["username"] else ""
        return f"{r['first_name']}{uname}"

    # ── AUCTIONS ────────────────────────────────────────
    def create_auction(self, name: str, max_teams: int, purse: int,
                       min_p: int, max_p: int, chat_id: int) -> int:
        cur = self.cx.execute(
            "INSERT INTO auctions(name,max_teams,purse,min_players,max_players,chat_id)"
            " VALUES(?,?,?,?,?,?)",
            (name, max_teams, purse, min_p, max_p, chat_id),
        )
        self.cx.commit()
        return cur.lastrowid

    def get_auction(self, auction_id: int) -> Optional[sqlite3.Row]:
        return self.cx.execute("SELECT * FROM auctions WHERE auction_id=?", (auction_id,)).fetchone()

    def update_auction_status(self, auction_id: int, status: str):
        self.cx.execute("UPDATE auctions SET status=? WHERE auction_id=?", (status, auction_id))
        self.cx.commit()

    def set_reg_msg_id(self, auction_id: int, msg_id: int):
        self.cx.execute("UPDATE auctions SET reg_msg_id=? WHERE auction_id=?", (msg_id, auction_id))
        self.cx.commit()

    def count_participants(self, auction_id: int) -> int:
        r = self.cx.execute(
            "SELECT COUNT(*) as c FROM participants WHERE auction_id=?", (auction_id,)
        ).fetchone()
        return r["c"] if r else 0

    # ── PARTICIPANTS ─────────────────────────────────────
    def join_auction(self, auction_id: int, user_id: int, username: str,
                     team_name: str, purse: int) -> bool:
        try:
            self.cx.execute(
                "INSERT OR IGNORE INTO participants"
                "(auction_id,user_id,username,team_name,purse) VALUES(?,?,?,?,?)",
                (auction_id, user_id, username, team_name, purse),
            )
            self.cx.commit()
            return self.cx.execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(e)
            return False

    def get_participant(self, auction_id: int, user_id: int) -> Optional[Participant]:
        r = self.cx.execute(
            "SELECT * FROM participants WHERE auction_id=? AND user_id=?",
            (auction_id, user_id),
        ).fetchone()
        return self._to_part(r) if r else None

    def get_all_participants(self, auction_id: int) -> list:
        rows = self.cx.execute(
            "SELECT * FROM participants WHERE auction_id=?", (auction_id,)
        ).fetchall()
        return [self._to_part(r) for r in rows]

    def _to_part(self, r) -> Participant:
        return Participant(
            user_id=r["user_id"], auction_id=r["auction_id"],
            username=r["username"] or "", team_name=r["team_name"] or "",
            purse=r["purse"], total_spent=r["total_spent"],
            squad=json.loads(r["squad"]),
            is_muted=bool(r["is_muted"]), rtm_cards=r["rtm_cards"],
        )

    def update_part_purse(self, auction_id: int, user_id: int, deduct: int):
        self.cx.execute(
            "UPDATE participants SET purse=purse-?, total_spent=total_spent+?"
            " WHERE auction_id=? AND user_id=?",
            (deduct, deduct, auction_id, user_id),
        )
        self.cx.commit()

    def refund_part_purse(self, auction_id: int, user_id: int, amount: int):
        self.cx.execute(
            "UPDATE participants SET purse=purse+?, total_spent=MAX(0,total_spent-?)"
            " WHERE auction_id=? AND user_id=?",
            (amount, amount, auction_id, user_id),
        )
        self.cx.commit()

    def add_to_squad(self, auction_id: int, user_id: int, player_id: int):
        part = self.get_participant(auction_id, user_id)
        if part:
            sq = part.squad
            if player_id not in sq:
                sq.append(player_id)
            self.cx.execute(
                "UPDATE participants SET squad=? WHERE auction_id=? AND user_id=?",
                (json.dumps(sq), auction_id, user_id),
            )
            self.cx.commit()

    def remove_from_squad(self, auction_id: int, user_id: int, player_id: int):
        part = self.get_participant(auction_id, user_id)
        if part and player_id in part.squad:
            sq = [p for p in part.squad if p != player_id]
            self.cx.execute(
                "UPDATE participants SET squad=? WHERE auction_id=? AND user_id=?",
                (json.dumps(sq), auction_id, user_id),
            )
            self.cx.commit()

    def set_muted(self, auction_id: int, user_id: int, muted: bool):
        self.cx.execute(
            "UPDATE participants SET is_muted=? WHERE auction_id=? AND user_id=?",
            (1 if muted else 0, auction_id, user_id),
        )
        self.cx.commit()

    def set_team_name(self, auction_id: int, user_id: int, name: str):
        self.cx.execute(
            "UPDATE participants SET team_name=? WHERE auction_id=? AND user_id=?",
            (name, auction_id, user_id),
        )
        self.cx.commit()

    def set_purse(self, auction_id: int, user_id: int, amount: int):
        self.cx.execute(
            "UPDATE participants SET purse=? WHERE auction_id=? AND user_id=?",
            (amount, auction_id, user_id),
        )
        self.cx.commit()

    def add_purse(self, auction_id: int, user_id: int, amount: int):
        self.cx.execute(
            "UPDATE participants SET purse=purse+? WHERE auction_id=? AND user_id=?",
            (amount, auction_id, user_id),
        )
        self.cx.commit()

    def deduct_purse(self, auction_id: int, user_id: int, amount: int):
        self.cx.execute(
            "UPDATE participants SET purse=MAX(0,purse-?), total_spent=total_spent+?"
            " WHERE auction_id=? AND user_id=?",
            (amount, amount, auction_id, user_id),
        )
        self.cx.commit()

    def clear_squad(self, auction_id: int, user_id: int):
        self.cx.execute(
            "UPDATE participants SET squad='[]', total_spent=0 WHERE auction_id=? AND user_id=?",
            (auction_id, user_id),
        )
        self.cx.commit()

    def swap_participants(self, auction_id: int, uid1: int, uid2: int) -> bool:
        p1 = self.get_participant(auction_id, uid1)
        p2 = self.get_participant(auction_id, uid2)
        if not p1 or not p2:
            return False
        self.cx.execute(
            "UPDATE participants SET purse=?,total_spent=?,squad=? WHERE auction_id=? AND user_id=?",
            (p2.purse, p2.total_spent, json.dumps(p2.squad), auction_id, uid1),
        )
        self.cx.execute(
            "UPDATE participants SET purse=?,total_spent=?,squad=? WHERE auction_id=? AND user_id=?",
            (p1.purse, p1.total_spent, json.dumps(p1.squad), auction_id, uid2),
        )
        self.cx.commit()
        return True

    # ── PLAYERS ─────────────────────────────────────────
    def add_player(self, auction_id: int, name: str, base_price: int,
                   role: str, nat: str, ipl_team: str, tier: str) -> int:
        cur = self.cx.execute(
            "INSERT INTO players(auction_id,name,base_price,role,nationality,ipl_team,tier)"
            " VALUES(?,?,?,?,?,?,?)",
            (auction_id, name, base_price, role, nat, ipl_team, tier),
        )
        self.cx.commit()
        return cur.lastrowid

    def get_player(self, player_id: int) -> Optional[Player]:
        r = self.cx.execute("SELECT * FROM players WHERE player_id=?", (player_id,)).fetchone()
        return self._to_player(r) if r else None

    def get_available_players(self, auction_id: int) -> list:
        rows = self.cx.execute(
            "SELECT * FROM players WHERE auction_id=? AND status='available' ORDER BY player_id",
            (auction_id,),
        ).fetchall()
        return [self._to_player(r) for r in rows]

    def get_unsold_players(self, auction_id: int) -> list:
        rows = self.cx.execute(
            "SELECT * FROM players WHERE auction_id=? AND status='unsold'", (auction_id,)
        ).fetchall()
        return [self._to_player(r) for r in rows]

    def update_player(self, player_id: int, status: str,
                      sold_to: Optional[int] = None, sold_price: Optional[int] = None):
        self.cx.execute(
            "UPDATE players SET status=?,sold_to=?,sold_price=? WHERE player_id=?",
            (status, sold_to, sold_price, player_id),
        )
        self.cx.commit()

    def _to_player(self, r) -> Player:
        return Player(
            player_id=r["player_id"], auction_id=r["auction_id"],
            name=r["name"], base_price=r["base_price"], role=r["role"],
            nationality=r["nationality"], ipl_team=r["ipl_team"] or "",
            tier=r["tier"], status=r["status"],
            sold_to=r["sold_to"], sold_price=r["sold_price"],
        )

    def clear_players(self, auction_id: int):
        self.cx.execute("DELETE FROM players WHERE auction_id=?", (auction_id,))
        self.cx.commit()

    # ── AUCTION HISTORY ──────────────────────────────────
    def save_history(self, auction_id: int, summary: dict):
        self.cx.execute(
            "INSERT INTO auction_history(auction_id,summary) VALUES(?,?)",
            (auction_id, json.dumps(summary)),
        )
        self.cx.commit()

    def get_history_list(self, user_id: int) -> list:
        """Last 10 auctions this user participated in."""
        rows = self.cx.execute("""
            SELECT ah.history_id, ah.auction_id, ah.completed_at,
                   a.name as auction_name, ah.summary
            FROM auction_history ah
            JOIN auctions a ON ah.auction_id = a.auction_id
            JOIN participants p ON p.auction_id = a.auction_id AND p.user_id = ?
            ORDER BY ah.history_id DESC LIMIT 10
        """, (user_id,)).fetchall()
        return rows

    def get_history_entry(self, history_id: int) -> Optional[sqlite3.Row]:
        return self.cx.execute(
            "SELECT * FROM auction_history WHERE history_id=?", (history_id,)
        ).fetchone()

    # ── TEAM LINKS ───────────────────────────────────────
    def link_teams(self, auction_id: int, primary_uid: int, linked_uid: int):
        self.cx.execute(
            "INSERT OR REPLACE INTO team_links(auction_id,primary_user_id,linked_user_id)"
            " VALUES(?,?,?)",
            (auction_id, primary_uid, linked_uid),
        )
        self.cx.commit()

    def get_primary(self, auction_id: int, linked_uid: int) -> Optional[int]:
        r = self.cx.execute(
            "SELECT primary_user_id FROM team_links WHERE auction_id=? AND linked_user_id=?",
            (auction_id, linked_uid),
        ).fetchone()
        return r["primary_user_id"] if r else None

    # ── SETTINGS ────────────────────────────────────────
    def get_setting(self, key: str, default=None):
        r = self.cx.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_setting(self, key: str, value):
        self.cx.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value))
        )
        self.cx.commit()


db = DB()


# ──────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────
def currency(auction_id: Optional[int] = None) -> str:
    if auction_id:
        r = db.cx.execute("SELECT currency FROM auctions WHERE auction_id=?", (auction_id,)).fetchone()
        if r:
            return r["currency"]
    return db.get_setting("currency", "Rs.")


def fmt(lakhs: int, auction_id: Optional[int] = None) -> str:
    s = currency(auction_id)
    if lakhs >= 100:
        c = lakhs / 100
        return f"{s}{c:.1f}Cr" if c % 1 else f"{s}{int(c)}Cr"
    return f"{s}{lakhs}L"


def parse_price(s: str) -> Optional[int]:
    s = s.strip().lower().replace(" ", "")
    try:
        if s.endswith("cr"):  return int(float(s[:-2]) * 100)
        if s.endswith("l"):   return int(float(s[:-1]))
        return int(s)
    except ValueError:
        return None


def flag(nat: str) -> str:
    return "🇮🇳" if nat.lower() == "indian" else "🌍"


def role_emoji(r: str) -> str:
    return {"batsman":"🏏","bat":"🏏","bowler":"⚡","bowl":"⚡",
            "all-rounder":"🌟","allrounder":"🌟","ar":"🌟",
            "wicketkeeper":"🧤","wk":"🧤"}.get(r.lower(),"🏏")


def tier_str(t: str) -> str:
    return {"Marquee":"⭐Marquee","A":"🔷A","B":"🔹B","C":"▪️C","Uncapped":"🔸Uncapped"}.get(t, t)


def normalize_role(r: str) -> str:
    r = r.lower().strip()
    if r in ("bat","batsman","batter"):          return "Batsman"
    if r in ("bowl","bowler"):                   return "Bowler"
    if r in ("ar","allrounder","all-rounder"):   return "All-rounder"
    if r in ("wk","wicketkeeper","keeper"):      return "Wicketkeeper"
    return r.title()


def normalize_nat(n: str) -> str:
    return "Indian" if n.lower() in ("indian","india","ind") else "Overseas"


def part_display(part: Participant) -> str:
    """'TeamName (@username)' format."""
    uname = f" (@{part.username})" if part.username else ""
    return f"{part.team_name}{uname}"


def resolve_participant(auction_id: int, arg: str) -> Optional[Participant]:
    """Resolve @username or user_id → Participant."""
    uid = db.resolve_user(arg)
    if uid:
        return db.get_participant(auction_id, uid)
    return None


def effective_user_id(user_id: int, auction_id: int) -> int:
    """If this user is a teamup proxy, return the primary user_id."""
    primary = db.get_primary(auction_id, user_id)
    return primary if primary else user_id


def validate_bid(part: Participant, player: Player, bid_l: int,
                 auction_row: sqlite3.Row) -> Optional[str]:
    if part.is_muted:
        return "Your team is muted and cannot bid."
    if part.purse < bid_l:
        return f"Not enough purse! You have {fmt(part.purse, part.auction_id)} left."
    max_sq = auction_row["max_players"]
    if len(part.squad) >= max_sq:
        return f"Squad full! Max {max_sq} players."
    if bid_l < player.base_price:
        return f"Min bid is {fmt(player.base_price, part.auction_id)}."
    if live.current_bid > 0 and bid_l <= live.current_bid:
        return f"Bid must exceed current {fmt(live.current_bid, part.auction_id)}."
    return None


# ──────────────────────────────────────────────────────────
# MESSAGE BUILDERS
# ──────────────────────────────────────────────────────────
def player_card(p: Player) -> str:
    rtm = f"\n🎴 RTM: {p.ipl_team} can match!" if p.ipl_team else ""
    return (
        f"{'─'*28}\n"
        f"{role_emoji(p.role)} *{flag(p.nationality)} {p.name}*\n"
        f"Role: {p.role}  |  {p.nationality}\n"
        f"Tier: {tier_str(p.tier)}\n"
        f"Base: *{fmt(p.base_price, p.auction_id)}*  |  Prev: *{p.ipl_team or 'None'}*"
        f"{rtm}\n"
    )


def bid_msg(p: Player, bid: int, bidder: str, timer: Optional[int] = None) -> str:
    aid = p.auction_id
    bid_line = (
        f"Highest Bid: *{fmt(bid, aid)}*\nHighest Bidder: *{bidder}*"
        if bid > 0
        else f"Base: *{fmt(p.base_price, aid)}*  —  No bids yet"
    )
    t = f"⏱ Timer: *{timer}s*" if timer is not None else "⏱ Timer starts on first bid"
    return (
        f"🔨 *{live.auction_name}* — Set {live.set_number}\n"
        f"{player_card(p)}\n"
        f"{bid_line}\n{t}"
    )


def bid_keyboard(p: Player, current_bid: int) -> InlineKeyboardMarkup:
    b1 = p.base_price if current_bid == 0 else current_bid + Config.INCREMENT
    b2 = b1 + Config.INCREMENT
    aid = p.auction_id
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"Bid {fmt(b1,aid)}", callback_data=f"bid_{b1}"),
            InlineKeyboardButton(f"Bid {fmt(b2,aid)}", callback_data=f"bid_{b2}"),
        ],
        [InlineKeyboardButton("My Purse", callback_data="my_purse")],
    ])


def rtm_offer_msg(p: Player, bid: int, bidder: str, rtm_team: str) -> str:
    aid = p.auction_id
    return (
        f"🎴 *RTM OPPORTUNITY!*\n{'─'*28}\n"
        f"Player: *{p.name}*\n"
        f"Highest Bid: *{fmt(bid,aid)}* by *{bidder}*\n\n"
        f"*{rtm_team}* — You have *{Config.RTM_TIMER}s* to use your RTM card!\n\n"
        f"✅ *Accept RTM* → Match {fmt(bid,aid)}, player is yours\n"
        f"❌ *Decline RTM* → Player goes to {bidder}"
    )


def rtm_offer_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Accept RTM", callback_data="rtm_accept"),
        InlineKeyboardButton("❌ Decline RTM", callback_data="rtm_decline"),
    ]])


def rtm_counter_ask_msg(p: Player, counter: int, rtm_team: str) -> str:
    aid = p.auction_id
    return (
        f"🔥 *RTM COUNTER BID!*\n{'─'*28}\n"
        f"Player: *{p.name}*\n"
        f"*{live.rtm_original_bidder_name}* raised the bid to *{fmt(counter,aid)}*\n\n"
        f"*{rtm_team}* — Do you accept *{fmt(counter,aid)}* for {p.name}?\n\n"
        f"✅ *Yes* → You buy {p.name} for {fmt(counter,aid)}\n"
        f"❌ *No* → Player goes to {live.rtm_original_bidder_name} for {fmt(live.rtm_original_bid,aid)}"
    )


def rtm_counter_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, buy", callback_data="rtm_counter_yes"),
        InlineKeyboardButton("❌ No, pass", callback_data="rtm_counter_no"),
    ]])


def reauction_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 ReAuction", callback_data="reauction_prompt")
    ]])


def reauction_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, ReAuction", callback_data="reauction_yes"),
        InlineKeyboardButton("❌ No, Next", callback_data="reauction_no"),
    ]])


# ──────────────────────────────────────────────────────────
# TIMER & AUCTION CORE LOGIC
# ──────────────────────────────────────────────────────────
async def bid_timer(context: ContextTypes.DEFAULT_TYPE):
    duration = live.auto_sell_secs or Config.BID_TIMER
    end = _time.time() + duration
    live.timer_ends_at = end

    while True:
        await asyncio.sleep(5)
        if not live.active or live.paused or not live.current_player:
            return
        remaining = max(0, int(live.timer_ends_at - _time.time()))
        if remaining <= 0:
            break
        if live.last_msg_id:
            try:
                p = live.current_player
                await context.bot.edit_message_text(
                    chat_id=live.chat_id,
                    message_id=live.last_msg_id,
                    text=bid_msg(p, live.current_bid, live.highest_bidder_name, remaining),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=bid_keyboard(p, live.current_bid),
                )
            except Exception:
                pass

    if not live.active or not live.current_player:
        return

    if live.current_bid == 0:
        await _no_bids(context)
    else:
        await _check_rtm(context)


async def _no_bids(context: ContextTypes.DEFAULT_TYPE):
    p = live.current_player
    db.update_player(p.player_id, "unsold")
    live.unsold_count += 1
    live.current_player = None
    msg = await context.bot.send_message(
        chat_id=live.chat_id,
        text=f"❌ *{p.name}* goes *UNSOLD!* No bids received.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reauction_keyboard(),
    )
    live.last_sold_player_id   = p.player_id
    live.last_sold_player_name = p.name
    live.last_sold_buyer_id    = None
    live.last_sold_buyer_name  = ""
    live.last_sold_price       = 0
    live.reauction_msg_id      = msg.message_id
    await _try_auto_next(context)


async def _check_rtm(context: ContextTypes.DEFAULT_TYPE):
    p = live.current_player
    if p.ipl_team:
        # Find the participant whose team_name contains the ipl_team string
        rtm_part = _find_part_by_ipl_name(p.ipl_team)
        if rtm_part and rtm_part.user_id != live.highest_bidder_id and not rtm_part.is_muted:
            if rtm_part.purse >= live.current_bid:
                # Offer RTM
                live.rtm_state            = RTM_OFFERED
                live.rtm_team_id          = rtm_part.user_id
                live.rtm_team_name        = part_display(rtm_part)
                live.rtm_original_bidder_id   = live.highest_bidder_id
                live.rtm_original_bidder_name = live.highest_bidder_name
                live.rtm_original_bid     = live.current_bid

                msg = await context.bot.send_message(
                    chat_id=live.chat_id,
                    text=rtm_offer_msg(p, live.current_bid,
                                       live.highest_bidder_name, live.rtm_team_name),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=rtm_offer_keyboard(),
                )
                live.rtm_msg_id = msg.message_id
                live.timer_task = asyncio.create_task(_rtm_timer(context))
                return
    await _finalize_sale(context)


async def _rtm_timer(context: ContextTypes.DEFAULT_TYPE):
    end = _time.time() + Config.RTM_TIMER
    while _time.time() < end:
        await asyncio.sleep(1)
        if live.rtm_state not in (RTM_OFFERED, RTM_ACCEPTED):
            return
    # Timed out
    if live.rtm_state == RTM_OFFERED:
        live.rtm_state = RTM_NONE
        await context.bot.send_message(
            chat_id=live.chat_id,
            text="⏰ RTM window expired. Player goes to highest bidder.",
        )
        await _finalize_sale(context)
    elif live.rtm_state == RTM_ACCEPTED:
        # Orig bidder didn't counter — sell to RTM team at original bid
        live.rtm_state = RTM_NONE
        live.highest_bidder_id   = live.rtm_team_id
        live.highest_bidder_name = live.rtm_team_name
        live.current_bid         = live.rtm_original_bid
        await _finalize_sale(context, rtm_used=True)


async def _finalize_sale(context: ContextTypes.DEFAULT_TYPE, rtm_used: bool = False):
    p = live.current_player
    if not p or not live.highest_bidder_id:
        return

    winner_id    = live.highest_bidder_id
    winner_name  = live.highest_bidder_name
    final_price  = live.current_bid
    aid          = live.auction_id

    db.update_player(p.player_id, "sold", winner_id, final_price)
    db.update_part_purse(aid, winner_id, final_price)
    db.add_to_squad(aid, winner_id, p.player_id)

    live.sold_count += 1
    winner_part = db.get_participant(aid, winner_id)

    # Stash for reauction
    live.last_sold_player_id   = p.player_id
    live.last_sold_player_name = p.name
    live.last_sold_buyer_id    = winner_id
    live.last_sold_buyer_name  = winner_name
    live.last_sold_price       = final_price

    # Reset live state
    live.current_player        = None
    live.current_bid           = 0
    live.highest_bidder_id     = None
    live.highest_bidder_name   = ""
    live.rtm_state             = RTM_NONE
    live.rtm_team_id           = None

    remaining_purse = winner_part.purse if winner_part else 0

    msg = await context.bot.send_message(
        chat_id=live.chat_id,
        text=(
            f"🔨 *SOLD!*\n{'─'*28}\n"
            f"Player: *{p.name}*\n"
            f"Team: *{winner_name}*\n"
            f"Price: *{fmt(final_price,aid)}*"
            f"{'  🎴 RTM' if rtm_used else ''}\n\n"
            f"Remaining purse: *{fmt(remaining_purse,aid)}*"
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reauction_keyboard(),
    )
    live.reauction_msg_id = msg.message_id
    await _try_auto_next(context)


async def _try_auto_next(context: ContextTypes.DEFAULT_TYPE):
    if live.auto_next_on and live.auto_next_secs and live.active:
        await asyncio.sleep(live.auto_next_secs)
        if not live.current_player and not live.paused and live.active:
            await _do_next(context, live.chat_id)


def _find_part_by_ipl_name(ipl_team: str) -> Optional[Participant]:
    ipl_l = ipl_team.lower()
    for p in db.get_all_participants(live.auction_id):
        if ipl_l in p.team_name.lower() or p.team_name.lower() in ipl_l:
            return p
    return None


# ──────────────────────────────────────────────────────────
# NEXT PLAYER
# ──────────────────────────────────────────────────────────
async def _do_next(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    if not live.active:
        return
    if live.current_player:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Current player still active! Use /sold or /forcesold first.",
        )
        return

    if not live.player_queue:
        unsold = db.get_unsold_players(live.auction_id)
        if unsold:
            live.player_queue = unsold
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"♻️ Loading {len(unsold)} unsold players back into queue...",
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id, text="✅ All players auctioned! Use /endauction for summary.",
            )
            return

    player = live.player_queue.pop(0)
    fresh  = db.get_player(player.player_id)
    if not fresh or fresh.status != "available":
        await _do_next(context, chat_id)
        return

    live.current_player        = fresh
    live.current_bid           = 0
    live.highest_bidder_id     = None
    live.highest_bidder_name   = ""
    live.timer_task            = None
    live.timer_ends_at         = None
    live.rtm_state             = RTM_NONE
    live.reauction_msg_id      = None

    queued = len(live.player_queue)
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🔨 *{live.auction_name}*\n"
            f"{player_card(fresh)}\n"
            f"⏱ Timer starts on first bid\n"
            f"📋 Players remaining: {queued}"
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=bid_keyboard(fresh, 0),
    )
    live.last_msg_id = msg.message_id
    live.chat_id     = chat_id


# ──────────────────────────────────────────────────────────
# BID PROCESSING
# ──────────────────────────────────────────────────────────
async def process_bid(update, context: ContextTypes.DEFAULT_TYPE,
                      user_id: int, bid_l: int):
    aid = live.auction_id

    # TeamUp: if user is a proxy, act as primary
    effective_uid = effective_user_id(user_id, aid)
    part = db.get_participant(aid, effective_uid)

    def _err(msg: str):
        if update.callback_query:
            return update.callback_query.answer(msg, show_alert=True)
        return update.message.reply_text(msg)

    if not part:
        await _err("You are not registered in this auction.")
        return
    if not live.active or not live.current_player:
        await _err("No active auction right now.")
        return
    if live.paused:
        await _err("Auction is paused.")
        return
    if live.rtm_state == RTM_OFFERED:
        await _err("RTM phase active. Wait for RTM to resolve.")
        return

    # Block current highest bidder from bidding again (unless in RTM_ACCEPTED phase)
    if live.highest_bidder_id == effective_uid and live.rtm_state != RTM_ACCEPTED:
        await _err(f"You are already the highest bidder at {fmt(live.current_bid,aid)}!")
        return

    # In RTM_ACCEPTED phase only the original bidder can counter
    if live.rtm_state == RTM_ACCEPTED and effective_uid != live.rtm_original_bidder_id:
        await _err("Waiting for original bidder to counter or pass.")
        return

    auction_row = db.get_auction(aid)
    err = validate_bid(part, live.current_player, bid_l, auction_row)
    if err:
        await _err(err)
        return

    # Anti-snipe
    if live.timer_ends_at:
        remaining = live.timer_ends_at - _time.time()
        if 0 < remaining < Config.ANTI_SNIPE:
            live.timer_ends_at = _time.time() + Config.ANTI_SNIPE

    prev_name = live.highest_bidder_name

    # If RTM_ACCEPTED and orig bidder is countering → move to COUNTER state
    if live.rtm_state == RTM_ACCEPTED and effective_uid == live.rtm_original_bidder_id:
        if live.timer_task and not live.timer_task.done():
            live.timer_task.cancel()
        live.rtm_state       = RTM_COUNTER
        live.rtm_counter_bid = bid_l

        p = live.current_player
        msg = await context.bot.send_message(
            chat_id=live.chat_id,
            text=rtm_counter_ask_msg(p, bid_l, live.rtm_team_name),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rtm_counter_keyboard(),
        )
        live.rtm_msg_id = msg.message_id
        if update.callback_query:
            await update.callback_query.answer(
                f"Counter bid of {fmt(bid_l,aid)} sent to {live.rtm_team_name}!"
            )
        else:
            await update.message.reply_text(
                f"⬆️ Counter bid of *{fmt(bid_l,aid)}* sent! Waiting for {live.rtm_team_name}...",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    # Normal bid
    live.current_bid        = bid_l
    live.highest_bidder_id  = effective_uid
    live.highest_bidder_name= part_display(part)

    if live.timer_task is None or live.timer_task.done():
        live.timer_task = asyncio.create_task(bid_timer(context))

    p = live.current_player
    duration = live.auto_sell_secs or Config.BID_TIMER
    outbid = f"⬆️ Outbids: {prev_name}" if prev_name and prev_name != part_display(part) else "🎯 Opening bid!"

    new_msg = await context.bot.send_message(
        chat_id=live.chat_id,
        text=(
            f"💥 *New Bid*\n{'─'*28}\n"
            f"Player: *{p.name}*\n"
            f"Amount: *{fmt(bid_l,aid)}*\n"
            f"Team: *{part_display(part)}*\n"
            f"{outbid}\n"
            f"⏱ Timer: {duration}s"
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=bid_keyboard(p, bid_l),
    )
    live.last_msg_id = new_msg.message_id

    if update.callback_query:
        await update.callback_query.answer(f"Bid of {fmt(bid_l,aid)} placed!")


# ──────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ──────────────────────────────────────────────────────────

async def _register_user(user):
    """Save/update user in global_users table."""
    db.upsert_user(user.id, user.username or "", user.first_name or "")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await _register_user(user)
    uname = f"@{user.username}" if user.username else user.first_name
    await update.message.reply_text(
        f"Welcome *{uname}*!\n\n"
        f"Use /create\\_auction to start a new auction.\n"
        f"Use /help for all commands.\n\n"
        f"Your ID: `{user.id}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    is_adm = db.is_global_admin(uid)
    text = (
        "*USER COMMANDS*\n"
        "/start — Register\n"
        "/setteamname <n> — Set your team name\n"
        "/purse [@user] — Check purse\n"
        "/squad [@user] — View squad\n"
        "/bid <amount> — Bid (e.g. /bid 2cr)\n"
        "/status — Current auction status\n"
        "/auctionhistory — View past auctions\n"
        "/leaderboard — Top teams\n"
    )
    if is_adm:
        text += (
            "\n*ADMIN: AUCTION*\n"
            "/create\\_auction <teams>,<purse>,<min\\_max> — Create auction\n"
            "/setauctionname <n> — Rename\n"
            "/startauction — Begin bidding\n"
            "/next — Next player\n"
            "/pass — Pass/unsell current player\n"
            "/sold — Confirm sale\n"
            "/forcesold — Force sell (skip RTM)\n"
            "/pauseauction | /resumeauction\n"
            "/endauction — End session\n"
            "/autosell <secs|off>\n"
            "/autonext <enable|disable|secs>\n"
            "\n*ADMIN: TEAMS*\n"
            "/mute\\_team @user — Mute team\n"
            "/unmute\\_team @user — Unmute team\n"
            "/teamup @u1 @u2 — Link users\n"
            "/setpurse @user <amt>\n"
            "/addpurse @user <amt>\n"
            "/deductpurse @user <amt>\n"
            "/setsquadlimit <n>\n"
            "/addtosquad @user p1,p2\n"
            "/removefromsquad @user 1,2\n"
            "/clearsquad @user\n"
            "/swap @u1 @u2\n"
            "/setteamname @user <n>\n"
            "\n*ADMIN: QUEUE*\n"
            "/addtoqueue p1,p2  (.atq)\n"
            "/addtoqueueunsolds  (.atqu)\n"
            "/removefromqueue 1,2  (.rfq)\n"
            "/shufflequeue  (.sq)\n"
            "/swapqueue 1 2\n"
            "/clearqueue\n"
            "/queue [page]\n"
            "\n*ADMIN: PLAYERS*\n"
            "/addplayer <n> <role> <team> <nat> <price> [tier]\n"
            "/add\\_player\\_list — Bulk add\n"
            "/admin @user — Promote admin\n"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ── CREATE AUCTION ────────────────────────────────────────

async def cmd_create_auction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await _register_user(user)
    if not db.is_global_admin(user.id):
        await update.message.reply_text("Admin only.")
        return

    # Parse: /create_auction 10,100cr,11_11  or  /create_auction 10, 100cr, 11_11
    raw = " ".join(context.args).replace(" ", "")
    parts = raw.split(",")
    if len(parts) < 3:
        await update.message.reply_text(
            "Usage: /create\\_auction <teams>,<purse>,<min\\_max>\n"
            "Example: /create\\_auction 10,100cr,11\\_25\n"
            "(min\\_max can be 11\\_11 or just 11 for both)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        max_teams = int(parts[0])
    except ValueError:
        await update.message.reply_text("Invalid team count.")
        return

    purse = parse_price(parts[1])
    if not purse:
        await update.message.reply_text("Invalid purse amount.")
        return

    player_range = parts[2].replace("-", "_").split("_")
    try:
        min_p = int(player_range[0])
        max_p = int(player_range[1]) if len(player_range) > 1 else min_p
    except (ValueError, IndexError):
        await update.message.reply_text("Invalid player range. Use min_max e.g. 11_25")
        return

    name = f"IPL Auction #{db.cx.execute('SELECT COUNT(*) FROM auctions').fetchone()[0]+1}"
    aid  = db.create_auction(name, max_teams, purse, min_p, max_p, update.effective_chat.id)

    # Update live state
    live.auction_id   = aid
    live.auction_name = name

    join_btn = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"🏏 Join Auction ({0}/{max_teams})", callback_data=f"join_{aid}")
    ]])

    msg = await update.message.reply_text(
        f"🏏 *{name}*\n{'─'*28}\n"
        f"Teams: {max_teams}\n"
        f"Purse per team: {fmt(purse, aid)}\n"
        f"Squad: {min_p}–{max_p} players\n\n"
        f"Tap the button below to join!\n"
        f"Teams joined: *0/{max_teams}*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=join_btn,
    )
    db.set_reg_msg_id(aid, msg.message_id)


# ── AUCTION NAME ──────────────────────────────────────────

async def cmd_set_auction_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args or not live.auction_id:
        await update.message.reply_text("Usage: /setauctionname <n>")
        return
    name = " ".join(context.args)
    live.auction_name = name
    db.cx.execute("UPDATE auctions SET name=? WHERE auction_id=?", (name, live.auction_id))
    db.cx.commit()
    await update.message.reply_text(f"Auction name set to: *{name}*", parse_mode=ParseMode.MARKDOWN)


# ── USER COMMANDS ─────────────────────────────────────────

async def cmd_set_team_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await _register_user(user)
    aid = live.auction_id
    if not aid:
        await update.message.reply_text("No active auction session.")
        return

    # Admin can set for someone else
    if context.args and db.is_global_admin(user.id) and (context.args[0].startswith("@") or context.args[0].isdigit()):
        target_uid = db.resolve_user(context.args[0])
        name       = " ".join(context.args[1:])
        if not target_uid or not name:
            await update.message.reply_text("Usage: /setteamname @user <n>")
            return
    else:
        target_uid = user.id
        name = " ".join(context.args)
        if not name:
            await update.message.reply_text("Usage: /setteamname <Your Team Name>")
            return

    db.set_team_name(aid, target_uid, name)
    # Also update participants table username for display
    await update.message.reply_text(f"Team name set to: *{name}*", parse_mode=ParseMode.MARKDOWN)


async def cmd_purse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await _register_user(user)
    aid = live.auction_id
    if not aid:
        await update.message.reply_text("No active auction.")
        return

    if context.args:
        uid = db.resolve_user(context.args[0])
        if not uid:
            await update.message.reply_text("User not found.")
            return
    else:
        uid = effective_user_id(user.id, aid)

    part = db.get_participant(aid, uid)
    if not part:
        await update.message.reply_text("Not registered in this auction.")
        return

    auction_row = db.get_auction(aid)
    sq = [db.get_player(pid) for pid in part.squad]
    sq = [p for p in sq if p]
    overseas = sum(1 for p in sq if p.nationality == "Overseas")

    lines = [
        f"💼 *{part_display(part)}*\n{'─'*28}\n"
        f"Purse: *{fmt(part.purse,aid)}*\n"
        f"Spent: {fmt(part.total_spent,aid)}\n"
        f"Squad: {len(sq)}/{auction_row['max_players']}\n"
        f"Indian: {len(sq)-overseas}  |  Overseas: {overseas}\n"
        f"RTM Cards: {part.rtm_cards}\n"
        f"Muted: {'Yes 🔇' if part.is_muted else 'No'}\n\nBy Role:"
    ]
    roles: dict = {}
    for p in sq:
        roles[p.role] = roles.get(p.role, 0) + 1
    for r, c in roles.items():
        lines.append(f"  {role_emoji(r)} {r}: {c}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_squad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await _register_user(user)
    aid = live.auction_id
    if not aid:
        await update.message.reply_text("No active auction.")
        return

    if context.args:
        uid = db.resolve_user(context.args[0])
    else:
        uid = effective_user_id(user.id, aid)

    part = db.get_participant(aid, uid) if uid else None
    if not part:
        await update.message.reply_text("Team not found in this auction.")
        return

    sq = [db.get_player(pid) for pid in part.squad]
    sq = [p for p in sq if p]
    if not sq:
        await update.message.reply_text(f"*{part_display(part)}* — Squad empty.", parse_mode=ParseMode.MARKDOWN)
        return

    by_role: dict = {}
    for p in sq:
        by_role.setdefault(p.role, []).append(p)

    lines = [f"🏏 *{part_display(part)}* ({len(sq)} players)\n{'─'*28}"]
    for role, players in by_role.items():
        lines.append(f"\n{role_emoji(role)} *{role}s*")
        for i, p in enumerate(players, 1):
            lines.append(f"  {i}. {flag(p.nationality)} {p.name} — {fmt(p.sold_price or p.base_price,aid)}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not live.active:
        await update.message.reply_text("No auction currently running.")
        return
    aid = live.auction_id
    p   = live.current_player

    if not p:
        await update.message.reply_text(
            f"*{live.auction_name}*\nWaiting for next player.\n"
            f"Sold: {live.sold_count} | Unsold: {live.unsold_count} | Queue: {len(live.player_queue)}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    remaining = max(0, int(live.timer_ends_at - _time.time())) if live.timer_ends_at else None
    rtm_note  = ""
    if live.rtm_state == RTM_OFFERED:
        rtm_note = f"\n🎴 RTM offered to {live.rtm_team_name}"
    elif live.rtm_state == RTM_ACCEPTED:
        rtm_note = f"\n🎴 RTM accepted — waiting for {live.rtm_original_bidder_name} to counter"
    elif live.rtm_state == RTM_COUNTER:
        rtm_note = f"\n🎴 Counter bid — waiting for {live.rtm_team_name} to accept/decline"

    await update.message.reply_text(
        f"📊 *Auction Status*\n{'─'*28}\n"
        f"{bid_msg(p, live.current_bid, live.highest_bidder_name, remaining)}"
        f"{rtm_note}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_bid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not context.args:
        await update.message.reply_text("Usage: /bid <amount>  e.g. /bid 2cr")
        return
    bid_l = parse_price(context.args[0])
    if bid_l is None:
        await update.message.reply_text("Invalid amount.")
        return
    await process_bid(update, context, update.effective_user.id, bid_l)


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    aid = live.auction_id
    if not aid:
        await update.message.reply_text("No auction session.")
        return
    parts = sorted(db.get_all_participants(aid), key=lambda p: p.total_spent, reverse=True)
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏆 *{live.auction_name} — Leaderboard*\n{'─'*28}"]
    for i, p in enumerate(parts, 1):
        m  = medals[i-1] if i <= 3 else f"#{i}"
        sq = len(p.squad)
        lines.append(f"{m} *{part_display(p)}*\n  Spent: {fmt(p.total_spent,aid)} | {sq} players")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── AUCTION HISTORY ───────────────────────────────────────

async def cmd_auction_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await _register_user(user)
    rows = db.get_history_list(user.id)
    if not rows:
        await update.message.reply_text("No auction history found.")
        return

    lines = ["📜 *Your Auction History* (last 10)\n"]
    buttons = []
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['auction_name']}  —  {str(r['completed_at'])[:10]}")
        buttons.append(InlineKeyboardButton(str(i), callback_data=f"hist_{r['history_id']}"))

    # Group buttons in rows of 5
    btn_rows = [buttons[j:j+5] for j in range(0, len(buttons), 5)]
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(btn_rows),
    )


# ── ADMIN: MUTE / UNMUTE ──────────────────────────────────

async def cmd_mute_team(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args or not live.auction_id:
        await update.message.reply_text("Usage: /mute_team @user")
        return
    uid = db.resolve_user(context.args[0])
    if not uid:
        await update.message.reply_text("User not found.")
        return
    db.set_muted(live.auction_id, uid, True)
    part = db.get_participant(live.auction_id, uid)
    name = part_display(part) if part else str(uid)
    await update.message.reply_text(f"🔇 *{name}* has been muted. Bids won't be counted.",
                                    parse_mode=ParseMode.MARKDOWN)


async def cmd_unmute_team(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args or not live.auction_id:
        await update.message.reply_text("Usage: /unmute_team @user")
        return
    uid = db.resolve_user(context.args[0])
    if not uid:
        await update.message.reply_text("User not found.")
        return
    db.set_muted(live.auction_id, uid, False)
    part = db.get_participant(live.auction_id, uid)
    name = part_display(part) if part else str(uid)
    await update.message.reply_text(f"🔊 *{name}* unmuted. Can bid again.",
                                    parse_mode=ParseMode.MARKDOWN)


# ── ADMIN: TEAMUP ─────────────────────────────────────────

async def cmd_teamup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if len(context.args) < 2 or not live.auction_id:
        await update.message.reply_text("Usage: /teamup @user1 @user2")
        return
    uid1 = db.resolve_user(context.args[0])
    uid2 = db.resolve_user(context.args[1])
    if not uid1 or not uid2:
        await update.message.reply_text("Could not resolve one or both users.")
        return
    p1 = db.get_participant(live.auction_id, uid1)
    if not p1:
        await update.message.reply_text(f"{context.args[0]} is not in this auction.")
        return
    db.link_teams(live.auction_id, uid1, uid2)
    # Also update live state
    live.team_links[uid2] = uid1
    await update.message.reply_text(
        f"🤝 *TeamUp set!*\n{context.args[1]} can now bid on behalf of *{part_display(p1)}*.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── ADMIN: PASS ───────────────────────────────────────────

async def cmd_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.current_player:
        await update.message.reply_text("No player up for auction.")
        return
    if live.timer_task and not live.timer_task.done():
        live.timer_task.cancel()
    p = live.current_player
    db.update_player(p.player_id, "unsold")
    live.unsold_count   += 1
    live.current_player  = None
    await update.message.reply_text(
        f"⏭ *{p.name}* passed (unsold).", parse_mode=ParseMode.MARKDOWN,
        reply_markup=reauction_keyboard(),
    )
    live.last_sold_player_id   = p.player_id
    live.last_sold_player_name = p.name
    live.last_sold_buyer_id    = None
    await _try_auto_next(context)


# ── ADMIN: AUCTION CONTROL ────────────────────────────────

async def cmd_start_auction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if live.active:
        await update.message.reply_text("Auction already running!")
        return
    if not live.auction_id:
        await update.message.reply_text("Create an auction first with /create\\_auction.",
                                        parse_mode=ParseMode.MARKDOWN)
        return

    players = db.get_available_players(live.auction_id)
    if not players:
        await update.message.reply_text("No players added yet.")
        return

    if not live.player_queue:
        live.player_queue = players

    live.active       = True
    live.paused       = False
    live.sold_count   = 0
    live.unsold_count = 0
    live.set_number   = 1
    live.chat_id      = update.effective_chat.id
    db.update_auction_status(live.auction_id, "active")

    auction_row = db.get_auction(live.auction_id)
    await update.message.reply_text(
        f"🏏 *{live.auction_name}* — STARTED!\n{'─'*28}\n"
        f"{len(live.player_queue)} players in queue\n"
        f"Purse per team: {fmt(auction_row['purse'], live.auction_id)}\n"
        f"Squad limit: {auction_row['min_players']}–{auction_row['max_players']}\n\n"
        f"Use /next to bring up the first player!",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.active:
        await update.message.reply_text("Start the auction first with /startauction.")
        return
    await _do_next(context, update.effective_chat.id)


async def cmd_sold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.current_player:
        await update.message.reply_text("No player up for auction.")
        return
    if not live.highest_bidder_id:
        await update.message.reply_text("No bids. Use /pass to mark unsold.")
        return
    if live.timer_task and not live.timer_task.done():
        live.timer_task.cancel()
    await _finalize_sale(context)


async def cmd_force_sold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.current_player or not live.highest_bidder_id:
        await update.message.reply_text("No active bid.")
        return
    if live.timer_task and not live.timer_task.done():
        live.timer_task.cancel()
    live.rtm_state = RTM_NONE
    await _finalize_sale(context)


async def cmd_pause_auction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    live.paused = True
    await update.message.reply_text("⏸ Auction PAUSED. Use /resumeauction to continue.")


async def cmd_resume_auction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    live.paused = False
    await update.message.reply_text("▶️ Auction RESUMED!")


async def cmd_end_auction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.active:
        await update.message.reply_text("No active auction.")
        return
    if live.timer_task and not live.timer_task.done():
        live.timer_task.cancel()

    live.active         = False
    live.current_player = None
    aid = live.auction_id
    db.update_auction_status(aid, "completed")

    parts = sorted(db.get_all_participants(aid), key=lambda p: p.total_spent, reverse=True)

    # Build summary for history
    summary = {
        "name": live.auction_name,
        "sold": live.sold_count,
        "unsold": live.unsold_count,
        "teams": [],
    }
    lines = [
        f"🏆 *{live.auction_name} — FINAL SUMMARY*\n{'─'*28}\n"
        f"✅ Sold: {live.sold_count}  ❌ Unsold: {live.unsold_count}\n"
    ]
    medals = ["🥇","🥈","🥉"]
    for i, p in enumerate(parts, 1):
        sq = [db.get_player(pid) for pid in p.squad]
        sq = [x for x in sq if x]
        ov = sum(1 for x in sq if x.nationality == "Overseas")
        m  = medals[i-1] if i <= 3 else f"#{i}"
        lines.append(
            f"{m} *{part_display(p)}*\n"
            f"  Spent: {fmt(p.total_spent,aid)}  |  Left: {fmt(p.purse,aid)}\n"
            f"  Squad: {len(sq)}  |  Overseas: {ov}"
        )
        team_sq = [{"name":x.name,"role":x.role,"price":x.sold_price,"nat":x.nationality}
                   for x in sq]
        summary["teams"].append({
            "user_id": p.user_id,
            "team": part_display(p),
            "spent": p.total_spent,
            "purse": p.purse,
            "squad": team_sq,
        })

    db.save_history(aid, summary)
    await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_auto_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /autosell <seconds|off>")
        return
    val = context.args[0].lower()
    if val == "off":
        live.auto_sell_secs = None
        await update.message.reply_text("Auto-sell disabled.")
    else:
        try:
            live.auto_sell_secs = int(val)
            await update.message.reply_text(f"Auto-sell set to *{val}s*.", parse_mode=ParseMode.MARKDOWN)
        except ValueError:
            await update.message.reply_text("Invalid.")


async def cmd_auto_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /autonext <enable|disable|secs>")
        return
    val = context.args[0].lower()
    if val == "enable":
        live.auto_next_on   = True
        live.auto_next_secs = live.auto_next_secs or 5
        await update.message.reply_text(f"Auto-next enabled ({live.auto_next_secs}s).")
    elif val == "disable":
        live.auto_next_on = False
        await update.message.reply_text("Auto-next disabled.")
    else:
        try:
            live.auto_next_on   = True
            live.auto_next_secs = int(val)
            await update.message.reply_text(f"Auto-next every *{val}s*.", parse_mode=ParseMode.MARKDOWN)
        except ValueError:
            await update.message.reply_text("Invalid.")


# ── ADMIN: PURSE / SQUAD MANAGEMENT ──────────────────────

async def cmd_set_purse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if len(context.args) < 2 or not live.auction_id:
        await update.message.reply_text("Usage: /setpurse @user <amount>")
        return
    uid, amt_str = db.resolve_user(context.args[0]), context.args[-1]
    amt = parse_price(amt_str)
    if not uid or not amt:
        await update.message.reply_text("Invalid user or amount.")
        return
    db.set_purse(live.auction_id, uid, amt)
    p = db.get_participant(live.auction_id, uid)
    await update.message.reply_text(
        f"Set *{part_display(p)}* purse to *{fmt(amt,live.auction_id)}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_add_purse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if len(context.args) < 2 or not live.auction_id:
        await update.message.reply_text("Usage: /addpurse @user <amount>")
        return
    uid, amt = db.resolve_user(context.args[0]), parse_price(context.args[-1])
    if not uid or not amt:
        await update.message.reply_text("Invalid.")
        return
    db.add_purse(live.auction_id, uid, amt)
    p = db.get_participant(live.auction_id, uid)
    await update.message.reply_text(
        f"Added *{fmt(amt,live.auction_id)}* to *{part_display(p)}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_deduct_purse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if len(context.args) < 2 or not live.auction_id:
        await update.message.reply_text("Usage: /deductpurse @user <amount>")
        return
    uid, amt = db.resolve_user(context.args[0]), parse_price(context.args[-1])
    if not uid or not amt:
        await update.message.reply_text("Invalid.")
        return
    db.deduct_purse(live.auction_id, uid, amt)
    p = db.get_participant(live.auction_id, uid)
    await update.message.reply_text(
        f"Deducted *{fmt(amt,live.auction_id)}* from *{part_display(p)}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_add_to_squad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if len(context.args) < 2 or not live.auction_id:
        await update.message.reply_text("Usage: /addtosquad @user Player1,Player2")
        return
    uid   = db.resolve_user(context.args[0])
    names = [n.strip() for n in " ".join(context.args[1:]).split(",") if n.strip()]
    if not uid:
        await update.message.reply_text("User not found.")
        return
    for name in names:
        pid = db.add_player(live.auction_id, name, 0, "Batsman", "Indian", "", "C")
        db.update_player(pid, "sold", uid, 0)
        db.add_to_squad(live.auction_id, uid, pid)
    p = db.get_participant(live.auction_id, uid)
    await update.message.reply_text(
        f"Added {len(names)} player(s) to *{part_display(p)}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_remove_from_squad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if len(context.args) < 2 or not live.auction_id:
        await update.message.reply_text("Usage: /removefromsquad @user 1,2")
        return
    uid = db.resolve_user(context.args[0])
    if not uid:
        await update.message.reply_text("User not found.")
        return
    try:
        positions = [int(x.strip()) for x in " ".join(context.args[1:]).split(",")]
    except ValueError:
        await update.message.reply_text("Invalid positions.")
        return
    part = db.get_participant(live.auction_id, uid)
    if not part:
        await update.message.reply_text("Participant not found.")
        return
    sq  = part.squad[:]
    pids_to_remove = []
    for pos in sorted(positions, reverse=True):
        idx = pos - 1
        if 0 <= idx < len(sq):
            pids_to_remove.append(sq[idx])
    for pid in pids_to_remove:
        db.remove_from_squad(live.auction_id, uid, pid)
        db.update_player(pid, "available")
    await update.message.reply_text(
        f"Removed {len(pids_to_remove)} player(s) from *{part_display(part)}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_clear_squad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.auction_id:
        await update.message.reply_text("No active auction.")
        return
    uid = db.resolve_user(context.args[0]) if context.args else update.effective_user.id
    db.clear_squad(live.auction_id, uid)
    p = db.get_participant(live.auction_id, uid)
    await update.message.reply_text(f"Cleared squad for *{part_display(p)}*",
                                    parse_mode=ParseMode.MARKDOWN)


async def cmd_swap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if len(context.args) < 2 or not live.auction_id:
        await update.message.reply_text("Usage: /swap @user1 @user2")
        return
    uid1 = db.resolve_user(context.args[0])
    uid2 = db.resolve_user(context.args[1])
    if not uid1 or not uid2:
        await update.message.reply_text("Invalid users.")
        return
    ok = db.swap_participants(live.auction_id, uid1, uid2)
    if not ok:
        await update.message.reply_text("One or both users not in this auction.")
        return
    p1 = db.get_participant(live.auction_id, uid1)
    p2 = db.get_participant(live.auction_id, uid2)
    await update.message.reply_text(
        f"Swapped purse & squad between *{part_display(p1)}* and *{part_display(p2)}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_set_currency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id) or not live.auction_id:
        await update.message.reply_text("Admin only / no auction.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /setcurrency <symbol>")
        return
    db.cx.execute("UPDATE auctions SET currency=? WHERE auction_id=?",
                  (context.args[0], live.auction_id))
    db.cx.commit()
    await update.message.reply_text(f"Currency set to: *{context.args[0]}*",
                                    parse_mode=ParseMode.MARKDOWN)


# ── QUEUE MANAGEMENT ─────────────────────────────────────

async def cmd_add_to_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args or not live.auction_id:
        await update.message.reply_text("Usage: /addtoqueue Player1,Player2")
        return
    names = [n.strip() for n in " ".join(context.args).split(",") if n.strip()]
    for name in names:
        pid = db.add_player(live.auction_id, name, 20, "Batsman", "Indian", "", "C")
        p   = db.get_player(pid)
        if p:
            live.player_queue.append(p)
    await update.message.reply_text(f"Added {len(names)} to queue. Total: {len(live.player_queue)}")


async def cmd_atq_unsolds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.auction_id:
        await update.message.reply_text("No auction.")
        return
    unsold = db.get_unsold_players(live.auction_id)
    live.player_queue.extend(unsold)
    await update.message.reply_text(
        f"Added {len(unsold)} unsold players. Queue: {len(live.player_queue)}"
    )


async def cmd_remove_from_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /removefromqueue 1,3")
        return
    try:
        positions = sorted([int(x.strip()) for x in " ".join(context.args).split(",")], reverse=True)
    except ValueError:
        await update.message.reply_text("Invalid positions.")
        return
    removed = []
    for pos in positions:
        idx = pos - 1
        if 0 <= idx < len(live.player_queue):
            removed.append(live.player_queue.pop(idx).name)
    await update.message.reply_text(f"Removed: {', '.join(removed)}\nQueue: {len(live.player_queue)}")


async def cmd_shuffle_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    random.shuffle(live.player_queue)
    await update.message.reply_text(f"Queue shuffled! {len(live.player_queue)} players.")


async def cmd_swap_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
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
    q = live.player_queue
    if not (0 <= p1 < len(q) and 0 <= p2 < len(q)):
        await update.message.reply_text("Positions out of range.")
        return
    q[p1], q[p2] = q[p2], q[p1]
    await update.message.reply_text(f"Swapped positions {p1+1} and {p2+1}.")


async def cmd_clear_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    live.player_queue.clear()
    await update.message.reply_text("Queue cleared.")


async def cmd_view_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.player_queue:
        await update.message.reply_text("Queue is empty.")
        return
    page = int(context.args[0]) if context.args else 1
    per  = 15
    start= (page - 1) * per
    chunk= live.player_queue[start:start + per]
    total_pages = max(1, (len(live.player_queue) + per - 1) // per)
    lines = [f"Queue (Page {page}/{total_pages}, {len(live.player_queue)} total)"]
    for i, p in enumerate(chunk, start + 1):
        lines.append(
            f"{i}. {flag(p.nationality)} {p.name} — {role_emoji(p.role)} | {fmt(p.base_price,p.auction_id)}"
        )
    if page < total_pages:
        lines.append(f"\nUse /queue {page+1} for next page.")
    await update.message.reply_text("\n".join(lines))


# ── PLAYER MANAGEMENT ────────────────────────────────────

async def cmd_add_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.auction_id:
        await update.message.reply_text("Create an auction first.")
        return
    if len(context.args) < 5:
        await update.message.reply_text(
            "Usage: /addplayer <n> <Role> <PrevTeam> <Nat> <Price> [Tier]\n"
            "e.g. /addplayer ViratKohli Bat RCB Indian 2cr Marquee"
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
    pid = db.add_player(live.auction_id, name, price, role, nat, team, tier)
    if live.active:
        p = db.get_player(pid)
        if p:
            live.player_queue.append(p)
        note = " — Added to queue!"
    else:
        note = ""
    await update.message.reply_text(
        f"Added: *{flag(nat)} {name}* | {role} | {fmt(price,live.auction_id)} | {tier}{note}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_add_player_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if not db.is_global_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.auction_id:
        await update.message.reply_text("Create an auction first.")
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
        pid = db.add_player(
            live.auction_id, parts[0], price,
            normalize_role(parts[1]), normalize_nat(parts[3]), parts[2], tier
        )
        if live.active:
            p = db.get_player(pid)
            if p:
                live.player_queue.append(p)
        added.append(parts[0])

    msg = f"Added {len(added)} players!"
    if live.active and added:
        msg += f" ({len(added)} in queue)"
    if added:
        msg += "\n" + "\n".join(f"  {n}" for n in added[:20])
        if len(added) > 20:
            msg += f"\n  ...and {len(added)-20} more"
    if failed:
        msg += f"\n\nFailed:\n" + "\n".join(failed[:5])
    await update.message.reply_text(msg)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if update.effective_user.id != Config.SUPER_ADMIN_ID:
        await update.message.reply_text("Super Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /admin @user  or  /admin <user_id>")
        return
    uid = db.resolve_user(context.args[0])
    if not uid:
        await update.message.reply_text("User not found. They must /start first.")
        return
    db.set_global_admin(uid, True)
    u = db.get_user(uid)
    name = f"@{u['username']}" if u and u["username"] else str(uid)
    await update.message.reply_text(f"✅ *{name}* is now an admin!", parse_mode=ParseMode.MARKDOWN)


async def cmd_clear_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _register_user(update.effective_user)
    if update.effective_user.id != Config.SUPER_ADMIN_ID:
        await update.message.reply_text("Super Admin only.")
        return
    if not live.auction_id:
        await update.message.reply_text("No auction.")
        return
    db.clear_players(live.auction_id)
    await update.message.reply_text("All players cleared.")


# ──────────────────────────────────────────────────────────
# CALLBACK HANDLER
# ──────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data
    uid   = query.from_user.id
    await _register_user(query.from_user)

    # ── JOIN AUCTION ─────────────────────────
    if data.startswith("join_"):
        aid = int(data.split("_")[1])
        auction_row = db.get_auction(aid)
        if not auction_row:
            await query.answer("Auction not found.", show_alert=True)
            return
        if auction_row["status"] != "registration":
            await query.answer("Registration is closed.", show_alert=True)
            return

        count = db.count_participants(aid)
        max_t = auction_row["max_teams"]

        if count >= max_t:
            await query.answer("Auction is full!", show_alert=True)
            return

        existing = db.get_participant(aid, uid)
        if existing:
            await query.answer(f"Already joined as {existing.team_name}!", show_alert=True)
            return

        u     = db.get_user(uid)
        uname = u["username"] if u and u["username"] else ""
        tname = u["first_name"] if u and u["first_name"] else f"Team{uid}"
        db.join_auction(aid, uid, uname, tname, auction_row["purse"])
        new_count = db.count_participants(aid)
        left      = max_t - new_count

        # Update the registration message button
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        f"🏏 Join Auction ({new_count}/{max_t})",
                        callback_data=f"join_{aid}"
                    )
                ]]),
            )
        except Exception:
            pass

        display_uname = f"@{uname}" if uname else tname
        await query.answer(f"✅ Joined! Purse: {fmt(auction_row['purse'], aid)}", show_alert=True)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                f"🏏 Team *#{new_count}* — *{display_uname}* joined *{auction_row['name']}*!\n"
                f"[{left} spot{'s' if left != 1 else ''} left]"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── MY PURSE ─────────────────────────────
    if data == "my_purse":
        aid  = live.auction_id
        euid = effective_user_id(uid, aid) if aid else uid
        part = db.get_participant(aid, euid) if aid else None
        if not part:
            await query.answer("Not registered in this auction.", show_alert=True)
            return
        sq = len(part.squad)
        auction_row = db.get_auction(aid)
        await query.answer(
            f"{part_display(part)}\n"
            f"Purse: {fmt(part.purse,aid)}\n"
            f"Squad: {sq}/{auction_row['max_players']}",
            show_alert=True,
        )
        return

    # ── BID ───────────────────────────────────
    if data.startswith("bid_"):
        try:
            bid_l = int(data.split("_")[1])
        except (ValueError, IndexError):
            await query.answer("Invalid bid.", show_alert=True)
            return
        await process_bid(update, context, uid, bid_l)
        return

    # ── RTM ACCEPT ───────────────────────────
    if data == "rtm_accept":
        if live.rtm_state != RTM_OFFERED:
            await query.answer("RTM phase already ended.", show_alert=True)
            return
        euid = effective_user_id(uid, live.auction_id)
        if live.rtm_team_id != euid:
            await query.answer("RTM is not for your team!", show_alert=True)
            return
        if live.timer_task and not live.timer_task.done():
            live.timer_task.cancel()

        live.rtm_state = RTM_ACCEPTED
        # Deduct an RTM card
        db.cx.execute(
            "UPDATE participants SET rtm_cards=MAX(0,rtm_cards-1)"
            " WHERE auction_id=? AND user_id=?",
            (live.auction_id, live.rtm_team_id),
        )
        db.cx.commit()

        try:
            await query.edit_message_text(
                f"✅ *{live.rtm_team_name}* accepted RTM for *{live.current_player.name}*!",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

        p = live.current_player
        await context.bot.send_message(
            chat_id=live.chat_id,
            text=(
                f"🎴 *{live.rtm_team_name}* uses RTM on *{p.name}*!\n"
                f"{'─'*28}\n"
                f"Current bid: *{fmt(live.rtm_original_bid, p.auction_id)}* by *{live.rtm_original_bidder_name}*\n\n"
                f"*{live.rtm_original_bidder_name}* — you can now raise your bid!\n"
                f"Use /bid <higher amount> within *{Config.RTM_TIMER}s*.\n"
                f"If no counter, {live.rtm_team_name} gets the player."
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        # Start RTM timer again for counter window
        live.timer_task = asyncio.create_task(_rtm_timer(context))
        await query.answer("RTM Accepted!")
        return

    # ── RTM DECLINE ──────────────────────────
    if data == "rtm_decline":
        if live.rtm_state != RTM_OFFERED:
            await query.answer("RTM phase already ended.", show_alert=True)
            return
        euid = effective_user_id(uid, live.auction_id)
        if live.rtm_team_id != euid:
            await query.answer("RTM is not for your team!", show_alert=True)
            return
        if live.timer_task and not live.timer_task.done():
            live.timer_task.cancel()
        live.rtm_state = RTM_NONE
        try:
            await query.edit_message_text(
                f"❌ *{live.rtm_team_name}* declined RTM. Player goes to *{live.highest_bidder_name}*.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        await query.answer("RTM Declined.")
        await _finalize_sale(context, rtm_used=False)
        return

    # ── RTM COUNTER: YES ─────────────────────
    if data == "rtm_counter_yes":
        if live.rtm_state != RTM_COUNTER:
            await query.answer("No active counter bid.", show_alert=True)
            return
        euid = effective_user_id(uid, live.auction_id)
        if live.rtm_team_id != euid:
            await query.answer("Only the RTM team can accept/decline!", show_alert=True)
            return
        # RTM team pays the counter bid
        live.current_bid        = live.rtm_counter_bid
        live.highest_bidder_id  = live.rtm_team_id
        live.highest_bidder_name= live.rtm_team_name
        live.rtm_state          = RTM_NONE
        try:
            await query.edit_message_text(
                f"✅ *{live.rtm_team_name}* accepts *{fmt(live.rtm_counter_bid,live.auction_id)}*!",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        await query.answer("Accepted!")
        await _finalize_sale(context, rtm_used=True)
        return

    # ── RTM COUNTER: NO ──────────────────────
    if data == "rtm_counter_no":
        if live.rtm_state != RTM_COUNTER:
            await query.answer("No active counter bid.", show_alert=True)
            return
        euid = effective_user_id(uid, live.auction_id)
        if live.rtm_team_id != euid:
            await query.answer("Only the RTM team can accept/decline!", show_alert=True)
            return
        # Sell to original bidder at original bid
        live.current_bid        = live.rtm_original_bid
        live.highest_bidder_id  = live.rtm_original_bidder_id
        live.highest_bidder_name= live.rtm_original_bidder_name
        live.rtm_state          = RTM_NONE
        try:
            await query.edit_message_text(
                f"❌ *{live.rtm_team_name}* declines. "
                f"Player goes to *{live.rtm_original_bidder_name}* for "
                f"*{fmt(live.rtm_original_bid,live.auction_id)}*.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        await query.answer("Declined.")
        await _finalize_sale(context, rtm_used=False)
        return

    # ── REAUCTION PROMPT ─────────────────────
    if data == "reauction_prompt":
        if not db.is_global_admin(uid):
            await query.answer("Admin only.", show_alert=True)
            return
        if not live.last_sold_player_id:
            await query.answer("No recent player to re-auction.", show_alert=True)
            return
        buyer_info = (
            f"Bought by *{live.last_sold_buyer_name}* for *{fmt(live.last_sold_price,live.auction_id)}*"
            if live.last_sold_buyer_id
            else "was UNSOLD"
        )
        await query.answer()
        await context.bot.send_message(
            chat_id=live.chat_id,
            text=(
                f"🔄 *ReAuction?*\n{'─'*28}\n"
                f"Player: *{live.last_sold_player_name}*\n"
                f"{buyer_info}\n\n"
                f"Do you want to re-auction this player?"
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reauction_confirm_keyboard(),
        )
        return

    # ── REAUCTION YES ────────────────────────
    if data == "reauction_yes":
        if not db.is_global_admin(uid):
            await query.answer("Admin only.", show_alert=True)
            return
        pid = live.last_sold_player_id
        if not pid:
            await query.answer("No player to re-auction.", show_alert=True)
            return

        # Refund buyer if player was sold
        if live.last_sold_buyer_id and live.last_sold_price > 0:
            db.refund_part_purse(live.auction_id, live.last_sold_buyer_id, live.last_sold_price)
            db.remove_from_squad(live.auction_id, live.last_sold_buyer_id, pid)

        # Reset player to available and put at front of queue
        db.update_player(pid, "available", None, None)
        fresh = db.get_player(pid)
        if fresh:
            live.player_queue.insert(0, fresh)

        live.last_sold_player_id = None

        try:
            await query.edit_message_text(
                f"✅ *{live.last_sold_player_name}* added back to front of queue!\n"
                f"{'Refund issued to ' + live.last_sold_buyer_name if live.last_sold_buyer_id else ''}",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        await query.answer("Re-auction queued!")
        return

    # ── REAUCTION NO ─────────────────────────
    if data == "reauction_no":
        if not db.is_global_admin(uid):
            await query.answer("Admin only.", show_alert=True)
            return
        live.last_sold_player_id = None
        try:
            await query.edit_message_text("Moving to next player...")
        except Exception:
            pass
        await query.answer("Skipped.")
        await _do_next(context, live.chat_id)
        return

    # ── HISTORY DETAIL ───────────────────────
    if data.startswith("hist_"):
        history_id = int(data.split("_")[1])
        row = db.get_history_entry(history_id)
        if not row:
            await query.answer("History not found.", show_alert=True)
            return
        summary = json.loads(row["summary"])
        lines = [
            f"📜 *{summary['name']}*\n{'─'*28}\n"
            f"✅ Sold: {summary['sold']}  ❌ Unsold: {summary['unsold']}\n"
        ]
        for team in summary.get("teams", []):
            lines.append(f"\n🏏 *{team['team']}*")
            lines.append(
                f"  Spent: {team['spent']} L  |  Purse left: {team['purse']} L\n"
                f"  Squad ({len(team['squad'])} players):"
            )
            for i, p in enumerate(team["squad"], 1):
                lines.append(
                    f"  {i}. {flag(p.get('nat','Indian'))} {p['name']} — "
                    f"{p.get('role','?')} | {p.get('price',0)} L"
                )
        await query.answer()
        # Send as new message (history can be long)
        try:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="\n".join(lines),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="\n".join(lines[:30]),  # Truncate if too long
            )
        return

    await query.answer()


# ──────────────────────────────────────────────────────────
# DOT COMMAND ROUTER
# ──────────────────────────────────────────────────────────
DOT_MAP = {
    "setteamname": cmd_set_team_name, "stn": cmd_set_team_name,
    "purse": cmd_purse, "bal": cmd_purse, "balance": cmd_purse,
    "squad": cmd_squad,
    "bid": cmd_bid,
    "status": cmd_status,
    "setpurse": cmd_set_purse, "setbal": cmd_set_purse,
    "addpurse": cmd_add_purse, "addbal": cmd_add_purse,
    "deductpurse": cmd_deduct_purse, "deductbal": cmd_deduct_purse,
    "addtosquad": cmd_add_to_squad, "ats": cmd_add_to_squad,
    "removefromsquad": cmd_remove_from_squad, "rfs": cmd_remove_from_squad,
    "clearsquad": cmd_clear_squad,
    "swap": cmd_swap,
    "addtoqueue": cmd_add_to_queue, "atq": cmd_add_to_queue,
    "addtoqueueunsolds": cmd_atq_unsolds, "atqu": cmd_atq_unsolds,
    "removefromqueue": cmd_remove_from_queue, "rfq": cmd_remove_from_queue,
    "shufflequeue": cmd_shuffle_queue, "sq": cmd_shuffle_queue,
    "swapqueue": cmd_swap_queue,
    "clearqueue": cmd_clear_queue,
    "queue": cmd_view_queue, "q": cmd_view_queue,
    "startauction": cmd_start_auction,
    "next": cmd_next,
    "pass": cmd_pass,
    "sold": cmd_sold,
    "forcesold": cmd_force_sold,
    "pauseauction": cmd_pause_auction,
    "resumeauction": cmd_resume_auction,
    "endauction": cmd_end_auction, "endsauction": cmd_end_auction,
    "autosell": cmd_auto_sell,
    "autonext": cmd_auto_next,
    "leaderboard": cmd_leaderboard,
    "help": cmd_help,
    "mute": cmd_mute_team,
    "unmute": cmd_unmute_team,
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


# ──────────────────────────────────────────────────────────
# FLASK
# ──────────────────────────────────────────────────────────
@flask_app.route("/")
def root():
    return "IPL Auction Bot v3.0 is running!", 200

@flask_app.route("/health")
def health():
    return {"status": "ok", "auction": live.auction_name, "active": live.active}, 200


# ──────────────────────────────────────────────────────────
# APP SETUP
# ──────────────────────────────────────────────────────────
def build_app() -> Application:
    app = Application.builder().token(Config.BOT_TOKEN).build()

    app.add_handler(CommandHandler(["start", "registration"], cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler(["create_auction", "createauction"], cmd_create_auction))
    app.add_handler(CommandHandler("setauctionname", cmd_set_auction_name))
    app.add_handler(CommandHandler(["setteamname", "stn"], cmd_set_team_name))
    app.add_handler(CommandHandler(["purse", "bal", "balance"], cmd_purse))
    app.add_handler(CommandHandler("squad", cmd_squad))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("bid", cmd_bid))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler(["auctionhistory", "auction_history"], cmd_auction_history))

    # Admin: teams
    app.add_handler(CommandHandler(["mute_team", "muteteam"], cmd_mute_team))
    app.add_handler(CommandHandler(["unmute_team", "unmuteteam"], cmd_unmute_team))
    app.add_handler(CommandHandler("teamup", cmd_teamup))
    app.add_handler(CommandHandler(["setpurse", "setbal"], cmd_set_purse))
    app.add_handler(CommandHandler(["addpurse", "addbal"], cmd_add_purse))
    app.add_handler(CommandHandler(["deductpurse", "deductbal"], cmd_deduct_purse))
    app.add_handler(CommandHandler(["addtosquad", "ats"], cmd_add_to_squad))
    app.add_handler(CommandHandler(["removefromsquad", "rfs"], cmd_remove_from_squad))
    app.add_handler(CommandHandler("clearsquad", cmd_clear_squad))
    app.add_handler(CommandHandler("swap", cmd_swap))
    app.add_handler(CommandHandler("setcurrency", cmd_set_currency))

    # Admin: auction
    app.add_handler(CommandHandler("startauction", cmd_start_auction))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("pass", cmd_pass))
    app.add_handler(CommandHandler("sold", cmd_sold))
    app.add_handler(CommandHandler("forcesold", cmd_force_sold))
    app.add_handler(CommandHandler("pauseauction", cmd_pause_auction))
    app.add_handler(CommandHandler("resumeauction", cmd_resume_auction))
    app.add_handler(CommandHandler(["endauction", "endsauction"], cmd_end_auction))
    app.add_handler(CommandHandler("autosell", cmd_auto_sell))
    app.add_handler(CommandHandler("autonext", cmd_auto_next))

    # Admin: queue
    app.add_handler(CommandHandler(["addtoqueue", "atq"], cmd_add_to_queue))
    app.add_handler(CommandHandler(["addtoqueueunsolds", "atqu"], cmd_atq_unsolds))
    app.add_handler(CommandHandler(["removefromqueue", "rfq"], cmd_remove_from_queue))
    app.add_handler(CommandHandler(["shufflequeue", "sq"], cmd_shuffle_queue))
    app.add_handler(CommandHandler("swapqueue", cmd_swap_queue))
    app.add_handler(CommandHandler("clearqueue", cmd_clear_queue))
    app.add_handler(CommandHandler(["queue", "q"], cmd_view_queue))

    # Admin: players
    app.add_handler(CommandHandler("addplayer", cmd_add_player))
    app.add_handler(CommandHandler("add_player_list", cmd_add_player_list))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("clearplayers", cmd_clear_players))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^\."), dot_handler))

    return app


async def _setup_webhook(app: Application, url: str):
    await app.initialize()
    await app.bot.set_webhook(
        url=f"{url}/webhook",
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )
    await app.start()
    logger.info(f"Webhook: {url}/webhook")


def main():
    if not Config.BOT_TOKEN or Config.BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise ValueError("BOT_TOKEN not set!")
    if not Config.SUPER_ADMIN_ID:
        raise ValueError("SUPER_ADMIN_ID not set!")

    logger.info("Starting IPL Auction Bot v3.0...")
    ptb = build_app()

    if Config.WEBHOOK_URL:
        global _ptb_app
        _ptb_app = ptb
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        @flask_app.route("/webhook", methods=["POST"])
        def wh():
            from telegram import Update as _U
            d   = request.get_json(force=True)
            upd = _U.de_json(d, _ptb_app.bot)
            asyncio.run_coroutine_threadsafe(_ptb_app.process_update(upd), loop)
            return "ok", 200

        loop.run_until_complete(_setup_webhook(ptb, Config.WEBHOOK_URL))
        import threading
        threading.Thread(
            target=lambda: flask_app.run(host="0.0.0.0", port=Config.PORT, use_reloader=False),
            daemon=True,
        ).start()
        logger.info(f"Webhook mode, port {Config.PORT}")
        loop.run_forever()
    else:
        logger.info("Polling mode")
        ptb.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
