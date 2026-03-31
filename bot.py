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
    # Optional: a private Telegram channel/group where bot is admin.
    # Bot stores state JSON here — survives Render filesystem wipes.
    # Set to the channel_id (e.g. -1001234567890) in Render env vars.
    STATE_CHANNEL_ID: int = int(os.getenv("STATE_CHANNEL_ID", "0"))
    BID_TIMER: int           = 30
    RTM_OFFER_TIMER: int     = 30   # Step 1 → window for eligible teams to use /rtm
    RTM_COUNTER_TIMER: int   = 20   # Step 2 → window for original bidder to raise
    RTM_DECISION_TIMER: int  = 15   # Step 3 → window for RTM team to accept/decline
    RTM_TIMER: int           = 20   # legacy alias kept for existing references
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
    rtm_offer_msg_id: Optional[int] = None   # message with RTM check after timer
    rtm_counter_ends_at: Optional[float] = None   # when counter window expires
    rtm_decision_ends_at: Optional[float] = None  # when decision window expires

    # ReAuction (no expiry — cleared only when next player starts)
    last_sold_pid: Optional[int]   = None
    last_sold_name: str        = ""
    last_sold_buyer_id: Optional[int] = None
    last_sold_buyer_name: str  = ""
    last_sold_price: int       = 0
    reauction_msg_id: Optional[int] = None

    # TeamUp links {linked_uid: primary_uid}
    team_links: dict           = field(default_factory=dict)

    # Squad composition limits (set via /setlimit)
    # Format: {"OS":(max,min),"Bat":(max,min),"Bowl":(max,min),"AR":(max,min),"WK":(max,min)}
    squad_limits: dict         = field(default_factory=dict)

    # Minimum bid increment (lakhs) — default 10L
    min_increment: int         = 10

    # Undo snapshot — stores last sold transaction for /undo
    undo_snapshot: Optional[dict] = None


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
# STATE PERSISTENCE — Full DB backup to Telegram
#
# On every save: upload auction.db as a document to STATE_CHANNEL_ID.
# On restart: download it and replace the local file — full restoration.
# This survives complete Render filesystem wipes because the DB itself
# is stored in Telegram, not the local disk.
# ─────────────────────────────────────────────────────────

_db_backup_msg_id: Optional[int] = None   # Telegram msg_id of latest DB backup


def save_live_state():
    """Save live_state JSON to SQLite, then schedule async full-DB upload."""
    import json as _json
    state = {
        "v":              3,
        "auction_id":     live.auction_id,
        "auction_name":   live.auction_name,
        "chat_id":        live.chat_id,
        "active":         live.active,
        "paused":         live.paused,
        "sold_count":     live.sold_count,
        "unsold_count":   live.unsold_count,
        "set_number":     live.set_number,
        "auto_sell_secs": live.auto_sell_secs,
        "auto_next_secs": live.auto_next_secs,
        "auto_next_on":   live.auto_next_on,
        "squad_limits":   live.squad_limits,
        "min_increment":  live.min_increment,
        "player_queue":   [
            (r["player_id"] if hasattr(r, "keys") else int(r))
            for r in live.player_queue
        ],
    }
    try:
        db.cx.execute(
            "INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
            ("live_state", _json.dumps(state))
        )
        db.cx.commit()
    except Exception as e:
        logger.warning(f"save_live_state SQLite: {e}")

    if Config.STATE_CHANNEL_ID and _ptb_app:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(_backup_db_to_telegram())
        except Exception:
            pass


async def _backup_db_to_telegram():
    """
    Upload the full SQLite DB file to STATE_CHANNEL_ID and PIN it.
    On restart, get_chat() returns the pinned message — no stored ID needed.
    """
    global _db_backup_msg_id
    if not Config.STATE_CHANNEL_ID or not _ptb_app:
        return
    try:
        db.cx.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.cx.commit()
    except Exception:
        pass
    caption = (
        f"\U0001f512 AuctionDB | {live.auction_name} | "
        f"sold={live.sold_count} queue={len(live.player_queue)}"
    )
    try:
        with open(Config.DB_PATH, "rb") as f:
            db_bytes = f.read()
        msg = await _ptb_app.bot.send_document(
            chat_id=Config.STATE_CHANNEL_ID,
            document=db_bytes,
            filename="auction_backup.db",
            caption=caption,
        )
        # Pin the new message — restore uses pinned_message, needs no stored ID
        try:
            await _ptb_app.bot.pin_chat_message(
                chat_id=Config.STATE_CHANNEL_ID,
                message_id=msg.message_id,
                disable_notification=True,
            )
        except Exception as pe:
            logger.warning(f"Pin failed (bot not channel admin?): {pe}")
        # Delete previous backup
        if _db_backup_msg_id and _db_backup_msg_id != msg.message_id:
            try:
                await _ptb_app.bot.delete_message(
                    chat_id=Config.STATE_CHANNEL_ID,
                    message_id=_db_backup_msg_id,
                )
            except Exception:
                pass
        _db_backup_msg_id = msg.message_id
        logger.info(f"DB backed up+pinned msg={msg.message_id} ({len(db_bytes):,}B)")
    except FileNotFoundError:
        logger.warning("DB file missing — skipping Telegram backup")
    except Exception as e:
        logger.warning(f"_backup_db_to_telegram: {e}")


def _apply_state_dict(state: dict) -> bool:
    """Apply a saved state dict to live. Returns True on success."""
    try:
        aid = state.get("auction_id")
        if not aid:
            return False
        ar = db.get_auction(aid)
        if not ar:
            logger.warning(f"_apply_state_dict: auction_id={aid} not in DB")
            return False
        live.auction_id     = aid
        live.auction_name   = state.get("auction_name", ar["name"])
        live.chat_id        = state.get("chat_id")
        live.active         = state.get("active", True)
        live.paused         = state.get("paused", False)
        live.sold_count     = state.get("sold_count", 0)
        live.unsold_count   = state.get("unsold_count", 0)
        live.set_number     = state.get("set_number", 1)
        live.auto_sell_secs = state.get("auto_sell_secs")
        live.auto_next_secs = state.get("auto_next_secs")
        live.auto_next_on   = state.get("auto_next_on", False)
        live.squad_limits   = state.get("squad_limits", {})
        live.min_increment  = state.get("min_increment", 10)
        pids = state.get("player_queue", [])
        live.player_queue = []
        for pid in pids:
            row = db.get_player(int(pid))
            if row and row["status"] == "available":
                live.player_queue.append(row)
        if not live.player_queue:
            live.player_queue = list(db.get_available(aid))
        return True
    except Exception as e:
        logger.error(f"_apply_state_dict: {e}", exc_info=True)
        return False


async def restore_live_state_async(bot) -> bool:
    """
    Restore on startup — survives complete Render filesystem wipe.

    Step 1: get_chat(STATE_CHANNEL_ID).pinned_message → download DB
            Works with NO stored IDs — pinned message is always findable.
    Step 2: Read live_state JSON from the restored DB.
    Step 3: Fallback — reconstruct from DB tables if they exist.
    """
    import json as _json
    global _db_backup_msg_id

    # ── Step 1: Restore DB from Telegram pinned message ────
    if Config.STATE_CHANNEL_ID:
        try:
            chat   = await bot.get_chat(Config.STATE_CHANNEL_ID)
            pinned = getattr(chat, "pinned_message", None)

            if pinned and pinned.document:
                logger.info(f"Found pinned DB backup msg={pinned.message_id}, downloading...")
                file_obj = await bot.get_file(pinned.document.file_id)
                db_bytes  = await file_obj.download_as_bytearray()
                import os
                os.makedirs(os.path.dirname(os.path.abspath(Config.DB_PATH)), exist_ok=True)
                with open(Config.DB_PATH, "wb") as f:
                    f.write(bytes(db_bytes))
                # Force SQLite reconnect with new file
                try:
                    db._local.__dict__.clear()
                except Exception:
                    pass
                try:
                    db._init()
                except Exception:
                    pass
                _db_backup_msg_id = pinned.message_id
                logger.info(f"DB restored from Telegram ({len(db_bytes):,} bytes)")
            else:
                logger.info("STATE_CHANNEL_ID set but no pinned DB backup found")
        except Exception as e:
            logger.warning(f"Telegram DB restore: {e}")

    # ── Step 2: Read live_state from (restored) DB ─────────
    try:
        raw = db.get_setting("live_state")
        if raw:
            state = _json.loads(raw)
            if _apply_state_dict(state):
                logger.info(
                    f"State applied: {live.auction_name} "
                    f"sold={live.sold_count} queue={len(live.player_queue)}"
                )
                return True
            logger.warning("live_state JSON present but _apply_state_dict failed")
    except Exception as e:
        logger.warning(f"live_state read: {e}")

    # ── Step 3: Reconstruct from DB tables ────────────────
    result = restore_live_state()
    if result:
        logger.info(f"Reconstructed from DB tables: {live.auction_name}")
    return result

def restore_live_state() -> bool:
    """Synchronous fallback: reconstruct from DB tables without any snapshot."""
    try:
        ar = db.cx.execute(
            "SELECT * FROM auctions WHERE status=\'active\' ORDER BY auction_id DESC LIMIT 1"
        ).fetchone()
        if not ar:
            return False
        aid = ar["auction_id"]
        sc  = db.cx.execute(
            "SELECT COUNT(*) c FROM players WHERE auction_id=? AND status=\'sold\'", (aid,)
        ).fetchone()
        uc  = db.cx.execute(
            "SELECT COUNT(*) c FROM players WHERE auction_id=? AND status=\'unsold\'", (aid,)
        ).fetchone()
        live.auction_id   = aid
        live.auction_name = ar["name"]
        live.chat_id      = ar["chat_id"]
        live.active       = True
        live.paused       = True
        live.sold_count   = sc["c"] if sc else 0
        live.unsold_count = uc["c"] if uc else 0
        live.player_queue = list(db.get_available(aid))
        logger.info(
            f"\u2705 Reconstructed: {live.auction_name} "
            f"sold={live.sold_count} queue={len(live.player_queue)}"
        )
        return True
    except Exception as e:
        logger.error(f"restore_live_state: {e}", exc_info=True)
        return False

# ─────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────
def cur(aid: Optional[int] = None) -> str:
    if aid:
        r = db.cx.execute("SELECT currency FROM auctions WHERE auction_id=?", (aid,)).fetchone()
        if r: return r["currency"]
    return db.get_setting("currency", "Rs.")


def md_safe(s: str) -> str:
    """Escape Markdown special characters in dynamic user-supplied values.
    Only escapes _ ` [ — these are the chars that appear in usernames/team names.
    We leave * alone since names don't use bold and escaping * can break display.
    """
    if not s:
        return str(s)
    s = str(s)
    for ch in ('_', '`', '['):
        s = s.replace(ch, f'\\{ch}')
    return s


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


def ist_now() -> str:
    """Current time in IST (UTC+5:30) formatted as 12-hour clock."""
    import datetime
    utc = datetime.datetime.utcnow()
    ist = utc + datetime.timedelta(hours=5, minutes=30)
    return ist.strftime("%I:%M:%S %p IST")


def _esc(s: str) -> str:
    """Escape Markdown special chars in dynamic values (names, usernames)."""
    return str(s).replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")


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
    """Build a t.me/c jump link for group/supergroup messages."""
    cid = str(chat_id)
    # Supergroup IDs start with -100; strip the leading minus and '100'
    if cid.startswith("-100"):
        cid = cid[4:]
    elif cid.startswith("-"):
        cid = cid[1:]
    return f"https://t.me/c/{cid}/{msg_id}"


def team_display(row) -> str:
    """'TeamName (@username)' from a participants row."""
    uname = f" (@{row['username']})" if row["username"] else ""
    return f"{row['team_name']}{uname}"


def bid_display(row) -> str:
    """'@username - TeamName' format used in bid messages (no tagging)."""
    uname = f"@{row['username']}" if row["username"] else f"ID:{row['user_id']}"
    return f"{uname} - {row['team_name']}"


def eff_uid(uid: int) -> int:
    """Return primary user_id if this user is a co-owner, else uid itself."""
    if live.auction_id:
        p = db.get_primary(live.auction_id, uid)
        return p if p else uid
    return uid


def get_rtm_eligible(aid: int, exclude_uid: int, ipl_team: str = "") -> list:
    """
    All participants with rtm_cards > 0, excluding the current highest bidder.
    If ipl_team is given, only teams whose rtm_team matches that ipl_team are returned.
    Players with no ipl_team are never RTM-eligible.
    """
    if not ipl_team or not ipl_team.strip():
        return []   # No ipl_team on player → RTM not applicable
    return [
        r for r in db.get_all_parts(aid)
        if (r["rtm_cards"] > 0
            and r["user_id"] != exclude_uid
            and not r["is_muted"]
            and r["rtm_team"].strip().lower() == ipl_team.strip().lower())
    ]


def validate_bid(row, player_row, bid_l: int, auction_row) -> Optional[str]:
    aid = row["auction_id"]
    if row["is_muted"]:
        return "Your team is muted and cannot bid."
    if row["purse"] < bid_l:
        return f"Not enough purse! You have {fmt(row['purse'], aid)} left."
    sq = json.loads(row["squad"])
    if len(sq) >= auction_row["max_players"]:
        return f"Squad full! Max {auction_row['max_players']} players."
    if bid_l < player_row["base_price"] and player_row["base_price"] > 0:
        return f"Min bid is {fmt(player_row['base_price'], aid)}."
    if live.current_bid > 0 and bid_l <= live.current_bid:
        return f"Bid must exceed current {fmt(live.current_bid, aid)}."
    # Minimum increment check
    if live.current_bid > 0 and live.min_increment > 0:
        if bid_l - live.current_bid < live.min_increment:
            return (
                f"Minimum raise is {fmt(live.min_increment, aid)}. "
                f"Bid at least {fmt(live.current_bid + live.min_increment, aid)}."
            )
    # Squad composition limits
    if live.squad_limits:
        lims = live.squad_limits
        p_role = player_row["role"]    # Bat / Bowl / AR / WK
        p_nat  = player_row["nationality"]  # Indian / Overseas

        # Build current squad role counts
        def _squad_counts(sq_ids):
            counts = {"Bat": 0, "Bowl": 0, "AR": 0, "WK": 0, "OS": 0, "Indian": 0}
            for pid in sq_ids:
                pr = db.get_player(int(pid))
                if not pr: continue
                counts[pr["role"]]  = counts.get(pr["role"], 0) + 1
                if pr["nationality"] == "Overseas":
                    counts["OS"] += 1
                else:
                    counts["Indian"] += 1
            return counts

        counts = _squad_counts(sq)
        remaining = auction_row["max_players"] - len(sq)  # slots left including this player

        # Overseas cap
        if p_nat == "Overseas" and "OS" in lims:
            os_max, os_min = lims["OS"]
            if counts["OS"] >= os_max:
                return f"Overseas limit reached! Max {os_max} overseas players per squad."

        # Role max cap
        role_key = p_role  # Bat, Bowl, AR, WK
        if role_key in lims:
            r_max, r_min = lims[role_key]
            if counts.get(role_key, 0) >= r_max:
                return f"Role limit reached! Max {r_max} {role_key} players per squad."

        # Check if buying this player would make min requirements for other roles impossible
        # (only warn if squad is nearly full)
        if remaining <= 3:
            for chk_role, (chk_max, chk_min) in lims.items():
                if chk_min <= 0 or chk_role == "OS":
                    continue
                chk_role_key = chk_role
                current_count = counts.get(chk_role_key, 0)
                if chk_role_key == p_role:
                    current_count += 1  # this player would add one
                slots_after = remaining - 1  # after buying this player
                if current_count + slots_after < chk_min:
                    return (
                        f"⚠️ Buying this player makes {chk_role} minimum ({chk_min}) "
                        f"impossible to reach with {slots_after} slots left."
                    )

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
        [InlineKeyboardButton("💼 My Purse", callback_data="my_purse")],
    ])


def _cr(lakhs: int) -> str:
    """Plain Crore/Lakh string (no currency symbol) for RTM templates."""
    if lakhs >= 100:
        c = lakhs / 100
        return f"{c:.1f}Cr" if c % 1 else f"{int(c)}Cr"
    return f"{lakhs}L"


# ── STEP 2: RTM CHALLENGE — bot asks Team A if they want to RTM ───
def rtm_check_text(player_row, eligible: list) -> str:
    """Sent to Team A after timer expires — USE RTM or PASS buttons."""
    ipl      = player_row["ipl_team"] or "N/A"
    aid      = player_row["auction_id"]
    # Show one team or list if multiple
    team_a   = ", ".join(f"*{md_safe(team_display(r))}*" for r in eligible)
    return (
        f"🔄 *RTM CHALLENGE!*\n"
        f"{'═'*20}\n\n"
        f"🏏 *{flag(player_row['nationality'])} {md_safe(player_row['name'])}*\n"
        f"💰 Winning Bid: *{fmt(live.current_bid, aid)}* by *{md_safe(live.highest_bidder_name)}*\n\n"
        f"🎴 {team_a}, do you want to exercise your *Right to Match*?\n\n"
        f"⏳ *{Config.RTM_OFFER_TIMER} seconds* to decide!"
    )


def rtm_challenge_keyboard(eligible: list, pid: int, orig_uid: int, orig_bid: int) -> InlineKeyboardMarkup:
    """USE RTM / PASS buttons — only eligible team(s) and admin can click."""
    rtm_uid = eligible[0]["user_id"] if eligible else 0
    # Embed all data in callback to avoid live-state dependency
    use_data  = f"rtm_use_btn|{pid}|{rtm_uid}|{orig_uid}|{orig_bid}"
    pass_data = f"rtm_pass_btn|{pid}|{orig_uid}|{orig_bid}"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ USE RTM", callback_data=use_data),
        InlineKeyboardButton("❌ PASS",    callback_data=pass_data),
    ]])


# ── STEP 3: Team B raise message (after Team A clicks USE RTM) ────
def rtm_activated_text(player_row) -> str:
    aid = player_row["auction_id"]
    return (
        f"📈 *TEAM B: RAISE THE STAKES!*\n"
        f"{'═'*20}\n\n"
        f"*{md_safe(live.rtm_team_name)}* wants to RTM!\n\n"
        f"🏏 *{md_safe(player_row['name'])}*\n"
        f"💰 Current bid: *{fmt(live.rtm_orig_bid, aid)}*\n\n"
        f"*{md_safe(live.rtm_orig_bidder_name)}*, you have one chance to raise.\n"
        f"Use /bid <amount> — must be higher than {fmt(live.rtm_orig_bid, aid)}.\n\n"
        f"⏳ *{Config.RTM_COUNTER_TIMER} seconds* to raise...\n"
        f"_(No raise = {md_safe(live.rtm_team_name)} wins at current price)_"
    )


# ── STEP 4: Final Decision — Team A MATCH or DECLINE ─────────────
def rtm_bid_raised_text(player_row, new_bid: int) -> str:
    diff = new_bid - live.rtm_orig_bid
    aid  = player_row["auction_id"]
    return (
        f"⚖️ *FINAL MATCH DECISION*\n"
        f"{'═'*20}\n\n"
        f"*{md_safe(live.rtm_orig_bidder_name)}* raised to *{fmt(new_bid, aid)}*!\n\n"
        f"🏏 *{md_safe(player_row['name'])}*\n"
        f"📈 Increase: +{fmt(diff, aid)}\n\n"
        f"*{md_safe(live.rtm_team_name)}*, will you match this final amount?\n\n"
        f"✅ *MATCH* → Player sold to you for {fmt(new_bid, aid)}\n"
        f"❌ *DECLINE* → Player goes to {md_safe(live.rtm_orig_bidder_name)} for {fmt(new_bid, aid)}\n\n"
        f"⏳ *{Config.RTM_DECISION_TIMER} seconds* to decide!\n"
        f"_Only {md_safe(live.rtm_team_name)} or Admin can click_"
    )


# ── STEP 4A: RTM ACCEPTED ─────────────────────────────────
def rtm_accepted_text(player_row, final_price: int, winner_name: str,
                      remaining_purse: int, squad_count: int,
                      original_team: str) -> str:
    import datetime
    ts  = datetime.datetime.now().strftime("%H:%M:%S")
    ipl = player_row["ipl_team"] or "N/A"
    return (
        f"✅ *RTM ACCEPTED - PLAYER SOLD!*\n"
        f"{'═'*20}\n\n"
        f"🏏 *{flag(player_row['nationality'])} {player_row['name']}* ({ipl})\n"
        f"🎯 {player_row['role']} | {player_row['nationality']}\n\n"
        f"💰 *Final Price:* ₹{_cr(final_price)}\n"
        f"🏆 *Winner:* *{winner_name}* 🎴 (via RTM)\n\n"
        f"📊 *Transaction:*\n"
        f"• Deducted: ₹{_cr(final_price)} from {winner_name}\n"
        f"• Remaining Purse: ₹{_cr(remaining_purse)}\n"
        f"• Squad: {squad_count} players\n\n"
        f"❌ {original_team} loses the bid\n\n"
        f"⏰ Sold at: {ts}"
    )


# ── STEP 4B: RTM DECLINED ─────────────────────────────────
def rtm_declined_text(player_row, original_bid: int, original_team: str,
                      remaining_purse: int, squad_count: int,
                      rtm_team: str) -> str:
    import datetime
    ts  = datetime.datetime.now().strftime("%H:%M:%S")
    ipl = player_row["ipl_team"] or "N/A"
    return (
        f"❌ *RTM DECLINED - ORIGINAL SALE!*\n"
        f"{'═'*20}\n\n"
        f"🏏 *{flag(player_row['nationality'])} {player_row['name']}* ({ipl})\n"
        f"🎯 {player_row['role']} | {player_row['nationality']}\n\n"
        f"💰 *Final Price:* ₹{_cr(original_bid)} (Original bid)\n"
        f"🏆 *Winner:* *{original_team}*\n\n"
        f"🎴 {rtm_team} declined to match the raised bid\n\n"
        f"📊 *Transaction:*\n"
        f"• Deducted: ₹{_cr(original_bid)} from {original_team}\n"
        f"• Remaining Purse: ₹{_cr(remaining_purse)}\n"
        f"• Squad: {squad_count} players\n\n"
        f"✅ {original_team} wins the player!\n\n"
        f"⏰ Sold at: {ts}"
    )


# ── STEP 5: NO RAISE — RTM wins at original bid ───────────
def rtm_no_raise_text(player_row, orig_bid: int, rtm_team: str,
                      rtm_cards_left: int, squad_count: int,
                      original_team: str) -> str:
    return (
        f"🎴 *RTM SUCCESSFUL - NO RAISE!*\n"
        f"{'═'*20}\n\n"
        f"🏏 *{flag(player_row['nationality'])} {player_row['name']}*\n\n"
        f"⏱️ {original_team} did not raise the bid\n\n"
        f"💰 *Final Price:* ₹{_cr(orig_bid)} (Original amount)\n"
        f"🏆 *Winner:* *{rtm_team}* 🎴 (via RTM)\n\n"
        f"📊 *Transaction:*\n"
        f"• Deducted: ₹{_cr(orig_bid)} from {rtm_team}\n"
        f"• RTM Cards Remaining: {rtm_cards_left}\n"
        f"• Squad: {squad_count} players\n\n"
        f"✅ Player acquired using RTM card!"
    )


# ── RTM SUMMARY (posted after any RTM sale) ──────────────
def rtm_summary_text(player_row, base_price: int, original_bid: int,
                     team_b: str, team_a: str, raised_bid: int,
                     accepted: Optional[bool], winner_team: str,
                     final_amount: int, rtm_cards_left: int) -> str:
    steps = (
        f"1️⃣ Base: ₹{_cr(base_price) if base_price else 'Open'}\n"
        f"2️⃣ Final Bid: ₹{_cr(original_bid)} by {team_b}\n"
        f"3️⃣ 🎴 RTM used by {team_a}\n"
    )
    if raised_bid and raised_bid != original_bid:
        steps += f"4️⃣ ⬆️ Raised to: ₹{_cr(raised_bid)} by {team_b}\n"
        outcome = "✅ Accepted" if accepted else "❌ Rejected"
        steps += f"5️⃣ {outcome} by {team_a}"
    else:
        steps += f"4️⃣ No raise — {team_a} wins at original bid"
    return (
        f"📋 *RTM SUMMARY - {player_row['name']}*\n"
        f"{'═'*20}\n\n"
        f"🔨 Auction Flow:\n{steps}\n\n"
        f"🏆 Winner: *{winner_team}*\n"
        f"💰 Paid: ₹{_cr(final_amount)}\n\n"
        f"📊 RTM Cards Left:\n{team_a}: {rtm_cards_left} card(s)"
    )


# ── ERROR: Invalid RTM command ────────────────────────────
def rtm_error_text(player_name: str, ipl_team: str, reason: str) -> str:
    return (
        f"❌ *RTM ERROR*\n"
        f"{'═'*20}\n\n"
        f"You cannot use RTM for {player_name}!\n\n"
        f"Reason: {reason}\n\n"
        f"💡 Check: `/myrtm` to see your available RTM cards"
    )


# ── ERROR: Wrong team trying to raise bid ─────────────────
def rtm_raise_error_text(original_team: str, your_team: str) -> str:
    return (
        f"❌ *RAISE BID ERROR*\n"
        f"{'═'*20}\n\n"
        f"Only *{original_team}* (current highest bidder) can raise the bid!\n\n"
        f"You are: {your_team}\n\n"
        f"💡 Wait for {original_team} to decide or let the RTM team win."
    )


# ── ERROR: RTM team must use buttons, not /bid ────────────
def rtm_wait_decision_text(rtm_team: str, new_bid: int, original_bid: int,
                            original_team: str, secs_left: int) -> str:
    return (
        f"❌ *WAIT FOR DECISION*\n"
        f"{'═'*20}\n\n"
        f"{rtm_team}, you must click the buttons!\n\n"
        f"The original bidder has raised to ₹{_cr(new_bid)}.\n\n"
        f"Click:\n"
        f"✅ *YES* to buy for ₹{_cr(new_bid)}\n"
        f"❌ *NO* to give up "
        f"(player goes to {original_team} for ₹{_cr(original_bid)})\n\n"
        f"⏱️ {secs_left}s remaining..."
    )


def rtm_ask_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ MATCH",   callback_data="rtm_yes"),
        InlineKeyboardButton("❌ DECLINE", callback_data="rtm_no"),
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
    """
    Countdown timer — 1s ticks.
    • Resets fully on every new bid (task cancelled + restarted in process_bid).
    • Half-time: sends one warning message.
    • Final 3-2-1: sends one new message per second.
    • On expiry: calls _mark_unsold or _check_rtm.
    • Fully wrapped in try/except — crashes are logged, SOLD still fires.
    """
    try:
        duration          = live.auto_sell_secs or Config.BID_TIMER
        half              = max(3, duration // 2)
        end               = _time.time() + duration
        live.timer_ends_at = end

        half_sent = False

        while True:
            await asyncio.sleep(1)

            if not live.active or live.paused:
                return
            if not live.current_player_id:
                return

            remaining = max(0, int(live.timer_ends_at - _time.time()))
            if remaining <= 0:
                break

            pr = db.get_player(live.current_player_id)
            if not pr:
                return

            aid    = pr["auction_id"]
            pname  = pr["name"]
            bid_s  = fmt(live.current_bid, aid) if live.current_bid > 0 else fmt(pr["base_price"], aid)
            leader = live.highest_bidder_name or "None"

            # ── 3-2-1 countdown ───────────────────────────
            if remaining <= 3:
                urgency = {3: "Hurry! Final bids!", 2: "Last chance!", 1: "CLOSING NOW!"}
                try:
                    await context.bot.send_message(
                        chat_id=live.chat_id,
                        text=(
                            f"⏱️ *{remaining} SECOND{'S' if remaining > 1 else ''} LEFT!*\n"
                            f"{'═'*20}\n\n"
                            f"🏏 {pname}\n"
                            f"💰 Current: *{bid_s}* — {leader}\n\n"
                            f"_{urgency.get(remaining, '')}_"
                        ),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass
                continue  # sleep 1s then next tick

            # ── Half-time warning (once per run) ──────────
            if not half_sent and remaining <= half:
                half_sent = True
                try:
                    await context.bot.send_message(
                        chat_id=live.chat_id,
                        text=(
                            f"⏱️ *{remaining} SECONDS LEFT!*\n"
                            f"{'═'*20}\n\n"
                            f"🏏 {pname}\n"
                            f"💰 Current: *{bid_s}* — {leader}\n\n"
                            f"{'Raise your bid now!' if live.current_bid > 0 else 'No bids yet — open bidding!'}"
                        ),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass

            # ── Edit bid message every 5s ──────────────────
            if remaining % 5 == 0 and live.last_bid_msg_id:
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

        # ── Timer expired ──────────────────────────────────
        if not live.active or not live.current_player_id:
            return

        pr = db.get_player(live.current_player_id)
        if not pr:
            return

        # Small delay so last countdown message renders before SOLD
        await asyncio.sleep(0.5)

        if live.current_bid == 0:
            await _mark_unsold(context, pr)
        else:
            await _check_rtm(context, pr)

    except asyncio.CancelledError:
        raise   # Expected — new bid resets the timer
    except Exception as exc:
        logger.error(f"bid_timer CRASHED: {exc}", exc_info=True)
        # Safety fallback — always try to finalize
        try:
            if live.current_player_id and live.active:
                pr = db.get_player(live.current_player_id)
                if pr:
                    if live.current_bid > 0 and live.highest_bidder_id:
                        await _check_rtm(context, pr)
                    else:
                        await _mark_unsold(context, pr)
        except Exception as e2:
            logger.error(f"bid_timer fallback failed: {e2}", exc_info=True)


async def _mark_unsold(context: ContextTypes.DEFAULT_TYPE, pr):
    db.set_player_status(pr["player_id"], "unsold")
    live.unsold_count += 1
    _set_last_sold(pr["player_id"], pr["name"], None, "", 0)
    live.current_player_id = None
    save_live_state()

    msg = await context.bot.send_message(
        chat_id=live.chat_id,
        text=f"❌ *{pr['name']}* goes *UNSOLD!* No bids received.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reauction_keyboard(),
    )
    live.reauction_msg_id = msg.message_id
    await _try_auto_next(context)


async def _check_rtm(context: ContextTypes.DEFAULT_TYPE, pr):
    """
    Called when bid timer expires with a bid.
    NEW FLOW: If eligible RTM teams exist, send RTM CHALLENGE with buttons.
    Team A clicks USE RTM (card deducted then) or PASS (card untouched).
    """
    ipl_team = (pr["ipl_team"] or "") if pr["ipl_team"] is not None else ""
    eligible = get_rtm_eligible(live.auction_id, live.highest_bidder_id, ipl_team)

    if not eligible:
        await _finalize(context, pr)
        return

    # Store who the challenge is for (first eligible team — or handle multiple below)
    live.rtm_state            = RTM_OFFERED
    live.rtm_team_id          = eligible[0]["user_id"]
    live.rtm_team_name        = bid_display(eligible[0])
    live.rtm_orig_bidder_id   = live.highest_bidder_id
    live.rtm_orig_bidder_name = live.highest_bidder_name
    live.rtm_orig_bid         = live.current_bid
    # Keep current_player_id alive so buttons can reference it

    msg = await context.bot.send_message(
        chat_id=live.chat_id,
        text=rtm_check_text(pr, eligible),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=rtm_challenge_keyboard(
            eligible,
            pid      = pr["player_id"],
            orig_uid = live.highest_bidder_id,
            orig_bid = live.current_bid,
        ),
    )
    live.rtm_offer_msg_id = msg.message_id
    live.timer_task = asyncio.create_task(_rtm_offer_timer(context))


async def _rtm_offer_timer(context: ContextTypes.DEFAULT_TYPE):
    """15s for Team A to click USE RTM or PASS. Expiry = auto-PASS."""
    end = _time.time() + Config.RTM_OFFER_TIMER
    while _time.time() < end:
        await asyncio.sleep(1)
        if live.rtm_state != RTM_OFFERED:
            return  # Team A already clicked
    if live.rtm_state == RTM_OFFERED:
        live.rtm_state = RTM_NONE
        # Remove buttons and show timeout message
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=live.chat_id,
                message_id=live.rtm_offer_msg_id,
                reply_markup=None,
            )
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=live.chat_id,
            text=(
                f"⏰ RTM window expired. "
                f"*{md_safe(live.rtm_orig_bidder_name)}* wins the player!"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        pr = db.get_player(live.current_player_id) if live.current_player_id else None
        if pr:
            await _finalize(context, pr)


async def _rtm_counter_timer(context: ContextTypes.DEFAULT_TYPE):
    """
    Step 3 — Wait RTM_COUNTER_TIMER for Team B (original bidder) to raise.
    If Team B does NOT raise: Team A auto-wins at the ORIGINAL price (no Step 4 needed).
    If Team B raises: move to Step 4 (MATCH / DECLINE).
    """
    end = _time.time() + Config.RTM_COUNTER_TIMER
    live.rtm_counter_ends_at = end
    while _time.time() < end:
        await asyncio.sleep(1)
        if live.rtm_state != RTM_ACTIVE:
            return  # Team B raised — state moved to RTM_COUNTER

    if live.rtm_state != RTM_ACTIVE:
        return

    # Team B did NOT raise → Team A auto-wins at original price
    orig_bid           = live.rtm_orig_bid
    rtm_uid            = live.rtm_team_id
    rtm_name           = live.rtm_team_name
    orig_bidder_name   = live.rtm_orig_bidder_name
    aid                = live.auction_id

    live.rtm_state           = RTM_NONE
    live.current_bid         = orig_bid
    live.highest_bidder_id   = rtm_uid
    live.highest_bidder_name = rtm_name

    pr = db.get_player(live.current_player_id)
    if not pr:
        return

    rtm_row    = db.get_part(aid, rtm_uid)
    cards_left = rtm_row["rtm_cards"] if rtm_row else 0
    sq_count   = len(json.loads(rtm_row["squad"])) + 1 if rtm_row else 0

    await context.bot.send_message(
        chat_id=live.chat_id,
        text=rtm_no_raise_text(
            pr,
            orig_bid       = orig_bid,
            rtm_team       = rtm_name,
            rtm_cards_left = cards_left,
            squad_count    = sq_count,
            original_team  = orig_bidder_name,
        ),
        parse_mode=ParseMode.MARKDOWN,
    )
    await _finalize(context, pr, rtm_accepted=True, rtm_no_raise=True)


async def _rtm_decision_timer(context: ContextTypes.DEFAULT_TYPE):
    """
    Auto-decline timer — waits RTM_DECISION_TIMER (15s) for RTM team YES/NO.
    If no action → auto-decline: original bidder wins at original bid.
    """
    end = _time.time() + Config.RTM_DECISION_TIMER
    live.rtm_decision_ends_at = end
    while _time.time() < end:
        await asyncio.sleep(1)
        if live.rtm_state != RTM_COUNTER:
            return
    if live.rtm_state != RTM_COUNTER:
        return
    # Auto-decline — refund RTM card since team didn't accept
    live.rtm_state           = RTM_NONE
    live.current_bid         = live.rtm_orig_bid
    live.highest_bidder_id   = live.rtm_orig_bidder_id
    live.highest_bidder_name = live.rtm_orig_bidder_name

    # Refund card
    if live.rtm_team_id and live.auction_id:
        try:
            db.cx.execute(
                "UPDATE participants SET rtm_cards=rtm_cards+1"
                " WHERE auction_id=? AND user_id=?",
                (live.auction_id, live.rtm_team_id)
            )
            db.cx.commit()
            logger.info(f"RTM card refunded to uid={live.rtm_team_id} (auto-decline timeout)")
        except Exception as e:
            logger.warning(f"RTM card refund failed: {e}")

    pr = db.get_player(live.current_player_id)
    if not pr:
        return

    winner_row = db.get_part(live.auction_id, live.rtm_orig_bidder_id)
    remaining  = (winner_row["purse"] - live.rtm_orig_bid) if winner_row else 0
    sq_count   = len(json.loads(winner_row["squad"])) + 1 if winner_row else 0

    await context.bot.send_message(
        chat_id=live.chat_id,
        text=rtm_declined_text(
            pr,
            original_bid   = live.rtm_orig_bid,
            original_team  = live.rtm_orig_bidder_name,
            remaining_purse= remaining,
            squad_count    = sq_count,
            rtm_team       = live.rtm_team_name,
        ),
        parse_mode=ParseMode.MARKDOWN,
    )
    await _finalize(context, pr, rtm_declined=True)


async def _finalize(context: ContextTypes.DEFAULT_TYPE, pr,
                    rtm_used: bool = False,
                    rtm_no_raise: bool = False,
                    rtm_declined: bool = False,
                    rtm_accepted: bool = False):
    """
    Persist the sale and send the appropriate SOLD message.
    rtm_accepted  → STEP 4A message
    rtm_declined  → STEP 4B message (already sent by timer or callback, skip duplicate)
    rtm_no_raise  → STEP 5 message already sent by _rtm_counter_timer
    else          → normal SOLD
    """
    if not live.highest_bidder_id:
        return

    aid          = live.auction_id
    winner_id    = live.highest_bidder_id
    winner_name  = live.highest_bidder_name
    final_price  = live.current_bid

    # Snapshot RTM fields before clearing live state
    _rtm_team     = live.rtm_team_name
    _rtm_team_id  = live.rtm_team_id
    _orig_bidder  = live.rtm_orig_bidder_name
    _orig_bid     = live.rtm_orig_bid
    _counter_bid  = live.rtm_counter_bid

    # Save undo snapshot BEFORE any DB writes so /undo can reverse this
    winner_row_pre = db.get_part(aid, winner_id)
    squad_pre      = winner_row_pre["squad"] if winner_row_pre else "[]"
    live.undo_snapshot = {
        "player_id":   pr["player_id"],
        "player_name": pr["name"],
        "winner_id":   winner_id,
        "winner_name": winner_name,
        "price":       final_price,
        "squad_pre":   squad_pre,       # squad BEFORE this player was added
        "purse_pre":   (winner_row_pre["purse"] if winner_row_pre else 0),
    }

    # Persist DB writes FIRST so remaining purse is correct
    db.set_player_status(pr["player_id"], "sold", winner_id, final_price, None, live.chat_id)
    db.deduct_purse(aid, winner_id, final_price)
    db.add_to_squad(aid, winner_id, pr["player_id"])
    db.record_bid(aid, winner_id, pr["player_id"], pr["name"], final_price, won=True)

    winner_row   = db.get_part(aid, winner_id)
    remaining    = winner_row["purse"] if winner_row else 0
    sq_count     = len(json.loads(winner_row["squad"])) if winner_row else 0

    # ── Choose sold message ────────────────────────────────
    import datetime
    any_rtm = rtm_accepted or rtm_declined or rtm_no_raise or rtm_used

    winner_uname = winner_row["username"] if winner_row else ""
    winner_at    = f"(@{winner_uname})" if winner_uname else ""
    sq_count     = len(json.loads(winner_row["squad"])) if winner_row else 0
    total_spent  = winner_row["total_spent"] if winner_row else 0
    ts           = ist_now()
    ar           = db.get_auction(aid)
    max_sq       = ar["max_players"] if ar else 25

    if rtm_accepted:
        sold_text = rtm_accepted_text(
            pr,
            final_price    = final_price,
            winner_name    = winner_name,
            remaining_purse= remaining,
            squad_count    = sq_count,
            original_team  = _orig_bidder,
        )
    elif rtm_declined or rtm_no_raise:
        sold_text = None
    else:
        sold_text = (
            f"✅ *SOLD!* ✅\n"
            f"{'═'*20}\n\n"
            f"🏏 *{flag(pr['nationality'])} {md_safe(pr['name'])}*\n"
            f"🎯 {md_safe(pr['role'])} | {md_safe(pr['nationality'])}\n\n"
            f"💰 *{md_safe(fmt(final_price, aid))}*\n"
            f"🏆 *{md_safe(winner_name)}* {md_safe(winner_at)}\n\n"
            f"📊 Stats:\n"
            f"• Purse Remaining: {md_safe(fmt(remaining, aid))}\n"
            f"• Players Bought: {sq_count}/{max_sq}\n"
            f"• Total Spent: {md_safe(fmt(total_spent, aid))}\n\n"
            f"⏰ Sold at: {ts}"
        )

    # Send with retry on rate-limit
    for attempt in range(3):
        try:
            if sold_text:
                msg = await context.bot.send_message(
                    chat_id=live.chat_id,
                    text=sold_text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reauction_keyboard(),
                )
            else:
                msg = await context.bot.send_message(
                    chat_id=live.chat_id,
                    text="➡️ Next player coming up...",
                    reply_markup=reauction_keyboard(),
                )
            break  # success
        except Exception as e:
            logger.warning(f"SOLD send attempt {attempt+1} failed: {e}")
            if attempt < 2:
                await asyncio.sleep(1 + attempt)
            else:
                logger.error("All SOLD send attempts failed!")
                # Create a dummy msg object to avoid crash
                msg = type("Msg", (), {"message_id": 0})()

    # ── RTM Summary ───────────────────────────────────────
    if any_rtm and _rtm_team:
        rtm_row  = db.get_part(aid, _rtm_team_id) if _rtm_team_id else None
        cards_l  = rtm_row["rtm_cards"] if rtm_row else 0
        raised   = _counter_bid if _counter_bid and _counter_bid != _orig_bid else 0
        accepted = True if rtm_accepted else (False if rtm_declined else None)
        try:
            await context.bot.send_message(
                chat_id=live.chat_id,
                text=rtm_summary_text(
                    pr,
                    base_price     = pr["base_price"] or 0,
                    original_bid   = _orig_bid,
                    team_b         = _orig_bidder,
                    team_a         = _rtm_team,
                    raised_bid     = raised,
                    accepted       = accepted,
                    winner_team    = winner_name,
                    final_amount   = final_price,
                    rtm_cards_left = cards_l,
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass

    # Save sold msg id
    db.cx.execute(
        "UPDATE players SET sold_msg_id=?, sold_chat_id=? WHERE player_id=?",
        (msg.message_id, live.chat_id, pr["player_id"]),
    )
    db.cx.commit()

    live.sold_count          += 1
    _set_last_sold(pr["player_id"], pr["name"], winner_id, winner_name, final_price)
    live.reauction_msg_id    = msg.message_id
    live.current_player_id   = None
    live.current_bid         = 0
    live.highest_bidder_id   = None
    live.highest_bidder_name = ""
    live.rtm_state           = RTM_NONE
    live.rtm_team_id         = None
    live.rtm_counter_bid     = 0
    save_live_state()
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
    save_live_state()  # queue shrunk by 1 — persist immediately

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

    async def err(m, md_v2: bool = False):
        if update.callback_query:
            plain = m.replace("*", "").replace("\\", "").replace("`", "")
            await update.callback_query.answer(plain[:200], show_alert=True)
        else:
            await update.message.reply_text(m, parse_mode=ParseMode.MARKDOWN)

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

    # Block current highest bidder from bidding again (outside RTM)
    if live.highest_bidder_id == uid and live.rtm_state not in (RTM_ACTIVE, RTM_COUNTER):
        await err(f"You are already the highest bidder at {fmt(live.current_bid, aid)}!")
        return

    # In RTM_ACTIVE — only the original bidder can raise
    if live.rtm_state == RTM_ACTIVE and uid != live.rtm_orig_bidder_id:
        row_caller = db.get_part(aid, uid)
        your_name  = team_display(row_caller) if row_caller else str(uid)
        if uid == live.rtm_team_id:
            secs_left = max(0, int((live.rtm_counter_ends_at or 0) - _time.time()))
            await err(rtm_wait_decision_text(
                live.rtm_team_name,
                live.rtm_counter_bid if live.rtm_counter_bid else live.rtm_orig_bid,
                live.rtm_orig_bid,
                live.rtm_orig_bidder_name,
                secs_left,
            ))
        else:
            await err(rtm_raise_error_text(live.rtm_orig_bidder_name, your_name))
        return

    # In RTM_COUNTER — RTM team must use YES/NO buttons, not /bid
    if live.rtm_state == RTM_COUNTER:
        row_caller = db.get_part(aid, uid)
        your_name  = team_display(row_caller) if row_caller else str(uid)
        if uid == live.rtm_team_id:
            secs_left = max(0, int((live.rtm_decision_ends_at or 0) - _time.time()))
            await err(rtm_wait_decision_text(
                live.rtm_team_name, live.rtm_counter_bid,
                live.rtm_orig_bid, live.rtm_orig_bidder_name, secs_left,
            ))
        else:
            await err(rtm_raise_error_text(live.rtm_orig_bidder_name, your_name))
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

    # RTM counter scenario — original bidder raises bid → Step 3
    if live.rtm_state == RTM_ACTIVE and uid == live.rtm_orig_bidder_id:
        # Bid MUST be strictly higher than current (original) bid
        if bid_l <= live.rtm_orig_bid:
            await err(
                f"Your raise must be *higher* than the current bid "
                f"({fmt(live.rtm_orig_bid, aid)}). "
                f"Or wait — {md_safe(live.rtm_team_name)} will win at current price."
            )
            return
        if live.timer_task and not live.timer_task.done():
            live.timer_task.cancel()

        live.rtm_state       = RTM_COUNTER
        live.rtm_counter_bid = bid_l

        # Embed all data in callback_data so YES/NO never depend on live state
        # Format: y|pid|rtm_uid|final_price|orig_uid|orig_price (max ~50 chars, Telegram limit 64)
        pid_val  = pr["player_id"]
        rtm_uid  = live.rtm_team_id
        orig_uid = live.rtm_orig_bidder_id
        orig_bid = live.rtm_orig_bid
        yes_data = f"ry|{pid_val}|{rtm_uid}|{bid_l}|{orig_uid}|{orig_bid}"
        no_data  = f"rn|{pid_val}|{rtm_uid}|{bid_l}|{orig_uid}|{orig_bid}"

        ask_msg = await context.bot.send_message(
            chat_id=live.chat_id,
            text=rtm_bid_raised_text(pr, bid_l),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ MATCH",   callback_data=yes_data),
                InlineKeyboardButton("❌ DECLINE", callback_data=no_data),
            ]]),
        )
        live.rtm_msg_id = ask_msg.message_id
        live.timer_task = asyncio.create_task(_rtm_decision_timer(context))

        if update.callback_query:
            await update.callback_query.answer(f"Bid raised to {fmt(bid_l, aid)}!")
        else:
            await update.message.reply_text(
                f"⬆️ Bid raised to *{fmt(bid_l, aid)}* — "
                f"waiting for *{live.rtm_team_name}* to decide!",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    # ── Normal bid ────────────────────────────────────────
    prev_name                = live.highest_bidder_name
    live.current_bid         = bid_l
    live.highest_bidder_id   = uid
    live.highest_bidder_name = bid_display(part)

    db.record_bid(aid, uid, pr["player_id"], pr["name"], bid_l, won=False)

    # Cancel existing timer and start fresh — every bid resets to full duration
    if live.timer_task and not live.timer_task.done():
        live.timer_task.cancel()
        try:
            await asyncio.shield(asyncio.sleep(0))  # yield so cancel propagates
        except Exception:
            pass
    live.timer_ends_at = None
    live.timer_task = asyncio.create_task(bid_timer(context))

    duration = live.auto_sell_secs or Config.BID_TIMER
    outbid   = f"⬆️ Outbids: {prev_name}" if prev_name and prev_name != bid_display(part) else "🎯 Opening bid!"

    new_msg = await context.bot.send_message(
        chat_id=live.chat_id,
        text=(
            f"💥 *New Bid*\n{'─'*28}\n"
            f"Player: *{pr['name']}*\n"
            f"Amount: *{fmt(bid_l,aid)}*\n"
            f"By: *{bid_display(part)}*\n"
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
        "/rtm — Use RTM card (after bid timer expires, if you hold a matching card)\n"
        "/myrtm — Check your RTM cards & assigned team\n"
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
            "/auctionowners — List all owners & usernames\n"
            "/soldplayers — Sold list with jump links\n"
            "/unsoldplayers — Unsold list\n"
            "/setrtm @user <cards> <TEAM> — Assign RTM (TEAM = ipl_team in player list)\n"
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

    # Auto-restore if bot restarted and lost live state
    if not live.auction_id:
        restore_live_state()

    if not live.auction_id:
        await update.message.reply_text("No auction session. Use /create\\_auction to start.",
                                        parse_mode=ParseMode.MARKDOWN)
        return

    aid = live.auction_id
    ar  = db.get_auction(aid)

    # Auction exists but bot may have restarted — show state + guidance
    if not live.active:
        ar_status = ar["status"] if ar else "unknown"
        if ar_status == "active":
            # Was active when bot restarted — guide admin to resume
            await update.message.reply_text(
                f"⏸ *{md_safe(live.auction_name)}* — PAUSED / BOT RESTARTED\n{'─'*28}\n\n"
                f"The auction was active but the bot restarted.\n\n"
                f"✅ State has been restored:\n"
                f"  Sold: {live.sold_count} | Unsold: {live.unsold_count}\n"
                f"  Queue: {len(live.player_queue)} players remaining\n\n"
                f"👉 Use /resumeauction to continue.\n"
                f"   Then /next for the next player.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text(
                f"No active auction.\n"
                f"Auction *{md_safe(live.auction_name)}* — status: {ar_status}\n"
                f"Use /startauction to begin.",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    if live.paused:
        await update.message.reply_text(
            f"⏸ *{md_safe(live.auction_name)}* — PAUSED\n{'─'*28}\n"
            f"Sold: {live.sold_count} | Unsold: {live.unsold_count} | Queue: {len(live.player_queue)}\n\n"
            f"Use /resumeauction to continue.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if not live.current_player_id:
        await update.message.reply_text(
            f"🏏 *{md_safe(live.auction_name)}* — RUNNING\n{'─'*28}\n"
            f"Waiting for next player.\n"
            f"Sold: {live.sold_count} | Unsold: {live.unsold_count} | Queue: {len(live.player_queue)}\n\n"
            f"Use /next to continue.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    pr  = db.get_player(live.current_player_id)
    rem = max(0, int(live.timer_ends_at - _time.time())) if live.timer_ends_at else None

    rtm_note = {
        RTM_OFFERED: f"\n🎴 RTM window open — teams with cards can use /rtm",
        RTM_ACTIVE:  f"\n🎴 RTM active — waiting for {md_safe(live.rtm_orig_bidder_name)} to counter",
        RTM_COUNTER: f"\n🎴 Counter bid — waiting for {md_safe(live.rtm_team_name)} to accept/decline",
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
    """Manual /rtm command — invoke during the RTM window."""
    await _reg(update.effective_user)
    uid = eff_uid(update.effective_user.id)
    await _handle_rtm_use(update, context, uid)


async def cmd_my_rtm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/myrtm — show this team's RTM card status."""
    await _reg(update.effective_user)
    aid = live.auction_id
    if not aid:
        await update.message.reply_text("No active auction.")
        return
    uid = eff_uid(update.effective_user.id)
    row = db.get_part(aid, uid)
    if not row:
        await update.message.reply_text("You are not registered in this auction.")
        return
    cards = row["rtm_cards"]
    team  = row["rtm_team"] or "N/A"
    if cards == 0:
        txt = (
            f"🎴 *No RTM Cards Remaining*\n{'─'*24}\n\n"
            f"Your assigned IPL team was: *{team}*\n"
            f"All RTM cards have been used or none were assigned."
        )
    else:
        txt = (
            f"🎴 *Your RTM Cards*\n{'─'*24}\n\n"
            f"Cards remaining: *{cards}*\n"
            f"Assigned IPL team: *{team}*\n\n"
            f"When a *{team}* player is auctioned and the bid timer expires,\n"
            f"you will be notified. Then use `/rtm` to activate your card!"
        )
    await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)


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

    def _uname(uid: int) -> str:
        u = db.get_user(uid)
        if not u:
            return f"ID:{uid}"
        if u["username"]:
            return md_safe(f"@{u['username']}")
        return md_safe(u["first_name"] or f"ID:{uid}")

    lines = [f"👥 *Auction Owners — {md_safe(live.auction_name)}*\n{'─'*28}"]
    for i, r in enumerate(parts, 1):
        main_uname = _uname(r["user_id"])
        co = db.get_co_owners(aid, r["user_id"])
        if co:
            co_names = [_uname(c["linked_user_id"]) for c in co]
            co_str   = ", " + ", ".join(co_names)
        else:
            co_str = ""
        rtm_info = (
            f"  🎴 RTM: {md_safe(r['rtm_team'])} ×{r['rtm_cards']}"
            if r["rtm_cards"] > 0 and r["rtm_team"]
            else ""
        )
        lines.append(
            f"{i}. *{md_safe(r['team_name'])}* — {main_uname}{co_str}{rtm_info}"
        )

    text = "\n".join(lines)
    try:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        # Fallback: strip all markdown if still fails
        plain = text.replace("*", "").replace("_", "").replace("\\", "")
        await update.message.reply_text(plain)


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

    header = f"✅ *Sold Players* ({len(sold)})\n{'─'*28}"
    lines  = [header]

    for i, p in enumerate(sold, 1):
        buyer      = db.get_part(aid, p["sold_to"]) if p["sold_to"] else None
        buyer_name = md_safe(team_display(buyer)) if buyer else "?"
        price_str  = md_safe(fmt(p["sold_price"], aid)) if p["sold_price"] else "?"
        pname      = md_safe(p["name"])

        # Build jump link if we have the message reference
        if p["sold_msg_id"] and p["sold_chat_id"]:
            link = jump_link(p["sold_chat_id"], p["sold_msg_id"])
            # Use plain URL in parentheses — works even if Markdown inline links fail
            line = f"{i}. *{pname}* → {buyer_name} — *{price_str}*\n   [↗️ Jump to message]({link})"
        else:
            line = f"{i}. *{pname}* → {buyer_name} — *{price_str}*"

        lines.append(line)

    async def _send_chunk(chunk_lines: list):
        text = "\n".join(chunk_lines)
        try:
            await update.message.reply_text(
                text, parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True
            )
        except Exception:
            # Fallback: strip Markdown if parse fails
            plain = text.replace("*", "").replace("_", "").replace("\\", "")
            await update.message.reply_text(plain, disable_web_page_preview=True)

    # Split into chunks ≤ 3500 chars
    chunk: list = []
    for line in lines:
        if chunk and len("\n".join(chunk + [line])) > 3500:
            await _send_chunk(chunk)
            chunk = [line]
        else:
            chunk.append(line)
    if chunk:
        await _send_chunk(chunk)


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



# ── UNDO LAST SALE ────────────────────────────────────────

async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/undo — Reverse the last SOLD transaction."""
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.undo_snapshot:
        await update.message.reply_text("Nothing to undo.")
        return

    snap = live.undo_snapshot
    aid  = live.auction_id
    pid  = snap["player_id"]
    wid  = snap["winner_id"]

    # Reverse: restore player to available
    db.cx.execute(
        "UPDATE players SET status='available', sold_to=NULL, "
        "sold_price=NULL, sold_msg_id=NULL, sold_chat_id=NULL WHERE player_id=?",
        (pid,)
    )
    # Restore purse
    db.cx.execute(
        "UPDATE participants SET purse=?, squad=? WHERE auction_id=? AND user_id=?",
        (snap["purse_pre"], snap["squad_pre"], aid, wid)
    )
    # Recalculate total_spent
    db.cx.execute(
        "UPDATE participants SET total_spent=("
        "  SELECT COALESCE(SUM(b.bid_amount),0) FROM bid_history b "
        "  WHERE b.auction_id=? AND b.user_id=? AND b.won=1"
        ") WHERE auction_id=? AND user_id=?",
        (aid, wid, aid, wid)
    )
    # Remove winning bid from history
    db.cx.execute(
        "DELETE FROM bid_history WHERE auction_id=? AND user_id=? AND player_id=? AND won=1",
        (aid, wid, pid)
    )
    db.cx.commit()

    # Add player back to front of queue
    fresh = db.get_player(pid)
    if fresh:
        live.player_queue.insert(0, fresh)
    live.sold_count = max(0, live.sold_count - 1)
    live.undo_snapshot = None  # can only undo once
    save_live_state()

    await update.message.reply_text(
        f"↩️ *Undone!*\n"
        f"*{md_safe(snap['player_name'])}* returned to queue.\n"
        f"*{md_safe(snap['winner_name'])}* purse restored by {fmt(snap['price'], aid)}.\n\n"
        f"Use /next to re-auction this player.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── SET SQUAD LIMITS ─────────────────────────────────────

async def cmd_set_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setlimit OS_max-OS_min,BAT_max-BAT_min,BOWL_max-BOWL_min,ALR_max-ALR_min,WK_max-WK_min
    
    Example: /setlimit 8-4,12-5,8-3,4-2,2-1
    Sets: Overseas max=8 min=4, Bat max=12 min=5, Bowl max=8 min=3, AR max=4 min=2, WK max=2 min=1
    Use 0-0 to leave a category unlimited.
    /setlimit show — display current limits
    /setlimit clear — remove all limits
    """
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return

    def _fmt_limits() -> str:
        if not live.squad_limits:
            return "No limits set."
        lines = []
        labels = {"OS": "🌏 Overseas", "Bat": "🏏 Batsmen", "Bowl": "🎳 Bowlers",
                  "AR": "⚡ All-Rounders", "WK": "🧤 Wicket-Keepers"}
        for k, lbl in labels.items():
            if k in live.squad_limits:
                mx, mn = live.squad_limits[k]
                lines.append(f"{lbl}: min {mn} — max {mx}")
        return "\n".join(lines) if lines else "No limits set."

    if not context.args:
        await update.message.reply_text(
            f"⚖️ *Squad Composition Limits*\n{'─'*28}\n"
            f"{_fmt_limits()}\n\n"
            f"Usage:\n"
            f"`/setlimit OS-BAT-BOWL-ALR-WK`\n"
            f"Each value: `max-min` (use 0-0 = no limit)\n\n"
            f"Example: `/setlimit 8-4,12-5,8-3,4-2,2-1`\n"
            f"→ Overseas: 4–8 | Bat: 5–12 | Bowl: 3–8 | AR: 2–4 | WK: 1–2",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    arg = context.args[0].lower()
    if arg == "show":
        await update.message.reply_text(
            f"⚖️ *Current Limits*\n{'─'*28}\n{_fmt_limits()}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    if arg == "clear":
        live.squad_limits = {}
        save_live_state()
        await update.message.reply_text("✅ All squad limits cleared.")
        return

    parts = context.args[0].split(",")
    if len(parts) != 5:
        await update.message.reply_text(
            "❌ Need exactly 5 values separated by commas:\n"
            "`/setlimit OS,BAT,BOWL,ALR,WK`\n"
            "Each as `max-min`, e.g. `8-4`\n"
            "Use `0-0` to leave unlimited.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    keys = ["OS", "Bat", "Bowl", "AR", "WK"]
    limits = {}
    for key, val in zip(keys, parts):
        val = val.strip()
        if val in ("0-0", "0"):
            continue  # unlimited
        if "-" not in val:
            await update.message.reply_text(f"❌ Bad format for {key}: `{val}`. Use `max-min` e.g. `8-4`.")
            return
        try:
            mx, mn = val.split("-", 1)
            mx, mn = int(mx.strip()), int(mn.strip())
            if mx < mn:
                await update.message.reply_text(f"❌ {key}: max ({mx}) must be ≥ min ({mn})")
                return
            if mx > 0:
                limits[key] = (mx, mn)
        except ValueError:
            await update.message.reply_text(f"❌ Bad numbers for {key}: `{val}`")
            return

    live.squad_limits = limits
    save_live_state()
    await update.message.reply_text(
        f"✅ *Squad Limits Set*\n{'─'*28}\n{_fmt_limits()}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── SET BID INCREMENT ─────────────────────────────────────

async def cmd_set_increment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setincrement <amount> — Set minimum bid raise (e.g. 25l, 1cr)."""
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text(
            f"📈 Min Increment: *{fmt(live.min_increment, live.auction_id)}*\n"
            f"Usage: /setincrement <amount>\n"
            f"Examples: /setincrement 25l  /setincrement 1cr  /setincrement 0 (off)",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    v = parse_price(context.args[0])
    if v is None:
        await update.message.reply_text("Invalid amount.")
        return
    live.min_increment = v
    save_live_state()
    label = fmt(v, live.auction_id) if v > 0 else "off"
    await update.message.reply_text(
        f"✅ Minimum bid increment: *{label}*",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── SET SQUAD SIZE MID-AUCTION ────────────────────────────

async def cmd_set_squad_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/setsquadlimit <min> <max> — Change squad size limits mid-auction."""
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.auction_id:
        await update.message.reply_text("No active auction.")
        return
    if len(context.args) < 2:
        ar = db.get_auction(live.auction_id)
        await update.message.reply_text(
            f"Squad size: min={ar['min_players']} max={ar['max_players']}\n"
            f"Usage: /setsquadlimit <min> <max>",
        )
        return
    try:
        mn, mx = int(context.args[0]), int(context.args[1])
        if mn > mx or mn < 1 or mx > 50:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Invalid. Example: /setsquadlimit 11 25")
        return
    db.cx.execute(
        "UPDATE auctions SET min_players=?, max_players=? WHERE auction_id=?",
        (mn, mx, live.auction_id)
    )
    db.cx.commit()
    await update.message.reply_text(
        f"✅ Squad limits updated: min *{mn}* — max *{mx}*",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── FIND PLAYER ───────────────────────────────────────────

async def cmd_find_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/findplayer <name|role|team> — Search players by name, role, or team."""
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.auction_id:
        await update.message.reply_text("No active auction.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /findplayer <name or role or team>")
        return

    query = " ".join(context.args).strip().lower()
    aid   = live.auction_id
    rows  = db.cx.execute(
        "SELECT p.*, pt.team_name as buyer_team FROM players p "
        "LEFT JOIN participants pt ON pt.auction_id=p.auction_id AND pt.user_id=p.sold_to "
        "WHERE p.auction_id=? AND ("
        "  LOWER(p.name) LIKE ? OR LOWER(p.role) LIKE ? OR "
        "  LOWER(p.ipl_team) LIKE ? OR LOWER(p.tier) LIKE ? OR "
        "  LOWER(p.nationality) LIKE ?"
        ") ORDER BY p.status, p.name LIMIT 20",
        (aid, f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%")
    ).fetchall()

    if not rows:
        await update.message.reply_text(f"No players found matching '{query}'")
        return

    lines = [f"🔍 *Results for '{query}'* ({len(rows)})\n{'─'*28}"]
    for r in rows:
        status_icon = {"sold": "✅", "unsold": "❌", "available": "🔵"}.get(r["status"], "❓")
        price_info  = f" → {fmt(r['sold_price'], aid)} ({r['buyer_team'] or '?'})" if r["status"] == "sold" else ""
        in_queue    = " _(in queue)_" if any(
            (q["player_id"] if hasattr(q, "keys") else q) == r["player_id"]
            for q in live.player_queue
        ) else ""
        lines.append(
            f"{status_icon} *{md_safe(r['name'])}* — {r['role']} | {r['ipl_team'] or 'N/A'}"
            f" | {fmt(r['base_price'], aid)}{price_info}{in_queue}"
        )

    text = "\n".join(lines)
    try:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await update.message.reply_text(text.replace("*","").replace("_",""))


# ── PLAYER CARD ───────────────────────────────────────────

async def cmd_player_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/playercard <name> — Show full player profile."""
    await _reg(update.effective_user)
    if not live.auction_id:
        await update.message.reply_text("No active auction.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /playercard <player name>")
        return

    query = " ".join(context.args).strip()
    aid   = live.auction_id
    rows  = db.cx.execute(
        "SELECT * FROM players WHERE auction_id=? AND LOWER(name) LIKE ?",
        (aid, f"%{query.lower()}%")
    ).fetchall()

    if not rows:
        await update.message.reply_text(f"Player '{query}' not found.")
        return

    r = rows[0]
    status_icon = {"sold": "✅ SOLD", "unsold": "❌ UNSOLD", "available": "🔵 Available"}.get(
        r["status"], r["status"])
    buyer_line = ""
    if r["status"] == "sold" and r["sold_to"]:
        pt = db.get_part(aid, r["sold_to"])
        buyer_name = pt["team_name"] if pt else "Unknown"
        buyer_line = f"\n🏆 *Sold to:* {md_safe(buyer_name)} at {fmt(r['sold_price'], aid)}"

    in_queue_pos = next(
        (i+1 for i, q in enumerate(live.player_queue)
         if (q["player_id"] if hasattr(q, "keys") else q) == r["player_id"]),
        None
    )
    queue_line = f"\n📋 Queue position: #{in_queue_pos}" if in_queue_pos else ""

    text = (
        f"🏏 *{flag(r['nationality'])} {md_safe(r['name'])}*\n"
        f"{'─'*28}\n"
        f"🎯 Role: *{r['role']}* | {r['nationality']}\n"
        f"🏟 Prev Team: *{r['ipl_team'] or 'None'}*\n"
        f"⭐ Tier: *{r['tier']}*\n"
        f"💰 Base Price: *{fmt(r['base_price'], aid) if r['base_price'] else 'Open'}*\n"
        f"📊 Status: *{status_icon}*"
        f"{buyer_line}{queue_line}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ── MY STATS ─────────────────────────────────────────────

async def cmd_my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/mystats — Personal auction stats for team owners."""
    await _reg(update.effective_user)
    uid = eff_uid(update.effective_user.id)
    aid = live.auction_id
    if not aid:
        await update.message.reply_text("No active auction.")
        return

    part = db.get_part(aid, uid)
    if not part:
        await update.message.reply_text("You are not registered in this auction.")
        return

    sq   = json.loads(part["squad"])
    bids = db.cx.execute(
        "SELECT * FROM bid_history WHERE auction_id=? AND user_id=? ORDER BY ts DESC",
        (aid, uid)
    ).fetchall()

    won_bids  = [b for b in bids if b["won"]]
    lost_bids = [b for b in bids if not b["won"]]
    sq_players = [db.get_player(int(pid)) for pid in sq]
    sq_players = [p for p in sq_players if p]

    # Role breakdown
    role_counts = {}
    for p in sq_players:
        role_counts[p["role"]] = role_counts.get(p["role"], 0) + 1

    os_count  = sum(1 for p in sq_players if p["nationality"] == "Overseas")
    ind_count = sum(1 for p in sq_players if p["nationality"] == "Indian")

    ar = db.get_auction(aid)
    max_sq = ar["max_players"] if ar else 25

    role_line = " | ".join(f"{k}:{v}" for k, v in role_counts.items()) or "None"

    text = (
        f"📊 *My Stats — {md_safe(part['team_name'])}*\n"
        f"{'─'*28}\n\n"
        f"💰 *Purse:* {fmt(part['purse'], aid)} remaining\n"
        f"💸 *Total Spent:* {fmt(part['total_spent'], aid)}\n\n"
        f"🏏 *Squad:* {len(sq)}/{max_sq} players\n"
        f"  🇮🇳 Indian: {ind_count}  🌏 Overseas: {os_count}\n"
        f"  Roles: {role_line}\n\n"
        f"📋 *Bid History:*\n"
        f"  🟢 Won: {len(won_bids)} players\n"
        f"  🔴 Lost bids: {len(lost_bids)}\n"
        f"  📈 Total bids placed: {len(bids)}\n"
    )
    if won_bids:
        avg = sum(b["bid_amount"] for b in won_bids) // len(won_bids)
        text += f"  💰 Avg price paid: {fmt(avg, aid)}\n"
    if sq_players:
        most_expensive = max(sq_players, key=lambda p: p["sold_price"] or 0)
        text += f"\n🌟 *Most expensive:* {md_safe(most_expensive['name'])} — {fmt(most_expensive['sold_price'] or 0, aid)}"

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ── ANNOUNCE ─────────────────────────────────────────────

async def cmd_announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/announce <message> — Broadcast a formatted message to the group."""
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /announce <your message>")
        return
    msg_text = " ".join(context.args)
    target   = live.chat_id or update.effective_chat.id
    await context.bot.send_message(
        chat_id=target,
        text=f"📢 *ANNOUNCEMENT*\n{'─'*28}\n{msg_text}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── TRANSFER PLAYER ───────────────────────────────────────

async def cmd_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/transfer @from @to <player name> — Trade a sold player between teams."""
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.auction_id:
        await update.message.reply_text("No active auction.")
        return
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: /transfer @FromTeam @ToTeam <Player Name>\n"
            "Example: /transfer @team1 @team2 Virat Kohli"
        )
        return

    aid      = live.auction_id
    from_arg = context.args[0].lstrip("@")
    to_arg   = context.args[1].lstrip("@")
    player_q = " ".join(context.args[2:]).strip()

    # Resolve teams by username or team_name
    parts = db.get_all_parts(aid)
    def _find_team(q):
        q_lo = q.lower()
        for r in parts:
            u = db.get_user(r["user_id"])
            un = (u["username"] or "").lower() if u else ""
            if un == q_lo or r["team_name"].lower() == q_lo:
                return r
        return None

    from_row = _find_team(from_arg)
    to_row   = _find_team(to_arg)
    if not from_row:
        await update.message.reply_text(f"Team '{from_arg}' not found.")
        return
    if not to_row:
        await update.message.reply_text(f"Team '{to_arg}' not found.")
        return

    # Find the player in from_team's squad
    from_sq = json.loads(from_row["squad"])
    player_found = None
    for pid in from_sq:
        p = db.get_player(int(pid))
        if p and player_q.lower() in p["name"].lower():
            player_found = p
            break

    if not player_found:
        await update.message.reply_text(
            f"'{player_q}' not found in {from_row['team_name']}'s squad.\n"
            f"Use /squad @{from_arg} to see their players."
        )
        return

    to_sq = json.loads(to_row["squad"])
    ar    = db.get_auction(aid)
    if len(to_sq) >= ar["max_players"]:
        await update.message.reply_text(f"{to_row['team_name']}'s squad is full (max {ar['max_players']}).")
        return

    price = player_found["sold_price"] or 0

    # Perform transfer
    # Remove from source squad
    new_from_sq = [p for p in from_sq if int(p) != player_found["player_id"]]
    # Add to destination squad
    to_sq.append(player_found["player_id"])

    db.cx.execute(
        "UPDATE participants SET squad=?, purse=purse+?, total_spent=MAX(0,total_spent-?) "
        "WHERE auction_id=? AND user_id=?",
        (json.dumps(new_from_sq), price, price, aid, from_row["user_id"])
    )
    db.cx.execute(
        "UPDATE participants SET squad=?, purse=MAX(0,purse-?), total_spent=total_spent+? "
        "WHERE auction_id=? AND user_id=?",
        (json.dumps(to_sq), price, price, aid, to_row["user_id"])
    )
    db.cx.execute(
        "UPDATE players SET sold_to=? WHERE player_id=?",
        (to_row["user_id"], player_found["player_id"])
    )
    db.cx.commit()

    await update.message.reply_text(
        f"🔄 *Transfer Complete!*\n{'─'*28}\n"
        f"🏏 *{md_safe(player_found['name'])}*\n"
        f"From: *{md_safe(from_row['team_name'])}*\n"
        f"To: *{md_safe(to_row['team_name'])}*\n"
        f"Price adjusted: {fmt(price, aid)}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── SET RTM ───────────────────────────────────────────────

async def cmd_set_rtm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if len(context.args) < 3 or not live.auction_id:
        await update.message.reply_text(
            "Usage: /setrtm @user <cards> <TEAM>\n"
            "• <cards> = number of RTM uses\n"
            "• <TEAM>  = IPL team name as used in /addplayer (e.g. RCB, CSK, MI)\n\n"
            "Example: /setrtm @dhoni 2 CSK\n"
            "         /setrtm @virat 1 RCB"
        )
        return

    uid = db.resolve_uid(context.args[0])
    if not uid:
        await update.message.reply_text("User not found. They must /start first.")
        return

    try:
        cards = int(context.args[1])
        if cards < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Cards must be a non-negative integer.")
        return

    team = " ".join(context.args[2:]).strip()
    db.set_rtm(live.auction_id, uid, cards, team)
    row  = db.get_part(live.auction_id, uid)
    name = team_display(row) if row else str(uid)
    await update.message.reply_text(
        f"✅ *{name}* assigned *{cards}* RTM card(s) for IPL team *{team}*.\n\n"
        f"They can use /rtm after a *{team}* player's bid timer expires.",
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
    # Persist chat_id in DB so Layer 3 restore always knows where to message
    db.cx.execute("UPDATE auctions SET chat_id=? WHERE auction_id=?",
                  (live.chat_id, live.auction_id))
    db.cx.commit()
    save_live_state()

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
    save_live_state()
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
    if live.timer_task and not live.timer_task.done():
        live.timer_task.cancel()
    live.paused = True
    save_live_state()
    await update.message.reply_text("⏸ *Auction PAUSED.* Use /resumeauction to continue.",
                                    parse_mode=ParseMode.MARKDOWN)


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        return

    # If live state is missing, try all restore layers
    if not live.auction_id:
        # Try async restore first (uses Telegram channel)
        restored = await restore_live_state_async(update.get_bot())
        if not restored:
            # Try sync DB layer
            restored = restore_live_state()
        if not restored:
            await update.message.reply_text(
                "❌ *No auction state found.*\n\n"
                "The bot restarted and could not recover the auction.\n\n"
                "📌 *What to do:*\n"
                "• If you have STATE\\_CHANNEL\\_ID set, make sure the bot is "
                "Admin with *Pin Messages* permission in that channel.\n"
                "• Otherwise, the database was wiped by Render — "
                "you need to recreate the auction with /create\\_auction.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    # Check if DB is wiped (auction_id restored from Telegram but DB gone)
    ar = db.get_auction(live.auction_id)
    db_missing = (ar is None)

    live.paused  = False
    live.active  = True
    live.chat_id = update.effective_chat.id

    # Update the chat_id in DB only if DB exists
    if ar:
        db.cx.execute("UPDATE auctions SET chat_id=? WHERE auction_id=?",
                      (live.chat_id, live.auction_id))
        db.cx.commit()

    save_live_state()
    queued = len(live.player_queue)

    if db_missing:
        await update.message.reply_text(
            f"⚠️ *Partial restore from Telegram channel*\n{'─'*28}\n"
            f"🏏 *{md_safe(live.auction_name)}*\n\n"
            f"State info recovered:\n"
            f"✅ Sold: {live.sold_count} | ❌ Unsold: {live.unsold_count}\n\n"
            f"⚠️ *Database was wiped by Render restart.*\n"
            f"Players and participant data is lost — "
            f"you need to recreate the auction.\n\n"
            f"To prevent this in future: add a Render persistent disk ($1/month) "
            f"and set DATABASE\\_PATH to point to it.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text(
        f"▶️ *Auction RESUMED!*\n{'─'*28}\n"
        f"🏏 *{md_safe(live.auction_name)}*\n\n"
        f"✅ Sold: {live.sold_count} | ❌ Unsold: {live.unsold_count}\n"
        f"📋 Queue: {queued} players remaining\n\n"
        f"Use /next to continue.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_auction_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Full auction summary — admin command."""
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    aid = live.auction_id
    if not aid:
        await update.message.reply_text("No auction session.")
        return

    sold_list   = db.get_sold(aid)
    unsold_list = db.get_unsold(aid)
    parts       = sorted(db.get_all_parts(aid), key=lambda r: r["total_spent"], reverse=True)
    total_purse = sum(r["total_spent"] for r in parts)

    lines = [
        f"📊 *{live.auction_name} — Full Summary*\n{'─'*28}\n"
        f"✅ Sold: *{len(sold_list)}*  ❌ Unsold: *{len(unsold_list)}*\n"
        f"💰 Total Purse Used: *{fmt(total_purse, aid)}*\n",
    ]

    # Sold players list
    lines.append(f"*Sold Players ({len(sold_list)}):*")
    for p in sold_list:
        buyer = db.get_part(aid, p["sold_to"]) if p["sold_to"] else None
        bname = team_display(buyer) if buyer else "?"
        lines.append(f"  • {p['name']} → {bname} — {fmt(p['sold_price'] or 0, aid)}")

    # Unsold players list
    if unsold_list:
        lines.append(f"\n*Unsold Players ({len(unsold_list)}):*")
        for p in unsold_list:
            lines.append(f"  • {p['name']} ({p['role']})")

    # Per-team breakdown
    lines.append(f"\n{'─'*28}\n*Team Breakdown:*")
    for r in parts:
        sq   = [db.get_player(p) for p in json.loads(r["squad"])]
        sq   = [p for p in sq if p]
        roles: dict = {}
        for p in sq:
            roles[p["role"]] = roles.get(p["role"], 0) + 1
        role_str = "  ".join(f"{r_emoji(k)}{k[:3]}:{v}" for k, v in roles.items()) or "Empty"
        # Count RTM cards used: original - current
        orig_row = db.cx.execute(
            "SELECT rtm_cards FROM participants WHERE auction_id=? AND user_id=?",
            (aid, r["user_id"])
        ).fetchone()
        rtm_used_count = "—"  # We track remaining cards; used = assigned - remaining
        lines.append(
            f"\n🏏 *{team_display(r)}*\n"
            f"  Purse Used: {fmt(r['total_spent'],aid)}  |  Left: {fmt(r['purse'],aid)}\n"
            f"  Squad: {len(sq)} players  |  RTM Left: {r['rtm_cards']}\n"
            f"  {role_str}"
        )

    # Send in chunks if too long
    text = "\n".join(lines)
    if len(text) > 3800:
        chunks, chunk = [], []
        for line in lines:
            if len("\n".join(chunk + [line])) > 3800:
                chunks.append("\n".join(chunk))
                chunk = [line]
            else:
                chunk.append(line)
        if chunk:
            chunks.append("\n".join(chunk))
        for ch in chunks:
            await update.message.reply_text(ch, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_end_auction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ask for confirmation before ending."""
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.active:
        await update.message.reply_text("No active auction to end.")
        return

    await update.message.reply_text(
        f"⚠️ *End Auction?*\n{'─'*28}\n"
        f"Auction: *{live.auction_name}*\n"
        f"Sold: {live.sold_count}  |  Unsold: {live.unsold_count}\n\n"
        f"*Yes* — End the auction and save results.\n"
        f"*No* — Pause instead. Resume with /resumeauction.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, End Auction", callback_data="endauction_yes"),
            InlineKeyboardButton("❌ No, Pause", callback_data="endauction_no"),
        ]]),
    )


async def _do_end_auction(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Internal: actually end the auction, save snapshot, send summary."""
    if live.timer_task and not live.timer_task.done():
        live.timer_task.cancel()

    live.active            = False
    live.current_player_id = None
    aid = live.auction_id
    db.set_auction_status(aid, "completed")

    parts   = sorted(db.get_all_parts(aid), key=lambda r: r["total_spent"], reverse=True)
    medals  = ["🥇","🥈","🥉"]
    summary = {"name": live.auction_name, "sold": live.sold_count,
               "unsold": live.unsold_count, "teams": []}
    lines   = [
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
    await context.bot.send_message(
        chat_id=chat_id,
        text="\n\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text="✅ *Auction Successfully Ended!* Start a new one with /create\\_auction.",
        parse_mode=ParseMode.MARKDOWN,
    )
    # Auto-send Excel data export to the group
    try:
        ar2   = db.get_auction(aid)
        xlsx  = _build_auction_excel(aid)
        fname = (live.auction_name or "Auction").replace(" ","_") + "_FinalData.xlsx"
        await context.bot.send_document(
            chat_id=chat_id,
            document=xlsx,
            filename=fname,
            caption=(
                f"📊 *{md_safe(live.auction_name)} — Final Auction Data*\n"
                f"Sheets: Summary · Teams · Players · Squad Details · Bid History"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.warning(f"Auto-export Excel on end: {e}")


# ─────────────────────────────────────────────────────────
# DATA EXPORT / IMPORT
# ─────────────────────────────────────────────────────────

def _build_auction_excel(aid: int) -> bytes:
    """Build a 5-sheet Excel workbook with all auction data."""
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    HDR_FILL    = PatternFill("solid", start_color="1F4E79")
    HDR_FONT    = Font(bold=True, color="FFFFFF", name="Arial", size=11)
    ALT_FILL    = PatternFill("solid", start_color="D6E4F0")
    SOLD_FILL   = PatternFill("solid", start_color="E2EFDA")
    UNSOLD_FILL = PatternFill("solid", start_color="FCE4D6")

    def style_header(ws, headers):
        ws.append(headers)
        for cell in ws[1]:
            cell.font      = HDR_FONT
            cell.fill      = HDR_FILL
            cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 22

    def autofit(ws):
        for col in ws.columns:
            col_letter = get_column_letter(col[0].column)
            max_len = max((len(str(cell.value or "")) for cell in col), default=8)
            ws.column_dimensions[col_letter].width = min(max(max_len + 3, 10), 42)

    def alt_rows(ws, start_row=2):
        for i, row in enumerate(ws.iter_rows(min_row=start_row), 1):
            if i % 2 == 0:
                for cell in row:
                    cell.fill = ALT_FILL

    ar      = db.get_auction(aid)
    parts   = db.get_all_parts(aid)
    players = db.cx.execute(
        "SELECT * FROM players WHERE auction_id=? ORDER BY status, name", (aid,)
    ).fetchall()
    team_map = {r["user_id"]: r["team_name"] for r in parts}

    wb = Workbook()

    # ── Sheet 1: Summary ──
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = f"Auction: {ar['name']}"
    ws["A1"].font = Font(bold=True, name="Arial", size=14)
    ws.merge_cells("A1:D1")
    ws.append([])
    for k, v in [
        ("Status",         ar["status"].title()),
        ("Max Teams",      ar["max_teams"]),
        ("Starting Purse", f"{ar['purse']}L"),
        ("Min / Max Squad", f"{ar['min_players']} / {ar['max_players']}"),
        ("Currency",       ar["currency"]),
        ("Players Total",  len(players)),
        ("Sold",           sum(1 for p in players if p["status"] == "sold")),
        ("Unsold",         sum(1 for p in players if p["status"] == "unsold")),
        ("Available",      sum(1 for p in players if p["status"] == "available")),
        ("Total Spent (L)", sum((p["sold_price"] or 0) for p in players if p["status"] == "sold")),
    ]:
        ws.append([k, v])
        ws[ws.max_row][0].font = Font(bold=True, name="Arial")
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 20

    # ── Sheet 2: Teams ──
    ws2 = wb.create_sheet("Teams")
    style_header(ws2, [
        "Team Name", "Username", "Purse Remaining (L)",
        "Total Spent (L)", "Spent (Cr)", "Squad Size", "RTM Cards", "RTM Team"
    ])
    for r in parts:
        sq = json.loads(r["squad"])
        u  = db.get_user(r["user_id"])
        uname = (f"@{u['username']}" if u and u["username"] else (u["first_name"] if u else str(r["user_id"])))
        ws2.append([
            r["team_name"], uname, r["purse"], r["total_spent"],
            round(r["total_spent"]/100, 2), len(sq),
            r["rtm_cards"], r["rtm_team"] or ""
        ])
    alt_rows(ws2); autofit(ws2)

    # ── Sheet 3: Players ──
    ws3 = wb.create_sheet("Players")
    style_header(ws3, [
        "#", "Player Name", "Role", "Nationality", "Prev Team (IPL)", "Tier",
        "Base Price (L)", "Status", "Sold To", "Sold Price (L)", "Sold Price (Cr)"
    ])
    for i, p in enumerate(players, 1):
        buyer = team_map.get(p["sold_to"], "") if p["sold_to"] else ""
        sp_cr = round((p["sold_price"] or 0)/100, 2) if (p["sold_price"] or 0) >= 100 else ""
        ws3.append([
            i, p["name"], p["role"], p["nationality"],
            p["ipl_team"] or "", p["tier"], p["base_price"],
            p["status"].title(), buyer, p["sold_price"] or "", sp_cr
        ])
        fill = SOLD_FILL if p["status"]=="sold" else (UNSOLD_FILL if p["status"]=="unsold" else None)
        if fill:
            for cell in ws3[ws3.max_row]: cell.fill = fill
    alt_rows(ws3); autofit(ws3)

    # ── Sheet 4: Squad Details ──
    ws4 = wb.create_sheet("Squad Details")
    style_header(ws4, [
        "Team Name", "Player Name", "Role", "Nationality",
        "Prev Team", "Tier", "Price Paid (L)", "Price Paid (Cr)"
    ])
    pmap = {p["player_id"]: p for p in players}
    for r in parts:
        for pid in json.loads(r["squad"]):
            p = pmap.get(int(pid))
            if not p: continue
            sp_cr = round((p["sold_price"] or 0)/100, 2) if (p["sold_price"] or 0) >= 100 else ""
            ws4.append([
                r["team_name"], p["name"], p["role"], p["nationality"],
                p["ipl_team"] or "", p["tier"], p["sold_price"] or "", sp_cr
            ])
    alt_rows(ws4); autofit(ws4)

    # ── Sheet 5: Bid History ──
    ws5 = wb.create_sheet("Bid History")
    style_header(ws5, ["Timestamp", "Team", "Player", "Bid (L)", "Bid (Cr)", "Result"])
    bids = db.cx.execute(
        "SELECT b.*, p.team_name FROM bid_history b "
        "LEFT JOIN participants p ON p.auction_id=b.auction_id AND p.user_id=b.user_id "
        "WHERE b.auction_id=? ORDER BY b.ts", (aid,)
    ).fetchall()
    for b in bids:
        cr = round(b["bid_amount"]/100, 2) if b["bid_amount"] >= 100 else ""
        ws5.append([
            b["ts"], b["team_name"] or "", b["player_name"],
            b["bid_amount"], cr, "Won" if b["won"] else "Lost"
        ])
    alt_rows(ws5); autofit(ws5)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def cmd_download_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/downloaddata — Export all auction data as Excel."""
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return

    aid = live.auction_id
    if not aid:
        row = db.cx.execute(
            "SELECT auction_id FROM auctions ORDER BY auction_id DESC LIMIT 1"
        ).fetchone()
        if row: aid = row["auction_id"]
    if not aid:
        await update.message.reply_text("No auction data found.")
        return

    ar  = db.get_auction(aid)
    msg = await update.message.reply_text("⏳ Building Excel report…")
    try:
        xlsx_bytes = _build_auction_excel(aid)
        fname      = (ar["name"] or "Auction").replace(" ", "_") + "_Data.xlsx"
        await update.message.reply_document(
            document=xlsx_bytes,
            filename=fname,
            caption=(
                f"📊 *{md_safe(ar['name'])} — Full Data*\n"
                f"Sheets: Summary · Teams · Players · Squad Details · Bid History"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"cmd_download_data: {e}", exc_info=True)
        await update.message.reply_text(f"Failed to build Excel: {e}")
    finally:
        try: await msg.delete()
        except Exception: pass


async def cmd_download_template(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/downloadtemplate — Send the player upload template."""
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return

    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb  = Workbook()
    ws  = wb.active
    ws.title = "Players"

    HDR_FILL = PatternFill("solid", start_color="1F4E79")
    HDR_FONT = Font(bold=True, color="FFFFFF", name="Arial", size=11)

    headers = ["name", "role", "ipl_team", "nationality", "base_price", "tier"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = HDR_FONT; cell.fill = HDR_FILL
        cell.alignment = Alignment(horizontal="center")
    ws.row_dimensions[1].height = 22

    # Notes row (grey, italic)
    notes = [
        "Full player name",
        "Bat / Bowl / AR / WK",
        "Prev IPL team for RTM (blank = none)",
        "Indian / Overseas",
        "e.g. 2cr / 50l / 200 (lakhs)",
        "Marquee / A / B / C / Uncapped"
    ]
    ws.append(notes)
    for cell in ws[2]:
        cell.font = Font(italic=True, color="808080", name="Arial", size=9)
        cell.alignment = Alignment(wrap_text=True)
    ws.row_dimensions[2].height = 30

    samples = [
        ["Virat Kohli",    "Bat",  "RCB", "Indian",   "2cr",   "Marquee"],
        ["Pat Cummins",    "Bowl", "SRH", "Overseas", "3cr",   "A"],
        ["Hardik Pandya",  "AR",   "MI",  "Indian",   "1.5cr", "A"],
        ["Rishabh Pant",   "WK",   "DC",  "Indian",   "2cr",   "Marquee"],
        ["Jasprit Bumrah", "Bowl", "MI",  "Indian",   "2cr",   "A"],
    ]
    for s in samples: ws.append(s)

    for i, w in enumerate([22,10,20,12,14,12], 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # Instructions sheet
    ws2 = wb.create_sheet("Instructions")
    lines = [
        ("IPL Auction Bot — Player Upload Template", True, 13),
        ("", False, 11),
        ("HOW TO USE", True, 11),
        ("1. Fill the Players sheet. Delete the grey notes row before uploading.", False, 11),
        ("2. Save as .xlsx, .csv, or .json", False, 11),
        ("3. Send the file with command /uploaddata (attach file to message)", False, 11),
        ("   OR paste CSV/JSON text after /uploaddata", False, 11),
        ("", False, 11),
        ("PRICE FORMAT", True, 11),
        ("  2cr  → 2 Crore (= 200 lakhs)", False, 11),
        ("  50l  → 50 Lakh", False, 11),
        ("  200  → 200 Lakh (bare number = lakhs)", False, 11),
        ("", False, 11),
        ("JSON FORMAT", True, 11),
        ('  [{"name":"Virat Kohli","role":"Bat","ipl_team":"RCB","nationality":"Indian","base_price":"2cr","tier":"Marquee"}]', False, 9),
        ("", False, 11),
        ("CSV FORMAT", True, 11),
        ("  name,role,ipl_team,nationality,base_price,tier", False, 11),
        ("  Virat Kohli,Bat,RCB,Indian,2cr,Marquee", False, 11),
    ]
    for text, bold, size in lines:
        ws2.append([text])
        ws2[ws2.max_row][0].font = Font(bold=bold, name="Arial", size=size)
    ws2.column_dimensions["A"].width = 90

    buf = io.BytesIO()
    wb.save(buf)

    await update.message.reply_document(
        document=buf.getvalue(),
        filename="PlayerTemplate.xlsx",
        caption=(
            "📋 *Player Upload Template*\n\n"
            "• Fill the *Players* sheet\n"
            "• Delete the grey notes row\n"
            "• Send back with /uploaddata (attach file)\n\n"
            "Also accepts: CSV text, JSON text, .csv file, .json file"
        ),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_upload_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/uploaddata — Import players from Excel, CSV, or JSON."""
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not live.auction_id:
        await update.message.reply_text(
            "Create an auction first with /create\_auction.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    import io, csv as _csv_mod

    def _parse_price(s: str) -> int:
        s = str(s or "20").strip().lower()
        try:
            if s.endswith("cr"): return int(float(s[:-2]) * 100)
            if s.endswith("l"):  return int(float(s[:-1]))
            return int(float(s))
        except Exception:
            return 20

    def _normalise(raw: dict) -> dict:
        kv  = {k.strip().lower().replace(" ","_"): str(v or "").strip() for k,v in raw.items()}
        name = kv.get("name") or kv.get("player_name") or kv.get("player") or ""
        role = kv.get("role","bat").strip().upper()
        role = {"BATSMAN":"Bat","BOWLER":"Bowl","ALLROUNDER":"AR","WICKETKEEPER":"WK",
                "BAT":"Bat","BOWL":"Bowl","AR":"AR","WK":"WK"}.get(role, "Bat")
        nat  = "Overseas" if "over" in kv.get("nationality","indian").lower() else "Indian"
        ipl  = kv.get("ipl_team") or kv.get("prev_team") or kv.get("team") or ""
        bp   = _parse_price(kv.get("base_price") or kv.get("price") or "20")
        tier = kv.get("tier","C").strip().title()
        tier = tier if tier in ("Marquee","A","B","C","Uncapped") else "C"
        return {"name": name.strip(), "role": role, "nationality": nat,
                "ipl_team": ipl.strip(), "base_price": bp, "tier": tier}

    rows = []
    doc  = update.message.document

    if doc:
        fname    = (doc.file_name or "").lower()
        file_obj = await doc.get_file()
        raw      = bytes(await file_obj.download_as_bytearray())

        if fname.endswith((".xlsx",".xls")):
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
            ws = wb["Players"] if "Players" in wb.sheetnames else wb.active
            hdrs = None
            for r in ws.iter_rows(values_only=True):
                if not any(c is not None for c in r): continue
                if hdrs is None:
                    # Detect header row: must contain "name" or "player"
                    row_strs = [str(c or "").strip().lower() for c in r]
                    if any(x in row_strs for x in ("name","player","player_name")):
                        hdrs = [str(c or "").strip().lower().replace(" ","_") for c in r]
                    continue
                d = _normalise(dict(zip(hdrs, r)))
                if d["name"]: rows.append(d)

        elif fname.endswith(".csv"):
            text   = raw.decode("utf-8", errors="replace")
            reader = _csv_mod.DictReader(io.StringIO(text))
            for r in reader:
                d = _normalise(r)
                if d["name"]: rows.append(d)

        elif fname.endswith(".json"):
            import json as _j
            data = _j.loads(raw.decode("utf-8"))
            if isinstance(data, dict): data = [data]
            for r in data:
                d = _normalise(r)
                if d["name"]: rows.append(d)

        else:
            await update.message.reply_text("Unsupported file type. Use .xlsx, .csv, or .json")
            return

    else:
        body = (update.message.text or "").strip()
        if body.lower().startswith("/uploaddata"): body = body[11:].strip()
        if not body:
            await update.message.reply_text(
                "Attach a file (.xlsx / .csv / .json) to this command,\n"
                "or paste JSON/CSV text after /uploaddata.\n\n"
                "Use /downloadtemplate to get the template."
            )
            return
        if body.startswith("[") or body.startswith("{"):
            import json as _j
            try:
                data = _j.loads(body)
                if isinstance(data, dict): data = [data]
                for r in data:
                    d = _normalise(r)
                    if d["name"]: rows.append(d)
            except Exception as e:
                await update.message.reply_text(f"Invalid JSON: {e}")
                return
        else:
            reader = _csv_mod.DictReader(io.StringIO(body))
            for r in reader:
                d = _normalise(r)
                if d["name"]: rows.append(d)

    if not rows:
        await update.message.reply_text(
            "No valid rows found. Make sure the file has a header row:\n"
            "name, role, ipl_team, nationality, base_price, tier\n\n"
            "Use /downloadtemplate for the correct format."
        )
        return

    aid    = live.auction_id
    added  = 0
    skipped= []
    for d in rows:
        try:
            db.cx.execute(
                "INSERT INTO players(auction_id,name,role,nationality,ipl_team,base_price,tier,status)"
                " VALUES(?,?,?,?,?,?,?,\'available\')",
                (aid, d["name"], d["role"], d["nationality"],
                 d["ipl_team"], d["base_price"], d["tier"])
            )
            added += 1
        except Exception as e:
            skipped.append(d["name"])
    db.cx.commit()

    if not live.active:
        live.player_queue = list(db.get_available(aid))

    skip_note = f"\n\u26a0\ufe0f Skipped {len(skipped)}: {', '.join(skipped[:5])}" if skipped else ""
    await update.message.reply_text(
        f"\u2705 *{added} players imported!*\n"
        f"Auction: *{md_safe(live.auction_name)}*\n"
        f"Available: {len(db.get_available(aid))} players{skip_note}\n\n"
        f"Use /startauction when ready.",
        parse_mode=ParseMode.MARKDOWN,
    )


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


async def cmd_dtime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /dtime <seconds> — Set ALL timers at once.
    /dtime show      — Show all current timer values.
    """
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return

    arg = (context.args[0].lower() if context.args else "show").rstrip("s")

    if arg == "show" or not arg:
        bid   = live.auto_sell_secs or Config.BID_TIMER
        await update.message.reply_text(
            f"⏱ *All Timer Settings*\n{'─'*28}\n"
            f"🔨 /bidtimer — Bid Timer: *{bid}s*\n"
            f"🛡 /antisnipe — Anti-Snipe Extension: *{Config.ANTI_SNIPE}s*\n"
            f"🎴 /rtmwindow — RTM Window (post-SOLD): *{Config.RTM_OFFER_TIMER}s*\n"
            f"⬆️ /rtmcounter — RTM Counter (raise window): *{Config.RTM_COUNTER_TIMER}s*\n"
            f"✅ /rtmdecision — RTM Decision (YES/NO): *{Config.RTM_DECISION_TIMER}s*\n"
            f"⏭ /autonext — Auto-Next Delay: *{live.auto_next_secs or 'off'}*\n\n"
            f"Use /dtime <seconds> to set all at once.\n"
            f"Or use individual commands above.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        secs = int(arg)
        if secs < 5:
            await update.message.reply_text("Minimum is 5 seconds.")
            return
    except ValueError:
        await update.message.reply_text("Usage: /dtime <seconds>  e.g. /dtime 30")
        return

    Config.BID_TIMER          = secs
    Config.RTM_OFFER_TIMER    = secs
    Config.RTM_COUNTER_TIMER  = secs
    Config.RTM_DECISION_TIMER = secs
    Config.RTM_TIMER          = secs
    live.auto_sell_secs       = secs

    await update.message.reply_text(
        f"✅ *All timers set to {secs}s*\n{'─'*28}\n"
        f"🔨 Bid Timer: {secs}s\n"
        f"🎴 RTM Window: {secs}s\n"
        f"⬆️ RTM Counter: {secs}s\n"
        f"✅ RTM Decision: {secs}s",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_bid_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set bid countdown timer."""
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    if not context.args:
        bid = live.auto_sell_secs or Config.BID_TIMER
        await update.message.reply_text(f"🔨 Bid Timer: *{bid}s*\nUsage: /bidtimer <secs>",
                                        parse_mode=ParseMode.MARKDOWN)
        return
    try:
        secs = int(context.args[0].rstrip("s"))
        if secs < 5: raise ValueError
        Config.BID_TIMER    = secs
        live.auto_sell_secs = secs
        await update.message.reply_text(f"✅ Bid Timer: *{secs}s*", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("Usage: /bidtimer <seconds>  (min 5)")


async def cmd_antisnipe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set anti-snipe extension."""
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text(
            f"🛡 Anti-Snipe: *{Config.ANTI_SNIPE}s*\n"
            f"If a bid arrives in the last N seconds, timer resets to N.\n"
            f"Usage: /antisnipe <secs>",
            parse_mode=ParseMode.MARKDOWN)
        return
    try:
        secs = int(context.args[0].rstrip("s"))
        if secs < 3: raise ValueError
        Config.ANTI_SNIPE = secs
        await update.message.reply_text(f"✅ Anti-Snipe: *{secs}s*", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("Usage: /antisnipe <seconds>  (min 3)")


async def cmd_rtm_window(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set RTM silent window after SOLD."""
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text(
            f"🎴 RTM Window: *{Config.RTM_OFFER_TIMER}s*\n"
            f"Silent window after SOLD — use /rtm within this time.\n"
            f"Usage: /rtmwindow <secs>",
            parse_mode=ParseMode.MARKDOWN)
        return
    try:
        secs = int(context.args[0].rstrip("s"))
        if secs < 5: raise ValueError
        Config.RTM_OFFER_TIMER = secs
        Config.RTM_TIMER       = secs
        await update.message.reply_text(f"✅ RTM Window: *{secs}s*", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("Usage: /rtmwindow <seconds>  (min 5)")


async def cmd_rtm_counter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set RTM counter window (how long original bidder has to raise)."""
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text(
            f"⬆️ RTM Counter: *{Config.RTM_COUNTER_TIMER}s*\n"
            f"How long original bidder has to raise after /rtm is used.\n"
            f"Usage: /rtmcounter <secs>",
            parse_mode=ParseMode.MARKDOWN)
        return
    try:
        secs = int(context.args[0].rstrip("s"))
        if secs < 5: raise ValueError
        Config.RTM_COUNTER_TIMER = secs
        await update.message.reply_text(f"✅ RTM Counter: *{secs}s*", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("Usage: /rtmcounter <seconds>  (min 5)")


async def cmd_rtm_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set RTM decision window (how long RTM team has to click YES/NO)."""
    await _reg(update.effective_user)
    if not db.is_admin(update.effective_user.id): return
    if not context.args:
        await update.message.reply_text(
            f"✅ RTM Decision: *{Config.RTM_DECISION_TIMER}s*\n"
            f"How long RTM team has to click YES or NO.\n"
            f"Usage: /rtmdecision <secs>",
            parse_mode=ParseMode.MARKDOWN)
        return
    try:
        secs = int(context.args[0].rstrip("s"))
        if secs < 5: raise ValueError
        Config.RTM_DECISION_TIMER = secs
        await update.message.reply_text(f"✅ RTM Decision: *{secs}s*", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("Usage: /rtmdecision <seconds>  (min 5)")


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
        line = re.sub(r"^\d+[.)\s]\s*", "", line.strip())
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
    """Core logic for when a team tries to use RTM via /rtm or /right_to_match command."""
    aid = live.auction_id

    async def err(m):
        if update.callback_query:
            await update.callback_query.answer(m, show_alert=True)
        else:
            await update.message.reply_text(m)

    if not live.active or not live.current_player_id:
        await err("No active auction.")
        return

    # RTM can only be invoked during the RTM_OFFERED window (after timer expires)
    if live.rtm_state not in (RTM_OFFERED,):
        if live.rtm_state in (RTM_ACTIVE, RTM_COUNTER):
            await err("RTM already in progress. Wait for it to resolve.")
        else:
            await err(
                "RTM window is not open yet. "
                "Wait for the bid timer to expire — if you hold a matching RTM card, "
                "a window will open."
            )
        return

    if live.highest_bidder_id is None:
        await err("No bid placed yet — nothing to RTM against.")
        return

    if uid == live.highest_bidder_id:
        await err("You are the highest bidder — you cannot RTM yourself!")
        return

    pr = db.get_player(live.current_player_id)
    if not pr:
        await err("Player not found.")
        return

    ipl_team = (pr["ipl_team"] or "") if pr["ipl_team"] is not None else ""
    if not ipl_team.strip():
        await err("This player has no previous IPL team — RTM is not applicable.")
        return

    row = db.get_part(aid, uid)
    if not row or row["rtm_cards"] <= 0:
        await err("You have no RTM cards.")
        return

    # Check that this team's RTM card matches the player's ipl_team
    rtm_team_assigned = (row["rtm_team"] or "").strip()
    if rtm_team_assigned.lower() != ipl_team.strip().lower():
        await err(
            f"Your RTM card is for *{rtm_team_assigned or 'N/A'}*, "
            f"but this player's previous team is *{ipl_team}*. "
            f"RTM does not apply."
        )
        return

    if row["purse"] < live.current_bid:
        await err(f"Not enough purse to RTM! You need {fmt(live.current_bid, aid)}.")
        return

    # Cancel RTM offer timer
    if live.timer_task and not live.timer_task.done():
        live.timer_task.cancel()

    # Deduct one RTM card
    db.cx.execute(
        "UPDATE participants SET rtm_cards=MAX(0,rtm_cards-1)"
        " WHERE auction_id=? AND user_id=?",
        (aid, uid),
    )
    db.cx.commit()

    live.rtm_state            = RTM_ACTIVE
    live.rtm_team_id          = uid
    live.rtm_team_name        = bid_display(row)
    live.rtm_orig_bidder_id   = live.highest_bidder_id
    live.rtm_orig_bidder_name = live.highest_bidder_name
    live.rtm_orig_bid         = live.current_bid

    if update.callback_query:
        await update.callback_query.answer("RTM card used!", show_alert=True)

    # Announce RTM use and invite original bidder to counter
    msg = await context.bot.send_message(
        chat_id=live.chat_id,
        text=(
            f"🎴 *{live.rtm_team_name}* uses RTM on *{pr['name']}*!\n"
            f"{'─'*28}\n"
            f"Current highest bid: *{fmt(live.rtm_orig_bid, aid)}* "
            f"by *{live.rtm_orig_bidder_name}*\n\n"
            f"*{live.rtm_orig_bidder_name}* — raise your bid using:\n"
            f"`/bid <amount>`  e.g. `/bid 19cr`\n\n"
            f"⏱ You have *{Config.RTM_TIMER}s* to counter. "
            f"If no counter, *{live.rtm_team_name}* wins the player at "
            f"*{fmt(live.rtm_orig_bid, aid)}*."
        ),
        parse_mode=ParseMode.MARKDOWN,
    )
    live.rtm_msg_id = msg.message_id
    # Start counter window — waits for original bidder to /bid
    live.timer_task = asyncio.create_task(_rtm_counter_timer(context))


# ─────────────────────────────────────────────────────────
# CALLBACK HANDLER
# ─────────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data  = query.data
    uid   = query.from_user.id

    try:
        await _reg(query.from_user)
    except Exception:
        pass  # Don't let _reg crash stop button response

    try:
        await _handle_callback_inner(update, context, query, data, uid)
    except Exception as e:
        logger.error(f"handle_callback crashed [data={data}]: {e}", exc_info=True)
        try:
            await query.answer("⚠️ An error occurred. Please try again.", show_alert=True)
        except Exception:
            pass


async def _handle_callback_inner(update, context, query, data, uid):

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

    # ── RTM USE BUTTON (Team A clicked USE RTM) ──────────────
    # Format: rtm_use_btn|pid|rtm_uid|orig_uid|orig_bid
    if data.startswith("rtm_use_btn") or data == "rtm_use":
        await query.answer("🎴 RTM Activated!", show_alert=False)
        try:
            if "|" in data:
                parts    = data.split("|")
                pid_val  = int(parts[1])
                rtm_uid  = int(parts[2])
                orig_uid = int(parts[3])
                orig_bid = int(parts[4])
            else:
                pid_val  = live.current_player_id
                rtm_uid  = live.rtm_team_id
                orig_uid = live.rtm_orig_bidder_id
                orig_bid = live.rtm_orig_bid

            euid = eff_uid(uid)
            if rtm_uid != euid and not db.is_admin(uid):
                await context.bot.send_message(query.message.chat_id,
                    "❌ Only the RTM team or admin can click USE RTM.")
                return

            if live.rtm_state != RTM_OFFERED:
                await context.bot.send_message(query.message.chat_id,
                    "⚠️ RTM window already closed.")
                return

            # Cancel offer timer
            if live.timer_task and not live.timer_task.done():
                live.timer_task.cancel()
            live.rtm_state = RTM_ACTIVE

            aid = live.auction_id
            # Deduct RTM card NOW (team confirmed they want to use it)
            db.cx.execute(
                "UPDATE participants SET rtm_cards=MAX(0,rtm_cards-1)"
                " WHERE auction_id=? AND user_id=?", (aid, rtm_uid)
            )
            db.cx.commit()

            pr = db.get_player(pid_val)
            if not pr:
                await context.bot.send_message(query.message.chat_id, "⚠️ Player not found.")
                return

            # Remove buttons from challenge message
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass

            # Send Step 3: Team B raise message
            msg = await context.bot.send_message(
                chat_id=live.chat_id,
                text=rtm_activated_text(pr),
                parse_mode=ParseMode.MARKDOWN,
            )
            live.rtm_msg_id = msg.message_id
            live.timer_task = asyncio.create_task(_rtm_counter_timer(context))
            logger.info(f"RTM USE: uid={uid} pid={pid_val} orig_bid={orig_bid}")

        except Exception as e:
            logger.error(f"rtm_use_btn crashed: {e}", exc_info=True)
            await context.bot.send_message(query.message.chat_id, f"⚠️ RTM error: {e}")
        return

    # ── RTM PASS BUTTON (Team A clicked PASS) ────────────────
    # Format: rtm_pass_btn|pid|orig_uid|orig_bid
    if data.startswith("rtm_pass_btn") or data == "rtm_skip":
        await query.answer("❌ RTM Passed", show_alert=False)
        try:
            if "|" in data:
                parts    = data.split("|")
                pid_val  = int(parts[1])
                orig_uid = int(parts[2])
                orig_bid = int(parts[3])
            else:
                pid_val  = live.current_player_id
                orig_uid = live.rtm_orig_bidder_id
                orig_bid = live.rtm_orig_bid

            euid = eff_uid(uid)
            rtm_uid = live.rtm_team_id
            if rtm_uid != euid and not db.is_admin(uid):
                await context.bot.send_message(query.message.chat_id,
                    "❌ Only the RTM team or admin can click PASS.")
                return

            if live.rtm_state != RTM_OFFERED:
                await context.bot.send_message(query.message.chat_id,
                    "⚠️ RTM window already closed.")
                return

            # Cancel offer timer
            if live.timer_task and not live.timer_task.done():
                live.timer_task.cancel()
            live.rtm_state = RTM_NONE
            # Card NOT deducted — team passed

            pr = db.get_player(pid_val)
            # Remove buttons, show passed message
            try:
                await query.edit_message_text(
                    f"❌ RTM Passed — Player goes to *{md_safe(live.rtm_orig_bidder_name)}*",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass

            if pr:
                # Finalize to original bidder at original bid
                live.current_player_id   = pid_val
                live.current_bid         = orig_bid
                live.highest_bidder_id   = orig_uid
                live.highest_bidder_name = live.rtm_orig_bidder_name
                await _finalize(context, pr)
            logger.info(f"RTM PASS: uid={uid} pid={pid_val}")

        except Exception as e:
            logger.error(f"rtm_pass_btn crashed: {e}", exc_info=True)
            await context.bot.send_message(query.message.chat_id, f"⚠️ RTM error: {e}")
        return

    # ── RTM YES — stateless: all data in callback_data ──────
    # Format: ry|player_id|rtm_uid|final_price|orig_uid|orig_price
    if data.startswith("ry|") or data == "rtm_yes":
        await query.answer("✅ Processing...", show_alert=False)
        try:
            if data.startswith("ry|"):
                parts       = data.split("|")
                pid_val     = int(parts[1])
                rtm_uid     = int(parts[2])
                final_price = int(parts[3])
                orig_uid    = int(parts[4])
                orig_bid    = int(parts[5])
            else:
                pid_val     = live.current_player_id
                rtm_uid     = live.rtm_team_id
                final_price = live.rtm_counter_bid
                orig_uid    = live.rtm_orig_bidder_id
                orig_bid    = live.rtm_orig_bid

            euid = eff_uid(uid)
            aid  = live.auction_id

            if rtm_uid != euid and not db.is_admin(uid):
                await context.bot.send_message(
                    query.message.chat_id,
                    "❌ Only the RTM team or an admin can click YES.")
                return

            # Cancel any running timer and clear state
            if live.timer_task and not live.timer_task.done():
                live.timer_task.cancel()
            live.rtm_state = RTM_NONE

            pr = db.get_player(pid_val)
            if not pr:
                await context.bot.send_message(query.message.chat_id, "⚠️ Player not found.")
                return

            # Guard: player must not already be finalized
            if pr["status"] == "sold" and pr["sold_to"] != rtm_uid:
                await context.bot.send_message(
                    query.message.chat_id,
                    f"⚠️ {pr['name']} already sold to another team.")
                return

            ipl      = (pr["ipl_team"] or "N/A") if pr["ipl_team"] is not None else "N/A"
            rtm_row_pre  = db.get_part(aid, rtm_uid)
            rtm_name     = team_display(rtm_row_pre) if rtm_row_pre else f"Team {rtm_uid}"
            orig_row_pre = db.get_part(aid, orig_uid)
            orig_name    = team_display(orig_row_pre) if orig_row_pre else live.rtm_orig_bidder_name

            # DB: sell to RTM team at raised price
            db.set_player_status(pr["player_id"], "sold", rtm_uid, final_price, None, query.message.chat_id)
            db.deduct_purse(aid, rtm_uid, final_price)
            db.add_to_squad(aid, rtm_uid, pr["player_id"])
            db.record_bid(aid, rtm_uid, pr["player_id"], pr["name"], final_price, won=True)

            rtm_row = db.get_part(aid, rtm_uid)
            rem     = rtm_row["purse"] if rtm_row else 0
            sq      = len(json.loads(rtm_row["squad"])) if rtm_row else 0
            ts      = ist_now()
            _ar     = db.get_auction(aid)
            max_sq  = _ar["max_players"] if _ar else 25

            p_name   = md_safe(pr['name'])
            s_ipl    = md_safe(ipl)
            s_role   = md_safe(pr['role'])
            s_nat    = md_safe(pr['nationality'])
            s_rtm    = md_safe(rtm_name)
            s_orig   = md_safe(orig_name)
            s_price  = md_safe(fmt(final_price, aid))
            s_rem    = md_safe(fmt(rem, aid))

            text = (
                f"✅ *RTM ACCEPTED - PLAYER SOLD!*\n"
                f"{'═'*20}\n\n"
                f"🏏 *{flag(pr['nationality'])} {p_name}* ({s_ipl})\n"
                f"🎯 {s_role} | {s_nat}\n\n"
                f"💰 *Final Price:* {s_price}\n"
                f"🏆 *Winner:* *{s_rtm}* 🎴 (via RTM)\n\n"
                f"📊 *Transaction:*\n"
                f"• Deducted: {s_price} from {s_rtm}\n"
                f"• Remaining Purse: {s_rem}\n"
                f"• Squad: {sq}/{max_sq} players\n\n"
                f"❌ {s_orig} loses the bid\n\n"
                f"⏰ Sold at: {ts}"
            )

            for _pm in (ParseMode.MARKDOWN, None):
                try:
                    await query.edit_message_text(
                        text, parse_mode=_pm,
                        reply_markup=reauction_keyboard() if _pm is None else None)
                    break
                except Exception as _e:
                    if _pm is None:
                        logger.error(f"ry send failed completely: {_e}")
                        await context.bot.send_message(
                            query.message.chat_id,
                            "✅ RTM Accepted! Sale recorded.")
                    continue

            live.sold_count         += 1
            _set_last_sold(pr["player_id"], pr["name"], rtm_uid, rtm_name, final_price)
            live.current_player_id   = None
            live.current_bid         = 0
            live.highest_bidder_id   = None
            live.highest_bidder_name = ""
            live.rtm_team_id         = None
            live.rtm_counter_bid     = 0
            save_live_state()
            await _try_auto_next(context)

        except Exception as e:
            logger.error(f"ry| handler CRASHED: {e}", exc_info=True)
            await context.bot.send_message(
                query.message.chat_id,
                f"⚠️ RTM YES error — admin use /forcesold\nDetails: {e}")
        return

    if data.startswith("rn|") or data == "rtm_no":
        await query.answer("❌ Processing...", show_alert=False)
        try:
            if data.startswith("rn|"):
                parts       = data.split("|")
                pid_val     = int(parts[1])
                rtm_uid     = int(parts[2])
                final_price = int(parts[3])
                orig_uid    = int(parts[4])
                orig_price  = int(parts[5])
            else:
                pid_val     = live.current_player_id
                rtm_uid     = live.rtm_team_id
                final_price = live.rtm_counter_bid
                orig_uid    = live.rtm_orig_bidder_id
                orig_price  = live.rtm_orig_bid

            euid = eff_uid(uid)
            aid  = live.auction_id

            if rtm_uid != euid and not db.is_admin(uid):
                await context.bot.send_message(
                    query.message.chat_id,
                    "❌ Only the RTM team or an admin can click NO.")
                return

            if live.timer_task and not live.timer_task.done():
                live.timer_task.cancel()
            live.rtm_state = RTM_NONE

            pr = db.get_player(pid_val)
            if not pr:
                await context.bot.send_message(query.message.chat_id, "⚠️ Player not found.")
                return

            if pr["status"] == "sold" and pr["sold_to"] != orig_uid:
                await context.bot.send_message(
                    query.message.chat_id,
                    f"⚠️ {pr['name']} already sold to another team.")
                return

            ipl          = (pr["ipl_team"] or "N/A") if pr["ipl_team"] is not None else "N/A"
            orig_row_pre = db.get_part(aid, orig_uid)
            orig_name    = team_display(orig_row_pre) if orig_row_pre else live.rtm_orig_bidder_name
            rtm_row_pre  = db.get_part(aid, rtm_uid)
            rtm_name     = team_display(rtm_row_pre) if rtm_row_pre else live.rtm_team_name

            # DB: sell to original bidder at original price
            db.set_player_status(pr["player_id"], "sold", orig_uid, orig_price, None, query.message.chat_id)
            db.deduct_purse(aid, orig_uid, orig_price)
            db.add_to_squad(aid, orig_uid, pr["player_id"])
            db.record_bid(aid, orig_uid, pr["player_id"], pr["name"], orig_price, won=True)

            # ✅ Refund the RTM card — team declined, card not consumed
            db.cx.execute(
                "UPDATE participants SET rtm_cards=rtm_cards+1"
                " WHERE auction_id=? AND user_id=?", (aid, rtm_uid)
            )
            db.cx.commit()
            logger.info(f"RTM card refunded to uid={rtm_uid} (declined)")

            orig_row = db.get_part(aid, orig_uid)
            rem      = orig_row["purse"] if orig_row else 0
            sq       = len(json.loads(orig_row["squad"])) if orig_row else 0
            ts       = ist_now()
            _ar2     = db.get_auction(aid)
            max_sq   = _ar2["max_players"] if _ar2 else 25

            p_name  = md_safe(pr['name'])
            s_ipl   = md_safe(ipl)
            s_role  = md_safe(pr['role'])
            s_nat   = md_safe(pr['nationality'])
            s_orig  = md_safe(orig_name)
            s_rtm   = md_safe(rtm_name)
            s_price = md_safe(fmt(orig_price, aid))
            s_rem   = md_safe(fmt(rem, aid))

            text = (
                f"❌ *RTM DECLINED - ORIGINAL SALE!*\n"
                f"{'═'*20}\n\n"
                f"🏏 *{flag(pr['nationality'])} {p_name}* ({s_ipl})\n"
                f"🎯 {s_role} | {s_nat}\n\n"
                f"💰 *Final Price:* {s_price} (Original bid)\n"
                f"🏆 *Winner:* *{s_orig}*\n\n"
                f"🎴 {s_rtm} declined to match the raised bid\n\n"
                f"📊 *Transaction:*\n"
                f"• Deducted: {s_price} from {s_orig}\n"
                f"• Remaining Purse: {s_rem}\n"
                f"• Squad: {sq}/{max_sq} players\n\n"
                f"✅ {s_orig} wins the player!\n\n"
                f"⏰ Sold at: {ts}"
            )

            for _pm in (ParseMode.MARKDOWN, None):
                try:
                    await query.edit_message_text(
                        text, parse_mode=_pm,
                        reply_markup=reauction_keyboard() if _pm is None else None)
                    break
                except Exception as _e:
                    if _pm is None:
                        logger.error(f"rn send failed completely: {_e}")
                        await context.bot.send_message(
                            query.message.chat_id,
                            "❌ RTM Declined. Original sale stands.")
                    continue

            live.sold_count         += 1
            _set_last_sold(pr["player_id"], pr["name"], orig_uid, orig_name, orig_price)
            live.current_player_id   = None
            live.current_bid         = 0
            live.highest_bidder_id   = None
            live.highest_bidder_name = ""
            live.rtm_team_id         = None
            live.rtm_counter_bid     = 0
            save_live_state()
            await _try_auto_next(context)

        except Exception as e:
            logger.error(f"rn| handler CRASHED: {e}", exc_info=True)
            await context.bot.send_message(
                query.message.chat_id,
                f"⚠️ RTM NO error — admin use /forcesold\nDetails: {e}")
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

    # ── END AUCTION CONFIRM ──────────────────
    if data == "endauction_yes":
        if not db.is_admin(uid):
            await query.answer("Admin only.", show_alert=True)
            return
        if not live.active:
            await query.answer("Auction already ended.", show_alert=True)
            return
        try:
            await query.edit_message_text("⏳ Ending auction and saving results...")
        except Exception:
            pass
        await query.answer()
        await _do_end_auction(context, query.message.chat_id)
        return

    if data == "endauction_no":
        if not db.is_admin(uid):
            await query.answer("Admin only.", show_alert=True)
            return
        live.paused = True
        try:
            await query.edit_message_text(
                "⏸ *Auction Paused.*\nUse /resumeauction to continue.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        await query.answer("Auction paused.")
        return

    await query.answer()


# ─────────────────────────────────────────────────────────
# DOT COMMAND ROUTER
# ─────────────────────────────────────────────────────────
DOT_MAP = {
    "setteamname": cmd_set_team_name, "stn": cmd_set_team_name,
    "purse": cmd_purse, "bal": cmd_purse, "balance": cmd_purse,
    "squad": cmd_squad, "status": cmd_status,
    "bid": cmd_bid, "rtm": cmd_rtm, "myrtm": cmd_my_rtm, "my_rtm": cmd_my_rtm,
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
    "dtime": cmd_dtime, "timers": cmd_dtime,
    "bidtimer": cmd_bid_timer, "bidduration": cmd_bid_timer,
    "antisnipe": cmd_antisnipe,
    "rtmwindow": cmd_rtm_window,
    "rtmcounter": cmd_rtm_counter,
    "rtmdecision": cmd_rtm_decision,
    "leaderboard": cmd_leaderboard, "help": cmd_help,
    "mute": cmd_mute, "unmute": cmd_unmute,
    "setrtm": cmd_set_rtm,
    "bulkplayer": cmd_bulk_player,
    "auctionowners": cmd_auction_owners,
    "soldplayers": cmd_sold_players,
    "unsoldplayers": cmd_unsold_players,
    "auctionsummary": cmd_auction_summary, "auction_summary": cmd_auction_summary,
    "right_to_match": cmd_rtm, "rtm": cmd_rtm,
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
    app.add_handler(CommandHandler(["rtm","right_to_match"], cmd_rtm))
    app.add_handler(CommandHandler(["myrtm","my_rtm"], cmd_my_rtm))
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
    app.add_handler(CommandHandler(["auctionsummary","auction_summary"], cmd_auction_summary))

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
    app.add_handler(CommandHandler(["downloaddata","exportdata"], cmd_download_data))
    app.add_handler(CommandHandler(["downloadtemplate","template"], cmd_download_template))
    app.add_handler(CommandHandler(["uploaddata","importdata"], cmd_upload_data))
    app.add_handler(CommandHandler(["undo","undosold"], cmd_undo))
    app.add_handler(CommandHandler(["setlimit","squadlimit"], cmd_set_limit))
    app.add_handler(CommandHandler(["setincrement","increment"], cmd_set_increment))
    app.add_handler(CommandHandler(["setsquadlimit"], cmd_set_squad_limit))
    app.add_handler(CommandHandler(["findplayer","search"], cmd_find_player))
    app.add_handler(CommandHandler(["playercard","pc"], cmd_player_card))
    app.add_handler(CommandHandler(["mystats","stats"], cmd_my_stats))
    app.add_handler(CommandHandler(["announce","broadcast"], cmd_announce))
    app.add_handler(CommandHandler(["transfer","tradeplayer"], cmd_transfer))
    app.add_handler(CommandHandler("autosell", cmd_auto_sell))
    app.add_handler(CommandHandler("autonext", cmd_auto_next))
    app.add_handler(CommandHandler(["dtime","timers"], cmd_dtime))
    app.add_handler(CommandHandler(["bidtimer","bidduration"], cmd_bid_timer))
    app.add_handler(CommandHandler("antisnipe", cmd_antisnipe))
    app.add_handler(CommandHandler("rtmwindow", cmd_rtm_window))
    app.add_handler(CommandHandler("rtmcounter", cmd_rtm_counter))
    app.add_handler(CommandHandler("rtmdecision", cmd_rtm_decision))

    # Admin: queue
    app.add_handler(CommandHandler(["addtoqueue","atq"], cmd_add_to_queue))
    app.add_handler(CommandHandler(["addtoqueueunsolds","atqu"], cmd_atq_unsolds))
    app.add_handler(CommandHandler(["removefromqueue","rfq"], cmd_remove_from_queue))
    app.add_handler(CommandHandler(["shufflequeue","sq"], cmd_shuffle_queue))
    app.add_handler(CommandHandler("swapqueue", cmd_swap_queue))
    app.add_handler(CommandHandler("clearqueue", cmd_clear_queue))
    app.add_handler(CommandHandler(["queue","q"], cmd_view_queue))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(r"^."), dot_handler))

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
    # Restore state after bot is fully started
    restored = await restore_live_state_async(app.bot)
    if restored:
        logger.info(
            f"✅ State restored: '{live.auction_name}' "
            f"active={live.active} paused={live.paused} "
            f"sold={live.sold_count} queue={len(live.player_queue)}"
        )
        if live.chat_id:
            try:
                await app.bot.send_message(
                    chat_id=live.chat_id,
                    text=(
                        f"🔄 *Bot restarted — Auction state restored!*\n"
                        f"🏏 *{md_safe(live.auction_name)}*\n"
                        f"Sold: {live.sold_count} | Unsold: {live.unsold_count} "
                        f"| Queue: {len(live.player_queue)}\n\n"
                        f"Admin: use /resumeauction to continue."
                    ),
                    parse_mode="Markdown",
                )
            except Exception:
                pass
    else:
        logger.info("No prior auction state to restore.")


def main():
    if not Config.BOT_TOKEN or "YOUR_TOKEN" in Config.BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set!")
    if not Config.SUPER_ADMIN_ID:
        raise ValueError("SUPER_ADMIN_ID not set!")

    logger.info("Starting IPL Auction Bot v4.0...")

    # Attempt a quick synchronous restore from DB (Layer 3) so
    # /status works immediately even before Telegram restore completes
    restore_live_state()

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
