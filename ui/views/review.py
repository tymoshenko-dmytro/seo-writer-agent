"""The article page's actions in the QA model: Review again, a proposal applied (one, or the ticked
ones), Try again, Not important, Revert, the person's own paragraph, the versions (view, compare,
restore), a picture removed or redrawn.

Every form carries the version the page showed; the engine checks it under the article's lock,
so a decision taken on an old page changes nothing. A model call runs as a job; the page follows
it and shows its outcome. The same functions answer `cli.py` (ui/commands.py)."""
from __future__ import annotations

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for

from core import store, versions
from core import qa_changes as qc

from .. import article_view as V
from ..context import CONFIG, out, project
from ..context import article as get_article
from ..jobs import runner
from ..privacy import scrub_mixed

bp = Blueprint("review", __name__)
WHO = "studio-operator"


def _from_tab() -> str:
    """The tab a decision was made on: Pictures offers Review's decisions on a picture too."""
    return "pictures" if request.form.get("back") == "pictures" else "review"


def _back(slug, tab="review", anchor=""):
    return redirect(url_for("articles.article", slug=slug, tab=tab) + (f"#{anchor}" if anchor else ""))


def _now_or_refused(slug, fn, done: str, tab="review", anchor="", queue: dict | None = None):
    """An action that runs now: its outcome, or the engine's refusal, as the toast. While jobs
    work on the article, an action that can wait (`queue`: its label, and the findings or the
    picture it answers) waits behind them instead of being refused, and runs on what they leave:
    "Not important" or a removal during a Redraw said "busy" (2026-10-06). `fn` must not read
    the request: it may run in the worker."""
    if queue is not None and runner.on_article(slug):
        queued = runner.on_article(slug)
        if any((queue.get("asset") and j.get("asset") == queue["asset"])
               or set(queue.get("findings") or ()) & set(j.get("findings") or ()) for j in queued):
            flash("Nothing done: it is already being made; the page shows it when it is ready.")
            return _back(slug, tab, anchor)

        def run(cb):
            fn()
            return {"step": done}

        runner.submit_on_article(get_article(slug), f"{queue['label']} · {slug}", run, "edit",
                                 request.form.get("version", ""), asset=queue.get("asset"),
                                 findings=queue.get("findings"))
        flash("In the queue: it happens after what is being made now; the page follows it.")
        return _back(slug, tab, anchor)
    try:
        fn()
        flash(done)
    except qc.Stale as e:
        flash(f"Nothing done: {scrub_mixed(str(e))}")
    except store.Busy as e:
        flash(f"Nothing done: {scrub_mixed(str(e))}.")
    except qc.Refused as e:
        flash(f"Nothing done: {scrub_mixed(str(e))}")
    except KeyError as e:
        flash(f"Nothing done: {scrub_mixed(str(e).strip(chr(39)))}")
    except ValueError as e:
        flash(f"Nothing done: {scrub_mixed(str(e))}")
    return _back(slug, tab, anchor)


def _job(slug, label: str, kind: str, fn, started: str, needs: tuple[str, ...] | None = None,
         tab: str = "review", asset: str | None = None, anchor: str | None = None,
         version: str | None = None, findings: list[str] | None = None):
    """A model call as a job, only with the keys it `needs` (the writing model's, unless told
    otherwise). Jobs on one article queue: this one runs after those already working on it, on
    the version they leave (JobRunner.submit_on_article). The page stays on `tab`: a Redraw on
    Pictures went to Review, and nothing said what was happening (2026-10-06)."""
    from .common import AI, missing_keys, needs_sentence

    art = get_article(slug)
    missing = missing_keys(*((AI,) if needs is None else needs))      # () is "none": a removal is free
    anchor = anchor or (f"pic-{asset.rsplit('/', 1)[-1]}" if asset else "")
    if missing:
        flash(needs_sentence(missing))
        return _back(slug, tab, anchor)
    queued = runner.on_article(slug)
    if art.busy() and not queued:
        # held by something that is not one of the Studio's jobs (the CLI, an agent)
        flash(f"Nothing done: a job is already working on this article ({scrub_mixed(str(art.busy()))}); "
              "try again when it ends.")
        return _back(slug, tab, anchor)
    if any((asset and j.get("asset") == asset) or set(findings or ()) & set(j.get("findings") or ()) for j in queued):
        flash("Nothing done: it is already being made; the page shows it when it is ready.")
        return _back(slug, tab, anchor)
    runner.submit_on_article(art, label, fn, kind, version or "", asset=asset, findings=findings)
    flash(started if not queued else f"{started.split(':')[0].rstrip('.')}: in the queue after what is being made now.")
    return _back(slug, tab, anchor)


# ---------------------------------------------------------------------------------------------
# the review's queue
# ---------------------------------------------------------------------------------------------
@bp.route("/a/<slug>/review-again", methods=["POST"])
def review_again(slug):
    """A new review of the version shown, with its safe changes (a model call: a job)."""
    from core.qa_review import review

    version, root = request.form.get("version", ""), out()        # the job runs outside the request

    def run(cb):
        cb("reviewing")
        review(slug, config=CONFIG, project=project(), version=version)
        from core import agent_door

        return {"step": agent_door.review_summary(store.Article(root, slug))}

    return _job(slug, f"Review · {slug}", "review", run, "Reviewing in the background: the page follows it.",
                version=version)


def _needs_of(slug, ids) -> tuple[str, ...]:
    """The keys the named findings' changes need together (an unknown id needs the writing
    model's: the engine refuses it anyway, and the page never offers it)."""
    from .common import AI, finding_needs

    try:
        rv = qc.records(get_article(slug))[1]
    except Exception:  # noqa: BLE001 — an unreadable record: the engine says why when it runs
        return (AI,)
    found = [qc.finding_by_id(rv, i) for i in ids]
    folder = get_article(slug).dir
    return tuple(dict.fromkeys(k for f in found for k in (finding_needs(f, folder) if f else (AI,))))


def _said(root, slug: str) -> dict:
    """Why the machine did not make each change, as its card says it: the plain sentence, never a
    model's or a tool's own words, which stay in the record and the agent's view (R058-08)."""
    from core import agent_door

    return {it["id"]: it.get("why") for it in agent_door.review_view(store.Article(root, slug)).get("needs_you") or []}


@bp.route("/a/<slug>/apply", methods=["POST"])
def apply_proposals(slug):
    """The proposals named (one, or the ticked ones), made by the machine (a job)."""
    ids = [i for i in request.form.getlist("ids") if i]
    if not ids:
        flash("Nothing done: tick a proposal first.")
        return _back(slug)
    version, review_id = request.form.get("version", ""), request.form.get("review", "")
    root = out()                                    # the job runs outside the request

    def run(cb):
        cb(f"making {len(ids)} change(s)")
        rep = qc.apply_proposals(slug, ids, version, review_id, config=CONFIG, project=project())
        missed = rep.get("not_landed") or {}
        said = _said(root, slug) if missed else {}
        return {"step": f"{len(rep.get('landed') or [])} made" + (f", {len(missed)} not made: "
                        + "; ".join(f"{k}: {said.get(k) or 'not made'}" for k in missed) if missed else "")}

    n = len(ids)
    return _job(slug, f"Proposals · {slug}", "proposals", run,
                f"Making the {n} proposal{'s' if n > 1 else ''} in the background.", needs=_needs_of(slug, ids),
                version=version, findings=ids, anchor=request.form.get("anchor") or None)


@bp.route("/a/<slug>/try-again/<fid>", methods=["POST"])
def try_again(slug, fid):
    """A safe change that did not land, tried once more (a job)."""
    version, root = request.form.get("version", ""), out()        # the job runs outside the request

    def run(cb):
        cb("trying again")
        rep = qc.fix_again(slug, fid, version, config=CONFIG, project=project())
        return {"step": "made" if rep.get("landed") else f"not made: {_said(root, slug).get(fid) or 'no change landed'}"}

    return _job(slug, f"Try again · {slug}", "fix", run, "Trying it again in the background.",
                needs=_needs_of(slug, [fid]), version=version, findings=[fid], anchor=request.form.get("anchor") or None)


@bp.route("/a/<slug>/not-important/<fid>", methods=["POST"])
def not_important(slug, fid):
    why = " ".join((request.form.get("why") or "").split())[:300]       # optional: one click closes it
    version, proj = request.form.get("version", ""), project()
    return _now_or_refused(slug, lambda: qc.not_important(slug, fid, version, WHO, why, config=CONFIG, project=proj),
                           "Closed as not important.", tab=_from_tab(), anchor=request.form.get("anchor", ""),
                           queue={"label": "Not important", "findings": [fid]})


@bp.route("/a/<slug>/revert/<gid>", methods=["POST"])
def revert(slug, gid):
    version, proj = request.form.get("version", ""), project()
    return _now_or_refused(slug, lambda: qc.revert(slug, gid, version, config=CONFIG, project=proj, who=WHO),
                           "Reverted: the article is as it was before that change (a version of its own).",
                           queue={"label": "Revert", "findings": [gid]})


@bp.route("/a/<slug>/keep/<gid>", methods=["POST"])
def keep_as_is(slug, gid):
    """A change the check says left its problem in place, kept as it is: it leaves Needs you."""
    version, proj = request.form.get("version", ""), project()
    return _now_or_refused(slug, lambda: qc.keep_as_is(slug, gid, version, WHO, config=CONFIG, project=proj),
                           "Kept as it is.", tab=_from_tab(), anchor=request.form.get("anchor", ""),
                           queue={"label": "Keep it as it is", "findings": [gid]})


@bp.route("/a/<slug>/paragraph", methods=["POST"])
def paragraph(slug):
    """The person's own words for one paragraph, from a review card."""
    def norm(v):
        return (v or "").replace("\r\n", "\n").replace("\r", "\n")

    before, after = norm(request.form.get("before")), norm(request.form.get("after"))
    if before.strip() == after.strip():
        flash("Nothing changed in the paragraph.")
        return _back(slug, anchor=request.form.get("anchor", ""))
    version, fid, proj = request.form.get("version", ""), request.form.get("finding") or None, project()
    return _now_or_refused(slug, lambda: qc.edit_block(slug, version, before, after, fid=fid, who=WHO,
                                                       config=CONFIG, project=proj),
                           "Saved: your paragraph is a new version.", anchor=request.form.get("anchor", ""),
                           queue={"label": "Your paragraph", "findings": [fid] if fid else []})


# ---------------------------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------------------------
@bp.route("/a/<slug>/v/<int:n>")
def version(slug, n):
    """One version as it read, and what changed from it to now."""
    art = get_article(slug)
    try:
        view = V.version_view(art, n)
    except (KeyError, versions.Damaged, UnicodeDecodeError):
        abort(404)
    fm, _, _ = art.read()
    return render_template("version.html", slug=slug, title=fm.get("title") or slug, view=view,
                           version=art.version(), current=versions.listing(art)[0]["n"] if versions.index(art) else None)


@bp.route("/a/<slug>/v/<int:n>/<path:ref>")
def version_picture(slug, n, ref):
    """A picture as a version kept it (its bytes by hash)."""
    import io
    import mimetypes

    art = get_article(slug)
    try:
        sha = (versions.get(art, n).get("files") or {})[ref]
        data = versions.blob(art, sha)
    except (KeyError, versions.Damaged):
        abort(404)
    return send_file(io.BytesIO(data), mimetype=mimetypes.guess_type(ref)[0] or "application/octet-stream",
                     max_age=3600)


@bp.route("/a/<slug>/v/<int:n>/restore", methods=["POST"])
def restore(slug, n):
    art = get_article(slug)
    done = f"Restored v{n} as the newest version. It is not an acceptance or a send."
    if request.form.get("back") == "edit":            # from the editor's Versions panel: back to it
        _now_or_refused(slug, lambda: versions.restore(art, n, request.form.get("version", ""), WHO), done)
        return redirect(url_for("articles.edit", slug=slug))
    return _now_or_refused(slug, lambda: versions.restore(art, n, request.form.get("version", ""), WHO), done,
                           tab="history")


# ---------------------------------------------------------------------------------------------
# pictures
# ---------------------------------------------------------------------------------------------
def _from_where() -> tuple[str, str]:
    """Pictures' actions are pressed on Pictures, or on a Review card that offers them: the page
    goes back where the button was."""
    if request.form.get("back") == "review":
        return "review", "".join(ch for ch in request.form.get("anchor", "") if ch.isalnum() or ch == "-")
    return "pictures", ""


@bp.route("/a/<slug>/pictures/remove", methods=["POST"])
def remove_picture(slug):
    """A picture taken out of the text by the person, with its caption (a version)."""
    asset = request.form.get("asset", "")
    tab, anchor = _from_where()
    version, proj = request.form.get("version", ""), project()
    return _now_or_refused(slug, lambda: qc.remove_picture_by_person(slug, version, asset, who=WHO, config=CONFIG,
                                                                     project=proj),
                           f"Removed {asset} from the article; the file stays in the versions.", tab=tab, anchor=anchor,
                           queue={"label": "Remove the picture", "asset": asset})


@bp.route("/a/<slug>/pictures/upload", methods=["POST"])
def upload_picture(slug):
    """The person's own picture in place of one the article shows (core/article_edits.upload_picture):
    free, now, a version of its own."""
    from core.article_edits import UPLOAD_MAX, upload_picture as upload

    asset = request.form.get("asset", "")
    f = request.files.get("file")
    data = f.read(UPLOAD_MAX + 1) if f else b""
    version, art = request.form.get("version", ""), get_article(slug)     # never in the worker (RE99-01)
    return _now_or_refused(slug, lambda: upload(art, version, asset, data, who=WHO),
                           f"Your picture is in place of {asset}; the old one stays in the versions.", tab="pictures",
                           queue={"label": "Your picture", "asset": asset})


_REDO_STEP = {None: "making the picture again", "auto": "making the picture again (the producer picks its kind)",
              "screenshot": "making it again as a screenshot", "infographic": "making it again as an infographic",
              "chart": "making it again as a chart", "image": "drawing the illustration"}


@bp.route("/a/<slug>/pictures/redraw", methods=["POST"])
def redraw_picture(slug):
    """A picture made again (a chart or an infographic redrawn, a screenshot re-captured;
    core/article_edits.redraw): a job, a version of its own."""
    from core.article_edits import redraw

    rel = request.form.get("asset", "")
    tab, anchor = _from_where()
    if store.local_asset(rel) != rel:
        flash("Nothing done: that is not one of the article's pictures.")
        return _back(slug, tab, anchor)
    art = get_article(slug)
    feedback = " ".join((request.form.get("feedback") or "").split())[:500]
    # one Redo (the producer chooses the kind) or a kind the person picked (the operator, 2026-10-07)
    kind = request.form.get("kind") or ("image" if request.form.get("as_image") == "1" else None)
    if kind not in (None, "auto", "screenshot", "infographic", "chart", "image"):
        flash("Nothing done: that is not a kind of picture the Studio makes.")
        return _back(slug, tab, anchor)
    version = request.form.get("version", "")
    if not version or version != art.version():
        flash("Nothing done: the article changed after the page was opened: look at it again.")
        return _back(slug, tab, anchor)
    rv = qc.records(art)[1]
    answers = [f["id"] for f in rv.get("findings") or [] if f.get("status") == "open"
               and (f.get("target") or {}).get("asset") == rel and f.get("review") == (qc.latest_review(rv) or {}).get("id")]

    def run(cb):
        cb(_REDO_STEP[kind])
        redraw(art, version, rel, CONFIG, project(), feedback=feedback, who=WHO, kind=kind)  # checked again when it runs
        return {"step": f"{rel} made again"}

    from .common import AI, IMAGES, picture_needs

    return _job(slug, f"Picture · {slug}", "visual", run,
                f"Making {rel} again: the picture shows when it is ready (the page refreshes by itself).",
                needs=picture_needs(rel, art.dir, kind=kind), tab=tab, asset=rel, anchor=anchor or None,
                version=version, findings=answers)
