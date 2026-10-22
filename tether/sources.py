"""Getting papers in: OpenAlex metadata, topic search, and URL downloads.

Citations are only marked `verified` when an external record's title matches the
paper closely. The LLM never writes bibliographic data.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import httpx
from rapidfuzz import fuzz

from .models import Citation, Page

OPENALEX = "https://api.openalex.org"
_HEADERS = {"User-Agent": "tether-litreview/0.3"}
TITLE_MATCH = 90


def _client() -> httpx.Client:
    return httpx.Client(headers=_HEADERS, timeout=40, follow_redirects=True)


def _params(**kw):
    return kw


def _citation_from_work(w: dict) -> Citation:
    doi = (w.get("doi") or "").replace("https://doi.org/", "") or None
    venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name")
    return Citation(
        title=w.get("title") or w.get("display_name") or "Untitled",
        authors=[a["author"]["display_name"] for a in w.get("authorships", []) if a.get("author")],
        year=w.get("publication_year"), venue=venue, doi=doi,
        url=(w.get("primary_location") or {}).get("landing_page_url") or w.get("id"),
        source="openalex", verified=True)


def _abstract(w: dict) -> str:
    inv = w.get("abstract_inverted_index") or {}
    words = sorted((pos, word) for word, positions in inv.items() for pos in positions)
    return " ".join(word for _, word in words)


def _pdf_url(w: dict) -> Optional[str]:
    for loc in [w.get("best_oa_location") or {}] + (w.get("locations") or []):
        if loc and loc.get("pdf_url"):
            return loc["pdf_url"]
    return None


def lookup_by_title(title: str) -> Optional[Citation]:
    try:
        with _client() as c:
            r = c.get(f"{OPENALEX}/works", params=_params(search=title, **{"per-page": 5}))
            r.raise_for_status()
    except httpx.HTTPError:
        return None
    for w in r.json().get("results", []):
        if fuzz.token_sort_ratio(title.lower(), (w.get("title") or "").lower()) >= TITLE_MATCH:
            return _citation_from_work(w)
    return None


def lookup_by_doi(doi: str) -> Optional[dict]:
    try:
        with _client() as c:
            r = c.get(f"{OPENALEX}/works/https://doi.org/{doi}", params=_params())
            r.raise_for_status()
            return r.json()
    except httpx.HTTPError:
        return None


def search_topic(topic: str, n: int = 15, since: Optional[int] = None) -> list[dict]:
    filt = "is_oa:true,has_abstract:true,type:article|preprint"
    if since:
        filt += f",from_publication_date:{since}-01-01"
    with _client() as c:
        r = c.get(f"{OPENALEX}/works", params=_params(search=topic, filter=filt, **{"per-page": n}))
        r.raise_for_status()
    return r.json().get("results", [])


def download(url: str, dest: Path) -> Optional[Path]:
    try:
        with _client() as c:
            r = c.get(url)
            r.raise_for_status()
    except httpx.HTTPError:
        return None
    if not r.content.startswith(b"%PDF"):
        return None
    dest.write_bytes(r.content)
    return dest


def resolve_work(work: dict, cache_dir: Path) -> tuple[Citation, Optional[Path], Optional[list[Page]]]:
    """Returns (citation, pdf_path or None, abstract pages or None)."""
    cit = _citation_from_work(work)
    slug = re.sub(r"\W+", "_", (work.get("id") or cit.title).split("/")[-1])[:60]
    url = _pdf_url(work)
    if url:
        pdf = download(url, cache_dir / f"{slug}.pdf")
        if pdf:
            return cit, pdf, None
    abstract = _abstract(work)
    return cit, None, ([Page(1, abstract)] if abstract else None)


def resolve_url(url: str, cache_dir: Path) -> tuple[Optional[Citation], Optional[Path], Optional[list[Page]]]:
    """arXiv links, doi.org links, or direct PDF links."""
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([\w.\-/]+?)(?:v\d+)?(?:\.pdf)?$", url)
    if m:
        aid = m.group(1)
        pdf = download(f"https://arxiv.org/pdf/{aid}", cache_dir / f"arxiv_{aid.replace('/', '_')}.pdf")
        w = lookup_by_doi(f"10.48550/arXiv.{aid}")
        return (_citation_from_work(w) if w else None), pdf, None
    m = re.search(r"(10\.\d{4,9}/[^\s?#]+)", url)
    if m:
        w = lookup_by_doi(m.group(1))
        if w:
            return resolve_work(w, cache_dir)
    slug = re.sub(r"\W+", "_", url.split("/")[-1])[:60] or "download"
    return None, download(url, cache_dir / f"{slug}.pdf"), None
