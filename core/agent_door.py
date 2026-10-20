"""What an agent and the Studio's pages read about an article: one view of the three facts (the
review, the person's acceptance, WordPress), the review's findings grouped as the Review tab shows
them, and the estimate of a paid action from what such actions cost before. `cli.py … --json` prints these; the screens draw
the same dicts, so an agent never sees another truth than a person.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import publication, store, usage
from . import qa_model as qm

# what a paid action costs when no such action was recorded yet on this install (dollars; None:
# paid in a service's own credits this install has no price for). Review again: $1.57 on its
# first live run (2,200 words, nine pictures, ten changes made and checked; 2026-10-05)
ESTIMATE_DEFAULTS = {"generation": 5.0, "review": 1.60, "fix": 0.10, "proposals": 0.10,
                     "visual_check": 0.10, "uniqueness": None,
                     # a picture made again: the producer on the visuals' model and its looks at a
                     # screenshot, ~$0.02 each (2026-10-06); the install's own record replaces these
                     "cover": 0.05, "visual_edit": 0.10, "visual_insert": 0.08, "ai_edit": 0.02}


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def review_view(art: store.Article) -> dict:
    """The latest review as the Review tab shows it: what needs the person (proposals, safe
    changes that did not land, outline changes), what the machine changed (each group, whether it
    reverts in one click now), and what is closed; the score as a hint."""
    try:
        run = art.run()
    except store.CorruptFile as e:
        return {"review": None, "state": "unreadable", "error": str(e)}
    rv = run.get("review") or {}
    last = (rv.get("reviews") or [None])[-1]
    if not last:
        return {"review": None, "state": "not_reviewed", "needs_you": [], "changed": [], "closed": [],
                "score": None, "counts": {"needs_you": 0, "changed": 0, "closed": 0}}
    text, cover = (art.text(), art.cover()) if art.path("article.md").is_file() else (None, None)
    now = art.version()
    mine = [f for f in rv.get("findings") or [] if f.get("review") == last["id"]]
    needs, closed = [], []
    for f in mine:
        item = {"id": f["id"], "text": f.get("text"), "also": f.get("also") or [], "kind": f.get("kind"), "action": f.get("action"),
                "severity": f.get("severity"), "target": qm.target_label(f.get("target")),
                "on": f.get("target"), "phrase": f.get("phrase"), "basis": f.get("basis"),
                "section": f.get("section"), "source": f.get("source"), "choices": f.get("choices") or []}
        if f.get("status") == "open":
            route, why = qm.route(f, text, cover)
            item["type"] = {"machine": "left", "proposal": "proposal", "manual": "outline"}[route]
            # a person reads one plain sentence; the raw reason stays for an agent (`note`)
            item["why"] = (plain_reason(f["note"], f.get("note_plain")) if route == "machine" and f.get("note")
                           else why or "the machine did not make it")
            item["note"] = f.get("note")
            needs.append(item)
        else:
            item["status"] = f.get("status")
            item["closed"] = f.get("closed")
            item["changed_by"] = f.get("changed_by")
            closed.append(item)
    from .qa_changes import group_at, how_it_reverts, target_span

    # the most important first, then the article's order, as the Review page lists them
    body = store.split_frontmatter(text)[1] if text is not None else ""

    def at(it):
        span = target_span(body, it.get("on")) if body else None
        return span[0] if span else None

    needs.sort(key=lambda it: qm.queue_key(it.get("severity"), at(it)))
    groups = [g for g in rv.get("changes") or [] if g.get("review") == last["id"] and not g.get("reverted")]

    texts = {f["id"]: f.get("text") for f in rv.get("findings") or []}
    sevs = {f["id"]: f.get("severity") for f in rv.get("findings") or []}
    changed = []
    for g in groups:
        how = how_it_reverts(art, rv, g, now)
        changed.append({"id": g["id"], "findings": g.get("findings"), "texts": [texts.get(x) for x in g.get("findings") or []],
                        "severity": qm.worst(sevs.get(x) for x in g.get("findings") or []),
                        "where": g.get("where"), "before": g.get("before"),
                        "after": g.get("after"), "check": g.get("check"), "check_note": g.get("check_note"),
                        "check_said": plain_check(g.get("check_note"), g.get("check_failed") is True),
                        "version_n": g.get("version_n"), "at": g.get("at"), "by": g.get("by"),
                        # the check says its problem is still there: it needs the person until
                        # they keep it as it is, or adjust it by hand
                        "kept_as_is": g.get("kept_as_is") if isinstance(g.get("kept_as_is"), dict) else None,
                        "adjusted": g.get("adjusted") if isinstance(g.get("adjusted"), dict) else None,
                        "half_done": qm.half_done(g),
                        # Revert takes it back now: the newest change, or a picture as it left it
                        "reverts_now": how is not None, "reverts": how,
                        "left": g.get("left") if isinstance(g.get("left"), dict) else None,
                        # the previous bytes of each picture it made again (a file it created: None)
                        # (a record that came with an import may hold anything: R6F6-01)
                        "kept": {str(r.get("path")): r.get("kept")
                                 for r in (g.get("files") if isinstance(g.get("files"), list) else [])
                                 if isinstance(r, dict) and str(r.get("path") or "").startswith("assets/")}})
    # everything that needs the person, in one order: the open findings and the changes whose check
    # says the problem is still there, as the Review page's Needs you shows them (Codex RA5D-03)
    queue = [(qm.queue_key(it.get("severity"), at(it)),
              {"id": it["id"], "is": "finding", "type": it["type"], "severity": it.get("severity"),
               "text": it.get("text")}) for it in needs]
    queue += [(qm.queue_key(c["severity"], group_at(body, g) if body else None),
               {"id": c["id"], "is": "change", "type": "half_done", "severity": c["severity"],
                "text": "Changed, and the check says the problem is still there: "
                        + "; ".join(t for t in c["texts"] if t)})
              for g, c in zip(groups, changed) if c["half_done"]]
    queue = [q for _, q in sorted(queue, key=lambda x: x[0])]
    from . import versions as V

    # the number the review recorded; a review from before it: the number only when one version
    # has those bytes (a Restore repeats them)
    read_n = last.get("version_n")
    if read_n is None:
        same = [v.get("n") for v in V.index(art) if v.get("token") == last.get("version")]
        read_n = same[0] if len(same) == 1 else None
    return {"by_person": rv.get("by_person") if isinstance(rv.get("by_person"), dict) else {},
            "review": {"id": last["id"], "at": last.get("at"), "version": last.get("version"),
                       "version_n": read_n,                 # the version it read, by its number
                       "number": len(rv.get("reviews") or []),
                       # whether the review worked: its reads that came back, the picture check
                       "valid": last.get("valid", True), "valid_reads": last.get("valid_reads"),
                       "reads_requested": last.get("reads_requested"), "picture_check": last.get("picture_check")},
            "state": "reviewed" if last.get("valid", True) else "failed",
            "score": qm.score(mine) if last.get("valid", True) else None,
            "needs_you": needs, "queue": queue, "changed": changed, "closed": closed,
            "counts": {"needs_you": len(queue), "changed": len(changed),
                       "closed": len(closed)}}


def review_summary(art: store.Article) -> str:
    """The end of a review in the words of the page that shows it: no result when it did not work;
    what needs the person (the counts' number, half-done changes in it); and the machine's changes
    by what the check said of each, one per change as the page counts them. "13 finding(s), 7
    changed by the machine" beside "10 need you" read as a contradiction (the operator,
    2026-10-07); a change no check looked at is not "fixed", and a change the person kept or
    adjusted is theirs (R058-07)."""
    rv = review_view(art)
    if not rv.get("review"):
        return "no review"
    head = f"review {rv['review']['id']}: "
    if rv["state"] == "failed":
        return head + "did not work, no result"
    needs = rv["counts"]["needs_you"]
    machine = [c for c in rv["changed"] if not (c["half_done"] or c["kept_as_is"] or c["adjusted"])]
    fixed = sum(1 for c in machine if c["check"] == "fixed")
    unchecked = len(machine) - fixed
    return (head + (f"{needs} need{'s' if needs == 1 else ''} you" if needs else "nothing needs you")
            + (f", {fixed} fixed by the machine" if fixed else "")
            + (f", {unchecked} changed but not checked" if unchecked else ""))


# the reasons the code wrote before it chose the card's words itself (R058-08), as a person reads
# them; anything else is the plain sentence
_PLAIN = (("its change ", " was undone", "its change was undone, so it is open again"),
          ("the picture could not be made", "", "the machine could not make the picture again"),
          ("the fixer call failed", "", "the model did not answer; try again"))


def plain_reason(note: str, plain: str | None = None) -> str:
    """Why the machine's change did not land, as a person reads it: the sentence the code chose
    when it wrote the reason (`plain`: the code's own reason, or a plain sentence for a model's or
    a provider's words, R058-08); for a reason recorded before that, one of the code's few, else
    one plain sentence. The raw reason stays in the record for an agent. The operator read "Tried
    and not kept: the picture could not be made again: producer agent returned no spec: ```json …"
    on a card (2026-10-07)."""
    from .qa_changes import TRIED

    if plain:
        return " ".join(str(plain).split())
    n = " ".join(str(note or "").split())
    for begins, has, said in _PLAIN:
        if n.startswith(begins) and has in n:
            return said
    return TRIED


def plain_check(note: str | None, failed: bool = False) -> str | None:
    """What the check said, for a card: a check that could not run says only that (its error stays
    in the record, R058-08, R9F0-06), by the record's state, or, for a record from before it, by
    the code's own words for it anywhere in the note; otherwise the check's own sentence."""
    from .qa_changes import CHECK_FAILED

    n = str(note or "")
    return CHECK_FAILED if failed or f"{CHECK_FAILED}:" in n or n == CHECK_FAILED else (note or None)


def facts(art: store.Article, prices: dict | None = None) -> dict:
    """One article's facts, kept apart: the version, the review, the acceptance, WordPress, the
    cost; never one combined status."""
    version = art.version()
    title = art.slug
    if art.source is not None:
        try:
            title = art.read()[0].get("title") or art.slug
        except Exception:  # noqa: BLE001 — a title is never worth an unreadable listing
            pass
    rv = review_view(art)
    src = art.path("article.md")
    try:
        cost = usage.article_summary(art.dir, prices)["total"]
    except Exception:  # noqa: BLE001
        cost = None
    return {"slug": art.slug, "title": title, "status": art.status(), "version": version,
            "updated": _iso(src.stat().st_mtime) if src.is_file() else None, "busy": art.busy(),
            "review": ({"state": "unreadable", "error": rv.get("error")} if rv.get("state") == "unreadable"
                       else None if not rv.get("review") else {**rv["review"], "score": rv["score"], **rv["counts"]}),
            "accepted": publication.accepted_state(art, version),
            "wordpress": publication.post_state(art, version), "cost_usd": cost}


def estimate(root, op: str, prices: dict | None = None, last: int = 10) -> dict:
    """What a paid action is likely to cost, in dollars: the average of the last `last` such
    actions recorded on this install, else a default. {"usd", "basis"}; "usd" is None for an
    action paid in credits without a price here."""
    seen = []
    for art in store.Repository(root).articles():
        try:
            ops = usage.article_summary(art.dir, prices)["by_operation"]
        except Exception:  # noqa: BLE001
            continue
        seen += [(o.get("started") or "", o["cost"]) for o in ops
                 if o.get("op") == op and not o.get("estimated") and o.get("cost")]
    recent = sorted(seen)[-last:]
    if recent:
        return {"usd": round(sum(c for _, c in recent) / len(recent), 2),
                "basis": f"the average of the last {len(recent)} on this install"}
    usd = ESTIMATE_DEFAULTS.get(op, 0.0)
    return {"usd": usd, "basis": "a default: none recorded on this install yet" if usd is not None
            else "paid in the service's credits, which have no price on this install"}


def item_op(item: dict) -> str | None:
    """The paid action a ticked item of what needs the person runs (None: free, or not ticked):
    what the machine tries again is a fix, a picture added is made, a picture removed costs
    nothing, any other proposal is made with the proposals."""
    if item.get("type") == "left":
        return "fix"
    if item.get("type") != "proposal" or item.get("action") == "remove_picture" or item.get("choices"):
        return None
    return "visual_insert" if item.get("action") == "add_picture" else "proposals"


def batch_estimate(root, items: list[dict], prices: dict | None = None) -> dict:
    """What making these items together is likely to cost: each item's own action's estimate,
    as the Review page prices each tick (Codex RA54-05). "usd" is None when one has no price."""
    ests = [estimate(root, op, prices) for op in (item_op(it) for it in items) if op]
    if any(e["usd"] is None for e in ests):
        return {"usd": None, "basis": "one of them is paid in a service's credits, which have no price here"}
    bases = sorted({e["basis"] for e in ests})
    return {"usd": round(sum(e["usd"] for e in ests), 2),
            "basis": "the sum of each change's own estimate" + (f" ({'; '.join(bases)})" if bases else "")}


def run_minutes(root, op: str = "generation", last: int = 10) -> dict:
    """How long such an action took here: the average span of its recorded calls (the first to
    the last), over the last `last`; {"minutes": None} when none was recorded."""
    spans = []
    for art in store.Repository(root).articles():
        by_op: dict[str, list[str]] = {}
        for ev in usage.events(art.dir):
            if ev.get("op") == op and ev.get("ts") and not ev.get("estimated"):
                by_op.setdefault(ev.get("op_id") or "?", []).append(ev["ts"])
        for stamps in by_op.values():
            try:
                a, b = (datetime.fromisoformat(min(stamps)), datetime.fromisoformat(max(stamps)))
            except ValueError:
                continue
            if (b - a).total_seconds() > 60:
                spans.append((min(stamps), (b - a).total_seconds() / 60))
    recent = [m for _, m in sorted(spans)[-last:]]
    if not recent:
        return {"minutes": None, "basis": "none recorded on this install yet"}
    return {"minutes": round(sum(recent) / len(recent)), "basis": f"the average of the last {len(recent)}"}
