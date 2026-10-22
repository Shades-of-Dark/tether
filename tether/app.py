"""Local app. Runs only on your machine (127.0.0.1) and opens in your browser.

Layout on disk:
    <your Obsidian vault>/
        Tether/
            <project>/          one folder per project you create
                Sources/        the PDFs you dropped in
                Ideas/ Papers/ 00 Overview.md  Lit Review Outline.md ...

Your API key is saved to the .env file next to the code. The browser can set or
replace it, but the server never sends it back (only a masked hint like sk-ant-…3f9a).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import __version__, config
from .vault import safe

WEB = Path(__file__).parent / "web"
PROJECTS_FOLDER = "Tether"
PORT = 8765

app = FastAPI(title="tether", docs_url=None, redoc_url=None, openapi_url=None)


# ------------------------------------------------------------------ security
@app.middleware("http")
async def local_only(request: Request, call_next):
    """Block other websites from talking to this server through your browser.
    The Host must be localhost (stops DNS rebinding) and any Origin must be this app (stops
    a random page POSTing your files or a new key to 127.0.0.1)."""
    host = (request.headers.get("host") or "").split(":")[0]
    if host not in {"127.0.0.1", "localhost", "testserver"}:
        return JSONResponse({"detail": "Forbidden host."}, status_code=403)
    origin = request.headers.get("origin")
    if origin and urlparse(origin).hostname not in {"127.0.0.1", "localhost"}:
        return JSONResponse({"detail": "Forbidden origin."}, status_code=403)
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


# ------------------------------------------------------------------ helpers
def _vault() -> Optional[Path]:
    v = config.get("TETHER_VAULT")
    return Path(v).expanduser() if v else None


def _projects_root() -> Path:
    vault = _vault()
    if not vault or not vault.is_dir():
        raise HTTPException(400, "Choose your Obsidian vault first.")
    return vault / PROJECTS_FOLDER


def _project_dir(name: str, must_exist: bool = True) -> Path:
    clean = safe(name, 60)
    if not clean:
        raise HTTPException(400, "Give the project a name.")
    d = _projects_root() / clean
    if must_exist and not (d / ".tether").is_dir():
        raise HTTPException(404, f"No project called {clean!r}.")
    return d


def _zotero():
    """The connected library, or None. Nothing is read unless the user connected Zotero."""
    from .zotero import ZoteroError, open_library

    d = config.get("TETHER_ZOTERO")
    if not d:
        return None
    try:
        return open_library(d)
    except ZoteroError:
        return None


def _require_zotero():
    from .zotero import ZoteroError, open_library

    d = config.get("TETHER_ZOTERO")
    if not d:
        raise HTTPException(400, "Zotero isn't connected. Connect it in Settings first.")
    try:
        return open_library(d)
    except ZoteroError as e:
        raise HTTPException(400, str(e))


def _mask(key: Optional[str]) -> str:
    if not key:
        return ""
    return f"{key[:7]}…{key[-4:]}" if len(key) > 14 else "set"


def _obsidian_installed() -> Optional[bool]:
    if sys.platform == "win32":
        local, roaming = os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", "")
        return any(Path(p).exists() for p in (
            Path(local) / "Programs" / "Obsidian" / "Obsidian.exe", Path(roaming) / "obsidian"))
    if sys.platform == "darwin":
        return Path("/Applications/Obsidian.app").exists()
    return None


def _open_uri(uri: str) -> None:
    if sys.platform == "win32":
        os.startfile(uri)                                       # noqa: S606 (user-initiated, local)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", uri])
    else:
        subprocess.Popen(["xdg-open", uri])


def _project_summary(d: Path) -> dict:
    from .pipeline import PIPELINE_VERSION

    graph = d / ".tether" / "graph.json"
    src = d / "Sources"
    info = {"name": d.name, "sources": len(list(src.glob("*.pdf"))) if src.exists() else 0,
            "built": graph.exists(), "papers": 0, "claims": 0, "ideas": 0, "updated": None, "current": False}
    if graph.exists():
        try:
            g = json.loads(graph.read_text(encoding="utf-8"))
            info.update(papers=len(g["papers"]), claims=len(g["claims"]), ideas=len(g.get("ideas", [])),
                        updated=graph.stat().st_mtime, current=g.get("version") == PIPELINE_VERSION)
        except (OSError, json.JSONDecodeError, KeyError):
            pass
    q = d / ".tether" / "question.txt"
    info["question"] = q.read_text(encoding="utf-8") if q.exists() else ""
    a = d / ".tether" / "abstract_only.json"
    try:
        info["abstracts"] = [x["citation"]["title"] for x in json.loads(a.read_text(encoding="utf-8"))] if a.exists() else []
    except (OSError, json.JSONDecodeError, KeyError):
        info["abstracts"] = []
    return info


def _pick_folder(title: str) -> str:
    """Native folder picker in a child process, so it can't wedge the server."""
    code = ("import sys, tkinter as t, tkinter.filedialog as f; r=t.Tk(); r.withdraw(); "
            "r.attributes('-topmost', True); print(f.askdirectory(title=sys.argv[1]) or '')")
    try:
        out = subprocess.run([sys.executable, "-c", code, title], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        raise HTTPException(500, "Couldn't open a folder picker. Paste the path instead.")
    return out.stdout.strip()


# ------------------------------------------------------------------ state
@app.get("/", response_class=HTMLResponse)
def index():
    return (WEB / "index.html").read_text(encoding="utf-8").replace("__VERSION__", __version__)


@app.get("/api/state")
def state():
    vault = _vault()
    ok = bool(vault and vault.is_dir())
    projects = []
    root = vault / PROJECTS_FOLDER if ok else None
    if root and root.is_dir():
        for d in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if d.is_dir() and (d / ".tether").is_dir():
                projects.append(_project_summary(d))
    key = config.api_key()
    from .llm import PROFILE_INFO, ask_above, month_spent, monthly_cap, profile
    return {
        "version": __version__,
        "profile": profile(), "profiles": PROFILE_INFO,
        "spent_month": round(month_spent(), 2), "ask_above": ask_above(), "monthly_cap": monthly_cap(),
        "key_set": bool(key), "key_hint": _mask(key),
        "vault": str(vault) if vault else "", "vault_ok": ok,
        "vault_is_obsidian": ok and (vault / ".obsidian").is_dir(),
        "projects_folder": str(root) if root else "",
        "obsidian_installed": _obsidian_installed(), "projects": projects,
        "zotero_connected": bool(config.get("TETHER_ZOTERO")),
        "busy": builder.busy(),
    }


# ------------------------------------------------------------------ API key
class KeyIn(BaseModel):
    key: str


@app.post("/api/key")
def set_key(body: KeyIn):
    key = body.key.strip().strip('"').strip("'")
    if not key.startswith("sk-ant-") or len(key) < 30 or any(c.isspace() for c in key):
        raise HTTPException(400, "That doesn't look like an Anthropic key (they start with sk-ant-).")
    config.set_value("ANTHROPIC_API_KEY", key)
    return state()


@app.delete("/api/key")
def clear_key():
    config.set_value("ANTHROPIC_API_KEY", "")
    return state()


@app.post("/api/key/test")
def test_key():
    key = config.api_key()
    if not key:
        raise HTTPException(400, "No key saved yet.")
    try:
        import anthropic
        anthropic.Anthropic(api_key=key).models.list(limit=1)
    except Exception as e:
        name = type(e).__name__
        msg = {"AuthenticationError": "Anthropic rejected this key. Check it was copied fully.",
               "PermissionDeniedError": "This key doesn't have API access.",
               "APIConnectionError": "Couldn't reach Anthropic. Check your internet connection."}.get(name, f"{name}: {e}")
        raise HTTPException(400, msg)
    return {"ok": True}


class SpendingIn(BaseModel):
    ask_above: Optional[float] = None
    monthly_cap: Optional[float] = None       # 0 or None = no cap


@app.post("/api/spending")
def set_spending(body: SpendingIn):
    if body.ask_above is not None:
        if body.ask_above < 0:
            raise HTTPException(400, "The ask-above amount can't be negative.")
        config.set_value("TETHER_ASK_ABOVE", f"{body.ask_above:.2f}")
    if body.monthly_cap is not None:
        if body.monthly_cap < 0:
            raise HTTPException(400, "The monthly cap can't be negative.")
        config.set_value("TETHER_MONTHLY_CAP", f"{body.monthly_cap:.2f}" if body.monthly_cap else "")
    return state()


class ProfileIn(BaseModel):
    profile: str


@app.post("/api/profile")
def set_profile(body: ProfileIn):
    from .llm import PROFILES
    if body.profile not in PROFILES:
        raise HTTPException(400, f"Unknown profile {body.profile!r}.")
    config.set_value("TETHER_PROFILE", body.profile)
    return state()


# ------------------------------------------------------------------ vault
class VaultIn(BaseModel):
    path: str


@app.post("/api/vault")
def set_vault(body: VaultIn):
    p = Path(body.path.strip().strip('"')).expanduser()
    if not p.is_dir():
        raise HTTPException(400, f"That folder doesn't exist: {p}")
    config.set_value("TETHER_VAULT", str(p.resolve()))
    return state()


class NewVaultIn(BaseModel):
    parent: str
    name: str = "Obsidian Vault"


@app.post("/api/vault/create")
def create_vault(body: NewVaultIn):
    parent = Path(body.parent.strip().strip('"')).expanduser()
    name = safe(body.name, 60)
    if not parent.is_dir():
        raise HTTPException(400, f"That folder doesn't exist: {parent}")
    if not name:
        raise HTTPException(400, "Give the vault a name.")
    v = parent / name
    if v.exists() and any(v.iterdir()):
        raise HTTPException(400, f"{v} already exists and isn't empty. Choose it with 'Use an existing vault' instead.")
    v.mkdir(parents=True, exist_ok=True)
    config.set_value("TETHER_VAULT", str(v.resolve()))
    return state()


@app.post("/api/pick-folder")
def pick_folder(purpose: str = "vault"):
    title = "Choose your Obsidian vault" if purpose == "vault" else "Where should the new vault go?"
    return {"path": _pick_folder(title)}


# ------------------------------------------------------------------ Zotero (opt-in, read-only)
@app.get("/api/zotero")
def zotero_status():
    from .zotero import ZoteroError, default_dir, open_library

    d = config.get("TETHER_ZOTERO")
    detected = default_dir()
    from .zotero_sync import parse_perms

    info = {"connected": bool(d), "dir": d or "", "detected": str(detected) if detected else "",
            "ok": False, "error": "", "items": 0, "with_pdf": 0, "collections": 0,
            "write": sorted(parse_perms(config.get("TETHER_ZOTERO_WRITE"))) if d else [],
            "key_hint": _mask(config.get("ZOTERO_API_KEY")) if d else ""}
    if d:
        try:
            lib = open_library(d)
            info.update(ok=True, items=len(lib.items), with_pdf=sum(i.pdf is not None for i in lib.items.values()),
                        collections=len(lib.collections))
        except ZoteroError as e:
            info["error"] = str(e)
    return info


class ZoteroIn(BaseModel):
    dir: str


@app.post("/api/zotero/connect")
def zotero_connect(body: ZoteroIn):
    from .zotero import ZoteroError, ZoteroLibrary

    d = Path(body.dir.strip().strip('"')).expanduser()
    try:
        ZoteroLibrary(d)                              # validate before saving anything
    except ZoteroError as e:
        raise HTTPException(400, str(e))
    config.set_value("TETHER_ZOTERO", str(d.resolve()))
    return zotero_status()


@app.post("/api/zotero/disconnect")
def zotero_disconnect():
    """Off: forget the folder, the write permissions and the zotero.org key."""
    from . import zotero as z

    for k in ("TETHER_ZOTERO", "TETHER_ZOTERO_WRITE", "ZOTERO_API_KEY"):
        config.set_value(k, "")
    z._cache.clear()
    return zotero_status()


ZOTERO_TRANSPORT = None          # tests swap in a fake Zotero web API here


def _web(key: str):
    from .zotero_sync import ZoteroWeb
    return ZoteroWeb(key, transport=ZOTERO_TRANSPORT)


class ZWriteIn(BaseModel):
    perms: list[str] = []
    api_key: str = ""


@app.post("/api/zotero/write")
def zotero_write(body: ZWriteIn):
    """Grant (or revoke, with perms=[]) write-back. The key is checked with Zotero before saving."""
    from .zotero_sync import ZoteroWriteError, parse_perms

    if not config.get("TETHER_ZOTERO"):
        raise HTTPException(400, "Connect Zotero for reading first.")
    perms = parse_perms(",".join(body.perms))
    if not perms:
        config.set_value("TETHER_ZOTERO_WRITE", "")
        config.set_value("ZOTERO_API_KEY", "")
        return zotero_status()
    key = body.api_key.strip() or config.get("ZOTERO_API_KEY") or ""
    if not key:
        raise HTTPException(400, "Paste a zotero.org API key to allow writing.")
    try:
        me = _web(key).check(perms)
    except ZoteroWriteError as e:
        raise HTTPException(400, str(e))
    except httpx_error() as e:
        raise HTTPException(400, f"Couldn't reach zotero.org ({type(e).__name__}).")
    config.set_value("ZOTERO_API_KEY", key)
    config.set_value("TETHER_ZOTERO_WRITE", ",".join(sorted(perms)))
    return zotero_status() | {"username": me["username"]}


def httpx_error():
    import httpx
    return httpx.HTTPError


def _sync(project_dir: Path, log) -> Optional[dict]:
    from .zotero_sync import parse_perms, sync_project

    perms = parse_perms(config.get("TETHER_ZOTERO_WRITE"))
    key = config.get("ZOTERO_API_KEY")
    if not (perms and key and config.get("TETHER_ZOTERO")):
        return None
    log("Syncing to Zotero...")
    stats = sync_project(_web(key), project_dir, perms, local_lib=_zotero(), log=log)
    log(f"  Zotero: {stats['created']} added, {stats['linked']} filed into the collection, "
        f"{stats['pdfs']} PDFs uploaded, {stats['notes'] + stats['notes_updated']} claim notes written")
    return stats


@app.post("/api/zotero/pick-folder")
def zotero_pick():
    return {"path": _pick_folder("Choose your Zotero data folder (it contains zotero.sqlite)")}


@app.get("/api/zotero/library")
def zotero_library(collection: Optional[int] = None, q: str = "", project: str = ""):
    lib = _require_zotero()
    in_project: set[str] = set()
    if project:
        cache = _project_dir(project) / ".tether"
        f = cache / "known_citations.json"
        if f.exists():
            in_project |= {v.get("zotero_key") for v in json.loads(f.read_text(encoding="utf-8")).values()}
        f = cache / "abstract_only.json"
        if f.exists():
            in_project |= {a["citation"].get("zotero_key") for a in json.loads(f.read_text(encoding="utf-8"))}
    items = lib.search(collection, q)
    return {"collections": lib.collection_tree(), "total": len(lib.items), "count": len(items),
            "items": [it.summary() | {"in_project": it.key in in_project} for it in items[:500]],
            "truncated": len(items) > 500}


class ZImportIn(BaseModel):
    keys: list[str]


@app.post("/api/projects/{name}/zotero-import")
def zotero_import(name: str, body: ZImportIn):
    from .pipeline import import_from_zotero

    if not body.keys:
        raise HTTPException(400, "Select at least one paper.")
    lib = _require_zotero()
    res = import_from_zotero(lib, body.keys, _project_dir(name))
    if res["pdf"] or res["abstract"]:
        _changed(name)
    return project(name) | {"imported": res}


# ------------------------------------------------------------------ projects
class ProjectIn(BaseModel):
    name: str


@app.post("/api/projects")
def create_project(body: ProjectIn):
    d = _project_dir(body.name, must_exist=False)
    if (d / ".tether").is_dir():
        raise HTTPException(409, f"You already have a project called {d.name!r}.")
    (d / ".tether").mkdir(parents=True)
    (d / "Sources").mkdir(exist_ok=True)
    return project(d.name)


@app.get("/api/projects/{name}")
def project(name: str):
    d = _project_dir(name)
    src = d / "Sources"
    files = sorted(src.glob("*.pdf")) if src.exists() else []
    return _project_summary(d) | {"files": [{"name": f.name, "kb": round(f.stat().st_size / 1024)} for f in files]}


@app.post("/api/projects/{name}/upload")
async def upload(name: str, files: list[UploadFile] = File(...)):
    from .pipeline import add_to_sources

    d = _project_dir(name)
    tmp = Path(tempfile.mkdtemp(prefix="tether-"))
    skipped = []
    try:
        saved = []
        for f in files:
            fname = Path(f.filename or "paper.pdf").name
            data = await f.read()
            if not data.startswith(b"%PDF"):
                skipped.append(fname)
                continue
            (tmp / fname).write_bytes(data)
            saved.append(tmp / fname)
        add_to_sources(saved, d / "Sources")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if saved:
        _changed(d.name)
    return project(name) | {"skipped": skipped}


@app.delete("/api/projects/{name}/sources/{filename}")
def remove_source(name: str, filename: str):
    f = _project_dir(name) / "Sources" / Path(filename).name
    if f.suffix.lower() == ".pdf" and f.exists():
        f.unlink()
        _changed(name)
    return project(name)


class AbstractIn(BaseModel):
    title: str


@app.post("/api/projects/{name}/remove-abstract")
def remove_abstract_paper(name: str, body: AbstractIn):
    from .pipeline import remove_abstract

    remove_abstract(_project_dir(name), body.title)
    _changed(name)
    return project(name)


# ------------------------------------------------------------------ automatic rebuilds
def _has_content(out: Path) -> bool:
    if (out / "Sources").exists() and any((out / "Sources").glob("*.pdf")):
        return True
    f = out / ".tether" / "abstract_only.json"
    try:
        return f.exists() and bool(json.loads(f.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return False


def _rebuild(name: str, work: dict, log, stage) -> dict:
    """One rebuild of one project. Runs on the AutoBuilder's worker thread."""
    from .autobuild import UserFacing
    from .llm import ClaudeLLM
    from .pipeline import Reporter, fetch_remote, run

    out = _project_dir(name)
    vault = _vault()
    if not config.api_key():
        raise UserFacing("Add your Anthropic API key in Settings, then retry.")
    rep = Reporter(log, stage)
    if work.get("full"):
        log("Rebuilding from scratch: every paper will be read again.")
    if work["urls"] or work["topics"]:
        stage("Fetching papers", 0.01)
        got = fetch_remote(out, work["urls"], work["topics"], rep)
        log(f"Added {got['pdf']} PDF{'s' if got['pdf'] != 1 else ''}"
            + (f" and {got['abstract']} abstract-only paper{'s' if got['abstract'] != 1 else ''}" if got["abstract"] else ""))
    if not _has_content(out):
        log("No papers in this project yet.")
        return {"empty": True}
    llm = ClaudeLLM(on_retry=log)
    log(f"Models: {llm.profile} ({llm.model_for('read')} reads, {llm.model_for('check')} checks)")

    def approve(est: dict) -> bool:
        from .llm import ask_above, month_spent, monthly_cap
        cap, spent = monthly_cap(), month_spent()
        if cap and spent + est["cost"] > cap:
            raise UserFacing(f"This update would cost about ${est['cost']:.2f}, which would pass your monthly cap "
                             f"(${spent:.2f} of ${cap:.2f} used). Raise the cap in Settings → Models & cost, "
                             "or wait until next month.")
        return est["cost"] <= max(ask_above(), work.get("approved_up_to", 0.0))

    stats = run(llm, out=out, vault_root=vault, model_tag=f"{llm.model}|{llm.judge_model}",
                reporter=rep, zotero=_zotero(), approve=approve, fresh=bool(work.get("full")))
    from .llm import describe_usage
    stats["tokens"] = dict(llm.usage)
    stats["usage_text"] = describe_usage(llm.usage)
    log(f"Claude usage: {stats['usage_text']}")
    try:
        stage("Syncing to Zotero", 0.99)
        stats["zotero"] = _sync(out, log)
    except Exception as e:                       # the graph is built; report the sync problem separately
        log(f"! Zotero sync failed: {type(e).__name__}: {e}")
        stats["zotero_error"] = str(e)
    return stats


def _stale(out: Path) -> bool:
    """True if the graph is missing or older than anything it's built from: PDFs in Sources
    (including ones copied in by hand), abstract-only papers, or the research question."""
    from .pipeline import PIPELINE_VERSION

    graph = out / ".tether" / "graph.json"
    if not graph.exists():
        return True
    try:                                            # built by an older tether: rebuild in the new layout
        if json.loads(graph.read_text(encoding="utf-8")).get("version") != PIPELINE_VERSION:
            return True
    except (OSError, json.JSONDecodeError):
        return True
    built = graph.stat().st_mtime
    inputs = list((out / "Sources").glob("*.pdf")) if (out / "Sources").exists() else []
    inputs += [f for f in (out / ".tether" / "abstract_only.json", out / ".tether" / "question.txt") if f.exists()]
    if any(f.stat().st_mtime > built for f in inputs):
        return True
    try:                                            # a PDF was deleted by hand
        papers = json.loads(graph.read_text(encoding="utf-8"))["papers"]
    except (OSError, json.JSONDecodeError, KeyError):
        return True
    return any(p.get("path") and not Path(p["path"]).exists() for p in papers)


from .autobuild import AutoBuilder  # noqa: E402

def _describe(e):
    from .llm import friendly_error
    return friendly_error(e)


builder = AutoBuilder(_rebuild, describe=_describe)


def _changed(name: str, **kw) -> None:
    builder.request(safe(name, 60), **kw)


class AddIn(BaseModel):
    urls: list[str] = []
    topic: str = ""
    n: int = 12
    since: Optional[int] = None


@app.post("/api/projects/{name}/add")
def add_sources(name: str, body: AddIn):
    d = _project_dir(name)
    urls = [u.strip() for u in body.urls if u.strip()]
    topics = [(body.topic.strip(), max(1, min(body.n, 40)), body.since)] if body.topic.strip() else []
    if not (urls or topics):
        raise HTTPException(400, "Paste a link or enter search terms.")
    builder.request(d.name, urls=urls, topics=topics, delay=0)
    return {"ok": True}


class QuestionIn(BaseModel):
    question: str


@app.post("/api/projects/{name}/question")
def set_question(name: str, body: QuestionIn):
    d = _project_dir(name)
    q = d / ".tether" / "question.txt"
    new = body.question.strip()
    if (q.read_text(encoding="utf-8") if q.exists() else "") != new:
        q.write_text(new, encoding="utf-8")
        if _has_content(d):
            _changed(d.name)
    return {"ok": True}


class RebuildIn(BaseModel):
    full: bool = False


@app.post("/api/projects/{name}/rebuild")
def rebuild(name: str, body: Optional[RebuildIn] = None):
    """Update now (reuses everything already done), or full=True to re-read every paper."""
    d = _project_dir(name)
    if not _has_content(d):
        raise HTTPException(400, "This project has no papers yet.")
    builder.request(d.name, delay=0, full=bool(body and body.full))
    return {"ok": True}


@app.post("/api/projects/{name}/approve")
def approve_update(name: str):
    d = _project_dir(name)
    if builder.approve(d.name) is None:
        raise HTTPException(409, "Nothing is waiting for approval.")
    return {"ok": True}


@app.post("/api/projects/{name}/decline")
def decline_update(name: str):
    builder.decline(_project_dir(name).name)
    return {"ok": True}


@app.get("/api/projects/{name}/activity")
def activity(name: str, since: int = 0, run: int = -1):
    """Also the catch-up check: a project whose graph is out of date (papers added before
    auto-updates existed, PDFs copied into Sources by hand, a failed earlier run) starts
    updating on its own. Never after an error in this session, so a broken key can't loop."""
    d = _project_dir(name)
    st = builder.status(d.name, since, run)
    content = _has_content(d)
    if content and st["state"] in ("idle", "done") and config.api_key() and _stale(d):
        builder.request(d.name, delay=0)
        st = builder.status(d.name, since, run)
    return st | {"has_content": content}


@app.post("/api/projects/{name}/zotero-sync")
def zotero_sync(name: str):
    from .zotero_sync import ZoteroWriteError

    d = _project_dir(name)
    if builder.busy():
        raise HTTPException(409, "Wait for the current update to finish.")
    lines: list[str] = []
    try:
        stats = _sync(d, lines.append)
    except ZoteroWriteError as e:
        raise HTTPException(400, str(e))
    except httpx_error() as e:
        raise HTTPException(400, f"Couldn't reach zotero.org ({type(e).__name__}).")
    if stats is None:
        raise HTTPException(400, "Zotero write-back isn't turned on. Enable it in Settings → Zotero.")
    return {"stats": stats, "log": lines}


@app.post("/api/shutdown")
def shutdown():
    """Lets a newer launch replace this one (origin-checked like every other route)."""
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return {"ok": True}


class OpenIn(BaseModel):
    project: str
    target: str = "obsidian"      # obsidian | folder


@app.post("/api/open")
def open_thing(body: OpenIn):
    d = _project_dir(body.project)
    if body.target == "folder":
        _open_uri(str(d))
        return {"ok": True}
    overview = d / "00 Overview.md"
    if not overview.exists():
        raise HTTPException(400, "Build the project first.")
    _open_uri(f"obsidian://open?path={quote(str(overview.resolve()), safe='')}")
    return {"ok": True}


def _replace_running_instance(port: int) -> None:
    """If tether is already running on this port (say, an older version), shut it down so
    this launch serves the current code instead of the browser talking to a stale server."""
    import socket

    import httpx

    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) != 0:
            return                                            # port free
    base = f"http://127.0.0.1:{port}"
    try:
        running = httpx.get(f"{base}/api/state", timeout=3).json().get("version", "old")
    except Exception:
        raise SystemExit(f"Port {port} is in use by another program. Close it, or run: tether app --port 8766")
    print(f"Replacing the tether that was already running (version {running})...")
    try:
        httpx.post(f"{base}/api/shutdown", timeout=3)
    except Exception:
        pass
    for _ in range(30):
        time.sleep(0.2)
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return
    raise SystemExit("An older tether is still running and couldn't be stopped automatically.\n"
                     "Close its window (or end python.exe in Task Manager), then run Tether.bat again.")


def serve(port: int = PORT, open_browser: bool = True) -> None:
    import uvicorn

    _replace_running_instance(port)
    url = f"http://127.0.0.1:{port}"
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"tether is running at {url}  (only reachable from this computer; close this window to stop it)")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
