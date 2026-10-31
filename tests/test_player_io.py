import asyncio
import csv
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import openpyxl
from test_auction_controls import setup


def record():
    return {'player_id':70,'set_no':2,'name':'Full Player','rating':'84','category':'Bowler','gender':'Female','country':'India',
            'bat_hand':'Left','bowl_hand':'Right','bowl_style':'Fast','bat_rating':'60','bowl_rating':'91','version':'2026',
            'tier':'A','icon_eligible':'yes','indian_status':'Indian','base_price':'2cr','role':'Bowl','nationality':'Indian','ipl_team':'MI'}


def upload_doc(raw,name):
    file=SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(raw)))
    return SimpleNamespace(file_name=name,file_size=len(raw),get_file=AsyncMock(return_value=file))


@pytest.mark.parametrize('fmt',['csv','json','xlsx'])
def test_all_fields_roundtrip_in_every_format(tmp_path,fmt):
    bot,u,c=setup(tmp_path)
    bot.auction_controls.import_rows([record()])
    exported=bot.player_io.export_rows(bot,bot.live.auction_id)
    raw=bot.player_io.export_file(exported,fmt)
    rows=bot.player_io.read_rows(raw,'players.'+fmt)
    assert set(rows[0])==set(bot.player_io.PLAYER_COLUMNS)
    token,data=bot.auction_rules.preview(bot,rows)
    assert data['changes'][0]['action']=='unchanged'
    bot.auction_rules.confirm(bot,token)
    p=bot.db.get_player(70)
    assert p['gender']=='Women' and p['base_price']==200 and p['tier']=='A'
    assert p['bat_hand']=='Left' and p['set_no']=='2' and p['version']=='2026'


@pytest.mark.parametrize('fmt',['csv','json','xlsx'])
def test_templates_import_without_notes_or_field_loss(tmp_path,fmt):
    bot,u,c=setup(tmp_path)
    c.args=[fmt]
    asyncio.run(bot.cmd_download_template(u,c))
    document=u.message.reply_document.call_args.kwargs
    rows=bot.player_io.read_rows(document['document'].getvalue(),document['filename'])
    assert list(rows[0])==list(bot.player_io.PLAYER_COLUMNS)
    assert rows[0]['name']=='Example Player'
    bot.auction_controls.import_rows(rows)
    p=bot.db.get_available(bot.live.auction_id)[0]
    assert p['rating']=='82' and p['gender']=='Women' and p['version']=='2026'


def test_report_players_sheet_and_squad_details_use_full_schema(tmp_path):
    bot,u,c=setup(tmp_path)
    aid=bot.live.auction_id
    bot.auction_controls.import_rows([record()])
    bot.db.set_player_status(70,'sold',1,200);bot.db.add_to_squad(aid,1,70)
    raw=bot._build_auction_excel(aid)
    wb=openpyxl.load_workbook(io.BytesIO(raw),data_only=True)
    assert tuple(cell.value for cell in wb['Players'][1])[:len(bot.player_io.PLAYER_COLUMNS)]==bot.player_io.FIELD_LABELS
    assert 'gender' in [cell.value for cell in wb['Squad Details'][1]]
    rows=bot.player_io.read_rows(raw,'report.xlsx')
    assert 'status' not in rows[0] and rows[0]['gender']=='Women'
    token,data=bot.auction_rules.preview(bot,rows)
    assert data['changes'][0]['action']=='protected'
    bot.auction_rules.confirm(bot,token)
    assert bot.db.get_player(70)['sold_to']==1


def test_legacy_upload_is_now_a_reviewed_full_metadata_import(tmp_path):
    bot,u,c=setup(tmp_path)
    u.message.text='/importdata'
    u.message.reply_to_message=SimpleNamespace(document=upload_doc(json.dumps([record()]).encode(),'players.json'))
    asyncio.run(bot.cmd_upload_data(u,c))
    assert bot.db.get_player(70) is None
    preview=bot.db.cx.execute('SELECT token FROM import_previews').fetchone()
    assert preview and '/asetsconfirm' in u.message.reply_text.call_args.args[0]
    bot.auction_rules.confirm(bot,preview['token'],actor=1)
    assert bot.db.get_player(70)['bowl_rating']=='91'


def test_caption_document_and_inline_json_use_same_schema(tmp_path):
    bot,u,c=setup(tmp_path)
    u.message.text=None;u.message.caption='/uploaddata';u.message.document=upload_doc(json.dumps([record()]).encode(),'players.json')
    asyncio.run(bot.cmd_player_document(u,c))
    token=bot.db.cx.execute('SELECT token FROM import_previews').fetchone()[0]
    bot.auction_rules.confirm(bot,token,actor=1)
    assert bot.db.get_player(70)['gender']=='Women'
    u.message.document=None;u.message.caption=None;u.message.text='/uploaddata '+json.dumps({'name':'Inline','rating':90,'gender':'Men','category':'Batsman'})
    asyncio.run(bot.cmd_upload_data(u,c))
    token=bot.db.cx.execute('SELECT token FROM import_previews').fetchone()[0]
    bot.auction_rules.confirm(bot,token,actor=1)
    assert bot.db.get_player_by_name(bot.live.auction_id,'Inline')['rating']=='90'


def test_detailed_chat_entry_preserves_gender_ratings_version_set(tmp_path):
    bot,u,c=setup(tmp_path)
    u.message.text='/addplayer Example Player,82,Bowler,Women,India,Right,Right,Fast,70,87,2026,3'
    asyncio.run(bot.cmd_add_player(u,c))
    p=bot.db.get_player_by_name(bot.live.auction_id,'Example Player')
    assert p['gender']=='Women' and p['set_no']=='3' and p['version']=='2026' and p['bowl_rating']=='87'
    u.message.text='/add_player_list\nSecond,90,Batsman,Men,India,Right,Right,None,94,10,2026,4\nThird,80,Bowler,Women,England,Left,Left,Spin,65,86,2026,5'
    asyncio.run(bot.cmd_add_player_list(u,c))
    assert bot.db.get_player_by_name(bot.live.auction_id,'Third')['country']=='England'
    assert bot.db.get_player_by_name(bot.live.auction_id,'Second')['bat_rating']=='94'


def test_legacy_entry_remains_supported_through_shared_normalizer(tmp_path):
    bot,u,c=setup(tmp_path)
    u.message.text='/addplayer Virat_Kohli Bat RCB Indian 2cr Marquee'
    asyncio.run(bot.cmd_add_player(u,c))
    p=bot.db.get_player_by_name(bot.live.auction_id,'Virat Kohli')
    assert p['category']=='Bat' and p['country']=='India' and p['base_price']==200 and p['tier']=='Marquee'
    u.message.text='/add_player_list\n1 Bowler One, Bowl, MI, Indian, 50l, A\n2 Bowler Two, Bowl, MI, Overseas, 75, B'
    asyncio.run(bot.cmd_add_player_list(u,c))
    assert bot.db.get_player_by_name(bot.live.auction_id,'Bowler One')['base_price']==50


def test_sparse_updates_preserve_omitted_fields_and_zero_ratings(tmp_path):
    bot,u,c=setup(tmp_path)
    bot.auction_controls.import_rows([record()])
    bot.auction_controls.import_rows([{'player_id':70,'name':'Renamed','rating':0,'bat_rating':0}])
    p=bot.db.get_player(70)
    assert p['rating']=='0' and p['bat_rating']=='0'
    assert p['gender']=='Women' and p['country']=='India' and p['base_price']==200 and p['tier']=='A' and p['version']=='2026'


@pytest.mark.parametrize('row',[{'name':'Bad','rating':'nan'},{'name':'Bad','base_price':'nonsense'},
                                {'name':'Bad','tier':'bogus'},{'name':'Bad','set_no':1.5},{'name':'Bad','gendre':'Female'}])
def test_invalid_data_rejects_entire_import(tmp_path,row):
    bot,u,c=setup(tmp_path)
    with pytest.raises(ValueError):bot.auction_controls.import_rows([{'name':'Good'},row])
    assert not bot.db.get_available(bot.live.auction_id)


def test_header_aliases_bom_and_formula_like_names_are_preserved(tmp_path):
    bot,u,c=setup(tmp_path)
    rows=bot.player_io.read_rows(b'\xef\xbb\xbfPlayer Id,Set No,Player Name,Gender,Batting Rating,Rating\n70,1,=literal,Female,0,84\n','players.csv')
    bot.auction_controls.import_rows(rows)
    raw=bot.player_io.export_file(bot.player_io.export_rows(bot,bot.live.auction_id),'xlsx')
    p=bot.player_io.read_rows(raw,'players.xlsx')[0]
    assert p['name']=='=literal' and p['bat_rating']=='0'
    with pytest.raises(ValueError):bot.player_io.read_rows(b'name,Name\nA,B','players.csv')


def test_web_templates_exports_and_json_preview(tmp_path):
    bot,u,c=setup(tmp_path);client=bot.flask_app.test_client()
    for fmt in ('json','csv','xlsx'):
        response=client.get('/draft/players/template?format='+fmt)
        assert response.status_code==200
        assert bot.player_io.read_rows(response.data,'template.'+fmt)[0]['gender']=='Women'
    response=client.post('/draft/players/preview',data={'file':(io.BytesIO(json.dumps([record()]).encode()),'players.json')})
    assert response.status_code==200
    token=bot.db.cx.execute('SELECT token FROM import_previews').fetchone()[0]
    assert client.post('/draft/players/confirm',data={'token':token}).status_code==302
    for fmt in ('json','csv','xlsx'):
        response=client.get('/draft/players/export?format='+fmt)
        assert response.status_code==200
        assert bot.player_io.read_rows(response.data,'players.'+fmt)[0]['version']=='2026'


def test_all_set_exports_and_explicit_report_formats_match_database(tmp_path):
    bot,u,c=setup(tmp_path)
    bot.auction_controls.import_rows([record()])
    for fmt in ('json','csv','xlsx'):
        c.args=[fmt]
        asyncio.run(bot.AUCTION_COMMANDS['asetsexport'](u,c))
        document=u.message.reply_document.call_args.kwargs
        row=bot.player_io.read_rows(document['document'].getvalue(),document['filename'])[0]
        assert row['gender']=='Women' and row['version']=='2026'
        asyncio.run(bot.cmd_download_data(u,c))
        document=u.message.reply_document.call_args.kwargs
        assert bot.player_io.read_rows(document['document'].getvalue(),document['filename'])[0]['bowl_style']=='Fast'


def test_bundled_examples_are_accepted_by_the_shared_schema(tmp_path):
    from pathlib import Path
    bot,u,c=setup(tmp_path)
    root=Path(bot.__file__).parent
    for fmt in ('csv','json'):
        rows=bot.player_io.read_rows((root/f'sample_players.{fmt}').read_bytes(),f'players.{fmt}')
        assert bot.player_io.prepare(bot,rows)[0][1]['gender']=='Women'
    u.message.text=(root/'sample_players.txt').read_text()
    asyncio.run(bot.cmd_add_player_list(u,c))
    assert bot.db.get_player_by_name(bot.live.auction_id,'Example Player')['set_name']=='Pace Bowlers'


def test_malformed_uploads_return_clear_errors_without_mutation(tmp_path):
    bot,u,c=setup(tmp_path)
    for raw,name in ((b'name,rating\nA,84,EXTRA','bad.csv'),(b'not-json','bad.json'),(b'name,rating\nA,84','old.xls')):
        u.message.text='/uploaddata';u.message.reply_to_message=SimpleNamespace(document=upload_doc(raw,name))
        asyncio.run(bot.cmd_upload_data(u,c))
        assert u.message.reply_text.call_args.args[0]
    assert not bot.db.get_available(bot.live.auction_id)


def test_paste_canonical_database_row_without_headers(tmp_path):
    bot,u,c=setup(tmp_path)
    out=io.StringIO();writer=csv.writer(out)
    sample=bot.player_io.sample();writer.writerow([sample[k] for k in bot.player_io.PLAYER_COLUMNS])
    u.message.text='/addplayer '+out.getvalue()
    asyncio.run(bot.cmd_add_player(u,c))
    p=bot.db.get_player_by_name(bot.live.auction_id,'Example Player')
    assert p['gender']=='Women' and p['bowl_rating']=='87' and p['base_price']==0
