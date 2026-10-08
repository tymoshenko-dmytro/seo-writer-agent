"""The app shell: what the sidebar and the page title
need on every page. The product's name is the same in every install; the project's name and logo
are the team's."""
from __future__ import annotations

import os
import re
import time
from functools import cached_property, lru_cache
from pathlib import Path

from flask import request, session

PRODUCT = "SEO Writer Agent"

# the sidebar: (group, [(key, label, href, icon)]); Configure has a page each (step 8)
NAV = [
    ("Work", [("articles", "Articles", "/", "file-text"),
              ("new", "New article", "/new", "square-pen")]),
    ("Insights", [("costs", "Costs", "/costs", "chart-column")]),
    ("Configure", [("project", "Project", "/settings/project", "folder"),
                   ("connections", "Connections", "/settings/keys", "plug"),
                   ("publishing", "Publishing", "/settings/publishing", "send"),
                   ("research", "Research", "/settings", "search"),
                   ("prompts", "Prompts", "/settings/prompts", "message-square-text"),
                   ("prices", "Prices", "/settings/prices", "tag"),
                   ("access", "Access", "/settings/access", "lock")]),
]
# which item a path belongs to: the first prefix that matches, most specific first ("/" only exactly)
_ACTIVE = [("/settings/project", "project"), ("/settings/keys", "connections"), ("/settings/wordpress", "connections"),
           ("/settings/publishing", "publishing"),
           ("/settings/prompts", "prompts"), ("/settings/prices", "prices"), ("/settings/access", "access"),
           ("/settings", "research"),
           ("/costs", "costs"), ("/new", "new"), ("/a/", "articles")]


def active_item(path: str) -> str | None:
    if path == "/":
        return "articles"
    for prefix, key in _ACTIVE:
        if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
            return key
    return None


def version() -> str:
    """The release, and the commit when the build recorded it (STUDIO_COMMIT)."""
    from core import __version__

    commit = (os.getenv("STUDIO_COMMIT") or "").strip()[:7]
    return f"v{__version__}" + (f" · {commit}" if commit else "")


# ---------------------------------------------------------------------------------------------
# the logo
# ---------------------------------------------------------------------------------------------
def logo_file(project) -> Path | None:
    brand = getattr(project, "brand", None)
    return brand.logo_file() if brand is not None else None


@lru_cache(maxsize=8)
def _shape(path: str, mtime: float) -> str:
    """'mark' for a squarish logo (drawn in a square beside the name), 'wordmark' for a wide one
    (drawn instead of the name: a wordmark already says it)."""
    w = h = None
    try:
        if path.lower().endswith(".svg"):
            text = Path(path).read_text(encoding="utf-8", errors="replace")[:4000]
            m = re.search(r'viewBox="\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)', text)
            if m:
                w, h = float(m.group(1)), float(m.group(2))
        else:
            from PIL import Image

            with Image.open(path) as im:
                w, h = im.size
    except Exception:  # noqa: BLE001 — an unreadable logo is drawn as a mark
        return "mark"
    return "wordmark" if w and h and w / h > 1.6 else "mark"


def logo(project) -> dict | None:
    f = logo_file(project)
    if f is None:
        return None
    try:
        mtime = f.stat().st_mtime
    except OSError:
        return None
    return {"url": f"/brand/logo?v={int(mtime)}", "shape": _shape(str(f), mtime)}


# ---------------------------------------------------------------------------------------------
# what is running, and this month's spend
# ---------------------------------------------------------------------------------------------
def jobs_line(runner) -> dict:
    """The running and queued jobs, and the one to name ("1 job · Reviewing invoice-software"):
    the running one, else the one the worker takes next (the oldest queued). ui.js makes the same
    choice when it follows /api/jobs."""
    active = [j for j in runner.all() if j.get("status") in ("queued", "running")]
    first = next((j for j in active if j.get("status") == "running"), active[-1] if active else None) or {}
    label = first.get("topic") or ""
    if first.get("kind") == "article":
        label = f"Writing “{label}”"
    return {"count": len(active), "label": label, "slug": first.get("slug"), "step": first.get("step") or ""}


_SUMMARIES: dict[str, tuple[tuple, dict]] = {}


def _stamp(p: Path) -> tuple:
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def month_spend(articles, prices: dict, month: str | None = None) -> float:
    """What the articles spent this month, as the dashboard counts it: each article's costs
    (core/usage.article_summary), by the month its operations started. A summary is kept until
    the article's cost ledger, its run record or the prices change, so the sidebar costs a few
    stat() calls a page."""
    from core import usage

    month = month or time.strftime("%Y-%m")
    price_key = repr(sorted((k, repr(v)) for k, v in (prices or {}).items()))
    total = 0.0
    for art in articles:
        key = (_stamp(art.dir / usage.USAGE_FILE), _stamp(art.dir / "_run.json"), price_key)
        hit = _SUMMARIES.get(str(art.dir))
        if hit is None or hit[0] != key:
            try:
                summary = usage.article_summary(art.dir, prices)
            except Exception:  # noqa: BLE001 — one unreadable ledger must not break every page
                summary = {"by_operation": []}
            hit = (key, summary)
            _SUMMARIES[str(art.dir)] = hit
        total += sum(op.get("cost") or 0 for op in hit[1].get("by_operation") or []
                     if str(op.get("started") or "").startswith(month))
    return total


class Shell:
    """Everything the layout reads: the names, the logo, the navigation, the foot. The job list
    and the month's spend are read only when a page draws them (the login card does not)."""

    product = PRODUCT
    nav = NAV

    def __init__(self, project, runner, repo, prices):
        self._project, self._runner, self._repo, self._prices = project, runner, repo, prices

    @cached_property
    def project(self) -> str:
        return self._project().name or PRODUCT

    @cached_property
    def initial(self) -> str:
        return self.project.strip()[:1].upper()

    @cached_property
    def logo(self) -> dict | None:
        return logo(self._project())

    @cached_property
    def active(self) -> str | None:
        return active_item(request.path) if request else None

    @cached_property
    def jobs(self) -> dict:
        return jobs_line(self._runner)

    @cached_property
    def spend(self) -> float | None:
        try:
            return month_spend(self._repo().articles(), self._prices())
        except Exception:  # noqa: BLE001 — the shell must render even when the volume does not
            return None

    @cached_property
    def signed_in(self) -> bool:
        from core import access

        return access.mode(self._repo().root) == "password" and bool(session.get("ok"))

    @cached_property
    def version(self) -> str:
        return version()
