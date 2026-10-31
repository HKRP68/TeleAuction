"""Bulk registration of teams with one primary owner and multiple co-owners."""
import csv
import io
import json
import re
from html import escape
from flask import request, redirect


def owner_ids(text):
    fields = [part.strip() for part in text.split(',')]
    if not fields or any(not re.fullmatch(r'[0-9]+',field) for field in fields):
        raise ValueError('Owners must be positive Telegram user IDs separated by commas.')
    ids = [int(field) for field in fields]
    if any(uid<=0 or uid>2**63-1 for uid in ids):
        raise ValueError('Owner IDs must be positive Telegram user IDs.')
    if len(set(ids))!=len(ids): raise ValueError('An owner ID is repeated in the same team.')
    return ids


def parse_teams(text):
    rows=[]; names=set(); seen=set()
    for number,line in enumerate(text.splitlines(),1):
        if not line.strip(): continue
        try:
            fields=next(csv.reader(io.StringIO(line),strict=True))
            if len(fields)<2 or not fields[0].strip(): raise ValueError('Use Team Name, primary_owner_id, co_owner_id, …')
            name=fields[0].strip()
            if len(name)>100: raise ValueError('Team names must be at most 100 characters.')
            ids=owner_ids(','.join(fields[1:]))
            if name.casefold() in names: raise ValueError(f'Duplicate team name: {name}.')
            if seen.intersection(ids): raise ValueError('An owner can belong to only one team in this auction.')
            names.add(name.casefold()); seen.update(ids); rows.append((name,ids))
        except (ValueError,csv.Error) as exc:
            raise ValueError(f'Line {number}: {exc}') from exc
    if not rows: raise ValueError('Add at least one line: Team Name, owner_id, co_owner_id, …')
    return rows


def add_teams(bot,aid,text,actor=None):
    rows=parse_teams(text)
    if bot.auction_controls.finish_task and not bot.auction_controls.finish_task.done():
        raise ValueError('Finish playback is running.')
    cx=bot.db.cx
    with cx:
        # Validate and insert under one write lock: no partial batch or competing owners.
        cx.execute('BEGIN IMMEDIATE')
        auction=bot.db.get_auction(aid)
        if not auction: raise ValueError('Create or restore an auction first.')
        if auction['status']=='completed': raise ValueError('This auction is completed. Create or restart an auction before adding teams.')
        teams=bot.db.get_all_parts(aid)
        if len(teams)+len(rows)>auction['max_teams']:
            raise ValueError(f'Too many teams: {len(teams)} registered, {len(rows)} requested, maximum {auction["max_teams"]}.')
        existing_names={t['team_name'].strip().casefold() for t in teams}
        existing_ids={t['user_id'] for t in teams}
        existing_ids.update(r[0] for r in cx.execute('SELECT linked_user_id FROM team_co_owners WHERE auction_id=?',(aid,)))
        for name,ids in rows:
            if name.casefold() in existing_names: raise ValueError(f'Team already exists: {name}. Edit it in /draft instead.')
            conflict=existing_ids.intersection(ids)
            if conflict: raise ValueError(f'Owner ID {min(conflict)} is already assigned to a team in this auction.')
        for name,ids in rows:
            primary=ids[0]
            for uid in ids:
                # Preserve existing usernames and first names; /start can fill unknown users later.
                cx.execute('INSERT OR IGNORE INTO global_users(user_id) VALUES(?)',(uid,))
            user=bot.db.get_user(primary)
            cx.execute('INSERT INTO participants(auction_id,user_id,username,team_name,purse) VALUES(?,?,?,?,?)',
                       (aid,primary,user['username'] or '',name,auction['purse']))
            cx.executemany('INSERT INTO team_co_owners(auction_id,primary_user_id,linked_user_id) VALUES(?,?,?)',
                           [(aid,primary,uid) for uid in ids[1:]])
        cx.execute('INSERT INTO auction_audit(auction_id,actor_id,action,detail) VALUES(?,?,?,?)',
                   (aid,actor,'teams_added',json.dumps({'teams':[{'team_name':name,'owner_ids':ids} for name,ids in rows]})))
    if aid==bot.live.auction_id: bot.save_live_state()
    return rows


async def command(bot,update,context):
    await bot._reg(update.effective_user)
    if not bot.db.is_admin(update.effective_user.id):
        await update.message.reply_text('Admin only.'); return
    if not bot.live.auction_id:
        await update.message.reply_text('Create or restore an auction first.'); return
    # context.args discards newlines; read the original message to keep team rows intact.
    raw=(update.message.text or '').strip()
    pieces=re.split(r'\s+',raw,maxsplit=1)
    payload=pieces[1] if len(pieces)>1 else ''
    if not payload and update.message.reply_to_message:
        payload=update.message.reply_to_message.text or ''
    if not payload:
        await update.message.reply_text('Use one team per line:\n/addteams\nRoyal Tigers, 123456789, 234567890\nBlue Hawks, 345678901, 456789012, 567890123\n\nFirst ID: primary owner. Remaining IDs: co-owners. All share one team purse and squad. You may also reply to a team list with /addteams.')
        return
    try: rows=add_teams(bot,bot.live.auction_id,payload,update.effective_user.id)
    except ValueError as exc:
        await update.message.reply_text(str(exc)); return
    text=f'Added {len(rows)} team(s). Each team starts with {bot.db.get_auction(bot.live.auction_id)["purse"]} purse units.\n'
    text+='\n'.join(f'{name}: primary {ids[0]}' + (f"; co-owners {', '.join(map(str,ids[1:]))}" if len(ids)>1 else '') for name,ids in rows)
    for start in range(0,len(text),3900): await update.message.reply_text(text[start:start+3900])


def install(bot):
    async def handler(update,context): await command(bot,update,context)

    @bot.flask_app.route('/draft/teams/bulk',methods=['POST'])
    def bulk_teams():
        aid=bot._web_draft_aid()
        try: add_teams(bot,aid,request.form.get('teams',''))
        except ValueError as exc:
            return bot.auction_rules.page('Teams not added','<section class="panel error">'+escape(str(exc))+'<p><a href="/draft">Back to control room</a></p></section>'),400
        return redirect('/draft')
    return {'addteams':handler,'addusers':handler}
