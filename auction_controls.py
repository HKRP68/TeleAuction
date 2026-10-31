"""Auction desk commands and per-auction clock policy."""
import asyncio
import csv
import io
import json
import time
from html import escape
from telegram.error import TelegramError
import auction_rules

b = None
finish_task = None
finish_mode = 'quick'
last_person_bid = {}
last_team_bid = {}
last_auction_bid = {}


def setting(key, default):
    return b.db.get_setting(f'auction:{b.live.auction_id}:{key}', default)


def clock():
    return [int(x) for x in setting('clock', '60 40 20 10 5').split()]


def next_amount():
    pr = b.db.get_player(b.live.current_player_id)
    return b.live.current_bid + max(1, b.live.min_increment) if b.live.current_bid else max(1, pr['base_price'] or b.live.min_increment)


def bid_error(caller, team, amount, typed):
    now = time.monotonic()
    key = (b.live.auction_id, caller)
    if now - last_person_bid.get(key, -100) < 1:
        return 'Please wait one second between bids.'
    if now - last_auction_bid.get(b.live.auction_id, -100) < float(setting('bidgap', '0')):
        return 'The auction bid gap is active. Please wait before bidding.'
    if setting('direct', 'on') == 'off' and amount != next_amount():
        return f'Next step only: /bid {next_amount()}.'
    return None


def note_bid(caller, team):
    now = time.monotonic()
    last_person_bid[(b.live.auction_id, caller)] = now
    last_team_bid[(b.live.auction_id, team)] = now
    last_auction_bid[b.live.auction_id] = now


def pid(item):
    return item['player_id'] if hasattr(item, 'keys') else int(item)


def resolve(value):
    value = value.strip().lstrip('#')
    if value.isdigit():
        row = b.db.get_player(int(value))
        if row and row['auction_id'] == b.live.auction_id:
            return row
    rows = b.db.cx.execute('SELECT * FROM players WHERE auction_id=? AND lower(name)=lower(?)',
                           (b.live.auction_id, value)).fetchall()
    if len(rows) != 1:
        raise ValueError(f'Use an exact unique player name or Player ID: {value}')
    return rows[0]


def dequeue(player_id):
    b.live.player_queue = [x for x in b.live.player_queue if pid(x) != player_id]


def clear_lot():
    if b.live.timer_task and not b.live.timer_task.done():
        b.live.timer_task.cancel()
    b.live.current_player_id = None
    b.live.current_bid = 0
    b.live.highest_bidder_id = None
    b.live.highest_bidder_name = ''
    b.live.timer_ends_at = None
    b.live.rtm_state = b.RTM_NONE


def in_squad(player_id):
    return any(player_id in json.loads(t['squad']) for t in b.db.get_all_parts(b.live.auction_id))


def refund(player):
    aid = b.live.auction_id
    if player['status'] != 'sold' or player['sold_to'] is None:
        raise ValueError('Only a sold player can be recalled.')
    team = b.db.get_part(aid, player['sold_to'])
    if not team:
        raise ValueError('Buyer team is missing.')
    price = player['sold_price'] or 0
    squad = [x for x in json.loads(team['squad']) if x != player['player_id']]
    with b.db.cx:
        b.db.cx.execute('UPDATE participants SET purse=purse+?,total_spent=MAX(0,total_spent-?),squad=? WHERE auction_id=? AND user_id=?',
                        (price, price, json.dumps(squad), aid, team['user_id']))
        b.db.cx.execute("UPDATE bid_history SET won=0 WHERE auction_id=? AND player_id=?", (aid, player['player_id']))
        b.db.cx.execute("UPDATE players SET status='available',sold_to=NULL,sold_price=NULL,sold_msg_id=NULL,sold_chat_id=NULL WHERE player_id=?", (player['player_id'],))
    b.live.sold_count = max(0, b.live.sold_count - 1)
    b.live.undo_snapshot = None


async def summary(update, context):
    teams = b.db.get_all_parts(b.live.auction_id)
    name = ' '.join(context.args).strip().lower()
    if name:
        teams = [t for t in teams if t['team_name'].lower() == name]
    if not teams:
        raise ValueError('Team not found.')
    ar = b.db.get_auction(b.live.auction_id)
    for team in teams:
        lines = [f"{team['team_name']} · {len(json.loads(team['squad']))}/{ar['max_players']} players (min {ar['min_players']})",
                 f"Spent {b.fmt(team['total_spent'])} · left {b.fmt(team['purse'])}", auction_rules.describe(b)]
        players = [b.db.get_player(x) for x in json.loads(team['squad'])]
        for p in sorted((p for p in players if p), key=lambda p: (p['category'], p['name'])):
            lines.append(f"[{p['category'] or p['role']}] #{p['player_id']} {p['name']} · {p['gender'] or 'Unspecified'} · rating {p['rating'] or '—'} · bat {p['bat_rating'] or '—'} · bowl {p['bowl_rating'] or '—'} · {b.fmt(p['sold_price'] or 0)}")
        text = '\n'.join(lines)
        for i in range(0, len(text), 3900):
            await update.message.reply_text(text[i:i+3900])


async def sets_export(update, context):
    fmt = context.args[0].lower() if context.args else 'json'
    if fmt not in ('json', 'csv'):
        raise ValueError('Usage: /asetsexport [json|csv]')
    columns = ('player_id',) + b.DRAFT_PLAYER_COLUMNS + b.DRAFT_OPTIONAL_PLAYER_COLUMNS + ('base_price', 'role', 'nationality', 'ipl_team')
    rows = [dict(r) for r in b.db.cx.execute('SELECT * FROM players WHERE auction_id=? ORDER BY CAST(set_no AS INTEGER),player_id', (b.live.auction_id,))]
    rows = [{k: r[k] for k in columns} for r in rows]
    if fmt == 'json':
        raw = json.dumps(rows, ensure_ascii=False, indent=2)
    else:
        out = io.StringIO(); writer = csv.DictWriter(out, fieldnames=columns)
        writer.writeheader(); writer.writerows(rows); raw = out.getvalue()
    await update.message.reply_document(document=io.BytesIO(raw.encode()), filename=f'auction_sets.{fmt}')


async def sets_import(update, context):
    if context.args not in ([], ['replace']):
        raise ValueError('Usage: /asetsimport [replace]')
    message = update.message.reply_to_message
    if not message or not message.document:
        raise ValueError('Reply to a JSON or CSV document.')
    if message.document.file_size and message.document.file_size > 5_000_000:
        raise ValueError('Maximum import size is 5 MB.')
    doc = message.document
    raw = bytes(await (await doc.get_file()).download_as_bytearray()).decode('utf-8-sig')
    if doc.file_name.lower().endswith('.json'):
        rows = json.loads(raw)
    elif doc.file_name.lower().endswith('.csv'):
        rows = list(csv.DictReader(io.StringIO(raw)))
    else:
        raise ValueError('Use a .json or .csv file.')
    token, data = auction_rules.preview(b, rows, replace=bool(context.args), actor=update.effective_user.id)
    counts = {action: sum(p['action']==action for p in data['changes']) for action in ('new','updated','unchanged','protected')}
    await update.message.reply_text(f"Preview: {counts}; withdraw {len(data['withdrawn'])}.\n" + '\n'.join(data['warnings']) + f'\nReview full changes: /asetsreview {token}\nApply within 30 minutes: /asetsconfirm {token}')


def import_rows(rows, replace=False):
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ValueError('Expected a list of player records.')
    if b.live.current_player_id:
        raise ValueError('Finish the current lot before importing sets.')
    if finish_task and not finish_task.done():
        raise ValueError('Finish playback is running.')
    aid = b.live.auction_id
    columns = b.DRAFT_PLAYER_COLUMNS + b.DRAFT_OPTIONAL_PLAYER_COLUMNS + ('base_price', 'role', 'nationality', 'ipl_team')
    prepared = prepare_rows(rows)
    kept = set()
    with b.db.cx:
        for player_id, values in sorted(prepared, key=lambda item: item[0] is None):
            old = b.db.get_player(player_id) if player_id else None
            if old:
                if old['status'] in ('sold', 'drafted') or in_squad(player_id):
                    kept.add(player_id); continue
                b.db.cx.execute(f"UPDATE players SET {','.join(k+'=?' for k in columns)} WHERE player_id=?", (*values.values(), player_id))
            else:
                cur = b.db.cx.execute(f"INSERT INTO players(auction_id,player_id,{','.join(columns)}) VALUES({','.join('?' for _ in range(len(columns)+2))})", (aid, player_id, *values.values()))
                player_id = cur.lastrowid
            kept.add(player_id)
        if replace:
            for player in b.db.get_available(aid):
                if player['player_id'] not in kept and not in_squad(player['player_id']):
                    b.db.cx.execute("UPDATE players SET status='withdrawn' WHERE player_id=?", (player['player_id'],))
    b.live.player_queue = list(b.db.cx.execute("SELECT * FROM players WHERE auction_id=? AND status='available' ORDER BY CAST(set_no AS INTEGER),player_id", (aid,)))
    b.save_live_state()


async def handle(command, update, context):
    global finish_mode, finish_task
    await b._reg(update.effective_user)
    if not b.db.is_admin(update.effective_user.id):
        await update.message.reply_text('Admin only.'); return
    if not b.live.auction_id:
        await update.message.reply_text('Create an auction first.'); return
    try:
        if finish_task and not finish_task.done() and command not in ('afinish', 'ateamsummary', 'asummary'):
            raise ValueError('Finish playback is running. Use /afinish quick, slow or mute.')
        auction_rules.audit(b, 'command_requested', {'command':command,'args':list(context.args)}, update.effective_user.id)
        args = context.args; arg = ' '.join(args).strip(); aid = b.live.auction_id
        if command == 'atimer':
            if args:
                nums = [int(x) for x in args]
                if len(nums) not in (1, 5) or min(nums) < 1 or max(nums) > 3600 or (len(nums) == 5 and not all(a > c for a, c in zip(nums, nums[1:]))):
                    raise ValueError('Use /atimer seconds or /atimer open reset warning warning countdown, in descending order (1–3600).')
                b.db.set_setting(f'auction:{aid}:clock', ' '.join(args))
            nums = clock()
            text = f'Classic clock: {nums[0]}s, reset on each bid.' if len(nums) == 1 else f'Open {nums[0]}s · bids under {nums[1]}s reset to {nums[1]}s · warnings {nums[2]}s & {nums[3]}s · count {nums[4]}→1.'
        elif command in ('abidgap', 'adirect'):
            key = 'bidgap' if command == 'abidgap' else 'direct'
            if args:
                if len(args) != 1: raise ValueError(f'Usage: /{command} value')
                if key == 'direct' and arg not in ('on', 'off'): raise ValueError('Use on or off.')
                value = str(int(arg)) if key == 'bidgap' and arg != 'off' else ('0' if key == 'bidgap' else arg)
                if key == 'bidgap' and not 0 <= int(value) <= 3600: raise ValueError('Gap must be 0–3600 seconds or off.')
                b.db.set_setting(f'auction:{aid}:{key}', value)
            text = f'{command}: {setting(key, "0" if key == "bidgap" else "on")}'
        elif command == 'asetsexport': return await sets_export(update, context)
        elif command == 'asetsimport': return await sets_import(update, context)
        elif command in ('ateamsummary', 'asummary'): return await summary(update, context)
        elif command == 'astart':
            if not b.live.active: await b.cmd_start_auction(update, context)
            if b.live.active and not b.live.current_player_id: await b._do_next(context, update.effective_chat.id)
            return
        elif command == 'apause':
            if b.live.paused:
                raise ValueError('Auction is already paused.')
            if b.live.rtm_state != b.RTM_NONE: raise ValueError('Resolve RTM before pausing the clock.')
            b.live.paused_remaining = max(1, (b.live.timer_ends_at or time.time()+clock()[0])-time.time())
            return await b.cmd_pause(update, context)
        elif command == 'aresume':
            if not b.live.paused and b.live.active:
                raise ValueError('Auction is already running.')
            if b.live.timer_task and not b.live.timer_task.done():
                b.live.timer_task.cancel()
            await b.cmd_resume(update, context)
            if b.live.current_player_id:
                b.live.timer_ends_at = time.time() + getattr(b.live, 'paused_remaining', clock()[0])
                b.live.timer_task = asyncio.create_task(b.bid_timer(context))
            return
        elif command == 'anext': return await b.cmd_next(update, context)
        elif command == 'asold': return await b.cmd_sold(update, context)
        elif command == 'aextend':
            seconds = int(arg or 10)
            if not 1 <= seconds <= 3600: raise ValueError('Extension must be 1–3600 seconds.')
            if not b.live.current_player_id or b.live.rtm_state != b.RTM_NONE: raise ValueError('No bidding lot on the block.')
            if b.live.paused: b.live.paused_remaining = getattr(b.live, 'paused_remaining', clock()[0]) + seconds
            else: b.live.timer_ends_at = max(time.time(), b.live.timer_ends_at or time.time()) + seconds
            text = f'Added {seconds}s.'
        elif command in ('awithdraw', 'areinstate', 'aunsold', 'aforce', 'arecall'):
            targets = [resolve(x) for x in arg.split(',')] if arg else ([b.db.get_player(b.live.current_player_id)] if command == 'aunsold' and b.live.current_player_id else [])
            if not targets: raise ValueError(f'Usage: /{command} <player | Player ID>')
            if command in ('aforce', 'arecall') and len(targets) != 1: raise ValueError('Choose one player.')
            for p in targets:
                current = p['player_id'] == b.live.current_player_id
                if command == 'arecall':
                    if current: raise ValueError('Player is currently on the block.')
                    continue
                if p['sold_to'] is not None or in_squad(p['player_id']) or p['status'] in ('sold', 'drafted'): raise ValueError('Player belongs to a squad.')
                if current and (b.live.highest_bidder_id is not None or b.live.rtm_state != b.RTM_NONE): raise ValueError('Player has a standing bid or RTM decision.')
                if command == 'areinstate' and p['status'] != 'withdrawn': raise ValueError('Player is not withdrawn.')
                if command == 'aforce' and p['status'] == 'withdrawn': raise ValueError('Reinstate this player first.')
            for p in targets:
                player_id = p['player_id']; dequeue(player_id)
                if command == 'arecall': refund(p)
                if player_id == b.live.current_player_id: clear_lot()
                status = {'awithdraw': 'withdrawn', 'aunsold': 'unsold'}.get(command, 'available')
                b.db.set_player_status(player_id, status)
                if status == 'available': b.live.player_queue.insert(0, b.db.get_player(player_id))
            b.live.unsold_count = len(b.db.get_unsold(aid))
            if command in ('aforce', 'arecall') and b.live.active and not b.live.current_player_id:
                await b._do_next(context, update.effective_chat.id)
            text = f'{command}: {len(targets)} player(s).'
        elif command == 'aundobid':
            if not b.live.current_player_id or b.live.highest_bidder_id is None: raise ValueError('No standing bid.')
            if b.live.rtm_state != b.RTM_NONE: raise ValueError('Resolve RTM first.')
            player_id = b.live.current_player_id
            with b.db.cx:
                b.db.cx.execute('DELETE FROM bid_history WHERE id=(SELECT MAX(id) FROM bid_history WHERE auction_id=? AND player_id=? AND won=0 AND id>?)', (aid, player_id, b.live.lot_bid_floor))
            previous = b.db.cx.execute('SELECT * FROM bid_history WHERE auction_id=? AND player_id=? AND won=0 AND id>? ORDER BY id DESC LIMIT 1', (aid, player_id, b.live.lot_bid_floor)).fetchone()
            b.live.current_bid = previous['bid_amount'] if previous else 0
            b.live.highest_bidder_id = previous['user_id'] if previous else None
            b.live.highest_bidder_name = b.bid_display(b.db.get_part(aid, previous['user_id'])) if previous else ''
            text = f'Standing bid voided. Current: {b.live.current_bid}.'
        elif command == 'arestart':
            if arg != 'confirm': raise ValueError('Use /arestart confirm to undo all sales and reset purses.')
            clear_lot()
            with b.db.cx:
                b.db.cx.execute("UPDATE players SET status='available',sold_to=NULL,sold_price=NULL,sold_msg_id=NULL,sold_chat_id=NULL WHERE auction_id=? AND status!='withdrawn'", (aid,))
                b.db.cx.execute("UPDATE participants SET purse=?,total_spent=0,squad='[]' WHERE auction_id=?", (b.db.get_auction(aid)['purse'], aid))
                b.db.cx.execute('DELETE FROM bid_history WHERE auction_id=?', (aid,))
                b.db.cx.execute('UPDATE draft_orders SET picked_player_id=NULL,picked_at=NULL WHERE auction_id=?', (aid,))
            b.live.player_queue = list(b.db.cx.execute("SELECT * FROM players WHERE auction_id=? AND status='available' ORDER BY CAST(set_no AS INTEGER),player_id", (aid,)))
            b.live.sold_count = b.live.unsold_count = 0
            b.live.undo_snapshot = None
            b.live.active = True; b.live.paused = False
            b.db.set_auction_status(aid, 'active')
            text = 'Auction restarted. Sales undone; starting purses restored. Use /anext.'
        elif command == 'aaccel':
            if arg not in ('', 'go'): raise ValueError('Usage: /aaccel [go]')
            players = b.db.get_unsold(aid)
            for p in players:
                if not in_squad(p['player_id']):
                    b.db.restore_player(p['player_id']); dequeue(p['player_id']); b.live.player_queue.append(b.db.get_player(p['player_id']))
            b.live.unsold_count = len(b.db.get_unsold(aid))
            text = f'Re-listed {len(players)} unsold players.'
        elif command == 'aremoveteam':
            teams = b.db.get_all_parts(aid)
            matches = [t for t in teams if t['team_name'].lower() == arg.lower()]
            if len(matches) != 1 or len(teams) < 2: raise ValueError('Choose one exact team name; at least one team must remain.')
            team = matches[0]
            if b.live.highest_bidder_id == team['user_id'] or b.live.rtm_state != b.RTM_NONE: raise ValueError('Resolve the standing bid/RTM first.')
            released = json.loads(team['squad'])
            balance = team['purse'] + team['total_spent']
            others = sorted((t for t in teams if t['user_id'] != team['user_id']), key=lambda t: t['user_id'])
            with b.db.cx:
                for player_id in released:
                    b.db.cx.execute("UPDATE players SET status='available',sold_to=NULL,sold_price=NULL WHERE player_id=?", (player_id,))
                b.db.cx.execute('DELETE FROM team_co_owners WHERE auction_id=? AND primary_user_id=?', (aid, team['user_id']))
                b.db.cx.execute('DELETE FROM participants WHERE auction_id=? AND user_id=?', (aid, team['user_id']))
                b.db.cx.execute('UPDATE bid_history SET won=0 WHERE auction_id=? AND user_id=?', (aid, team['user_id']))
                for i, t in enumerate(others):
                    b.db.cx.execute('UPDATE participants SET purse=purse+? WHERE auction_id=? AND user_id=?', (balance//len(others)+(i < balance%len(others)), aid, t['user_id']))
            for player_id in released: dequeue(player_id); b.live.player_queue.append(b.db.get_player(player_id))
            b.live.team_links = {linked: primary for linked, primary in b.live.team_links.items() if primary != team['user_id'] and linked != team['user_id']}
            b.live.sold_count = len(b.db.get_sold(aid)); b.live.undo_snapshot = None
            text = f'Removed {team["team_name"]}; returned {len(released)} players and shared {balance} purse units.'
        elif command == 'acall':
            text = arg or 'Teams, please join the auction.'
            tags = []; failures = 0; recipients = set()
            for team in b.db.get_all_parts(aid):
                ids = [team['user_id']] + [r['linked_user_id'] for r in b.db.get_co_owners(aid, team['user_id'])]
                for uid in ids:
                    if uid in recipients: continue
                    recipients.add(uid); tags.append(f'<a href="tg://user?id={uid}">{escape(team["team_name"])}</a>')
                    try:
                        if update.message.reply_to_message and update.message.reply_to_message.photo:
                            await context.bot.send_photo(chat_id=uid, photo=update.message.reply_to_message.photo[-1].file_id, caption=text[:1024])
                        else: await context.bot.send_message(chat_id=uid, text=text)
                    except TelegramError: failures += 1
            if update.message.reply_to_message and update.message.reply_to_message.photo:
                await update.message.reply_photo(update.message.reply_to_message.photo[-1].file_id, caption=escape(text[:800]), parse_mode='HTML')
            for i in range(0, len(tags), 15):
                await update.message.reply_text((escape(text) if i == 0 else '') + '\n' + ' · '.join(tags[i:i+15]), parse_mode='HTML')
            await update.message.reply_text(f'Called {len(recipients)} owners/co-owners. DM failures: {failures}.')
            return
        elif command == 'afinish': return await finish(update, context)
        else: raise ValueError('Unknown auction command.')
        b.save_live_state()
        await update.message.reply_text(text)
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        await update.message.reply_text(str(exc) or "Completion search exceeded its depth limit; reduce the pool or adjust squad limits.")


def install(bot):
    global b, finish_task
    last_person_bid.clear(); last_team_bid.clear(); last_auction_bid.clear()
    b = bot
    finish_task = None
    names = 'atimer abidgap adirect asetsexport asetsimport awithdraw areinstate astart apause aresume anext arestart aextend asold aunsold aforce arecall aundobid aaccel afinish acall aremoveteam ateamsummary asummary'.split()
    def handler(name):
        async def run(update, context):
            await handle(name, update, context)
        return run
    return {name: handler(name) for name in names}


def finish_plan():
    """Find a complete minimum-size allocation before committing any simulated sale."""
    aid = b.live.auction_id
    ar = b.db.get_auction(aid)
    teams = [dict(t) for t in b.db.get_all_parts(aid)]
    if not teams: raise ValueError('No teams to complete.')
    pool = [dict(p) for p in b.db.cx.execute("SELECT * FROM players WHERE auction_id=? AND status IN ('available','unsold') ORDER BY CAST(set_no AS INTEGER),player_id", (aid,)) if not in_squad(p['player_id'])]
    limits = auction_rules.current(b)
    def counts(players):
        return auction_rules.counts(players, limits)
    states = {}
    for team in teams:
        squad = [b.db.get_player(x) for x in json.loads(team['squad'])]
        states[team['user_id']] = {'purse': team['purse'], 'size': len(squad), 'counts': counts([p for p in squad if p])}
    standing = None
    if b.live.current_player_id and b.live.highest_bidder_id is not None:
        standing = (b.live.current_player_id, b.live.highest_bidder_id, b.live.current_bid)
        p = dict(b.db.get_player(standing[0])); state = states[standing[1]]
        if state['purse'] < standing[2]: raise ValueError('Standing bid exceeds purse.')
        state['purse'] -= standing[2]; state['size'] += 1
        state['counts'] = counts([*(b.db.get_player(x) for x in json.loads(b.db.get_part(aid, standing[1])['squad'])), p])
        pool = [x for x in pool if x['player_id'] != standing[0]]
    for state in states.values():
        if state['size'] > ar['max_players'] or any(hi > 0 and state['counts'].get(k,0) > hi for k,(hi,lo) in limits.items()):
            raise ValueError('An existing squad exceeds its limits; correct it before finishing.')
    plan = [standing] if standing else []
    attempts = 0
    def needs(state):
        return auction_rules.needed(state['size'], state['counts'], limits, ar['min_players'])
    def eligible(state, p):
        keys = [k for k in limits if auction_rules.matches(p,k)]
        price = max(1, p['base_price'])
        if state['size'] >= ar['max_players'] or state['purse'] < price: return False
        for k in keys:
            if k in limits and limits[k][0] > 0 and state['counts'].get(k,0) >= limits[k][0]: return False
        return True
    def search(remaining):
        nonlocal attempts
        attempts += 1
        if attempts > 100000: raise ValueError('Completion search exceeded its limit. Adjust the pool/limits and retry; no sales changed.')
        pending = [(uid, s) for uid,s in states.items() if needs(s)]
        if not pending: return True
        if sum(needs(s) for _,s in pending) > len(remaining): return False
        for k,(_,lo) in limits.items():
            if sum(max(0,lo-s['counts'].get(k,0)) for _,s in pending) > sum(auction_rules.matches(p,k) for p in remaining): return False
        uid, state = min(pending, key=lambda pair: sum(eligible(pair[1], p) for p in remaining)-needs(pair[1]))
        if state['size']+needs(state) > ar['max_players']: return False
        candidates = [p for p in remaining if eligible(state,p)]
        if len(candidates)<needs(state) or sum(sorted(max(1,p['base_price']) for p in candidates)[:needs(state)])>state['purse']: return False
        candidates.sort(key=lambda p: (-sum(state['counts'].get(k,0)<limits[k][1] for k in limits if auction_rules.matches(p,k)), max(1,p['base_price']), p['player_id']))
        for p in candidates:
            price = max(1,p['base_price']); old_counts = state['counts'].copy()
            state['size'] += 1; state['purse'] -= price
            for k in limits:
                if auction_rules.matches(p,k): state['counts'][k] = state['counts'].get(k,0)+1
            plan.append((p['player_id'], uid, price))
            if search([x for x in remaining if x['player_id']!=p['player_id']]): return True
            plan.pop(); state['size'] -= 1; state['purse'] += price; state['counts'] = old_counts
        return False
    if not search(pool): raise ValueError('Cannot complete every squad with the available players, purses and limits. No sales changed.')
    return plan


async def finish(update, context):
    global finish_mode, finish_task
    arg = ' '.join(context.args)
    if finish_task and not finish_task.done():
        if arg not in ('quick','slow','mute'): raise ValueError('Playback modes: /afinish quick | slow | mute')
        finish_mode = arg
        await update.message.reply_text(f'Playback: {arg}.'); return
    if arg not in ('', 'go', 'go quick'): raise ValueError('Start with /afinish [go | go quick].')
    if b.live.rtm_state != b.RTM_NONE: raise ValueError('Resolve RTM before finishing.')
    if not b.live.active: raise ValueError('Start the auction first.')
    plan = finish_plan()
    remaining_lots = [p['player_id'] for p in b.db.cx.execute("SELECT * FROM players WHERE auction_id=? AND status IN ('available','unsold') ORDER BY CAST(set_no AS INTEGER),player_id", (b.live.auction_id,)) if not in_squad(p['player_id'])]
    allocations = {p: (p, uid, price) for p, uid, price in plan}
    events = [allocations.get(p, (p, None, 0)) for p in remaining_lots]
    finish_mode = 'quick' if arg == 'go quick' else 'slow'
    # Commit the planned results together; playback is a view of durable results.
    clear_lot(); b.live.paused = True
    aid = b.live.auction_id
    with b.db.cx:
        for player_id, uid, price in plan:
            team = b.db.get_part(aid, uid); p = b.db.get_player(player_id)
            squad = json.loads(team['squad']); squad.append(player_id)
            b.db.cx.execute('UPDATE participants SET purse=purse-?,total_spent=total_spent+?,squad=? WHERE auction_id=? AND user_id=?', (price,price,json.dumps(squad),aid,uid))
            b.db.cx.execute("UPDATE players SET status='sold',sold_to=?,sold_price=? WHERE player_id=?", (uid,price,player_id))
            b.db.cx.execute('INSERT INTO bid_history(auction_id,user_id,player_id,player_name,bid_amount,won) VALUES(?,?,?,?,?,1)', (aid,uid,player_id,p['name'],price))
        b.db.cx.execute("UPDATE players SET status='unsold' WHERE auction_id=? AND status='available'", (aid,))
    b.live.sold_count = len(b.db.get_sold(aid)); b.live.unsold_count = len(b.db.get_unsold(aid))
    b.live.player_queue = []; b.live.undo_snapshot = None; b.live.active = False
    b.db.set_auction_status(aid, 'completed'); b.save_live_state()
    async def playback():
        try:
            index = 0
            while index < len(events) and finish_mode != 'mute':
                size = 5 if finish_mode == 'quick' else 1
                lines = []
                for player_id, uid, price in events[index:index+size]:
                    result = f"{b.db.get_part(aid,uid)['team_name']} · {b.fmt(price)}" if uid is not None else 'UNSOLD'
                    lines.append(f"#{player_id} {b.db.get_player(player_id)['name']} → {result}")
                await update.message.reply_text('\n'.join(lines))
                index += size
                await asyncio.sleep(1)
            await b._do_end_auction(context, update.effective_chat.id)
        except Exception:
            b.logger.exception('Finish playback failed; completed results remain in the database')
        finally:
            b.live.paused = False; b.save_live_state()
    finish_task = asyncio.create_task(playback())
    await update.message.reply_text(f'Completed squads with {len(plan)} simulated sales. Playback: {finish_mode}.')


def prepare_rows(rows):
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise ValueError('Expected a list of player records.')
    aid = b.live.auction_id
    columns = b.DRAFT_PLAYER_COLUMNS + b.DRAFT_OPTIONAL_PLAYER_COLUMNS + ('base_price', 'role', 'nationality', 'ipl_team')
    seen = set(); prepared = []
    for row in rows:
        if not str(row.get('name') or '').strip():
            raise ValueError('Every player requires a name.')
        number = int(row.get('set_no') or 1)
        if number < 1: raise ValueError('Set No must be positive.')
        player_id = int(row['player_id']) if row.get('player_id') else None
        if player_id is not None:
            old = b.db.get_player(player_id)
            if player_id < 1 or player_id in seen or (old and old['auction_id'] != aid):
                raise ValueError('Duplicate, invalid or foreign Player ID.')
            seen.add(player_id)
        values = {k: str(row.get(k) or '').strip() for k in columns}
        values['set_no'] = str(number)
        values['base_price'] = int(row.get('base_price') or 0)
        if values['base_price'] < 0: raise ValueError('Base price cannot be negative.')
        values['gender'] = auction_rules.gender(values['gender'])
        values['tier'] = b.normalize_draft_tier(values['tier'])
        values['role'] = auction_rules.role(values['role'] or values['category']) or 'Bat'
        values['nationality'] = values['nationality'] or ('Indian' if values['country'].lower() == 'india' else 'Overseas')
        prepared.append((player_id, values))
    return prepared
