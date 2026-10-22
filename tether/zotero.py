"""Read-only access to a local Zotero library.

tether never writes to Zotero. It copies zotero.sqlite to a temp file, opens the copy
read-only, and reads PDFs from Zotero's storage folder. Nothing happens until you
connect Zotero in Settings, and disconnecting stops all access.

Zotero data folder (default ~/Zotero):
    zotero.sqlite
    storage/<ATTACHMENT KEY>/<file>.pdf
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz

from .models import Citation

SKIP_TYPES = {"attachment", "note", "annotation"}
VENUE_FIELDS = ("publicationTitle", "proceedingsTitle", "conferenceName", "bookTitle", "repository",
                "university", "institution", "websiteTitle", "publisher")
TITLE_MATCH = 92


class ZoteroError(RuntimeError):
    pass


def default_dir() -> Optional[Path]:
    d = Path.home() / "Zotero"
    return d if (d / "zotero.sqlite").exists() else None


@dataclass
class ZItem:
    key: str
    item_type: str
    title: str
    authors: list[str]
    year: Optional[int]
    venue: Optional[str]
    doi: Optional[str]
    url: Optional[str]
    abstract: str
    pdf: Optional[Path]                       # resolved path to a stored PDF, if any
    collections: list[int] = field(default_factory=list)
    date_added: str = ""

    def citation(self) -> Citation:
        return Citation(title=self.title, authors=self.authors, year=self.year, venue=self.venue,
                        doi=self.doi, url=self.url, source="zotero", verified=True, zotero_key=self.key)

    def summary(self) -> dict:
        return {"key": self.key, "type": self.item_type, "title": self.title,
                "authors": self.authors[:3] + (["…"] if len(self.authors) > 3 else []),
                "year": self.year, "venue": self.venue, "doi": self.doi,
                "has_pdf": self.pdf is not None, "has_abstract": bool(self.abstract.strip()),
                "collections": self.collections, "added": self.date_added}


@dataclass
class ZCollection:
    id: int
    name: str
    parent: Optional[int]
    count: int = 0


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", t.lower()).strip()


class ZoteroLibrary:
    def __init__(self, data_dir: Path | str):
        self.dir = Path(data_dir).expanduser()
        db = self.dir / "zotero.sqlite"
        if not db.exists():
            raise ZoteroError(f"No zotero.sqlite in {self.dir}. In Zotero: Settings → Advanced → "
                              "Files and Folders → 'Show Data Directory' shows the right folder.")
        self.db = db
        self.items: dict[str, ZItem] = {}
        self.collections: dict[int, ZCollection] = {}
        self._load()

    # ---------------------------------------------------------------- loading
    def _snapshot(self) -> Path:
        """Copy the database (and WAL, if any) so we never touch, lock, or modify the original."""
        tmp = Path(tempfile.mkdtemp(prefix="tether-zotero-"))
        try:
            shutil.copy2(self.db, tmp / "zotero.sqlite")
            wal = self.db.with_name("zotero.sqlite-wal")
            if wal.exists():
                shutil.copy2(wal, tmp / "zotero.sqlite-wal")
        except OSError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            raise ZoteroError(f"Couldn't read Zotero's database ({e}). Try again, or close Zotero and retry.")
        return tmp

    def _load(self) -> None:
        tmp = self._snapshot()
        try:
            con = sqlite3.connect(f"file:{(tmp / 'zotero.sqlite').as_posix()}?mode=ro", uri=True)
            try:
                self._read(con)
            except sqlite3.DatabaseError as e:
                raise ZoteroError(f"Couldn't read Zotero's database: {e}")
            finally:
                con.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _read(self, con: sqlite3.Connection) -> None:
        q = lambda sql, *a: con.execute(sql, a).fetchall()
        deleted = {r[0] for r in q("SELECT itemID FROM deletedItems")} if self._has(con, "deletedItems") else set()

        rows = q("""SELECT i.itemID, i.key, t.typeName, i.dateAdded FROM items i
                    JOIN itemTypes t ON t.itemTypeID = i.itemTypeID""")
        meta = {iid: (key, typ, added) for iid, key, typ, added in rows}
        regular = {iid for iid, (_, typ, _) in meta.items() if typ not in SKIP_TYPES and iid not in deleted}

        fields: dict[int, dict[str, str]] = {}
        for iid, fname, value in q("""SELECT d.itemID, f.fieldName, v.value FROM itemData d
                                      JOIN fields f ON f.fieldID = d.fieldID
                                      JOIN itemDataValues v ON v.valueID = d.valueID"""):
            if iid in regular:
                fields.setdefault(iid, {})[fname] = str(value)

        authors: dict[int, list[str]] = {}
        for iid, first, last, mode in q("""SELECT ic.itemID, c.firstName, c.lastName, c.fieldMode
                                           FROM itemCreators ic JOIN creators c ON c.creatorID = ic.creatorID
                                           ORDER BY ic.itemID, ic.orderIndex"""):
            if iid in regular:
                name = (last or "") if mode == 1 else " ".join(x for x in (first, last) if x)
                if name.strip():
                    authors.setdefault(iid, []).append(name.strip())

        pdfs: dict[int, Path] = {}
        for att_id, parent, path, ctype, link_mode in q(
                "SELECT itemID, parentItemID, path, contentType, linkMode FROM itemAttachments"):
            if parent not in regular or att_id in deleted or not path:
                continue
            if (ctype or "") != "application/pdf" and not str(path).lower().endswith(".pdf"):
                continue
            if path.startswith("storage:"):
                f = self.dir / "storage" / meta[att_id][0] / path[len("storage:"):]
            elif path.startswith("attachments:"):
                continue                                   # relative to a base dir we don't know
            else:
                f = Path(path)                             # linked file, absolute path
            if f.exists() and parent not in pdfs:
                pdfs[parent] = f

        in_coll: dict[int, list[int]] = {}
        if self._has(con, "collections"):
            for cid, name, parent in q("SELECT collectionID, collectionName, parentCollectionID FROM collections"):
                self.collections[cid] = ZCollection(cid, name, parent)
            for cid, iid in q("SELECT collectionID, itemID FROM collectionItems"):
                if iid in regular and cid in self.collections:
                    in_coll.setdefault(iid, []).append(cid)
                    self.collections[cid].count += 1

        for iid in regular:
            f = fields.get(iid, {})
            title = f.get("title", "").strip()
            if not title:
                continue
            m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", f.get("date", ""))
            key, typ, added = meta[iid]
            doi = f.get("DOI") or None
            if not doi:                                    # Zotero often stashes preprint DOIs in Extra
                m2 = re.search(r"\bDOI:\s*(10\.\S+)", f.get("extra", ""), re.I)
                doi = m2.group(1) if m2 else None
            self.items[key] = ZItem(
                key=key, item_type=typ, title=title, authors=authors.get(iid, []),
                year=int(m.group(1)) if m else None,
                venue=next((f[v] for v in VENUE_FIELDS if f.get(v)), None),
                doi=doi.lower().removeprefix("https://doi.org/") if doi else None,
                url=f.get("url") or None, abstract=f.get("abstractNote", ""), pdf=pdfs.get(iid),
                collections=sorted(in_coll.get(iid, [])), date_added=added or "")
        self._by_doi = {it.doi: it for it in self.items.values() if it.doi}
        self._titles = [(_norm_title(it.title), it) for it in self.items.values()]

    @staticmethod
    def _has(con, table: str) -> bool:
        return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())

    # ---------------------------------------------------------------- queries
    def collection_tree(self) -> list[dict]:
        def node(c: ZCollection, depth: int) -> list[dict]:
            out = [{"id": c.id, "name": c.name, "depth": depth, "count": c.count}]
            for child in sorted((x for x in self.collections.values() if x.parent == c.id), key=lambda x: x.name.lower()):
                out += node(child, depth + 1)
            return out
        roots = sorted((c for c in self.collections.values() if c.parent not in self.collections), key=lambda x: x.name.lower())
        return [n for r in roots for n in node(r, 0)]

    def _descendants(self, cid: int) -> set[int]:
        out, stack = set(), [cid]
        while stack:
            c = stack.pop()
            out.add(c)
            stack += [x.id for x in self.collections.values() if x.parent == c]
        return out

    def search(self, collection: Optional[int] = None, query: str = "") -> list[ZItem]:
        items = list(self.items.values())
        if collection is not None:
            ids = self._descendants(collection)
            items = [i for i in items if ids & set(i.collections)]
        if query.strip():
            words = query.lower().split()
            items = [i for i in items
                     if all(w in f"{i.title} {' '.join(i.authors)} {i.year or ''} {i.venue or ''}".lower() for w in words)]
        return sorted(items, key=lambda i: i.date_added, reverse=True)

    def find(self, title: str = "", doi: Optional[str] = None) -> Optional[ZItem]:
        """Match a paper tether found elsewhere (a dropped PDF) to an item already in Zotero."""
        if doi and doi.lower() in self._by_doi:
            return self._by_doi[doi.lower()]
        nt = _norm_title(title)
        if len(nt.split()) < 3:
            return None
        best, score = None, 0.0
        for t, it in self._titles:
            s = fuzz.token_sort_ratio(nt, t)
            if s > score:
                best, score = it, s
        return best if score >= TITLE_MATCH else None


# ---------------------------------------------------------------- cached access
_cache: dict[str, tuple[float, ZoteroLibrary]] = {}
_lock = threading.Lock()


def open_library(data_dir: Path | str) -> ZoteroLibrary:
    """Reload only when Zotero's database has changed since the last read."""
    d = Path(data_dir).expanduser()
    db = d / "zotero.sqlite"
    if not db.exists():
        raise ZoteroError(f"No zotero.sqlite in {d}.")
    stamp = max(p.stat().st_mtime for p in (db, db.with_name("zotero.sqlite-wal")) if p.exists())
    with _lock:
        hit = _cache.get(str(d))
        if hit and hit[0] == stamp:
            return hit[1]
        lib = ZoteroLibrary(d)
        _cache[str(d)] = (stamp, lib)
        return lib
