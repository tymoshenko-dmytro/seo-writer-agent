"""The editor's model calls: rewrite a selection, insert a visual, redraw one, make the cover.
Each is a job, recorded in the article's cost ledger as a Studio edit. A job that writes into the
article folder holds the article's lock while it works, so it cannot run under a CLI fix or a
publish, and a Delete waits for it (review RV-06)."""
from __future__ import annotations

from flask import Blueprint, flash, jsonify, redirect, request, url_for

from core import store, usage, versions

from ..context import CONFIG, project
from ..context import article as get_article
from ..jobs import runner
from .common import IMAGES, missing_keys, needs_sentence, picture_needs, queue_json
from ..privacy import scrub_mixed


def _busy(art):
    holder = art.busy()
    return (jsonify({"error": f"a job is working on this article ({holder}); try again when it "
                              f"finishes"}), 409) if holder else None

bp = Blueprint("visuals", __name__)


@bp.route("/api/ai-edit", methods=["POST"])
def ai_edit():
    j = request.get_json(silent=True) or {}
    if not j.get("selection") or not j.get("instruction"):
        return jsonify({"error": "selection and instruction are required"}), 400
    art = get_article(j.get("slug", "")) if j.get("slug") else None

    def run(cb):
        from core.agents.interactive import AIEditor

        cb("rewriting")
        with usage.recording(art.dir if art else None, "ai_edit", config=CONFIG), usage.stage("studio"):
            text = AIEditor(project(), CONFIG).edit(j["selection"], j["instruction"],
                                                    j.get("before", ""), j.get("after", ""))
        return {"replacement": text, "step": "rewritten"}

    return queue_json("AI edit", "aiedit", run, slug=art.slug if art else None)


@bp.route("/api/visual-insert", methods=["POST"])
def visual_insert():
    j = request.get_json(silent=True) or {}
    art = get_article(j.get("slug", ""))
    if (busy := _busy(art)):
        return busy

    def run(cb):
        from core.agents.interactive import VisualProducerService

        cb("choosing a format")
        with art.lock(wait=False, holder="Insert visual"), \
                usage.recording(art.dir, "visual_insert", config=CONFIG), usage.stage("studio"), \
                versions.change(art, "the editor", "A picture made", kind="picture"):
            res = VisualProducerService(project(), CONFIG).create(
                art.dir, j.get("instruction", ""), j.get("before", ""), j.get("after", ""))
        if res.get("error"):
            raise RuntimeError(res["error"])
        res["step"] = "visual created"
        return res

    return queue_json("Insert visual", "visual", run, slug=art.slug)


@bp.route("/api/visual-edit", methods=["POST"])
def visual_edit():
    j = request.get_json(silent=True) or {}
    art = get_article(j.get("slug", ""))
    rel = str(j.get("path", ""))
    if not rel.startswith("assets/") or ".." in rel.split("/"):
        return jsonify({"error": "bad asset path"}), 400
    if (busy := _busy(art)):
        return busy
    # the version the editor showed: a picture is made again only on it (review RB32-02)
    version = str(j.get("version") or "")
    if not version or version != art.version():
        return jsonify({"error": "the article changed after the page was opened: save or reload, then try again"}), 409

    def run(cb):
        from core.agents.interactive import VisualProducerService
        from core.qa_changes import Stale

        cb("regenerating")
        with art.lock(wait=False, holder="Regenerate image"):
            if version != art.version():
                raise Stale("the article changed after the page was opened: save or reload, then try again")
            with usage.recording(art.dir, "visual_edit", label=rel, config=CONFIG), usage.stage("studio"), \
                    versions.change(art, "the Studio", "Picture redrawn", rel, kind="picture"):
                res = VisualProducerService(project(), CONFIG).edit_visual(art.dir, rel, j.get("feedback", ""),
                                                                          alt=j.get("alt", ""))
        if res.get("error"):
            raise RuntimeError(res["error"])
        res["step"] = "image regenerated"
        return res

    return queue_json("Regenerate image", "visual", run, slug=art.slug, needs=picture_needs(rel, art.dir))


@bp.route("/a/<slug>/cover", methods=["POST"])
def make_cover(slug):
    """A cover (the WordPress featured image) for an article that has none, or a new one: the
    pipeline's cover step on this article's headline (core/article_edits.new_cover), a job."""
    from core.article_edits import new_cover

    art = get_article(slug)
    version = request.form.get("version", "")

    def run(cb):
        cb("drawing the cover")
        new_cover(art, version, CONFIG, project(), who="studio-operator")    # checked again when it runs
        return "cover generated"

    queued = runner.on_article(slug)
    if not version or version != art.version():
        flash("Nothing done: the article changed after the page was opened: look at it again.")
    elif (missing := missing_keys(IMAGES)):
        flash(needs_sentence(missing))
    elif any(j.get("asset") == "assets/cover.png" for j in queued):
        flash("Nothing done: the cover is already being drawn.")
    elif art.busy() and not queued:
        flash("A job is already working on this article.")
    else:       # behind the jobs already working on it, on what they leave (JobRunner.submit_on_article)
        runner.submit_on_article(art, f"Cover · {slug}", run, "cover", version, asset="assets/cover.png")
        flash("Drawing the cover: it shows here when it is ready (about a minute; the page refreshes by itself)."
              if not queued else "Drawing the cover: in the queue after what is being made now.")
    return redirect(url_for("articles.article", slug=slug, tab="pictures"))


@bp.route("/a/<slug>/cleanup", methods=["POST"])
def cleanup_pictures(slug):
    """The picture rules in code, free and instant (core/cleanup.py): link the captions under our
    own screenshots, take out empty charts, separate pictures in a row; list what needs a redraw
    or a cover."""
    from core import cleanup

    art = get_article(slug)
    try:
        res = cleanup.clean_pictures(art)
    except store.Busy as e:
        flash(f"Not cleaned: {scrub_mixed(str(e))}.")
        return redirect(url_for("articles.article", slug=slug, tab="pictures"))
    flash("Pictures: " + cleanup.describe(res) + (". Each change is in History." if res["changes"] else ""))
    return redirect(url_for("articles.article", slug=slug, tab="pictures"))
