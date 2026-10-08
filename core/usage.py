"""What every paid call cost, attributed to the article and the action it was made for.

Every call to a paid service reports here (`record`) in that service's own units: tokens for the
models, images for gpt-image, runs and requests for Parallel, credits for Scrapfly and Winston,
API units for Ahrefs. The event is appended at once to the article's `_usage.jsonl`, so what was
paid for survives a crash halfway through a run, and it carries:

  - `op`: the action it belongs to (generation, Apply QA fixes, Fix, an AI edit in the editor,
    a picture redrawn from the Studio...), with an `op_id` per run of that action;
  - `stage`: the part of the article it paid for (keyword research, research, text, visuals,
    review, edits...), set by the orchestrator and the flows (`stage()`);
  - `cost_usd`: priced with the table in core/pricing.py at the moment of the call, or None when
    that service has no price in the table yet (then it is priced when shown).

Attribution rides on context variables: `recording(article_dir, op)` opens a sink for the
current thread (a background job runs one article at a time on its own thread), and LangGraph
copies the context into the nodes it runs. A call made outside any recording is still counted
in `PROCESS` (the CLI prints it), but belongs to no article.
"""
from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import pricing

USAGE_FILE = "_usage.jsonl"

# the parts of an article a person asks the price of, in reading order
CATEGORIES = [
    ("keywords", "Keyword research"),
    ("brief", "Brief"),
    ("research", "Research"),
    ("text", "Text"),
    ("charts", "Charts & infographics"),
    ("images", "Images"),
    ("screenshots", "Screenshots"),
    ("review", "QA review"),
    ("edits", "Edits & checks"),
    ("uniqueness", "Uniqueness"),
    ("studio", "Studio edits"),
    ("other", "Other"),
]
CATEGORY_LABEL = dict(CATEGORIES)
# the same parts, as column headers
CATEGORY_SHORT = {"keywords": "Keywords", "brief": "Brief", "research": "Research", "text": "Text",
                  "charts": "Charts", "images": "Images", "screenshots": "Shots", "review": "Review",
                  "edits": "Edits", "uniqueness": "Unique", "studio": "Studio", "other": "Other"}

OPERATIONS = {
    "generation": "Generation",
    # the old checklist's actions (gone in phase 5), named for the costs they left
    "apply_qa_fixes": "Apply QA fixes",
    "catchup": "Catch-up pass",
    "fix": "A change tried again",
    "review": "Review",
    "proposals": "Proposals applied",
    "revert": "Change reverted",
    "not-important": "Marked not important",
    "send": "Sent to WordPress",
    "accept": "Accepted",
    "check-post": "WordPress post read",
    "ai_edit": "AI edit",
    "visual_insert": "New visual",
    "visual_edit": "Visual redrawn",
    "uniqueness": "Uniqueness check",
    "estimate": "Before cost tracking (estimate)",
}

PROVIDER_LABEL = {"anthropic": "Anthropic", "openai": "OpenAI", "parallel": "Parallel",
                  "ahrefs": "Ahrefs", "scrapfly": "Scrapfly", "winston": "Winston AI"}


@dataclass
class Recording:
    article_dir: Path | None
    op: str
    op_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    label: str = ""
    # the action this one runs inside on the same article (the pipeline's first review runs
    # inside its generation): its op_id, so the cost of "a run to its first review" is known
    within: str | None = None
    started: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    prices: dict = field(default_factory=dict)
    events: list = field(default_factory=list)


_sink: ContextVar[Recording | None] = ContextVar("usage_sink", default=None)
_stage: ContextVar[str | None] = ContextVar("usage_stage", default=None)
_lock = threading.Lock()
# every event of this process, attributed or not: the CLI's end-of-command total
PROCESS: list[dict] = []


@contextmanager
def recording(article_dir, op: str, label: str = "", config: dict | None = None):
    """Attribute every paid call made inside the block to `article_dir`, as action `op`."""
    d = Path(article_dir) if article_dir is not None else None
    outer = _sink.get()
    rec = Recording(article_dir=d, op=op, label=label,
                    within=outer.op_id if outer and d is not None and outer.article_dir == d else None)
    try:
        from .config import load_core_config
        from .store import output_root

        cfg = config or load_core_config()
        rec.prices = pricing.table(cfg, output_root(cfg))
    except Exception:  # noqa: BLE001 — a price table problem must never stop the work
        rec.prices = pricing.DEFAULTS
    if d is not None:
        _freeze_legacy_baseline(d)
    token = _sink.set(rec)
    try:
        yield rec
    finally:
        _sink.reset(token)


def _has_baseline(evs: list[dict]) -> bool:
    """A ledger that accounts for the article's start: its generation was recorded, or the
    estimate of its earlier costs was frozen into it."""
    return any(e.get("op") in ("generation", "estimate") for e in evs)


def _estimate_before(d: Path, evs: list[dict]) -> tuple[list[dict], list[str]]:
    """The estimate of an article's costs from before its ledger, given the ledger it already
    has. The estimate reads the uniqueness report and the cover's background; when an action in
    the ledger rewrote one of them, that file now describes the recorded action, and the earlier
    check or cover it replaced can no longer be known: it is left out, and the summary says so
    (review RC-04)."""
    base = legacy_estimate(d)
    limits = []
    if any(e.get("provider") == "winston" for e in evs):
        base = [e for e in base if e["provider"] != "winston"]
        limits.append("an earlier uniqueness check was replaced by a recorded one; its cost is unknown")
    if any(e.get("op") == "cover" for e in evs):
        base = [e for e in base if not (e["provider"] == "openai" and e["stage"] == "visuals")]
        limits.append("an earlier cover was replaced by a recorded one; its cost is unknown")
    return [{**e, "op": "estimate"} for e in base], limits


def _freeze_legacy_baseline(d: Path) -> None:
    """Before the first recorded action in an article generated before the ledger, write the
    estimate of its earlier costs into the ledger, once. The estimate reads files a new action
    rewrites (the uniqueness report, the cover's background), so taken later it lost the old
    check and the old cover (review RR-05); frozen first, every later action adds to it. A ledger
    written before this existed (later actions only) gets its estimate now, with what can no
    longer be known left out and said (RC-04)."""
    f = d / USAGE_FILE
    if not (d / "_run.json").exists():
        return
    try:
        evs = events(d) if f.exists() else []
        if _has_baseline(evs):
            return
        base, limits = _estimate_before(d, evs)
        from .store import append_jsonl

        for ev in base:
            append_jsonl(f, {**ev, "note": "costs from before the ledger, estimated"})
        if limits:
            # what the estimate could not know stays on record with it, not only in a live summary
            append_jsonl(f, {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             "op": "estimate", "op_id": "estimate", "provider": "note", "item": "limits",
                             "units": {}, "cost_usd": 0.0, "limit": "; ".join(limits)})
    except Exception:  # noqa: BLE001 — accounting never stops the work
        pass


@contextmanager
def stage(name: str):
    token = _stage.set(name)
    try:
        yield
    finally:
        _stage.reset(token)


def current() -> Recording | None:
    return _sink.get()


def record(provider: str, item: str, units: dict, agent: str | None = None,
           stage_name: str | None = None, note: str | None = None) -> dict:
    """One paid call. Never raises: accounting must not fail the work it accounts for."""
    rec = _sink.get()
    ev = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
          "op": rec.op if rec else None, "op_id": rec.op_id if rec else None,
          "stage": stage_name or _stage.get(), "agent": agent,
          "provider": provider, "item": item,
          "units": {k: v for k, v in (units or {}).items() if v not in (None, 0, "")}}
    if rec and rec.within:
        ev["within"] = rec.within
    try:
        cost = pricing.price(provider, item, units or {}, rec.prices if rec else pricing.DEFAULTS)
    except Exception:  # noqa: BLE001
        cost = None
    ev["cost_usd"] = round(cost, 6) if cost is not None else None
    ev["price_as_of"] = (rec.prices if rec else pricing.DEFAULTS).get("as_of")
    if note:
        ev["note"] = note
    with _lock:
        PROCESS.append(ev)
        del PROCESS[:-5000]
    if rec is not None:
        rec.events.append(ev)
        if rec.article_dir is not None:
            try:
                from .store import append_jsonl

                append_jsonl(rec.article_dir / USAGE_FILE, ev)
            except OSError:
                pass
    return ev


# ---------------------------------------------------------------------------------------------
# reading it back
# ---------------------------------------------------------------------------------------------
def category(ev: dict) -> str:
    """The part of the article an event paid for."""
    prov, st, item = ev.get("provider"), ev.get("stage"), str(ev.get("item") or "")
    if prov == "openai" and item.startswith("gpt-image"):
        return "images"
    if prov == "scrapfly" and st in ("visuals", "edits", "studio"):
        return "screenshots"
    return {"keywords": "keywords", "brief": "brief", "research": "research", "writing": "text",
            "visuals": "charts", "review": "review", "edits": "edits", "uniqueness": "uniqueness",
            "studio": "studio"}.get(st or "", "other")


def events(article_dir) -> list[dict]:
    from .store import read_jsonl

    return read_jsonl(Path(article_dir) / USAGE_FILE)


def cost_of(ev: dict, prices: dict) -> float | None:
    if ev.get("cost_usd") is not None:
        return float(ev["cost_usd"])
    return pricing.price(ev.get("provider") or "", str(ev.get("item") or ""), ev.get("units") or {}, prices)


def summarize(evs: list[dict], prices: dict | None = None) -> dict:
    """Totals for a set of events: by part of the article, by service, by action, plus what
    could not be priced (in its own units) and whether any of it is an estimate."""
    prices = prices or pricing.DEFAULTS
    total = 0.0
    by_cat: dict[str, float] = {}
    by_prov: dict[str, dict] = {}
    by_op: dict[str, dict] = {}
    unpriced: dict[tuple, dict] = {}
    estimated = False
    for ev in evs:
        c = cost_of(ev, prices)
        cat = category(ev)
        prov = ev.get("provider") or "?"
        op_key = ev.get("op_id") or ev.get("op") or "?"
        op = by_op.setdefault(op_key, {"op": ev.get("op"), "label": OPERATIONS.get(ev.get("op") or "", ev.get("op") or "—"),
                                       "started": ev.get("ts"), "cost": 0.0, "calls": 0,
                                       "estimated": bool(ev.get("estimated"))})
        n_calls = int((ev.get("units") or {}).get("calls") or 1)   # an estimate event stands for several
        op["calls"] += n_calls
        pv = by_prov.setdefault(prov, {"cost": 0.0, "calls": 0, "units": {}})
        pv["calls"] += n_calls
        for k, v in (ev.get("units") or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                pv["units"][k] = pv["units"].get(k, 0) + v
        estimated |= bool(ev.get("estimated"))
        if c is None:
            key = (prov, str(ev.get("item")))
            u = unpriced.setdefault(key, {"provider": prov, "item": ev.get("item"), "units": {}, "calls": 0})
            u["calls"] += 1
            for k, v in (ev.get("units") or {}).items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    u["units"][k] = u["units"].get(k, 0) + v
            continue
        total += c
        by_cat[cat] = by_cat.get(cat, 0.0) + c
        pv["cost"] += c
        op["cost"] += c
    cats = [{"key": k, "label": lbl, "cost": round(by_cat[k], 4),
             "share": (by_cat[k] / total) if total else 0.0}
            for k, lbl in CATEGORIES if by_cat.get(k)]
    provs = [{"key": k, "label": PROVIDER_LABEL.get(k, k), "cost": round(v["cost"], 4),
              "calls": v["calls"], "units": v["units"]}
             for k, v in sorted(by_prov.items(), key=lambda kv: -kv[1]["cost"])]
    ops = sorted(({**v, "id": k, "cost": round(v["cost"], 4)} for k, v in by_op.items()),
                 key=lambda o: o.get("started") or "")
    return {"total": round(total, 4), "calls": len(evs), "by_category": cats, "by_provider": provs,
            "by_operation": ops, "unpriced": list(unpriced.values()), "estimated": estimated}


def article_events(article_dir) -> tuple[list[dict], list[str]]:
    """Every cost event of the article, the estimate of what it cost before the ledger first
    when it has no frozen one, the notes left out; and what could not be known. The events
    article_summary adds up (the Costs page draws them by day)."""
    d = Path(article_dir)
    evs = events(d)
    limits: list[str] = []
    if not _has_baseline(evs):
        base, limits = _estimate_before(d, evs)
        evs = base + evs
    limits += [x for e in evs if e.get("provider") == "note" and e.get("limit") for x in str(e["limit"]).split("; ")]
    return [e for e in evs if e.get("provider") != "note"], limits


def article_summary(article_dir, prices: dict | None = None) -> dict:
    """The article's costs: its ledger, which for an article generated before cost tracking
    starts with the estimate of those earlier costs, frozen before its first new action
    (_freeze_legacy_baseline). Without that start (no ledger yet, or one from before the freeze),
    the estimate is computed from the run record; `limits` says what it could not know."""
    d = Path(article_dir)
    evs = events(d)
    limits: list[str] = []
    if not _has_baseline(evs):
        base, limits = _estimate_before(d, evs)
        evs = base + evs
    out = summarize([e for e in evs if e.get("provider") != "note"], prices)
    out["limits"] = limits + [x for e in evs if e.get("provider") == "note" and e.get("limit")
                              for x in str(e["limit"]).split("; ")]
    return out


# ---------------------------------------------------------------------------------------------
# articles from before the ledger
# ---------------------------------------------------------------------------------------------
_LEGACY_STAGE = {"keyword_researcher": "keywords", "brief_generator": "brief",
                 "deep_researcher": "research", "copywriter": "writing",
                 "visual_director": "visuals", "visual_producer": "visuals",
                 "finding_matcher": "review", "qa_fixer": "edits", "fix_applier": "edits",
                 "edit_verifier": "edits", "claim_verifier": "edits", "ai_editor": "studio"}
_LEGACY_ROLE = {"keyword_researcher": "worker", "brief_generator": "worker", "deep_researcher": "worker",
                "visual_director": "worker", "visual_producer": "worker", "finding_matcher": "worker",
                "claim_verifier": "worker", "ai_editor": "worker", "copywriter": "writer",
                "qa_fixer": "editor", "fix_applier": "editor", "edit_verifier": "editor"}


def legacy_estimate(article_dir) -> list[dict]:
    """Events reconstructed from `_run.json` of an article generated before the ledger existed.

    Only the last full pipeline pass recorded its tokens (`model_usage`, per agent, without the
    model), its Scrapfly credits, the uniqueness credits and the research processor; edits made
    afterwards in the Studio were never counted. The model is inferred from the agent's role in
    today's config; cache writes are assumed at the 5-minute rate. Every event is marked
    `estimated`."""
    from .config import load_core_config
    from .store import read_json_lenient

    d = Path(article_dir)
    run = read_json_lenient(d / "_run.json", {}) or {}
    if not run:
        return []
    try:
        models = load_core_config().get("models") or {}
    except Exception:  # noqa: BLE001
        models = {}
    ts = None
    try:
        ts = datetime.fromtimestamp((d / "_run.json").stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    except OSError:
        pass
    out: list[dict] = []

    def add(provider, item, units, stage_name, agent=None):
        out.append({"ts": ts, "op": "estimate", "op_id": "estimate", "stage": stage_name, "agent": agent,
                    "provider": provider, "item": item, "units": units, "cost_usd": None,
                    "estimated": True})

    for agent, u in ((run.get("model_usage") or {}).get("by_agent") or {}).items():
        units = {"input_tokens": u.get("in", 0), "output_tokens": u.get("out", 0),
                 "cache_read_tokens": u.get("cache_read", 0), "cache_write_5m_tokens": u.get("cache_write", 0),
                 "calls": u.get("calls", 1)}
        if agent.startswith("editor["):
            model = agent[len("editor["):-1]
            if model.startswith("gpt-") or model.startswith("openai:"):
                add("openai", model.removeprefix("openai:"),
                    {"input_tokens": u.get("in", 0), "output_tokens": u.get("out", 0),
                     "cache_read_tokens": u.get("cache_read", 0), "calls": u.get("calls", 1)}, "review", agent)
                continue
            add("anthropic", model, units, "review", agent)
            continue
        model = models.get(_LEGACY_ROLE.get(agent, "worker")) or models.get("worker") or "claude-sonnet-5-5"
        add("anthropic", model, units, _LEGACY_STAGE.get(agent), agent)
    sf = run.get("scrapfly_usage") or {}
    if sf.get("credits"):
        add("scrapfly", "api", {"credits": sf["credits"], "calls": sf.get("calls")}, "visuals")
    research = run.get("research") or {}
    log = [str(n) for n in run.get("log") or []]
    reused = any(n.startswith("[init] reusing the cached brief") for n in log)
    if research.get("_run_id") and not reused:
        # "[research] 1 task-run (pro) · findings=…", else 03_research.md "(processor=pro, id=…)"
        import re

        m = next((re.search(r"task-run \((\w+)\)", n) for n in log if n.startswith("[research]")), None)
        if not m and (d / "03_research.md").is_file():
            m = re.search(r"processor=(\w+)", (d / "03_research.md").read_text(encoding="utf-8"))
        add("parallel", f"task:{m.group(1) if m else 'core'}", {"runs": 1}, "research")
    uniq = run.get("uniqueness") or read_json_lenient(d / "_plagiarism.json", {}) or {}
    if uniq.get("credits_used"):
        add("winston", "plagiarism", {"credits": uniq["credits_used"]}, "uniqueness")
    if (d / "assets" / "cover_bg.png").exists():
        add("openai", "gpt-image-2", {"images": 1, "quality": "medium", "size": [1536, 1024]}, "visuals")
    return out
