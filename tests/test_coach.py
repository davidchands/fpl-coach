import json
from collections import Counter

import pytest

from fpl_coach import analysis as A
from fpl_coach.cli import main
from fpl_coach.coach import analyze_team

from .fake_data import BLANK_GW, CURRENT_GW, DOUBLE_GW, FakeClient


@pytest.fixture
def client():
    return FakeClient()


@pytest.fixture
def game(client):
    return A.Game(client.bootstrap(), client.fixtures())


def test_game_context(game):
    assert game.current_gw == CURRENT_GW
    assert game.next_gw == CURRENT_GW + 1
    assert game.upcoming_gws(5) == [11, 12, 13, 14, 15]
    assert len(game.fixtures_for(1, DOUBLE_GW)) == 2
    assert game.fixtures_for(5, BLANK_GW) == []


def test_projection_respects_doubles_blanks_and_injuries(game):
    p = dict(game.players[1])  # ARS starting GK, ARS has two fixtures in the double
    single_gw = next(gw for gw in range(11, 20) if len(game.fixtures_for(1, gw)) == 1)
    dbl, single = A.project(p, game, [DOUBLE_GW, single_gw])
    assert dbl > single > 0
    blanker = next(x for x in game.players.values() if x["team"] == 5)
    assert A.project(blanker, game, [BLANK_GW]) == [0]

    injured = dict(p, chance_of_playing_next_round=0)
    inj = A.project(injured, game, [11, 12, 15])
    assert inj[0] == 0 and inj[1] > 0  # assumed to recover over time


def test_transfers_are_legal(game, client):
    squad = client.squad
    suggestions = A.suggest_transfers(game, squad, bank=1.5, free_transfers=1, gws=game.upcoming_gws(5), limit=15)
    assert suggestions, "fake squad has obvious weak links (benchwarmers)"
    names = {p["web_name"]: p for p in game.players.values()}
    club_counts = Counter(game.players[pid]["team"] for pid in squad)
    for t in suggestions:
        out, new = names[t.out], names[t.into]
        assert out["element_type"] == new["element_type"]
        assert new["now_cost"] <= out["now_cost"] + 15
        assert new["id"] not in squad
        after = club_counts[new["team"]] - (1 if new["team"] == out["team"] else 0) + 1
        assert after <= A.MAX_PER_CLUB
        assert not t.hit
    assert len({t.into for t in suggestions}) == len(suggestions)  # no duplicate targets
    gains = [t.gain for t in suggestions]
    assert gains == sorted(gains, reverse=True)


def test_hit_applied_without_free_transfers(game, client):
    s = A.suggest_transfers(game, client.squad, 1.5, 0, game.upcoming_gws(5))
    assert all(t.hit and t.net_gain == round(t.gain - 4, 1) for t in s)


def test_best_lineup_is_valid_formation(game, client):
    rows = A.squad_health(game, client.picks(123, 10)["picks"], game.upcoming_gws(5))
    lu = A.best_lineup(rows)
    d, m, f = map(int, lu["formation"].split("-"))
    assert d + m + f == 10 and 3 <= d <= 5 and 2 <= m <= 5 and 1 <= f <= 3
    assert len(lu["starting"]) == 11 and len(lu["bench"]) == 4
    assert lu["bench"][0].split("-")[1].startswith("G")  # backup keeper first on the bench
    assert lu["captain"] in lu["starting"]


def test_flags(game, client):
    rows = A.squad_health(game, client.picks(123, 10)["picks"], game.upcoming_gws(5))
    bench_mid = next(r for r in rows if r.name == "NEW-M5")
    assert not bench_mid.starting
    assert "rarely starts" in bench_mid.flags


def test_free_transfer_estimate(client):
    # transfers in GW3, 6, 9 (one each); wildcard in GW4 keeps transfers
    ft = A.estimate_free_transfers(client.history(123), CURRENT_GW)
    assert 1 <= ft <= 5
    history = {"current": [{"event": g, "event_transfers": 0} for g in range(1, 11)], "chips": []}
    assert A.estimate_free_transfers(history, 10) == 5  # capped
    history["current"][-1]["event_transfers"] = 3
    assert A.estimate_free_transfers(history, 10) == 3


def test_free_transfers_match_real_hits():
    # Real pattern: wildcard in GW4, then 3 transfers in GW8 cost -8, so only 1 FT was banked.
    made = {2: 1, 3: 1, 6: 2, 7: 1, 8: 3, 9: 1, 10: 1}
    history = {"current": [{"event": g, "event_transfers": made.get(g, 0)} for g in range(1, 12)],
               "chips": [{"name": "wildcard", "event": 4}]}
    assert A.estimate_free_transfers(history, 7) == 1  # FTs available for GW8: 3 transfers -> -8
    assert A.estimate_free_transfers(history, 11) == 2


def test_captain_is_never_a_goalkeeper(game, client):
    rows = A.squad_health(game, client.picks(123, 10)["picks"], game.upcoming_gws(5))
    for r in rows:
        if r.position == "GKP":
            r.next_gw = 99.0
    lu = A.best_lineup(rows)
    assert all(not n.split("-")[1].startswith("G") for n, _ in lu["captain_options"])


def test_chip_status(game, client):
    rows = A.squad_health(game, client.picks(123, 10)["picks"], game.upcoming_gws(8))
    chips = A.chip_status(game, client.history(123), rows)
    available = {c["chip"] for c in chips["available"]}
    # first-half wildcard/bboost used and the first window is over anyway: all second-half chips remain
    assert available == {"Wildcard", "Free Hit", "Bench Boost", "Triple Captain"}
    assert DOUBLE_GW in chips["doubles"] and BLANK_GW in chips["blanks"]
    assert any("Bench Boost" in t for t in chips["tips"])
    assert any("Free Hit" in t for t in chips["tips"])


def test_season_review(client):
    report = analyze_team(client, 123)
    rv = report["review"]
    assert rv["points_lost_to_hits"] == 4
    assert rv["bench_points_wasted"] == 5 * (CURRENT_GW - 1)  # bench boost week excluded
    assert rv["captain_weeks"] == CURRENT_GW
    assert rv["captain_points_missed"] >= 0
    assert rv["best_gw"]["gw"] == CURRENT_GW


def test_cli_text_and_json(client, capsys):
    assert main(["analyze", "123", "--free-transfers", "2"], client=client) == 0
    text = capsys.readouterr().out
    for section in ("Squad health", "Best lineup", "Transfer ideas", "Chips", "Season review"):
        assert section in text

    assert main(["analyze", "123", "--json", "--no-review"], client=client) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["team"]["bank"] == 1.5 and "review" not in data


def test_cli_bad_team(client, capsys):
    assert main(["analyze", "999"], client=client) == 1
    assert "error" in capsys.readouterr().err


# --- underlying-stats model -------------------------------------------------
def test_poisson_helpers():
    assert A._poisson_at_least(0, 3) == 0
    assert abs(A._poisson_at_least(10, 10) - 0.5421) < 1e-3
    assert abs(A._poisson_floor_half(2.0) - 0.7546) < 1e-3  # E[floor(X/2)], X~Poisson(2)


def test_xg_points_by_hand(client):
    b = client.bootstrap()
    fwd = dict(next(p for p in b["elements"] if p["element_type"] == 4), minutes=10**9)  # no shrinkage
    fwd.update(expected_goals_per_90=0.6, expected_assists_per_90=0.1, defensive_contribution_per_90=0,
               expected_goals_conceded_per_90=1.0, bonus=0, starts=10)
    game = A.Game(b, client.fixtures())
    # 2 (appearance) + 0.6*4 (goals) + 0.1*3 (assists) + 0 (no CS/DC points for forwards)
    assert abs(A.xg_points(fwd, game, 3) - 4.7) < 1e-6
    assert A.xg_points(fwd, game, 1) > A.xg_points(fwd, game, 5)


def test_defender_clean_sheet_value(client):
    b = client.bootstrap()
    d = dict(next(p for p in b["elements"] if p["element_type"] == 2), minutes=10**9)
    game = A.Game(b, client.fixtures())
    leaky = A.xg_points(dict(d, expected_goals_conceded_per_90=2.5), game, 3)
    tight = A.xg_points(dict(d, expected_goals_conceded_per_90=0.7), game, 3)
    assert tight - leaky > 1.5


def test_shrinkage_tames_small_samples(client):
    b = client.bootstrap()
    game = A.Game(b, client.fixtures())
    cameo = dict(next(p for p in b["elements"] if p["element_type"] == 4), minutes=45, expected_goals_per_90=2.0)
    assert game.shrunk(cameo, "expected_goals_per_90") < 0.4 * 2.0  # pulled most of the way to the average


def test_models_fall_back_without_xg(client):
    b = client.bootstrap()
    for p in b["elements"]:
        p.pop("expected_goals_per_90")
    game = A.Game(b, client.fixtures(), model="xg")
    p = b["elements"][0]
    assert A.fixture_points(p, game, 3) == A.base_rate(p, game)


def test_model_choice_changes_projection(client):
    b, f = client.bootstrap(), client.fixtures()
    p = b["elements"][3]
    results = {m: A.project(p, A.Game(b, f, model=m), [11, 12]) for m in A.MODELS}
    assert len({tuple(v) for v in results.values()}) == 3
    with pytest.raises(ValueError):
        A.Game(b, f, model="vibes")


def test_cli_model_flag(client, capsys):
    assert main(["analyze", "123", "--model", "xg", "--no-review"], client=client) == 0
    assert "model: xg" in capsys.readouterr().out
