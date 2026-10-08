"""The article's own fields and pictures, changed by a person or their agent: the SEO tab's Save,
a new cover, a picture made again. The page and `cli.py`
call these, under the article's lock, each change a version of its own (core/versions.py). Each
takes the version the page showed and checks it under the lock before it writes or calls a model:
a page older than the article changes nothing (review RB32-02)."""
from __future__ import annotations

import io
from pathlib import Path

from . import store, usage, versions

UPLOAD_MAX = 15 * 1024 * 1024        # bytes of a picture a person may put in
_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}


class BadField(ValueError):
    """A field the article cannot take as given (a slug that is not one)."""


def _check(art: store.Article, version: str) -> None:
    from .qa_changes import check_version

    check_version(art, version)         # the one rule, with the version a queued job follows


def save_seo(art: store.Article, version: str, *, meta_title: str, meta_description: str, faq: list[dict],
             slug: str | None = None, focus_keyword: str | None = None, who: str = "the SEO tab") -> dict:
    """The meta tags, the slug, the focus keyword and the FAQ written into the frontmatter (a
    version). The brief is updated in the same write: the review reads the meta from there, and
    two views of one fact drift. `slug`/`focus_keyword` None leave them as they are."""
    title = " ".join(str(meta_title or "").split())
    desc = " ".join(str(meta_description or "").split())
    faq = [{"question": str(q.get("question") or "").strip(), "answer": " ".join(str(q.get("answer") or "").split())}
           for q in faq or [] if str(q.get("question") or "").strip()]
    fields = [("meta_title", title), ("meta_description", desc), ("description", desc), ("faq", faq)]
    if slug is not None and slug.strip():
        if slug.strip() != store.slugify(slug.strip()):
            raise BadField(f"a slug is lower-case words joined by hyphens (“{store.slugify(slug.strip())}”)")
        fields.append(("slug", slug.strip()))
    if focus_keyword is not None:
        fields.append(("focus_keyword", " ".join(focus_keyword.split())))
    with art.lock(wait=False, holder="an SEO edit"):
        _check(art, version)
        block, body = store.split_frontmatter(art.path("article.md").read_text(encoding="utf-8"))
        for key, value in fields:
            block = store.set_frontmatter_field(block, key, value)
        art.write_article(block, body, who=who, what="SEO saved", kind="seo")
        run = art.run()
        if run:
            brief = run.get("brief") or {}
            brief["meta_title"], brief["meta_description"] = title, desc
            brief["faq"] = [q["question"] for q in faq]
            run["brief"] = brief
            art.save_run(run)
    return {"version": art.version(), "fields": [k for k, _ in fields]}


def new_cover(art: store.Article, version: str, config: dict, project, who: str = "the Studio") -> dict:
    """A new cover (the WordPress featured image), drawn on the article's headline: one image
    call, a version of its own; the old cover stays in the versions."""
    with art.lock(wait=False, holder="a new cover"):
        _check(art, version)
        with usage.recording(art.dir, "cover", config=config), usage.stage("visuals"), \
                versions.change(art, who, "New cover", kind="picture"):
            _draw_cover(art, config, project)
    return {"version": art.version(), "cover": "assets/cover.png"}


def _draw_cover(art: store.Article, config: dict, project) -> None:
    """The pipeline's cover step on the article's headline, then the frontmatter names it."""
    from .agents.visual_director import build_cover

    block, body = store.split_frontmatter(art.text())
    fm = store.frontmatter_fields(block)
    run = store.read_json_lenient(art.path("_run.json"), {}) or {}
    headline = ((run.get("keyword_research") or {}).get("primary_keyword")) or fm.get("title") or run.get("topic") or art.slug
    built, why = build_cover(project, config, art.dir, str(headline),
                             str(run.get("topic") or fm.get("title") or headline), fresh=True)
    if not built:
        raise RuntimeError(f"no cover: {why}")
    if fm.get("cover") != "assets/cover.png":
        art.write_article(store.set_frontmatter_field(block, "cover", "assets/cover.png"), body)


_INLINE_KIND = ("nothing changed: a picture inside a sentence or a table keeps its words, and another kind "
                "needs new ones. Put it on a line of its own first")


def redraw(art: store.Article, version: str, asset: str, config: dict, project, feedback: str = "",
           who: str = "the Studio", as_image: bool = False, kind: str | None = None) -> dict:
    """One picture made again (a chart or an infographic redrawn, a screenshot re-captured): a
    model or browser call, a version of its own; the old bytes stay in the versions."""
    from .agents.interactive import VisualProducerService

    from . import qa_changes as qc
    from .tools.markdown_render import parse

    if store.local_asset(asset) != asset:
        raise BadField(f"'{asset}' is not one of the article's pictures")
    with art.lock(wait=False, holder="a picture made again"):
        _check(art, version)
        # the person's instruction and the review's word on this picture, both: an instruction
        # replaced the word, and "redraw it considering the editor's feedback" told the producer
        # nothing (2026-10-06); with no instruction, the review's word alone
        said, problem = " ".join(str(feedback or "").split())[:500], qc.picture_problem(art, asset)[:500]
        feedback = (f"{said}\nWHAT THE REVIEW SAYS ABOUT THIS PICTURE: {problem}" if said and problem
                    else said or problem)
        block, body = store.split_frontmatter(art.text())
        alt = next((c.content for t in parse(body) for c in (t.children or [])
                    if c.type == "image" and store.local_asset(c.attrGet("src")) == asset), "")
        png, sidecar = art.path(asset), art.path(asset).with_suffix(".json")
        was = (png.read_bytes(), sidecar.read_bytes() if sidecar.is_file() else None)
        # another kind needs its own words, and only a picture on a line of its own takes them: one
        # inside a sentence or a table, however it is written there (a title, a reference), keeps
        # its words (R9F0-01, RA3C-01). A kind picked for it is refused before the paid call
        alone = store.picture_line(body, asset) is not None
        picked = "image" if as_image else None if kind in (None, "auto") else kind
        if picked and not alone:
            from .agents.interactive import picture_kind

            now = picture_kind(art.dir, asset)
            if picked != ("image" if now in ("illustration", "photo") else now):
                raise RuntimeError(_INLINE_KIND)
        with usage.recording(art.dir, "visual_edit", label=asset, config=config), usage.stage("studio"), \
                versions.change(art, who, "Picture made again", asset, kind="picture") as box:
            res = VisualProducerService(project, config).edit_visual(art.dir, asset, feedback, alt=alt,
                                                                      **({"as_image": True} if as_image else {}),
                                                                      **({"kind": kind} if kind else {}))
            def put_back():
                png.write_bytes(was[0])
                if was[1] is None:
                    sidecar.unlink(missing_ok=True)
                else:
                    sidecar.write_bytes(was[1])

            if res.get("ok") and res.get("changed_kind") and not alone:
                # the producer chose another kind (auto) for a picture whose words cannot change:
                # the old picture back and nothing changed (RA3C-01)
                put_back()
                raise RuntimeError(_INLINE_KIND)
            if res.get("ok"):
                # the new picture's own alt text and caption: a person's Re-capture showed the page's
                # header under "API documentation: quickstart, endpoints…" (2026-10-05)
                try:
                    new_body = qc.retitle_picture(body, asset, res.get("alt") or "", res.get("caption") or "")
                except qc.Refused as e:
                    if alone:
                        # a picture on a line of its own whose new words cannot be written with it:
                        # the old picture back and nothing changed, not new pixels under the old
                        # words reported as made (R83D-02)
                        put_back()
                        raise RuntimeError(f"the new picture could not be written with its words ({e}): "
                                           f"nothing changed") from e
                    new_body = body                 # a picture inside a sentence or a table keeps its words
                if new_body != body:
                    art.write_article(block, new_body, exact=True)
        # a Redo that gave the same picture and words made no version and answers nothing (R058-09)
        if res.get("ok") and box.get("version"):
            run = art.run()
            if run.get("review"):
                qc.answered_by_person(art, run, run["review"], asset, who, "made again",
                                      version_n=box["version"]["n"])
    if res.get("error"):
        raise RuntimeError(res["error"])
    return {"version": art.version(), "asset": asset}


def upload_picture(art: store.Article, version: str, asset: str, data: bytes, who: str = "the Studio") -> dict:
    """The person's own picture in place of one the article shows, or of the cover (the operator,
    2026-10-05: not only make it again). Free, a version of its own; the old bytes stay in the
    versions. It keeps the picture's file name, so the text, its alt text and its caption stay as
    they are, and it is written in that file's format whatever the upload's (a PNG, a JPEG or a
    WebP read as an image; anything else is refused before anything is written)."""
    from PIL import Image, UnidentifiedImageError

    if store.local_asset(asset) != asset or Path(asset).suffix.lower() not in _FORMATS:
        raise BadField(f"'{asset}' is not one of the article's pictures")
    if not data:
        raise BadField("choose a picture file first")
    if len(data) > UPLOAD_MAX:
        raise BadField(f"a picture up to {UPLOAD_MAX // (1024 * 1024)} MB")
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in ("PNG", "JPEG", "WEBP"):
                raise BadField("a PNG, a JPEG or a WebP picture")
            im.load()
            fmt = _FORMATS[Path(asset).suffix.lower()]
            out = im.convert("RGB") if fmt == "JPEG" or im.mode not in ("RGB", "RGBA") else im.copy()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError, ValueError) as e:
        if isinstance(e, BadField):
            raise
        raise BadField("the file is not a picture this Studio can read: a PNG, a JPEG or a WebP") from None
    buf = io.BytesIO()
    out.save(buf, fmt)
    with art.lock(wait=False, holder="your picture"):
        _check(art, version)
        if asset not in art.pictures():
            raise BadField(f"'{asset}' is not one of the pictures the article shows")
        target = art.own_file(asset)
        with versions.change(art, who, "Your picture", asset, kind="picture") as box:
            store.atomic_write_bytes(target, buf.getvalue())
        from . import qa_changes as qc

        run = art.run()
        # the same picture again made no version and answers nothing (R058-09)
        if run.get("review") and box.get("version"):
            qc.answered_by_person(art, run, run["review"], asset, who, "your picture",
                                  version_n=box["version"]["n"])
    return {"version": art.version(), "asset": asset}

