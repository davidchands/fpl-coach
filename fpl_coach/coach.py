"""High-level entry point: fetch everything for a team ID and build one report.

`analyze_team` returns plain dicts/lists so the same report can feed the CLI,
JSON output, a web app or an MCP server.
"""

from __future__ import annotations

from dataclasses import asdict

from .analysis import (
    Game,
    best_lineup,
    chip_status,
    estimate_free_transfers,
    season_review,
    squad_health,
    suggest_transfers,
)
from .api import FPLClient, FPLError
from .chips import plan_chips
from .planner import plan_gameweek


def analyze_team(
    client: FPLClient,
    team_id: int,
    horizon: int = 5,
    free_transfers: int | None = None,
    bank: float | None = None,
    review: bool = True,
    model: str = "blend",
    max_hits: int = 1,
) -> dict:
    game = Game(client.bootstrap(), client.fixtures(), model=model)
    if game.current_gw == 0:
        raise FPLError("The season hasn't started yet, so there are no picks to analyse.")

    entry = client.entry(team_id)
    history = client.history(team_id)
    gw = game.current_gw
    picks = client.picks(team_id, gw)
    notes = []

    # After a Free Hit the squad reverts to the one from the week before.
    if picks.get("active_chip") == "freehit" and gw > 1:
        picks = client.picks(team_id, gw - 1, finished=True)
        notes.append(f"You played Free Hit in GW{gw}, so your squad from GW{gw - 1} is used.")

    if bank is None:
        bank = (picks.get("entry_history", {}).get("bank") or entry.get("last_deadline_bank") or 0) / 10
    if free_transfers is None:
        free_transfers = estimate_free_transfers(history, gw)
        notes.append("Free transfers are estimated from your history; pass --free-transfers to set them.")
    notes.append("Sell prices use current prices; your real sell price can be lower after price rises.")

    gws = game.upcoming_gws(horizon)
    rows = squad_health(game, picks["picks"], gws)
    squad_ids = [p["element"] for p in picks["picks"]]

    plan = plan_gameweek(game, squad_ids, bank, free_transfers, gws, max_hits=max_hits)

    report = {
        "team": {
            "id": team_id,
            "name": entry.get("name"),
            "manager": f"{entry.get('player_first_name', '')} {entry.get('player_last_name', '')}".strip(),
            "overall_points": entry.get("summary_overall_points"),
            "overall_rank": entry.get("summary_overall_rank"),
            "gw_points": entry.get("summary_event_points"),
            "current_gw": gw,
            "next_gw": game.next_gw,
            "bank": round(bank, 1),
            "team_value": (picks.get("entry_history", {}).get("value") or 0) / 10,
            "free_transfers": free_transfers,
        },
        "horizon": gws,
        "model": model,
        "squad": [asdict(r) for r in rows],
        "plan": asdict(plan),
        "chip_plan": asdict(plan_chips(game, history, plan.final_squad or squad_ids, plan.bank_after)),
        "lineup": best_lineup(rows),
        "transfers": [asdict(t) for t in suggest_transfers(game, squad_ids, bank, free_transfers, gws)],
        "chips": chip_status(game, history, rows),
        "notes": notes,
    }

    if review:
        finished = [g["event"] for g in history.get("current", []) if g["event"] <= game.finished_gws]
        picks_by_gw, live_by_gw = {}, {}
        for g in finished:
            try:
                picks_by_gw[g] = client.picks(team_id, g, finished=True)
                live_by_gw[g] = client.live(g, finished=True)
            except FPLError:
                continue
        report["review"] = season_review(game, history, picks_by_gw, live_by_gw)

    return report
