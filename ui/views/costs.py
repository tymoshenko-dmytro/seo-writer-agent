"""Costs, like Parallel's Usage page, in dollars:
the period, the article, the service and the operation as filters; the spend per day, stacked by
part of the article; the total as one big number, the average article, by service; every
operation, with a toggle to its units; Export CSV. Read from each article's cost ledger
(core/usage.py), the estimate of an article from before the ledger included and marked."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, Response, render_template, request

from core import usage

from ..charts import stacked_bars
from ..context import repo
from .common import prices

bp = Blueprint("costs", __name__)

# the parts of an article the chart stacks (the ledger's categories, grouped)
PARTS = (("Research", ("keywords", "brief", "research")), ("Writing", ("text",)),
         ("Pictures", ("charts", "images", "screenshots")), ("Review", ("review",)),
         ("Edits", ("edits", "studio", "uniqueness", "other")))
PERIODS = (("30", "Last 30 days"), ("month", "This month"), ("all", "All time"))


def _day(ts: str) -> date | None:
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t.astimezone() if t.tzinfo else t).date()


def _when(ts: str) -> datetime:
    """An event's time, comparable whatever its zone is written as ("Z", "+00:00")."""
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _part(cat: str) -> int:
    return next(i for i, (_, cats) in enumerate(PARTS) if cat in cats or i == len(PARTS) - 1)


def _collect(pr: dict) -> tuple[list[dict], list[dict], list[str]]:
    """Every priced or unpriced event of every article: (events, articles, what is not known)."""
    evs, arts, limits = [], [], []
    for art in repo().articles():
        try:
            fm, _, _ = art.read() if art.source else ({}, "", "")
            items, lim = usage.article_events(art.dir)
        except Exception:  # noqa: BLE001 — one unreadable ledger must not take the page down
            continue
        title = fm.get("title") or art.slug
        if items:
            arts.append({"slug": art.slug, "title": title, "written": art.path("article.md").is_file()})
        limits += [f"{title}: {x}" for x in lim]
        for e in items:
            d = _day(e.get("ts") or "")
            evs.append({**e, "slug": art.slug, "title": title, "day": d, "cost": usage.cost_of(e, pr),
                        "category": usage.category(e)})
    return evs, arts, limits


def _filtered(evs: list[dict], args) -> tuple[list[dict], date | None, date]:
    today = date.today()
    period = args.get("p") or "30"
    start = {"30": today - timedelta(days=29), "month": today.replace(day=1)}.get(period)
    keep = []
    for e in evs:
        if start and (e["day"] is None or e["day"] < start):
            continue
        if args.get("article") and e["slug"] != args["article"]:
            continue
        if args.get("service") and e.get("provider") != args["service"]:
            continue
        if args.get("op") and (e.get("op") or "") != args["op"]:
            continue
        keep.append(e)
    if start is None:
        days = [e["day"] for e in keep if e["day"]]
        start = min(days) if days else today
    return keep, start, today


def _operations(evs: list[dict]) -> list[dict]:
    """One row per operation (a generation, a review, a redraw): its article, when, what it cost
    and in what units."""
    ops: dict[tuple, dict] = {}
    for e in evs:
        key = (e["slug"], e.get("op_id") or e.get("op") or "?")
        o = ops.setdefault(key, {"slug": e["slug"], "title": e["title"], "op": e.get("op"), "op_id": key[1],
                                 "within": e.get("within"),
                                 "label": usage.OPERATIONS.get(e.get("op") or "", e.get("op") or "—"),
                                 "started": e.get("ts") or "", "cost": 0.0, "unpriced": False, "units": {},
                                 "estimated": bool(e.get("estimated"))})
        o["started"] = min(o["started"], e.get("ts") or o["started"]) if o["started"] else (e.get("ts") or "")
        if e["cost"] is None:
            o["unpriced"] = True
        else:
            o["cost"] += e["cost"]
        for k, v in (e.get("units") or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                o["units"][k] = o["units"].get(k, 0) + v
    return sorted(ops.values(), key=lambda o: o["started"], reverse=True)


@bp.route("/costs")
def costs():
    pr = prices()
    evs, arts, limits = _collect(pr)
    shown, start, end = _filtered(evs, request.args)
    by_month = (end - start).days > 92
    if by_month:
        labels, keys, d = [], [], start.replace(day=1)
        while d <= end:
            labels.append(d.strftime("%b %Y"))
            keys.append((d.year, d.month))
            d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    else:
        keys = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        labels = [f"{k:%b} {k.day}" for k in keys]
    index = {k: i for i, k in enumerate(keys)}
    values = [[0.0] * len(PARTS) for _ in keys]
    for e in shown:
        if e["day"] is None or not e["cost"]:
            continue
        k = (e["day"].year, e["day"].month) if by_month else e["day"]
        if k in index:
            values[index[k]][_part(e["category"])] += e["cost"]
    total = sum(e["cost"] or 0 for e in shown)
    ops = _operations(shown)
    # an article written: its run left an article (RD41-05), and what it cost from the topic to
    # its first review is the run and the review that ran inside it (RD41-06), not a later one
    written_slugs = {a["slug"] for a in arts if a.get("written")}
    gens = [o for o in ops if o["op"] in ("generation", "estimate") and o["slug"] in written_slugs]
    written = {o["slug"] for o in gens}
    # each run's cycle is known when the ledger links its first review to it, when it reviewed
    # inside itself (the pipeline before the QA model), or when no review came before the next
    # run; a review recorded before the link may be its first or a later one. An article with
    # one run whose cycle is not known is left out of the average (RD9C-01: per run, not per
    # article, so a later linked run does not vouch for an earlier one)
    unknown, notes = [], []
    for slug in sorted(written):
        mine = sorted((e for e in evs if e["slug"] == slug), key=lambda e: _when(e.get("ts") or ""))
        starts: dict[str, datetime] = {}
        for e in mine:
            if e.get("op") == "generation":
                starts.setdefault(e.get("op_id") or "?", _when(e.get("ts") or ""))
        order = sorted(starts, key=lambda g: starts[g])
        for i, g in enumerate(order):
            upto = starts[order[i + 1]] if i + 1 < len(order) else None       # the next run's start
            linked = any(e.get("within") == g for e in mine)
            inside = any(e.get("op_id") == g and e["category"] in ("review", "edits") for e in mine)
            stray = any(e.get("op") == "review" and not e.get("within") and _when(e.get("ts") or "") >= starts[g]
                        and (upto is None or _when(e.get("ts") or "") < upto) for e in mine)
            if stray and not (linked or inside):
                unknown.append(slug)
                break
        if unknown and unknown[-1] == slug:
            notes.append(f"{next(e['title'] for e in mine)}: what its first review cost (recorded before a review "
                          "was linked to its run), so it is not in the average")
    runs = {(o["slug"], o["op_id"]) for o in gens if o["slug"] not in unknown}
    cycle: dict[str, float] = {}
    for o in [g for g in gens if g["slug"] not in unknown] + [o for o in ops if o.get("within") and (o["slug"], o["within"]) in runs]:
        cycle[o["slug"]] = cycle.get(o["slug"], 0.0) + o["cost"]
    services: dict[str, dict] = {}
    for e in shown:
        s = services.setdefault(e.get("provider") or "?", {"label": usage.PROVIDER_LABEL.get(e.get("provider"), e.get("provider") or "?"),
                                                           "cost": 0.0, "units": {}, "unpriced": False})
        if e["cost"] is None:
            s["unpriced"] = True
        else:
            s["cost"] += e["cost"]
        for k, v in (e.get("units") or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                s["units"][k] = s["units"].get(k, 0) + v
    by_article: dict[str, dict] = {}
    for e in shown:
        a = by_article.setdefault(e["slug"], {"slug": e["slug"], "title": e["title"], "cost": 0.0})
        a["cost"] += e["cost"] or 0
    by_article_rows = sorted(by_article.values(), key=lambda a: -a["cost"])
    every = 1 if len(labels) <= 12 else 7 if len(labels) <= 62 else 14
    args = {k: v for k, v in request.args.items() if v}

    def qs(**over) -> str:
        """This page's address with `over` changed (None takes a filter away)."""
        from urllib.parse import urlencode

        merged = {k: v for k, v in {**args, **over}.items() if v is not None}
        return "?" + urlencode(merged) if merged else "?"
    return render_template("costs.html", chart=stacked_bars(labels, [p for p, _ in PARTS], values, every=every),
                           parts=[p for p, _ in PARTS], by_part=[(p, sum(v[i] for v in values)) for i, (p, _) in enumerate(PARTS)],
                           total=total, articles_written=len(written), operations=len(ops),
                           average=(sum(cycle.values()) / len(cycle)) if cycle else None,
                           services=sorted(services.items(), key=lambda kv: -kv[1]["cost"]),
                           by_article=by_article_rows, peak=max((a["cost"] for a in by_article_rows), default=0) or 1,
                           ops=ops[:300], more=max(0, len(ops) - 300), units=request.args.get("units") == "1",
                           periods=PERIODS, period=request.args.get("p") or "30", args=args, arts=arts,
                           service_names=sorted({e.get("provider") for e in evs if e.get("provider")}),
                           op_names=sorted({e.get("op") for e in evs if e.get("op")}),
                           op_label=usage.OPERATIONS, provider_label=usage.PROVIDER_LABEL,
                           estimated=any(e.get("estimated") for e in shown), limits=(notes + limits)[:5], left_out=len(unknown), qs=qs,
                           any_unpriced=any(s["unpriced"] for s in services.values()),
                           start=start, end=end)


@bp.route("/costs.csv")
def costs_csv():
    """The operations the page shows, as a CSV: when, the article, the operation, the cost, the
    units."""
    pr = prices()
    evs, _, _ = _collect(pr)
    shown, _, _ = _filtered(evs, request.args)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["started", "article", "operation", "cost_usd", "priced", "estimated", "units"])
    for o in _operations(shown):
        w.writerow([o["started"], o["slug"], o["label"], f"{o['cost']:.4f}", "no" if o["unpriced"] else "yes",
                    "yes" if o["estimated"] else "no", "; ".join(f"{k}={v:g}" for k, v in sorted(o["units"].items()))])
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="costs-{stamp}.csv"'})
