"""fpl-coach: open-source analysis for Fantasy Premier League teams."""

__version__ = "0.3.0"

from .api import FPLClient, FPLError  # noqa: E402
from .coach import analyze_team  # noqa: E402

__all__ = ["FPLClient", "FPLError", "analyze_team", "__version__"]
