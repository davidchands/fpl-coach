"""Command-line interface: `fpl-coach analyze <team-id>`."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .api import FPLClient, FPLError
from .coach import analyze_team
from .report import render


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fpl-coach",
        description="Analyse a Fantasy Premier League team by its public team ID. No login needed.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    a = sub.add_parser("analyze", aliases=["analyse"], help="Full report for a team")
    a.add_argument("team_id", type=int, help="Your team ID (the number after /entry/ in the FPL URL)")
    a.add_argument("--horizon", type=int, default=5, help="Gameweeks to look ahead (default 5)")
    a.add_argument("--free-transfers", type=int, help="Your free transfers (estimated if omitted)")
    a.add_argument("--bank", type=float, help="Money in the bank in £m (read from FPL if omitted)")
    a.add_argument("--model", choices=["blend", "xg", "form"], default="blend",
                   help="Projection model: underlying stats + form (blend, default), xg only, or form only")
    a.add_argument("--no-review", action="store_true", help="Skip the season review (fewer API calls)")
    a.add_argument("--json", action="store_true", help="Print the report as JSON")
    a.add_argument("--no-cache", action="store_true", help="Always fetch fresh data")
    return parser


def main(argv: list[str] | None = None, client: FPLClient | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = client or FPLClient(use_cache=not args.no_cache)
    try:
        report = analyze_team(
            client,
            args.team_id,
            horizon=max(1, args.horizon),
            free_transfers=args.free_transfers,
            bank=args.bank,
            review=not args.no_review,
            model=args.model,
        )
    except FPLError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
