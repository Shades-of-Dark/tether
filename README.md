# tether

Turn a pile of research papers into the backbone of a literature review: **8–20 central ideas**, each woven from several papers and **tethered to verbatim quotes and page numbers**, linked in an Obsidian graph that shows where the papers **agree, disagree, or build on each other**.

## Quick start (Windows)

1. Install [Python 3.10+](https://www.python.org/downloads/) (tick **Add python.exe to PATH**).
2. Double-click **`Tether.bat`**. The first run sets things up (about a minute), then the app opens in your browser.
3. The app walks you through three things, once:
   - **API key**: paste it into the app. It's saved to `.env` on your computer (git-ignored) and only ever shown back masked.
   - **Obsidian vault**: pick your existing vault, or let the app create one. See *Vaults* below.
   - **Obsidian**: download it if you don't have it.
4. Create a project and add papers: drag in PDFs, import from Zotero, paste links, or search a topic. There's no build button: the graph updates itself whenever the project's papers or research question change.
5. Click **Open in Obsidian** and start at `00 Overview`.

Changes made close together are batched into one update, and a change that arrives mid-update queues exactly one more. Updates are incremental: papers already read, and comparisons between papers already in the project, are reused, so adding a paper only costs that paper's reading plus its comparisons with the rest. An update with nothing new makes no calls to Claude. If an update fails, the app explains why in plain words (the raw error is under *Details*) and offers *Retry*; it never retries in a loop on its own.

Two buttons let you force it: **Update now** processes anything new and reuses all earlier work (quick, cheap). **Rebuild from scratch** re-reads every paper and redoes every comparison (costs about as much as the first build; use it if the notes look wrong).

## Vaults: one or many?

An Obsidian vault is just a folder Obsidian treats as one notebook. Links, search and the graph only work *inside* a vault, so the usual setup is **one vault for everything**, with folders inside it. tether keeps all its projects in a single `Tether` folder in that vault and never touches your other notes:

```
Your Vault/
├── your own notes…
└── Tether/
    ├── <project one>/     Ideas/ Papers/ Sources/ 00 Overview, Lit Review Outline …
    └── <project two>/
```

## Zotero (optional)

In Settings → Zotero, choose how much access tether gets. You can change it any time:

| Level | What tether can do |
|---|---|
| **Off** | Nothing. tether can't see your library. |
| **Read** | List your collections and items, import the papers you pick (PDF + Zotero's citation), and use Zotero's citation for any PDF you drop in that's already in your library. It reads a *copy* of `zotero.sqlite`, so it never changes or locks your library. No key needed. |
| **Read & write** | Plus only the boxes you tick: **add papers** (creates `Tether / <project>`, adds papers tether found that aren't in Zotero yet, files existing ones into it), **claim notes** (one note per paper with the verified claims; tether only updates notes it created), **upload PDFs** (off by default; uses your Zotero storage quota). Needs a [zotero.org API key](https://www.zotero.org/settings/keys/new) with library, write and notes access. |

tether never deletes anything in Zotero and never edits your items' details, notes or tags. Writes go through Zotero's official web API and reach the desktop app through normal Zotero sync. Sync runs after each update, and there's a *Sync to Zotero again* button.

## Models, cost and limits

Settings → *Models, cost & limits* picks which Claude model does which job:

| Job | Balanced (default) | Budget |
|---|---|---|
| Read papers, pull out claims + quotes | Sonnet | Haiku |
| Pick each paper's central claims | Sonnet | Haiku |
| Fact-check claims and ideas | Haiku | Haiku |
| Weave the ideas and outline | Sonnet | Haiku |

**What keeps the cost down**
- **Fewer, better claims:** at most 4 per section and ~10–12 per paper (older versions kept ~50+), and only those get fact-checked. Most of the cost is the text Claude *writes*, so this is the big saving.
- **Skipped sections:** related work, background, appendices, acknowledgements.
- **One synthesis call** weaves all papers together, instead of comparing every pair of claims.
- **Nothing is paid for twice:** papers already read, selections and syntheses are cached; an update with nothing new makes no calls to Claude.
- **Prompt caching** on the instructions that repeat on every call (5–10% of the normal price after the first call).

**Limits**
- **Ask first:** if an update is estimated above a threshold (default $0.50), the project shows "Ready to update: about $X" with *Go ahead* / *Not now*, before anything is spent.
- **Monthly cap:** optional hard limit. Updates that would cross it are refused with a clear message, and the client stops calling Claude the moment the month's spend reaches it, even mid-update.
- Spend is tracked per month in `.tether_spend.json` (git-ignored). Each update's real cost and cache hit rate appear in the status card and log.

## Settings (`.env`)

The app manages this for you. `.env` is in `.gitignore`; `.env.example` shows the fields.

| Variable | What it is |
|---|---|
| `ANTHROPIC_API_KEY` | your key |
| `TETHER_VAULT` | your Obsidian vault folder |
| `TETHER_ZOTERO` | your Zotero data folder (empty = Zotero off) |
| `TETHER_ZOTERO_WRITE` / `ZOTERO_API_KEY` | write permissions you granted, and the zotero.org key (empty = read only) |
| `TETHER_PROFILE` | `balanced` or `budget` (the app sets this) |
| `TETHER_ASK_ABOVE` / `TETHER_MONTHLY_CAP` | ask-first threshold and monthly cap, in dollars (the app sets these) |
| `TETHER_MODEL` / `TETHER_JUDGE_MODEL` | the stronger / cheaper model (defaults: Sonnet 5.5 / Haiku 5.5) |

The app only listens on `127.0.0.1` and rejects requests from other websites, so a page you visit can't read your projects or change your key.

## Command line

```bash
.venv\Scripts\activate
tether app                                                   # same as Tether.bat
tether build papers\ --project "My Topic" --question "What am I trying to answer?"
tether build --topic "your search terms" -n 12 --since 2021 --project "My Topic"
tether build my.pdf https://arxiv.org/abs/XXXX.XXXXX --project "My Topic"
pytest                                                       # offline tests, no API calls
```

## What you get in the vault

```
<vault>/Tether/<project>/
  00 Overview.md            the ideas at a glance, and which papers feed (or contradict) each one
  Lit Review Outline.md     sections -> ideas -> compact citations, then open questions and gaps
  Ideas/    I-03 Grasp location and stability.md
                            the idea, its supporting / qualifying / contradicting evidence with
                            verbatim quotes and "PDF p. N" links, and how it connects to other ideas
  Papers/   P01 Smith et al., 2024.md     citation, the ~10 key claims, the ideas it feeds
  Sources/  *.pdf                         your PDFs (evidence links jump to the exact page)
  References.md, references.bib           bibliography (Zotero-importable)
  Audit.md                                everything filtered out, and why
```

In Obsidian's graph, ideas (teal) link to the papers (orange) they draw on and to each other. Rebuilding only replaces notes tether generated; your own notes in those folders are left alone. Projects built by older versions (one note per claim, in `Claims/` and `Concepts/`) are converted automatically when opened: tether picks the central claims out of the ones it already verified, so the PDFs aren't read or paid for again, and removes its old notes.

## How it avoids making things up

LLMs hallucinate when asked to *recall* or *write freely*. tether never lets them do either without a check the model can't fake:

| Step | What the model does | What checks it |
|---|---|---|
| Read | proposes at most 4 central claims per section of the paper, each with a quote + page | **Code**, not AI: the quote must be found in the PDF's text (exact, or ≥90% fuzzy with *identical numbers and negations*). Not found → rejected. The quote shown is cut from the PDF itself. Related work, background and appendices are skipped. |
| Attribution | flags claims the paper only cites from others | Secondhand claims are dropped, so "Smith et al. showed X" is never credited to the paper that mentioned Smith. |
| Select | picks each paper's ~10–12 most central claims | Can only return claims that passed the quote check; invented ids are ignored. |
| Fact-check claims | none | A **separate** model call sees only the real source passage: supported / partial / unsupported. Partial → the missing condition is added. Unsupported or no verdict → rejected (fails closed). |
| Synthesize ideas | weaves claims into 8–20 ideas, links them, outlines the review | Every idea must cite real, verified claims or it's **deleted**; links and outline entries pointing at nothing are dropped. |
| Fact-check ideas | none | A separate call checks each idea's statement against its own evidence and quotes. Overreaching ideas are narrowed with a scope note; unsupported ones are removed (listed in Audit). |
| Citations | **never** | Author / year / venue / DOI come from Zotero or OpenAlex, matched by title. Unmatched → marked *unverified*. Same author + year → 2025a / 2025b. |

What it can't guarantee: that the claims it picked are the ones *you* consider most important, or that a "contradicts" stance isn't comparing different conditions. Those are judgment calls, so the quote and the PDF page are always one click away. **Use the outline as a scaffold and write the review yourself.**

## Notes

- Scanned PDFs (no text layer) are skipped; OCR them first (e.g. `ocrmypdf`).
- Paywalled papers from links or topic search fall back to the abstract and are tagged `abstract-only`.
- Unchanged papers cost nothing on later updates. The status card shows each update's estimate beforehand and its real cost afterwards.
