"""Shared squad rules, durable audit events and auction desk extensions."""
import hashlib
import json
import math
import re
import secrets
import time
from html import escape
from flask import abort, jsonify, redirect, request, url_for

ROLES = {'bat':'Bat', 'batsman':'Bat', 'batsmen':'Bat', 'batter':'Bat',
         'bowl':'Bowl', 'bowler':'Bowl', 'bowlers':'Bowl',
         'ar':'AR', 'alr':'AR', 'allrounder':'AR', 'allrounders':'AR',
         'wk':'WK', 'wicketkeeper':'WK', 'wicketkeepers':'WK'}


def role(value):
    return ROLES.get(re.sub(r'[\s_-]+', '', str(value or '').lower()), str(value or ''))


def gender(value):
    raw = str(value or '').strip()
    return {'f':'Women','female':'Women','woman':'Women','women':'Women',
            'm':'Men','male':'Men','man':'Men','men':'Men'}.get(raw.lower(), raw)


def key(value):
    raw = re.sub(r'[\s_-]+', '', value.lower()).replace('≤','<=')
    if raw in ROLES: return ROLES[raw]
    if raw in ('woman','women','female'): return 'Women'
    if raw in ('man','men','male'): return 'Men'
    if raw in ('os','overseas'): return 'OS'
    if raw == 'indian': return 'Indian'
    match = re.fullmatch(r'(?:rating<=([0-9]+(?:\.[0-9]+)?)|([0-9]+(?:\.[0-9]+)?)rating)',raw)
    if match:
        number = float(match.group(1) or match.group(2))
        if not math.isfinite(number): raise ValueError('Rating threshold must be finite.')
        return f'Rating<={number:g}'
    raise ValueError(f'Unknown category: {value}. Use Batsman, Bowler, Allrounder, Wicketkeeper, Women, Men, Overseas or Rating<=84.')


def matches(player, category):
    p = dict(player)
    if category in ('Bat','Bowl','AR','WK'):
        return role(p.get('category')) == category or (role(p.get('category')) not in ('Bat','Bowl','AR','WK') and role(p.get('role')) == category)
    if category in ('Women','Men'): return gender(p.get('gender')) == category
    if category in ('OS','Indian'):
        return (p.get('nationality') == 'Overseas') if category == 'OS' else p.get('nationality') == 'Indian'
    if category.startswith('Rating<='):
        try:
            rating = float(p.get('rating',''))
            return math.isfinite(rating) and rating <= float(category.split('<=')[1])
        except (TypeError,ValueError): return False
    return False


def counts(players, limits):
    return {k: sum(matches(p,k) for p in players if p) for k in limits}


def needed(size, counted, limits, minimum):
    deficits = {k: max(0,lo-counted.get(k,0)) for k,(_,lo) in limits.items()}
    return max([0, minimum-size, sum(deficits.get(k,0) for k in ('Bat','Bowl','AR','WK')),
                sum(deficits.get(k,0) for k in ('Women','Men')),
                sum(deficits.get(k,0) for k in ('OS','Indian'))] + list(deficits.values()))


def current(bot):
    saved = bot.db.get_setting(f'auction:{bot.live.auction_id}:rules')
    if saved is not None: return json.loads(saved)
    return bot.live.squad_limits if bot.live.rules_auction_id in (None,bot.live.auction_id) else {}


def validate(limits, minimum, maximum):
    if not 1 <= minimum <= maximum <= 100: raise ValueError('Squad size must be 1–100 and minimum ≤ maximum.')
    for k,(hi,lo) in limits.items():
        if key(k) != k: raise ValueError('Use normalized category names.')
        if lo < 0 or hi < 0 or (hi and lo > hi) or lo > maximum:
            raise ValueError(f'{k}: use minimum_maximum, with maximum ≥ minimum. Maximum 0 means no category cap.')
    rating_rules = [(float(k.split('<=')[1]),hi,lo) for k,(hi,lo) in limits.items() if k.startswith('Rating<=')]
    if any(a<=c and d>0 and lo>d for a,hi,lo in rating_rules for c,d,mn in rating_rules):
        raise ValueError('Nested rating minimums conflict with a broader rating maximum.')
    if needed(0,{},limits,minimum) > maximum:
        raise ValueError('Category minimums cannot fit inside the squad maximum. Gender and rating quotas overlap role quotas.')


def parse(text, existing=None):
    limits = dict(existing or {})
    for item in re.split(r'[,;\n]+',text.strip()):
        if not item.strip(): continue
        parts = item.strip().rsplit(None,1)
        if len(parts) != 2: raise ValueError('Use category minimum_maximum, for example: Women 2_6 or Rating<=84 4.')
        k = key(parts[0]); raw = parts[1]
        if raw.lower() in ('off','clear'): limits.pop(k,None); continue
        nums = raw.split('_')
        if len(nums) not in (1,2) or any(not n.isdigit() for n in nums): raise ValueError('Use a minimum (4), or minimum_maximum (4_6).')
        lo = int(nums[0]); hi = int(nums[1]) if len(nums)==2 else 0
        if lo==hi==0: limits.pop(k,None)
        else: limits[k] = (hi,lo)
    return limits


def save(bot, limits, minimum=None, maximum=None):
    if hasattr(bot,'auction_controls') and bot.auction_controls.finish_task and not bot.auction_controls.finish_task.done():
        raise ValueError('Finish playback is running.')
    aid = bot.live.auction_id; ar = bot.db.get_auction(aid)
    before = {"squad":[ar["min_players"],ar["max_players"]],"rules":current(bot)}
    minimum = ar['min_players'] if minimum is None else minimum
    maximum = ar['max_players'] if maximum is None else maximum
    validate(limits,minimum,maximum)
    for team in bot.db.get_all_parts(aid):
        players = [bot.db.get_player(p) for p in json.loads(team['squad'])]
        c = counts(players,limits)
        if len(players)>maximum or any(hi and c[k]>hi for k,(hi,lo) in limits.items()) or len(players)+needed(len(players),c,limits,minimum)>maximum:
            raise ValueError(f'Rules conflict with the existing squad for {team["team_name"]}.')
    with bot.db.cx:
        bot.db.cx.execute('UPDATE auctions SET min_players=?,max_players=? WHERE auction_id=?',(minimum,maximum,aid))
        bot.db.cx.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',(f'auction:{aid}:rules',json.dumps(limits)))
    bot.live.squad_limits = limits
    bot.live.rules_auction_id = aid
    audit(bot,"rules_changed",{"before":before,"after":{"squad":[minimum,maximum],"rules":limits}})
    bot.save_live_state()


def describe(bot):
    ar = bot.db.get_auction(bot.live.auction_id)
    labels = {'Bat':'Batsman','Bowl':'Bowler','AR':'Allrounder','WK':'Wicketkeeper'}
    return f"Squad {ar['min_players']}_{ar['max_players']}\n" + '\n'.join(f'{labels.get(k,k)}: {lo}_{hi or ar["max_players"]}' for k,(hi,lo) in current(bot).items())


def bid_error(bot, team, player, amount, auction):
    limits = current(bot)
    players = [bot.db.get_player(p) for p in json.loads(team['squad'])] + [player]
    c = counts(players,limits)
    for k,(hi,lo) in limits.items():
        if hi and c[k]>hi: return f'{k} maximum is {hi}.'
    slots = auction['max_players']-len(players)
    required = needed(len(players),c,limits,auction['min_players'])
    if required > slots: return 'This bid leaves too few squad slots to meet category minimums.'
    reserve_mode = bot.db.get_setting(f'auction:{bot.live.auction_id}:reserve','auto')
    if reserve_mode != 'off' and required:
        pool = [p for p in bot.db.cx.execute("SELECT * FROM players WHERE auction_id=? AND status IN ('available','unsold') AND player_id!=?",(team['auction_id'],player['player_id']))
                if not bot.auction_controls.in_squad(p['player_id']) and all(not hi or c[k]<hi or not matches(p,k) for k,(hi,lo) in limits.items())]
        if len(pool)<required: return 'Not enough eligible players remain to complete this squad.'
        for k,(_,lo) in limits.items():
            if c[k]+sum(matches(p,k) for p in pool)<lo: return f'Not enough {k} players remain to meet the minimum.'
        floor = 1 if reserve_mode == 'auto' else int(reserve_mode)
        reserve = sum(sorted(max(floor,p['base_price']) for p in pool)[:required])
        if team['purse']-amount < reserve: return f'Keep at least {reserve} purse units reserved for {required} more players. Maximum bid: {max(0,team["purse"]-reserve)}.'
    return None


def init_schema(cx):
    cx.executescript('''
    CREATE TABLE IF NOT EXISTS auction_admins (
        auction_id INTEGER NOT NULL REFERENCES auctions(auction_id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL, granted_by INTEGER NOT NULL,
        granted_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(auction_id,user_id));
    CREATE TABLE IF NOT EXISTS auction_audit (
        id INTEGER PRIMARY KEY, auction_id INTEGER NOT NULL, actor_id INTEGER,
        action TEXT NOT NULL, detail TEXT NOT NULL, ts TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE INDEX IF NOT EXISTS idx_audit_auction ON auction_audit(auction_id,id);
    CREATE TABLE IF NOT EXISTS import_previews (
        token TEXT PRIMARY KEY, auction_id INTEGER NOT NULL, actor_id INTEGER,
        payload TEXT NOT NULL, fingerprint TEXT NOT NULL, expires REAL NOT NULL);
    CREATE TRIGGER IF NOT EXISTS audit_player_insert AFTER INSERT ON players
    BEGIN
      INSERT INTO auction_audit(auction_id,action,detail) VALUES(NEW.auction_id,'player_added',
        json_object('player_id',NEW.player_id,'name',NEW.name,'gender',NEW.gender,'rating',NEW.rating,'category',NEW.category));
    END;
    CREATE TRIGGER IF NOT EXISTS audit_team_delete AFTER DELETE ON participants
    BEGIN
      INSERT INTO auction_audit(auction_id,actor_id,action,detail) VALUES(OLD.auction_id,OLD.user_id,'team_removed',
        json_object('name',OLD.team_name,'purse',OLD.purse,'spent',OLD.total_spent,'squad',OLD.squad));
    END;
    CREATE TRIGGER IF NOT EXISTS audit_purse_update AFTER UPDATE ON participants
    WHEN OLD.purse!=NEW.purse OR OLD.squad!=NEW.squad
    BEGIN
      INSERT INTO auction_audit(auction_id,actor_id,action,detail) VALUES(NEW.auction_id,NEW.user_id,'team_changed',
        json_object('old_purse',OLD.purse,'purse',NEW.purse,'old_squad',OLD.squad,'squad',NEW.squad));
    END;
    CREATE TRIGGER IF NOT EXISTS audit_bid_insert AFTER INSERT ON bid_history
    BEGIN
      INSERT INTO auction_audit(auction_id,actor_id,action,detail) VALUES(NEW.auction_id,NEW.user_id,'bid_recorded',
        json_object('player_id',NEW.player_id,'amount',NEW.bid_amount,'won',NEW.won));
    END;
    CREATE TRIGGER IF NOT EXISTS audit_bid_delete AFTER DELETE ON bid_history
    BEGIN
      INSERT INTO auction_audit(auction_id,actor_id,action,detail) VALUES(OLD.auction_id,OLD.user_id,'bid_removed',
        json_object('player_id',OLD.player_id,'amount',OLD.bid_amount,'won',OLD.won));
    END;
    ''')
    fields = ('player_id','name','rating','category','gender','country','bat_hand','bowl_hand',
              'bowl_style','bat_rating','bowl_rating','version','set_no','status','sold_to','sold_price')
    def snapshot(prefix):
        return "json_object(" + ",".join("'"+k+"',"+prefix+"."+k for k in fields) + ")"
    cx.executescript("DROP TRIGGER IF EXISTS audit_player_update; CREATE TRIGGER audit_player_update AFTER UPDATE ON players BEGIN "
        "INSERT INTO auction_audit(auction_id,action,detail) VALUES(NEW.auction_id,'player_changed',"
        "json_object('player_id',NEW.player_id,'before',"+snapshot('OLD')+",'after',"+snapshot('NEW')+")); END;")



def audit(bot, action, detail, actor=None):
    with bot.db.cx:
        bot.db.cx.execute('INSERT INTO auction_audit(auction_id,actor_id,action,detail) VALUES(?,?,?,?)',
                          (bot.live.auction_id,actor,action,json.dumps(detail)))


def fingerprint(bot):
    aid = bot.live.auction_id
    data = [[dict(p) for p in bot.db.cx.execute('SELECT * FROM players WHERE auction_id=? ORDER BY player_id',(aid,))],
            [dict(t) for t in bot.db.get_all_parts(aid)]]
    return hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()


def preview(bot, rows, replace=False, actor=None):
    if bot.auction_lifecycle.closed(bot): raise ValueError('This auction is cancelled. Restart it before importing players.')
    prepared = bot.auction_controls.prepare_rows(rows)
    existing = [dict(p) for p in bot.db.cx.execute('SELECT * FROM players WHERE auction_id=?',(bot.live.auction_id,))]
    by_id = {p['player_id']:p for p in existing}
    changes=[]; kept=set(); names={}
    for p in existing: names[p['name'].casefold()] = names.get(p['name'].casefold(),0)+1
    for player_id,values in prepared:
        old=by_id.get(player_id); kept.add(player_id)
        name_key=values['name'].casefold()
        if not old or old['name'].casefold()!=name_key: names[name_key]=names.get(name_key,0)+1
        state='new'
        differences={}
        if old:
            if old['status'] in ('sold','drafted') or bot.auction_controls.in_squad(player_id): state='protected'
            else:
                differences={k:{'before':old[k],'after':v} for k,v in values.items() if str(old[k] or '') != str(v)}
                state='updated' if differences else 'unchanged'
        changes.append({'player_id':player_id,'name':values['name'],'action':state,'changes':differences})
    removals=[p for p in existing if replace and p['status']=='available' and p['player_id'] not in kept and not bot.auction_controls.in_squad(p['player_id'])]
    warnings=[f'Duplicate name: {n}; use Player IDs.' for n,count in names.items() if count>1]
    limits=current(bot)
    for player_id,values in prepared:
        if any(k in limits for k in ('Women','Men')) and gender(values.get('gender')) not in ('Women','Men'):
            warnings.append(f"{values['name']}: gender is unspecified or does not match a gender quota.")
        if any(k.startswith('Rating<=') for k in limits):
            try: valid_rating=math.isfinite(float(values.get('rating','')))
            except ValueError: valid_rating=False
            if not valid_rating: warnings.append(f"{values['name']}: missing or invalid rating; cannot count toward rating quotas.")
    token=secrets.token_urlsafe(24)
    data={'rows':rows,'replace':replace,'changes':changes,'withdrawn':[{'player_id':p['player_id'],'name':p['name']} for p in removals],
          'warnings':warnings}
    with bot.db.cx:
        bot.db.cx.execute('DELETE FROM import_previews WHERE expires<?',(time.time(),))
        bot.db.cx.execute('INSERT INTO import_previews VALUES(?,?,?,?,?,?)',(token,bot.live.auction_id,actor,json.dumps(data),fingerprint(bot),time.time()+1800))
    return token,data


def confirm(bot, token, actor=None):
    row=bot.db.cx.execute('SELECT * FROM import_previews WHERE token=?',(token,)).fetchone()
    if not row or row['auction_id']!=bot.live.auction_id or row['actor_id']!=actor or row['expires']<time.time():
        raise ValueError('Preview is missing or expired. Upload again.')
    if row['fingerprint']!=fingerprint(bot): raise ValueError('Auction data changed since this preview. Upload again to review fresh changes.')
    data=json.loads(row['payload'])
    with bot.db.cx:
        bot.db.cx.execute('DELETE FROM import_previews WHERE token=?',(token,))
        bot.auction_controls.import_rows(data['rows'],replace=data['replace'])
    audit(bot,'import_confirmed',{'players':len(data['rows']),'replace':data['replace']},actor)
    return len(data['rows'])


STYLE = '''<style>
:root{--ink:#14243b;--blue:#235ed2;--muted:#65748a;--line:#dce4ee}*{box-sizing:border-box}body{margin:0;background:#f3f6fb;color:var(--ink);font:15px system-ui}header{padding:32px max(20px,calc((100vw - 1160px)/2));background:#14243b;color:white}header a{color:#cce0ff;margin-right:20px}header p{color:#c6d2e3}main{max-width:1160px;margin:auto;padding:24px}.panel{background:white;border:1px solid var(--line);border-radius:14px;padding:24px;margin-bottom:20px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}label{display:block;font-weight:600;margin:16px 0}input,textarea,select{display:block;width:100%;padding:12px;border:1px solid #bccbde;border-radius:7px;font:inherit;margin-top:6px}textarea{min-height:220px;font-family:monospace}button,.button{background:var(--blue);color:white;border:0;padding:12px 18px;border-radius:7px;font:600 15px system-ui;cursor:pointer;text-decoration:none;display:inline-block}.muted,small{color:var(--muted)}.badge{display:inline-block;border-radius:20px;padding:5px 12px;background:#e4f5d2;color:#335b13;font-size:13px}table{border-collapse:collapse;width:100%}td,th{padding:12px;text-align:left;border-bottom:1px solid var(--line)}th{font-size:12px;text-transform:uppercase;color:var(--muted)}.scroll{overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}.hero{font-size:42px;font-weight:750;margin:10px 0}.error{color:#a32323;background:#fff0ed;padding:16px;border-radius:8px}nav{margin-top:20px}progress{width:100%;height:12px}input[type=checkbox]{width:auto;display:inline}h1{margin:0}h2{margin-top:0}a{color:var(--blue)}
</style>'''


def page(title, body):
    return '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+escape(title)+'</title>'+STYLE+'</head><body><header><span class="badge">AUCTION DESK</span><h1>'+escape(title)+'</h1><nav><a href="/draft">Control room</a><a href="/draft/rules">Squad rules</a><a href="/watch">Live board</a><a href="/draft/audit">Audit log</a></nav></header><main>'+body+'</main></body></html>'


def rules_form(bot, error=''):
    ar=bot.db.get_auction(bot.live.auction_id)
    text='\n'.join(f'{k} {lo}_{hi}' for k,(hi,lo) in current(bot).items())
    reserve=bot.db.get_setting(f'auction:{bot.live.auction_id}:reserve','auto')
    intro = '<section class="panel"><h2>Build a balanced squad</h2><p>Choose a squad range, then set category minimums and maximums. A single number is a minimum. Category maximum 0 means no cap.</p>'
    if error: intro += '<p class="error">'+escape(error)+'</p>'
    intro += '</section>'
    return page('Squad rules',intro + f'''<section class="panel"><form method="post"><div class="grid"><label>Squad minimum<input type="number" name="minimum" min="1" max="100" value="{ar['min_players']}" required></label><label>Squad maximum<input type="number" name="maximum" min="1" max="100" value="{ar['max_players']}" required></label></div>
    <label>Category rules<textarea name="rules" placeholder="Batsman 4&#10;Bowler 4&#10;Allrounder 4&#10;Wicketkeeper 2&#10;Women 2&#10;Rating&lt;=84 4">{escape(text)}</textarea></label><p class="muted">Format: Category minimum_maximum. Example: Women 2_6. Rating&lt;=84 means any rating of 84 or lower; change 84 to any number. Clear the field to remove all category rules.</p>
    <label>Purse reserve<select name="reserve"><option value="auto" {'selected' if reserve=='auto' else ''}>Auto — reserve cheapest remaining players</option><option value="off" {'selected' if reserve=='off' else ''}>Off</option></select></label>
    <button>Save rules</button></form></section><section class="panel"><h2>How categories overlap</h2><p>A woman bowler rated 82 counts toward Bowler, Women and Rating ≤84. Role minimums add together; gender and rating quotas can overlap those roles.</p><p>Your example needs at least 14 players by role. A squad range of 15–17 has room for it. Availability and purse budgets still matter.</p><p>Fill player <b>category</b> with Batsman, Bowler, Allrounder or Wicketkeeper, and <b>gender</b> with Women or Men. Female/Male aliases are accepted. Missing ratings and gender never satisfy those quotas.</p></section>''')


def install(bot):
    async def command(update, context, name):
        import io
        await bot._reg(update.effective_user)
        if not bot.db.is_admin(update.effective_user.id):
            await update.message.reply_text('Admin only.'); return
        if not bot.live.auction_id:
            await update.message.reply_text('Create an auction first.'); return
        arg=' '.join(context.args).strip(); actor=update.effective_user.id
        try:
            if name in ('arule','arules','asquad','areserve','asetsconfirm') and bot.auction_controls.finish_task and not bot.auction_controls.finish_task.done():
                raise ValueError('Finish playback is running.')
            if name in ('arule','arules'):
                if arg.lower()=='clear': save(bot,{})
                elif arg and arg.lower()!='show': save(bot,parse(arg,current(bot)))
                await update.message.reply_text(describe(bot)+'\nUse /arule Women 2_6 or /arule Rating<=84 4. A single number is a minimum.');
            elif name=='asquad':
                if arg:
                    lo,hi=map(int,arg.split('_'))
                    save(bot,current(bot),lo,hi)
                await update.message.reply_text(describe(bot))
            elif name=='areserve':
                if arg:
                    if arg not in ('auto','off'): raise ValueError('Use /areserve auto or /areserve off.')
                    bot.db.set_setting(f'auction:{bot.live.auction_id}:reserve',arg)
                await update.message.reply_text('Purse reserve: '+bot.db.get_setting(f'auction:{bot.live.auction_id}:reserve','auto'))
            elif name=='aaudit':
                records=[dict(r) for r in bot.db.cx.execute('SELECT * FROM auction_audit WHERE auction_id=? ORDER BY id',(bot.live.auction_id,))]
                if arg=='json': await update.message.reply_document(document=io.BytesIO(json.dumps(records,indent=2).encode()),filename='auction_audit.json')
                elif not arg:
                    text='\n'.join(f"#{r['id']} {r['ts']} {r['action']} · {r['detail']}" for r in records[-10:]) or 'No audit events yet.'
                    for start in range(0,len(text),3900): await update.message.reply_text(text[start:start+3900])
                else: raise ValueError('Use /aaudit or /aaudit json.')
            elif name=='asetsreview':
                row=bot.db.cx.execute('SELECT * FROM import_previews WHERE token=? AND auction_id=? AND actor_id=? AND expires>?',(arg,bot.live.auction_id,actor,time.time())).fetchone()
                if not row: raise ValueError('Preview missing or expired.')
                await update.message.reply_document(document=io.BytesIO(json.dumps(json.loads(row['payload']),indent=2).encode()),filename='import_preview.json')
            elif name=='asetsconfirm':
                count=confirm(bot,arg,actor)
                await update.message.reply_text(f'Imported {count} players.')
            audit(bot,name,{'args':context.args},actor)
        except (ValueError,TypeError) as exc:
            await update.message.reply_text(str(exc) or 'Invalid input.')
    def handler(name):
        async def run(update,context): await command(update,context,name)
        return run

    @bot.flask_app.route('/draft/rules',methods=['GET','POST'])
    def rules_page():
        bot._web_draft_aid()
        if request.method=='POST':
            try:
                reserve=request.form.get('reserve','auto')
                if reserve not in ('auto','off'): raise ValueError('Reserve must be auto or off.')
                save(bot,parse(request.form.get('rules','')),int(request.form['minimum']),int(request.form['maximum']))
                bot.db.set_setting(f'auction:{bot.live.auction_id}:reserve',reserve)
                audit(bot,'rules_saved',{'rules':current(bot),'reserve':reserve,'squad':[int(request.form['minimum']),int(request.form['maximum'])]})
                return redirect('/draft/rules')
            except (ValueError,KeyError) as exc: return rules_form(bot,str(exc)),400
        return rules_form(bot)

    @bot.flask_app.route('/draft/players/preview',methods=['POST'])
    def players_preview():
        bot._web_draft_aid()
        try:
            upload=request.files.get('file')
            rows=bot.player_io.read_rows(upload.read(),upload.filename)
            token,data=preview(bot,rows,replace=request.form.get('replace')=='1')
            changes=''.join('<tr><td>'+escape(str(p['player_id'] or 'New ID'))+'</td><td>'+escape(p['name'])+'</td><td>'+escape(p['action'])+'</td><td><pre>'+escape(json.dumps(p['changes'],indent=2))+'</pre></td></tr>' for p in data['changes'])
            withdrawn=', '.join(f"#{p['player_id']} {p['name']}" for p in data['withdrawn']) or 'None'
            return page('Review player import',f'<section class="panel"><h2>{len(rows)} player records</h2><p>Review the changes before importing. Owned players are protected. This preview expires in 30 minutes.</p><p>Withdraw from queue: {escape(withdrawn)}</p><p>{escape("; ".join(data["warnings"]))}</p><div class="scroll"><table><tr><th>Player ID</th><th>Name</th><th>Action</th><th>Changes</th></tr>{changes}</table></div><form method="post" action="/draft/players/confirm"><input type="hidden" name="token" value="{escape(token)}"><button>Apply reviewed import</button> <a href="/draft">Cancel</a></form></section>')
        except (ValueError,AttributeError,TypeError) as exc: return page('Import error','<section class="panel error">'+escape(str(exc))+'</section>'),400

    @bot.flask_app.route('/draft/players/confirm',methods=['POST'])
    def players_confirm():
        bot._web_draft_aid()
        try: confirm(bot,request.form.get('token',''))
        except ValueError as exc: return page('Import not applied','<section class="panel error">'+escape(str(exc))+'</section>'),400
        return redirect('/draft')

    @bot.flask_app.route('/draft/audit')
    def audit_page():
        bot._web_draft_aid()
        records=bot.db.cx.execute('SELECT * FROM auction_audit WHERE auction_id=? ORDER BY id DESC LIMIT 200',(bot.live.auction_id,)).fetchall()
        if request.args.get('format')=='json':
            from flask import Response
            return Response(json.dumps([dict(r) for r in bot.db.cx.execute('SELECT * FROM auction_audit WHERE auction_id=? ORDER BY id',(bot.live.auction_id,))],indent=2),mimetype='application/json',headers={'Content-Disposition':'attachment; filename=auction_audit.json'})
        rows=''.join(f'<tr><td>{r["id"]}</td><td>{escape(r["ts"])}</td><td>{escape(r["action"])}</td><td>{r["actor_id"] or "System / web"}</td><td><pre>{escape(r["detail"])}</pre></td></tr>' for r in records)
        return page('Auction audit log',f'<section class="panel"><h2>Latest 200 events</h2><p>Command entries identify the requester; team and bid events identify the affected team or bidder. Automatic database events capture changes made through older controls too.</p><a class="button" href="?format=json">Download full log</a><div class="scroll"><table><tr><th>ID</th><th>Time (UTC)</th><th>Event</th><th>User ID</th><th>Details</th></tr>{rows}</table></div></section>')

    @bot.flask_app.route('/watch/data')
    def watch_data():
        if not bot.live.auction_id: return jsonify({'available':False})
        ar=bot.db.get_auction(bot.live.auction_id); limits=current(bot)
        teams=[]
        for t in bot.db.get_all_parts(bot.live.auction_id):
            players=[dict(p) for p in (bot.db.get_player(i) for i in json.loads(t['squad'])) if p]
            counted=counts(players,limits)
            teams.append({'name':t['team_name'],'purse':t['purse'],'spent':t['total_spent'],'size':len(players),
                          'rules':[{'category':k,'count':counted[k],'minimum':lo,'maximum':hi or ar['max_players']} for k,(hi,lo) in limits.items()],
                          'players':[{k:p[k] for k in ('player_id','name','category','gender','rating','bat_rating','bowl_rating','sold_price')} for p in players]})
        p=bot.db.get_player(bot.live.current_player_id) if bot.live.current_player_id else None
        seconds=math.ceil(bot.live.paused_remaining if bot.live.paused else max(0,(bot.live.timer_ends_at or time.time())-time.time()))
        result={'available':True,'auction':ar['name'],'active':bot.live.active,'paused':bot.live.paused,'seconds':seconds,'bid':bot.live.current_bid,
                'leader':bot.live.highest_bidder_name,'status':ar['status'],'player':{k:p[k] for k in ('player_id','name','set_no','category','gender','rating')} if p else None,
                'minimum':ar['min_players'],'maximum':ar['max_players'],'teams':teams,'queued':len(bot.live.player_queue)}
        response=jsonify(result); response.headers['Cache-Control']='no-store'; return response

    @bot.flask_app.route('/watch')
    def watch_page():
        return page('Live auction board','''<section class="panel"><span class="badge" id="connection">Connecting…</span><h2 id="auction">Auction</h2><div class="grid"><div><small>ON THE BLOCK</small><div class="hero" id="player">Waiting for a lot</div><p id="details"></p></div><div><small>STANDING BID</small><div class="hero" id="bid">—</div><p id="leader"></p></div><div><small>CLOCK</small><div class="hero" id="clock">—</div><p id="state"></p></div></div></section><div id="teams" class="grid"></div><p class="muted">Read-only board · updates every 2 seconds · purse and bid amounts in auction purse units.</p>
<script>
const put=(id,value)=>document.getElementById(id).textContent=value;
function node(tag,text){const el=document.createElement(tag);if(text!==undefined)el.textContent=text;return el;}
async function refresh(){try{const response=await fetch('/watch/data',{cache:'no-store'});if(!response.ok)throw Error();const d=await response.json();if(!d.available){put('connection','No auction created');return;}
put('connection','Live · '+new Date().toLocaleTimeString());put('auction',d.auction);put('player',d.player?d.player.name:'Waiting for a lot');put('details',d.player?'#'+d.player.player_id+' · Set '+d.player.set_no+' · '+(d.player.category||'')+' · '+(d.player.gender||'Unspecified')+' · Rating '+(d.player.rating||'—'):'Queued: '+d.queued);put('bid',d.bid||'No bids');put('leader',d.leader||'Open bidding');put('clock',d.player?d.seconds+'s':'—');put('state',d.paused?'Paused':d.active?'Auction running':d.status==='cancelled'?'Auction cancelled':'Auction not running');
const container=document.getElementById('teams');container.replaceChildren();for(const t of d.teams){const card=node('section');card.className='panel';card.append(node('h2',t.name),node('p',t.size+' / '+d.maximum+' players · minimum '+d.minimum),node('p','Purse '+t.purse+' · spent '+t.spent));const progress=node('progress');progress.max=d.maximum;progress.value=t.size;card.append(progress);for(const rule of t.rules)card.append(node('p',rule.category+': '+rule.count+' (min '+rule.minimum+', max '+rule.maximum+')'));const details=node('details');details.append(node('summary','Full squad ('+t.players.length+')'));for(const p of t.players)details.append(node('p','#'+p.player_id+' '+p.name+' · '+(p.category||'')+' · '+(p.gender||'Unspecified')+' · Rating '+(p.rating||'—')+' · Bat '+(p.bat_rating||'—')+' · Bowl '+(p.bowl_rating||'—')));card.append(details);container.append(card);}}
catch(e){put('connection','Connection lost — retrying…');}finally{setTimeout(refresh,2000);}}refresh();
</script>''')
    return {name:handler(name) for name in ('arule','arules','asquad','areserve','aaudit','asetsconfirm','asetsreview')}
