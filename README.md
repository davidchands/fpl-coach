# fpl-coach ⚽

**Free, open-source analysis for your Fantasy Premier League team, from just your team ID.**

No login, no password, no account linking. `fpl-coach` reads the public FPL API and tells you:

- **Squad health**: form, fixtures for the next N gameweeks, projected points, and flags for injuries, rotation risk, blanks, tough runs and poor form
- **Best lineup**: the highest-projected starting XI, bench order and captain/vice for the next gameweek
- **Transfer ideas**: the best single transfers within your budget and the 3-per-club rule, with the -4 hit accounted for
- **Chips**: which chips you still have, upcoming double and blank gameweeks, and when to play them
- **Season review**: points lost to hits, points wasted on your bench, captaincy points missed, and how you compare with the average manager

## Install

```bash
pip install git+https://github.com/davidchands/fpl-coach.git
```

Requires Python 3.9+. No other dependencies.

## Use

Find your team ID: log in at [fantasy.premierleague.com](https://fantasy.premierleague.com), open **Points**, and take the number after `/entry/` in the URL.

```bash
fpl-coach analyze 1234567
```

Options:

| Flag | What it does |
|---|---|
| `--horizon 6` | Gameweeks to look ahead (default 5) |
| `--free-transfers 2` | Your free transfers (estimated from your history if omitted) |
| `--bank 1.5` | Money in the bank in £m (read from FPL if omitted) |
| `--no-review` | Skip the season review (much fewer API calls) |
| `--json` | Machine-readable output, to build your own tools on |
| `--no-cache` | Ignore the local cache and fetch fresh data |

Example output (from the test data):

```
Transfer ideas
==============
Pos  Out                In                 xPts gain    Net
---  -----------------  -----------------  ---------  -----
MID  NEW-M5 (NEW, 8.4)  CHE-M1 (CHE, 8.2)      +32.9  +32.9
DEF  MUN-D5 (MUN, 5.0)  MCI-D2 (MCI, 6.0)      +32.8  +32.8

Chips
=====
Available: Bench Boost (GW11–20), Free Hit (GW11–20), Triple Captain (GW11–20), Wildcard (GW11–20)
Double GW13: ARS, CHE, LIV, MCI
  * GW13 is a double for 4 teams: strong Bench Boost week.
```

## Use it as a library

```python
from fpl_coach import FPLClient, analyze_team

report = analyze_team(FPLClient(), 1234567, horizon=5)
print(report["lineup"]["captain"], report["transfers"][:3])
```

`analyze_team` returns plain dicts, so it's easy to put a web app, Discord bot or MCP server on top.

## How the projections work

The model is intentionally simple and transparent, so it's easy to read and to improve:

```
rate   = (0.6 × form + 0.4 × points per game) × (0.4 + 0.6 × share of games started)
xPts   = Σ over fixtures: rate × fixture factor(FDR)       # doubles count twice, blanks score 0
factor = FDR 1: 1.25 · 2: 1.12 · 3: 1.00 · 4: 0.85 · 5: 0.70
× availability (chance of playing), assumed to recover over the following weeks
```

It's a decent baseline, not an oracle. Better models (expected goals and assists, minutes prediction, team strength) are the most valuable contributions. See the roadmap.

## Limitations

- The FPL API is **unofficial** and can change without notice.
- **Sell prices**: your real sell price (after price rises) needs a login, so current prices are used.
- **Free transfers** are estimated using the current rules (+1 per week, up to 5 saved, kept through Wildcard and Free Hit). Pass `--free-transfers` if the estimate is off.
- Suggestions are single transfers; combined two-transfer moves aren't searched yet.

## Roadmap

- [ ] Better projections: xG, xA and minutes models, team attack/defence strength
- [ ] Two-transfer and wildcard squad optimiser (ILP)
- [ ] Mini-league comparison and effective ownership
- [ ] Web app (paste your ID, get a report)
- [ ] MCP server, so Claude and other assistants can analyse your team

## Contributing

PRs welcome!

```bash
git clone https://github.com/davidchands/fpl-coach.git && cd fpl-coach
pip install -e ".[dev]"
pytest
```

The tests use a fake season (`tests/fake_data.py`) and never call the real API. Analysis code in `fpl_coach/analysis.py` is pure functions; network code lives only in `fpl_coach/api.py`.

## License

MIT. Not affiliated with the Premier League or Fantasy Premier League.
