"""The upgrade of an article to the QA model (core/qa_model.py): `_run.json` schema 2 -> 3, the
article's own record, and the publishing setting.

Per article:

- The old ledger is kept as it was, with its history, under `legacy_findings`, for reading only.
  None of its findings is carried into the model (operator, 2026-10-01): most are leftovers of
  the old review loop, and an article from before the model shows "not reviewed in the new
  system" until a person presses Review again. So nothing old runs, or waits for a choice.
- An article READY under the old rules for exactly this version gets `accepted`, with
  `origin: "migrated from the old automatic READY (<date>)"`.
- `published` is filled from `_wp.json` (the post id, the status it recorded, the link);
  `sent_version` and `sent_at` stay unknown.

The migration reads the folder and writes `_article.json` and `_run.json`, nothing else: no
request to WordPress, no model call, no report rebuilt. It is idempotent: a record already at
schema 3 is left alone, and an `accepted` or `published` already in `_article.json` is kept.

Not applied on read yet: until the Studio's flows move to the model (plan phases 2-4), the old
code still reads `_run.json`'s `findings`. `cli.py migrate-qa` shows what it would do.
"""
from __future__ import annotations

import copy
import hashlib
import json

from . import qa_model as qm
from .store import (Article, CorruptFile, frontmatter_fields, read_json,
                    read_json_lenient, schema_of, split_frontmatter, strip_frontmatter, write_json)

RUN_SCHEMA = 3
LEGACY_OPEN = ("open", "applied", "already_satisfied", "stuck", "contested")   # the old ledger's states
READY_ORIGIN = "migrated from the old automatic READY ({date})"


# ---------------------------------------------------------------------------------------------
# the old rules, as the old records were stamped (frozen copies: the live code that wrote them
# is retired by the plan's last phase, and these records outlive it)
# ---------------------------------------------------------------------------------------------
def _legacy_qa_text_sha(md: str, faq: list | None) -> str:
    """validators.qa_text_sha as of 2026-10-01: what `qa_state.last_round.text_sha` holds."""
    body = strip_frontmatter(md or "").strip()
    canon = "\n".join(line.rstrip() for line in body.splitlines())
    canon += "\n---faq---\n" + json.dumps(list(faq or []), ensure_ascii=False, sort_keys=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


_REVIEWED_FIELDS = ("title", "meta_title", "meta_description", "cover")


def _legacy_manifest(art: Article, fm: dict, body: str) -> dict:
    """scorecard.review_manifest as of 2026-10-01: the title fields and the picture bytes a
    review saw (`qa_state.last_round.reviewed`)."""
    import re

    refs = set(re.findall(r"\]\((assets/[^)\s]+)\)", body or ""))
    if isinstance(fm.get("cover"), str) and fm["cover"].startswith("assets/"):
        refs.add(fm["cover"])
    assets = {}
    for ref in sorted(refs):
        f = art.dir / ref
        assets[ref] = hashlib.sha1(f.read_bytes()).hexdigest()[:16] if f.is_file() else None
    return {**{k: fm.get(k) for k in _REVIEWED_FIELDS}, "assets": assets}


def _manifest_changes(then: dict, now: dict) -> list[str]:
    out = [k for k in _REVIEWED_FIELDS if (then.get(k) or None) != (now.get(k) or None)]
    ta, na = then.get("assets") or {}, now.get("assets") or {}
    return out + [r for r in sorted(set(ta) | set(na)) if ta.get(r, "absent") != na.get(r, "absent")]


def old_ready(art: Article, run: dict) -> tuple[bool, str]:
    """Whether the article was READY under the old rules for exactly the version on disk.

    READY is the stored scorecard's verdict (publishable, QA exit ready: what the dashboard
    showed). It counts for this version when the card judged this text (its `revision`, or, for a
    card from before cards recorded one, the text hash the last QA round recorded) and, where the
    review recorded what it saw, the pictures and the title fields are still those. A READY given
    to an earlier text is not carried: the old rules themselves no longer called it READY."""
    card = read_json_lenient(art.path("_scorecard.json"), {}) or {}
    if not isinstance(card, dict) or card.get("publishable") is not True \
            or (card.get("qa") or {}).get("qa_exit") != "ready":
        return False, "the old checks did not say READY"
    md = art.text()
    block, body = split_frontmatter(md)
    fm = frontmatter_fields(block)
    lr = ((run.get("qa_state") or {}).get("last_round") or {})
    if card.get("revision"):
        if card["revision"] != art.revision():
            return False, "the old READY was given to an earlier version of the text"
    else:
        faq = fm.get("faq") if isinstance(fm.get("faq"), list) else ((run.get("brief") or {}).get("faq") or [])
        if not lr.get("text_sha") or lr["text_sha"] != _legacy_qa_text_sha(md, faq):
            return False, "the old READY was given to an earlier version of the text"
    if isinstance(lr.get("reviewed"), dict):
        changed = _manifest_changes(lr["reviewed"], _legacy_manifest(art, fm, body))
        if changed:
            return False, "changed since the old READY: " + ", ".join(changed[:4])
    return True, "READY under the old rules for this version"


def published_from_wp(art: Article, date: str) -> dict | None:
    """`published` from the binding the publisher kept: the post id, the status it recorded and
    the link. Which version was sent, and when, is unknown. A binding that cannot be read, or
    that holds uploads but no post id, says the outcome is unknown, not that nothing was sent."""
    p = art.path("_wp.json")
    if not p.exists():
        return None
    origin = f"migrated from _wp.json ({date})"
    try:
        wp = read_json(p, default={}) or {}
    except CorruptFile:
        wp = None
    if not isinstance(wp, dict):
        return qm.published_record(None, origin=origin, unknown="_wp.json could not be read")
    if not wp.get("post_id"):
        if wp.get("media"):
            return qm.published_record(None, origin=origin,
                                       unknown="pictures were uploaded but no post id was recorded")
        return None
    return qm.published_record(wp.get("post_id"), wp_status=wp.get("status"), link=wp.get("link"),
                               site=wp.get("site"), origin=origin)


# ---------------------------------------------------------------------------------------------
# the article
# ---------------------------------------------------------------------------------------------
def plan(art: Article, at: str | None = None) -> dict:
    """What the migration makes of one article, without writing anything:
    {"slug", "status": "migrate" | "already" | "empty", "run", "record", "report"}.
    An unreadable `_run.json` raises CorruptFile: it is not read as "no findings"."""
    at = at or qm.now_iso()
    run = art.run()
    out = {"slug": art.slug, "run": None, "record": None}
    if run and schema_of(run) >= RUN_SCHEMA:
        return {**out, "status": "already", "report": {"slug": art.slug, "status": "already migrated"}}
    if not run and art.source is None:
        return {**out, "status": "empty", "report": {"slug": art.slug, "status": "nothing to migrate"}}
    version = art.version()
    legacy = list(run.get("findings") or [])
    # how the old checklist stood, as history: its own states, nothing recomputed
    open_before = sum(1 for it in legacy if isinstance(it, dict) and it.get("state") in LEGACY_OPEN
                      and not it.get("waived"))

    ready, why = old_ready(art, run) if art.source is not None else (False, "no text")
    record = art.record()
    date = at[:10]
    accepted = record.get("accepted")
    if accepted is None and ready:
        accepted = qm.accepted_record(version, who="migration", origin=READY_ORIGIN.format(date=date), at=at)
    published = record.get("published") if "published" in record else published_from_wp(art, date)

    migration = {"at": at, "from_schema": schema_of(run) if run else None, "version": version,
                 "legacy": len(legacy), "legacy_open": open_before, "old_ready": ready, "old_ready_why": why}
    new_run = {k: v for k, v in run.items() if k != "findings"}
    new_run.update({
        "schema": RUN_SCHEMA,
        "legacy_findings": legacy,
        # no review has run in the model yet: nothing to apply, nothing to choose
        "review": {"reviews": [], "findings": [], "changes": [], "migration": migration},
    })
    new_record = {**{k: v for k, v in record.items() if k != "schema"},
                  "accepted": accepted, "published": published}
    report = {"slug": art.slug, "status": "migrated", "version": version, "legacy": len(legacy),
              "legacy_open": open_before, "accepted": bool(accepted), "old_ready": why,
              "published": (published or {}).get("post_id") if published else None,
              "published_unknown": (published or {}).get("unknown") if published else None}
    return {**out, "status": "migrate", "run": new_run, "record": new_record, "report": report}


def migrate_article(art: Article, apply: bool = True, at: str | None = None) -> dict:
    """Migrate one article; returns the report. With `apply`, under the article's lock (Busy
    while a job works on it): the article's record first, then the run record, so a crash between
    the two leaves a run that migrates again to the same result."""
    if not apply:
        return plan(art, at)["report"]
    with art.lock(wait=False, holder="the QA migration"):
        p = plan(art, at)
        if p["status"] == "migrate":
            art.save_record(p["record"])
            write_json(art.path("_run.json"), p["run"])
        return p["report"]


def migrate_install(root, at: str | None = None) -> dict:
    """Every article of an install into the model, once (the Studio does this when it starts;
    `cli.py migrate-qa --apply` the same). An article a job holds, or with an unreadable record,
    is left for the next start and named. No WordPress request, no model call.
    Returns {"migrated": [slugs], "already": n, "empty": n, "skipped": {slug: why}}."""
    from .store import Busy, CorruptFile, Repository

    out = {"migrated": [], "already": 0, "empty": 0, "skipped": {}}
    for art in Repository(root).articles():
        try:
            rep = migrate_article(art, apply=True, at=at)
        except Busy as e:
            out["skipped"][art.slug] = f"a job holds it ({e})"
            continue
        except CorruptFile as e:
            out["skipped"][art.slug] = f"its record does not parse: {e}"
            continue
        status = rep.get("status")
        if status == "migrated":
            out["migrated"].append(art.slug)
        elif status == "already migrated":
            out["already"] += 1
        else:
            out["empty"] += 1
    return out


# ---------------------------------------------------------------------------------------------
# the setting
# ---------------------------------------------------------------------------------------------
def migrate_settings(overrides: dict, at: str | None = None) -> tuple[dict, dict | None]:
    """`publishing.on_ready` (what happened to a READY article) is retired. The new choice,
    what Accept also does, starts at nothing whatever the old value was: the old value is not
    carried into it, only recorded once under `_migrated`, so Settings can say what it was.
    Returns (the new overrides, the note or None when there was nothing to migrate)."""
    from .settings import MIGRATED

    data = copy.deepcopy(overrides or {})
    pub = data.get("publishing") if isinstance(data.get("publishing"), dict) else None
    if not pub or "on_ready" not in pub:
        return data, None
    old = pub.pop("on_ready")
    if not pub:
        data.pop("publishing")
    note = {"publishing.on_ready": old, "at": at or qm.now_iso()}
    data.setdefault(MIGRATED, {}).update(note)
    return data, note


def migrate_settings_file(output_dir, at: str | None = None) -> dict | None:
    """migrate_settings on the install's `_settings.json`; writes only when there was an old
    value. Returns the note."""
    from .settings import file_path

    p = file_path(output_dir)
    try:
        current = read_json(p, default=None)
    except CorruptFile:
        return None
    if not isinstance(current, dict):
        return None
    new, note = migrate_settings(current, at)
    if note:
        write_json(p, new)
    return note
