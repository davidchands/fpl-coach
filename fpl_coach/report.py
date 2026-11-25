"""Turn a report dict into readable terminal text, with colour when it helps.

Colour guide (when enabled):
  green = good (gains, easy fixtures, strong form)   red = bad (losses, hard fixtures, injuries)
  yellow = warnings                                   magenta = captaincy
"""

from __future__ import annotations

from .style import Style, visible_len

_plain = Style(False)


def table(headers: list[str], rows: list[list], align: str | None = None, s: Style = _plain) -> str:
    """Rows may contain coloured cells: widths are computed on visible characters."""
    cells = [[str(h) for h in headers]] + [[str(c) for c in r] for r in rows]
    widths = [max(visible_len(row[i]) for row in cells) for i in range(len(headers))]
    align = align or "l" * len(headers)

    def pad(c: str, w: int, a: str) -> str:
        gap = " " * (w - visible_len(c))
        return gap + c if a == "r" else c + gap

    def fmt(row):
        return "  ".join(pad(c, w, a) for c, w, a in zip(row, widths, align)).rstrip()

    head = "  ".join(pad(s(h, "bold"), w, a) for h, w, a in zip(cells[0], widths, align)).rstrip()
    lines = [head, s("  ".join("-" * w for w in widths), "dim")]
    lines += [fmt(r) for r in cells[1:]]
    return "\n".join(lines)


def _rank(n) -> str:
    return f"{n:,}" if isinstance(n, int) else "-"


def render(report: dict, color: bool = False) -> str:
    s = Style(color)

    def heading(text: str) -> str:
        return f"\n{s(text, 'bold', 'bcyan')}\n{s('=' * len(text), 'dim')}"

    def bullet(text: str, mark: str = "*", colour: str = "dim") -> str:
        return f"  {s(mark, colour)} {text}"

    t = report["team"]
    gws = report["horizon"]
    out = [s(f"{t['name']}", "bold") + s(f"  ({t['manager']})", "dim")]
    bank = f"£{t['bank']}m"
    out.append(
        f"GW{t['current_gw']}: {s(t['gw_points'], 'bold')} pts | Total {s(t['overall_points'], 'bold')} | "
        f"Rank {s(_rank(t['overall_rank']), 'bold')} | Bank {s(bank, 'bold')} | "
        f"Value £{t['team_value']}m | Free transfers: {s(t['free_transfers'], 'bold', 'bgreen')}"
    )
    if gws:
        out.append(s(f"Looking ahead at GW{gws[0]}–GW{gws[-1]} (model: {report.get('model', 'blend')}).", "dim"))

    # ------------------------------------------------------------------ squad
    out.append(heading("Squad health"))
    rows = []
    weeks = max(len(gws), 1)
    for p in report["squad"]:
        role = s(" (C)", "bmagenta", "bold") if p["captain"] else s(" (V)", "magenta") if p["vice"] else ""
        name = p["name"] + role + ("" if p["starting"] else s(" [bench]", "dim"))
        rows.append([
            s(p["position"], "dim"), name, p["team"], s(f"{p['price']:.1f}", "dim"),
            s.form(p["form"]), s.per_gw(p["projected"], weeks), s.fixtures(p["fixtures"]),
            ", ".join(s.flag(f) for f in p["flags"]),
        ])
    out.append(table(["Pos", "Player", "Club", "£m", "Form", "xPts", "Fixtures (difficulty)", "Flags"],
                     rows, "llrrrrll", s))
    out.append(s("Fixtures: CAPS = home, lower case = away; ", "dim") + s("easy", "bgreen") + s(" · ", "dim")
               + "average" + s(" · ", "dim") + s("hard", "bred") + s(". xPts = projected points over the horizon.", "dim"))
    news = [bullet(f"{s(p['name'], 'bold')}: {s(p['news'], 'byellow')}", "!", "byellow")
            for p in report["squad"] if p["news"]]
    if news:
        out.append("News:\n" + "\n".join(news))

    # ------------------------------------------------------------------- plan
    pl = report.get("plan")
    if pl and pl.get("formation"):
        out.append(heading(f"Your plan for GW{pl['gw']}"))
        if pl["transfers"]:
            out.append(s("1. Make these transfers:", "bold", "bblue"))
            for tr in pl["transfers"]:
                out.append("")
                out.append(
                    f"   {s('OUT', 'bred', 'bold')} {s(tr['out'], 'bold')} ({tr['out_team']}, £{tr['price_out']:.1f}m)"
                    f"  {s('->', 'dim')}  {s('IN', 'bgreen', 'bold')} {s(tr['into'], 'bold')} "
                    f"({tr['into_team']}, £{tr['price_in']:.1f}m)   confidence: {s.confidence(tr['confidence'])}")
                out.append(f"      {s('Why sell ' + tr['out'] + ':', 'bold')}")
                out += [f"        {s('✗', 'bred', 'bold')} {r}" for r in tr["out_reasons"]]
                out.append(f"      {s('Why buy ' + tr['into'] + ':', 'bold')}")
                out += [f"        {s('✓', 'bgreen', 'bold')} {r}" for r in tr["in_reasons"]]
                out += [f"        {s('⚠', 'byellow', 'bold')} {s(r, 'byellow')}" for r in tr.get("cautions", [])]
                weekly = "  ".join(
                    f"{s('GW' + str(w['gw']), 'dim')} {s.signed(w['in'] - w['out'])}" for w in tr["weekly"])
                out.append(f"      {s('Week by week:', 'bold')} {weekly}")
                out.append(f"      {s(tr['summary'], 'dim')}")
                if tr["warning"]:
                    out.append(f"      {s('! ' + tr['warning'], 'byellow')}")
            cost = s(f"-{pl['hit_cost']} hit", "bred", "bold") if pl["hits"] else s("no hit", "bgreen")
            out.append("")
            out.append(f"   Uses {pl['free_transfers_used']} free transfer(s), {cost}, "
                       f"£{pl['bank_after']:.1f}m left in the bank.")
        else:
            out.append(s("1. No transfers: save your free transfer.", "bold", "bblue"))

        out.append(s(f"2. Start this XI ({pl['formation']}):", "bold", "bblue"))
        by_pos: dict = {}
        for p in pl["starting"]:
            by_pos.setdefault(p["position"], []).append(p)
        for pos in ("GKP", "DEF", "MID", "FWD"):
            names = []
            for p in by_pos.get(pos, []):
                if p["name"] == pl["captain"]:
                    nm = s(p["name"] + " (C)", "bmagenta", "bold")
                elif p["name"] == pl["vice"]:
                    nm = s(p["name"] + " (V)", "magenta")
                else:
                    nm = p["name"]
                names.append(f"{nm} {s.per_gw(p['xpts'])}")
            out.append(f"     {s(pos, 'dim')}  " + s("  ·  ", "dim").join(names))
        out.append(f"   {s('Bench (in order):', 'dim')} " + ", ".join(p["name"] for p in pl["bench"]))
        out.append(s(f"3. Captain {pl['captain']}, vice {pl['vice']}.", "bold", "bblue"))
        if pl.get("captain_reason"):
            out.append(f"   {s(pl['captain_reason'], 'dim')}")
        delta = pl["expected_points"] - pl["expected_points_no_transfers"]
        exp = f"{pl['expected_points']:.1f}"
        out.append(f"{s('Expected GW' + str(pl['gw']) + ' points:', 'bold')} {s(exp, 'bgreen', 'bold')} "
                   f"({s.signed(delta)} vs your best XI without transfers, after hits).")

        if pl.get("impact") and pl["transfers"]:
            out.append("")
            out.append(s("How the plan changes each upcoming gameweek (best XI + captain):", "bold"))
            rows = [[s(f"GW{r['gw']}", "dim"), f"{r['before']:.1f}", f"{r['after']:.1f}",
                     s.signed(r["after"] - r["before"])] for r in pl["impact"]]
            out.append(table(["GW", "Now", "With plan", "Change"], rows, "lrrr", s))
        for r in pl["reasoning"]:
            out.append(bullet(r))

    # -------------------------------------------------------------- transfers
    out.append(heading("More transfer ideas"))
    if report["transfers"]:
        rows = [
            [
                s(tr["position"], "dim"),
                f"{tr['out']} ({tr['out_team']}, {tr['price_out']:.1f})",
                f"{tr['into']} ({tr['into_team']}, {tr['price_in']:.1f})",
                s.signed(tr["gain"]),
                s.signed(tr["net_gain"]) + (s(" after -4", "bred") if tr["hit"] else ""),
            ]
            for tr in report["transfers"]
        ]
        out.append(table(["Pos", "Out", "In", "xPts gain", "Net"], rows, "lllrr", s))
        out.append(s("Other single transfers worth considering, each on its own within your budget.", "dim"))
    else:
        out.append("No single transfer clearly improves your squad. Rolling the transfer is fine.")

    # ------------------------------------------------------------------ chips
    cp = report.get("chip_plan")
    out.append(heading("Chip plan"))
    if cp:
        out.append(s(cp["this_week"], "bold", "bgreen" if cp["this_week"].startswith("Play") else "white"))
        if cp["advice"]:
            rows = []
            for a in cp["advice"]:
                when = f"GW{a['gw']}" if a["gw"] else "hold"
                rows.append([s(a["label"], "bold"),
                             s(when, "bgreen", "bold") + (s("  <- this week", "bgreen") if a["play_now"] else ""),
                             s.signed(a["expected_gain"]), s(f"after GW{a['window_end']}", "dim")])
            out.append(table(["Chip", "Play in", "xPts gain", "Expires"], rows, "llrl", s))
            for a in cp["advice"]:
                out.append(bullet(f"{s(a['label'], 'bold')}: {a['why']}."))
        if cp["weekly"]:
            keys = ["bboost", "3xc"] + [k for k in ("freehit", "wildcard") if any(k in r for r in cp["weekly"])]
            names = {"bboost": "Bench Boost", "3xc": "Triple Captain", "freehit": "Free Hit", "wildcard": "Wildcard"}
            chosen = {a["chip"]: a["gw"] for a in cp["advice"]}
            rows = []
            for r in cp["weekly"]:
                row = [s(f"GW{r['gw']}", "dim"), f"{r['your_xi']:.1f}"]
                for k in keys:
                    v = r.get(k, 0.0)
                    cell = f"+{v:.1f}" + (f" {r.get('3xc_player', '')}" if k == "3xc" else "")
                    row.append(s(cell + " <", "bgreen", "bold") if chosen.get(k) == r["gw"] else cell)
                rows.append(row)
            out.append("")
            out.append(s("Expected gain from each chip, week by week ", "bold")
                       + s("(", "dim") + s("< = chosen week", "bgreen", "bold") + s("):", "dim"))
            out.append(table(["GW", "Your XI"] + [names[k] for k in keys], rows, "lr" + "r" * len(keys), s))
        for gw, teams in cp["doubles"].items():
            out.append(s(f"Double GW{gw}: ", "bgreen", "bold") + ", ".join(teams))
        for gw, teams in cp["blanks"].items():
            out.append(s(f"Blank GW{gw}: ", "bred", "bold") + ", ".join(teams))
        for n in cp["notes"]:
            out.append(bullet(s(n, "dim")))

    # ----------------------------------------------------------------- review
    rv = report.get("review")
    if rv:
        out.append(heading("Season review"))
        out.append(f"Points lost to hits:        {s(rv['points_lost_to_hits'], 'bred' if rv['points_lost_to_hits'] else 'bgreen', 'bold')}")
        out.append(f"Points left on your bench:  {s(rv['bench_points_wasted'], 'byellow', 'bold')}")
        out.append(
            f"Captaincy points missed:    {s(rv['captain_points_missed'], 'bred', 'bold')} "
            f"(best pick in your XI {rv['captain_correct']}/{rv['captain_weeks']} weeks)"
        )
        if rv["avg_vs_average"] is not None:
            out.append(
                f"Vs the average manager:     {s.signed(rv['avg_vs_average'])} pts/GW, "
                f"above average in {rv['weeks_above_average']}/{rv['gameweeks']} weeks"
            )
        if rv["best_gw"]:
            out.append(f"Best / worst GW:            {s('GW' + str(rv['best_gw']['gw']), 'bgreen')} "
                       f"({rv['best_gw']['points']}) / {s('GW' + str(rv['worst_gw']['gw']), 'bred')} "
                       f"({rv['worst_gw']['points']})")
        worst = [r for r in rv["worst_captain_calls"] if r["lost"] > 0]
        if worst:
            out.append("Costliest captain calls:")
            for r in worst:
                lost = f"-{r['lost']} pts"
                out.append(f"  GW{r['gw']}: {r['captain']} ({r['captain_points']}) vs "
                           f"{r['best']} ({r['best_points']}), {s(lost, 'bred')}")
        trend = [f"GW{gw} {_rank(rk)}" for gw, rk in rv["rank_trend"]]
        if trend:
            out.append("Rank trend: " + " → ".join(trend))

    if report["notes"]:
        out.append(heading("Notes"))
        out += [s(f"- {n}", "dim") for n in report["notes"]]
    return "\n".join(out) + "\n"
