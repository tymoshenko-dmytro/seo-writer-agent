"""Who may use the Studio, and from where.

One rule for every page and action (RF-02): with a team password (set in Configure > Access, or
UI_PASSWORD; core/access.py) every page needs a signed-in session of the password in force; with
the password turned off in Configure > Access, every page is open (the operator, 2026-10-07: the
Studio sits behind Cloudflare Access); with neither, the Studio answers only a request from this
machine, to a Studio that listens only on this machine, not relayed by a proxy or tunnel. The
Docker image listens on every address, so there it needs the password unless the team turned it
off. Before, only the settings pages had the rule: generating, editing, deleting and publishing
answered anyone who could reach a server started without a password.

Every change (POST) must come from the Studio's own pages (Origin, or failing that Referer),
and the session cookie is SameSite=Lax: a form on another site cannot act on the Studio.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import urlparse

from flask import Blueprint, abort, flash, redirect, render_template, request, session, url_for

bp = Blueprint("auth", __name__)

_PROXY_HEADERS = ("X-Forwarded-For", "X-Real-IP", "Cf-Connecting-Ip", "True-Client-Ip", "Forwarded")
OPEN_ENDPOINTS = {"auth.login", "system.healthz", "system.brand_logo", "static"}


def _new_secret(p: Path) -> str:
    value = secrets.token_hex(32)
    try:
        from core.store import atomic_write_text

        atomic_write_text(p, value, mode=0o600)
    except OSError:
        pass        # a read-only volume: this process keeps its own secret (logins end on restart)
    return value


# a secret anyone could guess or has seen (a word, a name, a value once written in a repository)
# signs a cookie as well as the team's: below this length UI_SECRET is not used
MIN_SECRET = 32


def session_secret(output_root: Path) -> str:
    """UI_SECRET when it is set and long enough (MIN_SECRET characters); otherwise a random secret
    kept on the volume (`_ui_secret`, owner-only). Never a fixed fallback: a key in the code is
    public, and anyone could sign a session cookie with it and skip the team password. A short
    UI_SECRET is ignored with a warning, so an install that once set a short or published value
    keeps signing with the volume's secret."""
    env = (os.getenv("UI_SECRET") or "").strip()
    if len(env) >= MIN_SECRET:
        return env
    if env:
        import logging

        logging.getLogger(__name__).warning("UI_SECRET is shorter than %d characters: not used; the "
                                            "volume's own secret signs the sessions", MIN_SECRET)
    p = Path(output_root) / "_ui_secret"
    try:
        return p.read_text(encoding="utf-8").strip() or _new_secret(p)
    except OSError:
        return _new_secret(p)


def local_request() -> bool:
    """A request from this machine to a Studio that listens only on this machine, not relayed by a
    proxy or a tunnel running on it."""
    if os.getenv("UI_HOST", "127.0.0.1").strip().lower() not in ("127.0.0.1", "::1", "localhost"):
        return False
    return request.remote_addr in ("127.0.0.1", "::1") and not any(request.headers.get(h) for h in _PROXY_HEADERS)


def same_origin() -> bool:
    """The form was posted from the Studio's own pages: Origin (or, failing that, Referer) names
    this host, and an http page does not act on a Studio served over https (R058-11). The Studio
    does not read the proxy's scheme for anything else, so an https page of this host is ours
    whatever scheme the proxy hands the request over with."""
    src = request.headers.get("Origin") or ""
    if src in ("", "null"):
        src = request.headers.get("Referer") or ""
    u = urlparse(src)
    host = u.netloc.lower()
    ours = {request.host.lower()}
    fwd = request.headers.get("X-Forwarded-Host")
    if fwd:
        ours.add(fwd.split(",")[0].strip().lower())
    served = ((request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
              or request.scheme)
    return bool(host) and host in ours and (u.scheme.lower() == "https" or served == "http")


def safe_next(target: str | None) -> str:
    """Where to go after signing in: a path on this site only. `/login?next=https://evil/` used to
    send a freshly signed-in person to any site (RF-02)."""
    t = (target or "").strip()
    if not t.startswith("/") or t.startswith("//") or "\\" in t or urlparse(t).netloc:
        return url_for("articles.dashboard")
    return t


def guard():
    """before_request: the access rule and the same-origin rule, for every endpoint but the open
    ones."""
    if request.endpoint in OPEN_ENDPOINTS or request.endpoint is None:
        # open to read; a POST to one (the login) still comes from the Studio's own page (R058-11)
        if request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin():
            abort(403, "This was not sent from the Studio's own page.")
        return None
    from flask import current_app

    from core import access

    from .context import out

    how = access.mode(out())
    if how == "password":
        # signed in under the credentials in force: a changed, reset or rotated password signs
        # every other browser out (core/access.py, `epoch`)
        if not session.get("ok") or session.get("access") != access.epoch(out(), current_app.secret_key):
            if request.method == "GET" and not request.path.startswith("/api/"):
                return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
            abort(401, "Sign in first.")
    elif how == "local" and not local_request():
        abort(403, "Set a password (Configure > Access, or UI_PASSWORD) to use the Studio on a shared server.")
    if request.method not in ("GET", "HEAD", "OPTIONS") and not same_origin():
        abort(403, "This change was not sent from the Studio's own page.")
    return None


@bp.route("/login", methods=["GET", "POST"])
def login():
    from core import access

    from .context import out

    if access.mode(out()) == "off":                 # the password is turned off: nothing to sign in to
        return redirect(safe_next(request.args.get("next")))
    if request.method == "POST":
        from flask import current_app

        signed = access.login(out(), request.form.get("password") or "", current_app.secret_key)
        if signed:
            session["ok"], session["access"] = True, signed
            return redirect(safe_next(request.args.get("next")))
        flash("Wrong password")
    return render_template("login.html")


@bp.route("/logout", methods=["POST"])
def logout():
    """Ends this browser's session. A POST from the Studio's own page (the guard's same-origin
    rule), so a link on another site cannot sign anyone out."""
    session.clear()
    return redirect(url_for("auth.login"))
