"""The articles: the dashboard, an article's page, the editor, the SEO/FAQ form, delete."""
from __future__ import annotations

import shutil
import time
from datetime import datetime, timezone

from flask import (Blueprint, Response, abort, flash, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)

from core import store
from core.tools.markdown_render import render as md_render
from core.validators import validate_meta

from .. import meta as M
from ..context import article as get_article
from ..context import out, project, repo
from ..jobs import runner
from .common import key_state, prices
from ..privacy import scrub_mixed

bp = Blueprint("articles", __name__)


@bp.route("/")
def dashboard():
    """The articles: New article and the "⋯" menu (Import); four
    filters and a search; a row is the title, one muted line, the three facts and the cost; one
    banner line when something blocks work. The link index and the system's state are Connections'."""
    from .. import article_view as V

    pr = prices()
    jobs = runner.all()
    rows = []
    for art in repo().articles():
        try:
            rows.append(V.list_row(art, pr, jobs))
        except Exception as e:  # noqa: BLE001 — one broken folder must not take the list down
            rows.append({"slug": art.slug, "title": art.slug, "updated": "", "cost": None, "status": "broken",
                         "failed": ("Cannot be read", scrub_mixed(str(e))[:200]), "review": None, "yours": None,
                         "wp": None, "look": True, "accepted": False, "on_wp": False, "updated_at": ""})
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    key = request.args.get("filter") or "all"
    key = key if key in dict(V.FILTERS) else "all"
    q = request.args.get("q") or ""
    # the filter's whole set, the rows the search does not match hidden: the search box narrows
    # and widens it as it is typed, without JavaScript the GET's result is what shows (R6BD-02)
    shown = [{**r, "match": V.matches(r, q)} for r in V.list_filter(rows, key)]
    counts = {k: len(V.list_filter(rows, k)) for k, _ in V.FILTERS}
    from core import setup

    mem = M.mem_limit_mb()
    following = any(j.get("status") in ("queued", "running") for j in jobs)
    return render_template("articles.html", rows=shown, total=len(rows), filter=key, q=q, counts=counts,
                           following=following, setup=setup.state(out()),
                           filters=V.FILTERS,
                           mem_limit=mem if mem and mem < M.MIN_MEM_MB else None, mem_needed=M.MIN_MEM_MB)


@bp.route("/a/<slug>")
def article(slug):
    art = get_article(slug)
    # the version on the disk before the page reads anything, compared with the one after it has
    # read everything: the forms carry it only when both are the same, so a decision is never
    # taken on a version the page did not show (review RC-02, contract 1)
    version_before = art.version()
    pr = prices()
    meta = M.page_meta(art, pr)
    if meta["status"] != "article":
        return render_template("run_page.html", **_run_page(art, meta, pr))
    return render_template("article_page.html", **article_page(art, meta, pr, version_before))


def _run_page(art, meta: dict, pr: dict) -> dict:
    """A run without its article yet: in progress, failed, stopped, or only a draft."""
    from core import agent_door

    from .. import article_view as V
    from ..privacy import scrub

    try:
        run = art.run()
    except store.CorruptFile as e:
        run = {"status": "corrupt", "error": str(e)}
    jobs = [j for j in runner.all() if j.get("slug") == art.slug]
    job = next((j for j in jobs if j.get("status") in ("queued", "running")), None)
    status = run.get("status") or meta["status"]
    running = bool(job) or (status == "running" and bool(art.busy()))
    reason = {"failed": "The run failed", "aborted": "The run stopped on purpose before writing",
              "corrupt": "The run's record cannot be read", "empty": "Nothing was written in this folder",
              "running": "The run stopped before it finished: no job works on it any more (the Studio restarted, or "
                         "the process was stopped)"}.get(status, "The run did not finish")
    if run.get("error") and status != "running":
        reason += f": {scrub(str(run['error']))}"
    cached = bool((run.get("brief") or {}).get("sections") and (run.get("research") or {}).get("findings"))
    est = agent_door.estimate(out(), "generation", pr)
    draft = art.path("04_draft.md")
    return {"meta": meta, "run": run, "status": status, "running": running, "reason": reason + ".",
            "job_step": (job or {}).get("step"),
            "stages": V.run_stages(art, run, pr, job), "cached": cached,
            "again_cost": f"about ${est['usd'] * (0.5 if cached else 1):.0f}" if est.get("usd") else None,
            "stages_reports": M.stage_reports(art),
            "draft_html": M._HEADING.sub(lambda m: f"<{m[1]}h{min(int(m[2]) + 1, 6)}",
                                        M.render_article_html(art.slug, draft.read_text(encoding="utf-8"), art.dir))
            if draft.exists() else "",                  # its title under the run's: an h2
            "log": [scrub(str(x)) for x in (run.get("log") or [])[-60:]]}


@bp.route("/a/<slug>/run-again", methods=["POST"])
def run_again(slug):
    """Run a failed or stopped run again: from its cached keyword research, brief and research
    in its own folder when it has them (not paid for again), else the topic from the start."""
    from core import setup

    art = get_article(slug)
    if art.path("article.md").is_file():
        flash("Nothing started: this article is written; Review again is on its page.")
        return redirect(url_for("articles.article", slug=slug))
    refused = setup.ready_to_write(out())
    if refused:
        flash(f"Nothing started. {refused} See Connections.")
        return redirect(url_for("articles.article", slug=slug))
    if art.busy() or any(j.get("slug") == slug and j.get("status") in ("queued", "running") for j in runner.all()):
        flash("Nothing started: a job is already working on it.")
        return redirect(url_for("articles.article", slug=slug))
    try:
        run = art.run()
    except store.CorruptFile:
        run = {}
    topic = run.get("topic") or slug.replace("-", " ")
    cached = bool((run.get("brief") or {}).get("sections") and (run.get("research") or {}).get("findings"))
    if cached:
        runner.submit(topic, reuse_from=slug, reuse_in_place=True)
        flash("Running it again from its research: the page follows it.")
        return redirect(url_for("articles.article", slug=slug))
    runner.submit(topic)
    flash(f"Started “{topic}” again from the start, in a new folder.")
    return redirect(url_for("articles.dashboard"))


TABS = ("article", "review", "publish", "seo", "pictures", "sources", "history")


def article_page(art, meta: dict, pr: dict, version_before: str, tab: str | None = None,
                 seo_typed: dict | None = None, seo_refused: str | None = None) -> dict:
    """Everything the article's page shows on the tab asked for (ui/article_view.py). A refused
    SEO Save comes back with what was typed (`seo_typed`) and why (`seo_refused`)."""
    from core import agent_door, versions

    from .. import article_view as V

    tab = tab or request.args.get("tab") or "article"
    tab = tab if tab in TABS else "article"
    fm, body, _ = art.read()
    rv = agent_door.review_view(art)
    costs = V.costs_of(out(), pr)
    rail = V.rail(art, version_before, rv, project(), costs)
    items = V.review_queue(art, body, rv, costs) if rv.get("review") else []
    working = _working(art.slug)
    for it in items:
        if it.get("group") == "needs":
            it["working"] = working["findings"].get(it["id"]) or working["assets"].get(it.get("picture") or "")
    listing = versions.listing(art)
    ctx = {"meta": meta, "fm": fm, "tab": tab, "rv": rv, "rail": rail, "items": items, "costs": costs,
           "updated": V.when(datetime.fromtimestamp(meta["mtime"], timezone.utc).isoformat()) if meta.get("mtime") else "",
           "version_n": listing[0]["n"] if listing else None, "job": _page_job(art.slug), "working": working,
           "wp_admin": _wp_admin_url(meta), "cover": _cover_url(art), "cover_path": art.cover(), "keys": key_state(),
           "shown_marks": V.shown_marks(body, items)}
    if tab == "article":
        marks = request.args.get("marks") == "1"
        ctx["marks"] = marks
        ctx["html"] = V.marked_article(art.slug, body, items) if marks else V.article_html(art.slug, body)
        ctx["n_marks"] = len(ctx["shown_marks"])
        ctx["n_open"] = sum(1 for it in items if it.get("group") == "needs")
    elif tab == "history":
        ctx["hist"] = V.history(art, version_before, pr, rail)
    elif tab == "publish":
        from core import publication as P

        rec = P.published(art) or {}
        ctx["checks"] = P.pre_send_checks(art)
        ctx["options"] = P.send_options(rec) + ([("unpublish", "Take it off the blog (back to a draft)")]
                                                if P.is_live(rec) else [])
        ctx["sends"] = [{**x, "when": V.when(x.get("at"))} for x in reversed((art.record() or {}).get("sends") or [])]
        ctx["category"] = _default_category()
    elif tab == "seo":
        ctx["seo"] = {**_seo_view(art, fm, meta), **(seo_typed or {})}
        ctx["seo_refused"] = seo_refused
    elif tab == "pictures":
        ctx["pics"] = V.pictures(art, body, rv)
    elif tab == "sources":
        ctx["stages"] = M.stage_reports(art)
        try:
            run = art.run()
        except store.CorruptFile:
            run = {}
        ctx["log"] = [M_scrub(x) for x in (run.get("log") or [])[-40:]]
        # what the run worked from that the Studio changed: its prompts and the project's texts
        from core import project_texts, prompt_overrides

        qs = run.get("qa_state") or {}
        labels = {f["key"]: f["label"] for f in project_texts.FIELDS}
        ctx["edited"] = {"prompts": [prompt_overrides._LABELS.get(n, n) for n in qs.get("prompt_edits") or []],
                         "texts": [labels.get(k, k) for k in qs.get("project_edits") or []]}
    # the version the forms carry: only when nothing moved while the page was read
    ctx["version"] = version_before if art.version() == version_before else ""
    return ctx


def M_scrub(line):
    from ..privacy import scrub

    return scrub(str(line))


def _cover_url(art) -> str | None:
    c = art.cover()
    if not c or not art.path(c).is_file():
        return None
    return f"/a/{art.slug}/{c}?v={M.asset_version(art.dir, c)}"


def _working(slug: str) -> dict:
    """What the jobs on this article are making now and what waits in their queue: by finding and
    by picture ("running" or "queued"), and the active jobs' ids the page follows. A card whose
    change was asked for stays where it was with nothing to say so (2026-10-06)."""
    out = {"findings": {}, "assets": {}, "ids": []}
    for j in runner.on_article(slug):
        state = "running" if j.get("status") == "running" else "queued"
        out["ids"].append(j["id"])
        for fid in j.get("findings") or []:
            out["findings"].setdefault(fid, state)
        if j.get("asset"):
            out["assets"].setdefault(j["asset"], state)
    return out


def _page_job(slug: str) -> dict | None:
    """The job working on this article (the running one, with how many wait behind it), or the
    one that finished in the last ten minutes: the page follows the first and shows the outcome
    of the second."""
    from ..privacy import scrub_job

    mine = [j for j in runner.all() if j.get("slug") == slug and j.get("kind") != "article"]
    if not mine:
        return None
    active = runner.on_article(slug)
    if active:
        now = next((j for j in active if j.get("status") == "running"), active[0])
        return {**scrub_job(now), "active": True, "waiting": len(active) - 1, "ids": [j["id"] for j in active]}
    j = scrub_job(mine[0])
    if j.get("status") in ("queued", "running"):
        return {**j, "active": True}
    try:
        done = datetime.fromisoformat(str(j.get("finished")).replace("Z", "+00:00"))
        if done.tzinfo is None:
            done = done.astimezone()
        if (datetime.now(timezone.utc) - done).total_seconds() > 600:
            return None
    except ValueError:
        return None
    return {**j, "active": False}


def _seo_view(art, fm: dict, meta: dict) -> dict:
    """The SEO tab: the fields, the search preview's host, what the limits say, the keyword
    coverage as sentences."""
    kw = store.read_json_lenient(art.path("_keywords.json"), {}) or {}
    stale = bool(kw) and kw.get("revision") not in (None, art.revision())
    focus = fm.get("focus_keyword") or (kw.get("primary") or {}).get("keyword") or ""
    return {"meta_title": fm.get("meta_title") or "",
            "meta_description": fm.get("meta_description") or fm.get("description") or "",
            "slug": fm.get("slug") or art.slug, "focus_keyword": focus, "faq": fm.get("faq") or [],
            "host": _blog_host(),
            "problems": validate_meta({"meta_title": fm.get("meta_title"),
                                       "meta_description": fm.get("meta_description") or fm.get("description")},
                                      focus, fm.get("title") or ""),
            "checks": kw.get("checks") or [], "verdict": kw.get("verdict"), "stale": stale,
            "built": M.mtime_label(art.path("_keywords.json")) if hasattr(M, "mtime_label") else ""}


def _default_category() -> str:
    """The category the publish button suggests: the one chosen for automatic posts, else the
    first the profile maps."""
    from core.settings import _profile_categories

    p = project()
    chosen = ((p.seo or {}).get("publishing") or {}).get("category")
    return chosen or next(iter(_profile_categories()), "")


def _blog_host() -> str:
    from urllib.parse import urlparse

    p = project()
    base = (p.publish or {}).get("base_url") or (p.links or {}).get("blog_base_url") or ""
    return urlparse(base).netloc or "blog"


def _wp_admin_url(meta: dict) -> str | None:
    post = (meta.get("wp") or {}).get("post_id")
    if not post:
        return None
    from core.tools.wp_publisher import WPPublisher

    base = WPPublisher.site_url()
    return f"{base}/wp-admin/post.php?post={post}&action=edit" if base else None


@bp.route("/a/<slug>/edit", methods=["GET", "POST"])
def edit(slug):
    """The block editor: the article as blocks over its
    Markdown (ui/static/js/mdblocks.js), an explicit Save that is a version of its own, the
    leave prompt, the Versions panel. A save carries the revision it was written against; when
    the article changed meanwhile the person's text comes back with the choice to replace."""
    art = get_article(slug)
    if not art.path("article.md").is_file():
        flash("There is no finished article to edit here yet.")
        return redirect(url_for("articles.article", slug=slug))
    block, body = store.split_frontmatter(art.path("article.md").read_text(encoding="utf-8"))
    revision, newer, status, saved_body = art.revision(), None, 200, None
    if request.method == "POST":
        # browsers submit a form field with CRLF newlines; the file is LF everywhere else
        new_body = request.form.get("body", "").replace("\r\n", "\n").replace("\r", "\n")
        # The version the text was written against. After a conflict the page keeps the OLD one,
        # so pressing Save again conflicts again instead of saving over the newer text (review
        # RV-05); replacing the newer version is its own box, which carries that version.
        base = request.form.get("overwrite") or request.form.get("revision") or ""
        if not base:
            flash("Not saved: the form did not say which version it was opened at. Reload the page.")
            return render_template("editor.html", **_editor_page(art, new_body, revision, None, saved_body=body)), 400
        try:
            art.write_article(block, new_body, base_revision=base, who="the editor", exact=True)
        except store.Busy as e:
            flash(f"Not saved: {scrub_mixed(str(e))}. Your text is kept below; save again when it finishes.")
            saved_body, body, revision, status = body, new_body, base, 409
        except store.Conflict:
            flash("Not saved: the article changed since you opened it (a review or another tab saved it). "
                  "Your text is kept below: copy what you need and reload, or tick “Replace the newer "
                  "version with my text” and save.")
            saved_body = store.split_frontmatter(art.path("article.md").read_text(encoding="utf-8"))[1]
            body, revision, newer, status = new_body, request.form.get("revision") or base, art.revision(), 409
        else:
            from core import versions

            last = (versions.index(art) or [{}])[-1]
            flash(f"Saved as v{last.get('n')}." if last.get("n") else "Saved.")
            return redirect(url_for("articles.edit", slug=slug))
    return render_template("editor.html", **_editor_page(art, body, revision, newer, saved_body=saved_body)), status


def _editor_page(art, body: str, revision: str, newer: str | None, saved_body: str | None = None) -> dict:
    from core import agent_door, publication, versions

    from .. import article_view as V

    version = art.version()
    listing = versions.listing(art)
    current = next((v for v in listing if v.get("current")), None)
    grouped, _ = V.versions_list(art, publication.accepted_state(art, version), publication.post_state(art, version))
    est = {op: agent_door.estimate(out(), op, prices()) for op in ("ai_edit", "visual_insert", "visual_edit")}
    return {"meta": M.page_meta(art, prices()), "body": body, "revision": revision, "newer": newer,
            "saved_body": saved_body,
            "version": version, "versions": grouped, "current": current,
            "saved": f"v{current['n']}, {V.when(current.get('at'))}" if current else "",
            "costs": {k: (f"about ${e['usd']:.2f}" if e.get("usd") is not None else None) for k, e in est.items()}}


@bp.route("/a/<slug>/seo", methods=["POST"])
def save_seo(slug):
    """The SEO tab's Save (core/article_edits.save_seo): a version of its own, of the version the
    page showed. Refused, the tab comes back with what was typed and why: nothing is said saved."""
    from core.article_edits import BadField
    from core.article_edits import save_seo as save
    from core.qa_changes import Stale

    art = get_article(slug)
    faq = [{"question": q, "answer": a} for q, a in zip(request.form.getlist("faq_q"), request.form.getlist("faq_a"))]
    typed = {"meta_title": request.form.get("meta_title", ""), "meta_description": request.form.get("meta_description", ""),
             "faq": faq, "slug": request.form.get("slug"), "focus_keyword": request.form.get("focus_keyword")}
    try:
        save(art, request.form.get("version", ""), meta_title=typed["meta_title"],
             meta_description=typed["meta_description"], faq=faq,
             slug=typed["slug"] if "slug" in request.form else None,
             focus_keyword=typed["focus_keyword"] if "focus_keyword" in request.form else None)
    except (BadField, Stale, store.Busy) as e:
        why = ("the article changed after you opened this tab (a review, the editor or another tab): your "
               "fields are below; Save again puts them over what is there now" if isinstance(e, Stale)
               else scrub_mixed(str(e)))
        pr = prices()
        ctx = article_page(art, M.page_meta(art, pr), pr, art.version(), tab="seo",
                           seo_typed={k: v for k, v in typed.items() if v is not None}, seo_refused=f"Not saved: {why}.")
        return render_template("article_page.html", **ctx)       # the form again, with why: nothing saved
    flash("Saved, as a version of its own.")
    return redirect(url_for("articles.article", slug=slug, tab="seo"))


@bp.route("/api/preview", methods=["POST"])
def preview():
    j = request.get_json(silent=True) or {}
    slug = j.get("slug", "")
    html = md_render(j.get("md", ""))
    if slug and store.listed_slug(slug):
        html = M.versioned_assets(html, slug, get_article(slug).dir)
    return jsonify({"html": html})


@bp.route("/a/<slug>/delete", methods=["POST"])
def delete_article(slug):
    """Move an article to `_trash`, never rm: a generated article costs real money in research
    and image calls, and one mis-click should not destroy that. WordPress is untouched."""
    art = get_article(slug)
    trash = out() / "_trash"
    trash.mkdir(exist_ok=True)
    dest = trash / f"{slug}__{time.strftime('%Y%m%d-%H%M%S')}"
    wp = (store.read_json_lenient(art.path("_wp.json"), {}) or {}).get("post_id")
    try:
        # under the article's lock: a job that starts after this finds the folder gone
        with art.lock(wait=False, holder="Delete"):
            shutil.move(str(art.dir), str(dest))
    except store.Busy as e:
        flash(f"Not deleted: a job is working on “{slug}” ({e.holder or 'another job'}).")
        return redirect(url_for("articles.article", slug=slug))
    except OSError as e:
        flash(f"Could not delete {slug}: {scrub_mixed(str(e))}")
        return redirect(url_for("articles.dashboard"))
    msg = f"Moved “{slug}” to the trash (_trash/{dest.name}) — it can be restored from there."
    if wp:
        msg += f" WordPress post #{wp} is untouched."
    flash(msg)
    return redirect(url_for("articles.dashboard"))


@bp.route("/a/<slug>/assets/<path:fname>")
def assets(slug, fname):
    art = get_article(slug)
    if not art.assets.is_dir():
        abort(404)
    # no-cache: the browser asks again every time and gets a 304 while the file is the same. A
    # picture redrawn or re-captured keeps its name, and the default four-hour cache showed the
    # old one for hours after the fix had landed ("the screenshots never change", 2026-10-01)
    resp = send_from_directory(art.assets, fname, max_age=0)
    resp.cache_control.no_cache = True
    return resp


@bp.route("/a/<slug>/_kept/<gid>/<path:fname>")
def kept_picture(slug, gid, fname):
    """A picture as it was before a change made it again: the change group's kept copy, shown
    beside the new one on its card. Only pictures, only from that group's copies."""
    from flask import send_file

    from core.qa_changes import KEPT

    art = get_article(slug)
    try:
        f = art.own_file(f"{KEPT}/{gid}/{fname}", under=(f"{KEPT}/{gid}/assets/",))
    except store.UnsafePath:
        abort(404)
    if not f.is_file():
        abort(404)
    resp = send_file(f, max_age=0)
    resp.cache_control.no_cache = True
    return resp


@bp.route("/a/<slug>/export")
def export_article(slug):
    """The article folder as a zip, to import on another install (core/transfer.py)."""
    from core.transfer import export_article as pack

    art = get_article(slug)
    try:
        data = pack(art)
    except store.Busy as e:
        flash(f"Not exported: {scrub_mixed(str(e))}.")
        return redirect(url_for("articles.article", slug=slug))
    return Response(data, mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{art.slug}.zip"'})


@bp.route("/import", methods=["POST"])
def import_article():
    """An exported article, unpacked into a new folder: never over an existing article."""
    from core.transfer import BadArchive
    from core.transfer import import_article as unpack

    f = request.files.get("archive")
    if not f or not f.filename:
        flash("Choose an exported article (.zip) to import.")
        return redirect(url_for("articles.dashboard"))
    try:
        art = unpack(repo(), f.read())
    except BadArchive as e:
        flash(f"Not imported: {scrub_mixed(str(e))}")
        return redirect(url_for("articles.dashboard"))
    flash(f"Imported as “{art.slug}”.")
    return redirect(url_for("articles.article", slug=art.slug))


@bp.route("/a/<slug>/keywords", methods=["POST"])
def keywords(slug):
    """The keyword coverage report counted again: no model or network call, so inline."""
    from core.keyword_report import build as build_keyword_report

    art = get_article(slug)
    try:
        data = build_keyword_report(art.dir, project().seo)
        p = data["primary"] or {}
        flash(f"Keyword report: {data['failed']} failed, {data['warned']} to look at. The primary keyword "
              f"'{p.get('keyword')}' appears {p.get('total')} times, {p.get('density')}% of the words.")
    except Exception as e:  # noqa: BLE001
        flash(f"The keyword report could not be built: {scrub_mixed(str(e))}")
    return redirect(url_for("articles.article", slug=slug, tab="seo"))

