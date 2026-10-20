"""The project's own identity as the engine needs it: its domains, its logo, its colours.

Read from `brand:` (and `links:`) in project/profile.yaml, with what the setup and Configure >
Project saved laid over it (core/project_settings.py), so the engine carries no product's name:
another project is another profile.

    brand:
      domains: ["example.com"]           # our own site: internal links, the fixer's allowed
                                         # sources, what uniqueness does not match against
      logo: "assets/logo.png"            # composited on covers and infographics; absent = none
      palette: {ink, muted, accent, accent_2, line, soft}      # the infographics
      headline_acronyms: [api, faq]      # upper-cased in cover headlines
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

NEUTRAL_PALETTE = {"ink": "#0F1526", "muted": "#5B6775", "accent": "#2A6FDB", "accent_2": "#1B3A6B",
                   "line": "#E5E9F0", "soft": "#F7F8FB"}
BASE_ACRONYMS = ("ai", "api", "faq", "vs", "seo", "saas", "b2b", "b2c")


def _host(url: str | None) -> str:
    """The URL's host name: no port ("https://acme.example:443/" is still acme.example, Codex
    R102-02), no user, lower case, no "www."."""
    try:
        h = (urlparse(url or "").hostname or "").lower()
    except ValueError:          # a malformed port or bracket: no host to compare
        return ""
    return h[4:] if h.startswith("www.") else h


@dataclass(frozen=True)
class Brand:
    domains: tuple[str, ...] = ()
    site_host: str = ""
    blog_host: str = ""
    logo: Path | None = None
    palette: dict = field(default_factory=lambda: dict(NEUTRAL_PALETTE))
    acronyms: frozenset = frozenset(BASE_ACRONYMS)
    min_product_links: int = 2
    words_per_link: int = 350

    @property
    def domain(self) -> str:
        """The main domain, for prompts ("at least 2 links to <domain> product pages")."""
        return self.domains[0] if self.domains else "our site"

    def is_own(self, url: str) -> bool:
        h = _host(url)
        return bool(h) and any(h == d or h.endswith("." + d) for d in self.domains)

    def is_product_page(self, url: str) -> bool:
        """An own page that is not a blog post."""
        return self.is_own(url) and (not self.blog_host or _host(url) != self.blog_host)

    def is_app_page(self, url: str) -> bool:
        """Our own app, docs or API host (not the main site, not the blog): a legitimate target
        whether or not the sitemap index lists it."""
        h = _host(url)
        return self.is_own(url) and h not in (self.site_host, self.blog_host) and h not in self.domains

    def caption_for(self, caption: str, url: str) -> str:
        """The caption under a screenshot of `url`: a link to the page when the page is ours, so
        the reader can open what the picture shows (operator, 2026-09-28); plain text under
        anyone else's page."""
        text = " ".join(str(caption or "").replace("[", "(").replace("]", ")").split())
        return f"[{text}]({url})" if text and url and self.is_own(url) else text

    def logo_file(self) -> Path | None:
        return self.logo if self.logo and self.logo.is_file() else None


def from_profile(profile: dict, project_root: Path) -> Brand:
    b = profile.get("brand") or {}
    links = profile.get("links") or {}
    site_host = _host(links.get("site_sitemap"))
    blog_host = _host(links.get("blog_base_url") or links.get("blog_sitemap"))
    domains = [str(d).lower().removeprefix("www.") for d in (b.get("domains") or []) if d]
    if not domains:
        # derived from the sitemaps: the shortest registrable-looking host of the site and the blog
        for h in (site_host, blog_host):
            if h:
                parts = h.split(".")
                d = ".".join(parts[-2:]) if len(parts) >= 2 else h
                if d not in domains:
                    domains.append(d)
    logo = (Path(project_root) / b["logo"]) if b.get("logo") else None
    return Brand(domains=tuple(domains), site_host=site_host, blog_host=blog_host, logo=logo,
                 palette={**NEUTRAL_PALETTE, **(b.get("palette") or {})},
                 acronyms=frozenset(BASE_ACRONYMS) | {str(a).lower() for a in b.get("headline_acronyms") or []},
                 min_product_links=int(links.get("min_product_links", 2)),
                 words_per_link=int(links.get("words_per_link", 350)))


@lru_cache(maxsize=4)
def _cached(path: str, mtime: float, output_dir: str, saved: float) -> Brand:
    import yaml

    from . import project_settings

    p = Path(path)
    profile = project_settings.apply(yaml.safe_load(p.read_text(encoding="utf-8")) or {}, output_dir)
    return from_profile(profile, p.parent)


def default() -> Brand:
    """The active project's brand, for code that is not handed a Project (the structure
    measurements, the scorecard, the uniqueness exclusions), with what the Studio saved over
    the profile (core/project_settings.py)."""
    from . import project_settings
    from .config import project_root
    from .store import output_root

    p = project_root() / "profile.yaml"
    out = output_root()
    try:
        return _cached(str(p), p.stat().st_mtime, str(out), project_settings.stamp(out))
    except OSError:
        return Brand()
