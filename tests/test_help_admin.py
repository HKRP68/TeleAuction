import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from test_auction_controls import setup


def assign(bot,u,c,*args):
    c.args=list(args)
    asyncio.run(bot.cmd_auction_admin(u,c))


def test_help_sends_short_plain_overview_and_markdown_file(tmp_path):
    bot,u,c=setup(tmp_path)
    asyncio.run(bot.cmd_help(u,c))
    text=u.message.reply_text.call_args.args[0]
    assert '/auctionadmin' in text and '/arule Women 2_6' in text
    assert len(text)<4096
    assert 'parse_mode' not in u.message.reply_text.call_args.kwargs
    document=u.message.reply_document.call_args.kwargs
    assert document['filename']=='COMMANDS.md'
    guide=document['document'].getvalue().decode()
    assert '/auctionadmin remove' in guide and '/asquad 15_17' in guide
    assert '/atimer 60 40 20 10 5' in guide
    u.effective_user.id=2
    asyncio.run(bot.cmd_help(u,c))
    assert 'AUCTION ADMIN' not in u.message.reply_text.call_args.args[0]
    assert u.message.reply_document.await_count==2


def test_guide_download_without_an_auction(tmp_path):
    bot,u,c=setup(tmp_path)
    bot.live.auction_id=None
    asyncio.run(bot.cmd_commands(u,c))
    assert u.message.reply_document.call_args.kwargs['document'].getvalue().startswith(b'# Player Auction')


def test_every_registered_command_is_documented(tmp_path):
    bot,u,c=setup(tmp_path)
    guide=bot.help_admin.GUIDE_PATH.read_text()
    tree=ast.parse(Path(bot.__file__).read_text())
    build=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='build_app')
    commands=set(bot.AUCTION_COMMANDS)
    for node in ast.walk(build):
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='CommandHandler':
            arg=node.args[0]
            if isinstance(arg,ast.Constant): commands.add(arg.value)
            elif isinstance(arg,ast.List): commands.update(n.value for n in arg.elts)
    assert not {name for name in commands if '/'+name not in guide}


def test_grant_by_id_controls_current_auction_only_and_persists(tmp_path):
    bot,u,c=setup(tmp_path)
    aid=bot.live.auction_id
    assign(bot,u,c,'123456789')
    assert bot.db.is_admin(123456789)
    assert not bot.db.is_global_admin(123456789)
    bot.db._init()
    assert bot.db.is_admin(123456789)
    other=bot.db.create_auction('Other',2,100,1,3,0)
    bot.live.auction_id=other
    assert not bot.db.is_admin(123456789)
    assert bot.db.is_admin(123456789,aid)
    bot.live.auction_id=aid
    u.effective_user.id=123456789
    c.args=['Women','0_2']
    asyncio.run(bot.AUCTION_COMMANDS['arule'](u,c))
    assert bot.auction_rules.current(bot)['Women']==[2,0]


def test_only_global_admins_can_assign_or_revoke(tmp_path):
    bot,u,c=setup(tmp_path)
    aid=bot.live.auction_id
    assign(bot,u,c,'2')
    u.effective_user.id=2
    assign(bot,u,c,'3')
    assert not bot.db.is_admin(3)
    assert 'Only global admins' in u.message.reply_text.call_args.args[0]
    assign(bot,u,c,'remove','2')
    assert bot.db.is_admin(2)
    u.effective_user.id=1
    assign(bot,u,c,'remove','2')
    assert not bot.db.is_admin(2)
    events=bot.db.cx.execute('SELECT action,actor_id FROM auction_audit WHERE auction_id=? AND action LIKE ?', (aid,'auction_admin_%')).fetchall()
    assert [(r['action'],r['actor_id']) for r in events]==[('auction_admin_granted',1),('auction_admin_revoked',1)]


def test_username_reply_global_assignment_and_listing(tmp_path):
    bot,u,c=setup(tmp_path)
    bot.db.upsert_user(7,'assistant','Assistant')
    bot.db.set_admin(4,True)
    u.effective_user.id=4
    assign(bot,u,c,'add','@assistant')
    assert bot.db.is_admin(7)
    u.message.reply_to_message=SimpleNamespace(from_user=SimpleNamespace(id=8,username=None,first_name='Reply target',is_bot=False))
    assign(bot,u,c)
    assert bot.db.is_admin(8)
    assign(bot,u,c,'list')
    text=u.message.reply_text.call_args.args[0]
    assert '7 — @assistant' in text and '8 — Reply target' in text
    assign(bot,u,c,'remove')
    assert not bot.db.is_admin(8)


def test_scoped_admin_cannot_create_auctions_or_grant_global_access(tmp_path):
    bot,u,c=setup(tmp_path)
    aid=bot.live.auction_id
    assign(bot,u,c,'2')
    u.effective_user.id=2
    c.args=['2,100cr,15_17']
    asyncio.run(bot.cmd_create_auction(u,c))
    assert bot.live.auction_id==aid
    assert u.message.reply_text.call_args.args[0]=='Global admin only.'
    c.args=['3']
    asyncio.run(bot.cmd_admin(u,c))
    assert not bot.db.is_global_admin(3)
    assert u.message.reply_text.call_args.args[0]=='Super Admin only.'
    asyncio.run(bot.cmd_clear_players(u,c))
    assert u.message.reply_text.call_args.args[0]=='Super Admin only.'


def test_revoking_auction_grant_preserves_global_access(tmp_path):
    bot,u,c=setup(tmp_path)
    bot.db.set_admin(4,True)
    assign(bot,u,c,'4')
    assign(bot,u,c,'remove','4')
    assert bot.db.is_admin(4) and bot.db.is_global_admin(4)
    assert 'still has global admin' in u.message.reply_text.call_args.args[0]


def test_reject_unknown_user_and_non_admin(tmp_path):
    bot,u,c=setup(tmp_path)
    for target in ('@unknown','-123','0'):
        assign(bot,u,c,target)
        assert 'User not found' in u.message.reply_text.call_args.args[0]
    u.effective_user.id=2
    assign(bot,u,c,'3')
    assert u.message.reply_text.call_args.args[0]=='Admin only.'
    bot.live.auction_id=None
    u.effective_user.id=1
    assign(bot,u,c,'3')
    assert 'Create or restore' in u.message.reply_text.call_args.args[0]


def test_old_auction_rtm_buttons_cannot_mutate_other_auction(tmp_path):
    bot,u,c=setup(tmp_path)
    original=bot.live.auction_id
    p=bot.db.add_player(original,'Original',10)
    other=bot.db.create_auction('Other',2,100,1,3,0)
    bot.live.auction_id=other
    assign(bot,u,c,'2')
    query=SimpleNamespace(answer=AsyncMock())
    asyncio.run(bot._handle_callback_inner(u,c,query,f'ry|{p}|2|20|1|10',2))
    assert 'another auction' in query.answer.call_args.args[0]
    assert bot.db.get_player(p)['status']=='available'
