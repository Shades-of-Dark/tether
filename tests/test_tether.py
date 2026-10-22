from pathlib import Path

from tether.models import Page
from tether.pipeline import run
from tether.textmatch import find_quote

from .fake import FakeLLM, make_pdf

FILLER = ("This section describes the general setup, the example datasets, and the evaluation "
          "protocol used throughout this placeholder study of a fictional widget line. ") * 3


def test_exact_quote_found_on_other_page():
    pages = [Page(1, "intro text " * 50), Page(2, "The method reduced the error rate by 31% on the test set.")]
    m = find_quote("The method reduced the error rate by 31% on the test set.", pages, hint_page=1)
    assert m and m.page == 2 and m.score == 100


def test_hyphenation_and_ligatures_still_match():
    pages = [Page(1, "We estimate the work-\nload size with an extended Alﬁ filter using sample counts.")]
    assert find_quote("We estimate the workload size with an extended Alfi filter using sample counts", pages)


def test_fabricated_and_short_quotes_rejected():
    pages = [Page(1, "The widget line ran steadily with a 200 g load placed near its center.")]
    assert find_quote("The widget line ran perfectly with any load of any size whatsoever.", pages) is None
    assert find_quote("The widget line", pages) is None   # too short to be evidence


def test_low_value_sections_are_skipped():
    from tether.ingest import drop_low_value_sections
    body = "Real content sentence about the method and results. " * 30
    p1 = f"Abstract\nWe study X.\n1 Introduction\n{body}\n2 Related Work\nSmith et al. did A.\n{'Related prose. ' * 40}\n3 Method\n{body}"
    p2 = f"4 Results\n{body}\n5 Conclusion\n{body}\nAppendix A\n{'Derivation. ' * 40}"
    pages, dropped = drop_low_value_sections([Page(1, p1), Page(2, p2)])
    assert dropped == ["Related Work", "Appendix A"]
    assert "Smith" not in pages[0].text and "Derivation" not in pages[1].text
    assert "3 Method" in pages[0].text and "5 Conclusion" in pages[1].text
    # never guts a paper: if "skipping" would remove most of it, keep everything
    weird = [Page(1, "1 Background\n" + body * 3 + "\n2 Results\nshort")]
    assert drop_low_value_sections(weird)[0][0].text == weird[0].text


def many_findings_pdf(tmp_path, n=9, name="many.pdf", title="A Paper With Many Findings"):
    """n findings, 4 per page, each page long enough to be its own reading chunk."""
    lines = [f"FINDING: Result number {i} shows the {'beta filter' if i % 2 else 'scheduler'} variant {i} improves accuracy by {i} percent." for i in range(n)]
    pages = []
    for k in range(0, n, 4):
        pages.append([FILLER * 2, *lines[k:k + 4], FILLER * 2])
    doc_pages = [[p[0], *p[1:-1]] for p in pages]
    pdf = make_pdf(tmp_path / name, title, doc_pages)
    import pymupdf                                  # pad each page with more text so pages don't share a chunk
    d = pymupdf.open(pdf)
    out = pymupdf.open()
    for page in d:
        np_ = out.new_page()
        np_.insert_textbox(pymupdf.Rect(36, 36, 576, 780), page.get_text() + "\n" + (FILLER * 22), fontsize=4)
    out.set_metadata(d.metadata)
    tmp = tmp_path / ("x_" + name)
    out.save(tmp)
    d.close(); out.close()
    tmp.replace(pdf)
    return pdf


def test_end_to_end(tmp_path: Path):
    a = make_pdf(tmp_path / "a.pdf", "Weight Aware Scheduling for a Widget Line", [
        [FILLER, "FINDING: Conditioning the scheduler on widget weight improves {alpha scheduling} throughput in simulation.",
         FILLER],
        [FILLER, "FINDING: Our {beta filter} estimates widget weight within five percent after two seconds of operation.",
         "FINDING: Off-center widgets always make the line stall during peak load.", FILLER],
    ])
    b = make_pdf(tmp_path / "b.pdf", "Robust Throughput Without Weight Knowledge", [
        [FILLER, "FINDING: Throughput degrades when the {alpha scheduling} scheduler receives a wrong weight estimate.",
         "FINDING: The {beta filtering} stage converges slowly for widgets loaded far from the belt center.",
         FILLER],
    ])
    llm = FakeLLM()
    out = tmp_path / "vault"
    stats = run(llm, pdfs=[a, b], urls=[], topic=None, out=out, question="Does weight knowledge help?",
                lookup=False)

    # 5 findings: the overstated one ("always") fails the claim check; fabricated + secondhand rejected
    assert stats["papers"] == 2 and stats["claims"] == 4
    # ideas: the fabricated-citation idea is dropped by code, the overreaching one by the idea check
    assert stats["ideas"] == 2 and stats["contradictions"] == 1
    assert stats["dropped_points"] == 3              # fabricated idea + ghost section + gap citing nothing
    audit = (out / "Audit.md").read_text()
    assert "quote not found in paper text" in audit and "secondhand" in audit and "unsupported idea" in audit

    ideas = {p.stem: p.read_text() for p in (out / "Ideas").glob("*.md")}
    assert set(ideas) == {"I-01 Beta filtering of widget weight", "I-02 Weight-aware scheduling"}
    kal, pol = ideas["I-01 Beta filtering of widget weight"], ideas["I-02 Weight-aware scheduling"]
    assert "## Supports" in kal and "Lovelace & Turing, 2025a" in kal and "Lovelace & Turing, 2025b" in kal
    assert "> Our {beta filter} estimates widget weight" in kal                   # verbatim quote from the PDF
    assert "Sources/a.pdf#page=2|PDF p. 2" in kal                                  # jumps to the page
    assert "builds on [[Ideas/I-02 Weight-aware scheduling|Weight-aware scheduling]]" in kal
    assert "doesn't exist" not in kal                                              # link to a missing idea dropped
    assert "## Contradicts" in pol and "only shown in simulation" in pol          # narrowed by the idea check
    assert not any("Fabricated" in k or "Overreaching" in k for k in ideas)

    outline = (out / "Lit Review Outline.md").read_text()
    assert "Estimation and control" in outline and "Ghost section" not in outline
    assert "C-9999" not in outline and "citing nothing real" not in outline
    assert "Nobody tested off-center widgets" in outline
    overview = (out / "00 Overview.md").read_text()
    assert "2 central ideas drawn from 2 papers" in overview and "Where the papers disagree" in overview
    paper = next((out / "Papers").glob("P01*")).read_text()
    assert "## Feeds these ideas" in paper and "## Key claims" in paper and "tested only in simulation" in paper
    assert not (out / "Claims").exists() and not (out / "Concepts").exists()
    assert (out / "references.bib").read_text().count("@article") == 2
    refs = (out / "References.md").read_text()
    assert "(2025a)" in refs and "(2025b)" in refs                               # same authors, same year
    assert (out / ".obsidian" / "graph.json").exists()
    assert sorted(p.name for p in (out / "Sources").glob("*.pdf")) == ["a.pdf", "b.pdf"]

    # second run: everything cached, zero calls to Claude
    llm2 = FakeLLM()
    run(llm2, pdfs=[a, b], urls=[], topic=None, out=out, lookup=False)
    assert llm2.calls == []


def test_selection_keeps_only_central_claims(tmp_path: Path):
    from tether import stages
    pdf = many_findings_pdf(tmp_path, n=16)
    from tether.ingest import chunk_pages, read_pdf
    assert len(chunk_pages("P", read_pdf(pdf)[0])) >= 4           # really several chunks
    llm = FakeLLM()
    llm._selection = lambda user: {"keep": ["k3", "k1", "k3", "k42", "kX"]}     # dupes + bogus ids
    stats = run(llm, out=tmp_path / "P", pdfs=[pdf], lookup=False)
    assert stats["claims"] == 2 and stats["trimmed"] == 14
    # only the selected claims were fact-checked (that's the saving), and in the chosen order
    checked = [it["claim"] for u in llm.payloads["verdicts"] for it in __import__("json").loads(u)]
    assert len(checked) == 2 and "variant 3" in checked[0] and "variant 1" in checked[1]
    assert "trimmed as not central" in (tmp_path / "P" / "Audit.md").read_text()
    assert stages.MAX_PER_CHUNK == 4 and stages.MAX_PER_PAPER == 12


def test_fuzzy_match_returns_original_text_on_word_boundaries():
    src = "Results. The Learned Scheduler reduced queue delay by thirty-one percent on the real widget line."
    # a dropped hyphen/plural is tolerated, and the vault shows the source's own wording
    m = find_quote("the learned scheduler reduced queue delays by thirty one percent on the real widget line",
                   [Page(1, src)])
    assert m and m.score < 100
    assert m.span.startswith("The Learned Scheduler") and m.span.endswith("line")
    # but a changed number is a different claim, and gets rejected
    assert find_quote("the learned scheduler reduced queue delay by 31 percent on the real widget line",
                      [Page(1, src)]) is None


def test_fuzzy_match_cannot_flip_numbers_or_negations():
    src = ("In all forty trials the weight-aware scheduler reduced the average queue "
           "delay by 31% relative to the baseline that had no knowledge of each widget's weight.")
    page = [Page(1, src)]
    swapped = src.replace("31%", "13%")
    negated = src.replace("reduced", "did not reduce")
    assert find_quote(src, page)
    assert find_quote(swapped, page) is None
    assert find_quote(negated, page) is None


def test_abstract_reconstruction_and_citation_format():
    from tether.models import Citation
    from tether.sources import _abstract
    assert _abstract({"abstract_inverted_index": {"widgets": [0], "move": [1], "well": [2]}}) == "widgets move well"
    c = Citation(title="X", authors=["Ada Lovelace", "Alan Turing", "Grace Hopper"], year=2024, doi="10.1/abc")
    assert c.short() == "Lovelace et al., 2024"
    assert c.apa().startswith("Lovelace, A., Turing, A., Hopper, G. (2024). X.")


def test_claude_client_matches_the_real_sdk(monkeypatch):
    """Every request must bind against the installed SDK's real signature; unsupported output
    modes fall back (structured -> tool -> text) and the working one is remembered per model;
    only transient errors are retried."""
    import inspect
    from types import SimpleNamespace as NS
    import anthropic
    import httpx
    import pytest
    from tether import config, llm as llmmod
    from tether.llm import ClaudeLLM, friendly_error

    monkeypatch.setattr(config, "_SHELL_ENV", {"ANTHROPIC_API_KEY": "test"})
    monkeypatch.setattr(llmmod.time, "sleep", lambda s: None)
    monkeypatch.setattr(llmmod, "_mode_for", {})
    client = ClaudeLLM(model="m", judge_model="j")
    real = inspect.signature(client.client.messages.create)
    sent, script = [], []
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def bad_request(msg):
        return anthropic.BadRequestError(msg, response=httpx.Response(400, request=req),
                                         body={"type": "error", "error": {"type": "invalid_request_error", "message": msg}})

    def create(**kw):
        real.bind(**kw)                                   # TypeError if tether passes an argument the SDK lacks
        sent.append(kw)
        step = script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(kw) if callable(step) else step

    monkeypatch.setattr(client.client.messages, "create", create)
    text = lambda t: NS(usage=NS(input_tokens=10, output_tokens=5), stop_reason="end_turn",
                        content=[NS(type="text", text=t)])
    tool = NS(usage=NS(input_tokens=1, output_tokens=1), stop_reason="tool_use",
              content=[NS(type="tool_use", input={"claims": [1]})])
    schema = {"type": "object", "properties": {"claims": {"type": "array", "items": {"type": "object",
              "properties": {"x": {"type": "string"}}}}}, "required": ["claims"]}

    # 1. structured outputs work: schema is sent closed + fully required
    script[:] = [text('{"claims": []}')]
    assert client.call(system="s", user="u", name="claims", schema=schema) == {"claims": []}
    fmt = sent[-1]["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["additionalProperties"] is False
    assert fmt["schema"]["properties"]["claims"]["items"]["additionalProperties"] is False
    assert "tool_choice" not in sent[-1]
    assert llmmod._mode_for["m"] == "structured"

    # 2. the judge model rejects structured outputs (and forced tools): falls back to auto tool use
    script[:] = [bad_request("output_config.format: structured outputs are not supported for this model"), tool]
    assert client.call(system="s", user="u", name="claims", schema=schema, judge=True) == {"claims": [1]}
    assert sent[-1]["tool_choice"] == {"type": "auto"} and llmmod._mode_for["j"] == "tool"
    script[:] = [tool]                                     # remembered: no second probe
    client.call(system="s", user="u", name="claims", schema=schema, judge=True)
    assert "output_config" not in sent[-1]

    # 3. text fallback parses fenced JSON
    monkeypatch.setattr(llmmod, "_mode_for", {})
    script[:] = [bad_request("output_config: not supported"), bad_request('tool_choice: type "tool" and "any" are not supported for this model.'),
                 text('```json\n{"claims": ["a"]}\n```')]
    assert client.call(system="s", user="u", name="claims", schema=schema) == {"claims": ["a"]}
    assert llmmod._mode_for["m"] == "text"

    # transient errors retry; real bugs and other 400s don't
    retries = []
    client.on_retry = retries.append
    script[:] = [anthropic.APIConnectionError(request=req), text('{"claims": []}')]
    client.call(system="s", user="u", name="claims", schema=schema)
    assert len(retries) == 1
    script[:] = [TypeError("boom")]
    with pytest.raises(TypeError):
        client.call(system="s", user="u", name="claims", schema=schema)
    script[:] = [bad_request("messages: text content blocks must be non-empty")]
    with pytest.raises(anthropic.BadRequestError):
        client.call(system="s", user="u", name="claims", schema=schema)

    # errors read like sentences in the app, with the raw detail kept for the log
    short, detail = friendly_error(bad_request('tool_choice: type "tool" and "any" are not supported for this model.'))
    assert short == 'Claude refused the request: tool_choice: type "tool" and "any" are not supported for this model.'
    assert "BadRequestError" in detail and "request_id" not in short
    assert friendly_error(anthropic.AuthenticationError("x", response=httpx.Response(401, request=req), body=None))[0].startswith("Anthropic rejected your API key")

def _two_papers(tmp_path):
    a = make_pdf(tmp_path / "a.pdf", "Weight Aware Scheduling for a Widget Line", [
        [FILLER, "FINDING: Our {beta filter} estimates widget weight within five percent after two seconds of operation.",
         FILLER]])
    b = make_pdf(tmp_path / "b.pdf", "Robust Throughput Without Weight Knowledge", [
        [FILLER, "FINDING: Throughput degrades when the {alpha scheduling} scheduler receives a wrong weight estimate.",
         "FINDING: The {beta filtering} stage converges slowly for widgets loaded far from the belt center.",
         FILLER]])
    return a, b


def test_project_inside_existing_vault(tmp_path: Path):
    import json
    vault = tmp_path / "MyVault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".obsidian" / "graph.json").write_text(json.dumps({"colorGroups": [
        {"query": "tag:#mine"}, {"query": 'path:"Tether/Example Review/Claims"'}], "scale": 2}))
    a, b = _two_papers(tmp_path)
    proj = vault / "Tether" / "Example Review"
    # notes left by tether 0.7 (one-note-per-claim layout), plus one of the user's own
    (proj / "Claims").mkdir(parents=True)
    (proj / "Concepts").mkdir()
    for i in range(30):
        (proj / "Claims" / f"C-{i:04d}.md").write_text("---\ngenerator: tether\nid: x\n---\nold claim")
    (proj / "Concepts" / "Example concept.md").write_text("---\ngenerator: tether\n---\nold concept")
    (proj / "Concepts" / "my thoughts.md").write_text("mine, not tether's")
    run(FakeLLM(), out=proj, pdfs=[a, b], vault_root=vault, lookup=False)

    assert not (proj / "Claims").exists()                              # emptied old folder removed
    assert [f.name for f in (proj / "Concepts").iterdir()] == ["my thoughts.md"]   # user's note kept
    outline = (proj / "Lit Review Outline.md").read_text()
    assert "[[Tether/Example Review/Ideas/I-01 Beta filtering of widget weight|Beta filtering of widget weight]]" in outline
    overview = (proj / "00 Overview.md").read_text()
    assert "| [[Tether/Example Review/Ideas/I-01 Beta filtering of widget weight\\|Beta filtering of widget weight]] |" in overview
    assert not (proj / ".obsidian").exists()                          # no nested vault
    g = json.loads((vault / ".obsidian" / "graph.json").read_text())
    queries = [q["query"] for q in g["colorGroups"]]
    assert g["scale"] == 2 and "tag:#mine" in queries                 # user's settings kept
    assert 'path:"Tether/Example Review/Ideas"' in queries and 'path:"Tether/Example Review/Papers"' in queries
    assert 'path:"Tether/Example Review/Claims"' not in queries         # old layout's colours removed

    (proj / "Ideas" / "my idea.md").write_text("my own idea note")
    (proj / "Sources" / "b.pdf").unlink()
    run(FakeLLM(), out=proj, vault_root=vault, lookup=False)
    assert (proj / "Ideas" / "my idea.md").exists()
    assert len(json.loads((vault / ".obsidian" / "graph.json").read_text())["colorGroups"]) == 3   # not duplicated


def test_env_file_roundtrip(tmp_path: Path, monkeypatch):
    from tether import config
    env = tmp_path / ".env"
    monkeypatch.setattr(config, "ENV_FILE", env)
    monkeypatch.setattr(config, "_SHELL_ENV", {})
    assert config.api_key() is None and env.exists()                  # template created, key left empty
    assert "ANTHROPIC_API_KEY=\n" in env.read_text()
    env.write_text(env.read_text().replace("ANTHROPIC_API_KEY=", "ANTHROPIC_API_KEY=sk-ant-test"))
    assert config.api_key() == "sk-ant-test"                         # picked up without a restart
    config.set_value("TETHER_VAULT", r"C:\Users\me\Obsidian Vault")
    text = env.read_text()
    assert "TETHER_VAULT=C:\\Users\\me\\Obsidian Vault" in text and "# Paste your Anthropic API key" in text
    assert config.get("TETHER_VAULT") == r"C:\Users\me\Obsidian Vault"
    monkeypatch.setattr(config, "_SHELL_ENV", {"ANTHROPIC_API_KEY": "from-shell"})
    assert config.api_key() == "from-shell"                          # real env var still wins


def test_missing_key_is_a_clear_error(tmp_path: Path, monkeypatch):
    import pytest
    from tether import config
    from tether.llm import ClaudeLLM, MissingKey
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(config, "_SHELL_ENV", {})
    with pytest.raises(MissingKey, match="Add it in Settings"):
        ClaudeLLM()


def test_app_flow(tmp_path: Path, monkeypatch):
    import time
    from fastapi.testclient import TestClient
    from tether import app as appmod, config
    import tether.llm as llmmod
    import tether.sources as src

    env = tmp_path / ".env"
    monkeypatch.setattr(config, "ENV_FILE", env)
    monkeypatch.setattr(config, "_SHELL_ENV", {})
    opened = []
    monkeypatch.setattr(appmod, "_open_uri", opened.append)

    class Fake(FakeLLM):
        model, judge_model, usage = "fake", "fake", {"input": 0, "output": 0}

        def __init__(self, **kw):
            super().__init__()
    monkeypatch.setattr(llmmod, "ClaudeLLM", Fake)
    monkeypatch.setattr(src, "lookup_by_title", lambda t: None)
    monkeypatch.setattr(appmod.builder, "debounce", 0.2)

    c = TestClient(appmod.app)
    st = c.get("/api/state").json()
    assert not st["key_set"] and not st["vault_ok"] and st["projects"] == []

    # key: validated, saved to .env, never echoed back in full
    assert c.post("/api/key", json={"key": "hello"}).status_code == 400
    real = "sk-ant-api03-" + "x" * 40 + "WXYZ"
    st = c.post("/api/key", json={"key": f"  {real} "}).json()
    assert st["key_set"] and st["key_hint"] == "sk-ant-…WXYZ"
    assert real not in c.get("/api/state").text
    assert f"ANTHROPIC_API_KEY={real}" in env.read_text()

    # other websites can't drive the local server
    assert c.post("/api/key", json={"key": real}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.get("/api/state", headers={"Host": "attacker.example"}).status_code == 403

    # vault: create a new one
    assert c.post("/api/vault", json={"path": str(tmp_path / "nope")}).status_code == 400
    st = c.post("/api/vault/create", json={"parent": str(tmp_path), "name": "My Notes"}).json()
    vault = tmp_path / "My Notes"
    assert st["vault_ok"] and vault.is_dir() and st["projects_folder"] == str(vault.resolve() / "Tether")

    # projects: nothing until the user makes one, then it lives in <vault>/Tether/<name>
    assert c.post("/api/projects/Anything/rebuild").status_code == 404
    p = c.post("/api/projects", json={"name": "Topic: one/two"}).json()
    assert p["name"] == "Topic one two" and (vault / "Tether" / "Topic one two" / ".tether").is_dir()
    assert c.post("/api/projects", json={"name": "Topic one two"}).status_code == 409
    name = p["name"]

    a, b = _two_papers(tmp_path)
    with open(a, "rb") as fa, open(b, "rb") as fb:
        r = c.post(f"/api/projects/{name}/upload", files=[
            ("files", ("a.pdf", fa, "application/pdf")), ("files", ("b.pdf", fb, "application/pdf")),
            ("files", ("notes.pdf", b"not a pdf", "application/pdf"))]).json()
    assert [f["name"] for f in r["files"]] == ["a.pdf", "b.pdf"] and r["skipped"] == ["notes.pdf"]

    # no build button: the upload alone triggers a rebuild, and a question edit right after
    # is folded into the same rebuild (debounced), not a second one
    c.post(f"/api/projects/{name}/question", json={"question": "Does it help?"})
    j = wait_for_update(c, name)
    assert j["state"] == "done" and j["run"] == 1, j
    st = c.get("/api/state").json()
    assert st["projects"][0]["claims"] == j["stats"]["claims"] and st["projects"][0]["question"] == "Does it help?"
    assert st["projects"][0]["ideas"] == j["stats"]["ideas"] == 2 and st["projects"][0]["current"]
    assert "[[Tether/Topic one two/Ideas/I-01" in (vault / "Tether" / name / "Lit Review Outline.md").read_text()

    c.post("/api/open", json={"project": name, "target": "obsidian"})
    assert opened[-1].startswith("obsidian://open?path=") and "00%20Overview.md" in opened[-1]

    st = c.delete("/api/key").json()
    assert not st["key_set"] and "ANTHROPIC_API_KEY=\n" in env.read_text()
    page = c.get("/").text
    assert 'PAGE_VERSION = "' + st["version"] + '"' in page        # stale-server detection is wired up


def wait_for_update(c, name, after_run=0, timeout=10.0):
    import time
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/projects/{name}/activity").json()
        if j["run"] > after_run and j["state"] in ("done", "error"):
            return j
        time.sleep(0.05)
    raise AssertionError(f"no update finished: {j}")


def test_auto_rebuild_batches_and_queues(tmp_path: Path, monkeypatch):
    """Changes close together -> one rebuild; a change during a rebuild -> exactly one more."""
    import threading
    import time
    from tether.autobuild import AutoBuilder

    runs, gate = [], threading.Event()

    def fake_run(name, work, log, stage):
        runs.append((name, work))
        if len(runs) == 1:
            gate.wait(5)
        log("built")
        return {"n": len(runs)}

    b = AutoBuilder(fake_run, debounce=0.15)
    b.request("P")
    b.request("P", urls=["https://x/1"])
    b.request("P", urls=["https://x/1", "https://x/2"])
    time.sleep(0.4)
    assert len(runs) == 1 and runs[0][1]["urls"] == ["https://x/1", "https://x/2"]   # batched, deduped
    assert b.status("P")["state"] == "running"
    b.request("P", topics=[("example topic", 5, None)])
    b.request("P")
    time.sleep(0.3)
    assert b.status("P")["queued_again"] and len(runs) == 1                          # waits its turn
    gate.set()
    time.sleep(0.5)
    assert len(runs) == 2 and runs[1][1]["topics"] == [("example topic", 5, None)]
    st = b.status("P")
    assert st["state"] == "done" and st["run"] == 2 and st["stats"] == {"n": 2}

    def boom(name, work, log, stage):
        raise RuntimeError("API down")
    b2 = AutoBuilder(boom, debounce=0.05)
    b2.request("Q")
    time.sleep(0.4)
    st = b2.status("Q")
    assert st["state"] == "error" and "API down" in st["error"] and st["run"] == 1     # no retry loop
    time.sleep(0.3)
    assert b2.status("Q")["run"] == 1


def test_launcher_dependency_check():
    from tether import _deps
    reqs = _deps._requirements()
    assert any(r.startswith("fastapi") for r in reqs) and any(r.startswith("pytest") for r in reqs)
    assert _deps.missing() == []                          # everything is installed here: no reinstall
    assert _deps._ver("0.110.2") > _deps._ver("0.110") and _deps._ver("1.9") < _deps._ver("1.10")


def test_adding_a_paper_only_reads_that_paper(tmp_path: Path):
    a, b = _two_papers(tmp_path)
    c = make_pdf(tmp_path / "c.pdf", "A Third Study of Weight Estimation", [[FILLER,
        "FINDING: A second {beta filter} variant follows weight changes during peak load.", FILLER]])
    proj = tmp_path / "P"
    run(FakeLLM(), out=proj, pdfs=[a, b], lookup=False)
    llm = FakeLLM()
    run(llm, out=proj, pdfs=[c], lookup=False)
    assert len(llm.payloads["claims"]) == 1 and "Third Study" in llm.payloads["claims"][0]   # only the new PDF
    assert llm.calls.count("synthesis") == 1                                                  # ideas re-woven once
    llm3 = FakeLLM()
    run(llm3, out=proj, lookup=False)
    assert llm3.calls == []                                                                   # nothing new: free


def test_upgrade_reuses_claims_verified_by_the_old_version(tmp_path: Path):
    """Projects built by tether 0.7 (one note per claim, ~57 claims a paper) are rebuilt by picking
    the central claims out of the already-verified ones. The PDFs are not read again."""
    import json
    from tether.ingest import read_pdf
    from tether.pipeline import LEGACY_VERSION, _key
    pdf = many_findings_pdf(tmp_path, n=9)
    proj = tmp_path / "P"
    (proj / "Sources").mkdir(parents=True)
    (proj / "Sources" / pdf.name).write_bytes(pdf.read_bytes())
    pages, _ = read_pdf(proj / "Sources" / pdf.name)
    old = [{"text": f"Old verified claim {i}", "kind": "finding", "quote": f"quote {i}", "page": 1,
            "qualifiers": "", "concepts": ["x"], "secondhand": False, "match_score": 100.0, "entailment": "supported"}
           for i in range(40)]
    legacy = proj / ".tether" / "claims" / f"{_key(LEGACY_VERSION, pages)}.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"kept": old, "rejected": []}))

    llm = FakeLLM()
    logs = []
    from tether.pipeline import Reporter
    stats = run(llm, out=proj, lookup=False, reporter=Reporter(logs.append))
    assert "claims" not in llm.calls and "verdicts" not in llm.calls                 # no re-reading, no re-checking
    assert llm.calls.count("selection") == 1
    assert stats["claims"] == 12 and stats["trimmed"] == 28                         # 40 -> the 12 most central
    assert any("reused earlier claims" in l for l in logs)
    assert any("Estimated cost" in l for l in logs)


def test_cost_estimate_reflects_what_is_new(tmp_path: Path, monkeypatch):
    from tether import config
    from tether.llm import ClaudeLLM
    from tether.pipeline import estimate, gather_papers, Reporter
    monkeypatch.setattr(config, "_SHELL_ENV", {"ANTHROPIC_API_KEY": "test"})
    a, b = _two_papers(tmp_path)
    proj = tmp_path / "P"
    run(FakeLLM(), out=proj, pdfs=[a], lookup=False)
    (proj / "Sources" / "b.pdf").write_bytes(b.read_bytes())
    papers = gather_papers(proj, False, Reporter(lambda m: None))
    bal = ClaudeLLM(model="claude-sonnet-5-5", judge_model="claude-haiku-5-5", profile_name="balanced")
    cheap = ClaudeLLM(model="claude-sonnet-5-5", judge_model="claude-haiku-5-5", profile_name="budget")
    e = estimate(bal, papers, proj / ".tether")
    new_one = next(p for p in papers if p.path and p.path.endswith("b.pdf"))
    assert e["new_papers"] == 1 and 0 < e["cost"] < 0.5 and e["titles"] == [new_one.citation.short()]
    assert estimate(cheap, papers, proj / ".tether")["cost"] < e["cost"] / 5
    assert estimate(bal, papers, proj / ".tether", fresh=True)["new_papers"] == 2


def test_old_projects_catch_up_on_open(tmp_path: Path, monkeypatch):
    """Papers added before auto-updates existed (or copied into Sources by hand) get processed
    when the project is opened, and an up-to-date project isn't rebuilt for nothing."""
    import os
    import time
    from fastapi.testclient import TestClient
    from tether import app as appmod, config
    import tether.llm as llmmod
    import tether.sources as src

    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(config, "_SHELL_ENV", {})

    class Fake(FakeLLM):
        model, judge_model, usage = "fake", "fake", {"input": 0, "output": 0}

        def __init__(self, **kw):
            super().__init__()
    monkeypatch.setattr(llmmod, "ClaudeLLM", Fake)
    monkeypatch.setattr(src, "lookup_by_title", lambda t: None)
    c = TestClient(appmod.app)
    c.post("/api/key", json={"key": "sk-ant-api03-" + "x" * 40})
    (tmp_path / "V").mkdir()
    c.post("/api/vault", json={"path": str(tmp_path / "V")})

    # an "old" project: PDFs sitting in Sources, never built (no request was ever made)
    a, b = _two_papers(tmp_path)
    proj = tmp_path / "V" / "Tether" / "Old Project"
    (proj / ".tether").mkdir(parents=True)
    (proj / "Sources").mkdir()
    for f in (a, b):
        (proj / "Sources" / f.name).write_bytes(f.read_bytes())

    j = wait_for_update(c, "Old Project")                 # just polling activity kicks it off
    assert j["state"] == "done" and j["stats"]["papers"] == 2

    time.sleep(0.2)                                       # up to date: polling doesn't rebuild again
    assert c.get("/api/projects/Old Project/activity").json()["run"] == 1

    c3 = make_pdf(tmp_path / "c.pdf", "Copied In By Hand", [[FILLER, "FINDING: A {beta filter} note copied in via Explorer still gets read properly.", FILLER]])
    dest = proj / "Sources" / "c.pdf"
    dest.write_bytes(c3.read_bytes())
    later = time.time() + 5
    os.utime(dest, (later, later))
    j = wait_for_update(c, "Old Project", after_run=1)
    assert j["stats"]["papers"] == 3

    os.utime(proj / ".tether" / "graph.json", (later + 5, later + 5))
    (proj / "Sources" / "b.pdf").unlink()                 # deleted by hand
    j = wait_for_update(c, "Old Project", after_run=2)
    assert j["stats"]["papers"] == 2


def test_prompt_caching_profiles_and_cost(monkeypatch):
    """System prompts carry a cache breakpoint, the stage roles route to the right model per
    profile, cache reads/writes are priced correctly, and the repeated prompts are long enough
    to be cacheable at all (512-token minimum on Sonnet/Haiku 5.5)."""
    import inspect
    from types import SimpleNamespace as NS
    from tether import config, llm as llmmod, stages
    from tether.llm import ClaudeLLM, describe_usage

    monkeypatch.setattr(config, "_SHELL_ENV", {"ANTHROPIC_API_KEY": "test"})
    monkeypatch.setattr(llmmod, "_mode_for", {})
    monkeypatch.setattr(llmmod, "LEDGER", __import__("pathlib").Path(__import__("tempfile").mkdtemp()) / "spend.json")
    c = ClaudeLLM(model="claude-sonnet-5-5", judge_model="claude-haiku-5-5", profile_name="balanced")
    real = inspect.signature(c.client.messages.create)
    sent = []

    def create(**kw):
        real.bind(**kw)
        sent.append(kw)
        return NS(stop_reason="end_turn", content=[NS(type="text", text='{"claims": []}')],
                  usage=NS(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0,
                           cache_read_input_tokens=600))
    monkeypatch.setattr(c.client.messages, "create", create)
    schema = {"type": "object", "properties": {"claims": {"type": "array"}}, "required": ["claims"]}

    c.call(system="S" * 10, user="chunk one", name="claims", schema=schema, role="read")
    sys_block = sent[-1]["system"]
    assert sys_block[-1]["cache_control"] == {"type": "ephemeral"} and sys_block[-1]["text"] == "S" * 10
    assert "cache_control" not in str(sent[-1]["messages"])                  # per-call content isn't cached
    assert sent[-1]["model"] == "claude-sonnet-5-5"
    # usage: 1000 uncached + 600 cache reads on Sonnet 5.5 = (1000*2 + 600*0.10 + 200*10) / 1e6
    assert abs(c.usage["cost"] - (1000 * 2 + 600 * 0.10 + 200 * 10) / 1e6) < 1e-12
    assert c.usage["input"] == 1600 and c.usage["cache_read"] == 600
    assert "from cache" in describe_usage(c.usage) and describe_usage(c.usage).startswith("≈$")

    roles = {"read": "claude-sonnet-5-5", "curate": "claude-sonnet-5-5", "check": "claude-haiku-5-5",
             "synthesize": "claude-sonnet-5-5"}
    assert {r: c.model_for(r) for r in roles} == roles
    cheap = ClaudeLLM(model="claude-sonnet-5-5", judge_model="claude-haiku-5-5", profile_name="budget")
    assert {cheap.model_for(r) for r in roles} == {"claude-haiku-5-5"}
    assert describe_usage({"input": 0, "output": 0}).startswith("no Claude calls")

    # the prompts that repeat many times per update must clear the cache minimum (generous 4.2 chars/token)
    for p in (stages.EXTRACT_SYSTEM, stages.ENTAIL_SYSTEM):
        assert len(p) / 4.2 > 512, len(p)


def test_stages_ask_for_the_right_roles(tmp_path: Path):
    llm = FakeLLM()
    run(llm, out=tmp_path / "P", pdfs=[many_findings_pdf(tmp_path, n=12)], lookup=False)
    assert dict(llm.roles) == {"claims": "read", "selection": "curate", "verdicts": "check",
                               "synthesis": "synthesize", "idea_checks": "check"}


def test_approval_and_monthly_cap(tmp_path: Path, monkeypatch):
    """An update estimated above the ask threshold waits for the user (nothing spent); "Not now"
    pauses it; approving runs it; a monthly cap blocks it outright, and the client refuses calls
    once the cap is reached."""
    import json
    import pytest
    from fastapi.testclient import TestClient
    from tether import app as appmod, config
    import tether.llm as llmmod
    import tether.sources as src
    import tether.pipeline as pl

    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(config, "_SHELL_ENV", {})
    monkeypatch.setattr(llmmod, "LEDGER", tmp_path / "spend.json")
    monkeypatch.setattr(appmod.builder, "debounce", 0.1)

    RealClient = llmmod.ClaudeLLM

    class Fake(FakeLLM):
        model, judge_model, usage = "fake", "fake", {"input": 0, "output": 0}

        def __init__(self, **kw):
            super().__init__()
    monkeypatch.setattr(llmmod, "ClaudeLLM", Fake)
    monkeypatch.setattr(src, "lookup_by_title", lambda t: None)
    monkeypatch.setattr(pl, "estimate", lambda llm, papers, cache, fresh=False, synth_cached=False:
                        {"cost": 1.80, "new_papers": len(papers), "reused_papers": 0, "titles": ["X"]})
    c = TestClient(appmod.app)
    c.post("/api/key", json={"key": "sk-ant-api03-" + "x" * 40})
    (tmp_path / "V").mkdir()
    c.post("/api/vault", json={"path": str(tmp_path / "V")})
    st = c.post("/api/spending", json={"ask_above": 0.5}).json()
    assert st["ask_above"] == 0.5 and st["monthly_cap"] == 0 and st["spent_month"] == 0

    c.post("/api/projects", json={"name": "Pricey"})
    a, b = _two_papers(tmp_path)
    with open(a, "rb") as fa:
        c.post("/api/projects/Pricey/upload", files=[("files", ("a.pdf", fa, "application/pdf"))])

    import time
    for _ in range(100):
        j = c.get("/api/projects/Pricey/activity").json()
        if j["state"] == "approval":
            break
        time.sleep(0.05)
    assert j["state"] == "approval" and j["estimate"]["cost"] == 1.80
    assert not (tmp_path / "V" / "Tether" / "Pricey" / ".tether" / "graph.json").exists()      # nothing ran

    c.post("/api/projects/Pricey/decline")
    time.sleep(0.3)
    assert c.get("/api/projects/Pricey/activity").json()["state"] == "paused"                 # and stays paused

    assert c.post("/api/projects/Pricey/approve").status_code == 409                          # nothing waiting now
    c.post("/api/projects/Pricey/rebuild")                                                     # "Update now" asks again
    for _ in range(100):
        if c.get("/api/projects/Pricey/activity").json()["state"] == "approval":
            break
        time.sleep(0.05)
    c.post("/api/projects/Pricey/approve")
    j = wait_for_update(c, "Pricey", after_run=2)
    assert j["state"] == "done" and j["stats"]["ideas"] >= 1

    # a cap the update would cross blocks it with a clear message
    llmmod.LEDGER.write_text(json.dumps({llmmod._month(): 9.50}))
    c.post("/api/spending", json={"monthly_cap": 10})
    with open(b, "rb") as fb:
        c.post("/api/projects/Pricey/upload", files=[("files", ("b.pdf", fb, "application/pdf"))])
    j = wait_for_update(c, "Pricey", after_run=j["run"])
    assert j["state"] == "error" and "monthly cap" in j["error"] and "$9.50 of $10.00" in j["error"]
    assert c.get("/api/state").json()["spent_month"] == 9.5

    # and the real client refuses to call Claude once the cap is reached, even mid-update
    monkeypatch.setattr(config, "_SHELL_ENV", {"ANTHROPIC_API_KEY": "test", "TETHER_MONTHLY_CAP": "9"})
    client = RealClient(model="m", judge_model="j")
    monkeypatch.setattr(client.client.messages, "create", lambda **kw: pytest.fail("called Claude past the cap"))
    with pytest.raises(llmmod.BudgetExceeded):
        client.call(system="s", user="u", name="x", schema={"type": "object"})
    assert c.post("/api/spending", json={"monthly_cap": -1}).status_code == 400


def test_cli_build_end_to_end(tmp_path: Path, monkeypatch, capsys):
    from tether import cli, config
    import tether.llm as llmmod
    import tether.sources as src
    monkeypatch.setattr(config, "_SHELL_ENV", {"ANTHROPIC_API_KEY": "test"})

    class Fake(FakeLLM):
        usage = {"input": 0, "output": 0}

        def __init__(self, **kw):
            super().__init__()
    monkeypatch.setattr(llmmod, "ClaudeLLM", Fake)
    monkeypatch.setattr(src, "lookup_by_title", lambda t: None)
    a, b = _two_papers(tmp_path)
    assert cli.main(["build", str(a), str(b), "--out", str(tmp_path / "out"), "--offline"]) == 0
    out = capsys.readouterr().out
    assert "Done: 2 central ideas from 2 papers" in out and "Claude usage" in out
