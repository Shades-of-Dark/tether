"""The full-text finder: every route to a paper's PDF, with made-up publishers and papers."""
import json

import httpx
import pytest

from tether import fulltext
from tether.models import Citation
from tests.fake import make_pdf

TITLE = "Adaptive Damping of Oscillating Widget Conveyors"
OTHER = "A Completely Different Study of Garden Sprinklers"


@pytest.fixture
def pdfs(tmp_path):
    body = [["Filler text about conveyor lines. " * 30, "FINDING: Damping keeps the belt steady."]]
    right = make_pdf(tmp_path / "right.pdf", TITLE, body).read_bytes()
    wrong = make_pdf(tmp_path / "wrong.pdf", OTHER, body).read_bytes()
    return right, wrong


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def _cit(**kw):
    return Citation(title=TITLE, **kw)


def test_pdf_link_in_page_metadata(tmp_path, pdfs):
    """A landing page that names its PDF in citation_pdf_url (how most open-access journals do it)."""
    right, _ = pdfs
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if req.url.host == "doi.org":
            return httpx.Response(302, headers={"location": "https://journal.example/article/7"})
        if req.url.path == "/article/7":
            return httpx.Response(200, html='<html><head><meta name="citation_pdf_url" '
                                            'content="/article/7/pdf"></head><body>Abstract</body></html>')
        if req.url.path == "/article/7/pdf":
            assert req.headers.get("referer", "").endswith("/article/7")      # sent like a browser would
            return httpx.Response(200, content=right, headers={"content-type": "application/pdf"})
        return httpx.Response(404)
    res = fulltext.find_pdf(_cit(doi="10.9999/conv.7"), tmp_path / "out.pdf", work={}, client=_client(handler))
    assert res.path and res.path.read_bytes() == right
    assert res.url.endswith("/article/7/pdf")


def test_pdf_inside_an_iframe_viewer(tmp_path, pdfs):
    """Publisher pages that show the PDF in a viewer frame."""
    right, _ = pdfs

    def handler(req):
        if req.url.path == "/view":
            return httpx.Response(200, html='<html><body><iframe src="/files/paper.pdf"></iframe></body></html>')
        if req.url.path == "/files/paper.pdf":
            return httpx.Response(200, content=right)
        return httpx.Response(404)
    res = fulltext.find_pdf(_cit(url="https://viewer.example/view"), tmp_path / "out.pdf", work={},
                            client=_client(handler))
    assert res.path is not None


def test_wrong_paper_is_rejected(tmp_path, pdfs):
    """A PDF that isn't this paper (a different article, a supplement) is never kept."""
    _, wrong = pdfs

    def handler(req):
        if req.url.host in ("api.semanticscholar.org", "export.arxiv.org", "www.ebi.ac.uk", "api.openalex.org"):
            return httpx.Response(404)
        return httpx.Response(200, content=wrong)
    res = fulltext.find_pdf(_cit(pdf_url="https://mirror.example/x.pdf"), tmp_path / "out.pdf", work={},
                            client=_client(handler))
    assert res.path is None and not (tmp_path / "out.pdf").exists()
    assert "free copy" in res.reason


def test_blocked_site_is_reported(tmp_path):
    def handler(req):
        if req.url.host == "publisher.example":
            return httpx.Response(403, text="Access denied")
        return httpx.Response(404)
    res = fulltext.find_pdf(_cit(url="https://publisher.example/a/1"), tmp_path / "out.pdf", work={},
                            client=_client(handler))
    assert res.path is None and "blocked" in res.reason


def test_falls_back_to_an_arxiv_copy(tmp_path, pdfs):
    right, _ = pdfs
    feed = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2401.00001v2</id>'
            f'<title>{TITLE}</title></entry></feed>')

    def handler(req):
        if req.url.host == "export.arxiv.org":
            return httpx.Response(200, text=feed)
        if req.url.host == "arxiv.org" and req.url.path.startswith("/pdf/2401.00001"):
            return httpx.Response(200, content=right)
        return httpx.Response(404)
    res = fulltext.find_pdf(_cit(url="https://paywalled.example/a"), tmp_path / "out.pdf", work={},
                            client=_client(handler))
    assert res.path is not None and "arxiv" in res.url


def test_bare_link_never_searches_by_empty_title(tmp_path, pdfs):
    """A pasted page with no PDF must not pull in some unrelated paper from a title search."""
    right, _ = pdfs
    res = fulltext.find_pdf(Citation(title="", url="https://nothing.example/page"), tmp_path / "out.pdf",
                            client=_client(lambda req: httpx.Response(200, content=right)
                                           if req.url.host != "nothing.example" else httpx.Response(404)))
    assert res.path is None


def test_publisher_routes():
    r = fulltext.publisher_routes("https://www.mdpi.com/1424-8220/21/19/6452")
    assert any(u.endswith("/pdf") for u in r)
    r = fulltext.publisher_routes("https://ieeexplore.ieee.org/document/1234567")
    assert any("arnumber=1234567" in u for u in r)


def _project(tmp_path, abstracts):
    proj = tmp_path / "P"
    (proj / ".tether").mkdir(parents=True)
    (proj / "Sources").mkdir()
    (proj / ".tether" / "abstract_only.json").write_text(json.dumps(abstracts))
    return proj


def test_upgrade_replaces_abstract_with_full_text(tmp_path, pdfs, monkeypatch):
    import tether.sources as src
    from tether.pipeline import FINDER_VERSION, Reporter, upgrade_abstracts
    right, _ = pdfs
    proj = _project(tmp_path, [
        {"citation": {"title": TITLE, "authors": ["Ada Lovelace"], "year": 2023, "pdf_url": "https://oa.example/p.pdf"},
         "pages": [{"number": 1, "text": "abstract"}]},
        {"citation": {"title": OTHER, "authors": ["Alan Turing"], "year": 2021},
         "pages": [{"number": 1, "text": "abstract"}], "fulltext": {"finder": FINDER_VERSION, "reason": "x"}}])

    def handler(req):
        if req.url.host == "oa.example":
            return httpx.Response(200, content=right)
        return httpx.Response(404)
    monkeypatch.setattr(src, "_client", lambda: _client(handler))
    lines = []
    got = upgrade_abstracts(proj, None, Reporter(lines.append))          # only papers this finder hasn't tried
    assert got["found"] == 1 and got["missed"] == 0
    left = json.loads((proj / ".tether" / "abstract_only.json").read_text())
    assert [a["citation"]["title"] for a in left] == [OTHER]
    assert len(list((proj / "Sources").glob("*.pdf"))) == 1
    known = json.loads((proj / ".tether" / "known_citations.json").read_text())
    assert any(c["title"] == TITLE for c in known.values())


def test_dropped_pdf_replaces_matching_abstract(tmp_path, pdfs):
    from tether.pipeline import replace_abstract_with
    right, wrong = pdfs
    proj = _project(tmp_path, [{"citation": {"title": TITLE, "authors": ["Ada Lovelace"], "year": 2023},
                                "pages": [{"number": 1, "text": "abstract"}]}])
    (proj / "Sources" / "unrelated.pdf").write_bytes(wrong)
    assert replace_abstract_with(proj, "unrelated.pdf") is None                  # not that paper: kept as is
    (proj / "Sources" / "mine.pdf").write_bytes(right)
    assert replace_abstract_with(proj, "mine.pdf") == TITLE
    assert json.loads((proj / ".tether" / "abstract_only.json").read_text()) == []


def test_attach_pdf_endpoint(tmp_path, pdfs, monkeypatch):
    """Attach PDF: you downloaded a paywalled paper yourself; the wrong file is caught first."""
    from fastapi.testclient import TestClient
    from tether import app as appmod, config
    right, wrong = pdfs
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(config, "_SHELL_ENV", {})
    monkeypatch.setattr(appmod.builder, "request", lambda *a, **k: None)      # no update in this test
    c = TestClient(appmod.app)
    (tmp_path / "V").mkdir()
    c.post("/api/vault", json={"path": str(tmp_path / "V")})
    proj = tmp_path / "V" / "Tether" / "Proj"
    (proj / ".tether").mkdir(parents=True)
    (proj / "Sources").mkdir()
    (proj / ".tether" / "abstract_only.json").write_text(json.dumps([
        {"citation": {"title": TITLE, "authors": ["Ada Lovelace"], "year": 2023},
         "pages": [{"number": 1, "text": "abstract"}]}]))

    r = c.post("/api/projects/Proj/attach", data={"title": TITLE}, files={"file": ("w.pdf", wrong, "application/pdf")})
    assert r.status_code == 409                                              # asks before attaching the wrong paper
    r = c.post("/api/projects/Proj/attach", data={"title": TITLE}, files={"file": ("p.pdf", right, "application/pdf")})
    assert r.status_code == 200 and r.json()["file"].startswith("Lovelace - 2023")
    assert json.loads((proj / ".tether" / "abstract_only.json").read_text()) == []

    r = c.post("/api/projects/Proj/fulltext", json={"titles": ["*"]})
    assert r.status_code == 400                                              # nothing left to look for
