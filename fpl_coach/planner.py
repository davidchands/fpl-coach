"""Gameweek planner: which transfers to make, then who to start and captain.

A squad is valued by what it would actually score: for every upcoming gameweek,
the best legal starting XI plus the captain's points, with a small weight on the
bench (cover for injuries) and later gameweeks discounted because projections
further out are less certain.

The search tries every single transfer, every pair of transfers (so it can find
"downgrade X to afford upgrading Y" moves), then extends greedily up to the
number of transfers allowed. Hits are only taken when the gain clearly beats
the -4, and an unused free transfer is worth something because it rolls over.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from .analysis import HIT_COST, MAX_PER_CLUB, POSITIONS, Game, project

FORMATIONS = [(d, m, f) for d in range(3, 6) for m in range(2, 6) for f in range(1, 4) if d + m + f == 10]

DISCOUNT = 0.85         # weight of each gameweek relative to the one before
BENCH_WEIGHT = 0.1      # bench points matter a little (injury cover)
HIT_MARGIN = 2.0        # a hit must beat -4 by at least this much (projections are noisy)
ROLL_VALUE = 1.5        # value of saving a free transfer for next week
CANDIDATES_PER_POS = 25 # incoming players considered per position
PAIR_SHORTLIST = 10     # per outgoing player, candidates tried in two-transfer combinations
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


@dataclass
class GameweekPlan:
    gw: int
    transfers: list[PlannedTransfer]
    hits: int
    hit_cost: int
    free_transfers_used: int
    bank_after: float
    formation: str
    starting: list[dict]          # [{name, team, position, xpts}]
    bench: list[dict]
    captain: str
    vice: str
    expected_points: float        # next GW incl. captain, after transfers and hits
    expected_points_no_transfers: float
    horizon_gain: float           # discounted xPts gained over the horizon, after hits
    reasoning: list[str] = field(default_factory=list)
    final_squad: list[int] = field(default_factory=list)


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


def squad_value(squad: list[int], game: Game, proj: dict[int, list[float]]) -> float:
    n = len(next(iter(proj.values()))) if proj else 0
    value = 0.0
    for i in range(n):
        xi, _, bench = _best_xi([(game.players[pid]["element_type"], proj[pid][i]) for pid in squad])
        value += (DISCOUNT**i) * (xi + BENCH_WEIGHT * bench)
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
    proj: dict[int, list[float]] = {}

    def p(pid: int) -> list[float]:
        if pid not in proj:
            proj[pid] = project(game.players[pid], game, gws)
        return proj[pid]

    for pid in squad_ids:
        p(pid)

    def discounted(pid: int) -> float:
        return sum((DISCOUNT**i) * x for i, x in enumerate(p(pid)))

    # Shortlist of incoming players per position.
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
    base_value = squad_value(squad_ids, game, proj)

    def apply(squad: list[int], moves: list[tuple[int, int]]) -> tuple[list[int], int]:
        new = [pid for pid in squad if pid not in {o for o, _ in moves}] + [i for _, i in moves]
        spent = sum(cost[i] - cost[o] for o, i in moves)
        return new, bank10 - spent

    best_by_k: dict[int, tuple[float, list[tuple[int, int]]]] = {0: (base_value, [])}

    # k = 1: every single transfer.
    singles: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for out in squad_ids:
        et = game.players[out]["element_type"]
        for cand in pool[et]:
            new, left = apply(squad_ids, [(out, cand)])
            if not _legal(new, game, left):
                continue
            v = squad_value(new, game, proj)
            singles[out].append((v, cand))
            if v > best_by_k.get(1, (-1, []))[0]:
                best_by_k[1] = (v, [(out, cand)])

    max_k = min(MAX_FREE_TRANSFERS, max(free_transfers, 0) + max_hits)

    # k = 2: pairs of outgoing players; budget is shared, so ignore it when shortlisting.
    if max_k >= 2:
        shortlist = {}
        for out in squad_ids:
            et = game.players[out]["element_type"]
            ranked = sorted(pool[et], key=discounted, reverse=True)[:PAIR_SHORTLIST]
            cheap = sorted(pool[et], key=lambda c: discounted(c) / max(cost[c], 1), reverse=True)[:4]
            shortlist[out] = list(dict.fromkeys(ranked + cheap))
        for o1, o2 in combinations(squad_ids, 2):
            for c1 in shortlist[o1]:
                for c2 in shortlist[o2]:
                    if c1 == c2:
                        continue
                    new, left = apply(squad_ids, [(o1, c1), (o2, c2)])
                    if not _legal(new, game, left):
                        continue
                    v = squad_value(new, game, proj)
                    if v > best_by_k.get(2, (-1, []))[0]:
                        best_by_k[2] = (v, [(o1, c1), (o2, c2)])

    # k >= 3: extend the best plan greedily, one transfer at a time.
    for k in range(3, max_k + 1):
        if k - 1 not in best_by_k:
            break
        _, moves = best_by_k[k - 1]
        current, _ = apply(squad_ids, moves)
        used_out = {o for o, _ in moves}
        for out in squad_ids:
            if out in used_out:
                continue
            et = game.players[out]["element_type"]
            for cand in pool[et]:
                if cand in current:
                    continue
                trial = moves + [(out, cand)]
                new, left = apply(squad_ids, trial)
                if not _legal(new, game, left):
                    continue
                v = squad_value(new, game, proj)
                if v > best_by_k.get(k, (-1, []))[0]:
                    best_by_k[k] = (v, trial)

    # Choose how many transfers to make.
    def score(k: int) -> float:
        value, _ = best_by_k[k]
        hits = max(0, k - free_transfers)
        saved = max(0, min(free_transfers, MAX_FREE_TRANSFERS - 1) - k)  # FTs that roll over
        return value - hits * (HIT_COST + HIT_MARGIN) + saved * ROLL_VALUE

    options = [k for k in best_by_k if k <= max_k]
    chosen = max(options, key=score)
    value, moves = best_by_k[chosen]
    hits = max(0, chosen - free_transfers)
    final_squad, left = apply(squad_ids, moves)

    # Lineup for the next gameweek.
    lineup = _lineup_details(final_squad, game, proj)
    before = _best_xi([(game.players[pid]["element_type"], p(pid)[0]) for pid in squad_ids])[0] if gws else 0.0

    reasoning = []
    if not moves:
        reasoning.append("No transfer adds enough over the next few weeks, so save your free transfer.")
    else:
        gain = value - base_value
        reasoning.append(f"These {len(moves)} transfer(s) add about {gain:.1f} expected points over GW{gws[0]}–{gws[-1]}"
                         f" (weighted towards the next few weeks).")
        if hits:
            reasoning.append(f"Worth the -{hits * HIT_COST}: the gain beats the hit by more than {HIT_MARGIN:.0f} points.")
        unused = free_transfers - len(moves)
        if unused > 0:
            reasoning.append(f"Keeps {unused} free transfer(s) for next week.")

    return GameweekPlan(
        gw=gws[0] if gws else 0,
        transfers=[
            PlannedTransfer(
                out=game.players[o]["web_name"], out_team=game.team_short(game.players[o]["team"]),
                price_out=cost[o] / 10,
                into=game.players[i]["web_name"], into_team=game.team_short(game.players[i]["team"]),
                price_in=cost[i] / 10, position=POSITIONS[game.players[o]["element_type"]],
            )
            for o, i in moves
        ],
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
        expected_points_no_transfers=round(before, 1),
        horizon_gain=round(value - base_value - hits * HIT_COST, 1),
        reasoning=reasoning,
        final_squad=final_squad,
    )


def _lineup_details(squad: list[int], game: Game, proj: dict[int, list[float]]) -> dict:
    rows = [(pid, game.players[pid]["element_type"], proj[pid][0] if proj[pid] else 0.0) for pid in squad]
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

    def info(r):
        pl = game.players[r[0]]
        return {"name": pl["web_name"], "team": game.team_short(pl["team"]),
                "position": POSITIONS[r[1]], "xpts": round(r[2], 1)}

    return {
        "formation": f"{nd}-{nm}-{nf}",
        "starting": [info(r) for r in xi],
        "bench": [info(r) for r in bench],
        "captain": game.players[caps[0][0]]["web_name"] if caps else "",
        "vice": game.players[caps[1][0]]["web_name"] if len(caps) > 1 else "",
        "expected": round(sum(r[2] for r in xi) + (caps[0][2] if caps else 0), 1),
    }
