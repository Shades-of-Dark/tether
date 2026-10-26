"""Write the result as Obsidian notes: plain Markdown with [[links]].

Layout of a project (all inside <vault>/Tether/<project>/):
    00 Overview.md          the ideas at a glance + which papers feed which idea
    Lit Review Outline.md   sections -> ideas -> cited evidence, then open questions
    Ideas/                  8-20 central ideas, each with its verified evidence and quotes
    Papers/                 one note per paper: citation, its key claims, the ideas it feeds
    Sources/                your PDFs (evidence links jump to the exact page)
    References.md, references.bib, Audit.md

The graph shows ideas (teal) linked to papers (orange) and to each other. Every link is
path-qualified ([[Tether/My Topic/Ideas/I-01 ...|...]]) so several projects in one vault never
cross-link, and graph colours are merged into the vault's own settings.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from .models import Claim, Idea, Paper, Rejected

GENERATED_DIRS = ("Ideas", "Papers", "Claims", "Concepts")     # Claims/Concepts: layout before 0.8
OLD_DIRS = ("Claims", "Concepts")
MARKER = "generator: tether"
STANCE_LABEL = {"supports": "Supports", "qualifies": "Qualifies", "contradicts": "Contradicts"}
LINK_LABEL = {"builds_on": "builds on", "in_tension": "is in tension with", "related": "is related to"}


def safe(name: str, limit: int = 80) -> str:
    name = re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", name)
    return re.sub(r"\s+", " ", name).strip()[:limit].rstrip(" .")


def paper_note(p: Paper) -> str:
    return safe(f"{p.id} {p.citation.short()}")


def idea_note(i: Idea) -> str:
    return safe(f"{i.id} {i.title}", 70)


def _work_cite(w) -> str:
    if not w.authors:
        return str(w.year or "n.d.")
    last = w.authors[0].split()[-1]
    who = last if len(w.authors) == 1 else (f"{last} & {w.authors[1].split()[-1]}" if len(w.authors) == 2 else f"{last} et al.")
    return f"{who}, {w.year or 'n.d.'}"


def _n(k: int, word: str) -> str:
    return f"{k} {word}{'' if k == 1 else 's'}"


def _yaml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _bibkey(p: Paper) -> str:
    last = (p.citation.authors[0].split()[-1] if p.citation.authors else "anon")
    return re.sub(r"\W", "", f"{last}{p.citation.year or 'nd'}{p.id}").lower()


def _prefix(out: Path, vault_root: Optional[Path]) -> str:
    if vault_root is None:
        return ""
    try:
        rel = out.resolve().relative_to(vault_root.resolve())
    except ValueError:
        return ""
    return "" if str(rel) == "." else rel.as_posix() + "/"


def _clean_previous(out: Path) -> int:
    """Remove notes tether wrote on an earlier build (only files carrying tether's marker; your own
    notes in these folders stay). Old Claims/ and Concepts/ folders are removed once empty."""
    removed = 0
    for d in GENERATED_DIRS:
        for f in (out / d).glob("*.md"):
            try:
                if MARKER in f.read_text(encoding="utf-8")[:400]:
                    f.unlink()
                    removed += 1
            except OSError:
                pass
    for d in OLD_DIRS:
        try:
            (out / d).rmdir()                  # only succeeds if nothing of yours is left in it
        except OSError:
            pass
    return removed


def _merge_graph_colors(vault_root: Path, prefix: str) -> None:
    cfg = vault_root / ".obsidian" / "graph.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
    except (OSError, json.JSONDecodeError):
        return                                      # never clobber a config we can't parse

    def q(sub: str) -> str:
        return f'path:"{prefix}{sub}"' if prefix else f"path:{sub}"
    ours = [{"query": q("Ideas"), "color": {"a": 1, "rgb": 0x2EC4B6}},
            {"query": q("Papers"), "color": {"a": 1, "rgb": 0xFF5A36}}]
    stale = {q(d) for d in ("Ideas", "Papers", "Concepts", "Claims")} | {f"{q('Claims')} tag:#limitation"}
    keep = [g for g in data.get("colorGroups", []) if g.get("query") not in stale]
    data["colorGroups"] = ours + keep
    data.setdefault("showOrphans", False)
    cfg.write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_vault(out: Path, papers: dict[str, Paper], claims: list[Claim], ideas: list[Idea], outline: dict,
                rejected: list[Rejected], dropped_points: int, question: str = "",
                vault_root: Optional[Path] = None, trimmed: int = 0, single: Optional[list[Idea]] = None,
                cited: Optional[list] = None) -> None:
    from .stages import disagreeing_papers, tensions as find_tensions

    single = single or []
    cited = cited or []
    out = Path(out)
    _clean_previous(out)
    for sub in ("Ideas", "Papers"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    pre = _prefix(out, vault_root)

    def link(target: str, label: Optional[str] = None) -> str:
        if not pre and label is None:
            return f"[[{target}]]"
        return f"[[{pre}{target}|{label or target.split('/')[-1]}]]"

    def cell(s: str) -> str:                      # a "|" inside a table cell must be escaped for Obsidian
        return s.replace("|", "\\|")

    def L_paper(p: Paper, label: Optional[str] = None) -> str:
        return link(f"Papers/{paper_note(p)}", label or p.citation.short())

    def L_idea(i: Idea) -> str:
        return link(f"Ideas/{idea_note(i)}", i.title)

    def L_pdf(p: Paper, page: int) -> str:
        return link(f"Sources/{Path(p.path).name}#page={page}", f"PDF p. {page}") if p.path else ""

    claim = {c.id: c for c in claims}
    by_paper: dict[str, list[Claim]] = defaultdict(list)
    for c in claims:
        by_paper[c.paper_id].append(c)
    ideas_of_claim: dict[str, list[Idea]] = defaultdict(list)
    ideas_of_paper: dict[str, list[Idea]] = defaultdict(list)
    for i in ideas:
        for e in i.evidence:
            ideas_of_claim[e.claim].append(i)
            if i not in ideas_of_paper[claim[e.claim].paper_id]:
                ideas_of_paper[claim[e.claim].paper_id].append(i)
    idea_by_id = {i.id: i for i in ideas}

    def papers_of(i: Idea) -> list[Paper]:
        seen = []
        for e in i.evidence:
            p = papers[claim[e.claim].paper_id]
            if p not in seen:
                seen.append(p)
        return seen

    def cite(c: Claim) -> str:
        """In-text citation, APA style: (Huang et al., 2026, p. 6), the name linking to the paper's note."""
        return f"({L_paper(papers[c.paper_id])}, p. {c.page})"

    def quoted(c: Claim, linked: bool = True) -> str:
        who = cite(c) if linked else f"({papers[c.paper_id].citation.short()}, p. {c.page})"
        return f"“{c.quote.strip().strip(chr(34)).strip()}” {who}"

    def compact_refs(cs: list[Claim]) -> str:
        """'Chen et al., 2024 (pp. 3, 7); Ruiz, 2022 (p. 2)', each paper linked once."""
        pages: dict[str, list[int]] = {}
        for c in cs:
            pages.setdefault(c.paper_id, [])
            if c.page not in pages[c.paper_id]:
                pages[c.paper_id].append(c.page)
        out = []
        for pid, pg in pages.items():
            pg = sorted(pg)
            out.append(f"{L_paper(papers[pid])}, {'p.' if len(pg) == 1 else 'pp.'} {', '.join(map(str, pg))}")
        return "(" + "; ".join(out) + ")" if out else ""

    def evidence_block(c: Claim) -> list[str]:
        p = papers[c.paper_id]
        lines = [f"- {c.text} {cite(c)}{' · ' + L_pdf(p, c.page) if p.path else ''}"]
        if c.qualifiers:
            lines.append(f"    - *Scope:* {c.qualifiers}")
        lines.append(f"    > {quoted(c)}")
        return lines

    # ---- ideas
    for i in ideas:
        ps = papers_of(i)
        lines = ["---", MARKER, "type: idea", f"papers: {len(ps)}", f"evidence: {len(i.evidence)}",
                 f"check: {i.check}", "tags: [idea]", "---", "", f"# {i.title}", "", f"**{i.statement}**", ""]
        if i.scope:
            lines += ["> [!warning] Narrowed by the fact-check", f"> {i.scope}", ""]
        lines += [f"Drawn from {_n(len(ps), 'paper')}: " + ", ".join(L_paper(p) for p in ps), ""]
        for stance in ("supports", "qualifies", "contradicts"):
            group = [claim[e.claim] for e in i.evidence if e.stance == stance]
            if group:
                lines.append(f"## {STANCE_LABEL[stance]}")
                for c in group:
                    lines += evidence_block(c)
                lines.append("")
        if i.links:
            lines.append("## Connected ideas")
            lines += [f"- This idea {LINK_LABEL[l.kind]} {L_idea(idea_by_id[l.to])}: {l.why}"
                      for l in i.links if l.to in idea_by_id]
            lines.append("")
        (out / "Ideas" / f"{idea_note(i)}.md").write_text("\n".join(lines), encoding="utf-8")

    # ---- papers
    for p in papers.values():
        cit = p.citation
        mine = by_paper.get(p.id, [])
        lines = ["---", MARKER, "type: paper", f"title: {_yaml_str(cit.title)}", f"year: {cit.year or ''}",
                 f"authors: {json.dumps(cit.authors[:15], ensure_ascii=False)}",
                 f"doi: {cit.doi or ''}", f"metadata: {cit.source}{'' if cit.verified else ' (unverified)'}",
                 f"source: {p.source_kind}", "tags: [paper]", "---", "", f"# {cit.title}", "", cit.apa(), ""]
        opens = []
        if p.path:
            opens.append(link(f"Sources/{Path(p.path).name}", "Open the PDF"))
        if cit.zotero_key:
            opens.append(f"[Open in Zotero](zotero://select/library/items/{cit.zotero_key})")
        if opens:
            lines += [" · ".join(opens), ""]
        if not cit.verified:
            lines += ["> [!caution] Metadata not matched to an external record. Check authors/year before citing.", ""]
        if p.source_kind == "abstract-only":
            lines += ["> [!caution] Only the abstract was available; claims come from the abstract alone.", ""]
        if ideas_of_paper.get(p.id):
            lines += ["## Feeds these ideas", *[f"- {L_idea(i)}" for i in ideas_of_paper[p.id]], ""]
        else:
            lines += ["> [!note] Shares no central idea with the other papers",
                      "> Either it's off-topic for this project, or it covers ground nobody else here does. "
                      "See the Overview.", ""]
        mine_single = [s1 for s1 in single if claim[s1.evidence[0].claim].paper_id == p.id]
        if mine_single:
            lines.append("## Single-source findings")
            lines.append("*Results only this paper reports in the project. Cite them as this paper's finding.*")
            lines += [f"- {s1.title}" for s1 in mine_single]
            lines.append("")
        theirs = [(w, c) for w in cited for c in w.cited_by if c["paper"] == p.id]
        if theirs:
            lines.append("## Cited work behind its claims")
            lines.append("*Claims this paper reports from other work. They don't count as this paper's evidence; "
                         "the cited paper is the source to read.*")
            for w, c in theirs:
                who = _work_cite(w)
                where = f"p. {c['page']}" if c.get("page") else "page not found"
                tail = " · already in this project" if getattr(w, "in_project", False) else (
                    f" · [open]({w.url})" if w.url else "") + (" · free PDF" if w.free_pdf else "")
                lines.append(f"- {c['claim']}")
                lines.append(f"    > “{c['quote'].strip()}” ({p.citation.short()}, {where})")
                lines.append(f"    - Cites: **{w.title}** ({who}){tail}")
            lines.append("")
        if mine:
            lines.append("## Key claims")
            for c in mine:
                tag = {"finding": "Finding", "method": "Method", "limitation": "Limitation",
                       "hypothesis": "Hypothesis", "definition": "Definition"}.get(c.kind, c.kind.title())
                lines.append(f"- **{tag}**: {c.text} (p. {c.page}{' · ' + L_pdf(p, c.page) if p.path else ''})")
                if c.qualifiers:
                    lines.append(f"    - *Scope:* {c.qualifiers}")
                lines.append(f"    > {quoted(c, linked=False)}")
            lines.append("")
        else:
            lines += ["No claims from this paper passed verification. See [[Audit]].", ""]
        (out / "Papers" / f"{paper_note(p)}.md").write_text("\n".join(lines), encoding="utf-8")

    # ---- outline
    lines = ["# Literature Review Outline", ""]
    if question:
        lines += [f"**Research question:** {question}", ""]
    lines += ["> [!info] Scaffold, not finished prose",
              "> Each point is an idea checked against the papers' own words. Open the idea to see the exact "
              "quotes before you write the sentence yourself.", ""]
    for n, sec in enumerate(outline.get("sections", []), 1):
        lines.append(f"## {n}. {sec['title']}")
        if sec.get("note"):
            lines += [f"*{sec['note']}*", ""]
        for iid in sec["ideas"]:
            i = idea_by_id.get(iid)
            if not i:
                continue
            lines.append(f"- **{L_idea(i)}**: {i.statement}"
                         + (f" *(scope: {i.scope})*" if i.scope and not i.narrowed_from else ""))
            for stance, label in (("supports", "Evidence"), ("qualifies", "Qualified by"), ("contradicts", "Against")):
                refs = compact_refs([claim[e.claim] for e in i.evidence if e.stance == stance])
                if refs:
                    lines.append(f"    - {label}: {refs}")
        lines.append("")
    if single:
        lines += ["## Single-source findings",
                  "*Only one paper in this project reports each of these. Cite them as that paper's result, not as "
                  "a consensus, or find papers that replicate or dispute them.*", ""]
        for s1 in single:
            cs = [claim[e.claim] for e in s1.evidence]
            lines.append(f"- **{s1.title}** {compact_refs(cs)}")
            lines += [f"    - {c.text}: {quoted(c)}" for c in cs]
        lines.append("")
    if outline.get("gaps"):
        lines.append("## Open questions and gaps")
        for g in outline["gaps"]:
            refs = compact_refs([claim[cid] for cid in g["claims"] if cid in claim])
            lines.append(f"- {g['text']}" + (f" {refs}" if refs else ""))
        lines.append("")
    (out / "Lit Review Outline.md").write_text("\n".join(lines), encoding="utf-8")

    # ---- references
    ordered = sorted(papers.values(), key=lambda p: (p.citation.authors[:1] or [p.citation.title])[0].split()[-1])
    (out / "References.md").write_text(
        "# References\n\n" + "\n".join(
            f"- {p.citation.apa()}" + ("" if p.citation.verified else " **[metadata unverified]**")
            + f" ({L_paper(p, paper_note(p))})" for p in ordered) + "\n", encoding="utf-8")
    bib = []
    for p in ordered:
        c = p.citation
        fields = {"title": c.title, "author": " and ".join(c.authors), "year": c.year,
                  "journal": c.venue, "doi": c.doi, "url": c.url}
        body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields.items() if v)
        bib.append(f"@article{{{_bibkey(p)},\n{body}\n}}")
    (out / "references.bib").write_text("\n\n".join(bib) + "\n", encoding="utf-8")

    # ---- overview
    plist = list(papers.values())
    lines = ["# Overview", "",
             f"{_n(len(ideas), 'central idea')} drawn from {_n(len(plist), 'paper')} "
             f"({_n(len(claims), 'verified claim')}). Every idea draws on at least two papers"
             + (f"; {_n(len(single), 'single-source finding')} are listed separately in the outline" if single else "")
             + f". Start with {link('Lit Review Outline')}.", "",
             "## The ideas", ""]
    for i in ideas:
        ps = papers_of(i)
        lines.append(f"- **{L_idea(i)}** ({_n(len(ps), 'paper')}): {i.statement}")
    lines += ["", "## Which papers feed which idea", "",
              "| Idea | " + " | ".join(p.id for p in plist) + " |", "|---|" + "---|" * len(plist)]
    for i in ideas:
        counts = Counter(claim[e.claim].paper_id for e in i.evidence)
        stance = {claim[e.claim].paper_id: e.stance for e in i.evidence if e.stance == "contradicts"}
        row = []
        for p in plist:
            if stance.get(p.id):
                row.append("✕")
            else:
                row.append(("●" * min(counts[p.id], 3)) or "·")
        lines.append(f"| {cell(L_idea(i))} | " + " | ".join(row) + " |")
    lines += ["", "● supporting or qualifying evidence · ✕ contradicting evidence", "",
              "**Papers:** " + " · ".join(f"{p.id} = {L_paper(p)}" for p in plist), ""]
    disagree = [(i, disagreeing_papers(i, claim)) for i in ideas]
    disagree = [(i, ps) for i, ps in disagree if ps]
    pairs = find_tensions(ideas, claim)
    if disagree or pairs:
        lines += ["## Where the papers disagree", ""]
        for i, ps in disagree:
            lines.append(f"- {L_idea(i)}: contradicted by " + ", ".join(L_paper(papers[pid]) for pid in ps))
        for a, b, why in pairs:
            lines.append(f"- {L_idea(a)} vs {L_idea(b)}" + (f": {why}" if why else ""))
        lines.append("")
    used = {claim[e.claim].paper_id for i in ideas for e in i.evidence}
    isolated = [p for p in plist if p.id not in used]
    if isolated and ideas:
        lines += ["## Papers that share no idea with the others", "",
                  "These papers didn't connect to any central idea. They may be off-topic for this project "
                  "(consider removing them), or they cover ground no other paper here does (consider adding papers "
                  "that do). Their own results are under Single-source findings in the outline.", ""]
        lines += [f"- {L_paper(p)}: {p.citation.title}" for p in isolated]
        lines.append("")
    suggest = [w for w in cited if not getattr(w, "in_project", False) and (w.year or w.doi)]
    if suggest:
        lines += ["## Suggested reading: cited by your papers", "",
                  "Papers your sources rely on for claims they report from other work. The more of your papers cite one, "
                  "the more likely it belongs in this project. Add them from the app (Suggested papers).", ""]
        for w in suggest[:15]:
            by = list(dict.fromkeys(c["paper"] for c in w.cited_by))
            lines.append(f"- **{w.title}** ({_work_cite(w)})" + (f" · [open]({w.url})" if w.url else "")
                         + f": cited by " + ", ".join(L_paper(papers[pid]) for pid in by if pid in papers))
        lines.append("")
    lines += [f"See {link('References')} for the bibliography and {link('Audit')} for what was filtered out.", ""]
    (out / "00 Overview.md").write_text("\n".join(lines), encoding="utf-8")

    # ---- audit
    reasons = Counter(r.reason.split(":")[0] for r in rejected)
    lines = ["# Audit", "",
             "What tether filtered out, so you can see it isn't hiding anything.", "",
             f"- Central claims kept: **{len(claims)}** across {_n(len(plist), 'paper')}",
             f"- Claims trimmed as not central (verified, just less important): **{trimmed}**",
             f"- Ideas narrowed by the fact-check: **{sum(i.check == 'partial' for i in ideas)}**",
             f"- Fuzzy quote matches (score < 100, check these first): **{sum(c.match_score < 100 for c in claims)}**",
             f"- Ideas, sections or gaps removed for citing no real claim: **{dropped_points}**", ""]
    lines += [f"- Single-paper results kept as single-source findings (not central ideas): **{len(single)}**"]
    lines += [f"- Rejected ({k}): **{n}**" for k, n in reasons.most_common()]
    narrowed = [i for i in ideas if i.narrowed_from]
    if narrowed:
        lines += ["", "## Ideas the fact-check reworded", ""]
        for i in narrowed:
            lines += [f"### {L_idea(i)}", f"- Was: {i.narrowed_from}", f"- Now: {i.statement}",
                      f"- Why: {i.scope}", ""]
    lines += ["", "## Rejected", ""]
    for r in rejected:
        lines += [f"### {r.paper_id}: {r.reason}", f"- {'Idea' if r.paper_id == 'idea' else 'Claim'}: {r.text}",
                  f"- {'Title' if r.paper_id == 'idea' else 'Quote given'}: \"{r.quote}\"", ""]
    (out / "Audit.md").write_text("\n".join(lines), encoding="utf-8")

    _merge_graph_colors(vault_root or out, pre)
