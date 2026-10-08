"""WordPress publisher — standard REST API + Application Passwords (HTTP Basic).

Flow (verified against developer.wordpress.org):
  1. upload every local image (cover + assets/*) to /wp-json/wp/v2/media
     (raw bytes + Content-Disposition filename) -> {id, source_url}; set alt_text
  2. rewrite local image paths in the Markdown -> uploaded source_urls
  3. render MD -> HTML with the same renderer as the preview (markdown-it-py)
  4. create the post as DRAFT (default) with title/slug/excerpt/featured_media/
     categories (matched by name against EXISTING categories — never created)/
     tags (get-or-create)
  5. idempotent: the post id, the site it lives on and the uploaded media (with a hash of
     each file) are stored in <article_dir>/_wp.json — re-publishing UPDATES the same post
     on the same site instead of creating a duplicate, and re-uploads only changed images

Never publishes live unless status="publish" is passed explicitly; an update keeps the post's
status on WordPress.
"""
from __future__ import annotations

import html
import os
import re
from pathlib import Path

import requests

from ..store import shown_image_refs
from .markdown_render import render as md_to_html

_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


class NotSent(RuntimeError):
    """A refusal before any request reached WordPress: nothing was sent."""


def _ok(r) -> bool:
    return 200 <= int(getattr(r, "status_code", 0) or 0) < 300


_IMG_TAG = re.compile(r'<img\b[^>]*?\bsrc="([^"]*)"[^>]*>')


def _uploaded_srcs(html_text: str, media: dict, warnings: list) -> str:
    """Each local picture of the rendered post pointed at its upload; a local picture with no
    upload is taken out (and said), never left as a relative address the blog cannot serve."""
    from ..store import local_asset

    def one(m):
        src = html.unescape(m.group(1))
        if re.match(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|/)", src):
            return m.group(0)                 # a web address (or a site path): not ours to upload
        rel = local_asset(src)
        if rel is None:
            warnings.append(f"skipped {src}: not an image in this article's assets/, left out of the post")
            return ""
        info = media.get(rel)
        if not info or not info.get("source_url"):
            warnings.append(f"{rel} was not uploaded, so it is left out of the post")
            return ""
        return m.group(0).replace(f'src="{m.group(1)}"', f'src="{html.escape(info["source_url"], quote=True)}"', 1)

    return _IMG_TAG.sub(one, html_text)


def _cat_name(c: dict) -> str:
    """A category's name as a person typed it: the REST API returns it HTML-escaped ('&amp;')."""
    return html.unescape(str(c.get("name") or "")).strip().lower()


def _seo_clean_body(body: str) -> str:
    """SEO-team rules applied deterministically before rendering:
    - drop the leading H1 (WP renders the post title itself — avoids duplicate H1)
    - drop horizontal rules (`---` lines cause 'zebra' striping near headings)
    - de-duplicate links: every URL is linked once (first mention); later -> plain text

    The dedupe normally has nothing to do — the writer's output is already normalised — but
    it stays as the last-resort net for hand-edited articles. Same implementation, so the two
    can never disagree.
    """
    from ..validators import dedupe_links

    body = re.sub(r"^\s*#\s[^\n]*\n+", "", body, count=1)
    body = re.sub(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$\n?", "", body, flags=re.M)
    return dedupe_links(body)[0]


def _prepare_image(path: Path, max_kb: int = 100, max_w: int = 1400) -> tuple[bytes, str, str]:
    """Convert any raster to WebP under max_kb (SEO requirement). Returns (bytes, ext, mime)."""
    import io
    from PIL import Image

    im = Image.open(path)
    if im.mode in ("RGBA", "P"):
        im = im.convert("RGB")
    if im.width > max_w:
        im = im.resize((max_w, round(im.height * max_w / im.width)), Image.LANCZOS)
    data = b""
    for q in (82, 72, 62, 52, 42):
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=q)
        data = buf.getvalue()
        if len(data) <= max_kb * 1024:
            break
    return data, "webp", "image/webp"


_WORD = re.compile(r"[A-Za-z0-9]+")


def _kw_present(text: str, kw: str) -> bool:
    """Loose keyphrase match, read the way Yoast's English morphology reads it: word order
    kept, a trailing plural `s` and any hyphen/space mix allowed, so "real time translation
    app" matches "Real-Time Translation Apps".
    """
    words = _WORD.findall(kw or "")
    if not words:
        return False
    pat = r"s?[\s\-]+".join(map(re.escape, words)) + r"s?"
    return re.search(r"\b" + pat + r"\b", text or "", re.I) is not None


def _focus_keyword(article_dir: Path, fm: dict) -> str:
    """Yoast's focus keyphrase for this article.

    From the frontmatter when the run wrote one, else straight out of the run record: every
    article generated before `focus_keyword` existed in the frontmatter still carries its
    primary keyword in `_run.json`, and re-publishing one should fill the field rather than
    leave Yoast with nothing to score.
    """
    kw = str(fm.get("focus_keyword") or "").strip()
    if kw:
        return kw
    from ..store import read_json_lenient

    run = read_json_lenient(Path(article_dir) / "_run.json", {}) or {}
    return str((run.get("keyword_research") or {}).get("primary_keyword") or "").strip()


def _cover_alt(title: str, kw: str) -> str:
    """Alt text for the cover image.

    The cover illustrates the article's subject, so the title IS the honest description of
    it — and the title always carries the primary keyword because `validate_meta` enforces
    that. The prefixed form is the fallback for a hand-edited title that dropped it; it is
    never applied when the keyword is already there, so nothing gets stuffed.
    """
    return title if not kw or _kw_present(title, kw) else f"{kw}: {title}"


def author_for(publish: dict) -> int | None:
    """The author a post carries: the one picked in the Studio (None when the pick is the user
    the application password belongs to), else WP_AUTHOR_ID, else the profile's."""
    if (publish or {}).get("author_picked"):
        raw = publish.get("author_id")
    else:
        raw = os.getenv("WP_AUTHOR_ID")
        if not str(raw or "").isdigit():
            raw = (publish or {}).get("author_id")
    return int(raw) if str(raw or "").isdigit() and int(raw) else None


class WPPublisher:
    def __init__(self, base_url=None, username=None, app_password=None, author_id=None):
        # the password goes only to the site it was entered for (core/api_keys.py): a site URL
        # saved in the Studio on its own must not receive the server's password. Without explicit
        # credentials the site, the user, the password and that binding come from ONE reading of
        # the saved keys: the environment is updated one name at a time by a save in another
        # request, and three separate reads could pair one site's password with another site.
        self.refused = ""
        if app_password:
            site = (base_url or os.getenv("WP_BASE_URL", ""), username or os.getenv("WP_USERNAME"), app_password)
        else:
            from .. import api_keys

            d = api_keys._default_dir()
            if d is not None:
                *site, self.refused = api_keys.wp_credentials(d, base_url, username)
            else:
                site = (base_url or os.getenv("WP_BASE_URL", ""), username or os.getenv("WP_USERNAME"),
                        os.getenv("WP_APP_PASSWORD"))
        self.base_url = (site[0] or "").rstrip("/")
        self._auth = (site[1], site[2])
        self.api = f"{self.base_url}/wp-json/wp/v2"
        # posts must be attributed to the blog's byline author, not to whichever
        # account holds the API credentials
        if author_id is None:
            try:
                from ..config import load_project
                publish = load_project().publish
            except Exception:  # noqa: BLE001 — publishing works fine without it
                publish = {}
            author_id = author_for(publish)
        self.author_id = int(author_id) if str(author_id or "").isdigit() and int(author_id) else None

    @property
    def auth(self) -> tuple:
        """The credentials every request sends. Refused credentials raise instead, so no call site
        can send the password where it does not belong."""
        if self.refused:
            raise NotSent(f"WordPress credentials refused: {self.refused}")
        return self._auth

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self._auth[0] and self._auth[1]) and not self.refused

    # ---- media ----
    def upload_media(self, path: Path, filename: str, alt: str = "") -> dict:
        """Upload one image as light WebP (the SEO rule: <100 KB). A file that does not decode as
        an image is refused (ValueError), never sent as raw bytes."""
        try:
            data, ext, mime = _prepare_image(path)
        except Exception as e:  # noqa: BLE001 — not an image we can read
            raise ValueError(f"{path.name} is not a readable image ({type(e).__name__})") from None
        filename = re.sub(r"\.\w+$", f".{ext}", filename)
        r = requests.post(
            f"{self.api}/media",
            headers={"Content-Disposition": f'attachment; filename="{filename}"',
                     "Content-Type": mime},
            data=data, auth=self.auth, timeout=120,
        )
        r.raise_for_status()
        media = r.json()
        out = {"id": media["id"], "source_url": media["source_url"]}
        if alt:
            ra = requests.post(f"{self.api}/media/{media['id']}",
                               json={"alt_text": alt}, auth=self.auth, timeout=60)
            if not _ok(ra):
                out["alt_failed"] = True
        return out

    # ---- taxonomy ----
    def match_category(self, name: str) -> int | None:
        """Match against EXISTING categories only (never create — avoid duplicates)."""
        r = requests.get(f"{self.api}/categories",
                         params={"search": name, "per_page": 20}, auth=self.auth, timeout=60)
        r.raise_for_status()
        cats = r.json()
        if not cats:
            return None
        low = name.strip().lower()
        for c in cats:
            if _cat_name(c) == low:
                return c["id"]
        return cats[0]["id"]  # closest search hit

    def exact_category(self, name: str) -> int | None:
        """An existing category whose name matches exactly (case aside), or None. Never the
        'closest search hit': automatic publishing must not file a live post under a category
        nobody chose (a search for 'AI' also finds 'Al tools', spelled with a lower-case L)."""
        r = requests.get(f"{self.api}/categories",
                         params={"search": name, "per_page": 50}, auth=self.auth, timeout=60)
        r.raise_for_status()
        low = name.strip().lower()
        return next((c["id"] for c in r.json() if _cat_name(c) == low), None)

    def get_or_create_tag(self, name: str) -> int | None:
        try:
            r = requests.get(f"{self.api}/tags", params={"search": name, "per_page": 20},
                             auth=self.auth, timeout=60)
            r.raise_for_status()
            for t in r.json():
                if t["name"].strip().lower() == name.strip().lower():
                    return t["id"]
            r = requests.post(f"{self.api}/tags", json={"name": name}, auth=self.auth, timeout=60)
            if r.status_code in (200, 201):
                return r.json()["id"]
            if r.status_code == 400 and r.json().get("code") == "term_exists":
                return r.json()["data"]["term_id"]
        except Exception as e:  # noqa: BLE001 — tags are nice-to-have
            print(f"[wp] tag '{name}': {e}")
        return None

    # ---- main ----
    @classmethod
    def site_url(cls) -> str:
        """The WordPress site this install publishes to ("" when none is configured)."""
        return (os.getenv("WP_BASE_URL", "") or "").rstrip("/")

    @property
    def site(self) -> str:
        """The WordPress install's identity for a binding: scheme, host and the install's base
        path (https://example.test/site-a), no trailing slash. Scheme and host alone made two
        installs in subfolders of one host the same site (review RV-10)."""
        from urllib.parse import urlparse

        u = urlparse(self.base_url)
        if not u.netloc:
            return ""
        return f"{u.scheme}://{u.netloc}".lower() + u.path.rstrip("/")

    def _bind_site(self, state: dict, slug: str) -> None:
        """A post binding belongs to the site it was created on (RF-10): after the Studio was
        pointed at another WordPress, `post_id` 123 used to be updated THERE, where 123 is
        somebody else's post. A binding written before the site was recorded is adopted only
        when that post exists on this site under this article's slug."""
        if not state.get("post_id"):
            state["site"] = self.site
            return
        if state.get("site"):
            if state["site"] != self.site:
                raise NotSent(f"this article's WordPress post #{state['post_id']} is on {state['site']}, "
                                   f"and the Studio now publishes to {self.site}; nothing was sent")
            return
        r = requests.get(f"{self.api}/posts/{state['post_id']}", params={"context": "edit"},
                         auth=self.auth, timeout=60)
        if r.status_code == 200 and (r.json() or {}).get("slug") == slug:
            state["site"] = self.site
            return
        raise NotSent(f"this article's binding to post #{state['post_id']} predates site "
                           f"tracking and that post is not this article on {self.site}; nothing was sent")

    def find_by_slug(self, slug: str) -> dict | None:
        """The post with this slug on this site, in any status, or None. Raises when WordPress
        cannot be searched: "not found" must mean not found (core/publication: one post per
        article)."""
        r = requests.get(f"{self.api}/posts", params={"slug": slug, "status": "any", "context": "edit",
                                                      "per_page": 5}, auth=self.auth, timeout=60)
        if not _ok(r):
            raise RuntimeError(f"WordPress could not be searched for the post (HTTP {r.status_code})")
        return next((p for p in (r.json() or []) if isinstance(p, dict) and p.get("slug") == slug), None)

    def read_post(self, post_id) -> dict:
        """The post as WordPress has it now (status, modified time, link). Raises on any failure."""
        r = requests.get(f"{self.api}/posts/{post_id}", params={"context": "edit"}, auth=self.auth, timeout=60)
        if not _ok(r):
            raise RuntimeError(f"WordPress did not return post #{post_id} (HTTP {r.status_code})")
        return r.json() or {}

    def bind(self, article_dir: str | Path, post: dict) -> None:
        """Bind the article to an existing post of this site (one found by its slug)."""
        from ..store import read_json, write_json

        f = Path(article_dir) / "_wp.json"
        state = read_json(f, default=None) or {"media": {}}
        state.update(post_id=post["id"], link=post.get("link"), status=post.get("status"), site=self.site)
        write_json(f, state)

    def publish_article(self, article_dir: str | Path, category: str | None = None,
                        status: str | None = None, max_tags: int = 6) -> dict:
        """Create the post (as `status`, default draft) or update the one the article has.

        An update keeps the post's status unless `status` is given: the Studio's button sent
        status=draft on every update, which unpublished a live post (RF-11). Images are read
        only from the article's own assets/ folder (RF-04), uploaded again when their bytes
        changed and re-described when their alt text changed (RF-12). An FAQ removed from the
        article is removed from the post, and every write that matters is checked (RF-13); what
        could not be confirmed comes back in `warnings`."""
        from ..store import Article, parse_frontmatter, read_json

        d = Path(article_dir)
        art = Article(d.parent, d.name)
        if art.source is None:
            raise FileNotFoundError(f"no article.md in {d}")
        fm, body = parse_frontmatter(art.text())
        slug = fm.get("slug") or d.name
        title = fm.get("title") or slug.replace("-", " ").title()
        focus_kw = _focus_keyword(d, fm)
        warnings: list[str] = []

        state_file = d / "_wp.json"
        state = read_json(state_file, default=None) or {"media": {}}
        state.setdefault("media", {})
        self._bind_site(state, slug)
        assets_root = (d / "assets").resolve()
        counts = {"uploaded": 0, "reused": 0, "updated": 0}

        def _save_state():
            from ..store import write_json

            write_json(state_file, state)

        # 1) media: cover + inline images, only files inside the article's assets/
        def _upload(rel: str, alt: str) -> dict | None:
            import hashlib

            p = (d / rel).resolve()
            if assets_root not in p.parents or not p.is_file():
                warnings.append(f"skipped {rel}: not an image in this article's assets/")
                return None
            digest = hashlib.sha1(p.read_bytes()).hexdigest()[:16]
            cached = state["media"].get(rel)
            # an entry from before the hashes says nothing about which bytes WordPress holds, so
            # the picture is uploaded once more; taking today's bytes as the remote ones kept an
            # old chart on the post for good (review RV-09)
            if cached and cached.get("sha") == digest and cached.get("id"):
                if alt and cached.get("alt") != alt:
                    r = requests.post(f"{self.api}/media/{cached['id']}", json={"alt_text": alt},
                                      auth=self.auth, timeout=60)
                    if _ok(r):
                        cached["alt"] = alt
                        counts["updated"] += 1
                    else:
                        warnings.append(f"alt text of {rel} not updated (HTTP {r.status_code})")
                counts["reused"] += 1
                return cached
            try:
                info = self.upload_media(p, f"{slug}-{p.name}", alt=alt)
            except ValueError as e:
                warnings.append(f"skipped {rel}: {e}")
                return None
            info.update(sha=digest, alt=alt)
            if info.pop("alt_failed", False):
                warnings.append(f"alt text of {rel} not saved on WordPress")
            state["media"][rel] = info
            counts["uploaded"] += 1
            _save_state()
            return info

        cover_rel = fm.get("cover") if isinstance(fm.get("cover"), str) else None
        if not cover_rel and (d / "assets/cover.png").exists():
            cover_rel = "assets/cover.png"
        featured_id = None
        if cover_rel:
            info = _upload(cover_rel, alt=_cover_alt(title, focus_kw))
            featured_id = info["id"] if info else None
        # every picture the page shows, read with the page's own parser: a title, a reference-style
        # image, a query or an encoded name hid a picture from the old pattern, and it reached the
        # post as a broken relative address (found in phase 1 of the QA simplification)
        seen_refs: set[str] = set()
        for alt, rel in shown_image_refs(body):
            if rel not in seen_refs:
                seen_refs.add(rel)
                _upload(rel, alt=alt)

        # 2) SEO clean (single H1, no hr, unique links), render, then point each local picture at
        #    its upload; a local picture that could not be uploaded is left out, never sent broken
        body = _seo_clean_body(body)
        html_body = _uploaded_srcs(md_to_html(body), state["media"], warnings)

        # 3) schema.org/Table microdata on comparison tables
        html_body = html_body.replace("<table>", '<table itemscope itemtype="https://schema.org/Table">')

        # 4) taxonomy
        cat_ids = []
        if category:
            cid = self.match_category(category)
            if cid:
                cat_ids = [cid]
            else:
                # nothing is sent then: a new post gets no category, an existing one keeps its own
                warnings.append(f"category '{category}' not found on WordPress: "
                                + ("the post keeps the categories it has" if state.get("post_id")
                                   else "the post has none"))
        tag_ids = [tid for t in (fm.get("tags") or [])[:max_tags]
                   if (tid := self.get_or_create_tag(str(t)))]

        # 5) create or update — with Yoast meta (title 50-70 ch, description 130-150 ch)
        meta_desc = fm.get("meta_description") or fm.get("description") or ""
        payload = {"title": title, "content": html_body, "slug": slug, "excerpt": meta_desc,
                   "meta": {"_yoast_wpseo_title": fm.get("meta_title") or title,
                            "_yoast_wpseo_metadesc": meta_desc}}
        if status or not state.get("post_id"):
            payload["status"] = status or "draft"
        # Yoast scores nothing without a focus keyphrase; only written when we have one, so a
        # keyphrase a human set in wp-admin survives a re-publish
        if focus_kw:
            payload["meta"]["_yoast_wpseo_focuskw"] = focus_kw
        if self.author_id:
            payload["author"] = self.author_id
        if featured_id:
            payload["featured_media"] = featured_id
        if cat_ids:
            payload["categories"] = cat_ids
        if tag_ids:
            payload["tags"] = tag_ids
        faq_sent = None
        if "faq" in fm:     # an empty list clears the post's FAQ; no key at all leaves it alone
            faq_sent = [{"question": f.get("question", ""), "answer": f.get("answer", "")}
                        for f in (fm.get("faq") or []) if isinstance(f, dict)]
            payload["acf"] = {"faq": faq_sent}

        if state.get("post_id"):
            r = requests.post(f"{self.api}/posts/{state['post_id']}", json=payload, auth=self.auth, timeout=120)
            action = "updated"
        else:
            r = requests.post(f"{self.api}/posts", json=payload, auth=self.auth, timeout=120)
            action = "created"
        r.raise_for_status()
        post = r.json()
        state["post_id"] = post["id"]
        state["link"] = post.get("link")
        state["status"] = post.get("status")
        # the moment WordPress confirmed the post write: a failure after it (the FAQ read-back)
        # does not make the send's outcome unknown (core/publication)
        from datetime import datetime, timezone

        state["last_post_write"] = {"at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                                    "action": action, "status": post.get("status")}
        _save_state()

        # 6) verify the ACF FAQ persisted as sent (needs "Show in REST API" on the field group);
        #    if not, fall back to an FAQ section in the body so nothing is lost
        faq_status = "unchanged"
        if faq_sent is not None:
            chk = requests.get(f"{self.api}/posts/{post['id']}", params={"context": "edit"},
                               auth=self.auth, timeout=60)
            acf = (chk.json() or {}).get("acf") if _ok(chk) else None
            visible = isinstance(acf, dict) and "faq" in acf
            raw = acf.get("faq") if visible else None
            got = ([{"question": g.get("question", ""), "answer": g.get("answer", "")} for g in raw
                    if isinstance(g, dict)] if isinstance(raw, list) else [] if raw in (None, False, "") else raw)
            if not _ok(chk) or (not visible and not faq_sent):
                # an error, or an answer without the FAQ field, says nothing about what the post
                # holds: not "cleared", and not a reason to add a second FAQ to the body (RV-11)
                faq_status = "unverified"
                why = ("the read-back failed" if not _ok(chk) else
                       "the answer has no FAQ field; enable 'Show in REST API' on the ACF FAQ field group")
                warnings.append(f"the FAQ on the post could not be checked (HTTP {chk.status_code}): {why}")
            elif visible and got == faq_sent:
                faq_status = "acf" if faq_sent else "cleared"
            elif faq_sent:
                faq_html = "<h2>FAQ</h2>" + "".join(
                    f"<h3>{html.escape(f['question'])}</h3><p>{html.escape(f['answer'])}</p>" for f in faq_sent)
                fb = requests.post(f"{self.api}/posts/{post['id']}", json={"content": html_body + faq_html},
                                   auth=self.auth, timeout=120)
                if _ok(fb):
                    faq_status = "body-fallback (enable 'Show in REST API' on the ACF FAQ field group)"
                else:
                    faq_status = "not saved"
                    warnings.append(f"the FAQ was saved neither in ACF nor in the body (HTTP {fb.status_code})")
            else:
                faq_status = "not cleared"
                warnings.append("the FAQ was removed from the article but WordPress still shows the old one")

        return {"action": action, "post_id": post["id"], "status": post.get("status"),
                "link": post.get("link"),
                "edit_link": f"{self.base_url}/wp-admin/post.php?post={post['id']}&action=edit",
                "media_uploaded": counts["uploaded"], "media_reused": counts["reused"],
                "media_alt_updated": counts["updated"],
                "categories": cat_ids, "tags": len(tag_ids), "faq": faq_status,
                "focus_keyword": focus_kw, "warnings": warnings}
