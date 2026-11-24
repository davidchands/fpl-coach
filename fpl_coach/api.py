"""Thin, cached, read-only client for the public Fantasy Premier League API.

Only public endpoints are used: no login, no cookies, no passwords.
The API is unofficial and undocumented, so field names can change between seasons.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL = "https://fantasy.premierleague.com/api/"
USER_AGENT = "fpl-coach (+https://github.com/davidchands/fpl-coach)"

# Data for finished gameweeks never changes, so it can be cached for a long time.
LONG_TTL = 7 * 24 * 3600


class FPLError(Exception):
    """Raised when the FPL API can't be reached or returns an error."""


def default_cache_dir() -> Path:
    root = os.environ.get("XDG_CACHE_HOME") or os.path.join(Path.home(), ".cache")
    return Path(root) / "fpl-coach"


class FPLClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        cache_dir: Path | str | None = None,
        ttl: int = 600,
        timeout: int = 20,
        use_cache: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.cache_dir = Path(cache_dir) if cache_dir else default_cache_dir()
        self.ttl = ttl
        self.timeout = timeout
        self.use_cache = use_cache and not os.environ.get("FPL_COACH_NO_CACHE")

    # -- plumbing ---------------------------------------------------------
    def _cache_path(self, path: str) -> Path:
        key = hashlib.sha1(path.encode()).hexdigest()[:16]
        return self.cache_dir / f"{key}.json"

    def get(self, path: str, ttl: int | None = None) -> Any:
        ttl = self.ttl if ttl is None else ttl
        cache_file = self._cache_path(path)
        if self.use_cache and cache_file.exists():
            if time.time() - cache_file.stat().st_mtime < ttl:
                try:
                    return json.loads(cache_file.read_text())
                except (OSError, json.JSONDecodeError):
                    pass

        url = self.base_url + path
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise FPLError(f"Not found: {path} (is the team ID right?)") from e
            if e.code == 503:
                raise FPLError(
                    "The FPL API is unavailable. It usually is while the game "
                    "updates between gameweeks; try again later."
                ) from e
            raise FPLError(f"FPL API returned HTTP {e.code} for {path}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise FPLError(f"Could not reach the FPL API: {e}") from e

        if self.use_cache:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(json.dumps(data))
            except OSError:
                pass
        return data

    # -- endpoints --------------------------------------------------------
    def bootstrap(self) -> dict:
        return self.get("bootstrap-static/")

    def fixtures(self) -> list:
        return self.get("fixtures/")

    def entry(self, team_id: int) -> dict:
        return self.get(f"entry/{team_id}/")

    def history(self, team_id: int) -> dict:
        return self.get(f"entry/{team_id}/history/")

    def picks(self, team_id: int, gw: int, finished: bool = False) -> dict:
        return self.get(f"entry/{team_id}/event/{gw}/picks/", ttl=LONG_TTL if finished else None)

    def live(self, gw: int, finished: bool = False) -> dict:
        return self.get(f"event/{gw}/live/", ttl=LONG_TTL if finished else None)
