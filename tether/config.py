"""Settings live in one place: the .env file in the project root.

    ANTHROPIC_API_KEY=      your key (never committed: .env is in .gitignore)
    TETHER_VAULT=           your Obsidian vault folder (the app can set this for you)
    TETHER_ZOTERO=          your Zotero data folder; empty means Zotero is NOT connected
    TETHER_ZOTERO_WRITE=    write permissions you granted: papers,pdfs,notes (empty = read only)
    ZOTERO_API_KEY=         zotero.org key, only needed for write-back
    TETHER_PROFILE=         balanced (default) or budget: which model does which job
    TETHER_ASK_ABOVE=       ask before an update estimated above this many dollars (default 0.50)
    TETHER_MONTHLY_CAP=     hard monthly spending limit in dollars (empty = no cap)
    TETHER_MODEL=           optional model overrides
    TETHER_JUDGE_MODEL=

.env is re-read on every lookup, so pasting your key while the app is running just works.
A real environment variable with a value still wins over .env (handy for one-off overrides).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
_SHELL_ENV = {k: v for k, v in os.environ.items() if v.strip()}     # snapshot before .env is applied

TEMPLATE = """# tether settings. This file is in .gitignore, so your key never gets committed.

# Paste your Anthropic API key after the = (no quotes needed)
ANTHROPIC_API_KEY=

# Your Obsidian vault folder, e.g. C:\\Users\\you\\Documents\\Obsidian\\Research
# (the app's settings panel fills this in for you)
TETHER_VAULT=

# Zotero data folder, only set if you connected Zotero in the app (empty = not connected)
TETHER_ZOTERO=

# Zotero write-back (only if you turned on "Read & write"): which permissions, and your
# zotero.org API key. Empty = tether never writes to Zotero.
TETHER_ZOTERO_WRITE=
ZOTERO_API_KEY=

# Cost profile: "balanced" (Sonnet reads/compares, Haiku checks) or "budget" (Haiku for everything).
# The app's Settings screen sets this.
TETHER_PROFILE=balanced

# Ask before an update estimated above this many dollars, and a hard monthly cap (empty = none).
TETHER_ASK_ABOVE=0.50
TETHER_MONTHLY_CAP=

# Optional: model overrides
TETHER_MODEL=claude-sonnet-5-5
TETHER_JUDGE_MODEL=claude-haiku-5-5
"""


def _file_values() -> dict[str, str]:
    if not ENV_FILE.exists():
        ENV_FILE.write_text(TEMPLATE, encoding="utf-8")
    return {k: (v or "").strip() for k, v in dotenv_values(ENV_FILE).items()}


def load_env() -> None:
    """Copy .env values into os.environ (for libraries that read the environment directly)."""
    for k, v in _file_values().items():
        if v and k not in _SHELL_ENV:
            os.environ[k] = v


def get(key: str, default: Optional[str] = None) -> Optional[str]:
    if key in _SHELL_ENV:
        return _SHELL_ENV[key].strip()
    return _file_values().get(key) or default


def api_key() -> Optional[str]:
    return get("ANTHROPIC_API_KEY")


def set_value(key: str, value: str) -> None:
    """Update one line of .env in place (keeps comments and the rest of the file)."""
    _file_values()                                 # creates the file from the template if missing
    text = ENV_FILE.read_text(encoding="utf-8")
    line = f"{key}={value}"
    pat = re.compile(rf"^{re.escape(key)}=.*$", re.M)
    text = pat.sub(lambda _: line, text, count=1) if pat.search(text) else text.rstrip("\n") + f"\n{line}\n"
    ENV_FILE.write_text(text, encoding="utf-8")
    if key not in _SHELL_ENV:
        os.environ[key] = value
