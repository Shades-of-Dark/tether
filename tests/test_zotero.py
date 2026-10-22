import json
import time
from pathlib import Path

import pytest

from tether.pipeline import import_from_zotero, run
from tether.zotero import ZoteroError, ZoteroLibrary

from .fake import FakeLLM, make_pdf
from .test_tether import FILLER
from .zfake import FakeZoteroWeb, make_zotero


def _pdf(tmp, name, title, finding):
    return make_pdf(tmp / name, title, [[FILLER, f"FINDING: {finding}", FILLER]])


@pytest.fixture
def zlib(tmp_path):
    a = _pdf(tmp_path, "place.pdf", "Placement and Line Stability",
             "Placing widgets near the {belt center} reduces the corrections needed during peak load.")
    zdir = make_zotero(tmp_path / "Zotero", [
        {"key": "AAAA1111", "title": "Placement and Line Stability", "authors": ["Maya Chen", "Luis Ortiz"],
         "date": "2024-05-02 2024-05-02", "doi": "10.1000/example", "venue": "Journal of Examples", "pdf": a, "collection": "Research/Widgets"},
        {"key": "BBBB2222", "title": "Contact Models for Widget Handling", "authors": ["Ana Ruiz"], "date": "2022",
         "abstract": "We learn contact models for widget handling that transfer across loads and placements.",
         "collection": "Research"},
        {"key": "CCCC3333", "title": "A Preprint With DOI In Extra", "authors": ["Sam Lee"], "date": "2025",
         "extra": "DOI: 10.48550/arXiv.2501.00001"},
        {"key": "DDDD4444", "title": "Trashed Paper", "authors": ["Old Item"], "date": "2020", "deleted": True},
    ])
    return zdir


def test_reader(zlib):
    lib = ZoteroLibrary(zlib)
    assert set(lib.items) == {"AAAA1111", "BBBB2222", "CCCC3333"}                  # trash excluded
    a = lib.items["AAAA1111"]
    assert a.authors == ["Maya Chen", "Luis Ortiz"] and a.year == 2024 and a.venue == "Journal of Examples"
    assert a.pdf and a.pdf.exists() and a.pdf.parent.name == "AAAA1111A"
    assert lib.items["CCCC3333"].doi == "10.48550/arxiv.2501.00001"
    tree = lib.collection_tree()
    assert [(c["name"], c["depth"]) for c in tree] == [("Research", 0), ("Widgets", 1)]
    research = tree[0]["id"]
    assert {i.key for i in lib.search(research)} == {"AAAA1111", "BBBB2222"}       # includes subcollections
    assert [i.key for i in lib.search(query="ruiz widget")] == ["BBBB2222"]
    assert lib.find("placement & line stability").key == "AAAA1111"
    assert lib.find("", doi="10.1000/EXAMPLE").key == "AAAA1111"
    assert lib.find("Something Else Entirely About Teapots") is None
    with pytest.raises(ZoteroError):
        ZoteroLibrary(zlib.parent / "nope")


def test_reader_never_modifies_the_database(zlib):
    db = zlib / "zotero.sqlite"
    before = (db.read_bytes(), db.stat().st_mtime)
    ZoteroLibrary(zlib)
    assert (db.read_bytes(), db.stat().st_mtime) == before
    assert not list(zlib.glob("*-journal")) and not list(zlib.glob("*.sqlite-wal"))


def test_import_and_zotero_citations_in_graph(tmp_path, zlib):
    lib = ZoteroLibrary(zlib)
    proj = tmp_path / "Vault" / "Tether" / "P"
    res = import_from_zotero(lib, ["AAAA1111", "BBBB2222", "CCCC3333"], proj)
    assert res == {"pdf": 1, "abstract": 1, "skipped": ["A Preprint With DOI In Extra"]}   # no PDF, no abstract
    # a PDF dropped by hand that's also in Zotero gets Zotero's citation, not a guess
    dropped = _pdf(tmp_path, "mine.pdf", "Contact Models for Widget Handling",
                   "Learned {contact model} priors cut handling failures on unseen widgets in half.")
    run(FakeLLM(), out=proj, pdfs=[dropped], vault_root=tmp_path / "Vault", lookup=False, zotero=lib)

    g = json.loads((proj / ".tether" / "graph.json").read_text())
    cits = {p["citation"]["title"]: p for p in g["papers"]}
    assert cits["Placement and Line Stability"]["citation"]["source"] == "zotero"
    assert cits["Placement and Line Stability"]["citation"]["authors"] == ["Maya Chen", "Luis Ortiz"]
    assert cits["Contact Models for Widget Handling"]["source_kind"] == "fulltext"     # PDF beat the abstract
    assert cits["Contact Models for Widget Handling"]["citation"]["zotero_key"] == "BBBB2222"
    note = next((proj / "Papers").glob("*Chen*")).read_text()
    assert "zotero://select/library/items/AAAA1111" in note
    refs = (proj / "References.md").read_text()
    assert "Chen, M., Ortiz, L. (2024). Placement and Line Stability. *Journal of Examples*. https://doi.org/10.1000/example" in refs
    assert "unverified" not in refs


def _client(tmp_path, monkeypatch, web: FakeZoteroWeb):
    from fastapi.testclient import TestClient
    from tether import app as appmod, config
    import tether.llm as llmmod
    import tether.sources as src

    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(config, "_SHELL_ENV", {})
    monkeypatch.setattr(appmod, "ZOTERO_TRANSPORT", web.transport())

    class Fake(FakeLLM):
        model, judge_model, usage = "fake", "fake", {"input": 0, "output": 0}

        def __init__(self, **kw):
            super().__init__()
    monkeypatch.setattr(llmmod, "ClaudeLLM", Fake)
    monkeypatch.setattr(src, "lookup_by_title", lambda t: None)
    monkeypatch.setattr(appmod.builder, "debounce", 0.1)
    c = TestClient(appmod.app)
    c.post("/api/key", json={"key": "sk-ant-api03-" + "x" * 40})
    (tmp_path / "Vault").mkdir()
    c.post("/api/vault", json={"path": str(tmp_path / "Vault")})
    return c


def _build(c, name):
    from .test_tether import wait_for_update
    before = c.get(f"/api/projects/{name}/activity").json()["run"]
    c.post(f"/api/projects/{name}/rebuild")
    return wait_for_update(c, name, after_run=before)


def test_permission_tiers(tmp_path, monkeypatch, zlib):
    from tether import config
    web = FakeZoteroWeb(existing={
        "AAAA1111": {"itemType": "journalArticle", "title": "Placement and Line Stability", "collections": ["MINE"]},
        "BBBB2222": {"itemType": "journalArticle", "title": "Contact Models for Widget Handling", "collections": []}})
    c = _client(tmp_path, monkeypatch, web)

    # tier 0: off. nothing is read
    st = c.get("/api/zotero").json()
    assert not st["connected"] and st["write"] == []
    assert c.get("/api/zotero/library").status_code == 400
    assert c.post("/api/zotero/write", json={"perms": ["papers"], "api_key": "zkey-good"}).status_code == 400

    # tier 1: read
    assert c.post("/api/zotero/connect", json={"dir": str(tmp_path)}).status_code == 400       # wrong folder
    st = c.post("/api/zotero/connect", json={"dir": str(zlib)}).json()
    assert st["ok"] and st["items"] == 3 and st["with_pdf"] == 1 and st["write"] == []
    c.post("/api/projects", json={"name": "Widgets"})
    lib = c.get("/api/zotero/library?project=Widgets").json()
    assert lib["total"] == 3 and not any(i["in_project"] for i in lib["items"])
    r = c.post("/api/projects/Widgets/zotero-import", json={"keys": ["AAAA1111", "BBBB2222"]}).json()
    assert r["imported"]["pdf"] == 1 and r["files"][0]["name"] == "place.pdf" and len(r["abstracts"]) == 1
    lib = c.get("/api/zotero/library?project=Widgets").json()
    assert {i["key"] for i in lib["items"] if i["in_project"]} == {"AAAA1111", "BBBB2222"}

    # a paper tether found that ISN'T in Zotero
    newpdf = _pdf(tmp_path, "new.pdf", "Robust Throughput Under Unknown Loads",
                  "A {weight estimator} lets the scheduler recover throughput within one second.")
    with open(newpdf, "rb") as f:
        c.post("/api/projects/Widgets/upload", files=[("files", ("new.pdf", f, "application/pdf"))])

    j = _build(c, "Widgets")                       # read-only: build works, nothing written anywhere
    assert j["state"] == "done" and j["stats"]["zotero"] is None
    assert not [x for x in web.calls if x[0] != "GET"]
    assert c.post("/api/projects/Widgets/zotero-sync").status_code == 400

    # tier 2: write, with checks on the key itself
    assert c.post("/api/zotero/write", json={"perms": ["papers"], "api_key": "zkey-bad"}).status_code == 400
    web.access = {"library": True, "write": False, "notes": False, "files": False}
    msg = c.post("/api/zotero/write", json={"perms": ["papers"], "api_key": "zkey-good"}).json()["detail"]
    assert "write access" in msg
    web.access = {"library": True, "write": True, "notes": True, "files": True}
    st = c.post("/api/zotero/write", json={"perms": ["papers", "notes", "bogus"], "api_key": "zkey-good"}).json()
    assert st["write"] == ["notes", "papers"] and st["key_hint"] and "zkey-good" not in json.dumps(c.get("/api/zotero").json())
    assert "ZOTERO_API_KEY=zkey-good" in (tmp_path / ".env").read_text()

    j = _build(c, "Widgets")
    z = j["stats"]["zotero"]
    assert z["created"] == 1 and z["linked"] == 2 and z["pdfs"] == 0 and z["notes"] == 3, j["log"]
    colls = {c_["data"]["name"]: c_ for c_ in web.collections.values()}
    assert set(colls) == {"Tether", "Widgets"} and colls["Widgets"]["data"]["parentCollection"] == colls["Tether"]["key"]
    proj_coll = colls["Widgets"]["key"]
    created = [i for i in web.items.values() if i["data"].get("title") == "Robust Throughput Under Unknown Loads"][0]
    assert proj_coll in created["data"]["collections"] and {"tag": "tether"} in created["data"]["tags"]
    assert web.items["AAAA1111"]["data"]["collections"] == ["MINE", proj_coll]       # filed, nothing else touched
    notes = [i for i in web.items.values() if i["data"]["itemType"] == "note"]
    assert len(notes) == 3 and all("tether: key claims" in n["data"]["note"] for n in notes)

    # re-sync: notes updated in place, no duplicates, nothing deleted
    before = len(web.items)
    r = c.post("/api/projects/Widgets/zotero-sync").json()
    assert len(web.items) == before and r["stats"]["created"] == 0 and r["stats"]["notes"] == 0
    assert not [x for x in web.calls if x[0] == "DELETE"]

    # PDFs: opt-in. the upload host never sees the API key
    c.post("/api/zotero/write", json={"perms": ["papers", "pdfs"]})               # key reused, not re-sent
    (tmp_path / "Vault" / "Tether" / "Widgets" / ".tether" / "zotero_sync.json").unlink()   # pretend first sync
    web.items = {k: v for k, v in web.items.items() if v["data"].get("title") != "Robust Throughput Under Unknown Loads"}
    r = c.post("/api/projects/Widgets/zotero-sync").json()
    assert r["stats"]["pdfs"] == 1 and len(web.files) == 1
    assert all("zotero-api-key" not in {k.lower() for k in h} for h in web.upload_headers)

    # back to off: folder, permissions and key all forgotten
    st = c.post("/api/zotero/disconnect").json()
    assert not st["connected"]
    env = (tmp_path / ".env").read_text()
    assert "TETHER_ZOTERO=\n" in env and "TETHER_ZOTERO_WRITE=\n" in env and "ZOTERO_API_KEY=\n" in env
    assert config.get("ZOTERO_API_KEY") is None


def test_item_not_yet_on_zotero_org_is_reported_not_guessed(tmp_path, zlib):
    from tether.zotero_sync import ZoteroWeb, sync_project
    lib = ZoteroLibrary(zlib)
    proj = tmp_path / "P"
    import_from_zotero(lib, ["AAAA1111"], proj)
    run(FakeLLM(), out=proj, lookup=False, zotero=lib)
    web = FakeZoteroWeb()                                  # zotero.org has never seen AAAA1111
    lines = []
    stats = sync_project(ZoteroWeb("zkey-good", web.transport()), proj, {"papers", "notes"}, lib, lines.append)
    assert stats["created"] == 0 and stats["notes"] == 0   # no duplicate created, no orphan note
    assert any("isn't on zotero.org yet" in l for l in lines)
