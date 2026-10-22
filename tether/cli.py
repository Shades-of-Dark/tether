"""tether: turn a pile of papers into a citation-checked Obsidian graph.

  tether app                                       open the app (same as Tether.bat)
  tether build papers/ --project "My Topic"
  tether build --topic "your search terms" -n 12 --project "My Topic"
  tether build paper.pdf https://arxiv.org/abs/XXXX.XXXXX --project "My Topic" --question "..."

Projects go in <your vault>/Tether/<project>. The API key and vault path are read from
.env (the app's Settings screen fills it in).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config


def _build(a) -> int:
    from .llm import ClaudeLLM, MissingKey
    from .pipeline import run

    vault = Path(a.vault) if a.vault else (Path(config.get("TETHER_VAULT")) if config.get("TETHER_VAULT") else None)
    if a.out:
        out = a.out
    elif vault and a.project:
        from .app import PROJECTS_FOLDER
        from .vault import safe
        out = vault / PROJECTS_FOLDER / safe(a.project, 60)
    elif vault:
        print("Name the project with --project.", file=sys.stderr)
        return 1
    else:
        print("No vault set. Run `tether app` and pick one in Settings, or pass --vault or --out.", file=sys.stderr)
        return 1

    pdfs = [Path(x) for x in a.inputs if not x.startswith(("http://", "https://"))]
    urls = [x for x in a.inputs if x.startswith(("http://", "https://"))]
    for p in pdfs:
        if not p.exists():
            print(f"Not found: {p}", file=sys.stderr)
            return 1
    if not (pdfs or urls or a.topic or (out / "Sources").exists()):
        print("Give PDFs, URLs, or --topic.", file=sys.stderr)
        return 1
    try:
        llm = ClaudeLLM(model=a.model, judge_model=a.judge_model, profile_name=a.profile)
    except MissingKey as e:
        print(e, file=sys.stderr)
        return 1
    zot = None
    if config.get("TETHER_ZOTERO"):
        from .zotero import ZoteroError, open_library
        try:
            zot = open_library(config.get("TETHER_ZOTERO"))
        except ZoteroError as e:
            print(f"(Zotero not used: {e})", file=sys.stderr)
    stats = run(llm, out=out, pdfs=pdfs, urls=urls, topic=a.topic, vault_root=vault if not a.out else None,
                question=a.question, n_topic=a.num, since=a.since, lookup=not a.offline,
                model_tag=f"{a.model}|{a.judge_model}", zotero=zot)
    print(f"\nDone: {stats['ideas']} central ideas from {stats['papers']} papers "
          f"({stats['claims']} key claims, {stats['contradictions']} with disagreement, {stats['rejected']} rejected).")
    from .llm import describe_usage
    print(f"Claude usage: {describe_usage(llm.usage)}")
    print(f"Notes are in {out.resolve()}. Open '00 Overview' in Obsidian.")
    return 0


def main(argv=None) -> int:
    from .llm import DEFAULT_MODEL, JUDGE_MODEL

    ap = argparse.ArgumentParser(prog="tether", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("app", help="open the drag-and-drop app in your browser")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--no-browser", action="store_true")

    b = sub.add_parser("build", help="build notes from PDFs, URLs and/or a topic")
    b.add_argument("inputs", nargs="*", help="PDF files, folders of PDFs, or URLs (arXiv, DOI, direct PDF)")
    b.add_argument("--project", help="project name (a folder in <vault>/Tether)")
    b.add_argument("--vault", help="Obsidian vault folder (default: TETHER_VAULT from .env)")
    b.add_argument("--out", type=Path, help="write to this exact folder instead of <vault>/<project>")
    b.add_argument("--topic", help="search OpenAlex for open-access papers on this topic")
    b.add_argument("-n", "--num", type=int, default=12, help="papers to pull for --topic (default 12)")
    b.add_argument("--since", type=int, help="only topic papers published from this year on")
    b.add_argument("--question", default="", help="your research question; steers the outline")
    b.add_argument("--model", default=DEFAULT_MODEL)
    b.add_argument("--judge-model", default=JUDGE_MODEL, help="the cheaper model (fact-checks)")
    b.add_argument("--profile", choices=["balanced", "budget"],
                   help="balanced: Sonnet reads and weaves ideas, Haiku fact-checks; budget: Haiku for everything (default: .env)")
    b.add_argument("--offline", action="store_true", help="skip OpenAlex metadata lookups for local PDFs")
    a = ap.parse_args(argv)

    if a.cmd == "app":
        from .app import serve
        serve(port=a.port, open_browser=not a.no_browser)
        return 0
    return _build(a)


if __name__ == "__main__":
    raise SystemExit(main())
