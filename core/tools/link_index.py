"""Internal link index — the inventory of our OWN pages the writer links into.

Built from the public sitemaps (exactly the URLs Google sees), then enriched so a
target can be matched to an article's topic:

  - the blog (WordPress/Yoast): `sitemap_index.xml` -> post/page/category sitemaps,
    enriched in ONE bulk pass over the WP REST API (title, excerpt, category) — no
    page crawling, no auth required for published content
  - the marketing site: `sitemap.xml`, enriched by fetching each page's
    <title> and <meta name="description">

A rebuild is saved to `<output>/_cache/link_index.json` on the volume, so a release does not
roll it back; until the first rebuild a copy in the project folder (`project/link_index.json`),
if one is there, is read. A new install has none: the first run builds it.
It is rebuilt on demand (`cli.py refresh-links`, or the Refresh button in the Studio). Selection is
deterministic keyword scoring — no LLM call, so it costs nothing per article.

Everything product-specific (domains, exclusions, density) lives in
`project/profile.yaml` under `links:`; this module stays product-agnostic.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests
from ..store import atomic_write_text

_UA = {"User-Agent": "Mozilla/5.0 (compatible; seo-writer-agent link-index/1.0)"}
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.DOTALL)
_META_DESC = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', re.I | re.DOTALL)
_META_DESC_ALT = re.compile(
    r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']', re.I | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")

_STOP = frozenset("""
a an and are as at be but by can do does for from has have how in into is it its of on or
that the their them then there these this to was were what when where which who why will
with you your our we us they he she not no if than too very just get make made use used
""".split())


def _tokens(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 2 and w not in _STOP]


def _clean(html_text: str) -> str:
    """Strip tags and resolve entities — WP REST returns rendered HTML, not plain text."""
    import html as _html

    txt = _TAG.sub(" ", html_text or "")
    txt = _html.unescape(_html.unescape(txt))  # twice: WP double-encodes some titles
    return re.sub(r"\s+", " ", txt).strip()


@dataclass
class LinkTarget:
    url: str
    title: str
    description: str = ""
    source: str = "site"      # "blog" | "site"
    kind: str = "page"        # post | page | category | landing | home
    category: str = ""
    terms: list[str] = field(default_factory=list)

    @property
    def is_product_page(self) -> bool:
        """Marketing-site pages are the ones that carry commercial weight."""
        return self.source == "site"

    def to_dict(self) -> dict:
        return {"url": self.url, "title": self.title, "description": self.description,
                "source": self.source, "kind": self.kind, "category": self.category}

    @classmethod
    def from_dict(cls, d: dict) -> "LinkTarget":
        t = cls(url=d["url"], title=d.get("title", ""), description=d.get("description", ""),
                source=d.get("source", "site"), kind=d.get("kind", "page"),
                category=d.get("category", ""))
        t.terms = t._index_terms()
        return t

    def _index_terms(self) -> list[str]:
        slug = urlparse(self.url).path.strip("/").split("/")[-1].replace("-", " ")
        # title and slug carry the intent; the description only disambiguates
        return (_tokens(self.title) * 3) + (_tokens(slug) * 2) + _tokens(self.description)


# --------------------------------------------------------------------------- fetching

def _get(url: str, timeout: int = 10) -> str:
    r = requests.get(url, headers=_UA, timeout=timeout)
    r.raise_for_status()
    return r.text


def _sitemap_urls(url: str) -> list[str]:
    """Return every <loc> in a sitemap, expanding a sitemap index one level."""
    try:
        xml = _get(url)
    except Exception as e:  # noqa: BLE001 — a dead sitemap must not kill the build
        print(f"[links] sitemap {url}: {e}")
        return []
    locs = _LOC.findall(xml)
    if "<sitemapindex" in xml.lower():
        out: list[str] = []
        for child in locs:
            out += _sitemap_urls(child)
        return out
    return locs


def _wp_enrichment(base_url: str) -> dict[str, dict]:
    """title/excerpt/category for every published post & page, in bulk from WP REST."""
    base = base_url.rstrip("/")
    cats: dict[int, str] = {}
    try:
        for c in requests.get(f"{base}/wp-json/wp/v2/categories",
                              params={"per_page": 100, "_fields": "id,name"},
                              headers=_UA, timeout=15).json():
            cats[c["id"]] = c["name"]
    except Exception as e:  # noqa: BLE001
        print(f"[links] wp categories: {e}")

    out: dict[str, dict] = {}
    try:  # category archives are legitimate hub targets, but carry no post payload
        for c in requests.get(f"{base}/wp-json/wp/v2/categories",
                              params={"per_page": 100, "_fields": "name,link,description"},
                              headers=_UA, timeout=15).json():
            out[c["link"].rstrip("/")] = {
                "title": f"{_clean(c['name'])} (category archive)",
                "description": _clean(c.get("description", ""))[:300],
                "kind": "category", "category": _clean(c["name"]),
            }
    except Exception as e:  # noqa: BLE001
        print(f"[links] wp category archives: {e}")

    for typ in ("posts", "pages"):
        for page in range(1, 12):
            try:
                r = requests.get(f"{base}/wp-json/wp/v2/{typ}",
                                 params={"per_page": 100, "page": page, "status": "publish",
                                         "_fields": "link,title,excerpt,categories"},
                                 headers=_UA, timeout=20)
                if r.status_code != 200:
                    break
                items = r.json()
            except Exception as e:  # noqa: BLE001
                print(f"[links] wp {typ} p{page}: {e}")
                break
            if not items:
                break
            for it in items:
                cat_ids = it.get("categories") or []
                out[it["link"].rstrip("/")] = {
                    "title": _clean((it.get("title") or {}).get("rendered", "")),
                    "description": _clean((it.get("excerpt") or {}).get("rendered", ""))[:300],
                    "kind": "post" if typ == "posts" else "page",
                    "category": cats.get(cat_ids[0], "") if cat_ids else "",
                }
            if len(items) < 100:
                break
    return out


def _page_meta(url: str) -> tuple[str, str]:
    try:
        html = _get(url, timeout=8)
    except Exception:  # noqa: BLE001 — skip unreachable pages, keep the rest
        return "", ""
    title = _clean(_TITLE.search(html).group(1)) if _TITLE.search(html) else ""
    m = _META_DESC.search(html) or _META_DESC_ALT.search(html)
    return title, (_clean(m.group(1))[:300] if m else "")


# --------------------------------------------------------------------------- index

class LinkIndex:
    """Load / build / query the inventory of internal link targets."""

    def __init__(self, targets: list[LinkTarget], generated_at: float = 0.0):
        self.targets = targets
        self.generated_at = generated_at

    # ---- persistence ----
    @staticmethod
    def path(project_root: Path) -> Path:
        """Where the index is read from: the rebuilt copy on the volume (_cache/), else the one
        shipped in the project folder."""
        from ..store import cache_path

        cached = cache_path("link_index.json")
        return cached if cached.exists() else Path(project_root) / "link_index.json"

    @classmethod
    def load(cls, project_root: Path) -> "LinkIndex | None":
        p = cls.path(project_root)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"[links] unreadable index: {e}")
            return None
        return cls([LinkTarget.from_dict(d) for d in data.get("targets", [])],
                   generated_at=data.get("generated_at", 0.0))

    def save(self, project_root: Path | None = None) -> Path:
        """Always to the volume: rebuilding the index used to rewrite a file tracked in git (and,
        in the image, one a release reverts)."""
        from ..store import cache_path

        p = cache_path("link_index.json")
        atomic_write_text(p, json.dumps(
            {"generated_at": self.generated_at,
             "generated_at_iso": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.generated_at)),
             "count": len(self.targets),
             "targets": [t.to_dict() for t in self.targets]},
            indent=2, ensure_ascii=False))
        return p

    @property
    def age_days(self) -> float:
        return (time.time() - self.generated_at) / 86400 if self.generated_at else 1e9

    def stale(self, ttl_days: float) -> bool:
        return self.age_days > ttl_days

    # ---- build ----
    @classmethod
    def build(cls, cfg: dict, log=print) -> "LinkIndex":
        """cfg = the `links:` block of project/profile.yaml."""
        exclude = [re.compile(p, re.I) for p in cfg.get("exclude", [])]
        targets: dict[str, LinkTarget] = {}

        def add(url: str, **kw) -> None:
            url = url.rstrip("/") or url
            if not url.startswith("http") or any(rx.search(url) for rx in exclude):
                return
            if url not in targets:
                targets[url] = LinkTarget(url=url, **kw)

        # 1) the blog: sitemap urls, enriched from WP REST
        blog_base = cfg.get("blog_base_url", "")
        if blog_base:
            wp = _wp_enrichment(blog_base)
            log(f"[links] wp rest: {len(wp)} posts/pages")
            blog_map = cfg.get("blog_sitemap", f"{blog_base}/sitemap_index.xml")   # saved empty: not read
            for url in (_sitemap_urls(blog_map) if blog_map else []):
                meta = wp.get(url.rstrip("/"), {})
                kind = meta.get("kind") or ("category" if "/category/" in url else "page")
                add(url, title=meta.get("title", ""), description=meta.get("description", ""),
                    source="blog", kind=kind, category=meta.get("category", ""))

        # 2) the marketing site: sitemap urls + <title>/<meta description>
        site_urls = [u for u in _sitemap_urls(cfg.get("site_sitemap", ""))
                     if cfg.get("site_sitemap")]
        keep = [u for u in site_urls if not any(rx.search(u) for rx in exclude)]
        log(f"[links] site sitemap: {len(site_urls)} urls, {len(keep)} after exclusions")
        if keep:
            with ThreadPoolExecutor(max_workers=6) as ex:
                metas = list(ex.map(_page_meta, keep))
            unreachable = sum(1 for t, _ in metas if not t)
            if unreachable:
                log(f"[links] WARNING: {unreachable}/{len(keep)} site pages unreachable "
                    f"(egress to the site blocked?) — they stay in the index without titles")
            for url, (title, desc) in zip(keep, metas):
                add(url, title=title, description=desc, source="site",
                    kind="home" if urlparse(url).path.strip("/") == "" else "landing")

        # drop republished duplicates (same title, e.g. `…-2` reposts) — keep the shortest URL
        by_title: dict[str, LinkTarget] = {}
        loose: list[LinkTarget] = []
        for t in targets.values():
            key = " ".join(_tokens(t.title))
            if not key:
                loose.append(t)
            elif key not in by_title or len(t.url) < len(by_title[key].url):
                by_title[key] = t
        dupes = len(targets) - len(by_title) - len(loose)
        if dupes:
            log(f"[links] dropped {dupes} duplicate-title targets")

        out = list(by_title.values()) + loose
        for t in out:
            t.terms = t._index_terms()
        log(f"[links] index built: {len(out)} targets "
            f"({sum(1 for t in out if t.source == 'blog')} blog / "
            f"{sum(1 for t in out if t.source == 'site')} site)")
        return cls(out, generated_at=time.time())

    # ---- query ----
    def score(self, terms: list[str], t: LinkTarget) -> float:
        if not t.terms or not terms:
            return 0.0
        wanted = set(terms)
        hits = sum(1 for w in t.terms if w in wanted)
        if not hits:
            return 0.0
        # normalise so long descriptions don't win on volume alone
        return hits / (len(t.terms) ** 0.5)

    @staticmethod
    def _family(t: LinkTarget) -> str:
        """Templated siblings share an opening phrase ('Real-Time AI Translation - From X
        to English' x34). Without this, one page family crowds out the whole selection."""
        return " ".join(_tokens(t.title)[:3])

    def select(self, query: str, n: int = 12, min_product: int = 2,
               max_per_family: int = 2, exclude_urls: set[str] | None = None) -> list[LinkTarget]:
        """Best-matching targets for an article, guaranteeing some product pages."""
        terms = _tokens(query)
        skip = {u.rstrip("/") for u in (exclude_urls or set())}
        ranked = sorted(
            ((self.score(terms, t), t) for t in self.targets if t.url.rstrip("/") not in skip),
            key=lambda x: (-x[0], x[1].url))
        scored = [t for s, t in ranked if s > 0]

        picked, seen_family = [], {}
        for t in scored:
            fam = self._family(t)
            if fam and seen_family.get(fam, 0) >= max_per_family:
                continue
            seen_family[fam] = seen_family.get(fam, 0) + 1
            picked.append(t)
            if len(picked) >= n:
                break
        # the marketing site is small; make sure commercial pages are always on the table
        if min_product:
            have = sum(1 for t in picked if t.is_product_page)
            if have < min_product:
                extra = [t for t in scored if t.is_product_page and t not in picked]
                extra += [t for t in self.targets
                          if t.is_product_page and t not in picked and t not in extra]
                picked = picked[:max(0, n - (min_product - have))] + extra[:min_product - have]
        return picked
