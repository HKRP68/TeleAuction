import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_auction_controls import setup


def test_add_teams_shared_purse_profiles_and_coowner_bidding(tmp_path):
    bot,u,c=setup(tmp_path)
    bot.db.upsert_user(101,'known_owner','Known')
    rows=bot.team_management.add_teams(bot,bot.live.auction_id,'Royal Tigers, 101, 102, 103\nBlue Hawks, 201',actor=1)
    assert len(rows)==2
    team=bot.db.get_part(bot.live.auction_id,101)
    assert team['purse']==100 and team['username']=='known_owner'
    assert bot.db.get_user(101)['first_name']=='Known'
    assert {r['linked_user_id'] for r in bot.db.get_co_owners(bot.live.auction_id,101)}=={102,103}
    assert bot.db.get_part(bot.live.auction_id,102) is None
    assert bot.eff_uid(102)==101 and bot.eff_uid(103)==101
    assert bot.is_draft_picker(bot.live.auction_id,{'team_name':'Royal Tigers','owner_tag_id':101},103)
    player=bot.db.add_player(bot.live.auction_id,'Player',10)
    bot.live.current_player_id=player
    async def bid():
        await bot.process_bid(u,c,103,10)
        assert bot.live.highest_bidder_id==101
        bot.live.timer_task.cancel()
    asyncio.run(bid())
    audit=bot.db.cx.execute("SELECT * FROM auction_audit WHERE action='teams_added'").fetchone()
    assert audit['actor_id']==1 and '103' in audit['detail']


@pytest.mark.parametrize('text',[
    'Good, 101\nBroken', 'Good, 101\nBad, 0','Good, 101\nBad, -2',
    'Good, 101\nBad, @someone', 'Good, 101\nBad, 201,',
    'Good, 101\nBad, 201,201','Same,101\nsame,201',
    'One,101,102\nTwo,102,201','Bad,9223372036854775808',
    'Good,101\nExistingOwner,1',
])
def test_invalid_batch_never_adds_partial_teams(tmp_path,text):
    bot,u,c=setup(tmp_path)
    with pytest.raises(ValueError): bot.team_management.add_teams(bot,bot.live.auction_id,text)
    assert bot.db.count_participants(bot.live.auction_id)==2
    assert bot.db.get_part(bot.live.auction_id,101) is None
    assert bot.db.get_user(101) is None


def test_owner_conflicts_existing_names_capacity_and_auction_scope(tmp_path):
    bot,u,c=setup(tmp_path)
    aid=bot.live.auction_id
    bot.team_management.add_teams(bot,aid,'Three,101,102')
    for text in ('Other,102','Other,101','Three,201','one,201','Four,201\nFive,301'):
        with pytest.raises(ValueError): bot.team_management.add_teams(bot,aid,text)
    assert bot.db.count_participants(aid)==3
    other=bot.db.create_auction('Other',2,200,1,3,0)
    bot.team_management.add_teams(bot,other,'Three,101,102')
    assert bot.db.get_part(other,101)['purse']==200
    assert bot.db.get_part(aid,101)['purse']==100


def test_multiline_command_reply_and_permissions(tmp_path):
    bot,u,c=setup(tmp_path)
    u.message.text='/addteams@AuctionBot\nTeam One,101,102\nTeam Two,201,202,203'
    c.args=['Team','One,101,102','Team','Two,201,202,203']
    asyncio.run(bot.AUCTION_COMMANDS['addteams'](u,c))
    assert bot.db.get_part(bot.live.auction_id,101)['team_name']=='Team One'
    assert bot.db.get_primary(bot.live.auction_id,203)==201
    u.effective_user.id=2
    u.message.text='/addusers\nDisallowed,301'
    asyncio.run(bot.AUCTION_COMMANDS['addusers'](u,c))
    assert bot.db.get_part(bot.live.auction_id,301) is None
    assert u.message.reply_text.call_args.args[0]=='Admin only.'
    u.effective_user.id=1
    new=bot.db.create_auction('Reply',4,100,1,3,0);bot.live.auction_id=new
    u.message.text='/addusers'
    u.message.reply_to_message=SimpleNamespace(text='Reply Team,301,302')
    asyncio.run(bot.AUCTION_COMMANDS['addusers'](u,c))
    assert bot.db.get_primary(new,302)==301


def test_web_bulk_and_edit_preserves_all_coowners(tmp_path):
    bot,u,c=setup(tmp_path)
    client=bot.flask_app.test_client()
    assert client.post('/draft/teams/bulk',data={'teams':'Web Team,101,102,103'}).status_code==302
    edit=client.get('/draft/teams/101/edit').get_data(as_text=True)
    assert '102, 103' in edit
    response=client.post('/draft/teams/101/edit',data={'team_name':'Web Team','owner_tag_id':101,'co_owner_tag_id':'102, 103'})
    assert response.status_code==302
    assert bot.db.get_primary(bot.live.auction_id,102)==101
    assert bot.db.get_primary(bot.live.auction_id,103)==101
    assert client.post('/draft/teams/bulk',data={'teams':'Good,201\nInvalid,nope'}).status_code==400
    assert bot.db.get_part(bot.live.auction_id,201) is None
    assert 'Add teams in bulk' in client.get('/draft').get_data(as_text=True)


def test_coowner_cannot_register_a_second_team(tmp_path):
    bot,u,c=setup(tmp_path)
    bot.team_management.add_teams(bot,bot.live.auction_id,'Team,101,102')
    query=SimpleNamespace(answer=AsyncMock())
    asyncio.run(bot._handle_callback_inner(u,c,query,f'join_{bot.live.auction_id}',102))
    assert bot.db.get_part(bot.live.auction_id,102) is None
    assert 'Already registered' in query.answer.call_args.args[0]


def test_quoted_team_name_and_blank_lines(tmp_path):
    bot,u,c=setup(tmp_path)
    assert bot.team_management.parse_teams('\n"Team, North",101,102\n\nSolo,201\n')==[('Team, North',[101,102]),('Solo',[201])]
