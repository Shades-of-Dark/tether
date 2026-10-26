"""A scripted stand-in for Claude so the pipeline can be tested offline.
It deliberately misbehaves (a fabricated quote, a secondhand claim, a bogus selection id, an
idea citing a claim that doesn't exist, an overreaching idea) to prove the checks catch it."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pymupdf


class FakeLLM:
    profile = "balanced"

    def __init__(self):
        self.calls = []
        self.roles = []
        self.payloads = {}

    def model_for(self, role):
        return "fake"

    def call(self, *, system, user, name, schema, judge=False, role=None, max_tokens=None):
        self.calls.append(name)
        self.roles.append((name, role or ("check" if judge else "read")))
        self.payloads.setdefault(name, []).append(user)
        return getattr(self, f"_{name}")(user)

    # extract: every "FINDING: ..." sentence becomes a claim, plus two bad ones
    def _claims(self, user):
        text = user.split("<paper_text>")[1].split("</paper_text>")[0]
        out = []
        parts = re.split(r"\[\[page (\d+)\]\]", text)
        for num, body in zip(parts[1::2], parts[2::2]):
            flat = re.sub(r"\s+", " ", body)
            for m in re.finditer(r"FINDING: (.+?\.)", flat):
                sent = m.group(1)
                clean = sent.replace("{", "").replace("}", "")
                out.append({"text": clean, "kind": "finding", "quote": sent, "page": int(num),
                            "qualifiers": "", "importance": 3, "secondhand": False})
        out.append({"text": "The line can carry any load.", "kind": "finding",
                    "quote": "the widget line can carry arbitrarily heavy loads without any loss of throughput",
                    "page": 1, "qualifiers": "", "importance": 3, "secondhand": False})
        out.append({"text": "Prior work used fixed schedules.", "kind": "finding",
                    "quote": "Earlier studies by Smith et al. relied on one fixed schedule for every shift",
                    "page": 1, "qualifiers": "", "importance": 1, "secondhand": True})
        return {"claims": out}

    # select: keep everything, in order, plus an id that doesn't exist
    def _selection(self, user):
        items = json.loads(user.split("Candidate claims:\n", 1)[1])
        return {"keep": [it["id"] for it in items] + ["k999"]}

    def _verdicts(self, user):
        items = json.loads(user)
        res = []
        for it in items:
            if "always" in it["claim"]:
                res.append({"index": it["index"], "verdict": "unsupported", "missing_qualifier": "", "reason": "overstated"})
            elif "simulation" in it["claim"]:
                res.append({"index": it["index"], "verdict": "partial", "missing_qualifier": "tested only in simulation", "reason": "scope"})
            else:
                res.append({"index": it["index"], "verdict": "supported", "missing_qualifier": "", "reason": "ok"})
        return {"verdicts": res}

    # synthesize: one idea per keyword, a contradicting stance, and two ideas that must not survive
    def _synthesis(self, user):
        claims = json.loads(user)["claims"]
        groups = {"filter": [], "scheduler": [], "other": []}
        for c in claims:
            t = c["claim"].lower()
            groups["filter" if "beta filter" in t else "scheduler" if ("schedul" in t or "weight" in t) else "other"].append(c)
        ideas = []
        if groups["filter"]:
            ideas.append({"key": "A", "title": "Beta filtering of widget weight",
                          "statement": "A beta filter can estimate widget weight, though it converges slowly for off-center loads.",
                          "supports": [c["id"] for c in groups["filter"]], "qualifies": [], "contradicts": [],
                          "links": [{"to": "B", "kind": "builds_on", "why": "the estimate feeds the scheduler"},
                                    {"to": "Z", "kind": "related", "why": "link to an idea that doesn't exist"}]})
        if groups["scheduler"]:
            sup = [c["id"] for c in groups["scheduler"] if "degrades" not in c["claim"]]
            con = [c["id"] for c in groups["scheduler"] if "degrades" in c["claim"]]
            if not sup:
                sup, con = con[:1], con[1:]
            ideas.append({"key": "B", "title": "Weight-aware scheduling",
                          "statement": "Conditioning the scheduler on weight helps in simulation, but a wrong estimate hurts.",
                          "supports": sup, "qualifies": [], "contradicts": con, "links": []})
        ideas.append({"key": "C", "title": "Fabricated idea", "statement": "Everything works perfectly.",
                      "supports": ["C-9999"], "qualifies": [], "contradicts": [], "links": []})
        ideas.append({"key": "D", "title": "Overreaching idea", "statement": "The line always succeeds on every widget.",
                      "supports": [claims[0]["id"], claims[-1]["id"]], "qualifies": [], "contradicts": [], "links": []})
        ideas.append({"key": "E", "title": "Single-paper result", "statement": "One paper reports this on its own.",
                      "supports": [claims[min(1, len(claims) - 1)]["id"]], "qualifies": [], "contradicts": [],
                      "links": [{"to": "A", "kind": "related", "why": "a link that must be dropped"}]})
        return {"ideas": ideas}

    # outline: a real section, a section of ghost ideas, a real gap and a gap citing nothing
    def _outline(self, user):
        ideas = json.loads(user)["ideas"]
        some_claim = ideas[-1]["claims"][-1]["id"] if ideas else "C-0000"
        return {"sections": [{"title": "Estimation and scheduling", "note": "how the pieces fit",
                              "ideas": [i["id"] for i in ideas]},
                             {"title": "Ghost section", "note": "", "ideas": ["I-99"]}],
                "gaps": [{"text": "Nobody tested off-center widgets on a real line.", "claims": [some_claim]},
                         {"text": "A gap citing nothing real.", "claims": ["C-9999"]}]}

    def _idea_checks(self, user):
        items = json.loads(user)
        out = []
        for it in items:
            if "always" in it["statement"]:
                out.append({"index": it["index"], "verdict": "unsupported", "scope": "", "reason": "not in the evidence",
                            "revised_statement": ""})
            elif "simulation" in it["statement"]:
                out.append({"index": it["index"], "verdict": "partial", "scope": "only shown in simulation", "reason": "scope",
                            "revised_statement": "Weight-aware scheduling helped only in simulation; a wrong estimate hurt."})
            else:
                out.append({"index": it["index"], "verdict": "supported", "scope": "", "reason": "ok",
                            "revised_statement": ""})
        return {"verdicts": out}


def make_pdf(path: Path, title: str, pages: list[list[str]]) -> Path:
    doc = pymupdf.open()
    for i, lines in enumerate(pages):
        page = doc.new_page()
        body = "\n\n".join(lines)
        top = 72
        if i == 0:
            page.insert_text((72, 90), title, fontsize=18)
            top = 120
        page.insert_textbox(pymupdf.Rect(72, top, 540, 760), body, fontsize=10)
    doc.set_metadata({"title": title, "author": "Ada Lovelace; Alan Turing", "creationDate": "D:20250101"})
    doc.save(path)
    return path
