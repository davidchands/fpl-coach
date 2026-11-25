"""Gameweek planner: which transfers to make, then who to start and captain, and why.

The planner aims for consistent points, not one lucky week:

- It plans over at least PLAN_MIN_HORIZON gameweeks, so a transfer that helps
  next week but hurts the weeks after is seen for what it is.
- Decisions use confidence-adjusted projections (see `analysis.risk_adjusted`):
  proven starters whose points are backed by underlying stats are preferred
  over hot streaks and small samples.
- A squad is valued by what it would actually score: the best legal XI plus
  captain in every gameweek, a little for the bench, later weeks discounted.
- Weeks in which the new squad would score LESS than your current one are
  penalised on top (DOWNSIDE), so the plan avoids "great now, bad later" moves.

The search tries every single transfer, every pair (finds "downgrade X to afford
Y" moves), then extends greedily. Hits are only taken when the gain clearly
beats the -4, and an unused free transfer has value because it rolls over.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from .analysis import (
    DC_THRESHOLD,
    HIT_COST,
    MAX_PER_CLUB,
    POSITIONS,
    Game,
    _poisson_at_least,
    availability,
    confidence,
    confidence_label,
    project,
    reliability,
    risk_adjusted,
)

FORMATIONS = [(d, m, f) for d in range(3, 6) for m in range(2, 6) for f in range(1, 4) if d + m + f == 10]

PLAN_MIN_HORIZON = 6    # always look at least this many gameweeks ahead when planning transfers
DISCOUNT = 0.85         # weight of each gameweek relative to the one before
BENCH_WEIGHT = 0.1      # bench points matter a little (injury cover)
DOWNSIDE = 1.0          # extra penalty per point a week gets WORSE than your current squad
HIT_MARGIN = 2.0        # a hit must beat -4 by at least this much (projections are noisy)
ROLL_VALUE = 1.5        # value of saving a free transfer for next week
CANDIDATES_PER_POS = 25
PAIR_SHORTLIST = 10
MAX_FREE_TRANSFERS = 5


@dataclass
class PlannedTransfer:
    out: str
    out_team: str
    price_out: float
    into: str
    into_team: str
    price_in: float
    position: str
    out_reasons: list[str] = field(default_factory=list)
    in_reasons: list[str] = field(default_factory=list)
    weekly: list[dict] = field(default_factory=list)   # [{gw, out, in, fixture_out, fixture_in}]
    summary: str = ""
    warning: str = ""
    confidence: str = ""
    cautions: list[str] = field(default_factory=list)


@dataclass
class GameweekPlan:
    gw: int
    transfers: list[PlannedTransfer]
    hits: int
    hit_cost: int
    free_transfers_used: int
    bank_after: float
    formation: str
    starting: list[dict]          # [{name, team, position, xpts, confidence}]
    bench: list[dict]
    captain: str
    vice: str
    expected_points: float        # next GW incl. captain, after transfers and hits
    expected_points_no_transfers: float
    horizon_gain: float           # discounted, confidence-adjusted points gained, after hits
    reasoning: list[str] = field(default_factory=list)
    final_squad: list[int] = field(default_factory=list)
    impact: list[dict] = field(default_factory=list)   # [{gw, before, after}] best XI + captain per week
    captain_reason: str = ""


# ---------------------------------------------------------------------------
# Lineup maths on plain numbers (fast, used thousands of times per search)
# ---------------------------------------------------------------------------
def _best_xi(players: list[tuple[int, float]]) -> tuple[float, tuple[int, int, int], float]:
    """players: (element_type, points). Returns (XI points incl. captain, formation, bench points)."""
    by_pos: dict[int, list[float]] = defaultdict(list)
    for et, pts in players:
        by_pos[et].append(pts)
    for v in by_pos.values():
        v.sort(reverse=True)
    gk, d, m, f = by_pos[1], by_pos[2], by_pos[3], by_pos[4]
    total = sum(pts for _, pts in players)
    best, best_form = -1.0, (4, 4, 2)
    for nd, nm, nf in FORMATIONS:
        if len(d) < nd or len(m) < nm or len(f) < nf or not gk:
            continue
        outfield = d[:nd] + m[:nm] + f[:nf]
        score = gk[0] + sum(outfield) + max(outfield)  # captain doubles the best outfielder
        if score > best:
            best, best_form = score, (nd, nm, nf)
    if best < 0:
        return 0.0, best_form, 0.0
    captain = max(d[: best_form[0]] + m[: best_form[1]] + f[: best_form[2]])
    bench = total - (best - captain)
    return best, best_form, bench


def weekly_xi(squad: list[int], game: Game, proj: dict[int, list[float]]) -> list[float]:
    n = len(proj[squad[0]]) if squad else 0
    return [_best_xi([(game.players[pid]["element_type"], proj[pid][i]) for pid in squad])[0] for i in range(n)]


def squad_value(squad: list[int], game: Game, proj: dict[int, list[float]],
                baseline: list[float] | None = None) -> float:
    """Discounted best-XI points over the horizon, a little for the bench, minus a
    penalty for every week that scores below `baseline` (the current squad)."""
    n = len(proj[squad[0]]) if squad else 0
    value = 0.0
    for i in range(n):
        xi, _, bench = _best_xi([(game.players[pid]["element_type"], proj[pid][i]) for pid in squad])
        w = DISCOUNT**i
        value += w * (xi + BENCH_WEIGHT * bench)
        if baseline is not None and xi < baseline[i]:
            value -= w * DOWNSIDE * (baseline[i] - xi)
    return value


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def _legal(squad: list[int], game: Game, budget_left: int) -> bool:
    if budget_left < 0:
        return False
    clubs: dict[int, int] = defaultdict(int)
    for pid in squad:
        clubs[game.players[pid]["team"]] += 1
    return max(clubs.values()) <= MAX_PER_CLUB


def plan_gameweek(
    game: Game,
    squad_ids: list[int],
    bank: float,
    free_transfers: int,
    gws: list[int],
    max_hits: int = 1,
) -> GameweekPlan:
    if gws and len(gws) < PLAN_MIN_HORIZON:
        gws = [g for g in range(gws[0], gws[0] + PLAN_MIN_HORIZON) if g <= game.last_gw]

    plain: dict[int, list[float]] = {}
    safe: dict[int, list[float]] = {}

    def ensure(pid: int) -> None:
        if pid not in safe:
            plain[pid] = project(game.players[pid], game, gws)
            safe[pid] = risk_adjusted(game.players[pid], game, gws)

    for pid in squad_ids:
        ensure(pid)

    def discounted(pid: int) -> float:
        ensure(pid)
        return sum((DISCOUNT**i) * x for i, x in enumerate(safe[pid]))

    pool: dict[int, list[int]] = defaultdict(list)
    for cand in game.players.values():
        if cand["id"] in squad_ids or not game.selectable(cand):
            continue
        pool[cand["element_type"]].append(cand["id"])
    for et in pool:
        pool[et].sort(key=discounted, reverse=True)
        pool[et] = pool[et][:CANDIDATES_PER_POS]

    bank10 = round(bank * 10)
    cost = {pid: game.players[pid]["now_cost"] for pid in set(squad_ids) | {c for v in pool.values() for c in v}}
    baseline = weekly_xi(squad_ids, game, safe)
    base_value = squad_value(squad_ids, game, safe, baseline)

    def value_of(squad: list[int]) -> float:
        return squad_value(squad, game, safe, baseline)

    def apply(squad: list[int], moves: list[tuple[int, int]]) -> tuple[list[int], int]:
        outs = {o for o, _ in moves}
        new = [pid for pid in squad if pid not in outs] + [i for _, i in moves]
        return new, bank10 - sum(cost[i] - cost[o] for o, i in moves)

    best_by_k: dict[int, tuple[float, list[tuple[int, int]]]] = {0: (base_value, [])}
    max_k = min(MAX_FREE_TRANSFERS, max(free_transfers, 0) + max_hits)

    if max_k >= 1:
        for out in squad_ids:
            for cand in pool[game.players[out]["element_type"]]:
                new, left = apply(squad_ids, [(out, cand)])
                if _legal(new, game, left):
                    v = value_of(new)
                    if v > best_by_k.get(1, (-1e9, []))[0]:
                        best_by_k[1] = (v, [(out, cand)])

    if max_k >= 2:
        shortlist = {}
        for out in squad_ids:
            et = game.players[out]["element_type"]
            ranked = pool[et][:PAIR_SHORTLIST]
            cheap = sorted(pool[et], key=lambda c: discounted(c) / max(cost[c], 1), reverse=True)[:4]
            shortlist[out] = list(dict.fromkeys(ranked + cheap))
        for o1, o2 in combinations(squad_ids, 2):
            for c1 in shortlist[o1]:
                for c2 in shortlist[o2]:
                    if c1 == c2:
                        continue
                    new, left = apply(squad_ids, [(o1, c1), (o2, c2)])
                    if _legal(new, game, left):
                        v = value_of(new)
                        if v > best_by_k.get(2, (-1e9, []))[0]:
                            best_by_k[2] = (v, [(o1, c1), (o2, c2)])

    for k in range(3, max_k + 1):
        if k - 1 not in best_by_k:
            break
        _, moves = best_by_k[k - 1]
        current, _ = apply(squad_ids, moves)
        used_out = {o for o, _ in moves}
        for out in squad_ids:
            if out in used_out:
                continue
            for cand in pool[game.players[out]["element_type"]]:
                if cand in current:
                    continue
                trial = moves + [(out, cand)]
                new, left = apply(squad_ids, trial)
                if _legal(new, game, left):
                    v = value_of(new)
                    if v > best_by_k.get(k, (-1e9, []))[0]:
                        best_by_k[k] = (v, trial)

    def score(k: int) -> float:
        value, _ = best_by_k[k]
        hits = max(0, k - free_transfers)
        saved = max(0, min(free_transfers, MAX_FREE_TRANSFERS - 1) - k)
        return value - hits * (HIT_COST + HIT_MARGIN) + saved * ROLL_VALUE

    chosen = max((k for k in best_by_k if k <= max_k), key=score)
    value, moves = best_by_k[chosen]
    hits = max(0, chosen - free_transfers)
    final_squad, left = apply(squad_ids, moves)
    for pid in final_squad:
        ensure(pid)

    lineup = _lineup_details(final_squad, game, plain, safe, gws[0] if gws else 0)
    before_weeks = weekly_xi(squad_ids, game, plain)
    after_weeks = weekly_xi(final_squad, game, plain)
    if hits and after_weeks:
        after_weeks[0] -= hits * HIT_COST
    impact = [{"gw": g, "before": round(b, 1), "after": round(a, 1)} for g, b, a in zip(gws, before_weeks, after_weeks)]

    reasoning = []
    if not moves:
        reasoning.append("No transfer adds enough over the coming weeks, so save your free transfer "
                         "(you'll have more flexibility next week).")
    else:
        worse = [r for r in impact if r["after"] < r["before"] - 0.5]
        better = [r for r in impact if r["after"] > r["before"] + 0.5]
        reasoning.append(f"Better than your current team in {len(better)} of the next {len(impact)} gameweeks"
                         + (", worse in none." if not worse else
                            f"; slightly worse in {', '.join('GW' + str(r['gw']) for r in worse)}."))
        total = sum(r["after"] - r["before"] for r in impact)
        reasoning.append(f"About {total:+.1f} expected points over GW{gws[0]}–{gws[-1]}"
                         + (f" after the -{hits * HIT_COST} hit." if hits else "."))
        if hits:
            reasoning.append(f"The hit is worth it: the gain beats -{hits * HIT_COST} by more than {HIT_MARGIN:.0f} "
                             "points even after shading down uncertain projections.")
        unused = free_transfers - len(moves)
        if unused > 0:
            reasoning.append(f"Keeps {unused} free transfer(s) for next week.")

    spent = {o: cost[i] - cost[o] for o, i in moves}
    transfers = [explain_transfer(game, o, i, plain, gws, cost, spent, moves) for o, i in moves]

    return GameweekPlan(
        gw=gws[0] if gws else 0,
        transfers=transfers,
        hits=hits,
        hit_cost=hits * HIT_COST,
        free_transfers_used=min(len(moves), free_transfers),
        bank_after=left / 10,
        formation=lineup["formation"],
        starting=lineup["starting"],
        bench=lineup["bench"],
        captain=lineup["captain"],
        vice=lineup["vice"],
        expected_points=round(lineup["expected"] - hits * HIT_COST, 1),
        expected_points_no_transfers=round(before_weeks[0], 1) if before_weeks else 0.0,
        horizon_gain=round(value - base_value - hits * HIT_COST, 1),
        reasoning=reasoning,
        final_squad=final_squad,
        impact=impact,
        captain_reason=lineup["captain_reason"],
    )


# ---------------------------------------------------------------------------
# Explanations
# ---------------------------------------------------------------------------
def fixture_text(game: Game, team: int, gw: int) -> str:
    fx = game.fixtures_for(team, gw)
    if not fx:
        return "blank"
    return "+".join(
        (game.team_short(f["opp"]) if f["home"] else game.team_short(f["opp"]).lower()) + f"({f['fdr']})" for f in fx
    )


def explain_transfer(game: Game, out_id: int, in_id: int, proj: dict[int, list[float]], gws: list[int],
                     cost: dict[int, int], spent: dict[int, int], moves: list[tuple[int, int]]) -> PlannedTransfer:
    po, pi = game.players[out_id], game.players[in_id]
    pos = POSITIONS[po["element_type"]]
    played = max(game.finished_gws, 1)

    # Why he goes
    out_r = []
    if po.get("news"):
        out_r.append(po["news"])
    elif availability(po) < 1:
        out_r.append(f"{int(availability(po) * 100)}% chance of playing")
    starts = po.get("starts") or 0
    if reliability(po, game) < 0.5:
        out_r.append(f"started only {starts} of {max(played, starts)} games")
    form_o = float(po.get("form") or 0)
    if form_o < 2.5 and game.finished_gws >= 4 and availability(po) == 1:
        out_r.append(f"form {form_o:.1f}")
    fdrs = [f["fdr"] for g in gws[:4] for f in game.fixtures_for(po["team"], g)]
    if fdrs and sum(fdrs) / len(fdrs) >= 3.5:
        out_r.append("tough fixtures: " + " ".join(fixture_text(game, po["team"], g) for g in gws[:4]))
    if gws and not game.fixtures_for(po["team"], gws[0]):
        out_r.append(f"no match in GW{gws[0]}")
    _, note_o = confidence(po, game)
    if "cool off" in note_o:
        out_r.append("his points have been running ahead of his underlying stats")
    if not out_r:
        out_r.append(f"projected only {sum(proj[out_id]):.1f} pts over GW{gws[0]}–{gws[-1]}")

    # Why his replacement comes in
    in_r = []
    form_i = float(pi.get("form") or 0)
    if form_i >= 4:
        in_r.append(f"form {form_i:.1f}")
    s_i = pi.get("starts") or 0
    if reliability(pi, game) >= 0.9:
        in_r.append(f"nailed: started {s_i} of {max(played, s_i)} games")
    xg, xa = pi.get("expected_goals_per_90"), pi.get("expected_assists_per_90")
    prior_g = game.priors.get((pi["element_type"], "expected_goals_per_90"))
    prior_a = game.priors.get((pi["element_type"], "expected_assists_per_90"))
    if xg is not None and prior_g and float(xg) >= 1.5 * prior_g and float(xg) >= 0.15:
        in_r.append(f"{float(xg):.2f} xG per 90 ({float(xg) / prior_g:.1f}x the average {pos.lower()})")
    if xa is not None and prior_a and float(xa) >= 1.5 * prior_a and float(xa) >= 0.12:
        in_r.append(f"{float(xa):.2f} xA per 90")
    if pos in ("GKP", "DEF") and pi.get("expected_goals_conceded_per_90") is not None:
        cs = math.exp(-game.shrunk(pi, "expected_goals_conceded_per_90"))
        if cs >= 0.28:
            in_r.append(f"{cs * 100:.0f}% clean-sheet chance per game")
    if pos in DC_THRESHOLD and pi.get("defensive_contribution_per_90") is not None:
        dc = _poisson_at_least(game.shrunk(pi, "defensive_contribution_per_90"), DC_THRESHOLD[pos])
        if dc >= 0.35:
            in_r.append(f"earns defensive-contribution points in ~{dc * 100:.0f}% of games")
    fdrs_i = [f["fdr"] for g in gws[:4] for f in game.fixtures_for(pi["team"], g)]
    if fdrs_i and sum(fdrs_i) / len(fdrs_i) <= 2.75:
        in_r.append("good fixtures: " + " ".join(fixture_text(game, pi["team"], g) for g in gws[:4]))
    dgw = [g for g in gws if len(game.fixtures_for(pi["team"], g)) >= 2]
    if dgw:
        in_r.append("double gameweek in GW" + ", GW".join(map(str, dgw)))
    conf, note_i = confidence(pi, game)
    cautions = []
    for part in filter(None, note_i.split("; ")):
        (cautions if ("cool off" in part or "small sample" in part) else in_r).append(part)
    if availability(pi) < 1:
        cautions.append(pi.get("news") or f"{int(availability(pi) * 100)}% chance of playing")
    sel = float(pi.get("selected_by_percent") or 0)
    if sel and sel < 5:
        in_r.append(f"differential: {sel:.1f}% owned")
    diff = cost[in_id] - cost[out_id]
    if diff < 0:
        others = [game.players[i]["web_name"] for o, i in moves if o != out_id and spent.get(o, 0) > 0]
        in_r.append(f"saves £{-diff / 10:.1f}m" + (f", which helps pay for {', '.join(others)}" if others else ""))

    weekly = [
        {"gw": g, "out": proj[out_id][k], "in": proj[in_id][k],
         "fixture_out": fixture_text(game, po["team"], g), "fixture_in": fixture_text(game, pi["team"], g)}
        for k, g in enumerate(gws)
    ]
    better = sum(1 for w in weekly if w["in"] > w["out"] + 0.2)
    worse = [w for w in weekly if w["in"] < w["out"] - 0.5]
    tot_in, tot_out = sum(proj[in_id]), sum(proj[out_id])
    summary = (f"Projected {tot_in:.1f} pts vs {tot_out:.1f} over GW{gws[0]}–{gws[-1]}, "
               f"better in {better} of {len(weekly)} weeks.")
    warning = ""
    if worse:
        warning = "Weaker in " + ", ".join(f"GW{w['gw']} ({w['fixture_in']})" for w in worse) + ", but ahead overall."

    return PlannedTransfer(
        out=po["web_name"], out_team=game.team_short(po["team"]), price_out=cost[out_id] / 10,
        into=pi["web_name"], into_team=game.team_short(pi["team"]), price_in=cost[in_id] / 10,
        position=pos, out_reasons=out_r, in_reasons=in_r, weekly=weekly,
        summary=summary, warning=warning, confidence=confidence_label(conf), cautions=cautions,
    )


def _lineup_details(squad: list[int], game: Game, plain: dict[int, list[float]],
                    safe: dict[int, list[float]], gw: int) -> dict:
    """Pick the XI and captain on confidence-adjusted points; show plain projections."""
    rows = [(pid, game.players[pid]["element_type"], safe[pid][0] if safe[pid] else 0.0) for pid in squad]
    _, (nd, nm, nf), _ = _best_xi([(et, x) for _, et, x in rows])
    by_pos: dict[int, list[tuple[int, int, float]]] = defaultdict(list)
    for r in rows:
        by_pos[r[1]].append(r)
    for v in by_pos.values():
        v.sort(key=lambda r: r[2], reverse=True)
    xi = by_pos[1][:1] + by_pos[2][:nd] + by_pos[3][:nm] + by_pos[4][:nf]
    xi_ids = {r[0] for r in xi}
    bench = [r for r in by_pos[1] if r[0] not in xi_ids] + sorted(
        (r for r in rows if r[0] not in xi_ids and r[1] != 1), key=lambda r: r[2], reverse=True)
    caps = sorted((r for r in xi if r[1] != 1), key=lambda r: r[2], reverse=True)

    def nxt(pid: int) -> float:
        return plain[pid][0] if plain[pid] else 0.0

    def info(r):
        pl = game.players[r[0]]
        conf, _ = confidence(pl, game)
        return {"name": pl["web_name"], "team": game.team_short(pl["team"]), "position": POSITIONS[r[1]],
                "xpts": round(nxt(r[0]), 1), "confidence": confidence_label(conf)}

    reason = ""
    if caps:
        cap = caps[0][0]
        cp = game.players[cap]
        conf, _ = confidence(cp, game)
        reason = (f"{cp['web_name']}: {nxt(cap):.1f} projected pts, {fixture_text(game, cp['team'], gw)}, "
                  f"{confidence_label(conf)} confidence")
        outfield = [r for r in xi if r[1] != 1]
        top_plain = max(outfield, key=lambda r: nxt(r[0]))[0]
        if top_plain != cap:
            other = game.players[top_plain]
            reason += (f"; chosen over {other['web_name']} ({nxt(top_plain):.1f}) because his points are "
                       "more reliable")
    return {
        "formation": f"{nd}-{nm}-{nf}",
        "starting": [info(r) for r in xi],
        "bench": [info(r) for r in bench],
        "captain": game.players[caps[0][0]]["web_name"] if caps else "",
        "vice": game.players[caps[1][0]]["web_name"] if len(caps) > 1 else "",
        "expected": round(sum(nxt(r[0]) for r in xi) + (nxt(caps[0][0]) if caps else 0), 1),
        "captain_reason": reason,
    }
