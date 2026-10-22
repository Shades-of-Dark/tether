"""Exit 0 if every dependency in pyproject.toml is installed at a new-enough version, else 1.

Tether.bat runs this (in ~0.1s) and only calls pip when it says something is missing,
so launching doesn't reinstall anything when you've just updated tether's code.
Standard library only, and doesn't import the tether package.
"""
from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _requirements() -> list[str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.S | re.M)
    dev = re.search(r"^dev\s*=\s*\[(.*?)\]", text, re.S | re.M)
    reqs = []
    for m in (block, dev):
        if m:
            reqs += re.findall(r'"([^"]+)"', m.group(1))
    return reqs


def _ver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def missing() -> list[str]:
    out = []
    try:
        metadata.distribution("tether")
    except metadata.PackageNotFoundError:
        out.append("tether")
    for req in _requirements():
        m = re.match(r"([A-Za-z0-9_.\-]+)\s*(?:>=\s*([\d.]+))?", req)
        name, minimum = m.group(1), m.group(2)
        try:
            have = metadata.version(name)
        except metadata.PackageNotFoundError:
            out.append(req)
            continue
        if minimum and _ver(have) < _ver(minimum):
            out.append(f"{req} (have {have})")
    return out


if __name__ == "__main__":
    gone = missing()
    if gone:
        print("Needs installing: " + ", ".join(gone))
    sys.exit(1 if gone else 0)
