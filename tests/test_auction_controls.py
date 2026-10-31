import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_price_utils import load_bot


def setup(tmp_path, min_players=1, max_players=3):
    bot = load_bot(tmp_path)
    aid = bot.db.create_auction('Auction', 4, 100, min_players, max_players, -10)
    bot.live.auction_id = aid
    bot.live.auction_name = 'Auction'
    bot.live.chat_id = -10
    bot.live.active = True
    bot.db.join(aid, 1, 'owner1', 'One', 100)
    bot.db.join(aid, 2, 'owner2', 'Two', 100)
    message = SimpleNamespace(reply_text=AsyncMock(), reply_document=AsyncMock(), reply_to_message=None)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=1, username='owner1', first_name='Owner'), effective_chat=SimpleNamespace(id=-10), message=message, callback_query=None)
    context = SimpleNamespace(args=[], bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1))))
    return bot, update, context


def command(bot, update, context, name, *args):
    context.args = list(args)
    asyncio.run(bot.AUCTION_COMMANDS[name](update, context))


def test_clock_settings_scoped_and_invalid_input_keeps_policy(tmp_path):
    bot,u,c = setup(tmp_path)
    assert bot.auction_controls.clock() == [60,40,20,10,5]
    command(bot,u,c,'atimer','45')
    assert bot.auction_controls.clock() == [45]
    command(bot,u,c,'atimer','20','40','10','5','1')
    assert bot.auction_controls.clock() == [45]
    other = bot.db.create_auction('Other', 2, 100, 1, 3, 0)
    bot.live.auction_id = other
    assert bot.auction_controls.clock() == [60,40,20,10,5]


def test_set_ids_roundtrip_replace_and_foreign_id_rejected(tmp_path):
    bot,u,c = setup(tmp_path)
    ctrl = bot.auction_controls
    ctrl.import_rows([{'player_id':71,'set_no':2,'name':'Second','version':'v2'}, {'player_id':70,'set_no':1,'name':'First'}])
    assert [p['player_id'] for p in bot.live.player_queue] == [70,71]
    ctrl.import_rows([{'player_id':71,'set_no':3,'name':'Updated'}], replace=True)
    assert bot.db.get_player(70)['status'] == 'withdrawn'
    assert [ctrl.pid(x) for x in bot.live.player_queue] == [71]
    other = bot.db.create_auction('Other',2,100,1,3,0)
    foreign = bot.db.add_player(other,'Foreign',1)
    try: ctrl.import_rows([{'player_id':foreign,'name':'Overwrite'}])
    except ValueError: pass
    else: assert False, 'Foreign auction ID was accepted'
    assert bot.db.get_player(foreign)['name'] == 'Foreign'


def test_withdraw_reinstate_and_unsold_bid_guard(tmp_path):
    bot,u,c = setup(tmp_path)
    p = bot.db.add_player(bot.live.auction_id,'Player',10)
    bot.live.current_player_id = p
    bot.live.current_bid = 10; bot.live.highest_bidder_id = 2
    command(bot,u,c,'aunsold',str(p))
    assert bot.db.get_player(p)['status'] == 'available'
    command(bot,u,c,'awithdraw',str(p))
    assert bot.live.current_player_id == p
    bot.auction_controls.clear_lot()
    command(bot,u,c,'awithdraw',str(p))
    assert bot.db.get_player(p)['status'] == 'withdrawn'
    command(bot,u,c,'areinstate',str(p))
    assert bot.db.get_player(p)['status'] == 'available'
    assert bot.auction_controls.pid(bot.live.player_queue[0]) == p


def test_recall_refunds_exact_sale_and_queues_next(tmp_path):
    bot,u,c = setup(tmp_path)
    aid = bot.live.auction_id
    p = bot.db.add_player(aid,'Sold',10)
    bot.db.set_player_status(p,'sold',2,30)
    bot.db.add_to_squad(aid,2,p); bot.db.deduct_purse(aid,2,30)
    bot.live.current_player_id = bot.db.add_player(aid,'Active',10)
    command(bot,u,c,'arecall',str(p))
    team = bot.db.get_part(aid,2)
    assert team['purse'] == 100 and team['total_spent'] == 0
    assert json.loads(team['squad']) == []
    assert bot.db.get_player(p)['status'] == 'available'
    assert bot.auction_controls.pid(bot.live.player_queue[0]) == p


def test_undo_bid_falls_back_only_within_current_lot(tmp_path):
    bot,u,c = setup(tmp_path)
    aid = bot.live.auction_id
    p = bot.db.add_player(aid,'Player',10)
    bot.db.record_bid(aid,1,p,'Player',10)
    bot.live.lot_bid_floor = bot.db.cx.execute('SELECT MAX(id) FROM bid_history').fetchone()[0]
    bot.db.record_bid(aid,1,p,'Player',20)
    bot.db.record_bid(aid,2,p,'Player',30)
    bot.live.current_player_id = p; bot.live.current_bid = 30; bot.live.highest_bidder_id = 2
    command(bot,u,c,'aundobid')
    assert (bot.live.current_bid,bot.live.highest_bidder_id) == (20,1)
    command(bot,u,c,'aundobid')
    assert (bot.live.current_bid,bot.live.highest_bidder_id) == (0,None)


def test_bid_gap_person_throttle_and_next_step(tmp_path):
    bot,u,c = setup(tmp_path)
    ctrl = bot.auction_controls
    ctrl.last_person_bid.clear(); ctrl.last_team_bid.clear()
    p = bot.db.add_player(bot.live.auction_id,'Player',10)
    bot.live.current_player_id = p
    command(bot,u,c,'adirect','off')
    assert ctrl.bid_error(10,1,20,True)
    assert ctrl.bid_error(10,1,10,True) is None
    ctrl.note_bid(10,1)
    assert ctrl.bid_error(10,1,10,True)
    assert ctrl.bid_error(20,2,10,True) is None
    command(bot,u,c,'abidgap','10')
    assert ctrl.bid_error(11,1,10,True)


def test_restart_resets_sales_and_keeps_withdrawals(tmp_path):
    bot,u,c = setup(tmp_path)
    aid = bot.live.auction_id
    p = bot.db.add_player(aid,'Sold',10)
    w = bot.db.add_player(aid,'Withdrawn',10)
    bot.db.set_player_status(w,'withdrawn')
    bot.db.set_player_status(p,'sold',2,30)
    bot.db.add_to_squad(aid,2,p); bot.db.deduct_purse(aid,2,30)
    command(bot,u,c,'arestart','confirm')
    assert bot.db.get_part(aid,2)['purse'] == 100
    assert bot.db.get_player(p)['status'] == 'available'
    assert bot.db.get_player(w)['status'] == 'withdrawn'
    assert json.loads(bot.db.get_part(aid,2)['squad']) == []


def test_completion_plans_role_limits_and_does_not_mutate_on_failure(tmp_path):
    bot,u,c = setup(tmp_path,min_players=2,max_players=2)
    aid = bot.live.auction_id
    bot.live.squad_limits = {'Bat':(1,1),'Bowl':(1,1)}
    for name,role in [('Bat1','Bat'),('Bat2','Bat'),('Bowl1','Bowl'),('Bowl2','Bowl')]:
        bot.db.add_player(aid,name,10,role)
    plan = bot.auction_controls.finish_plan()
    assert len(plan) == 4
    for uid in (1,2):
        assert sorted(bot.db.get_player(p)['role'] for p,t,price in plan if t == uid) == ['Bat','Bowl']
    bot.db.cx.execute('UPDATE participants SET purse=1 WHERE auction_id=?',(aid,)); bot.db.cx.commit()
    try: bot.auction_controls.finish_plan()
    except ValueError as exc: assert 'Cannot complete' in str(exc)
    else: assert False
    assert len(bot.db.get_sold(aid)) == 0


def test_finish_commits_complete_squads_and_modes(tmp_path):
    bot,u,c = setup(tmp_path)
    aid = bot.live.auction_id
    for name in ('First','Second','Remaining'): bot.db.add_player(aid,name,10,'Bat')
    async def run():
        c.args = ['go','quick']
        await bot.AUCTION_COMMANDS['afinish'](u,c)
        c.args = ['mute']
        await bot.AUCTION_COMMANDS['afinish'](u,c)
        await bot.auction_controls.finish_task
    asyncio.run(run())
    assert not bot.live.active
    assert len(bot.db.get_sold(aid)) == 2
    assert len(bot.db.get_unsold(aid)) == 1
    assert all(len(json.loads(t['squad'])) == 1 for t in bot.db.get_all_parts(aid))


def test_pause_resume_preserves_time_and_staged_bid_reset(tmp_path):
    bot,u,c = setup(tmp_path)
    aid = bot.live.auction_id
    bot.db.add_player(aid,'Clock Player',10,'Bat')
    bot.live.player_queue = list(bot.db.get_available(aid))
    async def run():
        await bot._do_next(c,-10)
        assert 58 <= bot.live.timer_ends_at - bot._time.time() <= 60
        await bot.process_bid(u,c,2,10)
        assert bot.live.timer_ends_at-bot._time.time() > 58
        await bot.AUCTION_COMMANDS['apause'](u,c)
        remaining = bot.live.paused_remaining
        await bot.AUCTION_COMMANDS['aresume'](u,c)
        assert abs(bot.live.timer_ends_at-bot._time.time()-remaining) < 1
        bot.live.timer_ends_at = bot._time.time()+8
        await bot.process_bid(u,c,1,20)
        assert 39 <= bot.live.timer_ends_at-bot._time.time() <= 40
        bot.live.timer_task.cancel()
    bot.auction_controls.last_person_bid.clear(); bot.auction_controls.last_team_bid.clear()
    asyncio.run(run())


def test_sets_export_contains_stable_ids_and_all_sets(tmp_path):
    bot,u,c = setup(tmp_path)
    bot.auction_controls.import_rows([{'player_id':70,'name':'One','set_no':1}, {'player_id':71,'name':'Two','set_no':2}])
    for fmt in ('json','csv'):
        command(bot,u,c,'asetsexport',fmt)
        kwargs = u.message.reply_document.call_args.kwargs
        raw = kwargs['document'].getvalue().decode()
        assert 'player_id' in raw and 'set_no' in raw and '70' in raw and '71' in raw
        assert kwargs['filename'].endswith('.'+fmt)


def test_invalid_import_is_atomic(tmp_path):
    bot,u,c = setup(tmp_path)
    try:
        bot.auction_controls.import_rows([{'player_id':70,'name':'Valid'}, {'player_id':71,'name':'Invalid','set_no':-1}])
    except ValueError: pass
    else: assert False
    assert bot.db.get_player(70) is None


def test_non_admin_cannot_change_auction_settings(tmp_path):
    bot,u,c = setup(tmp_path)
    u.effective_user.id = 2
    command(bot,u,c,'atimer','45')
    assert bot.auction_controls.clock() == [60,40,20,10,5]
    u.message.reply_text.assert_called_with('Admin only.')


def test_staged_clock_warnings_and_countdown(tmp_path, monkeypatch):
    bot,u,c = setup(tmp_path)
    player_id = bot.db.add_player(bot.live.auction_id,'Timer Player',10)
    bot.live.current_player_id = player_id
    now = [1000.0]
    monkeypatch.setattr(bot._time,'time',lambda: now[0])
    async def tick(seconds): now[0] += seconds
    monkeypatch.setattr(bot.asyncio,'sleep',tick)
    monkeypatch.setattr(bot,'_mark_unsold',AsyncMock())
    asyncio.run(bot.bid_timer(c))
    texts = [call.kwargs.get('text','') for call in c.bot.send_message.call_args_list]
    assert any('20 SECONDS LEFT' in text for text in texts)
    assert any('10 SECONDS LEFT' in text for text in texts)
    for second in (5,4,3,2,1):
        assert any(f'{second} SECOND' in text for text in texts)
    bot._mark_unsold.assert_awaited_once()


def test_normal_bidding_respects_role_caps(tmp_path):
    bot,u,c = setup(tmp_path)
    aid = bot.live.auction_id
    bot.live.squad_limits = {'Bat':(1,1),'Bowl':(2,1)}
    bat = bot.db.add_player(aid,'Bat',10,'Bat')
    bowler = bot.db.add_player(aid,'Bowl',10,'Bowl')
    bot.db.add_to_squad(aid,1,bat)
    assert bot.validate_bid(bot.db.get_part(aid,1), bot.db.get_player(bat), 10, bot.db.get_auction(aid))
    assert bot.validate_bid(bot.db.get_part(aid,1), bot.db.get_player(bowler), 10, bot.db.get_auction(aid)) is None


def test_remove_team_returns_players_and_conserves_budget(tmp_path):
    bot,u,c = setup(tmp_path)
    aid = bot.live.auction_id
    p = bot.db.add_player(aid,'Bought',10)
    bot.db.set_player_status(p,'sold',2,30)
    bot.db.add_to_squad(aid,2,p); bot.db.deduct_purse(aid,2,30)
    command(bot,u,c,'aremoveteam','Two')
    assert bot.db.get_part(aid,2) is None
    assert bot.db.get_part(aid,1)['purse'] == 200
    assert bot.db.get_player(p)['status'] == 'available'
    assert bot.auction_controls.pid(bot.live.player_queue[-1]) == p


def test_set_import_never_overwrites_owned_players(tmp_path):
    bot,u,c = setup(tmp_path)
    aid = bot.live.auction_id
    p = bot.db.add_player(aid,'Owned',10)
    bot.db.set_player_status(p,'sold',2,30)
    bot.db.add_to_squad(aid,2,p); bot.db.deduct_purse(aid,2,30)
    bot.auction_controls.import_rows([{'player_id':p,'name':'Overwrite','set_no':9}],replace=True)
    assert bot.db.get_player(p)['name'] == 'Owned'
    assert bot.db.get_player(p)['sold_to'] == 2
    assert bot.db.get_part(aid,2)['purse'] == 70


def test_live_lot_restore_preserves_bid_and_freezes_clock(tmp_path):
    bot,u,c = setup(tmp_path)
    p = bot.db.add_player(bot.live.auction_id,'Current',10)
    bot.live.current_player_id = p; bot.live.current_bid = 20
    bot.live.highest_bidder_id = 2; bot.live.highest_bidder_name = 'Two'
    bot.live.paused = True; bot.live.paused_remaining = 17
    bot.save_live_state()
    saved = json.loads(bot.db.get_setting('live_state'))
    bot.live.current_player_id = None; bot.live.current_bid = 0
    assert bot._apply_state_dict(saved)
    assert bot.live.current_player_id == p and bot.live.current_bid == 20
    assert bot.live.highest_bidder_id == 2
    assert bot.live.paused and bot.live.paused_remaining == 17
    assert not bot.live.player_queue
