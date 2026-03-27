"""
IPL Cricket Auction Bot — v4.0
• Fresh independent auctions (no data bleed)
• /auctionowners, /unsoldplayers, /soldplayers (with jump links)
• /forceauction, /bulkplayer
• /mybidhistory
• /setrtm — admin-assigned RTM (not ipl_team based)
• ReAuction button stays active until next player
• Full RTM state machine
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

# ─────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────
class Config:
    BOT_TOKEN: str      = os.getenv("BOT_TOKEN", "YOUR_TOKEN_HERE")
    SUPER_ADMIN_ID: int = int(os.getenv("SUPER_ADMIN_ID", "0"))
    WEBHOOK_URL: str    = os.getenv("WEBHOOK_URL", "")
    PORT: int           = int(os.getenv("PORT", "8080"))
    DB_PATH: str        = os.getenv("DATABASE_PATH", "auction.db")
    BID_TIMER: int      = 30
    RTM_TIMER: int      = 20
    ANTI_SNIPE: int     = 10
    INCREMENT: int      = 10        # Default increment in Lakhs


# ─────────────────────────────────────────────────────────
# RTM STATES
# ─────────────────────────────────────────────────────────
RTM_NONE     = "none"
RTM_OFFERED  = "offered"    # Timer expired, RTM opportunity sent
RTM_ACTIVE   = "active"     # A team clicked Use RTM, waiting for orig bidder counter
RTM_COUNTER  = "counter"    # Orig bidder countered, RTM team must accept/decline


# ─────────────────────────────────────────────────────────
# LIVE STATE (in-memory, resets per auction run)
# ─────────────────────────────────────────────────────────
@dataclass
class LiveState:
    active: bool               = False
    paused: bool               = False
    auction_id: Optional[int]  = None
    auction_name: str          = ""
    chat_id: Optional[int]     = None

    # Current player
    current_player_id: Optional[int]  = None
    current_bid: int           = 0
    highest_bidder_id: Optional[int]  = None
    highest_bidder_name: str   = ""
    last_bid_msg_id: Optional[int]    = None

    # Timer
    timer_task: Optional[asyncio.Task] = None
    timer_ends_at: Optional[float]    = None
    auto_sell_secs: Optional[int]     = None
    auto_next_secs: Optional[int]     = None
    auto_next_on: bool         = False

    # Queue
    player_queue: list         = field(default_factory=list)
    set_number: int            = 1
    sold_count: int            = 0
    unsold_count: int          = 0

    # RTM state machine
    rtm_state: str             = RTM_NONE
    rtm_team_id: Optional[int] = None        # team using RTM
    rtm_team_name: str         = ""
    rtm_orig_bidder_id: Optional[int] = None
    rtm_orig_bidder_name: str  = ""
    rtm_orig_bid: int          = 0
    rtm_counter_bid: int       = 0
    rtm_msg_id: Optional[int]  = None
    rtm_offer_msg_id: Optional[int] = None  # message with "Use RTM" buttons after timer

    # ReAuction (no expiry — cleared only when next player starts)
    last_sold_pid: Optional[int]   = None
    last_sold_name: str        = ""
    last_sold_buyer_id: Optional[int] = None
    last_sold_buyer_name: str  = ""
    last_sold_price: int       = 0
    reauction_msg_id: Optional[int] = None

    # TeamUp links {linked_uid: primary_uid}
    team_links: dict           = field(default_factory=dict)


live = LiveState()
flask_app = Flask(__name__)
_ptb_app: Optional[Application] = None


# ─────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────
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
    def cx(self): return self._cx()

    def _init(self):
        c = sqlite3.connect(self.path)
        c.executescript("""
        CREATE TABLE IF NOT EXISTS global_users (
            user_id    INTEGER PRIMARY KEY,
            username   TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            is_admin   INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS auctions (
            auction_id  INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            max_teams   INTEGER NOT NULL,
            purse       INTEGER NOT NULL,
            min_players INTEGER DEFAULT 11,
            max_players INTEGER DEFAULT 25,
            currency    TEXT DEFAULT 'Rs.',
            status      TEXT DEFAULT 'registration',
            chat_id     INTEGER,
            reg_msg_id  INTEGER,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            rtm_team    TEXT DEFAULT '',
            UNIQUE(auction_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS team_co_owners (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id      INTEGER NOT NULL,
            primary_user_id INTEGER NOT NULL,
            linked_user_id  INTEGER NOT NULL,
            UNIQUE(auction_id, linked_user_id)
        );

        CREATE TABLE IF NOT EXISTS players (
            player_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id      INTEGER NOT NULL,
            name            TEXT NOT NULL,
            base_price      INTEGER DEFAULT 0,
            role            TEXT DEFAULT 'Batsman',
            nationality     TEXT DEFAULT 'Indian',
            ipl_team        TEXT DEFAULT '',
            tier            TEXT DEFAULT 'C',
            status          TEXT DEFAULT 'available',
            sold_to         INTEGER,
            sold_price      INTEGER,
            sold_msg_id     INTEGER,
            sold_chat_id    INTEGER
        );

        CREATE TABLE IF NOT EXISTS bid_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id  INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            player_id   INTEGER NOT NULL,
            player_name TEXT NOT NULL,
            bid_amount  INTEGER NOT NULL,
            won         INTEGER DEFAULT 0,
            ts          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS auction_snapshots (
            snap_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id  INTEGER NOT NULL,
            summary     TEXT NOT NULL,
            completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        c.commit()
        c.close()

    # ── GLOBAL USERS ────────────────────────────────────
    def upsert_user(self, uid: int, username: str, first_name: str):
        self.cx.execute(
            "INSERT INTO global_users(user_id,username,first_name) VALUES(?,?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,"
            " first_name=excluded.first_name",
            (uid, username or "", first_name or ""),
        )
        self.cx.commit()

    def get_user(self, uid: int):
        return self.cx.execute("SELECT * FROM global_users WHERE user_id=?", (uid,)).fetchone()

    def resolve_uid(self, arg: str) -> Optional[int]:
        """@username or raw int → user_id"""
        arg = arg.strip()
        if arg.startswith("@"):
            r = self.cx.execute(
                "SELECT user_id FROM global_users WHERE LOWER(username)=?",
                (arg[1:].lower(),),
            ).fetchone()
            return int(r["user_id"]) if r else None
        try:
            return int(arg)
        except ValueError:
            return None

    def is_admin(self, uid: int) -> bool:
        if uid == Config.SUPER_ADMIN_ID:
            return True
        r = self.cx.execute("SELECT is_admin FROM global_users WHERE user_id=?", (uid,)).fetchone()
        return bool(r and r["is_admin"])

    def set_admin(self, uid: int, val: bool):
        self.cx.execute(
            "INSERT INTO global_users(user_id,is_admin) VALUES(?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET is_admin=excluded.is_admin",
            (uid, 1 if val else 0),
        )
        self.cx.commit()

    def display(self, uid: int) -> str:
        r = self.get_user(uid)
        if not r:
            return str(uid)
        u = f" (@{r['username']})" if r["username"] else ""
        return f"{r['first_name']}{u}"

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

    def get_auction(self, aid: int):
        return self.cx.execute("SELECT * FROM auctions WHERE auction_id=?", (aid,)).fetchone()

    def set_auction_status(self, aid: int, status: str):
        self.cx.execute("UPDATE auctions SET status=? WHERE auction_id=?", (status, aid))
        self.cx.commit()

    def set_reg_msg(self, aid: int, msg_id: int):
        self.cx.execute("UPDATE auctions SET reg_msg_id=? WHERE auction_id=?", (msg_id, aid))
        self.cx.commit()

    def count_participants(self, aid: int) -> int:
        r = self.cx.execute(
            "SELECT COUNT(*) c FROM participants WHERE auction_id=?", (aid,)
        ).fetchone()
        return r["c"] if r else 0

    # ── PARTICIPANTS ─────────────────────────────────────
    def join(self, aid: int, uid: int, username: str, team_name: str, purse: int) -> bool:
        try:
            self.cx.execute(
                "INSERT OR IGNORE INTO participants"
                "(auction_id,user_id,username,team_name,purse) VALUES(?,?,?,?,?)",
                (aid, uid, username, team_name, purse),
            )
            self.cx.commit()
            return self.cx.execute("SELECT changes()").fetchone()[0] > 0
        except sqlite3.Error as e:
            logger.error(e)
            return False

    def get_part(self, aid: int, uid: int):
        return self.cx.execute(
            "SELECT * FROM participants WHERE auction_id=? AND user_id=?", (aid, uid)
        ).fetchone()

    def get_all_parts(self, aid: int) -> list:
        return self.cx.execute(
            "SELECT * FROM participants WHERE auction_id=?", (aid,)
        ).fetchall()

    def update_part(self, aid: int, uid: int, **kwargs):
        if not kwargs:
            return
        cols = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values()) + [aid, uid]
        self.cx.execute(
            f"UPDATE participants SET {cols} WHERE auction_id=? AND user_id=?", vals
        )
        self.cx.commit()

    def deduct_purse(self, aid: int, uid: int, amount: int):
        self.cx.execute(
            "UPDATE participants SET purse=purse-?, total_spent=total_spent+?"
            " WHERE auction_id=? AND user_id=?",
            (amount, amount, aid, uid),
        )
        self.cx.commit()

    def refund_purse(self, aid: int, uid: int, amount: int):
        self.cx.execute(
            "UPDATE participants SET purse=purse+?, total_spent=MAX(0,total_spent-?)"
            " WHERE auction_id=? AND user_id=?",
            (amount, amount, aid, uid),
        )
        self.cx.commit()

    def add_to_squad(self, aid: int, uid: int, pid: int):
        row = self.get_part(aid, uid)
        if row:
            sq = json.loads(row["squad"])
            if pid not in sq:
                sq.append(pid)
            self.cx.execute(
                "UPDATE participants SET squad=? WHERE auction_id=? AND user_id=?",
                (json.dumps(sq), aid, uid),
            )
            self.cx.commit()

    def remove_from_squad(self, aid: int, uid: int, pid: int):
        row = self.get_part(aid, uid)
        if row:
            sq = [p for p in json.loads(row["squad"]) if p != pid]
            self.cx.execute(
                "UPDATE participants SET squad=? WHERE auction_id=? AND user_id=?",
                (json.dumps(sq), aid, uid),
            )
            self.cx.commit()

    def set_muted(self, aid: int, uid: int, muted: bool):
        self.cx.execute(
            "UPDATE participants SET is_muted=? WHERE auction_id=? AND user_id=?",
            (1 if muted else 0, aid, uid),
        )
        self.cx.commit()

    def set_rtm(self, aid: int, uid: int, cards: int, team: str):
        self.cx.execute(
            "UPDATE participants SET rtm_cards=?, rtm_team=? WHERE auction_id=? AND user_id=?",
            (cards, team, aid, uid),
        )
        self.cx.commit()

    def swap_parts(self, aid: int, u1: int, u2: int) -> bool:
        r1 = self.get_part(aid, u1)
        r2 = self.get_part(aid, u2)
        if not r1 or not r2:
            return False
        self.cx.execute(
            "UPDATE participants SET purse=?,total_spent=?,squad=? WHERE auction_id=? AND user_id=?",
            (r2["purse"], r2["total_spent"], r2["squad"], aid, u1),
        )
        self.cx.execute(
            "UPDATE participants SET purse=?,total_spent=?,squad=? WHERE auction_id=? AND user_id=?",
            (r1["purse"], r1["total_spent"], r1["squad"], aid, u2),
        )
        self.cx.commit()
        return True

    # ── CO-OWNERS ────────────────────────────────────────
    def link_co_owner(self, aid: int, primary: int, linked: int):
        self.cx.execute(
            "INSERT OR REPLACE INTO team_co_owners(auction_id,primary_user_id,linked_user_id)"
            " VALUES(?,?,?)",
            (aid, primary, linked),
        )
        self.cx.commit()

    def get_primary(self, aid: int, linked: int) -> Optional[int]:
        r = self.cx.execute(
            "SELECT primary_user_id FROM team_co_owners WHERE auction_id=? AND linked_user_id=?",
            (aid, linked),
        ).fetchone()
        return r["primary_user_id"] if r else None

    def get_co_owners(self, aid: int, primary: int) -> list:
        return self.cx.execute(
            "SELECT linked_user_id FROM team_co_owners WHERE auction_id=? AND primary_user_id=?",
            (aid, primary),
        ).fetchall()

    # ── PLAYERS ─────────────────────────────────────────
    def add_player(self, aid: int, name: str, base_price: int,
                   role: str = "Batsman", nat: str = "Indian",
                   ipl_team: str = "", tier: str = "C") -> int:
        cur = self.cx.execute(
            "INSERT INTO players(auction_id,name,base_price,role,nationality,ipl_team,tier)"
            " VALUES(?,?,?,?,?,?,?)",
            (aid, name, base_price, role, nat, ipl_team, tier),
        )
        self.cx.commit()
        return cur.lastrowid

    def get_player(self, pid: int):
        return self.cx.execute("SELECT * FROM players WHERE player_id=?", (pid,)).fetchone()

    def get_player_by_name(self, aid: int, name: str):
        return self.cx.execute(
            "SELECT * FROM players WHERE auction_id=? AND LOWER(name) LIKE ?",
            (aid, f"%{name.lower()}%"),
        ).fetchone()

    def get_available(self, aid: int) -> list:
        return self.cx.execute(
            "SELECT * FROM players WHERE auction_id=? AND status='available' ORDER BY player_id",
            (aid,),
        ).fetchall()

    def get_unsold(self, aid: int) -> list:
        return self.cx.execute(
            "SELECT * FROM players WHERE auction_id=? AND status='unsold'", (aid,)
        ).fetchall()

    def get_sold(self, aid: int) -> list:
        return self.cx.execute(
            "SELECT * FROM players WHERE auction_id=? AND status='sold' ORDER BY player_id",
            (aid,),
        ).fetchall()

    def set_player_status(self, pid: int, status: str,
                          sold_to: Optional[int] = None,
                          sold_price: Optional[int] = None,
                          sold_msg_id: Optional[int] = None,
                          sold_chat_id: Optional[int] = None):
        self.cx.execute(
            "UPDATE players SET status=?,sold_to=?,sold_price=?,"
            "sold_msg_id=?,sold_chat_id=? WHERE player_id=?",
            (status, sold_to, sold_price, sold_msg_id, sold_chat_id, pid),
        )
        self.cx.commit()

    def restore_player(self, pid: int):
        self.cx.execute(
            "UPDATE players SET status='available',sold_to=NULL,sold_price=NULL,"
            "sold_msg_id=NULL,sold_chat_id=NULL WHERE player_id=?",
            (pid,),
        )
        self.cx.commit()

    def clear_players(self, aid: int):
        self.cx.execute("DELETE FROM players WHERE auction_id=?", (aid,))
        self.cx.commit()

    # ── BID HISTORY ─────────────────────────────────────
    def record_bid(self, aid: int, uid: int, pid: int, name: str,
                   amount: int, won: bool = False):
        self.cx.execute(
            "INSERT INTO bid_history(auction_id,user_id,player_id,player_name,bid_amount,won)"
            " VALUES(?,?,?,?,?,?)",
            (aid, uid, pid, name, amount, 1 if won else 0),
        )
        self.cx.commit()

    def get_my_bids(self, uid: int, aid: int) -> list:
        return self.cx.execute(
            "SELECT * FROM bid_history WHERE user_id=? AND auction_id=?"
            " ORDER BY id DESC LIMIT 50",
            (uid, aid),
        ).fetchall()

    # ── SNAPSHOTS ────────────────────────────────────────
    def save_snapshot(self, aid: int, summary: dict):
        self.cx.execute(
            "INSERT INTO auction_snapshots(auction_id,summary) VALUES(?,?)",
            (aid, json.dumps(summary)),
        )
        self.cx.commit()

    def get_snapshots(self, uid: int) -> list:
        return self.cx.execute("""
            SELECT s.snap_id, s.auction_id, s.completed_at, a.name auction_name, s.summary
            FROM auction_snapshots s
            JOIN auctions a ON a.auction_id = s.auction_id
            JOIN participants p ON p.auction_id = a.auction_id AND p.user_id = ?
            ORDER BY s.snap_id DESC LIMIT 10
        """, (uid,)).fetchall()

    def get_snapshot(self, snap_id: int):
        return self.cx.execute(
            "SELECT * FROM auction_snapshots WHERE snap_id=?", (snap_id,)
        ).fetchone()

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


# ─────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────
def cur(aid: Optional[int] = None) -> str:
    if aid:
        r = db.cx.execute("SELECT currency FROM auctions WHERE auction_id=?", (aid,)).fetchone()
        if r: return r["currency"]
    return db.get_setting("currency", "Rs.")


def fmt(lakhs: int, aid: Optional[int] = None) -> str:
    s = cur(aid)
    if lakhs >= 100:
        c = lakhs / 100
        return f"{s}{c:.1f}Cr" if c % 1 else f"{s}{int(c)}Cr"
    return f"{s}{lakhs}L"


def parse_price(s: str) -> Optional[int]:
    s = s.strip().lower().replace(" ", "")
    try:
        if s.endswith("cr"): return int(float(s[:-2]) * 100)
        if s.endswith("l"):  return int(float(s[:-1]))
        return int(s)
    except ValueError:
        return None


def flag(nat: str) -> str:
    return "🇮🇳" if "indian" in nat.lower() else "🌍"


def r_emoji(r: str) -> str:
    return {"batsman":"🏏","bat":"🏏","bowler":"⚡","bowl":"⚡",
            "all-rounder":"🌟","allrounder":"🌟","ar":"🌟",
            "wicketkeeper":"🧤","wk":"🧤"}.get(r.lower(), "🏏")


def tier_s(t: str) -> str:
    return {"Marquee":"⭐Marquee","A":"🔷A","B":"🔹B","C":"▪️C","Uncapped":"🔸Uncapped"}.get(t, t)


def norm_role(r: str) -> str:
    r = r.lower().strip()
    if r in ("bat","batsman","batter"):         return "Batsman"
    if r in ("bowl","bowler"):                  return "Bowler"
    if r in ("ar","allrounder","all-rounder"):  return "All-rounder"
    if r in ("wk","wicketkeeper","keeper"):     return "Wicketkeeper"
    return r.title()


def norm_nat(n: str) -> str:
    return "Indian" if n.lower() in ("indian","india","ind") else "Overseas"


def jump_link(chat_id: int, msg_id: int) -> str:
    """Build a t.me jump link for group messages."""
    if chat_id < 0:
        cid = str(chat_id).replace("-100", "")
        return f"https://t.me/c/{cid}/{msg_id}"
    return f"https://t.me/c/{chat_id}/{msg_id}"


def team_display(row) -> str:
    """'TeamName (@username)' from a participants row."""
    uname = f" (@{row['username']})" if row["username"] else ""
    return f"{row['team_name']}{uname}"


def eff_uid(uid: int) -> int:
    """Return primary user_id if this user is a co-owner, else uid itself."""
    if live.auction_id:
        p = db.get_primary(live.auction_id, uid)
        return p if p else uid
    return uid


def get_rtm_eligible(aid: int, exclude_uid: int) -> list:
    """All participants with rtm_cards > 0, excluding the current highest bidder."""
    return [
        r for r in db.get_all_parts(aid)
        if r["rtm_cards"] > 0 and r["user_id"] != exclude_uid and not r["is_muted"]
    ]


def validate_bid(row, player_row, bid_l: int, auction_row) -> Optional[str]:
    if row["is_muted"]:
        return "Your team is muted and cannot bid."
    if row["purse"] < bid_l:
        return f"Not enough purse! You have {fmt(row['purse'], row['auction_id'])} left."
    sq = json.loads(row["squad"])
    if len(sq) >= auction_row["max_players"]:
        return f"Squad full! Max {auction_row['max_players']} players."
    if bid_l < player_row["base_price"] and player_row["base_price"] > 0:
        return f"Min bid is {fmt(player_row['base_price'], row['auction_id'])}."
    if live.current_bid > 0 and bid_l <= live.current_bid:
        return f"Bid must exceed current {fmt(live.current_bid, row['auction_id'])}."
    return None


# ─────────────────────────────────────────────────────────
# MESSAGE BUILDERS
# ─────────────────────────────────────────────────────────
def player_card(row) -> str:
    base = fmt(row["base_price"], row["auction_id"]) if row["base_price"] > 0 else "Open"
    rtm  = f"\n🎴 RTM: Teams with RTM cards may use /rtm" if row["ipl_team"] else ""
    return (
        f"{'─'*28}\n"
        f"{r_emoji(row['role'])} *{flag(row['nationality'])} {row['name']}*\n"
        f"Role: {row['role']}  |  {row['nationality']}\n"
        f"Tier: {tier_s(row['tier'])}\n"
        f"Base: *{base}*  |  Prev Team: *{row['ipl_team'] or 'None'}*"
        f"{rtm}\n"
    )


def bid_status_text(player_row, bid: int, bidder: str,
                    timer: Optional[int] = None) -> str:
    aid     = player_row["auction_id"]
    bid_str = fmt(bid, aid) if bid > 0 else (fmt(player_row["base_price"], aid)
                                              if player_row["base_price"] > 0 else "Open")
    bidder_line = f"👑 Highest: *{bidder}* at *{bid_str}*" if bid > 0 else "No bids yet — open bidding!"
    t = f"⏱ *{timer}s* left" if timer is not None else "⏱ Timer starts on first bid"
    return (
        f"🔨 *{live.auction_name}* — Set {live.set_number}\n"
        f"{player_card(player_row)}\n"
        f"{bidder_line}\n{t}"
    )


def bid_keyboard(player_row, current_bid: int) -> InlineKeyboardMarkup:
    aid = player_row["auction_id"]
    if current_bid == 0:
        b1 = player_row["base_price"] if player_row["base_price"] > 0 else Config.INCREMENT
    else:
        b1 = current_bid + Config.INCREMENT
    b2 = b1 + Config.INCREMENT
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"💰 Bid {fmt(b1,aid)}", callback_data=f"bid_{b1}"),
            InlineKeyboardButton(f"➕ Bid {fmt(b2,aid)}", callback_data=f"bid_{b2}"),
        ],
        [
            InlineKeyboardButton("🎴 Use RTM", callback_data="rtm_use"),
            InlineKeyboardButton("💼 My Purse", callback_data="my_purse"),
        ],
    ])


def rtm_offer_text(player_row, teams_with_rtm: list) -> str:
    aid = player_row["auction_id"]
    names = ", ".join(team_display(r) for r in teams_with_rtm)
    return (
        f"🎴 *RTM AVAILABLE!*\n{'─'*28}\n"
        f"Player: *{player_row['name']}*\n"
        f"Sold to: *{live.highest_bidder_name}* for *{fmt(live.current_bid,aid)}*\n\n"
        f"Teams with RTM cards: {names}\n\n"
        f"Click *Use RTM* to exercise your Right To Match!"
    )


def rtm_offer_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎴 Use RTM", callback_data="rtm_use"),
        InlineKeyboardButton("❌ Skip RTM", callback_data="rtm_skip"),
    ]])


def rtm_counter_text(player_row, rtm_team: str, orig_bidder: str, orig_bid: int) -> str:
    aid = player_row["auction_id"]
    return (
        f"🎴 *{rtm_team}* has used RTM on *{player_row['name']}*!\n"
        f"{'─'*28}\n"
        f"Current bid: *{fmt(orig_bid,aid)}* by *{orig_bidder}*\n\n"
        f"*{orig_bidder}* — you may raise your bid!\n"
        f"Use /bid <amount> within *{Config.RTM_TIMER}s*.\n"
        f"If no counter, {rtm_team} wins the player."
    )


def rtm_ask_text(player_row, counter: int, rtm_team: str) -> str:
    aid = player_row["auction_id"]
    return (
        f"🔥 *Counter Bid!*\n{'─'*28}\n"
        f"*{live.rtm_orig_bidder_name}* raised to *{fmt(counter,aid)}*\n\n"
        f"*{rtm_team}* — do you accept *{fmt(counter,aid)}* for *{player_row['name']}*?\n\n"
        f"✅ YES → Player sold to you for {fmt(counter,aid)}\n"
        f"❌ NO → Player goes to {live.rtm_orig_bidder_name} for {fmt(live.rtm_orig_bid,aid)}"
    )


def rtm_ask_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ YES, accept", callback_data="rtm_yes"),
        InlineKeyboardButton("❌ NO, decline", callback_data="rtm_no"),
    ]])


def reauction_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 ReAuction", callback_data="reauction_prompt")
    ]])


def reauction_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, ReAuction", callback_data="reauction_yes"),
        InlineKeyboardButton("❌ No", callback_data="reauction_no"),
    ]])


# ─────────────────────────────────────────────────────────
# TIMER & AUCTION CORE
# ─────────────────────────────────────────────────────────
async def bid_timer(context: ContextTypes.DEFAULT_TYPE):
    duration = live.auto_sell_secs or Config.BID_TIMER
    end = _time.time() + duration
    live.timer_ends_at = end

    while True:
        await asyncio.sleep(5)
        if not live.active or live.paused or not live.current_player_id:
            return
        remaining = max(0, int(live.timer_ends_at - _time.time()))
        if remaining <= 0:
            break
        if live.last_bid_msg_id:
            pr = db.get_player(live.current_player_id)
            if pr:
                try:
                    await context.bot.edit_message_text(
                        chat_id=live.chat_id,
                        message_id=live.last_bid_msg_id,
                        text=bid_status_text(pr, live.current_bid,
                                             live.highest_bidder_name, remaining),
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=bid_keyboard(pr, live.current_bid),
                    )
                except Exception:
                    pass

    if not live.active or not live.current_player_id:
        return

    pr = db.get_player(live.current_player_id)
    if not pr:
        return

    if live.current_bid == 0:
        await _mark_unsold(context, pr)
    else:
        await _check_rtm(context, pr)


async def _mark_unsold(context: ContextTypes.DEFAULT_TYPE, pr):
    db.set_player_status(pr["player_id"], "unsold")
    live.unsold_count += 1
    _set_last_sold(pr["player_id"], pr["name"], None, "", 0)
    live.current_player_id = None

    msg = await context.bot.send_message(
        chat_id=live.chat_id,
        text=f"❌ *{pr['name']}* goes *UNSOLD!* No bids received.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reauction_keyboard(),
    )
    live.reauction_msg_id = msg.message_id
    await _try_auto_next(context)


async def _check_rtm(context: ContextTypes.DEFAULT_TYPE, pr):
    """After timer expires with a bid — check if any team has RTM cards."""
    aid = live.auction_id
    rtm_eligible = get_rtm_eligible(aid, live.highest_bidder_id)

    if rtm_eligible:
        live.rtm_state = RTM_OFFERED
        msg = await context.bot.send_message(
            chat_id=live.chat_id,
            text=rtm_offer_text(pr, rtm_eligible),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rtm_offer_keyboard(),
        )
        live.rtm_offer_msg_id = msg.message_id
        live.timer_task = asyncio.create_task(_rtm_offer_timer(context))
    else:
        await _finalize(context, pr)


async def _rtm_offer_timer(context: ContextTypes.DEFAULT_TYPE):
    """Wait RTM_TIMER seconds for someone to use RTM, then finalize."""
    end = _time.time() + Config.RTM_TIMER
    while _time.time() < end:
        await asyncio.sleep(1)
        if live.rtm_state != RTM_OFFERED:
            return  # Someone acted on it
    if live.rtm_state == RTM_OFFERED:
        live.rtm_state = RTM_NONE
        await context.bot.send_message(
            chat_id=live.chat_id,
            text="⏰ RTM window expired. Player goes to highest bidder.",
        )
        pr = db.get_player(live.current_player_id) if live.current_player_id else None
        if pr:
            await _finalize(context, pr)


async def _rtm_counter_timer(context: ContextTypes.DEFAULT_TYPE):
    """Wait for original bidder to counter after RTM is used."""
    end = _time.time() + Config.RTM_TIMER
    while _time.time() < end:
        await asyncio.sleep(1)
        if live.rtm_state != RTM_ACTIVE:
            return
    # No counter — RTM team wins at original bid
    if live.rtm_state == RTM_ACTIVE:
        live.current_bid         = live.rtm_orig_bid
        live.highest_bidder_id   = live.rtm_team_id
        live.highest_bidder_name = live.rtm_team_name
        live.rtm_state           = RTM_NONE
        await context.bot.send_message(
            chat_id=live.chat_id,
            text=f"⏰ No counter. *{live.rtm_team_name}* wins *{db.get_player(live.current_player_id)['name']}* "
                 f"for *{fmt(live.rtm_orig_bid, live.auction_id)}*!",
            parse_mode=ParseMode.MARKDOWN,
        )
        pr = db.get_player(live.current_player_id)
        if pr:
            await _finalize(context, pr, rtm_used=True)


async def _finalize(context: ContextTypes.DEFAULT_TYPE, pr, rtm_used: bool = False):
    if not live.highest_bidder_id:
        return

    aid          = live.auction_id
    winner_id    = live.highest_bidder_id
    winner_name  = live.highest_bidder_name
    final_price  = live.current_bid

    # Persist
    msg = await context.bot.send_message(
        chat_id=live.chat_id,
        text=(
            f"🔨 *SOLD!*\n{'─'*28}\n"
            f"Player: *{pr['name']}*\n"
            f"Team: *{winner_name}*\n"
            f"Price: *{fmt(final_price,aid)}*"
            f"{'  🎴 RTM' if rtm_used else ''}\n\n"
            f"Remaining purse for {winner_name}: "
            f"*{fmt(db.get_part(aid,winner_id)['purse']-final_price, aid)}*"
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reauction_keyboard(),
    )

    db.set_player_status(pr["player_id"], "sold", winner_id, final_price,
                         msg.message_id, live.chat_id)
    db.deduct_purse(aid, winner_id, final_price)
    db.add_to_squad(aid, winner_id, pr["player_id"])

    # Record bid history as WON for winner, LOST for previous bidders
    db.record_bid(aid, winner_id, pr["player_id"], pr["name"], final_price, won=True)

    live.sold_count += 1
    _set_last_sold(pr["player_id"], pr["name"], winner_id, winner_name, final_price)
    live.reauction_msg_id    = msg.message_id
    live.current_player_id   = None
    live.current_bid         = 0
    live.highest_bidder_id   = None
    live.highest_bidder_name = ""
    live.rtm_state           = RTM_NONE
    live.rtm_team_id         = None

    await _try_auto_next(context)


def _set_last_sold(pid, name, buyer_id, buyer_name, price):
    live.last_sold_pid       = pid
    live.last_sold_name      = name
    live.last_sold_buyer_id  = buyer_id
    live.last_sold_buyer_name= buyer_name
    live.last_sold_price     = price


async def _try_auto_next(context: ContextTypes.DEFAULT_TYPE):
    if live.auto_next_on and live.auto_next_secs and live.active:
        await asyncio.sleep(live.auto_next_secs)
        if not live.current_player_id and not live.paused and live.active:
            await _do_next(context, live.chat_id)


# ─────────────────────────────────────────────────────────
# NEXT PLAYER
# ─────────────────────────────────────────────────────────
async def _do_next(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    if not live.active:
        return
    if live.current_player_id:
        await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Current player still active! Use /sold or /pass first.",
        )
        return

    # Clear ReAuction button from previous message
    live.last_sold_pid = None

    if not live.player_queue:
        unsold = db.get_unsold(live.auction_id)
        if unsold:
            live.player_queue = list(unsold)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"♻️ Loading {len(unsold)} unsold players back into queue...",
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id, text="✅ All players done! Use /endauction for summary.",
            )
            return

    pr = live.player_queue.pop(0)
    # Re-fetch fresh
    if hasattr(pr, "keys"):
        fresh = db.get_player(pr["player_id"])
    else:
        fresh = db.get_player(pr)

    if not fresh or fresh["status"] != "available":
        await _do_next(context, chat_id)
        return

    live.current_player_id   = fresh["player_id"]
    live.current_bid         = 0
    live.highest_bidder_id   = None
    live.highest_bidder_name = ""
    live.timer_task          = None
    live.timer_ends_at       = None
    live.rtm_state           = RTM_NONE
    live.rtm_team_id         = None

    queued = len(live.player_queue)
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🔨 *{live.auction_name}*\n"
            f"{player_card(fresh)}\n"
            f"⏱ Timer starts on first bid\n"
            f"📋 Remaining in queue: {queued}"
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=bid_keyboard(fresh, 0),
    )
    live.last_bid_msg_id = msg.message_id
    live.chat_id         = chat_id


# ─────────────────────────────────────────────────────────
# BID PROCESSING
# ─────────────────────────────────────────────────────────
async def process_bid(update, context: ContextTypes.DEFAULT_TYPE,
                      caller_uid: int, bid_l: int):
    aid  = live.auction_id
    uid  = eff_uid(caller_uid)
    part = db.get_part(aid, uid) if aid else None

    async def err(m):
        if update.callback_query:
            await update.callback_query.answer(m, show_alert=True)
        else:
            await update.message.reply_text(m)

    if not part:
        await err("You are not registered in this auction.")
        return
    if not live.active or not live.current_player_id:
        await err("No active auction right now.")
        return
    if live.paused:
        await err("Auction is paused.")
        return
    if live.rtm_state == RTM_OFFERED:
        await err("RTM window active. Wait for RTM to resolve first.")
        return

    # Block current highest bidder from bidding again
    if live.highest_bidder_id == uid and live.rtm_state not in (RTM_ACTIVE, RTM_COUNTER):
        await err(f"You are already the highest bidder at {fmt(live.current_bid, aid)}!")
        return

    # In RTM_ACTIVE: only original bidder can counter
    if live.rtm_state == RTM_ACTIVE and uid != live.rtm_orig_bidder_id:
        await err("Waiting for the original bidder to counter or pass.")
        return

    pr = db.get_player(live.current_player_id)
    if not pr:
        await err("Player not found.")
        return

    auction_row = db.get_auction(aid)
    v_err = validate_bid(part, pr, bid_l, auction_row)
    if v_err:
        await err(v_err)
        return

    # Anti-snipe
    if live.timer_ends_at:
        rem = live.timer_ends_at - _time.time()
        if 0 < rem < Config.ANTI_SNIPE:
            live.timer_ends_at = _time.time() + Config.ANTI_SNIPE

    # RTM counter scenario
    if live.rtm_state == RTM_ACTIVE and uid == live.rtm_orig_bidder_id:
        if live.timer_task and not live.timer_task.done():
            live.timer_task.cancel()

        live.rtm_state       = RTM_COUNTER
        live.rtm_counter_bid = bid_l

        ask_msg = await context.bot.send_message(
            chat_id=live.chat_id,
            text=rtm_ask_text(pr, bid_l, live.rtm_team_name),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=rtm_ask_keyboard(),
        )
        live.rtm_msg_id = ask_msg.message_id
        if update.callback_query:
            await update.callback_query.answer(f"Counter bid {fmt(bid_l,aid)} sent!")
        else:
            await update.message.reply_text(
                f"⬆️ Counter *{fmt(bid_l,aid)}* sent to *{live.rtm_team_name}*!",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    # Normal bid
    prev_name             = live.highest_bidder_name
    live.current_bid      = bid_l
    live.highest_bidder_id   = uid
    live.highest_bidder_name = team_display(part)

    # Record as bid attempt (not won yet)
    db.record_bid(aid, uid, pr["player_id"], pr["name"], bid_l, won=False)

    if live.timer_task is None or live.timer_task.done():
        live.timer_task = asyncio.create_task(bid_timer(context))

    duration = live.auto_sell_secs or Config.BID_TIMER
    outbid   = f"⬆️ Outbids: {prev_name}" if prev_name and prev_name != team_display(part) else "🎯 Opening bid!"

    new_msg = await context.bot.send_message(
        chat_id=live.chat_id,
        text=(
            f"💥 *New Bid*\n{'─'*28}\n"
            f"Player: *{pr['name']}*\n"
            f"Amount: *{fmt(bid_l,aid)}*\n"
            f"Team: *{team_display(part)}*\n"
            f"{outbid}\n"
            f"⏱ Timer: {duration}s"
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=bid_keyboard(pr, bid_l),
    )
    live.last_bid_msg_id = new_msg.message_id

    if update.callback_query:
        await update.callback_query.answer(f"Bid {fmt(bid_l,aid)} placed!")


# ─────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────────────────
async def _reg(user):
    db.upsert_user(user.id, user.username or "", user.first_name or "")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await _reg(user)
    uname = f"@{user.username}" if user.username else user.first_name
    if user.id == Config.SUPER_ADMIN_ID:
        db.set_admin(user.id, True)
    await update.message.reply_text(
        f"Welcome *{uname}*! 🏏\n\n"
        f"Your ID: `{user.id}`\n"
        f"Use /help for all commands.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid    = update.effective_user.id
    is_adm = db.is_admin(uid)

    u = (
        "*USER COMMANDS*\n"
        "/start — Register\n"
        "/setteamname <n> — Rename your team\n"
        "/purse [@user] — Check purse & squad\n"
        "/squad [@user] — View squad\n"
        "/bid <amount> — Place bid\n"
        "/rtm — Use your RTM card\n"
        "/status — Current auction status\n"
        "/mybidhistory — Your bid results\n"
        "/auctionhistory — Past auctions\n"
        "/leaderboard — Top teams\n"
    )
    a = ""
    if is_adm:
        a = (
            "\n*ADMIN: SETUP*\n"
            "/create\\_auction <teams>,<purse>,<min\\_max>\n"
            "/setauctionname <n>\n"
            "/setcurrency <symbol>\n"
            "/admin @user — Grant admin\n"
            "\n*ADMIN: PLAYERS*\n"
            "/addplayer <n> <role> <team> <nat> <price> [tier]\n"
            "/add\\_player\\_list — Bulk from list\n"
            "/bulkplayer <P1>,<P2>,... — Quick bulk add\n"
            "/clearplayers\n"
            "\n*ADMIN: AUCTION*\n"
            "/startauction — Begin bidding\n"
            "/next — Next player\n"
            "/pass — Mark unsold / skip\n"
            "/forceauction <name|#n> — Force player now\n"
            "/sold — Confirm sale\n"
            "/forcesold — Force sell (skip RTM)\n"
            "/pauseauction | /resumeauction\n"
            "/endauction\n"
            "/autosell <secs|off>\n"
            "/autonext <enable|disable|secs>\n"
            "\n*ADMIN: TEAMS*\n"
            "/auctionowners — List all owners\n"
            "/soldplayers — Sold list with jump links\n"
            "/unsoldplayers — Unsold list\n"
            "/setrtm @user <cards> <team>\n"
            "/mute\\_team @user | /unmute\\_team @user\n"
            "/teamup @primary @proxy\n"
            "/setpurse @user <amt>\n"
            "/addpurse @user <amt>\n"
            "/deductpurse @user <amt>\n"
            "/addtosquad @user P1,P2\n"
            "/removefromsquad @user 1,2\n"
            "/clearsquad @user\n"
            "/swap @u1 @u2\n"
            "\n*ADMIN: QUEUE*\n"
            "/addtoqueue P1,P2 (.atq)\n"
            "/addtoqueueunsolds (.atqu)\n"
            "/removefromqueue 1,2 (.rfq)\n"
            "/shufflequeue (.sq)\n"
            "/swapqueue 1 2\n"
            "/clearqueue\n"
            "/queue [page]\n"
        )
    await update.message.reply_text(u + a, parse_mode=ParseMode.MARKDOWN)


# ── CREATE AUCTION ────────────────────────────────────────

async def cmd_create_auction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await _reg(user)
    if not db.is_admin(user.id):
        await update.message.reply_text("Admin only.")
        return

    raw = " ".join(context.args).replace(" ", "")
    parts = raw.split(",")
    if len(parts) < 3:
        await update.message.reply_text(
            "Usage: /create\\_auction <teams>,<purse>,<min\\_max>\n"
            "e.g. /create\\_auction 10,100cr,11\\_25",
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
        await update.message.reply_text("Invalid purse.")
        return

    rng = parts[2].replace("-", "_").split("_")
    try:
        min_p = int(rng[0])
        max_p = int(rng[1]) if len(rng) > 1 else min_p
    except (ValueError, IndexError):
        await update.message.reply_text("Invalid player range e.g. 11_25")
        return

    total = db.cx.execute("SELECT COUNT(*) c FROM auctions").fetchone()["c"]
    name  = f"IPL Auction #{total + 1}"
    aid   = db.create_auction(name, max_teams, purse, min_p, max_p,
                              update.effective_chat.id)

    # ✅ Reset live state for this fresh auction
    live.auction_id   = aid
    live.auction_name = name
    live.player_queue.clear()
    live.sold_count   = 0
    live.unsold_count = 0
    live.active       = False
    live.last_sold_pid= None

    join_btn = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"🏏 Join Auction (0/{max_teams})",
                             callback_data=f"join_{aid}")
    ]])
    msg = await update.message.reply_text(
        f"🏏 *{name}*\n{'─'*28}\n"
        f"Max Teams: *{max_teams}*\n"
        f"Purse: *{fmt(purse,aid)}* per team\n"
        f"Squad: {min_p}–{max_p} players\n\n"
        f"Tap below to join! Spots: *0/{max_teams}*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=join_btn,
    )
    db.set_reg_msg(aid, msg.message_id)


# ── USER INFO COMMANDS ────────────────────────────────────

async def cmd_set_team_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await _reg(user)
    aid = live.auction_id
    if not aid:
        await update.message.reply_text("No active auction.")
        return

    # Admin can set for another user
    if context.args and db.is_admin(user.id) and \
            (context.args[0].startswith("@") or context.args[0].isdigit()):
        target = db.resolve_uid(context.args[0])
        name   = " ".join(context.args[1:])
    else:
        target = user.id
        name   = " ".join(context.args)

    if not target or not name:
        await update.message.reply_text("Usage: /setteamname <Your Name>")
        return

    db.cx.execute(
        "UPDATE participants SET team_name=? WHERE auction_id=? AND user_id=?",
        (name, aid, target),
    )
    db.cx.commit()
    await update.message.reply_text(f"Team name set to: *{name}*",
                                    parse_mode=ParseMode.MARKDOWN)


async def cmd_purse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await _reg(user)
    aid = live.auction_id
    if not aid:
        await update.message.reply_text("No active auction.")
        return

    uid = db.resolve_uid(context.args[0]) if context.args else eff_uid(user.id)
    row = db.get_part(aid, uid) if uid else None
    if not row:
        await update.message.reply_text("Team not found in this auction.")
        return

    ar  = db.get_auction(aid)
    sq  = json.loads(row["squad"])
    sq_rows = [db.get_player(p) for p in sq]
    sq_rows = [p for p in sq_rows if p]
    ov  = sum(1 for p in sq_rows if p["nationality"] == "Overseas")
    roles: dict = {}
    for p in sq_rows:
        roles[p["role"]] = roles.get(p["role"], 0) + 1

    lines = [
        f"💼 *{team_display(row)}*\n{'─'*28}\n"
        f"Purse: *{fmt(row['purse'],aid)}*\n"
        f"Spent: {fmt(row['total_spent'],aid)}\n"
        f"Squad: {len(sq)}/{ar['max_players']}\n"
        f"Indian: {len(sq)-ov}  |  Overseas: {ov}\n"
        f"RTM Cards: {row['rtm_cards']}  (team: {row['rtm_team'] or 'N/A'})\n"
        f"Muted: {'Yes 🔇' if row['is_muted'] else 'No'}\n\nBy Role:"
    ]
    for r, c in roles.items():
        lines.append(f"  {r_emoji(r)} {r}: {c}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_squad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await _reg(user)
    aid = live.auction_id
    if not aid:
        await update.message.reply_text("No active auction.")
        return

    uid = db.resolve_uid(context.args[0]) if context.args else eff_uid(user.id)
    row = db.get_part(aid, uid) if uid else None
    if not row:
        await update.message.reply_text("Team not found.")
        return

    sq = [db.get_player(p) for p in json.loads(row["squad"])]
    sq = [p for p in sq if p]
    if not sq:
        await update.message.reply_text(f"*{team_display(row)}* — Squad empty.",
                                        parse_mode=ParseMode.MARKDOWN)
        return

    by_role: dict = {}
    for p in sq:
        by_role.setdefault(p["role"], []).append(p)

    lines = [f"🏏 *{team_display(row)}* ({len(sq)} players)\n{'─'*28}"]
    for role, players in by_role.items():
        lines.append(f"\n{r_emoji(role)} *{role}s*")
        for i, p in enumerate(players, 1):
            price = fmt(p["sold_price"] or p["base_price"], aid)
            lines.append(f"  {i}. {flag(p['nationality'])} {p['name']} — {price}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not live.active:
        await update.message.reply_text("No auction running right now.")
        return

    aid = live.auction_id
    if not live.current_player_id:
        await update.message.reply_text(
            f"*{live.auction_name}*\nWaiting for next player.\n"
            f"Sold: {live.sold_count} | Unsold: {live.unsold_count} "
            f"| Queue: {len(live.player_queue)}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    pr  = db.get_player(live.current_player_id)
    rem = max(0, int(live.timer_ends_at - _time.time())) if live.timer_ends_at else None

    rtm_note = {
        RTM_OFFERED: f"\n🎴 RTM window open — teams with cards can use RTM",
        RTM_ACTIVE:  f"\n🎴 RTM active — waiting for {live.rtm_orig_bidder_name} to counter",
        RTM_COUNTER: f"\n🎴 Counter bid — waiting for {live.rtm_team_name} to accept/decline",
    }.get(live.rtm_state, "")

    await update.message.reply_text(
        bid_status_text(pr, live.current_bid, live.highest_bidder_name, rem) + rtm_note,
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_bid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not context.args:
        await update.message.reply_text("Usage: /bid <amount>  e.g. /bid 2cr")
        return
    bid_l = parse_price(context.args[0])
    if bid_l is None:
        await update.message.reply_text("Invalid amount.")
        return
    await process_bid(update, context, update.effective_user.id, bid_l)


async def cmd_rtm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manual /rtm command — same as clicking Use RTM button."""
    await _reg(update.effective_user)
    uid = eff_uid(update.effective_user.id)
    await _handle_rtm_use(update, context, uid)


async def cmd_my_bid_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await _reg(user)
    aid = live.auction_id
    if not aid:
        await update.message.reply_text("No active auction.")
        return

    uid  = eff_uid(user.id)
    rows = db.get_my_bids(uid, aid)
    if not rows:
        await update.message.reply_text("No bids yet in this auction.")
        return

    # Deduplicate — keep last bid per player
    seen: dict = {}
    for r in rows:
        if r["player_id"] not in seen:
            seen[r["player_id"]] = r

    lines = [f"📊 *Your Bid History*\n{'─'*28}"]
    for r in seen.values():
        icon = "🟢 Won" if r["won"] else "🔴 Lost"
        lines.append(f"• *{r['player_name']}* — {fmt(r['bid_amount'],aid)} {icon}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_auction_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await _reg(user)
    snaps = db.get_snapshots(user.id)
    if not snaps:
        await update.message.reply_text("No past auctions found.")
        return

    lines  = ["📜 *Your Auction History* (last 10)\n"]
    btns   = []
    for i, s in enumerate(snaps, 1):
        lines.append(f"{i}. {s['auction_name']}  —  {str(s['completed_at'])[:10]}")
        btns.append(InlineKeyboardButton(str(i), callback_data=f"snap_{s['snap_id']}"))

    rows = [btns[j:j+5] for j in range(0, len(btns), 5)]
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    aid = live.auction_id
    if not aid:
        await update.message.reply_text("No auction session.")
        return
    parts = sorted(db.get_all_parts(aid), key=lambda r: r["total_spent"], reverse=True)
    medals = ["🥇","🥈","🥉"]
    lines  = [f"🏆 *{live.auction_name} — Leaderboard*\n{'─'*28}"]
    for i, r in enumerate(parts, 1):
        m  = medals[i-1] if i <= 3 else f"#{i}"
        sq = len(json.loads(r["squad"]))
        lines.append(f"{m} *{team_display(r)}*\n  Spent: {fmt(r['total_spent'],aid)} | {sq} players")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── ADMIN INFO COMMANDS ───────────────────────────────────

async def cmd_auction_owners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    aid = live.auction_id
    if not aid:
        await update.message.reply_text("No auction.")
        return

    parts = db.get_all_parts(aid)
    if not parts:
        await update.message.reply_text("No participants yet.")
        return

    lines = [f"👥 *Auction Owners — {live.auction_name}*\n{'─'*28}"]
    for r in parts:
        co = db.get_co_owners(aid, r["user_id"])
        co_names = ""
        if co:
            co_names = ", " + ", ".join(
                db.display(c["linked_user_id"]) for c in co
            )
        lines.append(f"• *{r['team_name']}* — @{r['username'] or r['user_id']}{co_names}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_unsold_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    aid = live.auction_id
    if not aid:
        await update.message.reply_text("No auction.")
        return

    unsold = db.get_unsold(aid)
    if not unsold:
        await update.message.reply_text("No unsold players.")
        return

    lines = [f"❌ *Unsold Players* ({len(unsold)})\n{'─'*28}"]
    for i, p in enumerate(unsold, 1):
        base = fmt(p["base_price"], aid) if p["base_price"] > 0 else "Open"
        lines.append(
            f"{i}. {flag(p['nationality'])} *{p['name']}* — "
            f"{p['role']} | Base: {base}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_sold_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    aid = live.auction_id
    if not aid:
        await update.message.reply_text("No auction.")
        return

    sold = db.get_sold(aid)
    if not sold:
        await update.message.reply_text("No players sold yet.")
        return

    lines = [f"✅ *Sold Players* ({len(sold)})\n{'─'*28}"]
    for p in sold:
        buyer = db.get_part(aid, p["sold_to"]) if p["sold_to"] else None
        buyer_name = team_display(buyer) if buyer else "?"
        price_str  = fmt(p["sold_price"], aid) if p["sold_price"] else "?"

        jump = ""
        if p["sold_msg_id"] and p["sold_chat_id"]:
            link = jump_link(p["sold_chat_id"], p["sold_msg_id"])
            jump = f"  [↗️ Jump]({link})"

        lines.append(
            f"• *{p['name']}* → {buyer_name} — *{price_str}*{jump}"
        )

    # Telegram message limit: split if too long
    text  = "\n".join(lines)
    if len(text) > 3800:
        chunk = lines[:1]
        for line in lines[1:]:
            if len("\n".join(chunk + [line])) > 3800:
                await update.message.reply_text(
                    "\n".join(chunk), parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=True
                )
                chunk = [line]
            else:
                chunk.append(line)
        if chunk:
            await update.message.reply_text(
                "\n".join(chunk), parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
    else:
        await update.message.reply_text(
            text, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True
        )


# ── FORCE AUCTION ─────────────────────────────────────────

async def cmd_force_auction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.active or not live.auction_id:
        await update.message.reply_text("Start an auction first.")
        return
    if live.current_player_id:
        await update.message.reply_text("Finish current player first (/pass or /sold).")
        return
    if not context.args:
        await update.message.reply_text("Usage: /forceauction <PlayerName|#QueuePosition>")
        return

    arg = " ".join(context.args).strip()
    aid = live.auction_id
    pr  = None

    if arg.startswith("#"):
        # Queue position
        try:
            idx = int(arg[1:]) - 1
            if 0 <= idx < len(live.player_queue):
                item = live.player_queue.pop(idx)
                pr   = db.get_player(item["player_id"] if hasattr(item, "keys") else item)
        except ValueError:
            pass
    else:
        # Search by name in queue first
        for i, item in enumerate(live.player_queue):
            pid  = item["player_id"] if hasattr(item, "keys") else item
            row  = db.get_player(pid)
            if row and arg.lower() in row["name"].lower():
                pr  = row
                live.player_queue.pop(i)
                break
        # Then search unsold
        if not pr:
            row = db.get_player_by_name(aid, arg)
            if row and row["status"] in ("available", "unsold"):
                pr = row

    if not pr:
        await update.message.reply_text(
            f"Player '{arg}' not found in queue or unsold list."
        )
        return

    # Reset player to available if unsold
    if pr["status"] == "unsold":
        db.restore_player(pr["player_id"])
        pr = db.get_player(pr["player_id"])

    live.current_player_id   = pr["player_id"]
    live.current_bid         = 0
    live.highest_bidder_id   = None
    live.highest_bidder_name = ""
    live.timer_task          = None
    live.timer_ends_at       = None
    live.rtm_state           = RTM_NONE

    chat_id = update.effective_chat.id
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"🔨 *FORCE AUCTION* — *{live.auction_name}*\n"
            f"{player_card(pr)}\n"
            f"⏱ Timer starts on first bid\n"
            f"📋 Queue remaining: {len(live.player_queue)}"
        ),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=bid_keyboard(pr, 0),
    )
    live.last_bid_msg_id = msg.message_id
    live.chat_id         = chat_id


# ── BULK PLAYER ───────────────────────────────────────────

async def cmd_bulk_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.auction_id:
        await update.message.reply_text("Create an auction first.")
        return

    raw   = " ".join(context.args)
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        await update.message.reply_text("Usage: /bulkplayer Player1,Player2,Player3...")
        return

    added = []
    aid   = live.auction_id
    for name in names:
        pid = db.add_player(aid, name, 0, "Batsman", "Indian", "", "C")
        if live.active:
            live.player_queue.append(db.get_player(pid))
        added.append(name)

    note = f" ({len(added)} added to queue)" if live.active else ""
    await update.message.reply_text(
        f"✅ Added {len(added)} players{note}:\n" +
        "\n".join(f"  • {n}" for n in added[:20]) +
        (f"\n  ...and {len(added)-20} more" if len(added) > 20 else "")
    )


# ── SET RTM ───────────────────────────────────────────────

async def cmd_set_rtm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if len(context.args) < 3 or not live.auction_id:
        await update.message.reply_text(
            "Usage: /setrtm @user <cards> <TeamName>\n"
            "e.g. /setrtm @dhoni 2 CSK"
        )
        return

    uid = db.resolve_uid(context.args[0])
    if not uid:
        await update.message.reply_text("User not found.")
        return

    try:
        cards = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Cards must be a number.")
        return

    team = " ".join(context.args[2:])
    db.set_rtm(live.auction_id, uid, cards, team)
    row = db.get_part(live.auction_id, uid)
    name = team_display(row) if row else str(uid)
    await update.message.reply_text(
        f"✅ *{name}* assigned *{cards}* RTM card(s) for team *{team}*.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── MUTE / UNMUTE ─────────────────────────────────────────

async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    if not context.args or not live.auction_id:
        await update.message.reply_text("Usage: /mute_team @user")
        return
    uid = db.resolve_uid(context.args[0])
    if not uid:
        await update.message.reply_text("User not found.")
        return
    db.set_muted(live.auction_id, uid, True)
    row  = db.get_part(live.auction_id, uid)
    name = team_display(row) if row else str(uid)
    await update.message.reply_text(f"🔇 *{name}* muted.", parse_mode=ParseMode.MARKDOWN)


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    if not context.args or not live.auction_id:
        await update.message.reply_text("Usage: /unmute_team @user")
        return
    uid = db.resolve_uid(context.args[0])
    if not uid:
        await update.message.reply_text("User not found.")
        return
    db.set_muted(live.auction_id, uid, False)
    row  = db.get_part(live.auction_id, uid)
    name = team_display(row) if row else str(uid)
    await update.message.reply_text(f"🔊 *{name}* unmuted.", parse_mode=ParseMode.MARKDOWN)


# ── TEAMUP ────────────────────────────────────────────────

async def cmd_teamup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if len(context.args) < 2 or not live.auction_id:
        await update.message.reply_text("Usage: /teamup @primary @proxy")
        return
    primary = db.resolve_uid(context.args[0])
    proxy   = db.resolve_uid(context.args[1])
    if not primary or not proxy:
        await update.message.reply_text("Could not resolve both users.")
        return
    p1 = db.get_part(live.auction_id, primary)
    if not p1:
        await update.message.reply_text(f"{context.args[0]} is not in this auction.")
        return
    db.link_co_owner(live.auction_id, primary, proxy)
    live.team_links[proxy] = primary
    await update.message.reply_text(
        f"🤝 *TeamUp!* {db.display(proxy)} can now bid as *{team_display(p1)}*.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── PURSE MANAGEMENT ─────────────────────────────────────

async def _purse_cmd(update, context, fn_name: str):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if len(context.args) < 2 or not live.auction_id:
        await update.message.reply_text(f"Usage: /{fn_name} @user <amount>")
        return
    uid = db.resolve_uid(context.args[0])
    amt = parse_price(context.args[-1])
    if not uid or not amt:
        await update.message.reply_text("Invalid user or amount.")
        return
    aid = live.auction_id
    {"setpurse": lambda: db.update_part(aid, uid, purse=amt),
     "addpurse": lambda: db.cx.execute("UPDATE participants SET purse=purse+? WHERE auction_id=? AND user_id=?", (amt,aid,uid)) or db.cx.commit(),
     "deductpurse": lambda: db.deduct_purse(aid, uid, amt),
    }[fn_name]()
    row  = db.get_part(aid, uid)
    name = team_display(row) if row else str(uid)
    await update.message.reply_text(
        f"✅ {fn_name} applied to *{name}*. Purse now: *{fmt(row['purse'],aid)}*",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_set_purse(u, c):    await _purse_cmd(u, c, "setpurse")
async def cmd_add_purse(u, c):    await _purse_cmd(u, c, "addpurse")
async def cmd_deduct_purse(u, c): await _purse_cmd(u, c, "deductpurse")


# ── SQUAD MANAGEMENT ─────────────────────────────────────

async def cmd_add_to_squad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    if len(context.args) < 2 or not live.auction_id:
        await update.message.reply_text("Usage: /addtosquad @user P1,P2")
        return
    uid   = db.resolve_uid(context.args[0])
    names = [n.strip() for n in " ".join(context.args[1:]).split(",") if n.strip()]
    if not uid:
        await update.message.reply_text("User not found.")
        return
    aid = live.auction_id
    for name in names:
        pid = db.add_player(aid, name, 0, "Batsman", "Indian", "", "C")
        db.set_player_status(pid, "sold", uid, 0)
        db.add_to_squad(aid, uid, pid)
    row = db.get_part(aid, uid)
    await update.message.reply_text(
        f"Added {len(names)} player(s) to *{team_display(row)}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_remove_from_squad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    if len(context.args) < 2 or not live.auction_id:
        await update.message.reply_text("Usage: /removefromsquad @user 1,2")
        return
    uid = db.resolve_uid(context.args[0])
    if not uid:
        await update.message.reply_text("User not found.")
        return
    try:
        positions = [int(x.strip()) for x in " ".join(context.args[1:]).split(",")]
    except ValueError:
        await update.message.reply_text("Invalid positions.")
        return
    aid  = live.auction_id
    row  = db.get_part(aid, uid)
    if not row:
        await update.message.reply_text("Participant not found.")
        return
    sq   = json.loads(row["squad"])
    for pos in sorted(positions, reverse=True):
        idx = pos - 1
        if 0 <= idx < len(sq):
            db.remove_from_squad(aid, uid, sq[idx])
            db.restore_player(sq[idx])
    await update.message.reply_text(
        f"Removed positions {positions} from *{team_display(row)}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_clear_squad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    if not live.auction_id:
        await update.message.reply_text("No auction.")
        return
    uid = db.resolve_uid(context.args[0]) if context.args else eff_uid(update.effective_user.id)
    row = db.get_part(live.auction_id, uid)
    if not row:
        await update.message.reply_text("Participant not found.")
        return
    sq = json.loads(row["squad"])
    for pid in sq:
        db.restore_player(pid)
    db.cx.execute("UPDATE participants SET squad='[]',total_spent=0 WHERE auction_id=? AND user_id=?",
                  (live.auction_id, uid))
    db.cx.commit()
    await update.message.reply_text(f"Squad cleared for *{team_display(row)}*",
                                    parse_mode=ParseMode.MARKDOWN)


async def cmd_swap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    if len(context.args) < 2 or not live.auction_id:
        await update.message.reply_text("Usage: /swap @u1 @u2")
        return
    u1 = db.resolve_uid(context.args[0])
    u2 = db.resolve_uid(context.args[1])
    if not u1 or not u2 or not db.swap_parts(live.auction_id, u1, u2):
        await update.message.reply_text("Could not swap. Both users must be in this auction.")
        return
    r1 = db.get_part(live.auction_id, u1)
    r2 = db.get_part(live.auction_id, u2)
    await update.message.reply_text(
        f"Swapped purse & squad: *{team_display(r1)}* ↔ *{team_display(r2)}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_set_currency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id) or not live.auction_id: return
    if not context.args:
        await update.message.reply_text("Usage: /setcurrency <symbol>")
        return
    db.cx.execute("UPDATE auctions SET currency=? WHERE auction_id=?",
                  (context.args[0], live.auction_id))
    db.cx.commit()
    await update.message.reply_text(f"Currency set to *{context.args[0]}*",
                                    parse_mode=ParseMode.MARKDOWN)


# ── QUEUE COMMANDS ────────────────────────────────────────

async def cmd_add_to_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    if not context.args or not live.auction_id:
        await update.message.reply_text("Usage: /addtoqueue P1,P2")
        return
    names = [n.strip() for n in " ".join(context.args).split(",") if n.strip()]
    for name in names:
        pid = db.add_player(live.auction_id, name, 20, "Batsman", "Indian", "", "C")
        r   = db.get_player(pid)
        if r: live.player_queue.append(r)
    await update.message.reply_text(
        f"Added {len(names)} to queue. Total: {len(live.player_queue)}"
    )


async def cmd_atq_unsolds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    if not live.auction_id:
        await update.message.reply_text("No auction.")
        return
    unsold = db.get_unsold(live.auction_id)
    live.player_queue.extend(list(unsold))
    await update.message.reply_text(
        f"Added {len(unsold)} unsold players. Queue: {len(live.player_queue)}"
    )


async def cmd_remove_from_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
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
            item = live.player_queue.pop(idx)
            name = item["name"] if hasattr(item, "keys") else str(item)
            removed.append(name)
    await update.message.reply_text(
        f"Removed: {', '.join(removed)}\nQueue: {len(live.player_queue)}"
    )


async def cmd_shuffle_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    random.shuffle(live.player_queue)
    await update.message.reply_text(f"Queue shuffled! {len(live.player_queue)} players.")


async def cmd_swap_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /swapqueue 1 2")
        return
    try:
        a, b = int(context.args[0]) - 1, int(context.args[1]) - 1
    except ValueError:
        await update.message.reply_text("Invalid.")
        return
    q = live.player_queue
    if not (0 <= a < len(q) and 0 <= b < len(q)):
        await update.message.reply_text("Out of range.")
        return
    q[a], q[b] = q[b], q[a]
    await update.message.reply_text(f"Swapped positions {a+1} and {b+1}.")


async def cmd_clear_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    live.player_queue.clear()
    await update.message.reply_text("Queue cleared.")


async def cmd_view_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    if not live.player_queue:
        await update.message.reply_text("Queue is empty.")
        return
    page  = int(context.args[0]) if context.args else 1
    per   = 15
    start = (page - 1) * per
    chunk = live.player_queue[start:start + per]
    total = max(1, (len(live.player_queue) + per - 1) // per)
    lines = [f"Queue (p{page}/{total}, {len(live.player_queue)} total)"]
    for i, item in enumerate(chunk, start + 1):
        r = item if hasattr(item, "keys") else db.get_player(item)
        if r:
            base = fmt(r["base_price"], r["auction_id"]) if r["base_price"] > 0 else "Open"
            lines.append(f"{i}. {flag(r['nationality'])} {r['name']} — {r['role']} | {base}")
    if page < total:
        lines.append(f"\n/queue {page+1} for next page.")
    await update.message.reply_text("\n".join(lines))


# ── AUCTION FLOW ──────────────────────────────────────────

async def cmd_start_auction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if live.active:
        await update.message.reply_text("Auction already running!")
        return
    if not live.auction_id:
        await update.message.reply_text("Create an auction first with /create\\_auction.",
                                        parse_mode=ParseMode.MARKDOWN)
        return

    available = db.get_available(live.auction_id)
    if not available:
        await update.message.reply_text("No available players. Add players first.")
        return

    if not live.player_queue:
        live.player_queue = list(available)

    live.active       = True
    live.paused       = False
    live.sold_count   = 0
    live.unsold_count = 0
    live.set_number   = 1
    live.chat_id      = update.effective_chat.id
    db.set_auction_status(live.auction_id, "active")

    ar = db.get_auction(live.auction_id)
    await update.message.reply_text(
        f"🏏 *{live.auction_name}* — STARTED!\n{'─'*28}\n"
        f"{len(live.player_queue)} players in queue\n"
        f"Purse: {fmt(ar['purse'], live.auction_id)} per team\n"
        f"Squad: {ar['min_players']}–{ar['max_players']} players\n\n"
        f"Use /next to begin!",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.active:
        await update.message.reply_text("Start auction first.")
        return
    await _do_next(context, update.effective_chat.id)


async def cmd_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.current_player_id:
        await update.message.reply_text("No active player.")
        return
    if live.timer_task and not live.timer_task.done():
        live.timer_task.cancel()
    pr = db.get_player(live.current_player_id)
    db.set_player_status(pr["player_id"], "unsold")
    live.unsold_count    += 1
    _set_last_sold(pr["player_id"], pr["name"], None, "", 0)
    live.current_player_id= None
    msg = await update.message.reply_text(
        f"⏭ *{pr['name']}* passed (UNSOLD).",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reauction_keyboard(),
    )
    live.reauction_msg_id = msg.message_id
    await _try_auto_next(context)


async def cmd_sold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.current_player_id:
        await update.message.reply_text("No active player.")
        return
    if not live.highest_bidder_id:
        await update.message.reply_text("No bids. Use /pass.")
        return
    if live.timer_task and not live.timer_task.done():
        live.timer_task.cancel()
    pr = db.get_player(live.current_player_id)
    await _finalize(context, pr)


async def cmd_force_sold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.current_player_id or not live.highest_bidder_id:
        await update.message.reply_text("No active bid.")
        return
    if live.timer_task and not live.timer_task.done():
        live.timer_task.cancel()
    live.rtm_state = RTM_NONE
    pr = db.get_player(live.current_player_id)
    await _finalize(context, pr)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    live.paused = True
    await update.message.reply_text("⏸ Auction PAUSED.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    live.paused = False
    await update.message.reply_text("▶️ Auction RESUMED!")


async def cmd_end_auction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.active:
        await update.message.reply_text("No active auction.")
        return
    if live.timer_task and not live.timer_task.done():
        live.timer_task.cancel()

    live.active         = False
    live.current_player_id = None
    aid = live.auction_id
    db.set_auction_status(aid, "completed")

    parts = sorted(db.get_all_parts(aid), key=lambda r: r["total_spent"], reverse=True)
    medals = ["🥇","🥈","🥉"]
    summary = {"name": live.auction_name, "sold": live.sold_count,
               "unsold": live.unsold_count, "teams": []}
    lines = [
        f"🏆 *{live.auction_name} — FINAL SUMMARY*\n{'─'*28}\n"
        f"✅ Sold: {live.sold_count}  ❌ Unsold: {live.unsold_count}\n"
    ]

    for i, r in enumerate(parts, 1):
        sq  = json.loads(r["squad"])
        sqs = [db.get_player(p) for p in sq]
        sqs = [p for p in sqs if p]
        ov  = sum(1 for p in sqs if p["nationality"] == "Overseas")
        m   = medals[i-1] if i <= 3 else f"#{i}"
        lines.append(
            f"{m} *{team_display(r)}*\n"
            f"  Spent: {fmt(r['total_spent'],aid)}  |  Left: {fmt(r['purse'],aid)}\n"
            f"  Squad: {len(sqs)}  |  Overseas: {ov}"
        )
        summary["teams"].append({
            "user_id": r["user_id"],
            "team": team_display(r),
            "spent": r["total_spent"],
            "purse": r["purse"],
            "squad": [{"name":p["name"],"role":p["role"],
                       "price":p["sold_price"],"nat":p["nationality"]}
                      for p in sqs],
        })

    db.save_snapshot(aid, summary)
    await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_auto_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: /autosell <secs|off>")
        return
    v = context.args[0].lower()
    if v == "off":
        live.auto_sell_secs = None
        await update.message.reply_text("Auto-sell disabled.")
    else:
        try:
            live.auto_sell_secs = int(v)
            await update.message.reply_text(f"Auto-sell: *{v}s*", parse_mode=ParseMode.MARKDOWN)
        except ValueError:
            await update.message.reply_text("Invalid.")


async def cmd_auto_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text("Usage: /autonext <enable|disable|secs>")
        return
    v = context.args[0].lower()
    if v == "disable":
        live.auto_next_on = False
        await update.message.reply_text("Auto-next disabled.")
    else:
        try:
            live.auto_next_secs = int(v) if v not in ("enable",) else (live.auto_next_secs or 5)
            live.auto_next_on   = True
            await update.message.reply_text(f"Auto-next: *{live.auto_next_secs}s*",
                                            parse_mode=ParseMode.MARKDOWN)
        except ValueError:
            await update.message.reply_text("Invalid.")


# ── PLAYER MANAGEMENT ────────────────────────────────────

async def cmd_add_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    if not live.auction_id:
        await update.message.reply_text("Create an auction first.")
        return
    if len(context.args) < 5:
        await update.message.reply_text(
            "Usage: /addplayer <n> <role> <team> <nat> <price> [tier]\n"
            "e.g. /addplayer ViratKohli Bat RCB Indian 2cr Marquee"
        )
        return
    name  = context.args[0].replace("_", " ")
    role  = norm_role(context.args[1])
    team  = context.args[2]
    nat   = norm_nat(context.args[3])
    price = parse_price(context.args[4])
    tier  = context.args[5].capitalize() if len(context.args) > 5 else "C"
    if price is None:
        await update.message.reply_text("Invalid price.")
        return
    aid = live.auction_id
    pid = db.add_player(aid, name, price, role, nat, team, tier)
    if live.active:
        r = db.get_player(pid)
        if r: live.player_queue.append(r)
        note = " — added to queue!"
    else:
        note = ""
    await update.message.reply_text(
        f"Added: *{flag(nat)} {name}* | {role} | "
        f"{fmt(price,aid) if price else 'Open'} | {tier}{note}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_add_player_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    if not live.auction_id:
        await update.message.reply_text("Create an auction first.")
        return
    lines  = update.message.text.strip().split("\n")[1:]
    added, failed = [], []
    for line in lines:
        line = re.sub(r"^\d+[\.\)]\s*", "", line.strip())
        if not line: continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            failed.append(line[:40])
            continue
        price = parse_price(parts[4])
        if price is None:
            failed.append(line[:40])
            continue
        tier = parts[5].capitalize() if len(parts) > 5 else "C"
        pid  = db.add_player(live.auction_id, parts[0], price,
                             norm_role(parts[1]), norm_nat(parts[3]), parts[2], tier)
        if live.active:
            r = db.get_player(pid)
            if r: live.player_queue.append(r)
        added.append(parts[0])

    msg = f"Added {len(added)} players!"
    if live.active and added: msg += f" ({len(added)} queued)"
    if added:
        msg += "\n" + "\n".join(f"  {n}" for n in added[:20])
        if len(added) > 20: msg += f"\n  ...+{len(added)-20} more"
    if failed:
        msg += f"\n\nFailed:\n" + "\n".join(failed[:5])
    await update.message.reply_text(msg)


async def cmd_clear_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if update.effective_user.id != Config.SUPER_ADMIN_ID:
        await update.message.reply_text("Super Admin only.")
        return
    if not live.auction_id:
        await update.message.reply_text("No auction.")
        return
    db.clear_players(live.auction_id)
    live.player_queue.clear()
    await update.message.reply_text("Players cleared.")


async def cmd_set_auction_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    if not context.args or not live.auction_id:
        await update.message.reply_text("Usage: /setauctionname <n>")
        return
    live.auction_name = " ".join(context.args)
    db.cx.execute("UPDATE auctions SET name=? WHERE auction_id=?",
                  (live.auction_name, live.auction_id))
    db.cx.commit()
    await update.message.reply_text(f"Auction name: *{live.auction_name}*",
                                    parse_mode=ParseMode.MARKDOWN)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if update.effective_user.id != Config.SUPER_ADMIN_ID:
        await update.message.reply_text("Super Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /admin @user  or  /admin <user_id>")
        return
    uid = db.resolve_uid(context.args[0])
    if not uid:
        await update.message.reply_text("User not found. They must /start first.")
        return
    db.set_admin(uid, True)
    u    = db.get_user(uid)
    name = f"@{u['username']}" if u and u["username"] else str(uid)
    await update.message.reply_text(f"✅ *{name}* is now an admin!",
                                    parse_mode=ParseMode.MARKDOWN)


# ─────────────────────────────────────────────────────────
# RTM HELPER
# ─────────────────────────────────────────────────────────
async def _handle_rtm_use(update, context: ContextTypes.DEFAULT_TYPE, uid: int):
    """Core logic for when a team tries to use RTM."""
    aid = live.auction_id
    if not live.active or not live.current_player_id:
        msg = "No active auction."
        if update.callback_query: await update.callback_query.answer(msg, show_alert=True)
        else: await update.message.reply_text(msg)
        return

    if live.rtm_state not in (RTM_NONE, RTM_OFFERED):
        msg = "RTM already in progress."
        if update.callback_query: await update.callback_query.answer(msg, show_alert=True)
        else: await update.message.reply_text(msg)
        return

    if live.highest_bidder_id is None:
        msg = "No bid placed yet — nothing to RTM against."
        if update.callback_query: await update.callback_query.answer(msg, show_alert=True)
        else: await update.message.reply_text(msg)
        return

    if uid == live.highest_bidder_id:
        msg = "You are the highest bidder — you cannot RTM yourself!"
        if update.callback_query: await update.callback_query.answer(msg, show_alert=True)
        else: await update.message.reply_text(msg)
        return

    row = db.get_part(aid, uid)
    if not row or row["rtm_cards"] <= 0:
        msg = "You have no RTM cards."
        if update.callback_query: await update.callback_query.answer(msg, show_alert=True)
        else: await update.message.reply_text(msg)
        return

    if row["purse"] < live.current_bid:
        msg = f"Not enough purse to RTM! You need {fmt(live.current_bid,aid)}."
        if update.callback_query: await update.callback_query.answer(msg, show_alert=True)
        else: await update.message.reply_text(msg)
        return

    # Cancel any running timers
    if live.timer_task and not live.timer_task.done():
        live.timer_task.cancel()

    # Deduct RTM card
    db.cx.execute("UPDATE participants SET rtm_cards=MAX(0,rtm_cards-1)"
                  " WHERE auction_id=? AND user_id=?", (aid, uid))
    db.cx.commit()

    live.rtm_state            = RTM_ACTIVE
    live.rtm_team_id          = uid
    live.rtm_team_name        = team_display(row)
    live.rtm_orig_bidder_id   = live.highest_bidder_id
    live.rtm_orig_bidder_name = live.highest_bidder_name
    live.rtm_orig_bid         = live.current_bid

    pr = db.get_player(live.current_player_id)

    if update.callback_query:
        await update.callback_query.answer("RTM card used!", show_alert=True)

    msg = await context.bot.send_message(
        chat_id=live.chat_id,
        text=rtm_counter_text(pr, live.rtm_team_name,
                              live.rtm_orig_bidder_name, live.rtm_orig_bid),
        parse_mode=ParseMode.MARKDOWN,
    )
    live.rtm_msg_id = msg.message_id
    # Start RTM counter window
    live.timer_task = asyncio.create_task(_rtm_counter_timer(context))


# ─────────────────────────────────────────────────────────
# CALLBACK HANDLER
# ─────────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data
    uid   = query.from_user.id
    await _reg(query.from_user)

    # ── JOIN ─────────────────────────────────
    if data.startswith("join_"):
        aid = int(data.split("_")[1])
        ar  = db.get_auction(aid)
        if not ar or ar["status"] != "registration":
            await query.answer("Registration closed.", show_alert=True)
            return
        count = db.count_participants(aid)
        if count >= ar["max_teams"]:
            await query.answer("Auction is full!", show_alert=True)
            return
        if db.get_part(aid, uid):
            await query.answer("Already registered!", show_alert=True)
            return
        u     = db.get_user(uid)
        uname = u["username"] if u and u["username"] else ""
        tname = u["first_name"] if u and u["first_name"] else f"Team{uid}"
        db.join(aid, uid, uname, tname, ar["purse"])
        new_count = db.count_participants(aid)
        left = ar["max_teams"] - new_count
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=query.message.chat_id,
                message_id=query.message.message_id,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        f"🏏 Join Auction ({new_count}/{ar['max_teams']})",
                        callback_data=f"join_{aid}",
                    )
                ]]),
            )
        except Exception: pass
        display_n = f"@{uname}" if uname else tname
        await query.answer(f"✅ Joined! Purse: {fmt(ar['purse'],aid)}", show_alert=True)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                f"🏏 Team *#{new_count}* — *{display_n}* joined *{ar['name']}*!\n"
                f"[{left} spot{'s' if left != 1 else ''} left]"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── MY PURSE ─────────────────────────────
    if data == "my_purse":
        aid  = live.auction_id
        euid = eff_uid(uid)
        row  = db.get_part(aid, euid) if aid else None
        if not row:
            await query.answer("Not in this auction.", show_alert=True)
            return
        ar   = db.get_auction(aid)
        sq   = len(json.loads(row["squad"]))
        await query.answer(
            f"{team_display(row)}\nPurse: {fmt(row['purse'],aid)}\n"
            f"Squad: {sq}/{ar['max_players']}\nRTM: {row['rtm_cards']}",
            show_alert=True,
        )
        return

    # ── BID ───────────────────────────────────
    if data.startswith("bid_"):
        try:    bid_l = int(data.split("_")[1])
        except: await query.answer("Invalid.", show_alert=True); return
        await process_bid(update, context, uid, bid_l)
        return

    # ── RTM USE ───────────────────────────────
    if data == "rtm_use":
        await _handle_rtm_use(update, context, eff_uid(uid))
        return

    # ── RTM SKIP (after timer-based RTM offer) ─
    if data == "rtm_skip":
        if not db.is_admin(uid) and live.rtm_team_id != eff_uid(uid):
            await query.answer("Not your action.", show_alert=True)
            return
        if live.rtm_state == RTM_OFFERED:
            if live.timer_task and not live.timer_task.done():
                live.timer_task.cancel()
            live.rtm_state = RTM_NONE
            try:
                await query.edit_message_text("RTM skipped. Player goes to highest bidder.")
            except Exception: pass
            pr = db.get_player(live.current_player_id)
            if pr: await _finalize(context, pr)
        await query.answer()
        return

    # ── RTM YES (RTM team accepts counter bid) ─
    if data == "rtm_yes":
        euid = eff_uid(uid)
        if live.rtm_state != RTM_COUNTER or live.rtm_team_id != euid:
            await query.answer("Not your action.", show_alert=True)
            return
        live.current_bid         = live.rtm_counter_bid
        live.highest_bidder_id   = live.rtm_team_id
        live.highest_bidder_name = live.rtm_team_name
        live.rtm_state           = RTM_NONE
        try:
            await query.edit_message_text(
                f"✅ *{live.rtm_team_name}* accepts *{fmt(live.rtm_counter_bid,live.auction_id)}*!",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception: pass
        await query.answer("Accepted!")
        pr = db.get_player(live.current_player_id)
        if pr: await _finalize(context, pr, rtm_used=True)
        return

    # ── RTM NO (RTM team declines counter bid) ─
    if data == "rtm_no":
        euid = eff_uid(uid)
        if live.rtm_state != RTM_COUNTER or live.rtm_team_id != euid:
            await query.answer("Not your action.", show_alert=True)
            return
        live.current_bid         = live.rtm_orig_bid
        live.highest_bidder_id   = live.rtm_orig_bidder_id
        live.highest_bidder_name = live.rtm_orig_bidder_name
        live.rtm_state           = RTM_NONE
        try:
            await query.edit_message_text(
                f"❌ *{live.rtm_team_name}* declines. "
                f"Player goes to *{live.rtm_orig_bidder_name}* for "
                f"*{fmt(live.rtm_orig_bid,live.auction_id)}*.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception: pass
        await query.answer("Declined.")
        pr = db.get_player(live.current_player_id)
        if pr: await _finalize(context, pr, rtm_used=False)
        return

    # ── REAUCTION PROMPT ─────────────────────
    if data == "reauction_prompt":
        if not db.is_admin(uid):
            await query.answer("Admin only.", show_alert=True)
            return
        if not live.last_sold_pid:
            await query.answer("No recent player to re-auction.", show_alert=True)
            return
        buyer_info = (
            f"Bought by *{live.last_sold_buyer_name}* for *{fmt(live.last_sold_price,live.auction_id)}*"
            if live.last_sold_buyer_id else "was UNSOLD"
        )
        await query.answer()
        await context.bot.send_message(
            chat_id=live.chat_id,
            text=(
                f"🔄 *ReAuction?*\n{'─'*28}\n"
                f"Player: *{live.last_sold_name}*\n"
                f"{buyer_info}\n\nConfirm?"
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reauction_confirm_keyboard(),
        )
        return

    # ── REAUCTION YES ────────────────────────
    if data == "reauction_yes":
        if not db.is_admin(uid):
            await query.answer("Admin only.", show_alert=True)
            return
        pid = live.last_sold_pid
        if not pid:
            await query.answer("Nothing to re-auction.", show_alert=True)
            return
        # Refund buyer
        if live.last_sold_buyer_id and live.last_sold_price > 0:
            db.refund_purse(live.auction_id, live.last_sold_buyer_id, live.last_sold_price)
            db.remove_from_squad(live.auction_id, live.last_sold_buyer_id, pid)
        db.restore_player(pid)
        fresh = db.get_player(pid)
        if fresh:
            live.player_queue.insert(0, fresh)
        buyer_n     = live.last_sold_buyer_name
        player_n    = live.last_sold_name
        live.last_sold_pid = None
        try:
            await query.edit_message_text(
                f"✅ *{player_n}* added back to queue!"
                f"{(' Refund issued to ' + buyer_n) if live.last_sold_buyer_id else ''}",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception: pass
        await query.answer("Re-auction queued!")
        return

    # ── REAUCTION NO ─────────────────────────
    if data == "reauction_no":
        if not db.is_admin(uid):
            await query.answer("Admin only.", show_alert=True)
            return
        live.last_sold_pid = None
        try:
            await query.edit_message_text("Skipped.")
        except Exception: pass
        await query.answer()
        await _do_next(context, live.chat_id)
        return

    # ── AUCTION HISTORY DETAIL ───────────────
    if data.startswith("snap_"):
        snap_id = int(data.split("_")[1])
        snap    = db.get_snapshot(snap_id)
        if not snap:
            await query.answer("Not found.", show_alert=True)
            return
        summary = json.loads(snap["summary"])
        lines   = [
            f"📜 *{summary['name']}*\n{'─'*28}\n"
            f"✅ Sold: {summary['sold']}  ❌ Unsold: {summary['unsold']}\n"
        ]
        for team in summary.get("teams", []):
            lines.append(f"\n🏏 *{team['team']}*")
            lines.append(f"  Spent: {team['spent']}L | Left: {team['purse']}L | {len(team['squad'])} players")
            for i, p in enumerate(team["squad"], 1):
                lines.append(
                    f"  {i}. {flag(p.get('nat','Indian'))} {p['name']}"
                    f" — {p.get('role','?')} | {p.get('price',0)}L"
                )
        await query.answer()
        text = "\n".join(lines)
        if len(text) > 3800:
            text = text[:3800] + "\n..."
        try:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            await context.bot.send_message(chat_id=query.message.chat_id, text=text)
        return

    await query.answer()


# ─────────────────────────────────────────────────────────
# DOT COMMAND ROUTER
# ─────────────────────────────────────────────────────────
DOT_MAP = {
    "setteamname": cmd_set_team_name, "stn": cmd_set_team_name,
    "purse": cmd_purse, "bal": cmd_purse, "balance": cmd_purse,
    "squad": cmd_squad, "status": cmd_status,
    "bid": cmd_bid, "rtm": cmd_rtm,
    "mybidhistory": cmd_my_bid_history,
    "setpurse": cmd_set_purse, "setbal": cmd_set_purse,
    "addpurse": cmd_add_purse, "addbal": cmd_add_purse,
    "deductpurse": cmd_deduct_purse, "deductbal": cmd_deduct_purse,
    "addtosquad": cmd_add_to_squad, "ats": cmd_add_to_squad,
    "removefromsquad": cmd_remove_from_squad, "rfs": cmd_remove_from_squad,
    "clearsquad": cmd_clear_squad, "swap": cmd_swap,
    "addtoqueue": cmd_add_to_queue, "atq": cmd_add_to_queue,
    "addtoqueueunsolds": cmd_atq_unsolds, "atqu": cmd_atq_unsolds,
    "removefromqueue": cmd_remove_from_queue, "rfq": cmd_remove_from_queue,
    "shufflequeue": cmd_shuffle_queue, "sq": cmd_shuffle_queue,
    "swapqueue": cmd_swap_queue, "clearqueue": cmd_clear_queue,
    "queue": cmd_view_queue, "q": cmd_view_queue,
    "startauction": cmd_start_auction,
    "next": cmd_next, "pass": cmd_pass,
    "sold": cmd_sold, "forcesold": cmd_force_sold,
    "forceauction": cmd_force_auction,
    "pauseauction": cmd_pause, "resumeauction": cmd_resume,
    "endauction": cmd_end_auction, "endsauction": cmd_end_auction,
    "autosell": cmd_auto_sell, "autonext": cmd_auto_next,
    "leaderboard": cmd_leaderboard, "help": cmd_help,
    "mute": cmd_mute, "unmute": cmd_unmute,
    "setrtm": cmd_set_rtm,
    "bulkplayer": cmd_bulk_player,
    "auctionowners": cmd_auction_owners,
    "soldplayers": cmd_sold_players,
    "unsoldplayers": cmd_unsold_players,
}


async def dot_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text.startswith("."): return
    parts = text[1:].split()
    if not parts: return
    handler = DOT_MAP.get(parts[0].lower())
    if not handler: return
    context.args = parts[1:]
    await handler(update, context)


# ─────────────────────────────────────────────────────────
# FLASK
# ─────────────────────────────────────────────────────────
@flask_app.route("/")
def root(): return "IPL Auction Bot v4.0 is running!", 200

@flask_app.route("/health")
def health():
    return {"status": "ok", "auction": live.auction_name, "active": live.active}, 200


# ─────────────────────────────────────────────────────────
# APP SETUP & MAIN
# ─────────────────────────────────────────────────────────
def build_app() -> Application:
    app = Application.builder().token(Config.BOT_TOKEN).build()

    # User
    app.add_handler(CommandHandler(["start","registration"], cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler(["setteamname","stn"], cmd_set_team_name))
    app.add_handler(CommandHandler(["purse","bal","balance"], cmd_purse))
    app.add_handler(CommandHandler("squad", cmd_squad))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("bid", cmd_bid))
    app.add_handler(CommandHandler("rtm", cmd_rtm))
    app.add_handler(CommandHandler(["mybidhistory","bidhistory"], cmd_my_bid_history))
    app.add_handler(CommandHandler(["auctionhistory","auction_history"], cmd_auction_history))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))

    # Admin: setup
    app.add_handler(CommandHandler(["create_auction","createauction"], cmd_create_auction))
    app.add_handler(CommandHandler("setauctionname", cmd_set_auction_name))
    app.add_handler(CommandHandler("setcurrency", cmd_set_currency))
    app.add_handler(CommandHandler("admin", cmd_admin))

    # Admin: info
    app.add_handler(CommandHandler(["auctionowners","auction_owners"], cmd_auction_owners))
    app.add_handler(CommandHandler(["soldplayers","sold_players"], cmd_sold_players))
    app.add_handler(CommandHandler(["unsoldplayers","unsold_players"], cmd_unsold_players))

    # Admin: players
    app.add_handler(CommandHandler("addplayer", cmd_add_player))
    app.add_handler(CommandHandler("add_player_list", cmd_add_player_list))
    app.add_handler(CommandHandler(["bulkplayer","bulk_player"], cmd_bulk_player))
    app.add_handler(CommandHandler("clearplayers", cmd_clear_players))

    # Admin: teams
    app.add_handler(CommandHandler("setrtm", cmd_set_rtm))
    app.add_handler(CommandHandler(["mute_team","muteteam"], cmd_mute))
    app.add_handler(CommandHandler(["unmute_team","unmuteteam"], cmd_unmute))
    app.add_handler(CommandHandler("teamup", cmd_teamup))
    app.add_handler(CommandHandler(["setpurse","setbal"], cmd_set_purse))
    app.add_handler(CommandHandler(["addpurse","addbal"], cmd_add_purse))
    app.add_handler(CommandHandler(["deductpurse","deductbal"], cmd_deduct_purse))
    app.add_handler(CommandHandler(["addtosquad","ats"], cmd_add_to_squad))
    app.add_handler(CommandHandler(["removefromsquad","rfs"], cmd_remove_from_squad))
    app.add_handler(CommandHandler("clearsquad", cmd_clear_squad))
    app.add_handler(CommandHandler("swap", cmd_swap))

    # Admin: auction
    app.add_handler(CommandHandler("startauction", cmd_start_auction))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CommandHandler("pass", cmd_pass))
    app.add_handler(CommandHandler(["forceauction","force_auction"], cmd_force_auction))
    app.add_handler(CommandHandler("sold", cmd_sold))
    app.add_handler(CommandHandler("forcesold", cmd_force_sold))
    app.add_handler(CommandHandler("pauseauction", cmd_pause))
    app.add_handler(CommandHandler("resumeauction", cmd_resume))
    app.add_handler(CommandHandler(["endauction","endsauction"], cmd_end_auction))
    app.add_handler(CommandHandler("autosell", cmd_auto_sell))
    app.add_handler(CommandHandler("autonext", cmd_auto_next))

    # Admin: queue
    app.add_handler(CommandHandler(["addtoqueue","atq"], cmd_add_to_queue))
    app.add_handler(CommandHandler(["addtoqueueunsolds","atqu"], cmd_atq_unsolds))
    app.add_handler(CommandHandler(["removefromqueue","rfq"], cmd_remove_from_queue))
    app.add_handler(CommandHandler(["shufflequeue","sq"], cmd_shuffle_queue))
    app.add_handler(CommandHandler("swapqueue", cmd_swap_queue))
    app.add_handler(CommandHandler("clearqueue", cmd_clear_queue))
    app.add_handler(CommandHandler(["queue","q"], cmd_view_queue))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^\."), dot_handler))

    return app


async def _setup_wh(app: Application, url: str):
    await app.initialize()
    await app.bot.set_webhook(
        url=f"{url}/webhook",
        allowed_updates=["message","callback_query"],
        drop_pending_updates=True,
    )
    await app.start()
    logger.info(f"Webhook: {url}/webhook")


def main():
    if not Config.BOT_TOKEN or "YOUR_TOKEN" in Config.BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set!")
    if not Config.SUPER_ADMIN_ID:
        raise ValueError("SUPER_ADMIN_ID not set!")

    logger.info("Starting IPL Auction Bot v4.0...")
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

        loop.run_until_complete(_setup_wh(ptb, Config.WEBHOOK_URL))
        import threading
        threading.Thread(
            target=lambda: flask_app.run(host="0.0.0.0", port=Config.PORT, use_reloader=False),
            daemon=True,
        ).start()
        logger.info(f"Webhook mode on port {Config.PORT}")
        loop.run_forever()
    else:
        logger.info("Polling mode")
        ptb.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
