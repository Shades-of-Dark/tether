"""Test doubles for Zotero: a minimal zotero.sqlite + storage folder, and an in-memory
Zotero Web API (httpx.MockTransport) that records every write."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs

import httpx

SCHEMA = """
CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
CREATE TABLE items (itemID INTEGER PRIMARY KEY, itemTypeID INT, libraryID INT, key TEXT, dateAdded TEXT);
CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value);
CREATE TABLE itemData (itemID INT, fieldID INT, valueID INT);
CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT, fieldMode INT);
CREATE TABLE itemCreators (itemID INT, creatorID INT, creatorTypeID INT, orderIndex INT);
CREATE TABLE itemAttachments (itemID INTEGER PRIMARY KEY, parentItemID INT, linkMode INT, contentType TEXT, path TEXT);
CREATE TABLE collections (collectionID INTEGER PRIMARY KEY, collectionName TEXT, parentCollectionID INT, key TEXT);
CREATE TABLE collectionItems (collectionID INT, itemID INT, orderIndex INT);
CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
"""


def make_zotero(root: Path, papers: list[dict]) -> Path:
    """papers: {key, title, authors, date, doi?, venue?, abstract?, pdf? (Path), collection?, deleted?}"""
    root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(root / "zotero.sqlite")
    con.executescript(SCHEMA)
    types = {"journalArticle": 1, "attachment": 2, "note": 3, "preprint": 4}
    con.executemany("INSERT INTO itemTypes VALUES (?,?)", [(v, k) for k, v in types.items()])
    fields = {n: i for i, n in enumerate(["title", "date", "DOI", "publicationTitle", "abstractNote", "url", "extra"], 1)}
    con.executemany("INSERT INTO fields VALUES (?,?)", [(v, k) for k, v in fields.items()])
    colls: dict[str, int] = {}
    nid, vid, cid = 1, 1, 1
    for p in papers:
        iid = nid; nid += 1
        con.execute("INSERT INTO items VALUES (?,?,1,?,?)", (iid, types["journalArticle"], p["key"], p.get("added", f"2026-01-{iid:02d}")))
        for f, k in [("title", "title"), ("date", "date"), ("DOI", "doi"), ("publicationTitle", "venue"),
                     ("abstractNote", "abstract"), ("extra", "extra")]:
            if p.get(k):
                con.execute("INSERT INTO itemDataValues VALUES (?,?)", (vid, p[k]))
                con.execute("INSERT INTO itemData VALUES (?,?,?)", (iid, fields[f], vid)); vid += 1
        for i, a in enumerate(p.get("authors", [])):
            first, last = a.rsplit(" ", 1)
            con.execute("INSERT INTO creators VALUES (?,?,?,0)", (cid, first, last))
            con.execute("INSERT INTO itemCreators VALUES (?,?,1,?)", (iid, cid, i)); cid += 1
        if p.get("pdf"):
            aid = nid; nid += 1
            akey = p["key"] + "A"
            con.execute("INSERT INTO items VALUES (?,?,1,?,'2026-01-01')", (aid, types["attachment"], akey))
            con.execute("INSERT INTO itemAttachments VALUES (?,?,1,'application/pdf',?)",
                        (aid, iid, f"storage:{p['pdf'].name}"))
            (root / "storage" / akey).mkdir(parents=True, exist_ok=True)
            (root / "storage" / akey / p["pdf"].name).write_bytes(p["pdf"].read_bytes())
        if p.get("collection"):
            parts = p["collection"].split("/")
            parent = None
            for depth in range(len(parts)):
                name = "/".join(parts[:depth + 1])
                if name not in colls:
                    colls[name] = len(colls) + 1
                    con.execute("INSERT INTO collections VALUES (?,?,?,?)", (colls[name], parts[depth], parent, f"COL{colls[name]}"))
                parent = colls[name]
            con.execute("INSERT INTO collectionItems VALUES (?,?,0)", (parent, iid))
        if p.get("deleted"):
            con.execute("INSERT INTO deletedItems VALUES (?)", (iid,))
    con.commit()
    con.close()
    return root


class FakeZoteroWeb:
    """Just enough of api.zotero.org v3 for tether's write-back."""

    def __init__(self, existing: dict[str, dict] | None = None, access: dict | None = None):
        self.items: dict[str, dict] = {k: {"key": k, "version": 1, "data": dict(v, key=k)} for k, v in (existing or {}).items()}
        self.collections: dict[str, dict] = {}
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.access = access or {"library": True, "write": True, "notes": True, "files": True}
        self.n = 0
        self.upload_headers: list[dict] = []

    def _key(self):
        self.n += 1
        return f"NEW{self.n:05d}"

    def handler(self, req: httpx.Request) -> httpx.Response:
        path, m = req.url.path, req.method
        self.calls.append((m, path))
        if req.url.host == "uploads.example":
            self.upload_headers.append(dict(req.headers))
            self.files[req.url.params["att"]] = req.content
            return httpx.Response(201)
        if req.headers.get("Zotero-API-Key") != "zkey-good":
            return httpx.Response(403)
        if path == "/keys/current":
            return httpx.Response(200, json={"userID": 42, "username": "vedh", "access": {"user": self.access}})
        assert path.startswith("/users/42"), path
        sub = path[len("/users/42"):]
        if sub in ("/collections/top",) and m == "GET":
            return httpx.Response(200, json=[c for c in self.collections.values() if not c["data"]["parentCollection"]])
        if sub.startswith("/collections/") and sub.endswith("/collections") and m == "GET":
            parent = sub.split("/")[2]
            return httpx.Response(200, json=[c for c in self.collections.values() if c["data"]["parentCollection"] == parent])
        if sub.startswith("/collections/") and sub.endswith("/items/top") and m == "GET":
            coll = sub.split("/")[2]
            start = int(req.url.params.get("start", 0)); limit = int(req.url.params.get("limit", 25))
            top = [i for i in self.items.values() if coll in i["data"].get("collections", []) and not i["data"].get("parentItem")]
            return httpx.Response(200, json=top[start:start + limit])
        if sub == "/collections" and m == "POST":
            out = {}
            for i, obj in enumerate(json.loads(req.content)):
                k = self._key(); self.collections[k] = {"key": k, "data": dict(obj, key=k)}; out[str(i)] = k
            return httpx.Response(200, json={"success": out, "failed": {}})
        if sub == "/items" and m == "POST":
            out = {}
            for i, obj in enumerate(json.loads(req.content)):
                k = self._key(); self.items[k] = {"key": k, "version": 1, "data": dict(obj, key=k)}; out[str(i)] = k
            return httpx.Response(200, json={"success": out, "failed": {}})
        if sub.startswith("/items/") and sub.endswith("/file") and m == "POST":
            key = sub.split("/")[2]
            form = parse_qs(req.content.decode())
            if "upload" in form:
                return httpx.Response(204)
            return httpx.Response(200, json={"url": f"https://uploads.example/up?att={key}", "contentType": "multipart/form-data; boundary=x",
                                             "prefix": "--x\r\n", "suffix": "\r\n--x--", "uploadKey": "UK"})
        if sub.startswith("/items/"):
            key = sub.split("/")[2]
            it = self.items.get(key)
            if it is None:
                return httpx.Response(404)
            if m == "GET":
                return httpx.Response(200, json=it)
            if m == "PATCH":
                if req.headers.get("If-Unmodified-Since-Version") != str(it["version"]):
                    return httpx.Response(412)
                it["data"].update(json.loads(req.content)); it["version"] += 1
                return httpx.Response(204)
            if m == "DELETE":
                raise AssertionError("tether must never delete")
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
