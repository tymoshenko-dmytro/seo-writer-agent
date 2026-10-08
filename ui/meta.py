"""What the article's pages show about it, read from its folder: the header's facts
(`page_meta`), the article rendered with versioned picture addresses, each step's report for
Sources, and this install's state for Connections. Nothing here writes a file."""
from __future__ import annotations

import html
import re
import time
from pathlib import Path
from urllib.parse import quote

from core import pricing, store, usage
from core.tools.markdown_render import render as md_render

_HEADING = re.compile(r"<(/?)h([1-6])\b", re.I)


def report_html(md: str) -> str:
    """A step's report rendered under the page's own headings: its h1 becomes an h3, and so on
    (one h1 per page, the article's or the run's title)."""
    return _HEADING.sub(lambda m: f"<{m[1]}h{min(int(m[2]) + 2, 6)}", md_render(md))

STAGES = [
    ("01_keyword_research.md", "Keyword research", "Ahrefs: primary keyword, clusters, SERP, competitor outlines"),
    ("02_brief.md", "SEO brief", "outline from the SERP, keywords per section, formats, research questions"),
    ("03_research.md", "Deep research", "parallel.ai — cited facts, key statistics, sources"),
    # the old system's reports, kept on old articles as history (the new review records its own)
    ("edits.md", "Edits checklist (the old review)", "every finding of the review before the new system, the edit made"),
    ("05_qa.md", "Editor QA (the old review)", "the reads, checks and findings of the review before the new system"),
    ("06_keywords.md", "Keyword coverage", "which targets landed, how often, and where"),
    ("07_plagiarism.md", "Uniqueness", "Winston: % unique, matched sources, passages to rewrite"),
]

# The ledger's notes are engineering telemetry; the operator needs plain language about WHY
# this item is on their desk (measured complaint: the raw notes read as gibberish).
def costs(art: store.Article, prices: dict) -> dict:
    return usage.article_summary(art.dir, prices)


def page_meta(art: store.Article, prices: dict | None = None) -> dict:
    """What the article's page and its editor show in their header, read only: the title, the
    words, the cost, when it changed, its folder's state. Nothing is computed into a file (the
    old scorecard rewrote the keyword report on a page view; review RB32-02)."""
    d = art.dir
    try:
        run = art.run()
    except store.CorruptFile:
        run = {}
    fm, body, _ = art.read() if art.source else ({}, "", "")
    wp = store.read_json_lenient(d / "_wp.json", {}) or {}
    src = art.source
    return {"slug": art.slug, "status": art.status(), "busy": art.busy(), "error": run.get("error"),
            "title": fm.get("title") or run.get("topic") or art.slug.replace("-", " ").title(),
            "topic": run.get("topic") or "", "words": len(body.split()),
            "wp": {"post_id": wp.get("post_id"), "link": wp.get("link"), "status": wp.get("status")},
            "mtime": src.stat().st_mtime if src else (d.stat().st_mtime if d.exists() else 0),
            "cover": (d / "assets/cover.png").exists(), "cost": costs(art, prices or pricing.DEFAULTS)}


def asset_version(adir, rel: str) -> int:
    """The picture's version for its URL: its modification time. A redrawn or re-captured picture
    keeps its name, and Cloudflare's four-hour browser cache served the old one under that name
    long after the fix landed (2026-10-01); a new version is a new URL."""
    try:
        return int((Path(adir) / rel).stat().st_mtime) if adir else 0
    except OSError:
        return 0


def versioned_assets(html_text: str, slug: str, adir=None) -> str:
    """Point every `src="assets/…"` at the Studio's asset route, with the picture's version. The
    path, its query and its fragment are taken apart: the version is looked up by the path, set
    in place of any `v=` the Markdown had, and the other parameters and the fragment stay (a
    `?v=old` or `#detail` address was left relative and 404ed, review R0E-01)."""
    from urllib.parse import unquote

    route = f"/a/{quote(slug, safe='')}/assets/"

    def one(m):
        raw = html.unescape(m.group(1))
        rest, _, frag = raw.partition("#")
        path, _, query = rest.partition("?")
        params = [q for q in query.split("&") if q and not q.startswith("v=")]
        v = asset_version(adir, "assets/" + unquote(path))
        if v:
            params.append(f"v={v}")
        url = route + path + ("?" + "&".join(params) if params else "") + ("#" + frag if frag else "")
        return f'src="{html.escape(url, quote=True)}"'

    return re.sub(r'src="assets/([^"]+)"', one, html_text)


def render_article_html(slug: str, body: str, adir=None) -> str:
    # drop the article's own H1: the page already shows the title above
    body = re.sub(r"^\s*#\s+.*$", "", body, count=1, flags=re.M)
    body = re.sub(r"<!--\s*visual:(\w+).*?-->", "", body, flags=re.DOTALL)
    return versioned_assets(md_render(body), slug, adir)


def _stage_from_run(art: store.Article, fname: str) -> str | None:
    """The keyword research, brief or deep research report rebuilt from the run record, for a
    folder that has the data but not the file (a re-run from cached inputs before they were
    copied along). Nothing is written; no model is called."""
    from types import SimpleNamespace

    try:
        run = art.run()
    except store.CorruptFile:
        return None
    key = {"01_keyword_research.md": "keyword_research", "02_brief.md": "brief",
           "03_research.md": "research"}.get(fname)
    if not key or not run.get(key):
        return None
    from .context import CONFIG, project

    st = SimpleNamespace(topic=run.get("topic") or art.slug, processor="",
                         keyword_research=run.get("keyword_research") or {}, brief=run.get("brief") or {},
                         research=run.get("research") or {},
                         target_words=(run.get("brief") or {}).get("target_words") or 0)
    try:
        if key == "keyword_research":
            from core.agents.keyword_researcher import KeywordResearcher

            return KeywordResearcher(project(), CONFIG)._format(st)
        if key == "brief":
            from core.agents.brief_generator import BriefGenerator

            return BriefGenerator(project(), CONFIG)._render(st, st.brief)
        from core.agents.deep_researcher import DeepResearcher

        return DeepResearcher(project(), CONFIG)._render(st, st.research)
    except Exception:  # noqa: BLE001 — a report that cannot be rebuilt is left out, as before
        return None


def stage_reports(art: store.Article) -> list[dict]:
    """The run's reports, rendered; the fixer's diagnostics in them (an OS error naming a file on
    the server) cleaned, the article's own words kept as written, a path in them too (reviews
    R4F5-01, RC77-07)."""
    from .privacy import scrub_errors

    out = []
    for fname, label, hint in STAGES:
        p = art.path(fname)
        if p.exists():
            out.append({"label": label, "hint": hint, "html": report_html(scrub_errors(p.read_text(encoding="utf-8")))})
        elif (md := _stage_from_run(art, fname)):
            out.append({"label": label, "hint": hint + " (from the run record)", "html": report_html(scrub_errors(md))})
    return out


# ---- install health, for the dashboard ----
# Chromium for screenshots and matplotlib for charts need room; below this every job is
# killed by the kernel the moment it starts, which reads as "the button does nothing".
MIN_MEM_MB = 768


def mem_limit_mb() -> int | None:
    """The container's memory ceiling, or None outside a cgroup."""
    for base in ("/sys/fs/cgroup", "/sys/fs/cgroup/memory"):
        for name in ("memory.max", "memory.limit_in_bytes"):
            try:
                v = (Path(base) / name).read_text().strip()
                return None if v == "max" else int(v) // 1048576
            except (OSError, ValueError):
                continue
    return None


def link_index_status(project, runner) -> dict:
    """Freshness of the internal-link inventory, for the dashboard card."""
    from core.tools.link_index import LinkIndex

    idx = LinkIndex.load(project.root)
    if idx is None:
        return {"count": 0, "job": runner.latest("links")}
    age = idx.age_days
    ttl = (project.links or {}).get("ttl_days", 7)
    return {"count": len(idx.targets), "stale": idx.stale(ttl),
            "age": "today" if age < 1 else f"{int(age)} day{'s' if int(age) != 1 else ''} old",
            "job": runner.latest("links"),
            "blog": sum(1 for t in idx.targets if t.source == "blog"),
            "site": sum(1 for t in idx.targets if t.source == "site"),
            "built": time.strftime("%d %b %H:%M", time.localtime(idx.generated_at))}
