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


def test_draft_desk_creates_draft_and_teams_without_token(tmp_path):
    bot = load_bot(tmp_path)
    client = bot.flask_app.test_client()
    assert client.get("/draft").status_code == 200
    response = client.post("/draft/auction", data={
        "name": "Website Draft", "max_teams": "2", "purse": "1000",
        "min_players": "11", "max_players": "25",
    })
    assert response.status_code == 302
    assert bot.live.auction_name == "Website Draft"
    response = client.post("/draft/teams", data={
        "team_name": "Tigers", "owner_tag_id": "101", "co_owner_tag_id": "202",
    })
    assert response.status_code == 302
    team = bot.db.get_all_parts(bot.live.auction_id)[0]
    assert team["team_name"] == "Tigers"
    assert team["username"] == "101"
    co_owner_id = bot.db.get_co_owners(bot.live.auction_id, team["user_id"])[0]["linked_user_id"]
    assert co_owner_id == 202


def test_draft_desk_imports_csv_without_token(tmp_path):
    bot = load_bot(tmp_path)
    make_auction(bot)
    client = bot.flask_app.test_client()
    csv = (b"name,rating,tier,icon_eligible,gender,indian_status,category,country,bat_hand,bowl_hand,bowl_style,bat_rating,bowl_rating\n"
           b"Jasprit Bumrah,95,Platinum,yes,Male,Indian,Pace,India,Right,Right,Fast,80,98\n")
    response = client.post("/draft/players", data={
        "file": (io.BytesIO(csv), "players.csv"),
    })
    assert response.status_code == 201
    assert response.json == {"imported_players": 1}


def test_draft_desk_shows_full_players_and_edits_tag_ids(tmp_path):
    bot = load_bot(tmp_path)
    aid = make_auction(bot)
    client = bot.flask_app.test_client()
    response = client.post("/draft/teams", data={
        "team_name": "Tigers", "owner_tag_id": "101", "co_owner_tag_id": "202",
    })
    assert response.status_code == 302
    bot.add_draft_player(aid, {"name": "Full Player", "tier": "Gold", "bowl_style": "Fast", "bat_rating": "81"})
    page = client.get("/draft").get_data(as_text=True)
    assert "Owner tag ID" in page
    assert "Bowl style" in page
    assert "Fast" in page
    assert bot.is_draft_picker(aid, {"team_name": "Tigers", "owner_tag_id": 101}, 101)
    assert bot.is_draft_picker(aid, {"team_name": "Tigers", "owner_tag_id": 101}, 202)
    assert not bot.is_draft_picker(aid, {"team_name": "Tigers", "owner_tag_id": 101}, 303)
    response = client.post("/draft/teams/101/edit", data={
        "team_name": "Lions", "owner_tag_id": "101", "co_owner_tag_id": "404",
    })
    assert response.status_code == 302
    team = bot.db.get_part(aid, 101)
    assert team["team_name"] == "Lions"
    assert bot.is_draft_picker(aid, {"team_name": "Lions", "owner_tag_id": 101}, 404)
