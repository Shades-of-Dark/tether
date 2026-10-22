"""The LLM stages. Each one is followed by a check the LLM can't fake:

1. extract     -> each quote must be found verbatim in the PDF text (textmatch.find_quote)
2. select      -> picks the ~12 most central claims per paper; may only return real claim ids
3. entail      -> a second, separate model call judges each claim against its real source span
4. synthesize  -> weaves claims into 8-20 central ideas; may only cite real, verified claim ids
5. check ideas -> a separate call checks every idea statement against its own evidence;
                  overreaching ideas are narrowed, unsupported ones are dropped
"""
from __future__ import annotations

import hashlib
import itertools
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from .ingest import Chunk
from .llm import LLM, dumps
from .models import Claim, Evidence, Idea, IdeaLink, Paper, Rejected
from .textmatch import context_around, find_quote

MAX_PER_CHUNK = 4
MAX_PER_PAPER = 12


def _h(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


# ---------------------------------------------------------------- 1. extract
EXTRACT_SYSTEM = f"""You extract the CENTRAL claims from one excerpt of ONE research paper, for a
literature review. A reader should be able to cite each claim as one of this paper's contributions.

Return at most {MAX_PER_CHUNK} claims for this excerpt, and only ones that matter: the paper's main
findings and results, its core method or design, and limitations the authors state. Return an empty
list if the excerpt is background, setup, notation, or a survey of other work. Never extract minor
implementation details, hyperparameters, figure captions, or restatements of the same point.

Rules (the output is checked by a program):
- `quote` must be copied character-for-character from the provided text: one contiguous
  span of 1-3 sentences, at least 8 words. No ellipses, no paraphrase, no stitching.
- `page` is the number in the nearest [[page N]] marker BEFORE the quote.
- Only extract what THIS paper's authors claim, propose, or found. If the text reports someone
  else's work ("Smith et al. showed..."), skip it, or if you include it set `secondhand` true.
- `text` restates the claim in one plain sentence, adding nothing the quote doesn't say. Keep
  hedges and conditions ("may", "in simulation", "for small samples").
- `qualifiers`: conditions, scope limits, or hedges the claim depends on ("" if none).
- `importance`: 3 = a headline result or the core method, 2 = a supporting result or key
  limitation, 1 = useful context. Prefer returning fewer, more important claims.

Worked example (a different field, to show the rules):
<paper_text>
[[page 6]]
In our cohort (n = 212), participants who consumed caffeine within six hours of bedtime
fell asleep 18 minutes later on average than those who did not. Prior work by Ruiz et
al. reported that this effect disappears in habitual users, which we did not observe.
Because sleep onset was self-reported, these estimates may be biased toward shorter latencies.
</paper_text>
Good claims:
1. text "Caffeine within six hours of bedtime delayed sleep onset by about 18 minutes on average in this cohort."
   kind finding · page 6 · importance 3 · secondhand false · qualifiers "n = 212; self-reported sleep onset"
   quote "participants who consumed caffeine within six hours of bedtime fell asleep 18 minutes later on average than those who did not"
2. text "Sleep onset was self-reported, which may bias latency estimates downward."
   kind limitation · page 6 · importance 2 · secondhand false · qualifiers ""
   quote "Because sleep onset was self-reported, these estimates may be biased toward shorter latencies"
Not extracted: the Ruiz et al. sentence (someone else's finding).
Mistakes to avoid: "Caffeine causes insomnia" (overgeneralizes; the quote says neither "causes" nor
"insomnia"); a quote that drops or reorders words; a quote stitched from two sentences with "...";
a page number taken from a marker AFTER the quote; five claims that all say the same thing."""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {"claims": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "kind": {"type": "string", "enum": ["finding", "method", "limitation", "hypothesis", "definition"]},
            "quote": {"type": "string"},
            "page": {"type": "integer"},
            "qualifiers": {"type": "string"},
            "importance": {"type": "integer"},
            "secondhand": {"type": "boolean"},
        },
        "required": ["text", "kind", "quote", "page", "qualifiers", "importance", "secondhand"],
    }}},
    "required": ["claims"],
}


def extract(llm: LLM, paper: Paper, chunks: list[Chunk], workers: int = 4) -> tuple[list[dict], list[Rejected]]:
    def one(chunk: Chunk):
        user = (f"Paper: {paper.citation.title}\n"
                f"{'(Only the abstract is available.)' if paper.source_kind == 'abstract-only' else ''}\n\n"
                f"<paper_text>\n{chunk.text}\n</paper_text>")
        got = llm.call(system=EXTRACT_SYSTEM, user=user, name="claims", schema=EXTRACT_SCHEMA, role="read")["claims"]
        return sorted(got, key=lambda c: -int(c.get("importance") or 1))[:MAX_PER_CHUNK]

    with ThreadPoolExecutor(workers) as ex:
        raw = list(itertools.chain.from_iterable(ex.map(one, chunks)))

    kept, rejected, seen = [], [], set()
    for c in raw:
        if c.get("secondhand"):
            rejected.append(Rejected(paper.id, c["text"], c["quote"], "secondhand: paper is citing other work"))
            continue
        m = find_quote(c["quote"], paper.pages, c.get("page"))
        if m is None:
            rejected.append(Rejected(paper.id, c["text"], c["quote"], "quote not found in paper text"))
            continue
        key = (m.page, m.span[:80].lower())
        if key in seen:                       # chunk overlap produces duplicates
            continue
        seen.add(key)
        kept.append({**c, "quote": m.span, "page": m.page, "match_score": m.score})
    return kept, rejected


# ---------------------------------------------------------------- 2. select
SELECT_SYSTEM = f"""You curate one research paper's claims for a literature review. You get the paper's
opening text (title/abstract) and a numbered list of candidate claims, each already checked to be
quoted verbatim from the paper.

Choose the claims a careful reviewer would cite from this paper: at most {MAX_PER_PAPER}, ideally 6-10.
- Cover the paper's headline contribution first, then its key supporting results, its core method,
  and the limitations the authors state.
- When several candidates say the same thing, keep only the clearest, most specific one.
- Drop minor details, setup, and anything that only makes sense inside the paper.
- Return the ids of the claims to keep, most important first. Use only ids from the list."""

SELECT_SCHEMA = {
    "type": "object",
    "properties": {"keep": {"type": "array", "items": {"type": "string"}}},
    "required": ["keep"],
}


def select(llm: LLM, paper: Paper, candidates: list[dict], cache: Optional[dict] = None) -> tuple[list[dict], list[dict]]:
    """Returns (kept, dropped). Small lists skip the call entirely."""
    if len(candidates) <= MAX_PER_PAPER // 2:
        return list(candidates), []
    opening = " ".join(p.text for p in paper.pages[:1])[:2500]
    items = [{"id": f"k{i}", "kind": c["kind"], "claim": c["text"], "importance": c.get("importance", 2)}
             for i, c in enumerate(candidates)]
    ck = _h(paper.citation.title, items)
    if cache is not None and ck in cache:
        keep = cache[ck]
    else:
        user = f"Paper: {paper.citation.title}\n\nOpening text:\n{opening}\n\nCandidate claims:\n{dumps(items)}"
        keep = llm.call(system=SELECT_SYSTEM, user=user, name="selection", schema=SELECT_SCHEMA, role="curate")["keep"]
        if cache is not None:
            cache[ck] = keep
    idx = []
    for k in keep:
        m = re.fullmatch(r"k(\d+)", str(k).strip())
        if m and int(m.group(1)) < len(candidates) and int(m.group(1)) not in idx:
            idx.append(int(m.group(1)))
    idx = idx[:MAX_PER_PAPER]
    if not idx:                                   # model returned nothing usable: fall back to importance
        idx = sorted(range(len(candidates)), key=lambda i: -int(candidates[i].get("importance") or 1))[:MAX_PER_PAPER]
    kept = [candidates[i] for i in idx]
    dropped = [c for i, c in enumerate(candidates) if i not in idx]
    return kept, dropped


# ---------------------------------------------------------------- 2. entail
ENTAIL_SYSTEM = """You are a strict fact-checker. For each item, decide whether the SOURCE
text supports the CLAIM as written. You see only the source; use no outside knowledge.

- supported: the source states it, with the same strength and scope.
- partial: the source supports a weaker / narrower version (e.g. the claim drops a
  condition, overstates certainty, or generalizes beyond what was tested). Put the
  missing condition in `missing_qualifier`.
- unsupported: the source does not say this, says something different, or the claim
  attributes to the authors something they only cite.

Judge the claim's strength word by word: "reduces" vs "may reduce", "in all trials" vs "in
most trials", "humans" vs "mice", "causes" vs "is associated with", a number vs a different
number. Any of those mismatches means partial (if a narrower version holds) or unsupported.

Worked examples:
1. CLAIM "Caffeine delays sleep onset in adults."
   SOURCE "...participants (n = 212, ages 18-25) who consumed caffeine within six hours of bedtime fell
   asleep 18 minutes later on average..."
   -> partial, missing_qualifier "only shown for ages 18-25, caffeine within six hours of bedtime"
2. CLAIM "The model reached 94% accuracy on the held-out set."
   SOURCE "...accuracy on the held-out set was 89%, compared with 94% on the training data..."
   -> unsupported, reason "94% is the training accuracy; held-out accuracy was 89%"
3. CLAIM "Self-reported sleep onset may bias the latency estimates."
   SOURCE "Because sleep onset was self-reported, these estimates may be biased toward shorter latencies."
   -> supported, missing_qualifier ""
4. CLAIM "This study found the effect disappears in habitual users."
   SOURCE "Prior work by Ruiz et al. reported that this effect disappears in habitual users, which we did not observe."
   -> unsupported, reason "that finding is Ruiz et al.'s, and this study did not observe it"
5. CLAIM "Blue-light filters improved sleep quality."
   SOURCE "Participants using blue-light filters reported better sleep quality, although the difference
   was not statistically significant (p = 0.21)."
   -> partial, missing_qualifier "self-reported, and the difference was not statistically significant"
6. CLAIM "The intervention was tested on 40 participants over two weeks."
   SOURCE "We enrolled 40 participants, each followed for 14 days."
   -> supported (same facts in different words is fine)

Return one verdict per item, using each item's index. Never skip an item."""

ENTAIL_SCHEMA = {
    "type": "object",
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "verdict": {"type": "string", "enum": ["supported", "partial", "unsupported"]},
            "missing_qualifier": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["index", "verdict", "missing_qualifier", "reason"],
    }}},
    "required": ["verdicts"],
}


def entail(llm: LLM, paper: Paper, candidates: list[dict], batch: int = 12) -> tuple[list[dict], list[Rejected]]:
    kept, rejected = [], []
    for start in range(0, len(candidates), batch):
        group = candidates[start:start + batch]
        items = [{"index": i, "claim": c["text"],
                  "source": context_around(c["quote"], paper.pages, c["page"], 400)}
                 for i, c in enumerate(group)]
        out = llm.call(system=ENTAIL_SYSTEM, user=dumps(items), name="verdicts",
                       schema=ENTAIL_SCHEMA, judge=True)["verdicts"]
        by_idx = {v["index"]: v for v in out if isinstance(v.get("index"), int)}
        for i, c in enumerate(group):
            v = by_idx.get(i)
            if v is None:                                     # judge skipped it: fail closed
                rejected.append(Rejected(paper.id, c["text"], c["quote"], "entailment check returned no verdict"))
            elif v["verdict"] == "unsupported":
                rejected.append(Rejected(paper.id, c["text"], c["quote"], f"unsupported: {v['reason']}"))
            else:
                q = c.get("qualifiers", "")
                if v["verdict"] == "partial" and v["missing_qualifier"]:
                    q = "; ".join(x for x in [q, v["missing_qualifier"]] if x)
                kept.append({**c, "entailment": v["verdict"], "qualifiers": q})
    return kept, rejected


# ---------------------------------------------------------------- 4. synthesize
SYNTH_SYSTEM = """You write the backbone of a literature review: the central IDEAS that run across a
set of papers, built only from verified claims. Each claim below was quoted verbatim from its paper and
fact-checked; you see the claim text and its qualifiers.

Produce 8-20 ideas (fewer if the papers are few or narrow). An idea is a point a reviewer would make,
e.g. "Late caffeine delays sleep onset, but the effect shrinks or vanishes in habitual users" — not a
topic label like "Caffeine and sleep".
- Prefer ideas supported by claims from two or more papers. A single-paper idea is fine only if
  it's a central contribution nobody else addresses (say so in the statement).
- `statement`: 1-2 sentences saying exactly what the evidence shows, keeping its conditions. Never
  add facts, numbers, causes, or generalizations the claims don't contain. If papers disagree, say so.
- `evidence`: the claim ids the idea rests on, each with a stance: supports, qualifies (narrows it or
  adds a condition), or contradicts (cuts against it). Every idea needs at least one supporting claim.
  A claim may appear in at most two ideas. Not every claim has to be used.
- `links`: how ideas relate: builds_on (one depends on or extends the other), in_tension (they pull
  in different directions), related (same mechanism or setting). Only link ideas that truly connect.
- `outline`: group the ideas into 3-6 sections in the order a review would present them, each with
  a one-sentence `note` on why these ideas belong together.
- `gaps`: 2-6 open questions the evidence exposes (a contradiction, a condition nobody tested, a
  single-paper result nobody replicated). Each cites the claim ids that reveal it.
Use only claim ids from the list. Keep titles under 9 words."""

SYNTH_SCHEMA = {
    "type": "object",
    "properties": {
        "ideas": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "title": {"type": "string"},
                "statement": {"type": "string"},
                "evidence": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"claim": {"type": "string"},
                                   "stance": {"type": "string", "enum": ["supports", "qualifies", "contradicts"]}},
                    "required": ["claim", "stance"]}},
                "links": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"to": {"type": "string"},
                                   "kind": {"type": "string", "enum": ["builds_on", "in_tension", "related"]},
                                   "why": {"type": "string"}},
                    "required": ["to", "kind", "why"]}},
            },
            "required": ["key", "title", "statement", "evidence", "links"]}},
        "outline": {"type": "array", "items": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "note": {"type": "string"},
                           "ideas": {"type": "array", "items": {"type": "string"}}},
            "required": ["title", "note", "ideas"]}},
        "gaps": {"type": "array", "items": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "claims": {"type": "array", "items": {"type": "string"}}},
            "required": ["text", "claims"]}},
    },
    "required": ["ideas", "outline", "gaps"],
}


def synthesize(llm: LLM, claims: list[Claim], papers: dict[str, Paper], question: str = "",
               cache: Optional[dict] = None) -> tuple[list[Idea], dict, int]:
    """Returns (ideas, outline, dropped) where outline = {"sections": [...], "gaps": [...]} and
    dropped counts ideas/sections/gaps removed for citing nothing real."""
    payload = {
        "research_question": question or "(none given: organize around the main threads)",
        "papers": [{"id": p.id, "cite": p.citation.short(), "title": p.citation.title} for p in papers.values()],
        "claims": [{"id": c.id, "paper": c.paper_id, "kind": c.kind, "claim": c.text, "qualifiers": c.qualifiers}
                   for c in claims],
    }
    ck = _h(payload)
    if cache is not None and ck in cache:
        out = cache[ck]
    else:
        out = llm.call(system=SYNTH_SYSTEM, user=dumps(payload), name="synthesis", schema=SYNTH_SCHEMA,
                       role="synthesize")
        if cache is not None:
            cache[ck] = out

    valid = {c.id for c in claims}
    dropped = 0
    ideas: list[Idea] = []
    key_to_id: dict[str, str] = {}
    uses: dict[str, int] = {}
    for raw in out.get("ideas", [])[:20]:
        ev = []
        for e in raw.get("evidence", []):
            cid = str(e.get("claim", "")).strip()
            if cid in valid and uses.get(cid, 0) < 2 and cid not in {x.claim for x in ev}:
                ev.append(Evidence(cid, e.get("stance") if e.get("stance") in ("supports", "qualifies", "contradicts") else "supports"))
                uses[cid] = uses.get(cid, 0) + 1
        if not any(x.stance == "supports" for x in ev) or not raw.get("statement", "").strip():
            dropped += 1                              # cites nothing real: an idea we can't stand behind
            continue
        iid = f"I-{len(ideas) + 1:02d}"
        key_to_id[str(raw.get("key", iid))] = iid
        ideas.append(Idea(iid, raw.get("title", "").strip()[:90] or raw["statement"][:60], raw["statement"].strip(), ev))
    by_id = {i.id: i for i in ideas}
    for raw in out.get("ideas", [])[:20]:
        src = key_to_id.get(str(raw.get("key")))
        if not src:
            continue
        for l in raw.get("links", []):
            dst = key_to_id.get(str(l.get("to")))
            if dst and dst != src and l.get("kind") in ("builds_on", "in_tension", "related"):
                by_id[src].links.append(IdeaLink(dst, l["kind"], l.get("why", "")))

    sections = []
    placed = set()
    for s in out.get("outline", []):
        ids = [key_to_id[k] for k in s.get("ideas", []) if k in key_to_id and key_to_id[k] not in placed]
        if ids:
            placed |= set(ids)
            sections.append({"title": s.get("title", "Section"), "note": s.get("note", ""), "ideas": ids})
        else:
            dropped += 1
    rest = [i.id for i in ideas if i.id not in placed]
    if rest:
        sections.append({"title": "Other threads", "note": "", "ideas": rest})
    gaps = []
    for g in out.get("gaps", []):
        cids = [c for c in g.get("claims", []) if c in valid]
        if cids and g.get("text", "").strip():
            gaps.append({"text": g["text"].strip(), "claims": list(dict.fromkeys(cids))})
        else:
            dropped += 1
    return ideas, {"sections": sections, "gaps": gaps}, dropped


# ---------------------------------------------------------------- 5. check ideas
IDEA_CHECK_SYSTEM = """You are a strict fact-checker for a literature review. Each item is an IDEA
statement plus the evidence it cites: claims from papers, each with its verbatim quote and the stance
it was assigned. Use only this evidence, no outside knowledge.

- supported: every part of the statement is backed by the supporting evidence, at the same strength
  and scope, and any contradicting evidence is acknowledged in the statement.
- partial: a narrower version holds. Put the missing condition in `scope` (e.g. "shown only in
  simulation", "only one of three papers found this", "the papers disagree on the size of the effect").
- unsupported: the statement asserts something the evidence doesn't show (a new number, a cause,
  a generalization, a consensus that isn't there), or misstates what a paper found.

Return one verdict per item, using each item's index. Never skip an item."""

IDEA_CHECK_SCHEMA = {
    "type": "object",
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object",
        "properties": {"index": {"type": "integer"},
                       "verdict": {"type": "string", "enum": ["supported", "partial", "unsupported"]},
                       "scope": {"type": "string"}, "reason": {"type": "string"}},
        "required": ["index", "verdict", "scope", "reason"]}}},
    "required": ["verdicts"],
}


def check_ideas(llm: LLM, ideas: list[Idea], claims: dict[str, Claim], papers: dict[str, Paper],
                cache: Optional[dict] = None) -> tuple[list[Idea], list[Rejected], dict[str, str]]:
    """Returns (kept ideas, rejected ideas, old id -> new id) since surviving ideas are renumbered."""
    if not ideas:
        return [], [], {}
    items = [{"index": i, "statement": idea.statement,
              "evidence": [{"stance": e.stance, "paper": papers[claims[e.claim].paper_id].citation.short(),
                            "claim": claims[e.claim].text, "quote": claims[e.claim].quote,
                            "qualifiers": claims[e.claim].qualifiers} for e in idea.evidence]}
             for i, idea in enumerate(ideas)]
    ck = _h(items)
    if cache is not None and ck in cache:
        out = cache[ck]
    else:
        out = llm.call(system=IDEA_CHECK_SYSTEM, user=dumps(items), name="idea_checks", schema=IDEA_CHECK_SCHEMA,
                       judge=True)["verdicts"]
        if cache is not None:
            cache[ck] = out
    by_idx = {v["index"]: v for v in out if isinstance(v.get("index"), int)}
    kept, rejected = [], []
    for i, idea in enumerate(ideas):
        v = by_idx.get(i)
        if v is None or v["verdict"] == "unsupported":
            why = "idea check returned no verdict" if v is None else f"unsupported idea: {v['reason']}"
            rejected.append(Rejected("idea", idea.statement, idea.title, why))
            continue
        if v["verdict"] == "partial":
            idea.check, idea.scope = "partial", v.get("scope", "").strip()
        kept.append(idea)
    # renumber so ids stay compact, and drop links to removed ideas
    remap = {idea.id: f"I-{n + 1:02d}" for n, idea in enumerate(kept)}
    for idea in kept:
        idea.links = [IdeaLink(remap[l.to], l.kind, l.why) for l in idea.links if l.to in remap]
    for idea in kept:
        idea.id = remap[idea.id]
    return kept, rejected, remap
