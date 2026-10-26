"""Claude client that returns schema-shaped JSON, whatever the model supports.

Modes, tried in order and remembered per model once one works:
    1. structured   output_config.format = json_schema (the response is guaranteed to match)
    2. tool         one tool, tool_choice "auto", told to call it (for models without 1)
    3. text         JSON in the reply text, parsed and checked (last resort)
A 400 that says a mode isn't supported moves to the next mode; anything else surfaces.
"""
from __future__ import annotations

import copy
import datetime
import json
import re
import threading
import time
from typing import Any, Callable, Optional, Protocol

from . import config

DEFAULT_MODEL = config.get("TETHER_MODEL", "claude-sonnet-5-5")       # the stronger model
JUDGE_MODEL = config.get("TETHER_JUDGE_MODEL", "claude-haiku-5-5")    # the cheap, fast model
MODES = ("structured", "tool", "text")

# Which model does which job. "main" = TETHER_MODEL, "small" = TETHER_JUDGE_MODEL.
#   read        pulling claims + quotes out of each chunk of a paper (most of the tokens)
#   curate      picking each paper's ~12 most central claims (small input: the claim list)
#   check       fact-checking claims against their source passages, and ideas against their evidence
#   synthesize  weaving the claims into 8-20 central ideas, their links, and the outline
PROFILES = {
    "balanced": {"read": "main", "curate": "main", "check": "small", "synthesize": "main"},
    "budget": {"read": "small", "curate": "small", "check": "small", "synthesize": "small"},
}
PROFILE_INFO = {
    "balanced": "Sonnet reads papers, picks the key claims and weaves the ideas; Haiku fact-checks.",
    "budget": "Haiku does everything. About 10-20x cheaper; somewhat less thorough. The quote check still blocks made-up quotes.",
}

# USD per million tokens: input, output, 5-min cache write, cache read. Unknown models: no estimate.
PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
}


# ---------------------------------------------------------------- spending ledger + monthly cap
LEDGER = config.ROOT / ".tether_spend.json"          # {"2026-10": 2.31, ...}; git-ignored
_ledger_lock = threading.Lock()


class BudgetExceeded(RuntimeError):
    pass


def _month() -> str:
    return datetime.date.today().strftime("%Y-%m")


def month_spent() -> float:
    try:
        return float(json.loads(LEDGER.read_text(encoding="utf-8")).get(_month(), 0.0))
    except (OSError, ValueError):
        return 0.0


def _add_spend(amount: float) -> None:
    if amount <= 0:
        return
    with _ledger_lock:
        try:
            d = json.loads(LEDGER.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        d[_month()] = round(float(d.get(_month(), 0.0)) + amount, 6)
        LEDGER.write_text(json.dumps(d, indent=1), encoding="utf-8")


def monthly_cap() -> float:
    try:
        return max(0.0, float(config.get("TETHER_MONTHLY_CAP") or 0))
    except ValueError:
        return 0.0


def ask_above() -> float:
    try:
        return max(0.0, float(config.get("TETHER_ASK_ABOVE") or 0.5))
    except ValueError:
        return 0.5


def profile() -> str:
    p = (config.get("TETHER_PROFILE") or "balanced").lower()
    return p if p in PROFILES else "balanced"

# Only these are worth waiting and retrying. Anything else fails immediately with its real message.
RETRYABLE = {"RateLimitError", "OverloadedError", "InternalServerError", "ServiceUnavailableError",
             "APIConnectionError", "APITimeoutError", "DeadlineExceededError", "RetryableError"}
_UNSUPPORTED = re.compile(r"not supported|unsupported|does not support|unknown (field|parameter)|"
                          r"extra inputs are not permitted|output_config|output_format|tool_choice|json_schema", re.I)

_mode_for: dict[str, str] = {}             # model -> the mode that worked (shared by all clients)


class MissingKey(RuntimeError):
    pass


class BadOutput(RuntimeError):
    pass


class CutOff(BadOutput):
    """Claude hit the output limit. Retrying the same request would just pay for the same cut-off."""

    def __init__(self, name: str, limit: int):
        super().__init__(f"Claude's {name!r} answer didn't fit in {limit:,} tokens.")
        self.name, self.limit = name, limit


MAX_OUTPUT = 16000          # SDK requires streaming above ~21k; nothing in tether should need more


class LLM(Protocol):
    def call(self, *, system: str, user: str, name: str, schema: dict, judge: bool = False,
             role: Optional[str] = None, max_tokens: Optional[int] = None) -> dict: ...


def strict(schema: dict) -> dict:
    """Structured outputs want every object closed and every property required."""
    s = copy.deepcopy(schema)

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "object" and "properties" in n:
                n.setdefault("additionalProperties", False)
                n["required"] = list(n["properties"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(s)
    return s


def parse_json(text: str) -> Any:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def _shape_ok(out: Any, schema: dict) -> bool:
    if not isinstance(out, dict):
        return False
    return all(k in out for k in schema.get("required", []))


class ClaudeLLM:
    def __init__(self, model: str = DEFAULT_MODEL, judge_model: str = JUDGE_MODEL, max_tokens: int = 8000,
                 on_retry: Optional[Callable[[str], None]] = None, profile_name: Optional[str] = None):
        import anthropic

        key = config.api_key()
        if not key:
            raise MissingKey("No Anthropic API key yet. Add it in Settings.")
        self.client = anthropic.Anthropic(api_key=key, max_retries=0)      # we retry ourselves, visibly
        self.on_retry = on_retry or print
        self.model, self.judge_model, self.max_tokens = model, judge_model, max_tokens
        self.profile = profile_name if profile_name in PROFILES else profile()
        self.usage = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0, "cost": 0.0, "priced": True}

    def model_for(self, role: str) -> str:
        return self.model if PROFILES[self.profile].get(role, "main") == "main" else self.judge_model

    def _account(self, model: str, u) -> None:
        inp = getattr(u, "input_tokens", 0) or 0
        out = getattr(u, "output_tokens", 0) or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        self.usage["input"] += inp + cw + cr
        self.usage["output"] += out
        self.usage["cache_write"] += cw
        self.usage["cache_read"] += cr
        price = PRICES.get(model)
        if price is None:
            self.usage["priced"] = False
        else:
            p_in, p_out, p_w, p_r = price
            cost = (inp * p_in + out * p_out + cw * p_w + cr * p_r) / 1e6
            with _ledger_lock:
                self.usage["cost"] += cost
            _add_spend(cost)

    # ------------------------------------------------------------ one request
    def _request(self, mode: str, model: str, system: str, user: str, name: str, schema: dict, max_tokens: int):
        kw = dict(model=model, max_tokens=max_tokens, messages=[{"role": "user", "content": user}])
        if mode == "structured":
            kw["output_config"] = {"format": {"type": "json_schema", "schema": strict(schema)}}
        elif mode == "tool":
            kw["tools"] = [{"name": name, "description": f"Submit the {name} result.", "input_schema": schema}]
            kw["tool_choice"] = {"type": "auto"}
            system = system + f"\n\nSubmit your answer by calling the `{name}` tool exactly once. Do not reply in text."
        else:
            system = (system + "\n\nReply with ONLY a JSON object (no prose, no code fences) matching this "
                      f"JSON schema:\n{json.dumps(schema)}")
        # Prompt caching: the instructions (and tool, if any) are identical for every call of a stage,
        # so mark the end of the system prompt as a cache breakpoint. The first call writes the cache
        # (1.25x); later calls within 5 minutes read it (5-10% of the normal input price). The
        # per-call content (the paper chunk, the claims) comes after the breakpoint and isn't cached.
        kw["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        return self.client.messages.create(**kw)

    def _extract(self, mode: str, resp, name: str, schema: dict, limit: int) -> dict:
        if resp.stop_reason == "max_tokens":
            raise CutOff(name, limit)
        if mode == "tool":
            for b in resp.content:
                if getattr(b, "type", "") == "tool_use":
                    return b.input
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        try:
            out = parse_json(text)
        except (json.JSONDecodeError, ValueError):
            raise BadOutput(f"Claude didn't return valid JSON for {name!r}.")
        if not _shape_ok(out, schema):
            raise BadOutput(f"Claude's {name!r} answer was missing fields.")
        return out

    # ------------------------------------------------------------ public
    def call(self, *, system: str, user: str, name: str, schema: dict, judge: bool = False,
             role: Optional[str] = None, max_tokens: Optional[int] = None) -> dict:
        model = self.model_for(role or ("check" if judge else "read"))
        budget = min(max_tokens or self.max_tokens, MAX_OUTPUT)
        cap = monthly_cap()
        if cap and month_spent() >= cap:          # hard stop, even mid-update
            raise BudgetExceeded(f"Monthly spending cap reached (${month_spent():.2f} of ${cap:.2f}). "
                                 "Raise it in Settings → Models & cost, or wait until next month.")
        modes = [_mode_for[model]] if model in _mode_for else list(MODES)
        last: Optional[Exception] = None
        for mode in modes:
            for attempt in range(6):
                try:
                    resp = self._request(mode, model, system, user, name, schema, budget)
                except Exception as e:
                    kind = type(e).__name__
                    if kind == "BadRequestError" and _UNSUPPORTED.search(str(e)) and mode != MODES[-1] \
                            and model not in _mode_for:
                        last = e
                        break                                   # this model can't do this mode: next mode
                    if kind not in RETRYABLE or attempt == 5:
                        raise
                    wait = min(60, 3 * 2 ** attempt)
                    self.on_retry(f"  ! Claude is busy or unreachable ({kind}); retrying in {wait}s")
                    time.sleep(wait)
                    continue
                self._account(model, resp.usage)
                try:
                    out = self._extract(mode, resp, name, schema, budget)
                except CutOff as e:
                    if budget < MAX_OUTPUT:                     # give it more room once; never repeat the same request
                        budget = min(budget * 2, MAX_OUTPUT)
                        self.on_retry(f"  ! {name}: answer was longer than expected; retrying with room for {budget:,} tokens")
                        last = e
                        continue
                    raise
                except BadOutput as e:
                    if attempt < 1:                             # one fresh try for a garbled answer
                        last = e
                        continue
                    raise
                _mode_for[model] = mode
                return out
        raise last or RuntimeError("No way to get structured output from this model.")


def friendly_error(e: BaseException) -> tuple[str, str]:
    """(short message for the status line, full detail for the log)."""
    kind = type(e).__name__
    detail = f"{kind}: {e}"
    msg = None
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        msg = (body.get("error") or {}).get("message")
    if "credit balance" in str(e).lower():
        return "Your Anthropic account is out of credits. Add some at console.anthropic.com.", detail
    short = {
        "MissingKey": "Add your Anthropic API key in Settings.",
        "AuthenticationError": "Anthropic rejected your API key. Replace it in Settings.",
        "PermissionDeniedError": "Your API key doesn't have access to this model.",
        "NotFoundError": f"Model not found. Check TETHER_MODEL in .env{f' ({msg})' if msg else ''}.",
        "RateLimitError": "Anthropic's rate limit was hit. Wait a minute, then retry.",
        "OverloadedError": "Claude is overloaded right now. Retry in a minute.",
        "APIConnectionError": "Couldn't reach Anthropic. Check your internet connection.",
        "APITimeoutError": "Anthropic took too long to respond. Retry.",
        "BadOutput": str(e),
        "CutOff": f"{e} This is a tether bug, not your papers: please report it. Nothing after this step ran, "
                  "and work already done is saved.",
        "BudgetExceeded": str(e),
    }.get(kind)
    if short is None and kind.endswith("Error") and msg:
        short = f"Claude refused the request: {msg}"
    return short or str(e) or kind, detail


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1)


def describe_usage(u: dict) -> str:
    """'≈$0.04 · 182k tokens in (61% from cache), 9k out' for the log and the app."""
    if not u.get("input"):
        return "no Claude calls (everything was already done)"
    k = lambda n: f"{n / 1000:.0f}k" if n >= 1000 else str(n)
    cached = u.get("cache_read", 0) / u["input"] if u["input"] else 0
    cost = f"≈${u['cost']:.2f} · " if u.get("priced", True) else ""
    return f"{cost}{k(u['input'])} tokens in ({cached:.0%} from cache), {k(u['output'])} out"
