"""The install itself: health, the job list, the link index, starting a new article, the
project's logo and the design reference."""
from __future__ import annotations

import os
import time
from pathlib import Path

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, send_file, url_for

from ..context import out, project, repo
from ..jobs import JOBS_FILE, runner
from ..privacy import scrub_job

bp = Blueprint("system", __name__)
BOOT = time.time()


@bp.route("/healthz")
def healthz():
    """Liveness probe, plus enough identity to tell a crash from a second replica: the in-memory
    job queue is only correct with exactly one process."""
    def cgroup(name: str):
        for base in ("/sys/fs/cgroup", "/sys/fs/cgroup/memory"):
            try:
                return (Path(base) / name).read_text().strip()
            except OSError:
                continue
        return None

    mem = {"limit": cgroup("memory.max") or cgroup("memory.limit_in_bytes"),
           "current": cgroup("memory.current") or cgroup("memory.usage_in_bytes"),
           "peak": cgroup("memory.peak"),
           "oom": (cgroup("memory.events") or "").replace("\n", " ")}
    return {"ok": True, "pid": os.getpid(), "boot": round(BOOT, 3),
            "uptime_s": round(time.time() - BOOT, 1),
            "jobs_file_exists": JOBS_FILE.exists(), "mem": mem}      # never the path (RD4B-01)


@bp.route("/api/job/<jid>")
def api_job(jid):
    j = runner.get(jid)
    if not j:
        return jsonify({"error": "unknown job"}), 404
    j = scrub_job(j)        # the record keeps the full error; the page never shows a server path
    return jsonify({"status": j["status"], "step": j.get("step"), "slug": j.get("slug"),
                    "result": j.get("result"), "error": j.get("error")})


@bp.route("/api/jobs")
def jobs_api():
    """The ten newest jobs, and every job still queued or running however old: the sidebar
    counts and names the whole queue from it (review R734-01)."""
    jobs = runner.all()
    return jsonify([scrub_job(j) for j in jobs[:10] + [j for j in jobs[10:] if j.get("status") in ("queued", "running")]])


@bp.route("/links/refresh", methods=["POST"])
def refresh_links():
    """Queue the rebuild instead of doing it inline: the sitemaps plus ~30 pages take ~20 s, and
    behind the ingress a synchronous handler returns a bare 502."""
    def build(cb):
        from core.tools.link_index import LinkIndex

        p = project()
        cb("fetching sitemaps")
        idx = LinkIndex.build(p.links, log=cb)
        idx.save(p.root)
        return f"{len(idx.targets)} pages indexed"

    if runner.active("links"):
        flash("A link-index rebuild is already running.")
    else:
        runner.submit_task("Internal link index", build, kind="links")
        flash("Rebuilding the link index in the background — this page updates when it finishes.")
    return redirect(url_for("articles.dashboard"))


@bp.route("/new")
def new():
    """New article: the composer, what a run is likely to cost and take here, the last runs."""
    from core import agent_door, setup
    from core import settings as user_settings

    from .. import article_view as V
    from .common import prices

    p = project()
    pr = prices()
    cost = agent_door.estimate(out(), "generation", pr)
    took = agent_door.run_minutes(out())
    low, high = cost["usd"] * 0.8, cost["usd"] * 1.25
    estimate = (f"About ${low:.0f}–{high:.0f}" + (f" and {took['minutes']} minutes" if took["minutes"] else " and 15–20 minutes")
                + (f", from {cost['basis'].removeprefix('the average of ')} here." if "average" in cost["basis"] else ", a first estimate."))
    jobs = runner.all()
    rows = []
    for art in repo().articles():
        try:
            rows.append(V.list_row(art, pr, jobs))
        except Exception:  # noqa: BLE001 — a broken folder is not a last run worth showing
            continue
    # the latest attempt first, written or not (RD41-04): the newest of the article, the run's
    # record and its progress
    def last_touched(slug: str) -> float:
        d = out() / slug
        return max((p.stat().st_mtime for p in (d / "article.md", d / "_run.json", d / "_progress.json") if p.is_file()),
                   default=0.0)
    rows.sort(key=lambda r: last_touched(r["slug"]), reverse=True)
    st = setup.state(out())
    return render_template("new.html", default_processor=p.defaults.get("deep_research_processor", "core"),
                           countries=user_settings.COUNTRIES, country=(p.seo or {}).get("country", "us"),
                           estimate=estimate, runs=rows[:5], can_write=st["can_write"], reason=st["reason"])


@bp.route("/run", methods=["POST"])
def run():
    """Start a run: the topic and its options (a job; the page of its folder follows it)."""
    from core import setup
    from core import settings as user_settings

    topic = " ".join((request.form.get("topic") or "").split())
    if not topic:
        flash("Nothing started: give a topic.")
        return redirect(url_for("system.new"))
    refused = setup.ready_to_write(out())
    if refused:
        flash(f"Nothing started. {refused} See Connections.")
        return redirect(url_for("system.new"))
    words = request.form.get("words") or ""
    country = request.form.get("country") or ""
    if country and country not in {c for c, _ in user_settings.COUNTRIES}:
        country = ""
    processor = request.form.get("processor") or None
    if processor not in (None, "lite", "core", "pro", "ultra"):
        processor = None
    runner.submit(topic, use_ahrefs=request.form.get("use_ahrefs") == "on", processor=processor,
                  target_words=int(words) if words.isdigit() and 300 <= int(words) <= 8000 else None,
                  country=country or None)
    flash(f"Started “{topic}”: the article's page follows it.")
    return redirect(url_for("articles.dashboard"))


@bp.route("/brand/logo")
def brand_logo():
    """The project's logo, for the sidebar and the login card (so it is served before signing
    in: it is the logo the project's site shows anyway)."""
    from ..shell import logo_file

    f = logo_file(project())
    if f is None:
        abort(404)
    return send_file(f, max_age=3600)


@bp.route("/sample", methods=["POST"])
def sample():
    """The sample article among the articles (once), and its page."""
    from core import sample as S

    try:
        slug = S.add(out())
    except (FileNotFoundError, OSError, ValueError) as e:
        flash(f"The sample could not be added: {e}.")
        return redirect(url_for("articles.dashboard"))
    return redirect(url_for("articles.article", slug=slug))


@bp.route("/design")
def design():
    """Every component of the interface with sample data: the reference the screens are built
    from, and what its tests and screenshots look at."""
    return render_template("design.html")
