"""Command documentation and persistent auction-scoped admin permissions."""
import io
from pathlib import Path

GUIDE_PATH = Path(__file__).resolve().with_name('COMMANDS.md')


async def send_guide(update):
    await update.message.reply_document(
        document=io.BytesIO(GUIDE_PATH.read_bytes()), filename='COMMANDS.md',
        caption='Auction command guide — syntax, examples, permissions and squad rules.')


async def help_command(bot, update, context):
    await bot._reg(update.effective_user)
    text = (
        'AUCTION HELP\n'
        '/start — Register and see your Telegram ID\n'
        '/bid [amount] — Bid the next step or a typed amount\n'
        '/status · /purse · /squad — Follow the auction\n'
        '/pick player — Make your scheduled draft pick\n'
        '/myrtm · /rtm — View/use your RTM cards\n'
        '/help or /commands — Download this command guide\n\n'
        'Website: /draft (control room), /watch (live spectator board).'
    )
    if bot.db.is_admin(update.effective_user.id):
        text += (
            '\n\nAUCTION ADMIN\n'
            '/addteams — Add Team Name, owner_id, co_owner_id per line\n'
            '/astart · /anext · /apause · /aresume — Run the clock\n'
            '/asold · /aunsold · /aforce · /arecall · /aundobid — Manage lots\n'
            '/atimer 60 40 20 10 5 — Staged clock\n'
            '/abidgap off · /adirect on — Bid controls\n'
            '/asquad 15_17 — Squad size\n'
            '/arule Women 2_6 · /arule Rating<=84 4 — Category quotas\n'
            '/arules show · /areserve auto — Rules and purse reserve\n'
            '/asetsexport csv · /asetsimport — Export/import with a preview\n'
            '/ateamsummary · /aaudit — Squads and audit log\n'
            '/auctionadmin list — List admins for the current auction\n'
            'Website: /draft/rules and /draft/audit.'
        )
    if bot.db.is_global_admin(update.effective_user.id):
        text += (
            '\n\nADMIN ACCESS\n'
            '/auctionadmin @username — Grant admin for this auction\n'
            '/auctionadmin remove 123456789 — Revoke auction access\n'
            'You can also reply to someone with /auctionadmin.\n'
            '/create_auction 8,100cr,15_17 — Create a fresh auction'
        )
    if update.effective_user.id == bot.Config.SUPER_ADMIN_ID:
        text += '\n/admin @username — Grant global admin access (super admin only).'
    await update.message.reply_text(text)
    await send_guide(update)


async def auction_admin(bot, update, context):
    await bot._reg(update.effective_user)
    actor = update.effective_user.id
    aid = bot.live.auction_id
    if not aid or not bot.db.get_auction(aid):
        await update.message.reply_text('Create or restore an auction first.'); return
    if not bot.db.is_admin(actor):
        await update.message.reply_text('Admin only.'); return
    args = list(context.args)
    reply = update.message.reply_to_message
    target_user = getattr(reply, 'from_user', None) if reply else None
    if not args and not target_user:
        await update.message.reply_text(
            'Usage: /auctionadmin @username or <user_id>\n'
            '/auctionadmin remove @username\n'
            '/auctionadmin list\n'
            'Or reply to a user with /auctionadmin to grant access.\n'
            'Only global admins can grant/revoke; access applies to this auction only.')
        return
    if args == ['list']:
        rows = bot.db.cx.execute('''SELECT a.user_id, u.username, u.first_name
            FROM auction_admins a LEFT JOIN global_users u ON u.user_id=a.user_id
            WHERE a.auction_id=? ORDER BY a.user_id''', (aid,)).fetchall()
        text = 'Auction admins — ' + bot.live.auction_name + '\n'
        text += '\n'.join(f"{r['user_id']} — @{r['username']}" if r['username'] else f"{r['user_id']} — {r['first_name'] or 'User'}" for r in rows) or 'No auction-specific admins assigned.'
        text += '\nGlobal admins and the super admin retain access.'
        for start in range(0,len(text),3900): await update.message.reply_text(text[start:start+3900])
        return
    if not bot.db.is_global_admin(actor):
        await update.message.reply_text('Only global admins can assign or remove auction admins.'); return
    mode = 'add'
    if args and args[0].lower() in ('add','remove'):
        mode = args.pop(0).lower()
    if len(args)>1:
        await update.message.reply_text('Usage: /auctionadmin [add|remove] @username or <user_id>'); return
    uid = bot.db.resolve_uid(args[0]) if args else (target_user.id if target_user else None)
    if not uid or uid<=0:
        await update.message.reply_text('User not found. Use a positive Telegram user ID, reply to their message, or ask them to /start before using @username.'); return
    if target_user and not args:
        if target_user.is_bot:
            await update.message.reply_text('Choose a person rather than a bot.'); return
        bot.db.upsert_user(target_user.id,target_user.username or '',target_user.first_name or '')
    with bot.db.cx:
        if mode == 'add':
            bot.db.cx.execute('INSERT OR IGNORE INTO auction_admins(auction_id,user_id,granted_by) VALUES(?,?,?)',(aid,uid,actor))
        else:
            bot.db.cx.execute('DELETE FROM auction_admins WHERE auction_id=? AND user_id=?',(aid,uid))
        import json
        bot.db.cx.execute('INSERT INTO auction_audit(auction_id,actor_id,action,detail) VALUES(?,?,?,?)',
                          (aid,actor,'auction_admin_'+('granted' if mode=='add' else 'revoked'),json.dumps({'user_id':uid})))
    bot.save_live_state()
    text = f"{'Granted' if mode=='add' else 'Removed'} auction-admin access for {uid} in {bot.live.auction_name}."
    if mode=='remove' and bot.db.is_global_admin(uid):
        text += '\nThis user still has global admin access.'
    await update.message.reply_text(text)
