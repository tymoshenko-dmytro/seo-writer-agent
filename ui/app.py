"""SEO Writer Agent — the web UI over the article pipeline: dashboard, article pages, the editor,
the QA and publishing actions, settings, costs.

Run:  .venv/bin/python ui/app.py           (http://localhost:5050)
Production: gunicorn ui.app:app, one worker (the job queue lives in the process).
Team access: set UI_PASSWORD (and UI_SECRET) and expose via the ingress, Tailscale or cloudflared;
the password can then be changed or turned off in Configure > Access (core/access.py).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")

from flask import Flask  # noqa: E402

from core import api_keys  # noqa: E402
from core.store import output_root  # noqa: E402

from ui import auth  # noqa: E402
from ui.context import CONFIG  # noqa: E402
from ui import privacy  # noqa: E402
from ui.diff import word_diff  # noqa: E402
from ui.icons import icon  # noqa: E402


def usd(v) -> str:
    """$1,234.56 / <$0.01 / — (unknown)."""
    if v is None:
        return "—"
    v = float(v)
    if 0 < v < 0.01:
        return "<$0.01"
    return f"${v:,.2f}"


def units(u: dict) -> str:
    """A service's own units, for what has no price: '12,400 units', '7,000 credits'."""
    names = {"units": ("unit", "units"), "credits": ("credit", "credits"),
             "input_tokens": ("input token", "input tokens"), "output_tokens": ("output token", "output tokens"),
             "images": ("image", "images"), "runs": ("research run", "research runs"),
             "requests": ("search", "searches")}
    parts = [f"{int(v):,} {names[k][0 if int(v) == 1 else 1]}" for k, v in (u or {}).items() if k in names and v]
    return ", ".join(parts) or "—"


def create_app(output_dir: Path | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    root = Path(output_dir) if output_dir is not None else output_root(CONFIG)
    app.config["OUTPUT_ROOT"] = root
    app.secret_key = auth.session_secret(root)
    from core import access

    if access.reset_if_asked(root):         # UI_ACCESS_RESET: a team locked out gets UI_PASSWORD back
        app.logger.warning("Access: the password choice saved in the Studio was reset (UI_ACCESS_RESET)")
    # the session cookie is not sent with a form another site posts; UI_COOKIE_SECURE=1 on an
    # install served over https keeps it off plain http too
    app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SECURE=os.getenv("UI_COOKIE_SECURE", "").strip() == "1",
                      MAX_CONTENT_LENGTH=450 * 1024 * 1024)      # an imported article with its pictures
    app.before_request(auth.guard)
    app.add_template_filter(usd, "usd")
    app.add_template_filter(units, "units")

    @app.context_processor
    def _globals():
        """Static files fingerprinted by mtime (Cloudflare and the browser cache /static hard;
        after a redesign new HTML arrived with the old stylesheet), the pill tooltips, and the
        shell: the product's and the project's names, the navigation, the jobs and the spend."""
        def asset_v(name: str) -> int:
            try:
                return int((Path(app.static_folder) / name).stat().st_mtime)
            except OSError:
                return 0

        from ui.context import project, repo
        from ui.jobs import runner
        from ui.shell import Shell
        from ui.views.common import prices

        return {"asset_v": asset_v, "shell": Shell(project, runner, repo, prices)}

    app.add_template_global(icon, "icon")
    app.add_template_filter(word_diff, "word_diff")
    app.add_template_filter(privacy.scrub, "scrub")
    app.add_template_filter(privacy.scrub_mixed, "scrub_mixed")

    from ui.views import articles, costs, publishing, review, settings, system, visuals

    for bp in (auth.bp, articles.bp, review.bp, visuals.bp, publishing.bp, settings.bp, costs.bp, system.bp):
        app.register_blueprint(bp)
    api_keys.apply(root)     # keys saved in the Studio are in force for this process from the start
    _migrate_at_start(app, root)
    return app


def _migrate_at_start(app, root: Path) -> None:
    """Every article into the QA model once, when the Studio starts (design step 2), with a
    summary in the log. A failure here never stops the Studio: an article it could not migrate
    is migrated by its first review."""
    from core.qa_migration import migrate_install

    try:
        rep = migrate_install(root)
    except Exception as e:  # noqa: BLE001
        app.logger.warning("QA migration at start did not run: %s", e)
        return
    if rep["migrated"] or rep["skipped"]:
        app.logger.warning("QA migration at start: %d migrated (%s), %d already, %d skipped %s",
                           len(rep["migrated"]), ", ".join(rep["migrated"]), rep["already"],
                           len(rep["skipped"]), rep["skipped"] or "")


app = create_app()


if __name__ == "__main__":
    # running directly = someone is working on the Studio, so pick up template edits without a
    # restart (gunicorn imports `app` and never reaches this branch)
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    port = int(os.getenv("UI_PORT") or os.getenv("PORT") or 5050)
    app.run(host=os.getenv("UI_HOST", "127.0.0.1"), port=port, debug=False)
