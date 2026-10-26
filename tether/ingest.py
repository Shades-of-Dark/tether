"""Turn PDFs into page-numbered text, and pages into overlapping chunks."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from .models import Citation, Page


@dataclass
class Chunk:
    paper_id: str
    pages: list[int]
    text: str        # page markers inline: "[[page 4]] ..."


_STOP_SECTIONS = re.compile(r"^\s*(references|bibliography|acknowledg(e)?ments?)\s*$", re.I | re.M)


def read_pdf(path: Path) -> tuple[list[Page], dict]:
    doc = pymupdf.open(path)
    pages = []
    for i, page in enumerate(doc):
        text = page.get_text("text")
        m = _STOP_SECTIONS.search(text)
        if m and i > len(doc) * 0.4:       # drop everything after the reference list starts
            text = text[: m.start()]
            if text.strip():
                pages.append(Page(i + 1, text))
            break
        pages.append(Page(i + 1, text))
    meta = doc.metadata or {}
    first = doc[0].get_text("dict") if len(doc) else {"blocks": []}
    doc.close()
    return pages, {"meta": meta, "first_page": first}


# Sections that cost money to read but rarely hold the paper's own contributions.
_SKIP = re.compile(r"^(related work|related works|prior work|previous work|background|literature review|"
                   r"related literature|acknowledg(e)?ments?|appendix|appendices|supplementary( material)?|"
                   r"author contributions|funding|conflicts? of interest|ethics statement|"
                   r"data availability|code availability)\b", re.I)
_KEEP = re.compile(r"^(abstract|introduction|method|methods|methodology|approach|model|"
                   r"system( design| overview)?|experiment|experiments|experimental setup|evaluation|results|"
                   r"discussion|conclusion|conclusions|limitations|future work|analysis|implementation|"
                   r"problem (statement|formulation)|preliminaries|overview|case study|findings)\b", re.I)
_HEADING = re.compile(r"^\s*(?:(?:\d+(?:\.\d+)*|[IVXLC]+|[A-H])[.)]?\s+)?([A-Za-z][A-Za-z &,:/-]{2,60}?)\s*:?\s*$")


def _heading(line: str) -> str | None:
    """A short line that looks like a section heading ('2 Related Work', 'IV. RESULTS')."""
    if len(line.strip()) > 70 or len(line.split()) > 8:
        return None
    m = _HEADING.match(line)
    if not m:
        return None
    name = m.group(1).strip()
    if not (name.isupper() or name.istitle() or re.match(r"^\s*(\d|[IVXLC]+\.)", line)):
        return None
    return name


def drop_low_value_sections(pages: list[Page]) -> tuple[list[Page], list[str]]:
    """Remove related work, background, appendices etc. Returns (pages, names of sections dropped).
    Conservative: only skips a section when its heading is clearly recognized, stops skipping at
    the next recognized content heading, and gives up (keeps everything) if the result would
    lose more than 45% of the paper."""
    out, dropped, skipping = [], [], False
    for pg in pages:
        kept_lines = []
        for line in pg.text.splitlines():
            h = _heading(line)
            if h:
                if _SKIP.match(h):
                    skipping = True
                    dropped.append(h)
                    continue
                if _KEEP.match(h):
                    skipping = False
            if not skipping:
                kept_lines.append(line)
        out.append(Page(pg.number, "\n".join(kept_lines)))
    before = sum(len(p.text) for p in pages)
    after = sum(len(p.text) for p in out)
    if before and after < before * 0.55:
        return pages, []
    return [p for p in out if p.text.strip()], list(dict.fromkeys(dropped))


def guess_title(info: dict, path: Path) -> str:
    title = (info.get("meta") or {}).get("title") or ""
    if len(title.split()) >= 3 and not title.lower().endswith((".pdf", ".docx", ".tex")):
        return title.strip()
    # fall back to the largest text on page 1
    spans = []
    for block in info.get("first_page", {}).get("blocks", []):
        for line in block.get("lines", []):
            for s in line.get("spans", []):
                if s["text"].strip():
                    spans.append((round(s["size"], 1), s["text"].strip()))
    if spans:
        top = max(size for size, _ in spans)
        t = " ".join(text for size, text in spans if size == top)
        if 3 <= len(t.split()) <= 40:
            return t
    return path.stem.replace("_", " ").replace("-", " ")


def citation_from_pdf(info: dict, path: Path) -> Citation:
    meta = info.get("meta") or {}
    authors = [a.strip() for a in re.split(r";|, and | and ", meta.get("author") or "") if a.strip()]
    year = None
    m = re.search(r"(19|20)\d{2}", meta.get("creationDate") or "")
    if m:
        year = int(m.group(0))
    return Citation(title=guess_title(info, path), authors=authors, year=year,
                    source="pdf-metadata", verified=False)


def chunk_pages(paper_id: str, pages: list[Page], max_chars: int = 9000, overlap: int = 800) -> list[Chunk]:
    chunks: list[Chunk] = []
    buf, nums = "", []
    for p in pages:
        piece = f"\n[[page {p.number}]]\n{p.text}"
        if buf and len(buf) + len(piece) > max_chars:
            chunks.append(Chunk(paper_id, nums, buf))
            tail = buf[-overlap:]
            buf, nums = tail, nums[-1:]
        buf += piece
        nums.append(p.number)
    if buf.strip():
        chunks.append(Chunk(paper_id, nums, buf))
    return chunks


def quick_citation(path: Path) -> Citation:
    """Title / authors / year from a PDF's metadata and first page only (fast; no full read)."""
    try:
        doc = pymupdf.open(path)
        info = {"meta": doc.metadata or {}, "first_page": doc[0].get_text("dict") if len(doc) else {"blocks": []}}
        doc.close()
        return citation_from_pdf(info, path)
    except Exception:
        return Citation(title=Path(path).stem)
