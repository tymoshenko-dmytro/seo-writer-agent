"""Sending an article to WordPress, and Accept (core/publication.py).

Every form carries the version the page showed (and a send the post it named, Accept the action
its label promised); under the article's lock a different one changes nothing and the person
looks at the page again. Nothing here is blocked by the review's findings or the score."""
from __future__ import annotations

import os

from flask import Blueprint, flash, redirect, request, url_for

from core import store, usage

from ..context import article as get_article
from ..privacy import scrub, scrub_mixed

bp = Blueprint("publishing", __name__)
WHO = "studio-operator"


def _describe(res: dict) -> str:
    """A send's toast: its detail and warnings are diagnostics, shown without the server's paths
    (review RC77-06); the full text stays in the article's record."""
    post = f" post #{res['post_id']}" if res.get("post_id") else ""
    if res["outcome"] == "sent":
        warn = f" Not confirmed: {'; '.join(scrub(str(w)) for w in res['warnings'])}" if res.get("warnings") else ""
        return f"Sent to WordPress:{post} ({res.get('status')}).{warn}"
    if res["outcome"] == "unknown":
        return f"Unknown outcome:{post} {scrub(res.get('detail'))}"
    return f"Not sent: {scrub(res.get('detail'))}"


def _act(slug, fn):
    from core.qa_changes import Stale

    get_article(slug)
    try:
        flash(fn())
    except Stale as e:
        flash(f"Nothing done: {scrub_mixed(str(e))}")
    except store.Busy as e:
        flash(f"Nothing done: {scrub_mixed(str(e))}.")
    except Exception as e:  # noqa: BLE001
        flash(f"Nothing done: {scrub_mixed(str(e))}")
    return redirect(url_for("articles.article", slug=slug))


@bp.route("/a/<slug>/publish", methods=["POST"])
def publish(slug):
    """Send the version shown, with the action its button named, to the post the page named."""
    from core import publication

    return _act(slug, lambda: _describe(publication.send(
        slug, request.form.get("version", ""), request.form.get("action", "draft"),
        request.form.get("post") or None, who=WHO)))


@bp.route("/a/<slug>/accept", methods=["POST"])
def accept(slug):
    """The person's mark on the version shown, and the action its label promised."""
    from core import publication

    def run():
        res = publication.accept(slug, request.form.get("version", ""), request.form.get("action") or None, WHO,
                                 post=request.form.get("post") or None)
        return "Accepted this version." + (f" {_describe(res['send'])}" if res.get("send") else "")

    return _act(slug, run)


@bp.route("/a/<slug>/check-post", methods=["POST"])
def check_post(slug):
    """Read the post's status from WordPress now; changes nothing there."""
    from core import publication

    def run():
        res = publication.check_post(slug)
        if res["outcome"] != "read":
            return f"Could not read the post: {scrub(res['detail'])}"
        return f"WordPress says the post is {res['status']} (modified {res.get('modified') or 'at an unknown time'})."

    return _act(slug, run)


@bp.route("/a/<slug>/uniqueness", methods=["POST"])
def uniqueness(slug):
    """The Winston uniqueness check, as a job: tens of seconds on the wire, about 2 credits a
    word. Its result is one of Publish's checks."""
    from ..context import CONFIG, project
    from ..jobs import runner

    art = get_article(slug)

    def run(cb):
        from core.plagiarism_report import build as build_uniqueness

        with art.lock(holder="uniqueness check"), usage.recording(art.dir, "uniqueness", config=CONFIG), \
                usage.stage("uniqueness"):
            from core import versions

            if art.path("article.md").is_file():
                versions.ensure(art)            # the text it reads is a version it can name (R9D2-04)
            cb("sending the text to Winston")
            data = build_uniqueness(art.dir, project().seo)
        rewrite = len([p for p in (data.get("passages") or []) if not p.get("cited")])
        return (f"{data['uniqueness']}% unique · {data['plagiarised_words']} of {data['total_words']} words "
                f"matched in {data['source_count']} source(s) · {rewrite} passage(s) to rewrite · "
                f"{data['credits_used']} credits")

    if not os.getenv("WINSTON_API_KEY"):
        flash("Not checked: the uniqueness check needs the Winston key (Connections).")
    elif runner.active("uniqueness", slug) or art.busy():
        flash("Not checked: a job is already working on this article.")
    else:
        runner.submit_task(f"Uniqueness · {slug}", run, kind="uniqueness", slug=slug)
        flash("Checking the uniqueness in the background: Publish shows the result when it is done.")
    return redirect(url_for("articles.article", slug=slug, tab="publish"))

