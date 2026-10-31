"""One player schema for entry, reviewed imports, templates and exports."""
import csv
import io
import json
import math
import re
from flask import request, abort
import auction_rules

CORE_COLUMNS = ('player_id','set_no','name','rating','category','gender','country','bat_hand',
                'bowl_hand','bowl_style','bat_rating','bowl_rating','version')
EXTRA_COLUMNS = ('tier','icon_eligible','indian_status','base_price','role','nationality','ipl_team')
PLAYER_COLUMNS = CORE_COLUMNS + EXTRA_COLUMNS
WRITE_COLUMNS = tuple(k for k in PLAYER_COLUMNS if k!='player_id')
ENTRY_COLUMNS = ('name','rating','category','gender','country','bat_hand','bowl_hand',
                 'bowl_style','bat_rating','bowl_rating','version','set_no','player_id','base_price')
READ_ONLY = {'auction_id','status','sold_to','sold_price','sold_msg_id','sold_chat_id','buyer_team','sold_price_cr'}
ALIASES = {'player_name':'name','player':'name','playerid':'player_id','id':'player_id',
           'set':'set_no','set_number':'set_no','setno':'set_no','batting_hand':'bat_hand',
           'bowling_hand':'bowl_hand','bowling_style':'bowl_style','batting_rating':'bat_rating',
           'bowling_rating':'bowl_rating','previous_team':'ipl_team','prev_team':'ipl_team','team':'ipl_team',
           'price':'base_price','sex':'gender'}


def header(value):
    name=re.sub(r'[\s-]+','_',str(value or '').strip().lower())
    return ALIASES.get(name,name)


def records(data):
    if isinstance(data,dict): data=[data]
    if not isinstance(data,list) or not data or not all(isinstance(row,dict) for row in data):
        raise ValueError('Expected a non-empty list of player records.')
    result=[]
    for number,row in enumerate(data,1):
        values={}
        for field,value in row.items():
            if field is None: raise ValueError(f'Row {number}: extra CSV values. Quote names containing commas.')
            key=header(field)
            if not key:
                if value not in (None,''): raise ValueError(f'Row {number}: a populated column has no header.')
                continue
            if key in READ_ONLY: continue
            if key not in PLAYER_COLUMNS: raise ValueError(f'Row {number}: unknown player field {field}. Download /downloadtemplate for the format.')
            if key in values: raise ValueError(f'Row {number}: duplicate column {key}.')
            values[key]=value
        result.append(values)
    return result


def read_rows(raw,filename):
    filename=(filename or '').lower()
    if filename.endswith('.xlsx'):
        import openpyxl
        wb=openpyxl.load_workbook(io.BytesIO(raw),data_only=True,read_only=True)
        try:
            ws=wb['Players'] if 'Players' in wb.sheetnames else wb.active
            rows=iter(ws.iter_rows(values_only=True))
            first=next((r for r in rows if any(v is not None and str(v).strip() for v in r)),None)
            if first is None: raise ValueError('The Players sheet is empty.')
            hdrs=[str(v or '').strip() for v in first]
            if len(set(header(h) for h in hdrs if h))!=len([h for h in hdrs if h]): raise ValueError('Duplicate spreadsheet headers.')
            data=[dict(zip(hdrs,r)) for r in rows if any(v is not None and str(v).strip() for v in r)]
        finally: wb.close()
    elif filename.endswith('.json'):
        data=json.loads(raw.decode('utf-8-sig'))
    elif filename.endswith('.csv'):
        reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig')),strict=True)
        hdrs=reader.fieldnames or []
        if len(set(header(h) for h in hdrs))!=len(hdrs): raise ValueError('Duplicate CSV headers.')
        data=list(reader)
    else: raise ValueError('Use .xlsx, .csv or .json. Save older .xls files as .xlsx first.')
    result=records(data)
    if any('name' not in row for row in result): raise ValueError('A name header is required. Use /downloadtemplate.')
    return result


def text_rows(text):
    text=text.strip()
    if text.startswith(('[','{')): return records(json.loads(text))
    first=next(csv.reader(io.StringIO(text)),[])
    if 'name' in [header(v) for v in first]: return read_rows(text.encode(),'players.csv')
    # Name-first records are convenient in chat; files use explicit database headers.
    data=[]
    for number,row in enumerate(csv.reader(io.StringIO(text),strict=True),1):
        if not row or not any(v.strip() for v in row): continue
        if len(CORE_COLUMNS)<=len(row)<=len(PLAYER_COLUMNS) and (not row[0].strip() or row[0].strip().isdigit()) and row[1].strip().isdigit():
            data.append(dict(zip(PLAYER_COLUMNS,row)))
            continue
        if not 11<=len(row)<=len(ENTRY_COLUMNS):
            raise ValueError(f'Line {number}: use name,rating,category,gender,country,bat_hand,bowl_hand,bowl_style,bat_rating,bowl_rating,version[,set_no,player_id,base_price], or JSON with field names.')
        data.append(dict(zip(ENTRY_COLUMNS,row)))
    return records(data)


def integer(value,field,default=None,minimum=0):
    if value is None or str(value).strip()=='': return default
    try:
        number=float(value)
        if not math.isfinite(number) or not number.is_integer() or number<minimum or number>2**53-1: raise ValueError
    except (ValueError,TypeError,OverflowError): raise ValueError(f'{field} must be a whole number ≥ {minimum}.')
    return int(number)


def normalize(bot,row,old=None):
    supplied=records([row])[0]
    # A sparse update preserves omitted fields, including pricing and tier.
    values={k:old[k] if old else '' for k in WRITE_COLUMNS}
    values.update({k:v for k,v in supplied.items() if k!='player_id'})
    for k in WRITE_COLUMNS: values[k]='' if values[k] is None else str(values[k]).strip()
    if not values['name']: raise ValueError('Every player requires a name.')
    values['set_no']=str(integer(values['set_no'],'Set No',1,1))
    values['gender']=auction_rules.gender(values['gender'])
    for field in ('rating','bat_rating','bowl_rating'):
        if values[field]:
            try:
                rating=float(values[field])
                if not math.isfinite(rating) or rating<0: raise ValueError
            except ValueError: raise ValueError(f'{field} must be a non-negative numeric rating or blank.')
    if values['base_price']:
        price=bot.parse_price(values['base_price'])
        if price is None or price<0: raise ValueError('Invalid base_price. Use 2cr, 50l or a non-negative integer in purse units.')
    else: price=0
    values['base_price']=price
    # Preserve both auction and draft tiers across export/import.
    tier=values['tier']
    draft={t.lower():t for t in bot.DRAFT_TIERS}
    legacy={t.lower():t for t in ('Marquee','A','B','C','Uncapped')}
    if tier and tier.lower() not in {**draft,**legacy}: raise ValueError('Invalid tier. Use Platinum/Gold/Silver/Bronze or Marquee/A/B/C/Uncapped.')
    values['tier']={**draft,**legacy}.get(tier.lower(),'Bronze')
    if 'role' in supplied or 'category' in supplied or not old:
        role=auction_rules.role(values['category'])
        values['role']=role if role in ('Bat','Bowl','AR','WK') else auction_rules.role(values['role']) or 'Bat'
        values['category']=values['category'] or values['role']
    if not values['country'] and values['nationality'].lower() in ('indian','india'): values['country']='India'
    if not old or 'country' in supplied or 'nationality' in supplied:
        nat=values['nationality'].lower()
        if 'nationality' in supplied and nat:
            if nat not in ('indian','india','overseas','foreign'): raise ValueError('nationality must be Indian or Overseas.')
            values['nationality']='Indian' if nat in ('indian','india') else 'Overseas'
        else: values['nationality']='Indian' if values['country'].lower()=='india' else 'Overseas'
    return values


def prepare(bot,rows):
    rows=records(rows); seen=set(); prepared=[]
    for number,row in enumerate(rows,1):
        try:
            player_id=integer(row.get('player_id'),'Player ID',minimum=1)
            old=bot.db.get_player(player_id) if player_id else None
            if player_id in seen or (old and old['auction_id']!=bot.live.auction_id):
                raise ValueError('Duplicate or foreign Player ID. Leave player_id blank to create a new player.')
            if player_id: seen.add(player_id)
            values=normalize(bot,row,old)
            prepared.append((player_id,values))
        except ValueError as exc: raise ValueError(f'Row {number}: {exc}') from exc
    return prepared


def export_rows(bot,aid):
    return [{k:r[k] for k in PLAYER_COLUMNS} for r in bot.db.cx.execute(
        'SELECT * FROM players WHERE auction_id=? ORDER BY CAST(set_no AS INTEGER),player_id',(aid,))]


def export_file(rows,fmt):
    if fmt=='json': return json.dumps(rows,ensure_ascii=False,indent=2).encode()
    if fmt=='csv':
        out=io.StringIO();writer=csv.DictWriter(out,fieldnames=PLAYER_COLUMNS)
        writer.writeheader();writer.writerows(rows)
        return out.getvalue().encode('utf-8-sig')
    if fmt=='xlsx':
        import openpyxl
        from openpyxl.cell import WriteOnlyCell
        wb=openpyxl.Workbook(write_only=True); ws=wb.create_sheet('Players')
        def literal(value):
            cell=WriteOnlyCell(ws,value=value)
            if isinstance(value,str): cell.data_type='s'  # Player text stays text, including names starting '='.
            return cell
        ws.append([literal(k) for k in PLAYER_COLUMNS])
        for row in rows: ws.append([literal(row[k]) for k in PLAYER_COLUMNS])
        out=io.BytesIO();wb.save(out);return out.getvalue()
    raise ValueError('Use json, csv or xlsx.')


def sample():
    row={k:'' for k in PLAYER_COLUMNS}
    row.update(set_no=1,name='Example Player',rating='82',category='Bowler',gender='Women',country='India',bat_hand='Right',
               bowl_hand='Right',bowl_style='Fast',bat_rating='70',bowl_rating='87',version='2026',tier='Gold',base_price=20,role='Bowl',nationality='Indian')
    return row


def payload(message):
    raw=message.text or getattr(message,'caption',None) or ''
    return re.split(r'\s+',raw.strip(),maxsplit=1)[1] if len(re.split(r'\s+',raw.strip(),maxsplit=1))>1 else ''


async def input_rows(update):
    message=update.message
    source=message if getattr(message,'document',None) else getattr(message,'reply_to_message',None)
    if source and getattr(source,'document',None):
        doc=source.document
        if doc.file_size and doc.file_size>5_000_000: raise ValueError('Maximum import size is 5 MB.')
        raw=bytes(await (await doc.get_file()).download_as_bytearray())
        return read_rows(raw,doc.file_name)
    text=payload(message)
    if not text and source: text=getattr(source,'text',None) or ''
    if not text: raise ValueError('Use /downloadtemplate, fill its Player Database fields, then reply to the file with /uploaddata or /asetsimport. You can also paste CSV/JSON after the command.')
    return text_rows(text)


async def preview_import(bot,update,context):
    doc=getattr(update.message,'document',None) or getattr(getattr(update.message,'reply_to_message',None),'document',None)
    if doc and payload(update.message).strip() not in ('','replace'):
        raise ValueError('Use /uploaddata [replace] or /asetsimport [replace] when replying to a file.')
    rows=await input_rows(update)
    replace=payload(update.message).strip()=='replace'
    token,data=auction_rules.preview(bot,rows,replace=replace,actor=update.effective_user.id)
    counts={a:sum(p['action']==a for p in data['changes']) for a in ('new','updated','unchanged','protected')}
    await update.message.reply_text(f'Preview: {counts}; withdraw {len(data["withdrawn"])}.\n'+'\n'.join(data['warnings'])+
                                    f'\nReview: /asetsreview {token}\nApply within 30 minutes: /asetsconfirm {token}')


async def import_command(bot,update,context):
    await bot._reg(update.effective_user)
    if not bot.db.is_admin(update.effective_user.id): await update.message.reply_text('Admin only.');return
    if not bot.live.auction_id: await update.message.reply_text('Create or restore an auction first.');return
    try: await preview_import(bot,update,context)
    except (ValueError,TypeError,csv.Error) as exc: await update.message.reply_text(str(exc))


async def template_command(bot,update,context):
    await bot._reg(update.effective_user)
    if not bot.db.is_admin(update.effective_user.id): await update.message.reply_text('Admin only.');return
    fmt=context.args[0].lower() if context.args else 'xlsx'
    try: raw=export_file([sample()],fmt)
    except ValueError as exc: await update.message.reply_text(str(exc));return
    await update.message.reply_document(document=io.BytesIO(raw),filename='PlayerTemplate.'+fmt,
        caption='Player Database template. Replace the example row; keep headers. Leave player_id blank for new players. Reply with /uploaddata to preview, then /asetsconfirm TOKEN. Prices are in lakhs unless suffixed cr/l.')


async def add_command(bot,update,context,mode='single'):
    await bot._reg(update.effective_user)
    if not bot.db.is_admin(update.effective_user.id): await update.message.reply_text('Admin only.');return
    if not bot.live.auction_id: await update.message.reply_text('Create or restore an auction first.');return
    text=payload(update.message)
    if not text:
        await update.message.reply_text('Add a detailed player:\n/addplayer Example Player,82,Bowler,Women,India,Right,Right,Fast,70,87,2026,1\n\nFor spreadsheets: /downloadtemplate → fill fields → reply /uploaddata → /asetsconfirm TOKEN. JSON records and CSV with database headers are also accepted.');return
    try:
        if mode=='single' and ',' not in text and not text.startswith(('{','[')):
            parts=text.split()
            if len(parts) not in (5,6): raise ValueError('Use the detailed CSV/JSON format, or name role former_team nationality price [tier].')
            rows=[{'name':parts[0].replace('_',' '),'category':parts[1],'role':parts[1],'ipl_team':parts[2],
                   'nationality':bot.norm_nat(parts[3]),'base_price':parts[4],'tier':parts[5] if len(parts)>5 else 'C'}]
        elif mode=='quick' and '\n' not in text and not text.startswith(('{','[')) and len(next(csv.reader([text])))<11:
            rows=[{'name':name.strip(),'category':'Batsman','country':'India'} for name in next(csv.reader([text])) if name.strip()]
        elif mode=='list' and not text.startswith(('{','[')):
            parsed=list(csv.reader(io.StringIO(text)))
            if parsed and all(not row or 5<=len(row)<=6 for row in parsed):
                rows=[]
                for row in parsed:
                    if not row: continue
                    name=re.sub(r'^\d+[.)]?\s+','',row[0].strip())
                    rows.append({'name':name,'category':row[1],'role':row[1],'ipl_team':row[2],
                                 'nationality':bot.norm_nat(row[3].strip()),'base_price':row[4],'tier':row[5].strip() if len(row)>5 else 'C'})
            else: rows=text_rows(text)
        else: rows=text_rows(text)
        if mode=='single' and len(rows)!=1: raise ValueError('Use /add_player_list or /bulkplayer for multiple records.')
        prepared=prepare(bot,rows)
        if any(pid and bot.db.get_player(pid) for pid,_ in prepared): raise ValueError('This Player ID exists. Use /uploaddata for a reviewed update.')
        bot.auction_controls.import_rows(rows)
        auction_rules.audit(bot,'players_added',{'players':[v['name'] for _,v in prepared]},update.effective_user.id)
        await update.message.reply_text(f'Added {len(rows)} player(s) with Player Database fields. Use /findplayer NAME to check them, or /asetsexport csv to download.')
    except (ValueError,TypeError,csv.Error) as exc: await update.message.reply_text(str(exc))


def install(bot):
    async def export_command(update,context):
        await bot._reg(update.effective_user)
        if not bot.db.is_admin(update.effective_user.id): await update.message.reply_text('Admin only.');return
        if not bot.live.auction_id: await update.message.reply_text('Create or restore an auction first.');return
        fmt=context.args[0].lower() if context.args else 'xlsx'
        try: raw=export_file(export_rows(bot,bot.live.auction_id),fmt)
        except ValueError as exc: await update.message.reply_text(str(exc));return
        await update.message.reply_document(document=io.BytesIO(raw),filename='Players.'+fmt)

    @bot.flask_app.route('/draft/players/template')
    def web_template():
        from flask import Response
        fmt=request.args.get('format','xlsx')
        try: raw=export_file([sample()],fmt)
        except ValueError as exc: abort(400,str(exc))
        return Response(raw,mimetype={'csv':'text/csv','json':'application/json','xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}[fmt],headers={'Content-Disposition':f'attachment; filename=PlayerTemplate.{fmt}'})

    @bot.flask_app.route('/draft/players/export')
    def web_export():
        from flask import Response
        aid=bot._web_draft_aid();fmt=request.args.get('format','xlsx')
        try: raw=export_file(export_rows(bot,aid),fmt)
        except ValueError as exc: abort(400,str(exc))
        return Response(raw,mimetype={'csv':'text/csv','json':'application/json','xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}[fmt],headers={'Content-Disposition':f'attachment; filename=Players.{fmt}'})
    return {'exportplayers':export_command}
