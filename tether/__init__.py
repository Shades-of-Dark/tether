"""tether: citation-checked literature graphs for Obsidian."""
__version__ = "0.8.0"

from .config import load_env as _load_env

_load_env()      # .env is read once, before any module looks up a setting
