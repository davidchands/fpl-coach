"""Tiny ANSI colour helper (no dependencies).

Colour is on when writing to a terminal, off when piping to a file, and can be
forced either way with --color / --no-color or the NO_COLOR / FORCE_COLOR
environment variables (https://no-color.org).
"""

from __future__ import annotations

import os
import re
import sys

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

CODES = {
    "bold": "1", "dim": "2", "italic": "3", "underline": "4",
    "red": "31", "green": "32", "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36", "white": "37",
    "bred": "91", "bgreen": "92", "byellow": "93", "bblue": "94", "bmagenta": "95", "bcyan": "96",
}


def should_color(stream=None, force: bool | None = None) -> bool:
    if force is not None:
        return force
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    stream = stream or sys.stdout
    return hasattr(stream, "isatty") and stream.isatty()


def visible_len(s: str) -> int:
    return len(ANSI_RE.sub("", s))


def strip(s: str) -> str:
    return ANSI_RE.sub("", s)


class Style:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def __call__(self, text, *names: str) -> str:
        text = str(text)
        names = tuple(n for n in names if n)
        if not self.enabled or not names:
            return text
        return f"\x1b[{';'.join(CODES[n] for n in names)}m{text}\x1b[0m"

    # --- semantic helpers -------------------------------------------------
    def signed(self, value: float, fmt: str = "{:+.1f}", neutral: float = 0.05) -> str:
        """+ numbers green, - numbers red."""
        text = fmt.format(value)
        if value > neutral:
            return self(text, "bgreen")
        if value < -neutral:
            return self(text, "bred")
        return self(text, "dim")

    def per_gw(self, value: float, weeks: int = 1, fmt: str = "{:.1f}") -> str:
        """Colour a points value by how good it is per gameweek."""
        avg = value / max(weeks, 1)
        text = fmt.format(value)
        if avg >= 5.5:
            return self(text, "bgreen", "bold")
        if avg >= 4:
            return self(text, "green")
        if avg >= 2.5:
            return self(text)
        return self(text, "bred")

    def form(self, value: float) -> str:
        text = f"{value:.1f}"
        if value >= 5:
            return self(text, "bgreen")
        if value < 2.5:
            return self(text, "bred")
        return text

    def fdr(self, token: str) -> str:
        """Colour one fixture token like 'MCI(4)' by difficulty, FPL-website style."""
        m = re.search(r"\((\d)\)", token)
        if token == "-blank-" or token == "blank":
            return self(token, "magenta")
        if not m:
            return token
        d = int(m.group(1))
        return self(token, *{1: ("bgreen", "bold"), 2: ("bgreen",), 3: ("white",), 4: ("bred",), 5: ("red", "bold")}[d])

    def fixtures(self, text: str) -> str:
        return " ".join(self.fdr(tok) for tok in text.split(" "))

    def flag(self, text: str) -> str:
        if "unavailable" in text or "% to play" in text:
            return self(text, "bred", "bold")
        return self(text, "byellow")

    def confidence(self, label: str) -> str:
        return self(label, {"high": "bgreen", "medium": "byellow", "low": "bred"}.get(label, ""))
