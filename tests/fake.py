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

    def call(self, *, system, user, name, schema, judge=False, role=None):
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
        out.append({"text": "Drones can carry unlimited payloads.", "kind": "finding",
                    "quote": "quadrotors can lift arbitrarily heavy payloads without any loss of stability",
                    "page": 1, "qualifiers": "", "importance": 3, "secondhand": False})
        out.append({"text": "Prior work used PID.", "kind": "finding",
                    "quote": "Earlier studies by Smith et al. relied on cascaded PID control loops for hover",
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

    # synthesize: one idea per keyword, a contradicting stance, and three things that must be dropped
    def _synthesis(self, user):
        claims = json.loads(user)["claims"]
        groups = {"kalman": [], "policy": [], "other": []}
        for c in claims:
            t = c["claim"].lower()
            groups["kalman" if "kalman" in t else "policy" if ("policy" in t or "payload" in t) else "other"].append(c)
        ideas = []
        if groups["kalman"]:
            ideas.append({"key": "A", "title": "Kalman filtering of payload mass",
                          "statement": "A Kalman filter can estimate payload mass, though it converges slowly for off-center loads.",
                          "evidence": [{"claim": c["id"], "stance": "supports"} for c in groups["kalman"]],
                          "links": [{"to": "B", "kind": "builds_on", "why": "the estimate feeds the policy"},
                                    {"to": "Z", "kind": "related", "why": "link to an idea that doesn't exist"}]})
        if groups["policy"]:
            ev = [{"claim": c["id"], "stance": "contradicts" if "degrades" in c["claim"] else "supports"}
                  for c in groups["policy"]]
            if not any(e["stance"] == "supports" for e in ev):
                ev[0]["stance"] = "supports"
            ideas.append({"key": "B", "title": "Payload-aware policies",
                          "statement": "Conditioning the policy on payload helps in simulation, but a wrong estimate hurts.",
                          "evidence": ev, "links": []})
        ideas.append({"key": "C", "title": "Fabricated idea", "statement": "Everything works perfectly.",
                      "evidence": [{"claim": "C-9999", "stance": "supports"}], "links": []})
        ideas.append({"key": "D", "title": "Overreaching idea", "statement": "Drones always succeed at every grasp.",
                      "evidence": [{"claim": claims[0]["id"], "stance": "supports"}], "links": []})
        return {"ideas": ideas,
                "outline": [{"title": "Estimation and control", "note": "how the pieces fit", "ideas": ["A", "B", "D"]},
                            {"title": "Ghost section", "note": "", "ideas": ["Q"]}],
                "gaps": [{"text": "Nobody tested off-center payloads on hardware.", "claims": [claims[-1]["id"]]},
                         {"text": "A gap citing nothing real.", "claims": ["C-9999"]}]}

    def _idea_checks(self, user):
        items = json.loads(user)
        out = []
        for it in items:
            if "always" in it["statement"]:
                out.append({"index": it["index"], "verdict": "unsupported", "scope": "", "reason": "not in the evidence"})
            elif "simulation" in it["statement"]:
                out.append({"index": it["index"], "verdict": "partial", "scope": "only shown in simulation", "reason": "scope"})
            else:
                out.append({"index": it["index"], "verdict": "supported", "scope": "", "reason": "ok"})
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
