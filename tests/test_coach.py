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
    for section in ("Squad health", "Your plan for GW", "More transfer ideas", "Chip plan", "Season review"):
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


# --- gameweek planner -------------------------------------------------------
from fpl_coach import planner as P  # noqa: E402


def test_best_xi_picks_formation_and_doubles_captain():
    players = [(1, 5), (1, 1)] + [(2, x) for x in (6, 5, 4, 1, 1)] + [(3, x) for x in (8, 3, 3, 2, 2)] + [(4, x) for x in (7, 1, 1)]
    score, formation, bench = P._best_xi(players)
    # best: GK 5 + DEF 6,5,4 + MID 8,3,3,2 + FWD 7,1  -> 3-4-3 = 44, +8 captain
    # Best 10 outfielders within the rules: 8,7,6,5,4,3,3,2,2,1 = 41; + GK 5 + captain 8 (doubles the 8)
    assert sum(formation) == 10 and formation[0] >= 3
    assert score == 54
    assert bench == sum(p for _, p in players) - (54 - 8)


def _check_plan_is_legal(plan, game, squad, bank):
    ids_by_name = {p["web_name"]: p for p in game.players.values()}
    out = [ids_by_name[t.out] for t in plan.transfers]
    inn = [ids_by_name[t.into] for t in plan.transfers]
    final = [pid for pid in squad if pid not in {p["id"] for p in out}] + [p["id"] for p in inn]
    assert len(final) == 15 == len(set(final))
    assert sum(p["now_cost"] for p in inn) <= sum(p["now_cost"] for p in out) + round(bank * 10)
    assert Counter(game.players[pid]["element_type"] for pid in final) == {1: 2, 2: 5, 3: 5, 4: 3}
    assert max(Counter(game.players[pid]["team"] for pid in final).values()) <= 3
    assert len(plan.starting) == 11 and len(plan.bench) == 4
    d, m, f = map(int, plan.formation.split("-"))
    assert d + m + f == 10 and 3 <= d <= 5 and 2 <= m <= 5 and 1 <= f <= 3
    starters = {p["name"] for p in plan.starting}
    assert plan.captain in starters and plan.vice in starters and plan.captain != plan.vice
    assert all(p["position"] != "GKP" for p in plan.starting if p["name"] == plan.captain)
    assert {p["name"] for p in plan.starting + plan.bench} == {game.players[pid]["web_name"] for pid in final}


@pytest.mark.parametrize("fts,max_hits", [(0, 0), (1, 0), (1, 1), (2, 1), (3, 0)])
def test_plan_is_always_legal(game, client, fts, max_hits):
    plan = P.plan_gameweek(game, client.squad, 1.5, fts, game.upcoming_gws(5), max_hits=max_hits)
    _check_plan_is_legal(plan, game, client.squad, 1.5)
    assert len(plan.transfers) <= fts + max_hits
    assert plan.hits == max(0, len(plan.transfers) - fts)


def test_plan_never_hits_when_disallowed(game, client):
    plan = P.plan_gameweek(game, client.squad, 1.5, 0, game.upcoming_gws(5), max_hits=0)
    assert plan.transfers == [] and plan.hits == 0


def test_plan_rolls_transfer_when_nothing_is_better(client):
    b = client.bootstrap()
    squad = set(client.squad)
    for p in b["elements"]:
        if p["id"] not in squad:  # everyone outside the squad is useless
            p.update(form="0", points_per_game="0", starts=0, minutes=0, expected_goals_per_90=0,
                     expected_assists_per_90=0, expected_goals_conceded_per_90=3, bonus=0)
    game = A.Game(b, client.fixtures())
    plan = P.plan_gameweek(game, client.squad, 1.5, 1, game.upcoming_gws(5))
    assert plan.transfers == []
    assert any("save your free transfer" in r for r in plan.reasoning)


def test_plan_takes_worthwhile_hit_and_never_starts_injured(client):
    b = client.bootstrap()
    game = A.Game(b, client.fixtures())
    # Make the squad's best forward a long-term injury: replacing him should beat a -4.
    fwd = max((pid for pid in client.squad if game.players[pid]["element_type"] == 4),
              key=lambda pid: float(game.players[pid]["form"]))
    game.players[fwd].update(status="i", chance_of_playing_next_round=0, news="Knee injury - Unknown return date")
    hit = P.plan_gameweek(game, client.squad, 1.5, 0, game.upcoming_gws(5), max_hits=1)
    assert hit.hits == 1 and hit.horizon_gain > P.HIT_MARGIN  # the -4 is clearly worth it
    for plan in (hit, P.plan_gameweek(game, client.squad, 1.5, 3, game.upcoming_gws(5), max_hits=0)):
        assert game.players[fwd]["web_name"] not in {p["name"] for p in plan.starting}


def test_long_injuries_recover_slowly(game):
    p = dict(next(iter(game.players.values())), status="i", chance_of_playing_next_round=0)
    knock = A.project(dict(p, news="Hamstring injury - 25% chance of playing"), game, [11, 12, 13, 14])
    long = A.project(dict(p, news="Knee injury - Unknown return date"), game, [11, 12, 13, 14])
    gone = A.project(dict(p, status="u", news="Joined another club"), game, [11, 12, 13, 14])
    assert sum(gone) == 0 < sum(long) < sum(knock)


def test_report_has_plan_section(client, capsys):
    assert main(["analyze", "123", "--no-review"], client=client) == 0
    text = capsys.readouterr().out
    assert "Your plan for GW11" in text and "Start this XI" in text and "Captain" in text


# --- chip planner -------------------------------------------------------------
from fpl_coach import chips as C  # noqa: E402


@pytest.fixture
def chip_plan(game, client):
    return C.plan_chips(game, client.history(123), client.squad, 1.5)


def test_chip_plan_assigns_each_chip_to_a_distinct_week_before_expiry(chip_plan):
    weeks = [a.gw for a in chip_plan.advice if a.gw]
    assert len(weeks) == len(set(weeks))  # one chip per gameweek
    for a in chip_plan.advice:
        assert a.gw is None or a.gw <= a.window_end
    labels = {a.label for a in chip_plan.advice}
    assert labels == {"Wildcard", "Free Hit", "Bench Boost", "Triple Captain"}


def test_chip_values_follow_doubles_and_blanks(chip_plan):
    by_gw = {r["gw"]: r for r in chip_plan.weekly}
    # Fake squad has four players from teams with a double in DGW13: the bench and captain gain.
    assert by_gw[DOUBLE_GW]["3xc"] == max(r["3xc"] for r in chip_plan.weekly)
    assert by_gw[DOUBLE_GW]["bboost"] == max(r["bboost"] for r in chip_plan.weekly)
    # Blank GW14 hits several of the fake squad's teams: a Free Hit beats any normal week.
    normal = [r["freehit"] for r in chip_plan.weekly if r["gw"] not in (DOUBLE_GW, BLANK_GW)]
    assert by_gw[BLANK_GW]["freehit"] > max(normal)
    assert by_gw[BLANK_GW]["your_xi"] < min(r["your_xi"] for r in chip_plan.weekly if r["gw"] != BLANK_GW)
    # The double and the blank are the weeks the plan uses.
    assert {DOUBLE_GW, BLANK_GW} <= {a.gw for a in chip_plan.advice}
    assert DOUBLE_GW in chip_plan.doubles and BLANK_GW in chip_plan.blanks


def test_chip_plan_this_week_message(chip_plan):
    assert chip_plan.this_week.startswith(("Play your", "No chip this week"))


def test_free_hit_squad_is_legal(game, client):
    gws = game.upcoming_gws(1)
    budget = sum(game.players[p]["now_cost"] for p in client.squad) + 15
    pool = {}
    for p in game.players.values():
        if game.selectable(p):
            pool.setdefault(p["element_type"], []).append(p["id"])
    score = lambda s: P._best_xi([(game.players[p]["element_type"], A.project(game.players[p], game, gws)[0]) for p in s])[0]  # noqa: E731
    squad = C.build_squad(game, budget, score, pool, start=client.squad)
    assert len(squad) == 15 == len(set(squad))
    assert Counter(game.players[p]["element_type"] for p in squad) == {1: 2, 2: 5, 3: 5, 4: 3}
    assert max(Counter(game.players[p]["team"] for p in squad).values()) <= 3
    assert sum(game.players[p]["now_cost"] for p in squad) <= budget
    assert score(squad) >= score(client.squad)


def test_unscheduled_weeks_are_not_blanks(client):
    b, f = client.bootstrap(), [x for x in client.fixtures() if x["event"] != 18]
    game = A.Game(b, f)
    plan = C.plan_chips(game, client.history(123), client.squad, 1.5)
    assert 18 not in plan.blanks


def test_report_has_chip_plan(client, capsys):
    assert main(["analyze", "123", "--no-review"], client=client) == 0
    text = capsys.readouterr().out
    assert "Chip plan" in text and "Expected gain from each chip" in text


# --- consistency, explanations and colour -----------------------------------
from fpl_coach.report import render, table  # noqa: E402
from fpl_coach.style import Style, strip, visible_len  # noqa: E402


def test_confidence_prefers_proven_players(game):
    p = dict(next(x for x in game.players.values() if x["element_type"] == 3 and x["starts"] == 10))
    proven, _ = A.confidence(p, game)
    cameo, note = A.confidence(dict(p, minutes=120, starts=1), game)
    assert proven > cameo and "small sample" in note
    hot, note = A.confidence(dict(p, form="12.0", points_per_game="10.0"), game)
    assert hot < proven and "cool off" in note
    assert A.confidence_label(proven) in ("high", "medium")
    assert all(r <= x + 1e-9 for r, x in zip(A.risk_adjusted(p, game, [11, 12]), A.project(p, game, [11, 12])))


def test_plan_looks_far_enough_ahead_and_avoids_bad_weeks(game, client):
    plan = P.plan_gameweek(game, client.squad, 1.5, 2, game.upcoming_gws(3), max_hits=1)
    assert len(plan.impact) >= P.PLAN_MIN_HORIZON
    # after the first week (which carries any hit), no week should get clearly worse
    for week in plan.impact[1:]:
        assert week["after"] >= week["before"] - 1.0


def test_every_transfer_is_explained(game, client):
    plan = P.plan_gameweek(game, client.squad, 1.5, 2, game.upcoming_gws(5), max_hits=1)
    assert plan.transfers
    for t in plan.transfers:
        assert t.out_reasons and t.in_reasons and t.summary
        assert len(t.weekly) == len(plan.impact)
        assert t.confidence in ("high", "medium", "low")
        assert not any("cool off" in r for r in t.in_reasons)  # warnings live in cautions
    assert plan.captain_reason.startswith(plan.captain)


def test_downside_penalty_prefers_steady_over_boom_and_bust():
    class G:  # minimal stand-in for Game
        players = {i: {"element_type": et} for i, et in enumerate([1, 1] + [2] * 5 + [3] * 5 + [4] * 3)}
    squad = list(G.players)
    current = {i: [4.0, 4.0] for i in squad}   # what you have now
    steady = {i: [4.5, 4.5] for i in squad}    # a bit better every week
    spiky = {i: [5.5, 3.5] for i in squad}     # same total, but great next week and worse than now after
    base = P.weekly_xi(squad, G, current)
    # Without the penalty, the front-loaded plan looks better (later weeks are discounted)...
    assert P.squad_value(squad, G, spiky) > P.squad_value(squad, G, steady)
    # ...with it, the steady plan wins because the boom-and-bust one makes a week worse than now.
    assert P.squad_value(squad, G, spiky, base) < P.squad_value(squad, G, steady, base)


def test_colour_on_and_off(client):
    report = analyze_team(client, 123, review=False)
    coloured, plain = render(report, color=True), render(report, color=False)
    assert "\x1b[" in coloured and "\x1b[" not in plain
    assert strip(coloured) == plain  # colour never changes the text or the alignment


def test_table_aligns_coloured_cells():
    s = Style(True)
    out = table(["A", "B"], [[s("x", "bred"), "1"], ["yyy", s("22", "bgreen")]], "lr", s)
    widths = {visible_len(line) for line in out.split("\n")}
    assert len(widths) == 1


def test_cli_colour_flags(client, capsys, monkeypatch):
    assert main(["analyze", "123", "--no-review", "--color"], client=client) == 0
    assert "\x1b[" in capsys.readouterr().out
    assert main(["analyze", "123", "--no-review", "--no-color"], client=client) == 0
    assert "\x1b[" not in capsys.readouterr().out
    monkeypatch.setenv("NO_COLOR", "1")
    assert main(["analyze", "123", "--no-review"], client=client) == 0
    assert "\x1b[" not in capsys.readouterr().out
