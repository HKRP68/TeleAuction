"""Explicit cancellation of an auction without deleting its historical results."""
import json
from flask import request, redirect


def cancel(bot,actor=None):
    aid=bot.live.auction_id
    ar=bot.db.get_auction(aid) if aid else None
    if not ar: raise ValueError('Create or restore an auction first.')
    if ar['status']=='cancelled': raise ValueError('This auction is already cancelled.')
    if ar['status']=='completed': raise ValueError('This auction is completed. Its results are already saved.')
    if bot.auction_controls.finish_task and not bot.auction_controls.finish_task.done():
        raise ValueError('Automatic completion has already saved results. Use /afinish mute to stop playback.')
    pr=bot.db.get_player(bot.live.current_player_id) if bot.live.current_player_id else None
    detail={'player_id':bot.live.current_player_id,'voided_bid':bot.live.current_bid,'bidder_id':bot.live.highest_bidder_id,
            'sales_preserved':len(bot.db.get_sold(aid))}
    with bot.db.cx:
        # RTM cards are deducted at activation; return an outstanding, unresolved card.
        if bot.live.rtm_state in (bot.RTM_ACTIVE,bot.RTM_COUNTER) and bot.live.rtm_team_id and (not pr or pr['status']!='sold'):
            bot.db.cx.execute('UPDATE participants SET rtm_cards=rtm_cards+1 WHERE auction_id=? AND user_id=?',(aid,bot.live.rtm_team_id))
        bot.db.cx.execute("UPDATE auctions SET status='cancelled' WHERE auction_id=?",(aid,))
        bot.db.cx.execute('DELETE FROM import_previews WHERE auction_id=?',(aid,))
        bot.db.cx.execute('INSERT INTO auction_audit(auction_id,actor_id,action,detail) VALUES(?,?,?,?)',(aid,actor,'auction_cancelled',json.dumps(detail)))
    bot.auction_controls.clear_lot()
    bot.live.auto_next_epoch+=1
    bot.live.active=False;bot.live.paused=False
    bot.live.auto_next_on=False;bot.live.auto_next_secs=None
    bot.live.player_queue=[];bot.live.undo_snapshot=None;bot.live.last_sold_pid=None
    bot.live.rtm_team_id=None;bot.live.rtm_team_name='';bot.live.rtm_counter_bid=0
    bot.live.rtm_counter_ends_at=None;bot.live.rtm_decision_ends_at=None
    bot.save_live_state()


def closed(bot):
    ar=bot.db.get_auction(bot.live.auction_id) if bot.live.auction_id else None
    return bool(ar and ar['status']=='cancelled')


def install(bot):
    async def command(update,context):
        await bot._reg(update.effective_user)
        if not bot.db.is_admin(update.effective_user.id): await update.message.reply_text('Admin only.');return
        if context.args!=['confirm']:
            await update.message.reply_text('To cancel: /acancel confirm\nThis stops the clock, voids the outstanding bid and closes registration/bidding. Completed sales, squads, purses and records are preserved for export. Reopen only with /arestart confirm, which resets sales and purses.');return
        try: cancel(bot,update.effective_user.id)
        except ValueError as exc: await update.message.reply_text(str(exc));return
        await update.message.reply_text('Auction cancelled. Clock and bidding stopped; completed sales, squads and purse balances are preserved. Export with /exportplayers or /downloaddata. Create a new auction, or /arestart confirm to reset and reopen this one.')

    @bot.flask_app.route('/draft/cancel',methods=['POST'])
    def web_cancel():
        bot._web_draft_aid()
        if request.form.get('confirm')!='confirm':
            return bot.auction_rules.page('Confirm cancellation','<section class="panel"><h2>Cancel this auction?</h2><p>Stops the clock, voids the outstanding bid and closes registration/bidding. Completed sales, squads, purses and player records are preserved. Reopening requires an explicit restart.</p><form method="post"><input type="hidden" name="confirm" value="confirm"><button>Cancel auction</button></form><p><a href="/draft">Keep auction</a></p></section>')
        try: cancel(bot)
        except ValueError as exc:
            from flask import abort
            abort(400,str(exc))
        return redirect('/draft')
    return {'acancel':command,'cancelauction':command}
