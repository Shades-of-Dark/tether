"""Following the citations behind claims a paper only reports from other work.

When a paper says "Smith et al. [12] showed X", tether never credits X to that paper (the claim
is rejected as secondhand). But [12] may be a paper worth reading, so this module:

1. reads the paper's own reference list (the part of the PDF after "References"),
2. finds the citation marker in or right after the quote ("[12]", "(12)", "Smith et al., 2019"),
3. matches it to the reference entry, and
4. looks that entry up (Crossref's bibliographic matcher, then OpenAlex for a free PDF).

All of it is code and public metadata lookups: no Claude calls, so it costs nothing. Results are
cached per project, so each reference is looked up once.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import httpx
from rapidfuzz import fuzz

from . import sources
from .models import Paper, Rejected
from .textmatch import find_quote

_REF_HEADING = re.compile(r"^\s*(?:\d+\.?\s*|[IVX]+\.\s*)?(references|bibliography|works cited|literature cited)\s*$",
                          re.I | re.M)
_AFTER_REFS = re.compile(r"^\s*(?:[A-H]\.?\s+|appendix\b|supplementary\b)", re.I)
CROSSREF = "https://api.crossref.org/works"
MAX_LOOKUPS = 80                      # per update; the rest are looked up next time (cached)


@dataclass
class CitedWork:
    """A paper that claims in this project rely on, as found in a paper's reference list."""
    key: str                                   # stable id: doi, openalex id, or a hash of the reference text
    title: str
    authors: list[str] = field(default_factory=list)
    year: Optional[int] = None
    venue: Optional[str] = None
    doi: Optional[str] = None
    url: Optional[str] = None
    openalex: Optional[str] = None             # W123...
    free_pdf: bool = False
    reference: str = ""                        # the reference entry as printed
    cited_by: list[dict] = field(default_factory=list)   # [{paper, claim, quote, page}]


# ---------------------------------------------------------------- the reference list
def reference_text(pdf: Path) -> str:
    """Everything after the last 'References' heading in the back half of the PDF."""
    import pymupdf

    try:
        doc = pymupdf.open(pdf)
    except Exception:
        return ""
    try:
        texts = [pg.get_text("text") for pg in doc]
    finally:
        doc.close()
    start = None
    for i in range(len(texts) - 1, -1, -1):
        if i < len(texts) * 0.4:
            break
        m = None
        for m in _REF_HEADING.finditer(texts[i]):
            pass
        if m:
            start = (i, m.end())
            break
    if start is None:
        return ""
    i, pos = start
    body = texts[i][pos:] + "\n" + "\n".join(texts[i + 1:])
    return body


def split_references(text: str) -> tuple[str, list[tuple[Optional[int], str]]]:
    """('numeric' | 'author-year', [(number or None, entry text)])."""
    flat = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    numbered = list(re.finditer(r"(?:^|\n)\s*\[(\d{1,3})\]\s*", flat))
    if len(numbered) >= 5:
        out = []
        for a, b in zip(numbered, numbered[1:] + [None]):
            entry = flat[a.end(): b.start() if b else len(flat)]
            out.append((int(a.group(1)), _clean(entry)))
        return "numeric", out
    dotted = list(re.finditer(r"(?:^|\n)\s*(\d{1,3})\.\s+(?=[A-Z])", flat))
    if len(dotted) >= 5 and [int(m.group(1)) for m in dotted[:3]] == [1, 2, 3]:
        out = []
        for a, b in zip(dotted, dotted[1:] + [None]):
            out.append((int(a.group(1)), _clean(flat[a.end(): b.start() if b else len(flat)])))
        return "numeric", out
    # author-year: an entry starts on a line beginning "Surname, X." (or "Surname X,"), once the
    # entry before it already has its year. Publisher styles differ in how entries end (a period,
    # a DOI, a URL), so the year is the reliable boundary, not the punctuation.
    start = re.compile(r"^\s*(?:van |von |de |da |del |le |la )?[A-Z][A-Za-z'\-]+(?:[ -][A-Z][A-Za-z'\-]+)?,\s+(?:[A-Z]\.\s?-?)+")
    entries, cur = [], ""
    for line in flat.splitlines():
        if start.match(line) and re.search(r"\b(19|20)\d{2}[a-z]?\b", cur):
            entries.append(cur)
            cur = line
        else:
            cur += " " + line
    if cur.strip():
        entries.append(cur)
    return "author-year", [(None, _clean(e)) for e in entries if re.search(r"(19|20)\d{2}", e)]


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s[:600]


# ---------------------------------------------------------------- markers in a claim
_NUM = re.compile(r"\[(\d{1,3}(?:\s*[,–\-]\s*\d{1,3})*)\]|\((\d{1,3}(?:\s*[,–\-]\s*\d{1,3})*)\)")
_AY = re.compile(r"([A-Z][A-Za-z'\-]+)(?:\s+et\s+al\.?|\s+(?:and|&)\s+[A-Z][A-Za-z'\-]+)?,?\s*\(?((?:19|20)\d{2})[a-z]?\)?")


def _expand(spec: str) -> list[int]:
    out = []
    for part in re.split(r"\s*,\s*", spec):
        m = re.match(r"(\d+)\s*[–\-]\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if 0 < b - a < 30:
                out += list(range(a, b + 1))
        elif part.strip().isdigit():
            out.append(int(part))
    return out


def markers(text: str, style: str, n_refs: int) -> list:
    """Reference numbers (numeric style) or (surname, year) pairs found in `text`."""
    if style == "numeric":
        nums = []
        for m in _NUM.finditer(text):
            for n in _expand(m.group(1) or m.group(2)):
                if 1 <= n <= n_refs and n not in nums:
                    nums.append(n)
        return nums
    pairs = []
    for m in _AY.finditer(text):
        name, year = m.group(1), int(m.group(2))
        if name.lower() in ("in", "the", "this", "our", "table", "fig", "figure", "section", "eq"):
            continue
        if (name, year) not in pairs:
            pairs.append((name, year))
    return pairs


def match_entry(marker, style: str, refs: list[tuple[Optional[int], str]]) -> Optional[str]:
    if style == "numeric":
        for n, entry in refs:
            if n == marker:
                return entry
        return None
    name, year = marker
    first = re.compile(rf"^\s*(?:van |von |de |da |del |le |la )?{re.escape(name)}\b", re.I)
    hits = [e for _, e in refs if first.match(e) and re.search(rf"\b{year}[a-z]?\b", e)]
    return hits[0] if len(hits) == 1 else None          # ambiguous (2019a / 2019b): don't guess


# ---------------------------------------------------------------- looking a reference up
def _client() -> httpx.Client:
    return sources._client()


def _surname_ok(entry: str, authors: list[str]) -> bool:
    return any(a.split()[-1].lower() in entry.lower()[:200] for a in authors[:4] if a.split())


def resolve_reference(entry: str) -> Optional[dict]:
    """A reference entry -> {title, authors, year, venue, doi, url, openalex, free_pdf}, or None."""
    years = [int(y) for y in re.findall(r"\b((?:19|20)\d{2})\b", entry)]
    hit = None
    try:
        with _client() as c:
            r = c.get(CROSSREF, params={"query.bibliographic": entry[:400], "rows": 2,
                                        "select": "DOI,title,author,issued,container-title,score"})
            r.raise_for_status()
        for it in r.json().get("message", {}).get("items", []):
            title = (it.get("title") or [""])[0]
            authors = [" ".join(x for x in (a.get("given"), a.get("family")) if x) for a in it.get("author", [])]
            year = ((it.get("issued") or {}).get("date-parts") or [[None]])[0][0]
            if not title or fuzz.partial_ratio(title.lower(), entry.lower()) < 85:
                continue                                  # the title must actually appear in the entry
            if years and year and all(abs(year - y) > 1 for y in years):
                continue
            hit = {"title": title, "authors": authors, "year": year,
                   "venue": (it.get("container-title") or [None])[0], "doi": it.get("DOI")}
            break
    except (httpx.HTTPError, ValueError):
        pass
    work = sources.lookup_by_doi(hit["doi"]) if hit and hit.get("doi") else None
    if work is None:
        try:
            with _client() as c:
                r = c.get(f"{sources.OPENALEX}/works", params={"search": re.sub(r"[^\w\s]", " ", entry)[:300], "per-page": 3})
                r.raise_for_status()
            for w in r.json().get("results", []):
                t = w.get("title") or ""
                names = [a["author"]["display_name"] for a in w.get("authorships", []) if a.get("author")]
                if t and fuzz.partial_ratio(t.lower(), entry.lower()) >= 85 and (not names or _surname_ok(entry, names)):
                    work = w
                    break
        except (httpx.HTTPError, ValueError):
            work = None
    if work is not None:
        cit = sources._citation_from_work(work)
        return {"title": cit.title, "authors": cit.authors, "year": cit.year, "venue": cit.venue, "doi": cit.doi,
                "url": cit.url, "openalex": (work.get("id") or "").split("/")[-1] or None,
                "free_pdf": bool(sources._pdf_url(work))}
    if hit:
        return hit | {"url": f"https://doi.org/{hit['doi']}" if hit.get("doi") else None, "openalex": None, "free_pdf": False}
    return None


# ---------------------------------------------------------------- the whole step
def follow(papers: dict[str, Paper], rejected: list[Rejected], cache_dir: Path,
           log: Callable[[str], None] = print, now: Callable[[str], None] = lambda s: None,
           lookup: bool = True) -> list[CitedWork]:
    """Cited works behind every secondhand claim, most-relied-on first."""
    cache_file = cache_dir / "cited_cache.json"
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cache = {}
    refs_by_paper: dict[str, tuple[str, list]] = {}
    works: dict[str, CitedWork] = {}
    lookups = 0
    second = [r for r in rejected if r.reason.startswith("secondhand") and r.paper_id in papers]
    for n, r in enumerate(second, 1):
        p = papers[r.paper_id]
        if not p.path:
            continue
        if r.paper_id not in refs_by_paper:
            now(f"Reading the reference list of {p.citation.short()}")
            refs_by_paper[r.paper_id] = split_references(reference_text(Path(p.path)))
        style, refs = refs_by_paper[r.paper_id]
        if not refs:
            continue
        m = find_quote(r.quote, p.pages)
        page = m.page if m else None
        context = r.quote
        if m:                                             # markers often sit just after the sentence
            flat = re.sub(r"\s+", " ", next((pg.text for pg in p.pages if pg.number == m.page), ""))
            i = flat.lower().find(m.span.lower()[:40])
            if i >= 0:
                context = flat[i: i + len(m.span) + 80]
        for mk in markers(context, style, len(refs))[:4]:
            entry = match_entry(mk, style, refs)
            if not entry:
                continue
            ek = hashlib.sha256(entry.encode()).hexdigest()[:16]
            if ek not in cache and lookup and lookups < MAX_LOOKUPS:
                now(f"Looking up work cited by {p.citation.short()} ({n} of {len(second)} cited claims)")
                cache[ek] = resolve_reference(entry)
                lookups += 1
            meta = cache.get(ek)
            key = (meta or {}).get("doi") or (meta or {}).get("openalex") or ek
            key = str(key).lower()
            if key not in works:
                if meta:
                    works[key] = CitedWork(key=key, title=meta["title"], authors=meta.get("authors") or [],
                                           year=meta.get("year"), venue=meta.get("venue"), doi=meta.get("doi"),
                                           url=meta.get("url"), openalex=meta.get("openalex"),
                                           free_pdf=bool(meta.get("free_pdf")), reference=entry)
                else:
                    works[key] = CitedWork(key=key, title=entry[:160], reference=entry)
            cw = works[key]
            if not any(c["paper"] == r.paper_id and c["claim"] == r.text for c in cw.cited_by):
                cw.cited_by.append({"paper": r.paper_id, "claim": r.text, "quote": r.quote, "page": page})
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    out = sorted(works.values(), key=lambda w: (-len({c["paper"] for c in w.cited_by}), -len(w.cited_by), w.title))
    if out:
        found = sum(1 for w in out if w.year or w.doi)
        log(f"  Followed {len(second)} cited claims to {len(out)} cited works ({found} identified)")
    return out
