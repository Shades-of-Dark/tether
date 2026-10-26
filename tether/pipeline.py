"""Orchestration: inputs -> papers -> verified claims -> graph -> vault.

A project is a folder (usually inside your Obsidian vault). Its PDFs live in
<project>/Sources, so dropping more papers in and rebuilding only pays for the new ones.
"""
from __future__ import annotations

import hashlib
from collections import Counter
import json
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from . import sources, stages
from .ingest import chunk_pages, citation_from_pdf, drop_low_value_sections, read_pdf
from .llm import LLM, PRICES
from .models import Citation, Claim, Page, Paper, Rejected
from .vault import write_vault

PROMPT_VERSION = "3"      # bump when prompts change in a way that should re-read papers
PIPELINE_VERSION = "5"    # bump when the vault layout changes; out-of-date projects rebuild on open
LEGACY_VERSION = "2"      # claims verified by tether 0.7 are reused instead of re-reading the PDF


class NeedsApproval(Exception):
    """Raised before any paid work when an update's estimated cost needs the user's OK."""

    def __init__(self, estimate: dict):
        super().__init__(f"needs approval: ~${estimate['cost']:.2f}")
        self.estimate = estimate


class Reporter:
    """Progress sink. The CLI prints; the app streams these to the browser."""

    def __init__(self, on_log: Optional[Callable[[str], None]] = None,
                 on_stage: Optional[Callable[[str, float], None]] = None,
                 on_now: Optional[Callable[[dict], None]] = None):
        self._log = on_log or (lambda m: print(m, flush=True))
        self._stage = on_stage or (lambda s, f: None)
        self._now = on_now or (lambda d: None)

    def now(self, **what) -> None:
        """What's happening right now, for the Details panel: paper, index, total, step."""
        self._now(what)

    def log(self, msg: str) -> None:
        self._log(msg)

    def stage(self, name: str, frac: float) -> None:
        self._stage(name, max(0.0, min(1.0, frac)))


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def add_to_sources(files: list[Path], sources_dir: Path) -> list[Path]:
    """Copy PDFs into the project's Sources folder. Returns where each input lives there
    (an exact duplicate maps to the copy that's already present)."""
    sources_dir.mkdir(parents=True, exist_ok=True)
    have = {_sha(p): p for p in sources_dir.glob("*.pdf")}
    added = []
    for f in files:
        h = _sha(f)
        if h in have:
            added.append(have[h])
            continue
        dest = sources_dir / f.name
        if dest.exists():
            dest = sources_dir / f"{f.stem}-{h[:6]}.pdf"
        if f.resolve() != dest.resolve():
            shutil.copy2(f, dest)
        have[h] = dest
        added.append(dest)
    return added


def _cached_lookup(title: str, cache: Path) -> Optional[Citation]:
    f = cache / "meta" / (hashlib.sha256(title.lower().encode()).hexdigest()[:16] + ".json")
    if f.exists():
        d = json.loads(f.read_text(encoding="utf-8"))
        return Citation(**d) if d else None
    cit = sources.lookup_by_title(title)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(asdict(cit) if cit else None), encoding="utf-8")
    return cit


def _load_known(cache: Path) -> dict[str, Citation]:
    f = cache / "known_citations.json"
    if not f.exists():
        return {}
    fields = set(Citation.__dataclass_fields__)
    return {k: Citation(**{a: b for a, b in v.items() if a in fields})
            for k, v in json.loads(f.read_text(encoding="utf-8")).items()}


def _save_known(cache: Path, known: dict[str, Citation]) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "known_citations.json").write_text(json.dumps({k: asdict(v) for k, v in known.items()}), encoding="utf-8")


def remove_abstract(out: Path, title: str) -> None:
    f = out / ".tether" / "abstract_only.json"
    if f.exists():
        keep = [a for a in json.loads(f.read_text(encoding="utf-8")) if a["citation"]["title"] != title]
        f.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")


def import_from_zotero(lib, keys: list[str], out: Path) -> dict:
    """Bring selected Zotero items into a project: PDFs are copied to Sources with Zotero's
    citation attached; items without a PDF come in as abstract-only (if they have one)."""
    cache = out / ".tether"
    known = _load_known(cache)
    abs_file = cache / "abstract_only.json"
    abstracts = json.loads(abs_file.read_text(encoding="utf-8")) if abs_file.exists() else []
    have_abs = {a["citation"].get("zotero_key") for a in abstracts}
    res = {"pdf": 0, "abstract": 0, "skipped": []}
    for key in keys:
        it = lib.items.get(key)
        if it is None:
            res["skipped"].append(key)
        elif it.pdf is not None:
            dest = add_to_sources([it.pdf], out / "Sources")[0]
            known[dest.name] = it.citation()
            res["pdf"] += 1
        elif it.abstract.strip():
            if key not in have_abs:
                abstracts.append({"citation": asdict(it.citation()), "abstract": it.abstract})
            res["abstract"] += 1
        else:
            res["skipped"].append(it.title)
    _save_known(cache, known)
    abs_file.write_text(json.dumps(abstracts, ensure_ascii=False), encoding="utf-8")
    return res


def _norm_title(t: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _nice_name(pdf: Optional[Path], cit: Optional[Citation]) -> Optional[Path]:
    """Rename a downloaded PDF to 'Author et al. - 2024 - Title.pdf' so Sources reads like a library."""
    import re
    if pdf is None:
        return pdf
    if cit is None:                                  # a bare PDF link: name it from the PDF itself
        from .ingest import quick_citation
        cit = quick_citation(pdf)
        if cit.title == pdf.stem:
            return pdf
    name = f"{cit.short().rsplit(',', 1)[0]} - {cit.year or 'n.d.'} - {cit.title}"
    if not cit.authors:
        name = f"{cit.title}"
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .")[:120].rstrip(" .")
    dest = pdf.with_name(name + ".pdf")
    if dest != pdf:
        pdf.replace(dest)
    return dest


def fetch_remote(out: Path, urls: list[str] = (), topics: list[tuple] = (), rep: Optional[Reporter] = None,
                 on_item: Optional[Callable[..., None]] = None) -> dict:
    """Pull in papers from links and topic searches, and keep them in the project:
    PDFs go to Sources, abstract-only papers to .tether/abstract_only.json. So later
    rebuilds (say, after you drop in another PDF) still include them."""
    rep = rep or Reporter()
    cache = out / ".tether"
    cache.mkdir(parents=True, exist_ok=True)
    found: list[tuple] = []                      # (citation | None, pdf | None, abstract | None)
    item = on_item or (lambda url, **kw: None)
    for n, u in enumerate(urls, 1):
        item(u, state="fetching")
        rep.now(step=f"Fetching link {n} of {len(urls)}: {u}")
        try:
            cit, pdf, pages = sources.resolve_url(u, cache / "downloads")
        except Exception as e:                       # one bad link never stops the others
            rep.log(f"  ! {u}: couldn't fetch it ({type(e).__name__}). Skipped; download the PDF and drop it in instead.")
            item(u, state="failed", reason=f"Couldn't reach it ({type(e).__name__})")
            continue
        if pdf is None and pages is None:
            rep.log(f"  ! {u}: no free PDF or abstract found. Skipped; download the PDF and drop it in instead.")
            item(u, state="failed", reason="No free PDF or abstract found. Download it and drop the PDF in.")
            continue
        label = cit.short() + " | " + cit.title[:70] if cit else u
        rep.log(f"  + {label}{'' if pdf else '  [abstract only: no free PDF]'}")
        pdf = _nice_name(pdf, cit)
        found.append((cit, pdf, pages[0].text if pages else None, u))
    for topic, n, since in topics:
        rep.log(f"Searching OpenAlex for {topic!r}...")
        for w in sources.search_topic(topic, n, since):
            try:
                cit, pdf, pages = sources.resolve_work(w, cache / "downloads")
            except Exception as e:
                rep.log(f"  ! couldn't fetch a search result ({type(e).__name__}); skipped")
                continue
            if pdf or pages:
                found.append((cit, _nice_name(pdf, cit), pages[0].text if pages else None, None))
                rep.log(f"  + {cit.short()} | {cit.title[:70]}{'' if pdf else '  [abstract only]'}")

    known = _load_known(cache)
    abs_file = cache / "abstract_only.json"
    abstracts = json.loads(abs_file.read_text(encoding="utf-8")) if abs_file.exists() else []
    have = {_norm_title(a["citation"]["title"]) for a in abstracts}
    res = {"pdf": 0, "abstract": 0}
    for cit, pdf, abstract, url in found:
        title = cit.title if cit else (pdf.stem if pdf else url)
        cite = cit.short() if cit else ""
        if pdf is not None:
            dest = add_to_sources([pdf], out / "Sources")[0]
            if cit:
                known.setdefault(dest.name, cit)
            res["pdf"] += 1
            if url:
                item(url, state="pdf", title=title, cite=cite, file=dest.name)
        elif cit and abstract:
            if _norm_title(cit.title) not in have:
                abstracts.append({"citation": asdict(cit), "abstract": abstract})
                have.add(_norm_title(cit.title))
                res["abstract"] += 1
            if url:
                item(url, state="abstract", title=title, cite=cite)
    _save_known(cache, known)
    abs_file.write_text(json.dumps(abstracts, ensure_ascii=False), encoding="utf-8")
    return res


def gather_papers(out: Path, lookup: bool, rep: Reporter, zotero=None) -> list[Paper]:
    """Everything the project currently holds: PDFs in Sources + saved abstract-only papers."""
    cache = out / ".tether"
    src_dir = out / "Sources"
    known = _load_known(cache)
    found = [(known.get(p.name), p, None, str(p)) for p in sorted(src_dir.glob("*.pdf"))] if src_dir.exists() else []
    abs_file = cache / "abstract_only.json"
    if abs_file.exists():                              # links, searches and Zotero items without a PDF
        fields = set(Citation.__dataclass_fields__)
        for a in json.loads(abs_file.read_text(encoding="utf-8")):
            cit = Citation(**{k: v for k, v in a["citation"].items() if k in fields})
            found.append((cit, None, [Page(1, a["abstract"])], cit.title))

    papers: list[Paper] = []
    seen_titles = set()
    for cit, pdf, pages, origin in found:
        kind = "fulltext"
        if pdf is not None:
            try:
                pages, info = read_pdf(pdf)
            except Exception as e:
                rep.log(f"  ! couldn't read {Path(origin).name}: {e}")
                continue
            if sum(len(pg.text.strip()) for pg in pages) < 500:
                rep.log(f"  ! {Path(origin).name} has almost no text layer (scanned?); skipped. OCR it first.")
                continue
            legacy = _key(LEGACY_VERSION, pages)
            pages, _dropped = drop_low_value_sections(pages)
            if cit is None:
                cit = citation_from_pdf(info, pdf)
                z = zotero.find(cit.title) if zotero is not None else None
                if z is not None:                      # your own library knows this paper: use its citation
                    cit = z.citation()
                    rep.log(f"  ↳ {pdf.name}: citation from your Zotero library")
                elif lookup:
                    cit = _cached_lookup(cit.title, cache) or cit
        else:
            kind = "abstract-only"
            legacy = _key(LEGACY_VERSION, pages)
        key = cit.title.lower()[:60]
        if key in seen_titles:
            continue
        seen_titles.add(key)
        papers.append(Paper(id=f"P{len(papers) + 1:02d}", citation=cit, pages=pages,
                            source_kind=kind, path=str(pdf) if pdf else None, legacy_key=legacy))
    disambiguate(papers)
    return papers


def disambiguate(papers: list[Paper]) -> None:
    """APA-style 2025a / 2025b when the same author(s) published several papers in one year."""
    groups: dict[str, list[Paper]] = {}
    for p in papers:
        p.citation.year_suffix = ""
        groups.setdefault(p.citation.short(), []).append(p)
    for same in groups.values():
        if len(same) > 1:
            for i, p in enumerate(sorted(same, key=lambda p: p.citation.title.lower())):
                p.citation.year_suffix = _suffix(i)


def _suffix(i: int) -> str:
    """0 -> a, 25 -> z, 26 -> aa, 27 -> ab ... (no limit on how many share an author and year)."""
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(97 + r) + s
    return s


class _StageCache:
    """Remembers per-paper selections, the synthesis and the idea checks between rebuilds, so an
    update with nothing new makes no calls to Claude."""

    def __init__(self, path: Path):
        self.path, self.tag = path, PROMPT_VERSION
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            d = {}
        self.data = d if d.get("_tag") == self.tag else {"_tag": self.tag}

    def section(self, name: str) -> dict:
        return self.data.setdefault(name, {})

    def save(self) -> None:
        for name in ("ideas", "idea_check", "outline"):           # keep only recent ones
            if len(self.data.get(name, {})) > 10:
                self.data[name] = dict(list(self.data[name].items())[-10:])
        self.path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")


def _key(version: str, pages: list[Page]) -> str:
    # Not keyed on the model: switching Balanced <-> Budget keeps the work already done
    # (claims were verified either way). "Rebuild from scratch" redoes it with the current model.
    h = hashlib.sha256(version.encode())
    for pg in pages:
        h.update(pg.text.encode())
    return h.hexdigest()[:20]


def _claims_file(cache: Path, paper: Paper) -> Path:
    return cache / "claims" / f"{_key(PROMPT_VERSION, paper.pages)}.json"


def paper_claims(llm: LLM, paper: Paper, cache: Path, sc: _StageCache, fresh: bool = False,
                 say: Optional[Callable[[str], None]] = None) -> tuple[list[dict], list[Rejected], int, str]:
    """(kept, rejected, trimmed, how) where how is cached | reused | read.
    reused = claims tether 0.7 already verified for this PDF, trimmed to the central ones (no re-reading).
    fresh = ignore everything saved and read the PDF again ("Rebuild from scratch")."""
    f = _claims_file(cache, paper)
    if f.exists() and not fresh:
        d = json.loads(f.read_text(encoding="utf-8"))
        return d["kept"], [Rejected(**r) for r in d["rejected"]], d.get("trimmed", 0), "cached"
    legacy = cache / "claims" / f"{paper.legacy_key}.json"
    if paper.legacy_key and legacy.exists() and not fresh:
        d = json.loads(legacy.read_text(encoding="utf-8"))
        kept, trimmed = stages.select(llm, paper, d["kept"], cache=sc.section("select"), progress=say)
        rejected = [Rejected(**r) for r in d["rejected"]]
        how = "reused"
    else:
        chunks = chunk_pages(paper.id, paper.pages)
        candidates, rej1 = stages.extract(llm, paper, chunks, progress=say)
        chosen, trimmed = stages.select(llm, paper, candidates, cache=sc.section("select"), progress=say)
        kept, rej2 = stages.entail(llm, paper, chosen, progress=say)
        rejected = rej1 + rej2
        how = "read"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"kept": kept, "rejected": [asdict(r) for r in rejected], "trimmed": len(trimmed)},
                            ensure_ascii=False), encoding="utf-8")
    return kept, rejected, len(trimmed), how


def estimate(llm: LLM, papers: list[Paper], cache: Path, fresh: bool = False, synth_cached: bool = False) -> dict:
    """What an update will roughly cost, before spending anything. Reading new PDFs dominates;
    reused and cached papers are nearly free. Prices from llm.PRICES (unknown models: 0)."""
    def price(role):
        return PRICES.get(llm.model_for(role), (0, 0, 0, 0))
    new = [p for p in papers if fresh or (not _claims_file(cache, p).exists()
           and not (p.legacy_key and (cache / "claims" / f"{p.legacy_key}.json").exists()))]
    reuse = [p for p in papers if p not in new and not _claims_file(cache, p).exists()]
    cost = 0.0
    r_in, r_out = price("read")[:2]
    c_in, c_out = price("check")[:2]
    s_in, s_out = price("synthesize")[:2]
    for p in new:
        chars = sum(len(pg.text) for pg in p.pages)
        n_chunks = max(1, len(chunk_pages(p.id, p.pages)))
        cost += (chars / 3.6 + n_chunks * 900) * r_in / 1e6 + n_chunks * 700 * r_out / 1e6   # read
        cost += (4000 * s_in + 300 * s_out) / 1e6                                           # select
        cost += (12 * 450 * c_in + 12 * 60 * c_out) / 1e6                                   # fact-check
    cost += len(reuse) * (8000 * s_in + 300 * s_out) / 1e6                                  # select only
    if new or reuse or not synth_cached:                                                     # ideas, check, outline
        n_claims = 10 * len(papers)
        cost += (n_claims * 45 + 1500) * s_in / 1e6 + 4500 * s_out / 1e6                    # ideas
        cost += (20 * 8 * 120) * c_in / 1e6 + 1500 * c_out / 1e6                             # idea check
        cost += (20 * 8 * 40 + 1500) * s_in / 1e6 + 1500 * s_out / 1e6                       # outline
    return {"cost": round(cost * 1.15, 2), "new_papers": len(new), "reused_papers": len(reuse),
            "titles": [p.citation.short() for p in new][:12]}


def _isolated(papers: list[Paper], ideas, claim_map) -> list[Paper]:
    """Papers that share no central idea with any other paper."""
    used = {claim_map[e.claim].paper_id for i in ideas for e in i.evidence}
    return [p for p in papers if p.id not in used]


def run(llm: LLM, *, out: Path, pdfs: list[Path] = (), urls: list[str] = (), topic: Optional[str] = None,
        vault_root: Optional[Path] = None, question: str = "", n_topic: int = 15, since: Optional[int] = None,
        lookup: bool = True, model_tag: str = "default", reporter: Optional[Reporter] = None,
        zotero=None, approve: Optional[Callable[[dict], bool]] = None, fresh: bool = False) -> dict:
    """`approve(estimate)` is called before any paid work; returning False raises NeedsApproval.
    `fresh` re-reads every paper and redoes every step, ignoring (but not deleting) saved work."""
    rep = reporter or Reporter()
    out = Path(out)
    cache = out / ".tether"
    cache.mkdir(parents=True, exist_ok=True)
    if question:
        (cache / "question.txt").write_text(question, encoding="utf-8")
    elif (cache / "question.txt").exists():
        question = (cache / "question.txt").read_text(encoding="utf-8")

    rep.stage("Collecting papers", 0.02)
    files: list[Path] = []
    for p in pdfs:
        p = Path(p)
        files += sorted(p.rglob("*.pdf")) if p.is_dir() else [p]
    if files:
        add_to_sources(files, out / "Sources")
    if urls or topic:
        fetch_remote(out, list(urls), [(topic, n_topic, since)] if topic else [], rep)
    papers = gather_papers(out, lookup, rep, zotero)
    if not papers:
        raise RuntimeError("No readable papers. Add PDFs, links, or a topic.")

    sc = _StageCache(cache / "stage_cache.json")
    if fresh:
        sc.data = {"_tag": sc.tag}
    graph = cache / "graph.json"
    try:
        up_to_date = json.loads(graph.read_text(encoding="utf-8")).get("version") == PIPELINE_VERSION
    except (OSError, ValueError):
        up_to_date = False
    # After a tether upgrade (layout version changed) with no new papers and the same question, the
    # ideas, checks and outline come straight from the saved results: don't quote a price for them.
    q_changed = (cache / "question.txt").exists() and graph.exists() and \
        (cache / "question.txt").stat().st_mtime > graph.stat().st_mtime
    synth_saved = bool(sc.data.get("ideas")) and not q_changed
    est = estimate(llm, papers, cache, fresh=fresh, synth_cached=(up_to_date or synth_saved) and not fresh)
    if approve is not None and not approve(est):
        raise NeedsApproval(est)
    if est["new_papers"] or est["reused_papers"]:
        rep.log(f"Estimated cost of this update: about ${est['cost']:.2f} "
                f"({est['new_papers']} new paper{'s' if est['new_papers'] != 1 else ''} to read"
                + (f", {est['reused_papers']} reused from an earlier version" if est["reused_papers"] else "") + ")")
    rep.log(f"{len(papers)} papers. Picking out each paper's central claims...")

    raw: list[tuple[Paper, dict]] = []
    rejected: list[Rejected] = []
    trimmed_total = 0
    for i, p in enumerate(papers):
        rep.stage(f"Reading paper {i + 1} of {len(papers)}: {p.citation.short()}", 0.05 + 0.6 * i / len(papers))
        rep.now(paper=p.citation.short(), title=p.citation.title, index=i + 1, total=len(papers),
                step="Starting", kind=p.source_kind)
        kept, rej, trimmed, how = paper_claims(llm, p, cache, sc, fresh=fresh, say=lambda m: rep.now(step=m))
        sc.save()
        raw += [(p, c) for c in kept]
        rejected += rej
        trimmed_total += trimmed
        if how == "cached":
            rep.log(f"  {p.id} {p.citation.short()}: {len(kept)} key claims (already read; nothing to pay for)")
        else:
            why = Counter(r.reason.split(":")[0] for r in rej)
            extra = [f"{n} {k}" for k, n in (("cited from other work", why.get("secondhand", 0)),
                                             ("quotes not found in the PDF", why.get("quote not found in paper text", 0)),
                                             ("not supported by their page", why.get("unsupported", 0))) if n]
            src = "reused earlier claims" if how == "reused" else "read"
            rep.log(f"  {p.id} {p.citation.short()}: {src}, kept {len(kept)} key claims"
                    + (f", {trimmed} less central trimmed" if trimmed else "")
                    + (f"; rejected {', '.join(extra)}" if extra else ""))
        rep.now(step=f"Done: {len(kept)} key claims")

    claims = [Claim(id=f"C-{i:04d}", paper_id=p.id, text=c["text"], kind=c["kind"], quote=c["quote"],
                    page=c["page"], match_score=c["match_score"], entailment=c.get("entailment", "supported"),
                    qualifiers=c.get("qualifiers", ""), concepts=[])
              for i, (p, c) in enumerate(raw, 1)]
    paper_map = {p.id: p for p in papers}
    claim_map = {c.id: c for c in claims}

    rep.stage("Weaving the papers into central ideas", 0.72)
    rep.now(paper=None, title=None, index=None, total=len(papers),
            step=f"Connecting {len(claims)} key claims from {len(papers)} papers into ideas that run across them")
    ideas, dropped = stages.synthesize(llm, claims, paper_map, question, cache=sc.section("ideas"))
    sc.save()
    min_papers = stages.MIN_PAPERS if len(papers) >= stages.MIN_PAPERS else 1
    ideas, single = stages.split_single_source(ideas, claim_map, min_papers)
    for n, s1 in enumerate(single, 1):
        s1.id = f"S-{n:02d}"
    if single:
        rep.log(f"  {len(single)} single-paper result(s) kept as single-source findings, not central ideas")
    rep.stage("Fact-checking each idea against its evidence", 0.84)
    rep.now(step=f"Checking each of {len(ideas)} ideas against its quotes; narrowing or dropping any that overreach")
    ideas, idea_rej, _remap = stages.check_ideas(llm, ideas, claim_map, paper_map, cache=sc.section("idea_check"))
    sc.save()
    if idea_rej:
        rep.log(f"  {len(idea_rej)} idea(s) dropped: their statement went beyond the evidence (see Audit)")
    narrowed = sum(bool(i.narrowed_from) for i in ideas)
    if narrowed:
        rep.log(f"  {narrowed} idea(s) reworded by the fact-check to say only what the evidence shows")
    rep.stage("Outlining the review", 0.9)
    rep.now(step=f"Ordering {len(ideas)} ideas into sections and naming the open questions")
    outline, dropped_outline = stages.outline(llm, ideas, claim_map, question, cache=sc.section("outline"),
                                              single=single)
    dropped += dropped_outline
    sc.save()
    rep.log(f"  {len(ideas)} central ideas from {len(claims)} claims across {len(papers)} papers")

    rep.stage("Following the citations behind claims papers report from other work", 0.93)
    from . import citations
    rep.now(paper=None, title=None, index=None, step="Matching cited claims to each paper's reference list")
    cited = citations.follow(paper_map, rejected, cache, log=rep.log, now=lambda m: rep.now(step=m), lookup=lookup)
    have_titles = {_norm_title(p.citation.title) for p in papers}
    have_dois = {p.citation.doi.lower() for p in papers if p.citation.doi}
    for w in cited:
        w_in = _norm_title(w.title) in have_titles or bool(w.doi and w.doi.lower() in have_dois)
        setattr(w, "in_project", w_in)

    rep.stage("Writing notes to Obsidian", 0.95)
    rep.now(step=f"Writing {len(ideas)} idea notes, {len(papers)} paper notes, the outline and the overview")
    write_vault(out, paper_map, claims, ideas, outline, rejected + idea_rej, dropped, question,
                vault_root=Path(vault_root) if vault_root else None, trimmed=trimmed_total, single=single,
                cited=cited)
    (cache / "graph.json").write_text(json.dumps({
        "version": PIPELINE_VERSION,
        "papers": [p.to_dict() | {"pages": len(p.pages),
                                  "abstract": p.pages[0].text if p.source_kind == "abstract-only" and p.pages else ""}
                   for p in papers],
        "claims": [asdict(c) for c in claims],
        "ideas": [asdict(i) for i in ideas],
        "single_source": [asdict(i) for i in single],
        "cited": [asdict(w) | {"in_project": getattr(w, "in_project", False)} for w in cited],
        "outline": outline,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    rep.stage("Done", 1.0)
    disagreements = (sum(bool(stages.disagreeing_papers(i, claim_map)) for i in ideas)
                     + len(stages.tensions(ideas, claim_map)))
    return {"papers": len(papers), "claims": len(claims), "ideas": len(ideas), "contradictions": disagreements,
            "single_source": len(single), "isolated": len(_isolated(papers, ideas, claim_map)),
            "suggested": sum(not getattr(w, "in_project", False) for w in cited),
            "rejected": len(rejected) + len(idea_rej), "trimmed": trimmed_total, "dropped_points": dropped,
            "estimate": est["cost"]}
