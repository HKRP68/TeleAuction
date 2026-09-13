import io

from test_price_utils import load_bot


def make_auction(bot):
    aid = bot.db.create_auction("Draft", 8, 1000, 11, 25, 1)
    bot.live.auction_id = aid
    bot.live.auction_name = "Draft"
    return aid


def test_draft_tier_rules_and_pick_progression(tmp_path):
    bot = load_bot(tmp_path)
    aid = make_auction(bot)
    platinum = bot.add_draft_player(aid, {"name": "Top Player", "tier": "Platinum"})
    bronze = bot.add_draft_player(aid, {"name": "Value Player", "tier": "Bronze"})
    bot.db.add_draft_order(aid, 1, 1, "Gold", "Team XYZ", "Owner", 123)

    order = bot.db.current_draft_order(aid)
    assert not bot.draft_tier_allowed("Platinum", order["tier"])
    assert bot.draft_tier_allowed("Bronze", order["tier"])
    assert bot.db.pick_draft_player(order["id"], bronze)
    assert not bot.db.pick_draft_player(order["id"], platinum)
    assert [p["name"] for p in bot.db.draft_squad(aid, "Team XYZ")] == ["Value Player"]


def test_draft_desk_requires_token_and_imports_csv(tmp_path, monkeypatch):
    monkeypatch.setenv("WEB_ADMIN_TOKEN", "draft-secret")
    bot = load_bot(tmp_path)
    make_auction(bot)
    client = bot.flask_app.test_client()
    assert client.get("/draft").status_code == 403
    csv = (b"name,rating,tier,icon_eligible,gender,indian_status,category,country,bat_hand,bowl_hand,bowl_style,bat_rating,bowl_rating\n"
           b"Jasprit Bumrah,95,Platinum,yes,Male,Indian,Pace,India,Right,Right,Fast,80,98\n")
    response = client.post("/draft/players?token=draft-secret", data={
        "file": (io.BytesIO(csv), "players.csv"),
    })
    assert response.status_code == 201
    assert response.json == {"imported_players": 1}
