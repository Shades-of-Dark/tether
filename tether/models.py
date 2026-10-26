"""Core data types. Everything the vault shows traces back to one of these."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Citation:
    """Bibliographic metadata. Comes from OpenAlex / PDF metadata, NEVER from the LLM."""
    title: str
    authors: list[str] = field(default_factory=list)
    year: Optional[int] = None
    venue: Optional[str] = None
    doi: Optional[str] = None
    url: Optional[str] = None
    source: str = "unknown"          # "openalex" | "pdf-metadata" | "filename"
    verified: bool = False           # True only when matched against an external record
    year_suffix: str = ""            # "a"/"b" when one author group has several papers in a year
    zotero_key: Optional[str] = None # set when the citation came from your Zotero library
    pdf_url: Optional[str] = None    # where the free PDF was found (linked from the Zotero item tether creates)

    def short(self) -> str:
        if not self.authors:
            who = self.title.split(":")[0][:40]
        else:
            last = self.authors[0].split()[-1]
            who = last if len(self.authors) == 1 else (
                f"{last} & {self.authors[1].split()[-1]}" if len(self.authors) == 2 else f"{last} et al.")
        return f"{who}, {self.year or 'n.d.'}{self.year_suffix}"

    def apa(self) -> str:
        def fmt(name: str) -> str:
            parts = name.split()
            if len(parts) < 2:
                return name
            return f"{parts[-1]}, {' '.join(p[0] + '.' for p in parts[:-1])}"
        authors = ", ".join(fmt(a) for a in self.authors[:20]) or "Unknown author"
        s = f"{authors} ({self.year or 'n.d.'}{self.year_suffix}). {self.title}."
        if self.venue:
            s += f" *{self.venue}*."
        if self.doi:
            s += f" https://doi.org/{self.doi}"
        elif self.url:
            s += f" {self.url}"
        return s


@dataclass
class Page:
    number: int      # 1-based, as printed in the PDF viewer
    text: str


@dataclass
class Paper:
    id: str                          # P01, P02, ...
    citation: Citation
    pages: list[Page]
    source_kind: str = "fulltext"    # "fulltext" | "abstract-only"
    path: Optional[str] = None
    legacy_key: str = ""             # cache key from tether <= 0.7, so old verified claims are reused

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d) -> "Paper":
        return Paper(id=d["id"], citation=Citation(**d["citation"]),
                     pages=[Page(**p) for p in d["pages"]],
                     source_kind=d.get("source_kind", "fulltext"), path=d.get("path"))


@dataclass
class Evidence:
    claim: str                       # claim id
    stance: str                      # supports | qualifies | contradicts


@dataclass
class IdeaLink:
    to: str                          # idea id
    kind: str                        # builds_on | in_tension | related
    why: str


@dataclass
class Idea:
    """A central idea woven from several papers' verified claims. The statement is checked
    against its evidence; it may only say what those claims say."""
    id: str                          # I-01
    title: str
    statement: str
    evidence: list[Evidence]
    links: list[IdeaLink] = field(default_factory=list)
    check: str = "supported"         # supported | partial
    scope: str = ""                  # qualifier added when the check found the statement too broad
    narrowed_from: str = ""          # the original statement, when the check replaced it with a narrower one


@dataclass
class Claim:
    id: str                          # C-0001
    paper_id: str
    text: str                        # the claim, in the model's words
    kind: str                        # finding | method | limitation | hypothesis | definition
    quote: str                       # the VERBATIM span pulled from the source text (not the LLM's copy)
    page: int                        # page where the span was actually found
    match_score: float               # 0-100 fuzzy score of the LLM quote vs the source
    entailment: str                  # supported | partial
    qualifiers: str = ""             # hedges / conditions the claim depends on
    concepts: list[str] = field(default_factory=list)


@dataclass
class Rejected:
    paper_id: str
    text: str
    quote: str
    reason: str


@dataclass
class Relation:
    source: str                      # claim id
    target: str                      # claim id
    kind: str                        # supports | contradicts | extends
    rationale: str
