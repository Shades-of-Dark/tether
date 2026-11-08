"""How relevant each paper is to the project's research question.

A research question is usually new: no single paper answers it, but many papers answer one of the
things it rests on. So the question is first split into its BUILDING BLOCKS (e.g. "where to hold a
payload on a drone" rests on centre-of-mass offset and stability, inertia and controllability,
attachment points of slung loads, grasp-point selection ...). Each block gets its own searches,
including in neighbouring fields where the same problem is studied.

Every paper is then scored twice, 0-10:
- direct: how directly it answers the question itself;
- block: how well it covers its best-matching building block.
Its relevance is the higher of the two, and the reason says which.

Scoring:
- quick: the cheap model, from title + abstract, for everything you see in a list (search results,
  suggestions, papers not read yet). Cached per question, so a list is scored once.
- full: the stronger model, from the paper's verified key claims, once tether has read it. That
  replaces the quick score for papers in the graph.

Search results (the queries made from a question, and each query's OpenAlex results) are cached for
a day, so re-running a search is free and instant.

The model only scores and explains. Titles, authors and abstracts come from OpenAlex or your PDFs.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Callable, Optional

from .llm import LLM, dumps

SEARCH_TTL = 24 * 3600          # how long a query's OpenAlex results are reused
PLAN_TTL = 7 * 24 * 3600        # how long a question's building blocks and queries are reused
BATCH = 25

# ---------------------------------------------------------------- planning the search
PLAN_SYSTEM = """You plan the literature search for a research question.

1. Split the question into 3-5 BUILDING BLOCKS: the distinct sub-problems, quantities, methods or
   phenomena an answer would have to rest on. A new question rarely has papers answering it
   directly, but each block usually has its own literature. Name each in 2-6 words and say in one
   short sentence why the question needs it.
2. For each block, write 2-3 search queries (2-6 words each) in the vocabulary researchers use in
   titles. Include at least one query aimed at a NEIGHBOURING FIELD where the same problem is
   studied under another name (e.g. robot-arm grasp planning for an aerial grasping question,
   helicopter sling loads for drone payloads).
3. Write 1-2 queries for the question as a whole.

Queries are keyword searches over titles and abstracts: no question words, no filler. You also see
titles already in the project, to learn the field's vocabulary; don't just repeat them."""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "blocks": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "why": {"type": "string"},
                           "queries": {"type": "array", "items": {"type": "string"}}},
            "required": ["name", "why", "queries"]}},
        "direct_queries": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["blocks", "direct_queries"],
}

# ---------------------------------------------------------------- scoring
RANK_SYSTEM = """You judge how useful each paper is for a literature review on a research question,
using only what you're given about the paper. No outside knowledge about it.

The question has building blocks (listed, numbered). Give each paper two scores, 0-10:
- direct: how directly it answers the QUESTION itself. 9-10 studies exactly this; 6-8 studies a
  close variant or a large part of it; 3-5 related background; 0-2 off-topic.
- block_score for its single best building block (`block` = that block's number, or -1 if none
  fits): 9-10 a core study of that block; 6-8 substantially addresses it; 3-5 touches it; 0-2 no.
Be strict: shared keywords are not relevance. A paper from a neighbouring field can score high on
a block if it really studies that block's problem.
reason: under 20 words. Say what it contributes and to which part ("Measures how CoM offset
degrades hover stability: core to block 1"), or why it doesn't help.
Return one item per paper, using each paper's index. Never skip one."""

RANK_SCHEMA = {"type": "object", "properties": {"scores": {"type": "array", "items": {
    "type": "object",
    "properties": {"index": {"type": "integer"}, "direct": {"type": "integer"}, "block": {"type": "integer"},
                   "block_score": {"type": "integer"}, "reason": {"type": "string"}},
    "required": ["index", "direct", "block", "block_score", "reason"]}}}, "required": ["scores"]}


def _norm_q(question: str) -> str:
    return " ".join(question.lower().split())


def _h(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]


class Store:
    """The project's relevance cache: plans, search results, and scores (one JSON file)."""

    def __init__(self, cache_dir: Path):
        self.path = cache_dir / "relevance.json"
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            d = {}
        if not isinstance(d, dict) or "scores" not in d:      # file from 0.12: a flat dict of old-style scores
            d = {"scores": {}}
        self.d = d
        for k in ("plans", "searches", "scores"):
            self.d.setdefault(k, {})

    def save(self) -> None:
        now = time.time()
        self.d["searches"] = {k: v for k, v in self.d["searches"].items() if now - v["at"] < SEARCH_TTL}
        self.d["plans"] = {k: v for k, v in self.d["plans"].items() if now - v["at"] < PLAN_TTL}
        if len(self.d["scores"]) > 6000:
            self.d["scores"] = dict(list(self.d["scores"].items())[-6000:])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.d, ensure_ascii=False), encoding="utf-8")


def plan(llm: Optional[LLM], store: Store, question: str, titles: list[str]) -> dict:
    """{blocks: [{name, why, queries}], direct_queries: [...]}, cached per question for a week."""
    k = _norm_q(question)
    hit = store.d["plans"].get(k)
    if hit and time.time() - hit["at"] < PLAN_TTL:
        return hit["plan"]
    if llm is None:
        raise RuntimeError("needs the model")
    out = llm.call(system=PLAN_SYSTEM, user=dumps({"research_question": question, "titles_in_project": titles[:25]}),
                   name="plan", schema=PLAN_SCHEMA, judge=True, max_tokens=1200)
    seen: set[str] = set()

    def clean(qs) -> list[str]:
        res = []
        for q in qs or []:
            q = " ".join(str(q).split())[:80]
            if q and q.lower() not in seen:
                seen.add(q.lower())
                res.append(q)
        return res
    blocks = [{"name": str(b.get("name", "")).strip()[:60], "why": str(b.get("why", "")).strip()[:200],
               "queries": clean(b.get("queries"))[:3]} for b in out.get("blocks", [])[:5] if str(b.get("name", "")).strip()]
    p = {"blocks": blocks, "direct_queries": clean(out.get("direct_queries"))[:2]}
    store.d["plans"][k] = {"at": time.time(), "plan": p}
    return p


def plan_if_cached(store: Store, question: str) -> Optional[dict]:
    hit = store.d["plans"].get(_norm_q(question))
    return hit["plan"] if hit and time.time() - hit["at"] < PLAN_TTL else None


def cached_search(store: Store, search: Callable[..., list], query: str, n: int, since: Optional[int],
                  oa_only: bool) -> tuple[list, bool]:
    """(OpenAlex results, came_from_cache). Results are reused for SEARCH_TTL."""
    k = _h(query.lower(), n, since, oa_only)
    hit = store.d["searches"].get(k)
    if hit and time.time() - hit["at"] < SEARCH_TTL:
        return hit["works"], True
    works = search(query, n, since, oa_only=oa_only)
    keep = ("id", "title", "display_name", "doi", "publication_year", "authorships", "primary_location",
            "best_oa_location", "locations", "abstract_inverted_index", "cited_by_count", "type")
    works = [{k2: w.get(k2) for k2 in keep if k2 in w} for w in works]
    store.d["searches"][k] = {"at": time.time(), "works": works}
    return works, False


def _score_key(question: str, blocks: list[dict], title: str, text: str, kind: str) -> str:
    return _h(_norm_q(question), [b["name"] for b in blocks], title.lower(), text[:1500], kind)


def score(llm: Optional[LLM], store: Store, question: str, blocks: list[dict], items: list[dict],
          kind: str = "quick") -> list[dict]:
    """items: [{title, abstract}] -> each item plus `relevance`: {score, direct, block, block_score, reason, kind}.
    kind "quick" reads title + abstract with the cheap model; "full" reads verified claims with the
    stronger one (pass `abstract` = the claims text). Cached per question; llm=None scores nothing new."""
    S = store.d["scores"]
    keys = [_score_key(question, blocks, it["title"], it.get("abstract") or "", kind) for it in items]
    todo = [i for i, k in enumerate(keys) if k not in S]
    if llm is not None:
        for start in range(0, len(todo), BATCH):
            group = todo[start:start + BATCH]
            payload = {"research_question": question,
                       "building_blocks": [{"number": n + 1, "name": b["name"], "why": b["why"]} for n, b in enumerate(blocks)],
                       "papers": [{"index": n, "title": items[i]["title"],
                                   ("key_claims" if kind == "full" else "abstract"):
                                       (items[i].get("abstract") or "")[:2500 if kind == "full" else 1200] or "(no abstract)"}
                                  for n, i in enumerate(group)]}
            out = llm.call(system=RANK_SYSTEM, user=dumps(payload), name="relevance", schema=RANK_SCHEMA,
                           judge=(kind == "quick"), role=None if kind == "quick" else "curate",
                           max_tokens=200 + 70 * len(group))
            by = {s["index"]: s for s in out.get("scores", []) if isinstance(s.get("index"), int)}
            for n, i in enumerate(group):
                s = by.get(n)
                if s is None:
                    continue                                      # try again next time rather than cache a gap
                direct = max(0, min(10, int(s.get("direct", 0))))
                bi = int(s.get("block", -1))
                bs = max(0, min(10, int(s.get("block_score", 0)))) if 1 <= bi <= len(blocks) else 0
                S[keys[i]] = {"direct": direct, "block": blocks[bi - 1]["name"] if bs else "", "block_score": bs,
                              "score": max(direct, bs), "reason": str(s.get("reason", ""))[:220], "kind": kind}
    return [it | {"relevance": S.get(k)} for it, k in zip(items, keys)]
