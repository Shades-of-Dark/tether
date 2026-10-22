"""Deterministic quote verification.

This is the part the LLM cannot talk its way past: every claim must come with a
quote, and the quote must actually be found in the paper's text by plain string
matching. If it isn't there, the claim is thrown out. The quote shown in the vault
is cut from the PDF's own text, not copied from the model's output.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

from rapidfuzz import fuzz

from .models import Page

_CHARMAP = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
            "‘": "'", "’": "'", "“": '"', "”": '"',
            "–": "-", "—": "-", "−": "-", "­": ""}

MIN_QUOTE_WORDS = 6
MATCH_THRESHOLD = 90.0


def normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Lowercased, ligature-free, de-hyphenated, whitespace-collapsed text, plus for each
    output character the index of the original character it came from."""
    chars: list[tuple[str, int]] = []
    for i, ch in enumerate(text):
        rep = _CHARMAP.get(ch)
        if rep is None:
            rep = unicodedata.normalize("NFKC", ch)
        chars += [(c, i) for c in rep]

    out: list[tuple[str, int]] = []
    n = len(chars)
    k = 0
    while k < n:
        c, i = chars[k]
        if c == "-" and out and out[-1][0].isalnum():
            j = k + 1
            saw_newline = False
            while j < n and chars[j][0].isspace():
                saw_newline |= chars[j][0] == "\n"
                j += 1
            if saw_newline and j < n and chars[j][0].isalnum():   # "exam-\nple" -> "example"
                k = j
                continue
        if c.isspace():
            if out and out[-1][0] != " ":
                out.append((" ", i))
            k += 1
            continue
        out.append((c.lower(), i))
        k += 1
    while out and out[-1][0] == " ":
        out.pop()
    return "".join(c for c, _ in out), [i for _, i in out]


def normalize(text: str) -> str:
    return normalize_with_map(text)[0]


@dataclass
class Match:
    page: int
    score: float
    span: str          # the source's own text that matched, original casing, whole words


def _original_span(text: str, origins: list[int], start: int, end: int) -> str:
    a, b = origins[start], origins[max(start, end - 1)] + 1
    while a > 0 and text[a - 1].isalnum():          # snap outward to whole words
        a -= 1
    while b < len(text) and text[b].isalnum():
        b += 1
    span = text[a:b]
    span = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", span)
    return re.sub(r"\s+", " ", span).strip()


def find_quote(quote: str, pages: list[Page], hint_page: Optional[int] = None) -> Optional[Match]:
    """Search the hinted page first, then the whole paper. Returns None if not found."""
    nq = normalize(quote).strip(" .\"'")
    if len(nq.split()) < MIN_QUOTE_WORDS:
        return None
    order = sorted(pages, key=lambda p: (p.number != hint_page, p.number))
    best: Optional[Match] = None
    for page in order:
        npage, origins = normalize_with_map(page.text)
        i = npage.find(nq)
        if i >= 0:
            return Match(page.number, 100.0, _original_span(page.text, origins, i, i + len(nq)))
        if len(npage) < len(nq):
            continue
        al = fuzz.partial_ratio_alignment(nq, npage, score_cutoff=MATCH_THRESHOLD)
        if al and al.score >= MATCH_THRESHOLD and (best is None or al.score > best.score):
            span = _original_span(page.text, origins, al.dest_start, al.dest_end)
            if _same_facts(nq, normalize(span)):
                best = Match(page.number, al.score, span)
    return best


_NEGATIONS = {"not", "no", "never", "none", "neither", "nor", "cannot", "without", "fails", "failed", "unable"}


def _same_facts(a: str, b: str) -> bool:
    """A near-identical string can still be a different claim: '31%' vs '13%', 'did' vs 'did not'.
    Fuzzy matches must agree exactly on numbers and negations."""
    nums = lambda s: sorted(re.findall(r"\d+(?:\.\d+)?", s))
    negs = lambda s: sorted(w for w in re.findall(r"[a-z']+", s.replace("n't", " not")) if w in _NEGATIONS)
    return nums(a) == nums(b) and negs(a) == negs(b)


def context_around(span: str, pages: list[Page], page_no: int, chars: int = 600) -> str:
    """The text surrounding a verified span, handed to the entailment judge."""
    page = next((p for p in pages if p.number == page_no), None)
    if page is None:
        return span
    flat = re.sub(r"\s+", " ", re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", page.text))
    i = flat.lower().find(span.lower()[:40])
    if i < 0:
        return span
    return flat[max(0, i - chars): i + len(span) + chars]
