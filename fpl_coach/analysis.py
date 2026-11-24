"""Pure analysis functions. No network access here: everything works on plain
API-shaped dicts, which keeps it easy to test and reuse (CLI, web app, MCP...)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import product

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

# How much an opponent's FDR (1 = easiest, 5 = hardest) scales expected points.
DIFFICULTY_FACTOR = {1: 1.25, 2: 1.12, 3: 1.0, 4: 0.85, 5: 0.7}

CHIP_LABELS = {
    "wildcard": "Wildcard",
    "freehit": "Free Hit",
    "bboost": "Bench Boost",
    "3xc": "Triple Captain",
    "manager": "Assistant Manager",
}

MAX_PER_CLUB = 3
HIT_COST = 4


# ---------------------------------------------------------------------------
# Game context
# ---------------------------------------------------------------------------
class Game:
    """Static game state: players, teams, gameweeks and fixtures."""

    def __init__(self, bootstrap: dict, fixtures: list) -> None:
        self.players = {p["id"]: p for p in bootstrap["elements"]}
        self.teams = {t["id"]: t for t in bootstrap["teams"]}
        self.events = bootstrap["events"]
        self.chips = bootstrap.get("chips", [])
        self.current_gw = next((e["id"] for e in self.events if e.get("is_current")), 0)
        self.next_gw = next((e["id"] for e in self.events if e.get("is_next")), None)
        self.last_gw = max((e["id"] for e in self.events), default=38)
        self.finished_gws = sum(1 for e in self.events if e.get("finished"))
        self.average_score = {e["id"]: e.get("average_entry_score") for e in self.events}

        self._fixtures: dict[tuple[int, int], list[dict]] = defaultdict(list)
        for f in fixtures:
            gw = f.get("event")
            if gw is None:  # postponed / not yet scheduled
                continue
            self._fixtures[(f["team_h"], gw)].append(
                {"opp": f["team_a"], "home": True, "fdr": f.get("team_h_difficulty", 3)}
            )
            self._fixtures[(f["team_a"], gw)].append(
                {"opp": f["team_h"], "home": False, "fdr": f.get("team_a_difficulty", 3)}
            )

    def fixtures_for(self, team_id: int, gw: int) -> list[dict]:
        return self._fixtures.get((team_id, gw), [])

    def upcoming_gws(self, horizon: int) -> list[int]:
        start = self.next_gw or (self.current_gw + 1)
        return [gw for gw in range(start, start + horizon) if gw <= self.last_gw]

    def team_short(self, team_id: int) -> str:
        return self.teams.get(team_id, {}).get("short_name", "???")

    def fixture_string(self, team_id: int, gws: list[int]) -> str:
        parts = []
        for gw in gws:
            fx = self.fixtures_for(team_id, gw)
            if not fx:
                parts.append("-blank-")
                continue
            parts.append(
                "+".join(
                    f"{self.team_short(f['opp']).lower() if not f['home'] else self.team_short(f['opp'])}"
                    f"({f['fdr']})"
                    for f in fx
                )
            )
        return " ".join(parts)

    def selectable(self, p: dict) -> bool:
        return not p.get("removed") and p.get("can_select", True) and p.get("status") != "u"


# ---------------------------------------------------------------------------
# Projection model (deliberately simple and transparent)
# ---------------------------------------------------------------------------
def availability(p: dict) -> float:
    chance = p.get("chance_of_playing_next_round")
    if chance is not None:
        return chance / 100
    return {"a": 1.0, "d": 0.5}.get(p.get("status", "a"), 0.0)


def reliability(p: dict, game: Game) -> float:
    """Share of finished gameweeks the player started (falls back to minutes)."""
    played = max(game.finished_gws, 1)
    if p.get("starts") is not None:
        return min(1.0, p["starts"] / played)
    return min(1.0, p.get("minutes", 0) / (60 * played))


def base_rate(p: dict, game: Game) -> float:
    form = float(p.get("form") or 0)
    ppg = float(p.get("points_per_game") or 0)
    rate = 0.6 * form + 0.4 * ppg
    return rate * (0.4 + 0.6 * reliability(p, game))


def project(p: dict, game: Game, gws: list[int]) -> list[float]:
    """Expected points per upcoming gameweek.

    rate = (0.6*form + 0.4*points-per-game) scaled by how often the player starts,
    multiplied by a fixture-difficulty factor for each fixture (doubles count
    twice, blanks score 0) and by availability, which is assumed to recover
    over the following weeks for flagged players.
    """
    rate = base_rate(p, game)
    avail = availability(p)
    out = []
    for i, gw in enumerate(gws):
        a = 1 - (1 - avail) * (0.6**i)
        pts = sum(rate * DIFFICULTY_FACTOR.get(f["fdr"], 1.0) for f in game.fixtures_for(p["team"], gw))
        out.append(round(pts * a, 2))
    return out


# ---------------------------------------------------------------------------
# Squad health
# ---------------------------------------------------------------------------
@dataclass
class PlayerRow:
    id: int
    name: str
    team: str
    position: str
    price: float
    form: float
    total_points: int
    selected_by: float
    fixtures: str
    projected: float
    next_gw: float
    starting: bool
    captain: bool
    vice: bool
    flags: list[str] = field(default_factory=list)
    news: str = ""


def squad_health(game: Game, picks: list[dict], gws: list[int]) -> list[PlayerRow]:
    rows = []
    for pick in picks:
        p = game.players[pick["element"]]
        proj = project(p, game, gws)
        flags = []
        avail = availability(p)
        if avail == 0:
            flags.append("unavailable")
        elif avail < 1:
            flags.append(f"{int(avail * 100)}% to play")
        if reliability(p, game) < 0.5 and p["element_type"] != 1:
            flags.append("rarely starts")
        if gws and not game.fixtures_for(p["team"], gws[0]):
            flags.append("blank next GW")
        fdrs = [f["fdr"] for gw in gws[:3] for f in game.fixtures_for(p["team"], gw)]
        if fdrs and sum(fdrs) / len(fdrs) >= 4:
            flags.append("tough fixtures")
        if float(p.get("form") or 0) < 2 and avail > 0 and game.finished_gws >= 4:
            flags.append("poor form")
        rows.append(
            PlayerRow(
                id=p["id"],
                name=p["web_name"],
                team=game.team_short(p["team"]),
                position=POSITIONS[p["element_type"]],
                price=p["now_cost"] / 10,
                form=float(p.get("form") or 0),
                total_points=p.get("total_points", 0),
                selected_by=float(p.get("selected_by_percent") or 0),
                fixtures=game.fixture_string(p["team"], gws),
                projected=round(sum(proj), 1),
                next_gw=proj[0] if proj else 0.0,
                starting=pick["position"] <= 11,
                captain=bool(pick.get("is_captain")),
                vice=bool(pick.get("is_vice_captain")),
                flags=flags,
                news=p.get("news") or "",
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Best lineup and captaincy for next gameweek
# ---------------------------------------------------------------------------
FORMATION_LIMITS = {"GKP": (1, 1), "DEF": (3, 5), "MID": (2, 5), "FWD": (1, 3)}


def best_lineup(rows: list[PlayerRow]) -> dict:
    by_pos: dict[str, list[PlayerRow]] = defaultdict(list)
    for r in rows:
        by_pos[r.position].append(r)
    for pos in by_pos:
        by_pos[pos].sort(key=lambda r: r.next_gw, reverse=True)

    best = None
    lo, hi = FORMATION_LIMITS["DEF"], FORMATION_LIMITS["MID"]
    for d, m, f in product(range(lo[0], lo[1] + 1), range(hi[0], hi[1] + 1), range(1, 4)):
        if d + m + f != 10:
            continue
        if len(by_pos["DEF"]) < d or len(by_pos["MID"]) < m or len(by_pos["FWD"]) < f or not by_pos["GKP"]:
            continue
        xi = by_pos["GKP"][:1] + by_pos["DEF"][:d] + by_pos["MID"][:m] + by_pos["FWD"][:f]
        score = sum(r.next_gw for r in xi)
        if best is None or score > best[0]:
            best = (score, f"{d}-{m}-{f}", xi)
    if best is None:
        return {"formation": None, "starting": [], "bench": [], "captain": None, "vice": None, "expected": 0}

    score, formation, xi = best
    xi_ids = {r.id for r in xi}
    bench_outfield = sorted(
        (r for r in rows if r.id not in xi_ids and r.position != "GKP"), key=lambda r: r.next_gw, reverse=True
    )
    bench_gk = [r for r in rows if r.id not in xi_ids and r.position == "GKP"]
    caps = sorted((r for r in xi if r.position != "GKP"), key=lambda r: r.next_gw, reverse=True)
    current_xi = {r.id for r in rows if r.starting}
    return {
        "formation": formation,
        "starting": [r.name for r in xi],
        "bench": [r.name for r in bench_gk + bench_outfield],
        "captain": caps[0].name,
        "vice": caps[1].name if len(caps) > 1 else None,
        "captain_options": [(r.name, r.next_gw) for r in caps[:3]],
        "expected": round(score + caps[0].next_gw, 1),
        "changes_in": [r.name for r in xi if r.id not in current_xi],
        "changes_out": [r.name for r in rows if r.starting and r.id not in xi_ids],
    }


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------
@dataclass
class Transfer:
    out: str
    out_team: str
    into: str
    into_team: str
    position: str
    price_out: float
    price_in: float
    gain: float
    net_gain: float
    hit: bool


def estimate_free_transfers(history: dict, current_gw: int, max_bank: int = 5) -> int:
    """Estimate free transfers for the next deadline (rules since 2024/25: +1 per
    gameweek, up to 5 saved; in a Wildcard/Free Hit week saved transfers are kept but
    no new one is added).
    Real values can differ (e.g. special top-ups), so users can override this."""
    gws = {g["event"]: g for g in history.get("current", [])}
    chips = {c["event"]: c["name"] for c in history.get("chips", [])}
    if not gws:
        return 1
    start = min(gws)
    ft = 1
    for gw in range(start + 1, current_gw + 1):
        made = gws.get(gw, {}).get("event_transfers", 0)
        if chips.get(gw) in ("wildcard", "freehit"):
            continue
        ft = min(max_bank, max(ft - made, 0) + 1)
    return ft


def suggest_transfers(
    game: Game,
    squad_ids: list[int],
    bank: float,
    free_transfers: int,
    gws: list[int],
    limit: int = 5,
) -> list[Transfer]:
    proj_cache: dict[int, float] = {}

    def proj(pid: int) -> float:
        if pid not in proj_cache:
            proj_cache[pid] = sum(project(game.players[pid], game, gws))
        return proj_cache[pid]

    club_counts: dict[int, int] = defaultdict(int)
    for pid in squad_ids:
        club_counts[game.players[pid]["team"]] += 1
    squad = set(squad_ids)

    options = []  # (best gain, out player, ranked candidates)
    for pid in squad_ids:
        out = game.players[pid]
        budget = out["now_cost"] + round(bank * 10)
        cands = []
        for cand in game.players.values():
            if cand["id"] in squad or cand["element_type"] != out["element_type"]:
                continue
            if cand["now_cost"] > budget or not game.selectable(cand):
                continue
            count = club_counts[cand["team"]] - (1 if cand["team"] == out["team"] else 0)
            if count >= MAX_PER_CLUB:
                continue
            cands.append(cand)
        cands.sort(key=lambda c: proj(c["id"]), reverse=True)
        cands = cands[:5]
        if cands:
            options.append((proj(cands[0]["id"]) - proj(pid), out, cands))

    # Greedy: strongest upgrades first, each incoming player suggested only once.
    options.sort(key=lambda o: o[0], reverse=True)
    used: set[int] = set()
    suggestions = []
    for _, out, cands in options:
        best = next((c for c in cands if c["id"] not in used), None)
        if best is None:
            continue
        gain = proj(best["id"]) - proj(out["id"])
        if gain <= 0.5:
            continue
        used.add(best["id"])
        hit = free_transfers < 1
        suggestions.append(
            Transfer(
                out=out["web_name"],
                out_team=game.team_short(out["team"]),
                into=best["web_name"],
                into_team=game.team_short(best["team"]),
                position=POSITIONS[out["element_type"]],
                price_out=out["now_cost"] / 10,
                price_in=best["now_cost"] / 10,
                gain=round(gain, 1),
                net_gain=round(gain - (HIT_COST if hit else 0), 1),
                hit=hit,
            )
        )
    suggestions.sort(key=lambda t: t.gain, reverse=True)
    return suggestions[:limit]


# ---------------------------------------------------------------------------
# Season review
# ---------------------------------------------------------------------------
def season_review(game: Game, history: dict, picks_by_gw: dict[int, dict], live_by_gw: dict[int, dict]) -> dict:
    gws = history.get("current", [])
    chips = {c["event"]: c["name"] for c in history.get("chips", [])}
    hits = sum(g.get("event_transfers_cost", 0) for g in gws)
    bench = sum(g.get("points_on_bench", 0) for g in gws if chips.get(g["event"]) != "bboost")

    captain_lost = 0
    captain_rows = []
    for gw, picks in sorted(picks_by_gw.items()):
        live = live_by_gw.get(gw)
        if not live:
            continue
        stats = {e["id"]: e["stats"] for e in live.get("elements", [])}
        pts = lambda pid: stats.get(pid, {}).get("total_points", 0)  # noqa: E731
        mins = lambda pid: stats.get(pid, {}).get("minutes", 0)  # noqa: E731
        plist = picks.get("picks", [])
        cap = next((p for p in plist if p.get("is_captain")), None)
        vice = next((p for p in plist if p.get("is_vice_captain")), None)
        if not cap:
            continue
        effective = cap["element"]
        if mins(effective) == 0 and vice and mins(vice["element"]) > 0:
            effective = vice["element"]
        mult = 3 if picks.get("active_chip") == "3xc" else 2
        starters = [p["element"] for p in plist if p["position"] <= 11]
        best_id = max(starters, key=pts) if starters else effective
        lost = (pts(best_id) - pts(effective)) * (mult - 1)
        captain_lost += lost
        captain_rows.append(
            {
                "gw": gw,
                "captain": game.players.get(effective, {}).get("web_name", "?"),
                "captain_points": pts(effective),
                "best": game.players.get(best_id, {}).get("web_name", "?"),
                "best_points": pts(best_id),
                "lost": lost,
            }
        )

    vs_avg = [
        g["points"] - game.average_score[g["event"]]
        for g in gws
        if game.average_score.get(g["event"]) is not None
    ]
    best_gw = max(gws, key=lambda g: g["points"], default=None)
    worst_gw = min(gws, key=lambda g: g["points"], default=None)
    return {
        "gameweeks": len(vs_avg),
        "points_lost_to_hits": hits,
        "bench_points_wasted": bench,
        "captain_points_missed": captain_lost,
        "captain_correct": sum(1 for r in captain_rows if r["lost"] == 0),
        "captain_weeks": len(captain_rows),
        "worst_captain_calls": sorted(captain_rows, key=lambda r: r["lost"], reverse=True)[:3],
        "avg_vs_average": round(sum(vs_avg) / len(vs_avg), 1) if vs_avg else None,
        "weeks_above_average": sum(1 for v in vs_avg if v > 0),
        "best_gw": {"gw": best_gw["event"], "points": best_gw["points"]} if best_gw else None,
        "worst_gw": {"gw": worst_gw["event"], "points": worst_gw["points"]} if worst_gw else None,
        "rank_trend": [(g["event"], g.get("overall_rank")) for g in gws[-6:]],
    }


# ---------------------------------------------------------------------------
# Chips
# ---------------------------------------------------------------------------
def chip_status(game: Game, history: dict, rows: list[PlayerRow], horizon: int = 8) -> dict:
    used = history.get("chips", [])
    next_gw = game.next_gw or game.current_gw + 1

    available = []
    for window in game.chips:
        name = window["name"]
        start, stop = window.get("start_event", 1), window.get("stop_event", game.last_gw)
        if stop < next_gw:
            continue
        if any(c["name"] == name and start <= c["event"] <= stop for c in used):
            continue
        available.append({"chip": CHIP_LABELS.get(name, name), "until_gw": stop, "from_gw": start})
    available.sort(key=lambda c: (c["until_gw"], c["chip"]))

    doubles, blanks = {}, {}
    for gw in game.upcoming_gws(horizon):
        counts = {t: len(game.fixtures_for(t, gw)) for t in game.teams}
        d = [game.team_short(t) for t, n in counts.items() if n >= 2]
        b = [game.team_short(t) for t, n in counts.items() if n == 0]
        if d:
            doubles[gw] = d
        if b:
            blanks[gw] = b

    names = {c["chip"] for c in available if c["from_gw"] <= next_gw}
    tips = []
    for gw, teams in doubles.items():
        if len(teams) >= 4:
            if "Bench Boost" in names:
                tips.append(f"GW{gw} is a double for {len(teams)} teams: strong Bench Boost week.")
            if "Triple Captain" in names:
                tips.append(f"GW{gw}: consider Triple Captain on a premium with two fixtures.")
    for gw, teams in blanks.items():
        if len(teams) >= 4 and "Free Hit" in names:
            tips.append(f"GW{gw} is a blank for {len(teams)} teams: a Free Hit can rescue that week.")
    flagged = sum(1 for r in rows if r.flags)
    if flagged >= 5 and "Wildcard" in names:
        tips.append(f"{flagged} of your 15 players have problem flags: a Wildcard may beat taking hits.")
    expiring = [c for c in available if c["until_gw"] - next_gw <= 3 and c["from_gw"] <= next_gw]
    for c in expiring:
        tips.append(f"Your {c['chip']} expires after GW{c['until_gw']}. Use it or lose it.")

    return {"available": available, "doubles": doubles, "blanks": blanks, "tips": tips}
