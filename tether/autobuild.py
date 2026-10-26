"""Keeps each project's graph in step with its sources.

Anything that changes a project (dropped PDFs, a Zotero import, links, a topic search, a
removed paper, a new research question) calls `request()`. Changes that arrive close
together are batched into one rebuild, one rebuild runs at a time, and changes that come in
mid-rebuild trigger exactly one more rebuild afterwards. Failures are shown, not retried
in a loop: the next change (or "Retry") starts a fresh attempt.

Expensive updates pause in an "approval" state (with a cost estimate) until the user says go;
"Not now" puts the project in "paused", which only a new change or "Update now" leaves.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

RunFn = Callable[[str, dict, Callable[[str], None], Callable[[str, float], None]], dict]


class AutoBuilder:
    def __init__(self, run_fn: RunFn, debounce: float = 2.0,
                 describe: Callable[[BaseException], tuple[str, str]] | None = None):
        self.run_fn = run_fn
        self.describe = describe or (lambda e: (str(e) if isinstance(e, UserFacing) else f"{type(e).__name__}: {e}",
                                                f"{type(e).__name__}: {e}"))
        self.debounce = debounce
        self._lock = threading.RLock()
        self._p: dict[str, dict] = {}
        self._running: str | None = None

    def _get(self, name: str) -> dict:
        return self._p.setdefault(name, {
            "state": "idle", "stage": "", "frac": 0.0, "log": [], "run": 0, "stats": None, "error": None,
            "updated": None, "dirty": False, "due": 0.0, "pending": self._empty(),
            "timer": None, "error_detail": None, "estimate": None, "now": {}, "adds": []})

    @staticmethod
    def _empty() -> dict:
        return {"urls": [], "topics": [], "full": False, "approved_up_to": 0.0}

    # ------------------------------------------------------------------ public
    def request(self, name: str, urls: list[str] = (), topics: list[tuple] = (), delay: float | None = None,
                full: bool = False, approved_up_to: float = 0.0) -> None:
        with self._lock:
            p = self._get(name)
            p["pending"]["full"] = p["pending"]["full"] or full
            p["pending"]["approved_up_to"] = max(p["pending"]["approved_up_to"], approved_up_to)
            p["pending"]["urls"] += [u for u in urls if u not in p["pending"]["urls"]]
            p["pending"]["topics"] += [t for t in topics if t not in p["pending"]["topics"]]
            p["dirty"] = True
            p["due"] = time.time() + (self.debounce if delay is None else delay)
            if p["state"] in ("idle", "done", "error", "approval", "paused"):
                p["state"] = "waiting"
            if p["timer"]:
                p["timer"].cancel()
            p["timer"] = threading.Timer(max(0.0, p["due"] - time.time()), self._kick)
            p["timer"].daemon = True
            p["timer"].start()

    def busy(self) -> bool:
        return self._running is not None

    def approve(self, name: str) -> dict | None:
        """Run the update that's waiting for approval (allowing up to 25% over the estimate)."""
        with self._lock:
            p = self._get(name)
            est = p["estimate"]
            full = p.get("approval_full", False)
        if p["state"] != "approval" or not est:
            return None
        self.request(name, delay=0, approved_up_to=est["cost"] * 1.25 + 0.01, full=full)
        return est

    def decline(self, name: str) -> None:
        with self._lock:
            p = self._get(name)
            if p["state"] == "approval":
                p.update(state="paused", stage="", frac=0.0)

    def status(self, name: str, since: int = 0, run: int = -1) -> dict:
        with self._lock:
            p = self._get(name)
            log = p["log"][since:] if run == p["run"] else p["log"]
            return {"state": p["state"], "stage": p["stage"], "frac": p["frac"], "run": p["run"],
                    "log": list(log), "log_len": len(p["log"]), "stats": p["stats"], "error": p["error"],
                    "error_detail": p["error_detail"], "full_pending": p["pending"]["full"],
                    "estimate": p["estimate"] if p["state"] == "approval" else None,
                    "updated": p["updated"], "queued_again": p["dirty"] and p["state"] == "running",
                    "now": dict(p["now"]) if p["state"] == "running" else {},
                    "adds": [dict(a) for a in p["adds"]]}

    # ------------------------------------------------------------------ live detail
    def note_now(self, name: str, what: dict) -> None:
        """Merge in what the running update is doing right now (shown in Details)."""
        with self._lock:
            self._get(name)["now"].update(what)

    def track_adds(self, name: str, items: list[dict]) -> None:
        """Papers the user just added, shown as a checklist until dismissed. A new batch replaces a
        finished one; while one is still in progress, new items join it."""
        with self._lock:
            p = self._get(name)
            if p["adds"] and all(a["state"] not in ("queued", "fetching") for a in p["adds"]):
                p["adds"] = []
            have = {a["key"] for a in p["adds"]}
            p["adds"] += [dict(i) for i in items if i["key"] not in have]

    def update_add(self, name: str, key: str, **fields) -> None:
        with self._lock:
            for a in self._get(name)["adds"]:
                if a["key"] == key:
                    a.update(fields)

    def clear_adds(self, name: str) -> None:
        with self._lock:
            self._get(name)["adds"] = []

    def forget(self, name: str) -> None:
        with self._lock:
            p = self._p.pop(name, None)
            if p and p["timer"]:
                p["timer"].cancel()

    # ------------------------------------------------------------------ internals
    def _kick(self) -> None:
        with self._lock:
            if self._running is not None:
                return                                         # picked up when the current run ends
            now = time.time()
            ready = [(p["due"], n) for n, p in self._p.items() if p["dirty"] and p["due"] <= now + 0.01]
            if not ready:
                return
            _, name = min(ready)
            p = self._p[name]
            work = p["pending"]
            p.update(dirty=False, pending=self._empty(), state="running", estimate=None, now={},
                     stage="Starting", frac=0.0, log=[], error=None, error_detail=None, run=p["run"] + 1)
            self._running = name
        threading.Thread(target=self._work, args=(name, work), daemon=True).start()

    def _work(self, name: str, work: dict) -> None:
        p = self._p[name]

        def log(m: str) -> None:
            with self._lock:
                p["log"].append(m)

        def stage(s: str, f: float) -> None:
            with self._lock:
                p["stage"], p["frac"] = s, f

        try:
            stats = self.run_fn(name, work, log, stage)
            with self._lock:
                p.update(state="done", stats=stats, updated=time.time(), stage="Up to date", frac=1.0)
        except NeedsApproval as e:                             # nothing was spent; wait for the user
            with self._lock:
                p.update(state="approval", estimate=e.estimate, stage="", frac=0.0, approval_full=work.get("full", False))
                p["log"].append(f"Waiting for your OK: this update would cost about ${e.estimate['cost']:.2f}.")
        except Exception as e:                                 # shown in the app; no retry loop
            short, detail = self.describe(e) if not isinstance(e, UserFacing) else (str(e), str(e))
            with self._lock:
                p.update(state="error", error=short, error_detail=detail)
                p["log"].append(f"✗ {short}")
                if detail != short:
                    p["log"].append(f"  details: {detail}")
        finally:
            with self._lock:
                self._running = None
                if p["dirty"]:                                 # changes arrived while we were busy
                    p["state"] = "waiting"
            self._kick()
            with self._lock:                                   # something still waiting on its timer?
                for q in self._p.values():
                    if q["dirty"] and not (q["timer"] and q["timer"].is_alive()):
                        q["timer"] = threading.Timer(max(0.0, q["due"] - time.time()), self._kick)
                        q["timer"].daemon = True
                        q["timer"].start()


class UserFacing(RuntimeError):
    """An error whose message is meant for the user as-is (no exception class prefix)."""


try:
    from .pipeline import NeedsApproval
except ImportError:                                            # pipeline imports are heavy; tests may stub
    class NeedsApproval(Exception):                            # type: ignore[no-redef]
        estimate: dict
