"""Turn a report dict into readable terminal text."""

from __future__ import annotations


def table(headers: list[str], rows: list[list], align: str | None = None) -> str:
    cells = [[str(h) for h in headers]] + [[str(c) for c in r] for r in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]
    align = align or "l" * len(headers)

    def fmt(row):
        return "  ".join(c.rjust(w) if a == "r" else c.ljust(w) for c, w, a in zip(row, widths, align)).rstrip()

    lines = [fmt(cells[0]), "  ".join("-" * w for w in widths)]
    lines += [fmt(r) for r in cells[1:]]
    return "\n".join(lines)


def heading(text: str) -> str:
    return f"\n{text}\n{'=' * len(text)}"


def _rank(n) -> str:
    return f"{n:,}" if isinstance(n, int) else "-"


def render(report: dict) -> str:
    t = report["team"]
    out = []
    out.append(f"{t['name']}  ({t['manager']})")
    out.append(
        f"GW{t['current_gw']}: {t['gw_points']} pts | Total {t['overall_points']} | "
        f"Rank {_rank(t['overall_rank'])} | Bank £{t['bank']}m | Value £{t['team_value']}m | "
        f"Free transfers: {t['free_transfers']}"
    )
    gws = report["horizon"]
    if gws:
        out.append(f"Looking ahead at GW{gws[0]}–GW{gws[-1]} (model: {report.get('model', 'blend')}).")

    # Squad
    out.append(heading("Squad health"))
    rows = []
    for p in report["squad"]:
        role = " (C)" if p["captain"] else " (V)" if p["vice"] else ""
        rows.append(
            [
                p["position"],
                p["name"] + role + ("" if p["starting"] else " [bench]"),
                p["team"],
                f"{p['price']:.1f}",
                f"{p['form']:.1f}",
                f"{p['projected']:.1f}",
                p["fixtures"],
                ", ".join(p["flags"]),
            ]
        )
    out.append(table(["Pos", "Player", "Club", "£m", "Form", "xPts", "Fixtures (FDR)", "Flags"], rows, "llrrrrll"))
    out.append("Fixtures: CAPS = home, lower case = away. xPts = projected points over the horizon.")
    news = [f"  - {p['name']}: {p['news']}" for p in report["squad"] if p["news"]]
    if news:
        out.append("News:\n" + "\n".join(news))

    # Lineup
    lu = report["lineup"]
    if lu.get("formation"):
        out.append(heading(f"Best lineup for GW{gws[0] if gws else '?'}"))
        out.append(f"Formation {lu['formation']}: {', '.join(lu['starting'])}")
        out.append(f"Bench (in order): {', '.join(lu['bench'])}")
        caps = ", ".join(f"{n} ({x:.1f})" for n, x in lu["captain_options"])
        out.append(f"Captain: {lu['captain']}  |  Vice: {lu['vice']}  |  Top options: {caps}")
        if lu["changes_in"]:
            out.append(f"Changes vs your current XI: start {', '.join(lu['changes_in'])}; "
                       f"bench {', '.join(lu['changes_out'])}.")

    # Transfers
    out.append(heading("Transfer ideas"))
    if report["transfers"]:
        rows = [
            [
                tr["position"],
                f"{tr['out']} ({tr['out_team']}, {tr['price_out']:.1f})",
                f"{tr['into']} ({tr['into_team']}, {tr['price_in']:.1f})",
                f"+{tr['gain']:.1f}",
                f"{tr['net_gain']:+.1f}" + (" after -4" if tr["hit"] else ""),
            ]
            for tr in report["transfers"]
        ]
        out.append(table(["Pos", "Out", "In", "xPts gain", "Net"], rows, "lllrr"))
        out.append("Each row is a single transfer within your budget. Don't take a hit for less than ~+4.")
    else:
        out.append("No single transfer clearly improves your squad. Rolling the transfer is fine.")

    # Chips
    ch = report["chips"]
    out.append(heading("Chips"))
    next_gw = gws[0] if gws else 0
    now = [c for c in ch["available"] if c["from_gw"] <= next_gw]
    later = [c for c in ch["available"] if c["from_gw"] > next_gw]
    if now:
        out.append("Available now: " + ", ".join(f"{c['chip']} (until GW{c['until_gw']})" for c in now))
    else:
        out.append("No chips left in the current window.")
    if later:
        out.append("Later: " + ", ".join(f"{c['chip']} (GW{c['from_gw']}–{c['until_gw']})" for c in later))
    for gw, teams in ch["doubles"].items():
        out.append(f"Double GW{gw}: {', '.join(teams)}")
    for gw, teams in ch["blanks"].items():
        out.append(f"Blank GW{gw}: {', '.join(teams)}")
    for tip in ch["tips"]:
        out.append(f"  * {tip}")

    # Review
    rv = report.get("review")
    if rv:
        out.append(heading("Season review"))
        out.append(f"Points lost to hits:        {rv['points_lost_to_hits']}")
        out.append(f"Points left on your bench:  {rv['bench_points_wasted']}")
        out.append(
            f"Captaincy points missed:    {rv['captain_points_missed']} "
            f"(best pick in your XI {rv['captain_correct']}/{rv['captain_weeks']} weeks)"
        )
        if rv["avg_vs_average"] is not None:
            out.append(
                f"Vs the average manager:     {rv['avg_vs_average']:+} pts/GW, "
                f"above average in {rv['weeks_above_average']}/{rv['gameweeks']} weeks"
            )
        if rv["best_gw"]:
            out.append(f"Best / worst GW:            GW{rv['best_gw']['gw']} ({rv['best_gw']['points']}) / "
                       f"GW{rv['worst_gw']['gw']} ({rv['worst_gw']['points']})")
        worst = [r for r in rv["worst_captain_calls"] if r["lost"] > 0]
        if worst:
            out.append("Costliest captain calls:")
            for r in worst:
                out.append(f"  GW{r['gw']}: {r['captain']} ({r['captain_points']}) vs "
                           f"{r['best']} ({r['best_points']}), -{r['lost']} pts")
        trend = [f"GW{gw} {_rank(rk)}" for gw, rk in rv["rank_trend"]]
        if trend:
            out.append("Rank trend: " + " → ".join(trend))

    if report["notes"]:
        out.append(heading("Notes"))
        out += [f"- {n}" for n in report["notes"]]
    return "\n".join(out) + "\n"
