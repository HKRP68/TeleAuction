import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_auction_controls import setup, command


def test_cancellation_voids_pending_bid_preserves_sales_purses_and_audit(tmp_path):
    bot,u,c=setup(tmp_path);aid=bot.live.auction_id
    sold=bot.db.add_player(aid,'Sold',10)
    current=bot.db.add_player(aid,'Pending',10)
    bot.db.set_player_status(sold,'sold',1,30);bot.db.deduct_purse(aid,1,30);bot.db.add_to_squad(aid,1,sold)
    bot.live.current_player_id=current;bot.live.current_bid=20;bot.live.highest_bidder_id=2
    bot.live.auto_next_on=True;bot.live.auto_next_secs=1
    bot.auction_rules.preview(bot,[{'name':'Preview'}],actor=1)
    async def run():
        bot.live.timer_task=asyncio.create_task(asyncio.sleep(3600))
        c.args=['confirm'];await bot.AUCTION_COMMANDS['acancel'](u,c)
        await asyncio.sleep(0)
        assert bot.live.timer_task.cancelled()
    asyncio.run(run())
    assert bot.db.get_auction(aid)['status']=='cancelled'
    assert not bot.live.active and not bot.live.auto_next_on and not bot.live.player_queue
    assert bot.live.current_player_id is None and bot.live.current_bid==0
    assert bot.db.get_player(sold)['sold_to']==1
    assert bot.db.get_part(aid,1)['purse']==70
    assert json.loads(bot.db.get_part(aid,1)['squad'])==[sold]
    assert bot.db.get_part(aid,2)['purse']==100
    assert bot.db.get_player(current)['status']=='available'
    assert not bot.db.cx.execute('SELECT * FROM import_previews').fetchall()
    event=bot.db.cx.execute("SELECT * FROM auction_audit WHERE action='auction_cancelled'").fetchone()
    assert event['actor_id']==1 and json.loads(event['detail'])['voided_bid']==20


def test_cancel_requires_admin_and_explicit_confirmation(tmp_path):
    bot,u,c=setup(tmp_path)
    command(bot,u,c,'acancel')
    assert bot.db.get_auction(bot.live.auction_id)['status']!='cancelled'
    assert '/acancel confirm' in u.message.reply_text.call_args.args[0]
    u.effective_user.id=2
    command(bot,u,c,'acancel','confirm')
    assert u.message.reply_text.call_args.args[0]=='Admin only.'
    u.effective_user.id=1
    command(bot,u,c,'cancelauction','confirm')
    assert bot.db.get_auction(bot.live.auction_id)['status']=='cancelled'


def test_cancelled_auction_cannot_resume_start_bid_or_import_but_exports_work(tmp_path):
    bot,u,c=setup(tmp_path);aid=bot.live.auction_id
    p=bot.db.add_player(aid,'Saved',10)
    bot.auction_lifecycle.cancel(bot,1)
    for name in ('aresume','astart','anext','afinish'):
        command(bot,u,c,name)
        assert not bot.live.active and not bot.live.current_player_id
    asyncio.run(bot.cmd_resume(u,c));asyncio.run(bot.cmd_start_auction(u,c))
    assert not bot.live.active
    with pytest.raises(ValueError,match='cancelled'):bot.auction_controls.import_rows([{'name':'New'}])
    with pytest.raises(ValueError,match='cancelled'):bot.auction_rules.preview(bot,[{'name':'New'}])
    command(bot,u,c,'asetsexport','json')
    assert bot.player_io.read_rows(u.message.reply_document.call_args.kwargs['document'].getvalue(),'players.json')[0]['name']=='Saved'
    bot.live.active=True;bot.live.current_player_id=p
    asyncio.run(bot.process_bid(u,c,2,10))
    assert bot.live.current_bid==0


def test_cancel_refunds_unresolved_rtm_card_once(tmp_path):
    bot,u,c=setup(tmp_path);aid=bot.live.auction_id
    p=bot.db.add_player(aid,'Pending RTM',10)
    bot.live.current_player_id=p;bot.live.rtm_state=bot.RTM_ACTIVE;bot.live.rtm_team_id=2
    bot.db.set_rtm(aid,2,0,'MI')
    bot.auction_lifecycle.cancel(bot,1)
    assert bot.db.get_part(aid,2)['rtm_cards']==1
    with pytest.raises(ValueError,match='already cancelled'):bot.auction_lifecycle.cancel(bot,1)
    assert bot.db.get_part(aid,2)['rtm_cards']==1


def test_restart_is_explicit_and_resets_cancelled_auction(tmp_path):
    bot,u,c=setup(tmp_path);aid=bot.live.auction_id
    p=bot.db.add_player(aid,'Saved',10)
    bot.auction_lifecycle.cancel(bot,1)
    command(bot,u,c,'arestart','confirm')
    assert bot.db.get_auction(aid)['status']=='active' and bot.live.active
    assert [bot.auction_controls.pid(item) for item in bot.live.player_queue]==[p]


def test_stale_snapshot_cannot_reactivate_cancelled_auction(tmp_path):
    bot,u,c=setup(tmp_path);aid=bot.live.auction_id
    p=bot.db.add_player(aid,'Player',10)
    bot.auction_lifecycle.cancel(bot,1)
    state=json.loads(bot.db.get_setting('live_state'))
    state.update(active=True,paused=True,current_player_id=p,current_bid=50,highest_bidder_id=2,player_queue=[p])
    assert bot._apply_state_dict(state)
    assert not bot.live.active and not bot.live.paused and not bot.live.player_queue
    assert bot.live.current_player_id is None and bot.live.highest_bidder_id is None


def test_web_cancellation_requires_review_and_keeps_exportable_data(tmp_path):
    bot,u,c=setup(tmp_path);client=bot.flask_app.test_client()
    bot.db.add_player(bot.live.auction_id,'Player',10)
    assert client.get('/draft/cancel').status_code==405
    response=client.post('/draft/cancel')
    assert response.status_code==200 and 'Confirm cancellation' in response.get_data(as_text=True)
    assert bot.db.get_auction(bot.live.auction_id)['status']!='cancelled'
    assert client.post('/draft/cancel',data={'confirm':'confirm'}).status_code==302
    assert client.get('/watch/data').json['status']=='cancelled'
    assert client.get('/draft/players/export?format=json').status_code==200
    query=SimpleNamespace(answer=AsyncMock())
    asyncio.run(bot._handle_callback_inner(u,c,query,'ry|1|2|20|1|10',1))
    assert query.answer.call_args.args[0]=='This auction is cancelled.'


def test_delayed_auto_next_cannot_reopen_after_cancel_and_restart(tmp_path,monkeypatch):
    bot,u,c=setup(tmp_path)
    bot.db.add_player(bot.live.auction_id,'Player',10)
    bot.live.auto_next_on=True;bot.live.auto_next_secs=1
    async def interrupt(seconds):
        bot.auction_lifecycle.cancel(bot,1)
        c.args=['confirm'];await bot.AUCTION_COMMANDS['arestart'](u,c)
        bot.live.auto_next_on=True
    monkeypatch.setattr(bot.asyncio,'sleep',interrupt)
    monkeypatch.setattr(bot,'_do_next',AsyncMock())
    asyncio.run(bot._try_auto_next(c))
    bot._do_next.assert_not_awaited()
