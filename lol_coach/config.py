"""Load config.toml. Local, gitignored, contains the Riot API key."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config.toml"
# Optional override of the default location (tests, alternate setups).
CONFIG_ENV_VAR = "LOL_COACH_CONFIG"


def load_config(path: Path | None = None) -> dict:
    """Precedence: explicit `path` > $LOL_COACH_CONFIG (if set) > config.toml."""
    if path:
        path = Path(path)
    elif os.environ.get(CONFIG_ENV_VAR):
        path = Path(os.environ[CONFIG_ENV_VAR])
    else:
        path = DEFAULT_CONFIG
    if not path.exists():
        raise FileNotFoundError(
            f"config.toml not found at {path}. "
            f"Copy config.example.toml to config.toml and fill it in."
        )
    with open(path, "rb") as f:
        return tomllib.load(f)
