"""Chip planner: when to play Bench Boost, Triple Captain, Free Hit and Wildcard.

Instead of rules of thumb, each chip is valued in expected points for every
gameweek it could still be played in, using the same projections as the rest
of the tool:

- Bench Boost    = expected points of your four bench players that week
- Triple Captain = expected points of your best captain that week (the extra 1x)
- Free Hit       = best XI you could build for that week alone (same budget)
                   minus the best XI from your own squad
- Wildcard       = how much a rebuilt squad beats yours over the following weeks

Chips are then assigned to gameweeks (one per week, before they expire) so the
total expected gain is as large as possible. This lines up with the standard
advice: Bench Boost and Triple Captain shine in double gameweeks and great
fixtures, Free Hit in blank gameweeks or weeks when your players all face hard
opponents, and Wildcard when the squad needs an overhaul. Projections far ahead
are less reliable, and doubles/blanks are often announced late, so the plan is
re-run every week.
"""

from __future__ import annotations

import copy
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from itertools import permutations

from .analysis import CHIP_LABELS, MAX_PER_CLUB, POSITIONS, Game, project
from .planner import DISCOUNT, _best_xi, squad_value

SQUAD_SHAPE = {1: 2, 2: 5, 3: 5, 4: 3}
POOL_PER_POS = 40
WILDCARD_HORIZON = 6
MAX_LOOKAHEAD = 12

# Expected-points gain below which a chip isn't worth playing yet (keep it for a better week),
# unless it's about to expire. Benchmarks from FPL analysts: a good Bench Boost or Triple
# Captain is worth roughly 15-25 points in a double gameweek, 8-12 in a normal one.
WORTH_IT = {"bboost": 10.0, "3xc": 8.0, "freehit": 12.0, "wildcard": 15.0}


@dataclass
class ChipAdvice:
    chip: str
    label: str
    gw: int | None
    expected_gain: float
    why: str
    window_end: int
    play_now: bool = False


@dataclass
class ChipPlan:
    advice: list[ChipAdvice]
    weekly: list[dict]                 # per gameweek: values of each chip
    this_week: str
    doubles: dict[int, list[str]]
    blanks: dict[int, list[str]]
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Squad builder (for Free Hit and Wildcard): local search under budget/club/shape rules
# ---------------------------------------------------------------------------
def build_squad(game: Game, budget: int, score_fn, pool: dict[int, list[int]], start: list[int] | None = None) -> list[int]:
    cost = lambda pid: game.players[pid]["now_cost"]  # noqa: E731

    def legal(squad: list[int]) -> bool:
        if sum(cost(p) for p in squad) > budget:
            return False
        clubs: dict[int, int] = defaultdict(int)
        for p in squad:
            clubs[game.players[p]["team"]] += 1
        return max(clubs.values()) <= MAX_PER_CLUB

    if start is None or not legal(start):
        # Cheapest legal squad as a starting point.
        start, clubs = [], defaultdict(int)
        for et, n in SQUAD_SHAPE.items():
            for pid in sorted(pool[et], key=cost):
                if n == 0:
                    break
                if clubs[game.players[pid]["team"]] < MAX_PER_CLUB:
                    start.append(pid)
                    clubs[game.players[pid]["team"]] += 1
                    n -= 1
    squad = list(start)
    best = score_fn(squad)
    improved = True
    while improved:
        improved = False
        for i, out in enumerate(list(squad)):
            et = game.players[out]["element_type"]
            for cand in pool[et]:
                if cand in squad:
                    continue
                trial = squad[:i] + [cand] + squad[i + 1:]
                if not legal(trial):
                    continue
                s = score_fn(trial)
                if s > best + 1e-9:
                    squad, best, improved = trial, s, True
                    break
    return squad


# ---------------------------------------------------------------------------
# Chip planner
# ---------------------------------------------------------------------------
def plan_chips(game: Game, history: dict, squad_ids: list[int], bank: float) -> ChipPlan:
    next_gw = game.next_gw or game.current_gw + 1
    used = history.get("chips", [])

    windows = []  # (chip name, start, stop) still available
    for w in game.chips:
        name, start, stop = w["name"], w.get("start_event", 1), w.get("stop_event", game.last_gw)
        if stop < next_gw or name not in WORTH_IT:
            continue
        if any(c["name"] == name and start <= c["event"] <= stop for c in used):
            continue
        windows.append((name, start, stop))

    current = [(n, s, e) for n, s, e in windows if s <= next_gw]
    later = [(n, s, e) for n, s, e in windows if s > next_gw]
    last_gw = min(max((e for _, _, e in current), default=next_gw), next_gw + MAX_LOOKAHEAD - 1)
    gws = [g for g in range(next_gw, last_gw + 1) if g <= game.last_gw]
    span = list(range(next_gw, min(game.last_gw, last_gw + WILDCARD_HORIZON) + 1))

    # Projections for everyone who might matter.
    candidates = [p for p in game.players.values() if game.selectable(p) or p["id"] in squad_ids]
    proj = {p["id"]: project(p, game, span) for p in candidates}
    idx = {g: i for i, g in enumerate(span)}
    # Triple Captain is about ceiling, not recent streaks: rank captains on underlying stats
    # (xG/xA), which favours premiums who keep getting chances over short hot runs.
    xg_game = copy.copy(game)
    xg_game.model = "xg"
    tc_proj = {pid: project(game.players[pid], xg_game, span) for pid in squad_ids}

    pool: dict[int, list[int]] = defaultdict(list)
    for p in candidates:
        if game.selectable(p):
            pool[p["element_type"]].append(p["id"])

    def xi_at(squad: list[int], g: int) -> tuple[float, float]:
        score, _, bench = _best_xi([(game.players[pid]["element_type"], proj[pid][idx[g]]) for pid in squad])
        return score, bench

    budget = sum(game.players[pid]["now_cost"] for pid in squad_ids) + round(bank * 10)
    names = {n for n, _, _ in current}

    weekly = []
    for g in gws:
        xi, bench = xi_at(squad_ids, g)
        caps = sorted(
            (pid for pid in squad_ids if game.players[pid]["element_type"] != 1),
            key=lambda pid: tc_proj[pid][idx[g]], reverse=True,
        )
        row = {"gw": g, "your_xi": round(xi, 1), "bboost": round(bench, 1)}
        if caps:
            row["3xc"] = round(tc_proj[caps[0]][idx[g]], 1)
            row["3xc_player"] = game.players[caps[0]]["web_name"]
        if "freehit" in names:
            fh_pool = {et: sorted(ids, key=lambda pid: proj[pid][idx[g]], reverse=True)[:POOL_PER_POS]
                       for et, ids in pool.items()}
            fh = build_squad(game, budget, lambda s: xi_at(s, g)[0], fh_pool, start=squad_ids)
            row["freehit"] = round(xi_at(fh, g)[0] - xi, 1)
        if "wildcard" in names:
            wc_span = [x for x in span if g <= x < g + WILDCARD_HORIZON]
            sub = {pid: [proj[pid][idx[x]] for x in wc_span] for pid in proj}
            wc_pool = {et: sorted(ids, key=lambda pid: sum(sub[pid]), reverse=True)[:POOL_PER_POS]
                       for et, ids in pool.items()}
            wc = build_squad(game, budget, lambda s: squad_value(s, game, sub), wc_pool, start=squad_ids)
            row["wildcard"] = round(squad_value(wc, game, sub) - squad_value(squad_ids, game, sub), 1)
        weekly.append(row)

    # Assign chips to distinct gameweeks to maximise total value.
    chips_now = [n for n, _, _ in current]
    stop = {n: e for n, _, e in current}
    value = {(r["gw"], c): r.get(c, 0.0) for r in weekly for c in chips_now}
    best_total, best_assign = -1.0, {}
    slots = [r["gw"] for r in weekly]
    if chips_now and slots:
        options = slots + [None] * len(chips_now)  # None = don't play (only allowed if the chip doesn't expire in view)
        for combo in set(permutations(options, len(chips_now))):
            assign = dict(zip(chips_now, combo))
            ok = all(g is None or g <= stop[c] for c, g in assign.items())
            ok = ok and all(assign[c] is not None for c in chips_now if stop[c] <= last_gw)
            if not ok:
                continue
            total = sum(value.get((g, c), 0.0) for c, g in assign.items() if g is not None)
            if total > best_total:
                best_total, best_assign = total, assign

    advice = []
    row_by_gw = {r["gw"]: r for r in weekly}
    for c in chips_now:
        g = best_assign.get(c)
        gain = value.get((g, c), 0.0) if g else 0.0
        advice.append(ChipAdvice(
            chip=c, label=CHIP_LABELS.get(c, c), gw=g, expected_gain=round(gain, 1),
            why=_explain(c, g, row_by_gw.get(g, {}), game, weekly, stop[c]),
            window_end=stop[c], play_now=(g == next_gw),
        ))
    advice.sort(key=lambda a: (a.gw or 99))

    now = [a for a in advice if a.play_now]
    if now:
        a = now[0]
        this_week = f"Play your {a.label} this week (GW{next_gw}): about +{a.expected_gain:.0f} expected points."
    elif advice:
        nxt = min((a for a in advice if a.gw), key=lambda a: a.gw, default=None)
        this_week = (f"No chip this week. Next up: {nxt.label} in GW{nxt.gw}." if nxt
                     else "No chip this week.")
    else:
        this_week = "No chips left in this half of the season."

    doubles, blanks = {}, {}
    for g in span:
        counts = {t: len(game.fixtures_for(t, g)) for t in game.teams}
        if not any(counts.values()):  # nothing scheduled yet: unknown, not a blank
            continue
        d = [game.team_short(t) for t, n in counts.items() if n >= 2]
        b = [game.team_short(t) for t, n in counts.items() if n == 0]
        if d:
            doubles[g] = d
        if b:
            blanks[g] = b

    notes = []
    if not doubles and not blanks:
        notes.append("No double or blank gameweeks scheduled in this window: chips are timed on fixtures and form.")
    if later:
        start = min(s for _, s, _ in later)
        notes.append(f"Second-half chips unlock in GW{start}: save Bench Boost/Triple Captain for doubles, Free Hit for blanks.")
    if "wildcard" in names and "bboost" in names:
        notes.append("Tip: a Wildcard the week before a Bench Boost lets you build all 15 players for that week.")
    if "freehit" in names and stop.get("freehit") == 19:
        notes.append("If you play your first Free Hit in GW19 you can't play the second one in GW20.")
    notes.append("Re-run every week: chip timings update as injuries and fixtures change.")

    return ChipPlan(advice=advice, weekly=weekly, this_week=this_week, doubles=doubles, blanks=blanks, notes=notes)


def _explain(chip: str, g: int | None, row: dict, game: Game, weekly: list[dict], stop: int) -> str:
    if g is None:
        return "Hold it: no week in view is clearly worth it yet."
    gain = row.get(chip, 0.0)
    teams_double = [t for t in game.teams if len(game.fixtures_for(t, g)) >= 2]
    teams_blank = [t for t in game.teams if not game.fixtures_for(t, g)]
    others = [r.get(chip, 0.0) for r in weekly if r["gw"] != g]
    margin = gain - max(others) if others else gain
    parts = []
    if chip == "3xc":
        player = row.get("3xc_player", "your captain")
        p = next((x for x in game.players.values() if x["web_name"] == player), None)
        fx = game.fixtures_for(p["team"], g) if p else []
        if len(fx) >= 2:
            parts.append(f"{player} plays twice")
        elif fx:
            f = fx[0]
            where = "at home" if f["home"] else "away"
            parts.append(f"{player} {where} to {game.team_short(f['opp'])} (difficulty {f['fdr']})")
        parts.append(f"projected {gain:.1f} pts as captain")
    elif chip == "bboost":
        parts.append(f"your bench is projected {gain:.1f} pts")
        if teams_double:
            parts.append(f"{len(teams_double)} teams play twice")
    elif chip == "freehit":
        parts.append(f"a one-week squad beats yours by {gain:.1f} pts")
        if teams_blank:
            parts.append(f"{len(teams_blank)} teams blank")
    elif chip == "wildcard":
        parts.append(f"a rebuilt squad gains {gain:.1f} pts over the next {WILDCARD_HORIZON} weeks")
    if others and margin > 0.5:
        parts.append(f"+{margin:.1f} vs its next-best week")
    if gain < WORTH_IT.get(chip, 0) and g >= stop - 1:
        parts.append(f"modest, but it expires after GW{stop}")
    return "; ".join(parts)


def chip_plan_dict(plan: ChipPlan) -> dict:
    return asdict(plan)
