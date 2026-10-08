"""The project's own facts changed from the Studio, layered over `project/profile.yaml`: the
logo, the pictures' colours, the website (our domains, the blog's address, the sitemaps), the competitors,
and the WordPress author. Stored on the persistent volume like the other settings
(`project/output/_project.json`, the logo in `_brand/`), so a release keeps them, and read by
`config.load_project` and `brand.default`, so the engine, the publisher and the interface see one
project.

    {"website": {"domains": [...], "blog_base_url", "site_sitemap", "blog_sitemap"},
     "palette": {"ink": "#0F1526", ...}, "competitor_hosts": [...], "author_id": 4,
     "wp_site": {"name", "categories": [...], "authors": [{"id", "name"}], "checked_at", "for"}}

`wp_site` is shown only while the connection is the one it read ("for", a fingerprint of the
address, the user and the password): another connection is not tested yet.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from .store import atomic_write_bytes, atomic_write_text, read_json_lenient

FILE = "_project.json"
BRAND_DIR = "_brand"
LOGO_TYPES = {"png": b"\x89PNG\r\n\x1a\n", "jpg": b"\xff\xd8\xff", "webp": b"RIFF"}
LOGO_WIDTH = 1200          # an SVG is drawn this wide: the pictures and the sidebar take a raster
MAX_LOGO = 2 * 1024 * 1024
PALETTE_KEYS = ("ink", "muted", "accent", "accent_2", "line", "soft")
_HEX = re.compile(r"#[0-9a-fA-F]{6}")
_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class Invalid(ValueError):
    """A value the project cannot take: the message names the field and the rule."""


def load(output_dir) -> dict:
    d = read_json_lenient(Path(output_dir) / FILE, {}) or {}
    return d if isinstance(d, dict) else {}


def _write(output_dir, data: dict) -> None:
    atomic_write_text(Path(output_dir) / FILE, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _url(value: str, label: str) -> str:
    v = " ".join(str(value or "").split())
    if not v:
        return ""
    p = urlparse(v)
    if p.scheme not in ("http", "https") or not p.netloc or " " in v:
        raise Invalid(f"{label}: an address that starts with https://")
    return v.rstrip("/") if p.path in ("", "/") else v


def _hosts(value, label: str) -> list[str]:
    items = value if isinstance(value, list) else re.split(r"[\s,]+", str(value or ""))
    out = []
    for raw in items:
        h = str(raw).strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0].removeprefix("www.")
        if not h:
            continue
        if not _HOST.match(h):
            raise Invalid(f"{label}: '{raw}' is not a domain like example.com")
        if h not in out:
            out.append(h)
    return out


def save_website(output_dir, *, domains=None, blog_base_url: str | None = None, site_sitemap: str | None = None,
                 blog_sitemap: str | None = None) -> dict:
    """Our domains, the blog's address and the sitemaps the link index reads. None leaves a
    field as it is (the profile's, when it was never saved); the domains and the blog cannot be
    emptied; an empty sitemap is not read."""
    data = load(output_dir)
    web = dict(data.get("website") or {})
    if domains is not None:
        web["domains"] = _hosts(domains, "Our domains")
        if not web["domains"]:
            raise Invalid("Our domains: at least one, like example.com")
    if blog_base_url is not None:
        web["blog_base_url"] = _url(blog_base_url, "The blog")
        if not web["blog_base_url"]:
            raise Invalid("The blog: its address, like https://blog.example.com")
    for key, value, label in (("site_sitemap", site_sitemap, "The site's sitemap"),
                              ("blog_sitemap", blog_sitemap, "The blog's sitemap")):
        if value is not None:
            web[key] = _url(value, label)
    data["website"] = web
    _write(output_dir, data)
    return web


def save_palette(output_dir, palette: dict) -> dict:
    """The colours of the article's charts, infographics and cover."""
    clean = {}
    for k in PALETTE_KEYS:
        v = str((palette or {}).get(k) or "").strip()
        if not v:
            continue
        if not _HEX.fullmatch(v):
            raise Invalid(f"The colour '{k.replace('_', ' ')}': a hex colour like #2A6FDB")
        clean[k] = v.upper()
    data = load(output_dir)
    data["palette"] = clean
    _write(output_dir, data)
    return clean


def save_competitors(output_dir, hosts) -> list[str]:
    data = load(output_dir)
    data["competitor_hosts"] = _hosts(hosts, "Competitor hosts")
    _write(output_dir, data)
    return data["competitor_hosts"]


def save_wordpress(output_dir, *, author_id=None, site: dict | None = None, tested_for: str | None = None) -> dict:
    """The author the posts carry (picked from the site) and what the last connection test read
    from it: its name, its categories and its authors, for the pickers. `author_id` None leaves
    the author as it is; "" (or 0) is the user the application password belongs to, so no author
    is sent, whatever the profile or WP_AUTHOR_ID says; a user id is that user."""
    data = load(output_dir)
    if author_id is not None:
        raw = str(author_id).strip()
        if not raw.isdigit() and raw != "":
            raise Invalid("The author: pick one of the site's users")
        data["author_id"] = int(raw or 0)
    if site is not None:
        from . import api_keys

        # the connection the test was of (taken before it asked), else the one in force now
        data["wp_site"] = {**site, "for": tested_for if tested_for is not None
                           else api_keys.fingerprint(output_dir, api_keys.WP_GROUP)}
    _write(output_dir, data)
    return data


def wp_site(output_dir) -> dict | None:
    """What the last connection test read from the site, while the connection is the one it
    tested; None when it was not tested, or another connection is set since."""
    from . import api_keys

    site = load(output_dir).get("wp_site")
    if not isinstance(site, dict) or site.get("for") != api_keys.fingerprint(output_dir, api_keys.WP_GROUP):
        return None
    return site


# ---- the logo --------------------------------------------------------------------------------
def logo_path(output_dir) -> Path | None:
    d = Path(output_dir) / BRAND_DIR
    for ext in LOGO_TYPES:
        p = d / f"logo.{ext}"
        if p.is_file() and not p.is_symlink():
            return p
    return None


def save_logo(output_dir, data: bytes, filename: str = "") -> Path:
    """An uploaded logo, kept as `_brand/logo.<type>`, the type read from its bytes. Every
    picture (the cover, the infographics) and the sidebar read it, so it is a raster they can
    open: an SVG is drawn to a PNG here, and a file that does not open is refused before the
    logo in use is replaced."""
    if not data:
        raise Invalid("The logo: choose a PNG, JPG, WebP or SVG file")
    if len(data) > MAX_LOGO:
        raise Invalid("The logo: at most 2 MB")
    head = data[:512].lstrip()
    kind = next((k for k, magic in LOGO_TYPES.items() if data.startswith(magic)), None)
    if kind == "webp" and data[8:12] != b"WEBP":
        kind = None
    if kind is None and head.startswith(b"<") and b"<svg" in data[:4096].lower():
        if re.search(rb"<script|on[a-z]+\s*=|javascript:", data, re.I):
            raise Invalid("The logo: an SVG with scripts is not taken")
        data, kind = _svg_to_png(data), "png"
    if kind is None:
        raise Invalid("The logo: a PNG, JPG, WebP or SVG file")
    _opens(data)
    remove_logo(output_dir)
    p = Path(output_dir) / BRAND_DIR / f"logo.{kind}"
    atomic_write_bytes(p, data)
    hide_logo(output_dir, False)
    return p


def _opens(data: bytes) -> None:
    """The image opens and decodes whole (what the cover's compositor will do with it)."""
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            if im.width < 1 or im.height < 1:
                raise ValueError("empty")
    except Exception as e:  # noqa: BLE001 — any decoder error: the file is not a usable image
        raise Invalid("The logo: the file does not open as an image") from e


def _svg_to_png(data: bytes) -> bytes:
    """An SVG drawn LOGO_WIDTH wide on a transparent background, by the browser the
    screenshots use (an SVG shown as an image loads nothing from outside)."""
    import base64

    src = "data:image/svg+xml;base64," + base64.b64encode(data).decode()
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": LOGO_WIDTH + 40, "height": 800})
                page.set_content('<html><body style="margin:0;background:transparent">'
                                 f'<img id="logo" style="display:block;width:{LOGO_WIDTH}px" src="{src}"></body></html>')
                ok = page.evaluate("""() => { const i = document.getElementById('logo');
                    return i.decode().then(() => i.naturalWidth > 0 && i.getBoundingClientRect().height > 0, () => false); }""")
                if not ok:
                    raise Invalid("The logo: the SVG does not draw (no size, or broken)")
                return page.locator("#logo").screenshot(omit_background=True, type="png")
            finally:
                browser.close()
    except Invalid:
        raise
    except Exception as e:  # noqa: BLE001 — no browser here: say what works instead
        raise Invalid("The logo: an SVG cannot be converted here (no browser); upload a PNG") from e


def remove_logo(output_dir) -> None:
    """The uploaded logo taken away (the shipped one, if any, shows again)."""
    d = Path(output_dir) / BRAND_DIR
    for ext in (*LOGO_TYPES, "svg"):            # svg: a logo kept before SVGs were drawn to PNG
        (d / f"logo.{ext}").unlink(missing_ok=True)


def hide_logo(output_dir, hidden: bool = True) -> None:
    """No logo at all: the shipped one is not used either (Remove with nothing uploaded)."""
    data = load(output_dir)
    if bool(data.get("logo_hidden")) == bool(hidden):
        return
    if hidden:
        data["logo_hidden"] = True
    else:
        data.pop("logo_hidden", None)
    _write(output_dir, data)


# ---- layered over the profile ----------------------------------------------------------------
def apply(profile: dict, output_dir) -> dict:
    """`profile` (profile.yaml as read) with what the Studio saved laid over it."""
    over = load(output_dir)
    p = json.loads(json.dumps(profile or {}))           # a deep copy of plain YAML data
    brand = p.setdefault("brand", {})
    links = p.setdefault("links", {})
    publish = p.setdefault("publish", {})
    web = over.get("website") or {}
    if web.get("domains"):
        brand["domains"] = list(web["domains"])
    if web.get("blog_base_url"):
        links["blog_base_url"] = web["blog_base_url"]
    for key in ("site_sitemap", "blog_sitemap"):     # saved empty: not read
        if key in web:
            links[key] = web[key]
    if web.get("blog_base_url"):
        publish["base_url"] = web["blog_base_url"]
    if over.get("palette"):
        brand["palette"] = {**(brand.get("palette") or {}), **over["palette"]}
    if "competitor_hosts" in over:
        links["competitor_hosts"] = list(over["competitor_hosts"])
    if "author_id" in over:                          # 0: the user the password belongs to
        publish["author_id"] = over["author_id"] or None
        publish["author_picked"] = True
    logo = logo_path(output_dir)
    if logo is not None:
        brand["logo"] = str(logo.resolve())
    elif over.get("logo_hidden"):
        brand.pop("logo", None)
    # a new project gives its domain and its blog, not their sitemaps: their usual places are read
    # (shown on the Project page, so saving the page keeps them; saved empty, they are not read)
    if "site_sitemap" not in links and brand.get("domains"):
        links["site_sitemap"] = f"https://{str(brand['domains'][0]).removeprefix('www.')}/sitemap.xml"
    if "blog_sitemap" not in links and links.get("blog_base_url"):
        links["blog_sitemap"] = f"{str(links['blog_base_url']).rstrip('/')}/sitemap_index.xml"
    return p


def stamp(output_dir) -> float:
    """When the saved project facts last changed (a cache key)."""
    out = 0.0
    for p in (Path(output_dir) / FILE, logo_path(output_dir)):
        try:
            out = max(out, p.stat().st_mtime) if p else out
        except OSError:
            pass
    return out
