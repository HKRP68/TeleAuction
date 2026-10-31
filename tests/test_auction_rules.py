import io
import json

import pytest

from test_auction_controls import setup, command


def test_example_rules_use_min_max_and_overlap(tmp_path):
    bot,u,c=setup(tmp_path)
    command(bot,u,c,'asquad','15_17')
    command(bot,u,c,'arules','Batsman 4, Bowler 4, Allrounder 4, Wicketkeeper 2, Women 2, 84_Rating 4')
    rules=bot.auction_rules.current(bot)
    assert rules['Bat']==[0,4] and rules['Women']==[0,2] and rules['Rating<=84']==[0,4]
    assert bot.db.get_auction(bot.live.auction_id)['max_players']==17
    assert bot.auction_rules.needed(0,{},rules,15)==15
    command(bot,u,c,'arule','Women','2_5')
    assert bot.auction_rules.current(bot)['Women']==[5,2]


def test_gender_role_and_rating_predicates_inclusive_and_missing(tmp_path):
    bot,u,c=setup(tmp_path)
    r=bot.auction_rules
    p={'category':'Bowler','role':'Batsman','gender':'Female','rating':'84'}
    assert all(r.matches(p,k) for k in ('Bowl','Women','Rating<=84','Rating<=90.5'))
    assert not r.matches(p,'Bat')
    p['rating']='84.01'
    assert not r.matches(p,'Rating<=84')
    for value in ('','bad','nan',None):
        p['rating']=value
        assert not r.matches(p,'Rating<=84')
    p['gender']=''
    assert not r.matches(p,'Women')
    assert r.key('90.5_Rating')=='Rating<=90.5'


def test_impossible_role_and_nested_rating_rules_are_rejected(tmp_path):
    bot,u,c=setup(tmp_path)
    r=bot.auction_rules
    with pytest.raises(ValueError,match='cannot fit'):
        r.save(bot,r.parse('Batsman 4,Bowler 4'),1,7)
    with pytest.raises(ValueError,match='Nested rating'):
        r.save(bot,r.parse('Rating<=84 4,Rating<=90 0_2'),1,10)
    assert not r.current(bot)
    assert bot.db.get_auction(bot.live.auction_id)['max_players']==3


def test_bid_rejects_gender_and_rating_caps_and_impossible_minimum(tmp_path):
    bot,u,c=setup(tmp_path,min_players=2,max_players=2)
    aid=bot.live.auction_id
    bot.auction_controls.import_rows([{'player_id':70,'name':'Woman','category':'Bowler','gender':'F','rating':'80'},
                                     {'player_id':71,'name':'Another','category':'Batsman','gender':'Women','rating':'84'},
                                     {'player_id':72,'name':'Man','category':'Batsman','gender':'Male','rating':'95'}])
    bot.db.add_to_squad(aid,1,70)
    bot.auction_rules.save(bot,bot.auction_rules.parse('Women 0_1,Rating<=84 0_1'))
    error=bot.validate_bid(bot.db.get_part(aid,1),bot.db.get_player(71),10,bot.db.get_auction(aid))
    assert 'maximum' in error
    bot.auction_rules.save(bot,bot.auction_rules.parse('Women 2'))
    error=bot.validate_bid(bot.db.get_part(aid,1),bot.db.get_player(72),10,bot.db.get_auction(aid))
    assert 'slots' in error


def test_purse_reserve_protects_cheapest_remaining_slots(tmp_path):
    bot,u,c=setup(tmp_path,min_players=3,max_players=3)
    aid=bot.live.auction_id
    offered=bot.db.add_player(aid,'Offered',10,'Bat')
    bot.db.add_player(aid,'Reserve1',20,'Bat');bot.db.add_player(aid,'Reserve2',30,'Bat')
    error=bot.validate_bid(bot.db.get_part(aid,1),bot.db.get_player(offered),60,bot.db.get_auction(aid))
    assert '50 purse units reserved' in error
    assert bot.validate_bid(bot.db.get_part(aid,1),bot.db.get_player(offered),40,bot.db.get_auction(aid)) is None
    command(bot,u,c,'areserve','off')
    assert bot.validate_bid(bot.db.get_part(aid,1),bot.db.get_player(offered),60,bot.db.get_auction(aid)) is None


def test_finish_satisfies_overlapping_gender_and_rating_rules(tmp_path):
    bot,u,c=setup(tmp_path,min_players=3,max_players=3)
    rows=[]
    for team in range(2):
        for category,gender,rating in [('Batsman','Men','95'),('Bowler','Women','82'),('Allrounder','Men','84')]:
            rows.append({'name':f'{category}{team}','category':category,'gender':gender,'rating':rating,'base_price':1})
    bot.auction_controls.import_rows(rows)
    rules=bot.auction_rules.parse('Batsman 1,Bowler 1,Allrounder 1,Women 1,84_Rating 2')
    bot.auction_rules.save(bot,rules)
    plan=bot.auction_controls.finish_plan()
    assert len(plan)==6
    for uid in (1,2):
        players=[bot.db.get_player(p) for p,t,price in plan if t==uid]
        counted=bot.auction_rules.counts(players,rules)
        assert counted['Women']>=1 and counted['Rating<=84']>=2
        assert all(counted[k]>=1 for k in ('Bat','Bowl','AR'))


def test_preview_is_non_mutating_requires_owner_and_fresh_snapshot(tmp_path):
    bot,u,c=setup(tmp_path)
    r=bot.auction_rules
    token,data=r.preview(bot,[{'player_id':70,'name':'Preview','gender':'Female'}],actor=1)
    assert bot.db.get_player(70) is None
    assert data['changes'][0]['action']=='new'
    with pytest.raises(ValueError):r.confirm(bot,token,actor=2)
    bot.db.add_player(bot.live.auction_id,'Changed pool',10)
    with pytest.raises(ValueError,match='changed since'):r.confirm(bot,token,actor=1)
    assert bot.db.get_player(70) is None
    token,data=r.preview(bot,[{'player_id':70,'name':'Preview','gender':'Female'}],actor=1)
    assert r.confirm(bot,token,actor=1)==1
    assert bot.db.get_player(70)['gender']=='Women'
    with pytest.raises(ValueError):r.confirm(bot,token,actor=1)


def test_web_import_preview_confirm_and_duplicate_warnings(tmp_path):
    bot,u,c=setup(tmp_path)
    client=bot.flask_app.test_client()
    data=b'name,rating,category,country,bat_hand,bowl_hand,bowl_style,bat_rating,bowl_rating,version,gender\nDemo,84,Bowler,India,R,R,Fast,75,84,v1,Female\n'
    response=client.post('/draft/players/preview',data={'file':(io.BytesIO(data),'players.csv')})
    assert response.status_code==200
    assert 'Apply reviewed import' in response.get_data(as_text=True)
    assert len(bot.db.get_available(bot.live.auction_id))==0
    token=bot.db.cx.execute('SELECT token FROM import_previews').fetchone()[0]
    assert client.post('/draft/players/confirm',data={'token':token}).status_code==302
    assert bot.db.get_available(bot.live.auction_id)[0]['gender']=='Women'
    token,preview=bot.auction_rules.preview(bot,[{'name':'Demo'}])
    assert preview['warnings']


def test_rules_page_and_read_only_board_show_progress_safely(tmp_path):
    bot,u,c=setup(tmp_path)
    client=bot.flask_app.test_client()
    response=client.post('/draft/rules',data={'minimum':15,'maximum':17,'rules':'Batsman 4\nBowler 4\nAllrounder 4\nWicketkeeper 2\nWomen 2\n84_Rating 4','reserve':'auto'})
    assert response.status_code==302
    assert client.get('/draft/rules').status_code==200
    assert 'Rating&lt;=84' in client.get('/draft/rules').get_data(as_text=True)
    data=client.get('/watch/data').json
    assert data['minimum']==15 and data['maximum']==17
    assert any(rule['category']=='Women' for rule in data['teams'][0]['rules'])
    assert 'user_id' not in data['teams'][0]
    assert client.post('/watch/data').status_code==405
    assert client.get('/watch').status_code==200
    page=client.get('/draft').get_data(as_text=True)
    assert '<th>Gender</th>' in page and '/draft/players/preview' in page


def test_audit_records_mutations_and_bid_reversal(tmp_path):
    bot,u,c=setup(tmp_path)
    aid=bot.live.auction_id
    p=bot.db.add_player(aid,'Audit',10)
    bot.db.record_bid(aid,2,p,'Audit',20)
    bot.live.current_player_id=p;bot.live.highest_bidder_id=2;bot.live.current_bid=20
    command(bot,u,c,'aundobid')
    bot.db.set_player_status(p,'withdrawn')
    events=[r['action'] for r in bot.db.cx.execute('SELECT * FROM auction_audit WHERE auction_id=?',(aid,))]
    assert {'player_added','bid_recorded','command_requested','bid_removed','player_changed'}<=set(events)
    response=bot.flask_app.test_client().get('/draft/audit?format=json')
    assert response.status_code==200 and len(response.json)>=5


def test_rules_are_scoped_to_auction_and_recover_from_storage(tmp_path):
    bot,u,c=setup(tmp_path)
    old=bot.live.auction_id
    bot.auction_rules.save(bot,bot.auction_rules.parse('Women 1'))
    bot.live.auction_id=bot.db.create_auction('Other',2,100,1,3,0)
    assert bot.auction_rules.current(bot)=={}
    bot.live.auction_id=old
    bot.live.squad_limits={}
    assert bot.auction_rules.current(bot)['Women']==[0,1]
