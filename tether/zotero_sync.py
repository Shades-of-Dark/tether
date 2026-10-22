"""Optional write-back to Zotero through the official Zotero Web API.

Never touches zotero.sqlite (writing it directly can corrupt your library). Changes reach
your desktop Zotero through Zotero's normal sync.

What each permission allows, and nothing more:
    papers  create the collection "Tether / <project>", create items for papers that aren't
            in your library yet, and add existing items to that collection
    pdfs    upload the PDF of each item tether created (uses your Zotero storage quota)
    notes   add one child note per paper with tether's verified claims, and update only
            notes that tether itself created

tether never deletes anything, and never edits an item's fields, your notes, or your tags.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path
from typing import Callable, Optional

import httpx

API = "https://api.zotero.org"
PERMS = ("papers", "pdfs", "notes")
ROOT_COLLECTION = "Tether"


class ZoteroWriteError(RuntimeError):
    pass


def parse_perms(s: Optional[str]) -> set[str]:
    return {p for p in (s or "").replace(" ", "").split(",") if p in PERMS}


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


class ZoteroWeb:
    def __init__(self, api_key: str, transport: Optional[httpx.BaseTransport] = None):
        self._transport = transport
        self.client = httpx.Client(base_url=API, timeout=60, transport=transport,
                                   headers={"Zotero-API-Key": api_key, "Zotero-API-Version": "3"})
        self._me: Optional[dict] = None

    # ------------------------------------------------------------ key / access
    def whoami(self) -> dict:
        if self._me is None:
            r = self.client.get("/keys/current")
            if r.status_code in (403, 404):
                raise ZoteroWriteError("Zotero rejected this API key. Check it was copied fully.")
            r.raise_for_status()
            d = r.json()
            user = (d.get("access") or {}).get("user") or {}
            self._me = {"user_id": d.get("userID"), "username": d.get("username", ""),
                        "library": bool(user.get("library")), "write": bool(user.get("write")),
                        "notes": bool(user.get("notes")), "files": bool(user.get("files"))}
        return self._me

    def check(self, perms: set[str]) -> dict:
        me = self.whoami()
        missing = []
        if perms and not (me["library"] and me["write"]):
            missing.append("'Allow library access' and 'Allow write access'")
        if "notes" in perms and not me["notes"]:
            missing.append("'Allow notes access'")
        if missing:
            raise ZoteroWriteError("This Zotero key needs " + " and ".join(missing) +
                                   ". Edit the key at zotero.org/settings/keys.")
        return me

    @property
    def base(self) -> str:
        return f"/users/{self.whoami()['user_id']}"

    # ------------------------------------------------------------ primitives
    def _create(self, objs: list[dict], kind: str = "items") -> list[str]:
        r = self.client.post(f"{self.base}/{kind}", json=objs)
        r.raise_for_status()
        d = r.json()
        if d.get("failed"):
            first = next(iter(d["failed"].values()))
            raise ZoteroWriteError(f"Zotero refused to create {kind}: {first.get('message', first)}")
        ok = d.get("success") or {}
        return [ok[str(i)] for i in range(len(objs))]

    def _get(self, path: str) -> Optional[dict]:
        r = self.client.get(f"{self.base}{path}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def _patch(self, key: str, data: dict, version: int) -> None:
        r = self.client.patch(f"{self.base}/items/{key}", json=data,
                              headers={"If-Unmodified-Since-Version": str(version)})
        if r.status_code == 412:
            raise ZoteroWriteError("An item changed in Zotero while syncing. Run the sync again.")
        r.raise_for_status()

    # ------------------------------------------------------------ operations
    def collection(self, name: str, parent: Optional[str] = None) -> str:
        path = f"/collections/{parent}/collections" if parent else "/collections/top"
        for c in self._get(f"{path}?limit=100") or []:
            if c["data"]["name"] == name:
                return c["key"]
        return self._create([{"name": name, "parentCollection": parent or False}], "collections")[0]

    def create_paper(self, cit: dict, abstract: str, coll: str, project: str) -> str:
        creators = []
        for a in cit.get("authors") or []:
            parts = a.rsplit(" ", 1)
            creators.append({"creatorType": "author", "firstName": parts[0], "lastName": parts[1]}
                            if len(parts) == 2 else {"creatorType": "author", "name": a})
        item = {"itemType": "journalArticle", "title": cit["title"], "creators": creators,
                "abstractNote": abstract[:10000], "publicationTitle": cit.get("venue") or "",
                "date": str(cit.get("year") or ""), "DOI": cit.get("doi") or "", "url": cit.get("url") or "",
                "collections": [coll], "tags": [{"tag": "tether"}, {"tag": f"tether:{project}"}]}
        return self._create([item])[0]

    def add_to_collection(self, key: str, coll: str) -> Optional[bool]:
        """True if filed, False if already there, None if zotero.org doesn't have the item yet."""
        it = self._get(f"/items/{key}")
        if it is None:
            return None
        cols = it["data"].get("collections", [])
        if coll in cols:
            return False
        self._patch(key, {"collections": cols + [coll]}, it["version"])
        return True

    def upsert_note(self, parent: str, note_html: str, note_key: Optional[str]) -> str:
        if note_key:
            existing = self._get(f"/items/{note_key}")
            if existing is not None:                   # only ever a note tether created (tracked key)
                if existing["data"].get("note") != note_html:
                    self._patch(note_key, {"note": note_html}, existing["version"])
                return note_key
        return self._create([{"itemType": "note", "parentItem": parent, "note": note_html,
                              "tags": [{"tag": "tether"}]}])[0]

    def upload_pdf(self, parent: str, pdf: Path) -> str:
        data = pdf.read_bytes()
        att = self._create([{"itemType": "attachment", "parentItem": parent, "linkMode": "imported_file",
                             "title": "Full Text PDF", "contentType": "application/pdf",
                             "filename": pdf.name, "tags": []}])[0]
        auth = self.client.post(f"{self.base}/items/{att}/file", headers={"If-None-Match": "*"}, data={
            "md5": hashlib.md5(data).hexdigest(), "filename": pdf.name, "filesize": str(len(data)),
            "mtime": str(int(pdf.stat().st_mtime * 1000))})
        if auth.status_code == 413:
            raise ZoteroWriteError("Your Zotero storage is full, so the PDF wasn't uploaded.")
        auth.raise_for_status()
        a = auth.json()
        if a.get("exists"):
            return att
        # the upload goes to Zotero's storage host: a separate client so the API key isn't sent there
        with httpx.Client(timeout=300, transport=self._transport) as raw:
            up = raw.post(a["url"], content=a["prefix"].encode("latin-1") + data + a["suffix"].encode("latin-1"),
                          headers={"Content-Type": a["contentType"]})
        up.raise_for_status()
        done = self.client.post(f"{self.base}/items/{att}/file", headers={"If-None-Match": "*"},
                                data={"upload": a["uploadKey"]})
        done.raise_for_status()
        return att


def claims_note(project: str, paper: dict, claims: list[dict], ideas: list[dict]) -> str:
    e = html.escape
    rows = []
    for c in claims:
        qual = f" <i>({e(c['qualifiers'])})</i>" if c.get("qualifiers") else ""
        rows.append(f"<li><b>{e(c['kind'])}</b>: {e(c['text'])}{qual} <span>(p. {c['page']})</span>"
                    f"<blockquote>{e(c['quote'])}</blockquote></li>")
    ids = {c["id"] for c in claims}
    mine = [i for i in ideas if any(ev["claim"] in ids for ev in i["evidence"])]
    out = [f"<h2>tether: key claims</h2>",
           f"<p>Project <b>{e(project)}</b>. Each claim was checked against the quoted text on the page shown. "
           f"This note is managed by tether and is rewritten on each sync.</p>",
           "<ul>" + "".join(rows) + "</ul>" if rows else "<p>No claims passed verification for this paper.</p>"]
    if mine:
        out.append("<h3>Ideas this paper feeds</h3><ul>" + "".join(
            f"<li><b>{e(i['title'])}</b>: {e(i['statement'])}</li>" for i in mine) + "</ul>")
    return "".join(out)


def sync_project(web: ZoteroWeb, project_dir: Path, perms: set[str], local_lib=None,
                 log: Callable[[str], None] = print) -> dict:
    """Push a built project to Zotero according to `perms`. Idempotent: re-running updates
    tether's own notes and skips anything already done (tracked in .tether/zotero_sync.json)."""
    if not perms:
        return {"skipped": "no write permissions"}
    web.check(perms)
    project = project_dir.name
    graph_file = project_dir / ".tether" / "graph.json"
    if not graph_file.exists():
        raise ZoteroWriteError("Build the project before syncing it to Zotero.")
    g = json.loads(graph_file.read_text(encoding="utf-8"))
    state_file = project_dir / ".tether" / "zotero_sync.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    state.setdefault("items", {}); state.setdefault("notes", {}); state.setdefault("pdfs", {})

    def save():
        state_file.write_text(json.dumps(state, indent=1), encoding="utf-8")

    stats = {"created": 0, "linked": 0, "pdfs": 0, "notes": 0, "notes_updated": 0}
    coll = None
    if "papers" in perms:
        root = state.get("root") or web.collection(ROOT_COLLECTION)
        coll = state.get("collection") or web.collection(project, root)
        state["root"], state["collection"] = root, coll
        save()

    by_paper: dict[str, list[dict]] = {}
    for c in g["claims"]:
        by_paper.setdefault(c["paper_id"], []).append(c)

    for p in g["papers"]:
        cit = p["citation"]
        tkey = _norm(cit["title"])
        key = cit.get("zotero_key") or state["items"].get(tkey)
        if not key and local_lib is not None:
            hit = local_lib.find(cit["title"], cit.get("doi"))
            key = hit.key if hit else None
        if not key and "papers" in perms:
            abstract = p.get("abstract", "")
            key = web.create_paper(cit, abstract, coll, project)
            state["items"][tkey] = key
            stats["created"] += 1
            log(f"  + added to Zotero: {cit['title'][:70]}")
            if "pdfs" in perms and p.get("path") and Path(p["path"]).exists():
                try:
                    web.upload_pdf(key, Path(p["path"]))
                    state["pdfs"][key] = True
                    stats["pdfs"] += 1
                except ZoteroWriteError as e:
                    log(f"  ! {e}")
            save()
        elif key and "papers" in perms:
            filed = web.add_to_collection(key, coll)
            if filed is None:
                log(f"  ! '{cit['title'][:60]}' isn't on zotero.org yet. Let Zotero desktop sync, then sync again.")
                continue
            if filed:
                stats["linked"] += 1
            state["items"].setdefault(tkey, key)
        if key and "notes" in perms:
            note = claims_note(project, p, by_paper.get(p["id"], []), g.get("ideas", []))
            prev = state["notes"].get(key)
            nk = web.upsert_note(key, note, prev)
            stats["notes" if nk != prev else "notes_updated"] += 1
            state["notes"][key] = nk
            save()
    save()
    return stats
