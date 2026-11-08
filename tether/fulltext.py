"""Finding the full text of a paper, not just its abstract.

An abstract is a condensed, sometimes inaccurate stand-in for a paper, so tether works hard to get
the PDF. Most "abstract only" papers are actually free to read: the publisher's site just doesn't
hand a PDF to a script on the first request (an HTML viewer page, a cookie check, an iframe). This
module tries, in order, until one gives a real PDF of the right paper:

1. your Zotero library: a PDF already attached to the same paper (Zotero's "Find Available PDF"
   is good at publishers that resist scripts);
2. every free-PDF location OpenAlex knows;
3. the paper's landing pages (DOI link, publisher page, repository pages), reading the
   `citation_pdf_url` tag most publishers put there, PDF links and viewer iframes, and the
   publisher-specific routes for IEEE Xplore, MDPI, Hindawi/Wiley and ScienceDirect;
4. an arXiv copy (by arXiv id, or by searching arXiv for the exact title);
5. Semantic Scholar's open-access PDF;
6. Europe PMC / PubMed Central.

Each download goes through one session that keeps cookies and sends the page it came from as the
referrer, like a browser following a link. Every PDF found is checked against the paper's title
(it must appear on the first pages), so a supplement, a cover page or another paper is never taken
for it. Paywalled papers with no free copy anywhere stay abstract-only: tether never gets around a
paywall. For those, open the paper's page, download it with your own access, and attach it.
"""
from __future__ import annotations

import html
import re
import shutil
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin, urlparse

import httpx
from rapidfuzz import fuzz

from .models import Citation

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": BROWSER_UA,
           "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
           "Accept-Language": "en-US,en;q=0.9"}
OPENALEX = "https://api.openalex.org"
MAX_FETCHES = 18              # per paper: enough for every route, bounded so one paper can't stall an update
MAX_BYTES = 80 * 1024 * 1024
TITLE_OK = 85


@dataclass
class Result:
    path: Optional[Path] = None
    url: Optional[str] = None
    how: str = ""                          # which route found it (shown in the log)
    tried: list[str] = field(default_factory=list)
    blocked: int = 0                       # publisher pages that refused us (403/429/captcha)
    reason: str = ""                       # why nothing was found, in plain words


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def title_matches(pdf: Path, title: str) -> bool:
    """The paper's title has to appear on its first two pages (allowing for line breaks and hyphens)."""
    if len(_norm(title).split()) < 3:
        return True
    try:
        import pymupdf
        doc = pymupdf.open(pdf)
        text = " ".join(doc[i].get_text("text") for i in range(min(2, len(doc))))
        meta = (doc.metadata or {}).get("title") or ""
        doc.close()
    except Exception:
        return False
    flat = _norm(re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text))
    t = _norm(title)
    return (t in flat or fuzz.partial_ratio(t, flat[:6000]) >= TITLE_OK
            or (meta and fuzz.token_sort_ratio(t, _norm(meta)) >= 90))


# ---------------------------------------------------------------- reading a landing page
_META_PDF = re.compile(r'<meta[^>]+name=["\'](?:citation_pdf_url|eprints\.document_url|bepress_citation_pdf_url)["\'][^>]*>', re.I)
_CONTENT = re.compile(r'content=["\']([^"\']+)["\']', re.I)


def pdf_links_in(page: str, base: str) -> list[str]:
    """PDF links a publisher page offers, best first."""
    out: list[str] = []

    def add(u: str) -> None:
        u = html.unescape(u.strip())
        if u and not u.startswith(("javascript:", "#", "mailto:")):
            u = urljoin(base, u)
            if u not in out:
                out.append(u)
    for tag in _META_PDF.findall(page):                       # the standard Highwire tag
        m = _CONTENT.search(tag)
        if m:
            add(m.group(1))
    for m in re.finditer(r'<link[^>]+type=["\']application/pdf["\'][^>]*href=["\']([^"\']+)', page, re.I):
        add(m.group(1))
    for m in re.finditer(r'<(?:iframe|embed|object)[^>]+(?:src|data)=["\']([^"\']+)["\']', page, re.I):
        if "pdf" in m.group(1).lower():                         # viewer pages wrap the PDF in a frame
            add(m.group(1))
    # IEEE Xplore keeps the PDF route in page data
    for m in re.finditer(r'"pdfUrl"\s*:\s*"([^"]+)"', page):
        add(m.group(1))
    for m in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.{0,200}?)</a>', page, re.I | re.S):
        href, text = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2)).lower()
        h = href.lower()
        if (h.endswith(".pdf") or "/pdf" in h or "pdfft" in h or "download" in h and "pdf" in (h + text)) \
                and not any(x in h for x in ("supplement", "suppl", "figure", "poster", "slides")) \
                and ("pdf" in text or "download" in text or h.endswith(".pdf")):
            add(href)
    return out[:8]


def publisher_routes(url: str) -> list[str]:
    """Known PDF routes for publishers whose pages don't link the PDF plainly."""
    out = []
    host = urlparse(url).netloc.lower()
    if "ieeexplore.ieee.org" in host:
        m = re.search(r"(?:document/|arnumber=)(\d+)", url)
        if m:
            n = m.group(1)
            out += [f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={n}&ref=",
                    f"https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber={n}"]
    if "mdpi.com" in host and not url.rstrip("/").endswith("/pdf") and re.search(r"mdpi\.com/[\d-]+x?/\d+/\d+/\d+", url):
        out.append(url.split("?")[0].rstrip("/") + "/pdf")
    if "hindawi.com" in host or "onlinelibrary.wiley.com" in host:
        m = re.search(r"(10\.\d{4,9}/[^\s?#]+)", url)
        if m:
            out.append(f"https://onlinelibrary.wiley.com/doi/pdfdirect/{m.group(1)}")
    if "sciencedirect.com" in host:
        m = re.search(r"/pii/(S?[0-9X]+)", url)
        if m:
            out.append(f"https://www.sciencedirect.com/science/article/pii/{m.group(1)}/pdfft?isDTMRedir=true&download=true")
    if "link.springer.com" in host:
        m = re.search(r"link\.springer\.com/(?:article|chapter)/(10\.[^?#]+)", url)
        if m:
            out.append(f"https://link.springer.com/content/pdf/{m.group(1)}.pdf")
    if "frontiersin.org" in host and "/full" in url:
        out.append(url.replace("/full", "/pdf"))
    return out


# ---------------------------------------------------------------- the search
class _Fetcher:
    def __init__(self, client: httpx.Client, dest: Path, title: str, res: Result):
        self.c, self.dest, self.title, self.res = client, dest, title, res
        self.seen: set[str] = set()
        self.queue: list[tuple[str, Optional[str], str, int]] = []      # (url, referrer, route, depth)

    def push(self, url: Optional[str], route: str, referrer: Optional[str] = None, depth: int = 0, front: bool = False):
        if not url or url in self.seen:
            return
        item = (url, referrer, route, depth)
        self.queue.insert(0, item) if front else self.queue.append(item)

    def run(self) -> bool:
        while self.queue and len(self.seen) < MAX_FETCHES:
            url, ref, route, depth = self.queue.pop(0)
            if url in self.seen:
                continue
            self.seen.add(url)
            self.res.tried.append(url)
            headers = {"Referer": ref} if ref else {}
            try:
                with self.c.stream("GET", url, headers=headers) as r:
                    if r.status_code in (401, 403, 429, 503):
                        self.res.blocked += 1
                        continue
                    if r.status_code >= 400:
                        continue
                    ctype = r.headers.get("content-type", "").lower()
                    body = b""
                    for chunk in r.iter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            break
                    final = str(r.url)
            except httpx.HTTPError:
                continue
            if body.lstrip()[:5].startswith(b"%PDF"):
                tmp = self.dest.with_suffix(".part")
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(body)
                if title_matches(tmp, self.title):
                    tmp.replace(self.dest)
                    self.res.path, self.res.url, self.res.how = self.dest, url, route
                    return True
                tmp.unlink(missing_ok=True)                        # a PDF, but not this paper
                continue
            if depth >= 2 or ("html" not in ctype and not body.lstrip()[:1] == b"<"):
                continue
            page = body[:3_000_000].decode("utf-8", "replace")
            if re.search(r"captcha|cf-chl|are you a robot|access denied|just a moment", page[:20000], re.I):
                self.res.blocked += 1
            # links the page offers come first: they're the publisher's own route to this paper
            for u in reversed(publisher_routes(final) + pdf_links_in(page, final)):
                self.push(u, route, referrer=final, depth=depth + 1, front=True)
        return False


def _arxiv_by_title(c: httpx.Client, title: str) -> Optional[str]:
    words = [w for w in re.findall(r"[A-Za-z0-9]+", title) if len(w) > 2][:12]
    if len(words) < 3:
        return None
    q = " AND ".join(f"ti:{w}" for w in words)
    try:
        r = c.get("https://export.arxiv.org/api/query", params={"search_query": q, "max_results": 5})
        r.raise_for_status()
        root = ET.fromstring(r.text)
    except (httpx.HTTPError, ET.ParseError):
        return None
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for e in root.findall("a:entry", ns):
        t = " ".join((e.findtext("a:title", "", ns) or "").split())
        if fuzz.token_sort_ratio(_norm(t), _norm(title)) >= 92:
            aid = (e.findtext("a:id", "", ns) or "").rsplit("/abs/", 1)[-1]
            return re.sub(r"v\d+$", "", aid) or None
    return None


def _semantic_scholar(c: httpx.Client, doi: Optional[str], title: str) -> tuple[Optional[str], Optional[str]]:
    """(open-access PDF url, arXiv id) from Semantic Scholar, by DOI or exact title."""
    fields = "title,openAccessPdf,externalIds"
    try:
        if doi:
            r = c.get(f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}", params={"fields": fields})
        else:
            r = c.get("https://api.semanticscholar.org/graph/v1/paper/search/match", params={"query": title, "fields": fields})
        if r.status_code != 200:
            return None, None
        d = r.json()
        d = (d.get("data") or [d])[0] if "data" in d else d
    except (httpx.HTTPError, ValueError, IndexError):
        return None, None
    if d.get("title") and fuzz.token_sort_ratio(_norm(d["title"]), _norm(title)) < 85:
        return None, None
    pdf = (d.get("openAccessPdf") or {}).get("url") or None
    return pdf, (d.get("externalIds") or {}).get("ArXiv")


def _europe_pmc(c: httpx.Client, doi: Optional[str]) -> Optional[str]:
    if not doi:
        return None
    try:
        r = c.get("https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                  params={"query": f'DOI:"{doi}"', "format": "json", "resultType": "lite"})
        r.raise_for_status()
        hits = r.json().get("resultList", {}).get("result", [])
    except (httpx.HTTPError, ValueError):
        return None
    pmcid = next((h.get("pmcid") for h in hits if h.get("pmcid")), None)
    return f"https://europepmc.org/articles/{pmcid}?pdf=render" if pmcid else None


def _openalex(c: httpx.Client, doi: Optional[str], title: str) -> Optional[dict]:
    try:
        if doi:
            r = c.get(f"{OPENALEX}/works/https://doi.org/{doi}")
        else:
            r = c.get(f"{OPENALEX}/works", params={"search": title, "per-page": 3})
        if r.status_code != 200:
            return None
        d = r.json()
        if "results" in d:
            d = next((w for w in d["results"] if fuzz.token_sort_ratio(_norm(w.get("title") or ""), _norm(title)) >= 92), None)
        return d
    except (httpx.HTTPError, ValueError):
        return None


def find_pdf(cit: Citation, dest: Path, work: Optional[dict] = None, zotero=None,
             client: Optional[httpx.Client] = None, say: Callable[[str], None] = lambda s: None) -> Result:
    """Try every route for the full text of `cit`; the PDF lands at `dest` (checked to be this paper)."""
    res = Result()
    title = cit.title or ""
    doi = (cit.doi or "").lower() or None
    dest.parent.mkdir(parents=True, exist_ok=True)

    # 1. Zotero already has it
    if zotero is not None:
        try:
            it = zotero.find(title, doi)
            pdf = getattr(it, "pdf", None) if it else None
            if pdf and Path(pdf).exists():
                shutil.copyfile(pdf, dest)
                if title_matches(dest, title):
                    res.path, res.how = dest, "your Zotero library"
                    return res
                dest.unlink(missing_ok=True)
        except Exception:
            pass

    own = client is None
    if client is None:
        from . import sources                    # one place to swap the HTTP client (tests do)
        client = sources._client()
    c = client
    try:
        f = _Fetcher(c, dest, title, res)
        # 2. OpenAlex's free copies, and 3. landing pages
        say("checking the free copies OpenAlex lists")
        work = work or ((doi or title) and _openalex(c, doi, title)) or None
        landing = []
        if work:
            for loc in [work.get("best_oa_location") or {}] + (work.get("locations") or []):
                f.push((loc or {}).get("pdf_url"), "a free copy OpenAlex lists")
                if (loc or {}).get("landing_page_url"):
                    landing.append(loc["landing_page_url"])
        if cit.pdf_url:
            f.push(cit.pdf_url, "the PDF link it came with")
        if doi:
            landing.insert(0, f"https://doi.org/{doi}")
        if cit.url and cit.url.startswith("http"):
            landing.append(cit.url)
        for u in dict.fromkeys(landing):
            for r in publisher_routes(u):
                f.push(r, "the publisher's site")
            f.push(u, "the publisher's page")
        if f.run():
            return res
        if not title and not doi:                # nothing to search other sources by (a bare link)
            res.reason = res.reason or ("the site blocked the download" if res.blocked else "no PDF on that page")
            return res
        # 4. arXiv, 5. Semantic Scholar, 6. Europe PMC
        say("looking for an arXiv or repository copy")
        s2_pdf, s2_arxiv = _semantic_scholar(c, doi, title)
        arx = None
        m = re.search(r"10\.48550/arxiv\.(.+)$", doi or "")
        arx = (m.group(1) if m else None) or s2_arxiv or _arxiv_by_title(c, title)
        if arx:
            f.push(f"https://arxiv.org/pdf/{arx}", "an arXiv copy")
        f.push(s2_pdf, "Semantic Scholar's open-access copy")
        f.push(_europe_pmc(c, doi), "PubMed Central")
        if f.run():
            return res
    finally:
        if own:
            c.close()
    res.reason = ("the publisher's site blocked the download" if res.blocked
                  else "no free copy found (it may be paywalled)" if res.tried
                  else "no link to try")
    return res
