"""A small, deterministic fake FPL season shaped like the real API responses."""

from __future__ import annotations

import random

from fpl_coach.api import FPLError

TEAMS = ["ARS", "CHE", "LIV", "MCI", "MUN", "TOT", "NEW", "AVL"]
CURRENT_GW = 10
LAST_GW = 20
DOUBLE_GW, DOUBLE_TEAMS = 13, [1, 2, 3, 4]
BLANK_GW, BLANK_TEAMS = 14, [5, 6, 7, 8]


def make_bootstrap() -> dict:
    rng = random.Random(42)
    teams = [{"id": i + 1, "name": n, "short_name": n} for i, n in enumerate(TEAMS)]
    elements, pid = [], 1
    for team in teams:
        for etype, n in ((1, 2), (2, 5), (3, 5), (4, 3)):
            for k in range(n):
                starter = k < {1: 1, 2: 4, 3: 4, 4: 2}[etype]
                form = round(rng.uniform(2, 8) if starter else rng.uniform(0, 2), 1)
                elements.append(
                    {
                        "id": pid,
                        "web_name": f"{team['short_name']}-{'GDMF'[etype - 1]}{k + 1}",
                        "team": team["id"],
                        "element_type": etype,
                        "now_cost": {1: 45, 2: 50, 3: 70, 4: 75}[etype] + rng.randint(0, 30),
                        "form": str(form),
                        "points_per_game": str(round(form * 0.9, 1)),
                        "total_points": int(form * 9),
                        "minutes": 900 if starter else 90,
                        "starts": 10 if starter else 1,
                        "selected_by_percent": "5.0",
                        "status": "a",
                        "chance_of_playing_next_round": None,
                        "news": "",
                        "removed": False,
                        "can_select": True,
                    }
                )
                pid += 1
    events = [
        {
            "id": gw,
            "finished": gw <= CURRENT_GW,
            "is_current": gw == CURRENT_GW,
            "is_next": gw == CURRENT_GW + 1,
            "average_entry_score": 50,
        }
        for gw in range(1, LAST_GW + 1)
    ]
    chips = [
        {"name": "wildcard", "start_event": 2, "stop_event": 10},
        {"name": "wildcard", "start_event": 11, "stop_event": 20},
        {"name": "freehit", "start_event": 2, "stop_event": 10},
        {"name": "freehit", "start_event": 11, "stop_event": 20},
        {"name": "bboost", "start_event": 1, "stop_event": 10},
        {"name": "bboost", "start_event": 11, "stop_event": 20},
        {"name": "3xc", "start_event": 1, "stop_event": 10},
        {"name": "3xc", "start_event": 11, "stop_event": 20},
    ]
    return {"elements": elements, "teams": teams, "events": events, "chips": chips}


def make_fixtures() -> list:
    fixtures = []
    ids = list(range(1, len(TEAMS) + 1))
    for gw in range(1, LAST_GW + 1):
        rot = ids[:1] + ids[1:][gw % 7:] + ids[1:][: gw % 7]
        pairs = [(rot[i], rot[-1 - i]) for i in range(len(rot) // 2)]
        for h, a in pairs:
            if gw == BLANK_GW and (h in BLANK_TEAMS or a in BLANK_TEAMS):
                continue
            fixtures.append({"event": gw, "team_h": h, "team_a": a,
                             "team_h_difficulty": 2 + (a % 3), "team_a_difficulty": 2 + (h % 3)})
        if gw == DOUBLE_GW:
            fixtures.append({"event": gw, "team_h": 1, "team_a": 2, "team_h_difficulty": 3, "team_a_difficulty": 3})
            fixtures.append({"event": gw, "team_h": 3, "team_a": 4, "team_h_difficulty": 3, "team_a_difficulty": 3})
    fixtures.append({"event": None, "team_h": 5, "team_a": 6, "team_h_difficulty": 3, "team_a_difficulty": 3})
    return fixtures


def squad_ids(bootstrap: dict) -> list[int]:
    """A legal 15: 2 GKP, 5 DEF, 5 MID, 3 FWD with max 3 per club, mixing starters and benchwarmers."""
    by = {}
    for p in bootstrap["elements"]:
        by.setdefault((p["team"], p["element_type"]), []).append(p["id"])
    picks = [by[(1, 1)][0], by[(2, 1)][1]]  # second GK is a backup who never plays
    picks += [by[(1, 2)][0], by[(2, 2)][0], by[(3, 2)][0], by[(4, 2)][0], by[(5, 2)][4]]
    picks += [by[(1, 3)][0], by[(3, 3)][0], by[(5, 3)][0], by[(6, 3)][0], by[(7, 3)][4]]
    picks += [by[(4, 4)][0], by[(6, 4)][0], by[(8, 4)][2]]
    return picks


class FakeClient:
    def __init__(self) -> None:
        self.b = make_bootstrap()
        self.f = make_fixtures()
        self.squad = squad_ids(self.b)
        self.calls = 0

    def bootstrap(self):
        return self.b

    def fixtures(self):
        return self.f

    def entry(self, team_id):
        if team_id != 123:
            raise FPLError("Not found")
        return {"name": "Test XI", "player_first_name": "Test", "player_last_name": "Manager",
                "summary_overall_points": 520, "summary_overall_rank": 1_234_567,
                "summary_event_points": 48, "last_deadline_bank": 15}

    def history(self, team_id):
        return {
            "current": [
                {"event": gw, "points": 40 + gw * 2, "event_transfers": 1 if gw % 3 == 0 else 0,
                 "event_transfers_cost": 4 if gw == 6 else 0, "points_on_bench": 5,
                 "overall_rank": 2_000_000 - gw * 50_000}
                for gw in range(1, CURRENT_GW + 1)
            ],
            "chips": [{"name": "wildcard", "event": 4}, {"name": "bboost", "event": 8}],
        }

    def lineup_order(self):
        s = self.squad  # [gk, gk, d*5, m*5, f*3]
        return [s[0], *s[2:6], *s[7:11], *s[12:14], s[1], s[6], s[11], s[14]]

    def picks(self, team_id, gw, finished=False):
        self.calls += 1
        order = self.lineup_order()
        return {
            "active_chip": "bboost" if gw == 8 else None,
            "entry_history": {"bank": 15, "value": 1003},
            "picks": [
                {"element": pid, "position": i + 1, "multiplier": 2 if i == 7 else 1,
                 "is_captain": i == 7, "is_vice_captain": i == 8}
                for i, pid in enumerate(order)
            ],
        }

    def live(self, gw, finished=False):
        self.calls += 1
        rng = random.Random(gw)
        return {"elements": [{"id": p["id"], "stats": {"total_points": rng.randint(0, 12), "minutes": 90}}
                             for p in self.b["elements"]]}
