import asyncio
import csv
import io
import json
from types import SimpleNamespace

import pytest
from test_auction_controls import setup,command

EXPECTED = ('Set No','Set Name','Player Id','Player Name','Category','country','Overall','bat_hand','bowl_hand','bowl_style','bat_rating','bowl_rating','version','gender')


def test_home_sets_classification_without_enabling_a_quota(tmp_path):
    bot,u,c=setup(tmp_path,max_players=17)
    assert bot.auction_rules.current(bot)=={}
    command(bot,u,c,'home','India')
    assert bot.auction_rules.home_country(bot)=='India'
    assert bot.auction_rules.current(bot)=={}
    assert bot.auction_rules.origin({'country':'IND'},'India')=='Home'
    assert bot.auction_rules.origin({'country':'AUS'},'India')=='Overseas'
    assert bot.auction_rules.origin({'country':'','nationality':'Overseas'},'India')=='Unclassified'
    command(bot,u,c,'overseas','7_8')
    assert bot.auction_rules.current(bot)['OS']==[8,7]
    assert 'Home' not in bot.auction_rules.current(bot)
    command(bot,u,c,'overseas','off')
    assert 'OS' not in bot.auction_rules.current(bot)


def test_home_and_overseas_settings_are_persistent_and_auction_scoped(tmp_path):
    bot,u,c=setup(tmp_path,max_players=17)
    aid=bot.live.auction_id
    command(bot,u,c,'home','Australia')
    command(bot,u,c,'overseas','7_8')
    bot.db._init()
    assert bot.auction_rules.home_country(bot)=='Australia'
    assert bot.auction_rules.current(bot)['OS']==[8,7]
    bot.live.auction_id=bot.db.create_auction('Fresh',2,100,15,17,0)
    assert bot.auction_rules.home_country(bot)=='India'
    assert bot.auction_rules.current(bot)=={}
    bot.live.auction_id=aid
    assert bot.auction_rules.home_country(bot)=='Australia'


def test_overseas_cap_uses_country_and_leaves_home_players_uncapped(tmp_path):
    bot,u,c=setup(tmp_path,min_players=15,max_players=17)
    aid=bot.live.auction_id
    rows=[{'name':f'Away{i}','country':'England'} for i in range(9)] + [{'name':f'Home{i}','country':'India'} for i in range(10)]
    bot.auction_controls.import_rows(rows)
    command(bot,u,c,'home','India');command(bot,u,c,'overseas','7_8')
    for i in range(8):
        p=bot.db.get_player_by_name(aid,f'Away{i}')
        bot.db.set_player_status(p['player_id'],'sold',1,1);bot.db.add_to_squad(aid,1,p['player_id'])
    bot.db.set_setting(f'auction:{aid}:reserve','off')
    away=bot.db.get_player_by_name(aid,'Away8');home=bot.db.get_player_by_name(aid,'Home0')
    # A historical nationality flag must not override the explicit country.
    bot.db.cx.execute("UPDATE players SET nationality='Indian' WHERE player_id=?",(away['player_id'],));bot.db.cx.commit()
    error=bot.validate_bid(bot.db.get_part(aid,1),bot.db.get_player(away['player_id']),10,bot.db.get_auction(aid))
    assert 'maximum' in error
    assert bot.validate_bid(bot.db.get_part(aid,1),home,10,bot.db.get_auction(aid)) is None
    data=bot.flask_app.test_client().get('/watch/data').json
    assert data['teams'][0]['origin']['Overseas']==8
    assert data['home_country']=='India'


def test_unknown_country_does_not_satisfy_minimum_and_warns_in_preview(tmp_path):
    bot,u,c=setup(tmp_path,min_players=1,max_players=2)
    bot.auction_controls.import_rows([{'name':'Unknown','country':''},{'name':'Home','country':'India'}])
    command(bot,u,c,'overseas','1_2')
    token,data=bot.auction_rules.preview(bot,[{'name':'No country'}])
    assert any('country is blank' in warning for warning in data['warnings'])
    with pytest.raises(ValueError,match='Cannot complete'):bot.auction_controls.finish_plan()


def test_home_change_rejects_conflicting_squads_and_standing_bids_atomically(tmp_path):
    bot,u,c=setup(tmp_path,min_players=1,max_players=3)
    aid=bot.live.auction_id
    bot.auction_controls.import_rows([{'name':'Away','country':'England'},{'name':'Pending','country':'India'}])
    away=bot.db.get_player_by_name(aid,'Away');pending=bot.db.get_player_by_name(aid,'Pending')
    bot.db.set_player_status(away['player_id'],'sold',1,1);bot.db.add_to_squad(aid,1,away['player_id'])
    command(bot,u,c,'overseas','0_1')
    bot.live.current_player_id=pending['player_id'];bot.live.highest_bidder_id=1;bot.live.current_bid=10
    command(bot,u,c,'home','Australia')
    assert bot.auction_rules.home_country(bot)=='India'
    assert 'conflict' in u.message.reply_text.call_args.args[0]
    assert bot.auction_rules.current(bot)['OS']==[1,0]


def test_finisher_meets_configured_overseas_range(tmp_path):
    bot,u,c=setup(tmp_path,min_players=3,max_players=3)
    rows=[]
    for i in range(2):
        rows += [{'name':f'Home{i}','country':'India'}, {'name':f'Away A{i}','country':'Australia'},{'name':f'Away B{i}','country':'England'}]
    bot.auction_controls.import_rows(rows)
    command(bot,u,c,'home','India');command(bot,u,c,'overseas','2_2')
    plan=bot.auction_controls.finish_plan()
    for uid in (1,2):
        players=[bot.db.get_player(pid) for pid,owner,price in plan if owner==uid]
        assert len(players)==3
        assert bot.auction_rules.origin_counts(players,'India')=={'Home':1,'Overseas':2,'Unclassified':0}


def test_public_templates_and_exports_have_only_the_requested_14_fields(tmp_path):
    bot,u,c=setup(tmp_path)
    sample=bot.player_io.sample()
    for fmt in ('json','csv','xlsx'):
        raw=bot.player_io.export_file([sample],fmt)
        parsed=bot.player_io.read_rows(raw,'players.'+fmt)[0]
        assert set(parsed)=={'set_no','set_name','player_id','name','category','country','rating','bat_hand','bowl_hand','bowl_style','bat_rating','bowl_rating','version','gender'}
        if fmt=='json': headers=tuple(json.loads(raw)[0])
        elif fmt=='csv': headers=tuple(next(csv.reader(io.StringIO(raw.decode('utf-8-sig')))))
        else:
            import openpyxl
            wb=openpyxl.load_workbook(io.BytesIO(raw),data_only=True)
            headers=tuple(c.value for c in wb['Players'][1]);wb.close()
        assert headers==EXPECTED
        assert 'tier' not in parsed and 'base_price' not in parsed and 'nationality' not in parsed


def test_exact_user_headers_and_raw_addplayer_fields_preserve_set_name(tmp_path):
    bot,u,c=setup(tmp_path)
    u.message.text='/addplayer 2,Spin Bowlers,70,Example Player,Bowler,IND,84,Left,Right,Spin,65,89,v2,Female'
    asyncio.run(bot.cmd_add_player(u,c))
    p=bot.db.get_player(70)
    assert p['set_no']=='2' and p['set_name']=='Spin Bowlers' and p['rating']=='84' and p['country']=='India' and p['gender']=='Women'
    raw='Set No,Set Name,Player Id,Player Name,Catagory,country,Overall,bat_hand,bowl_hand,bowl_style,bat_rating,bowl_rating,version,gender\n3,Batters,71,Example Batter,Batsman,Australia,91,Right,Right,None,93,0,v2,Male'
    bot.auction_controls.import_rows(bot.player_io.read_rows(raw.encode(),'players.csv'))
    assert bot.db.get_player(71)['rating']=='91'
    page=bot.flask_app.test_client().get('/draft').get_data(as_text=True)
    assert 'Set Name' in page and 'Spin Bowlers' in page and 'Overall' in page


def test_new_public_export_import_preserves_hidden_metadata_for_existing_players(tmp_path):
    bot,u,c=setup(tmp_path)
    bot.auction_controls.import_rows([{'player_id':70,'name':'Existing','base_price':'2cr','tier':'Platinum','ipl_team':'MI','country':'India','set_name':'Legends'}])
    for fmt in ('json','csv','xlsx'):
        raw=bot.player_io.export_file(bot.player_io.export_rows(bot,bot.live.auction_id),fmt)
        bot.auction_controls.import_rows(bot.player_io.read_rows(raw,'players.'+fmt))
    p=bot.db.get_player(70)
    assert p['base_price']==200 and p['tier']=='Platinum' and p['ipl_team']=='MI' and p['set_name']=='Legends'


def test_conflicting_set_names_and_invalid_overseas_ranges_leave_state_unchanged(tmp_path):
    bot,u,c=setup(tmp_path,max_players=17)
    with pytest.raises(ValueError,match='conflicting Set Names'):
        bot.auction_controls.import_rows([{'name':'One','set_no':1,'set_name':'Pace'},{'name':'Two','set_no':1,'set_name':'Spin'}])
    assert not bot.db.get_available(bot.live.auction_id)
    for value in ('8_7','7','-1_8','7_18'):
        command(bot,u,c,'overseas',value)
        assert 'OS' not in bot.auction_rules.current(bot)


def test_web_home_and_overseas_controls_save_together(tmp_path):
    bot,u,c=setup(tmp_path,max_players=17)
    client=bot.flask_app.test_client()
    response=client.post('/draft/rules',data={'minimum':15,'maximum':17,'rules':'Women 2','reserve':'auto','home_country':'India','overseas':'7_8'})
    assert response.status_code==302
    assert bot.auction_rules.current(bot)['OS']==[8,7]
    response=client.post('/draft/rules',data={'minimum':15,'maximum':17,'rules':'Women 2','reserve':'auto','home_country':'Australia','overseas':'8_7'})
    assert response.status_code==400 and bot.auction_rules.home_country(bot)=='India'
    assert bot.auction_rules.current(bot)['OS']==[8,7]
    assert 'Home country' in client.get('/draft/rules').get_data(as_text=True)


def test_ordinary_users_cannot_change_home_or_quotas(tmp_path):
    bot,u,c=setup(tmp_path,max_players=17)
    u.effective_user.id=2
    command(bot,u,c,'home','Australia');command(bot,u,c,'overseas','7_8')
    assert bot.auction_rules.home_country(bot)=='India' and bot.auction_rules.current(bot)=={}


def test_set_name_is_inherited_within_batch_and_persisted_by_migration(tmp_path):
    bot,u,c=setup(tmp_path)
    bot.auction_controls.import_rows([{'name':'First','set_no':2},{'name':'Second','set_no':2,'set_name':'Spinners'}])
    for name in ('First','Second'):
        assert bot.db.get_player_by_name(bot.live.auction_id,name)['set_name']=='Spinners'
    bot.db._init()
    assert bot.db.get_player_by_name(bot.live.auction_id,'Second')['set_name']=='Spinners'


def test_no_nationality_cap_is_imposed_until_opted_in(tmp_path):
    bot,u,c=setup(tmp_path,min_players=1,max_players=17)
    bot.auction_controls.import_rows([{'name':f'Foreign{i}','country':'England'} for i in range(10)])
    aid=bot.live.auction_id
    for i in range(9):
        p=bot.db.get_player_by_name(aid,f'Foreign{i}')
        bot.db.set_player_status(p['player_id'],'sold',1,1);bot.db.add_to_squad(aid,1,p['player_id'])
    offered=bot.db.get_player_by_name(aid,'Foreign9')
    assert bot.validate_bid(bot.db.get_part(aid,1),offered,10,bot.db.get_auction(aid)) is None
    assert bot.auction_rules.current(bot)=={}


def test_explicit_blank_country_is_unclassified_even_with_legacy_indian_flag(tmp_path):
    bot,u,c=setup(tmp_path)
    bot.auction_controls.import_rows([{'player_id':70,'name':'Player','country':'India'}])
    bot.auction_controls.import_rows([{'player_id':70,'name':'Player','country':''}])
    p=bot.db.get_player(70)
    assert p['country']=='' and bot.auction_rules.origin(p,'India')=='Unclassified'


def test_pasted_spreadsheet_row_uses_exact_fields_including_blank_gender(tmp_path):
    bot,u,c=setup(tmp_path)
    row=['1','Spinners','','Spreadsheet Player','Bowler','India','84','Left','Right','Spin','70','85','2026','']
    u.message.text='/addplayer '+ '\t'.join(row)
    asyncio.run(bot.cmd_add_player(u,c))
    p=bot.db.get_player_by_name(bot.live.auction_id,'Spreadsheet Player')
    assert p['set_name']=='Spinners' and p['rating']=='84' and p['gender']==''
