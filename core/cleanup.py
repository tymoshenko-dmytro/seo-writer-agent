"""Clean up an article's pictures by the rules the operator set on 2026-09-28, in code, for free.

Articles made before those rules carry their breaches: screenshots of our own pages with no link
to the page, charts with nothing to plot, pictures with no text between them. This pass repairs
what can be repaired without a model or a browser and lists the rest:

  - a screenshot of our own page gets its caption linked to that page;
  - a chart whose spec has nothing to plot is taken out, with its caption;
  - of two pictures in a row, the second moves below the next paragraph of text;
  - a screenshot taller than a screen, and a missing cover, are listed for the Studio's
    Regenerate and Generate cover (they need a browser or an image call).

The text changes, so the article leaves READY until QA checks it again, as after any edit.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import store
from .brand import default as _brand
from .producers.charts import plotted_values

_PIC = re.compile(r"^!\[[^\]]*\]\((assets/[^)\s]+)\)")
_CAP = re.compile(r"^\*[^*\n].*\*$")
_ASSET_REF = re.compile(r"\]\((assets/[^)\s]+)\)")


def _made(d: Path, ref: str) -> dict | None:
    """What made the bytes now at `ref`, by its sidecar (its own record or the one kept under
    `made`): None when the sidecar does not know them (an upload, no sidecar)."""
    from .agents.interactive import _bytes_sha, made_record

    side = store.read_json_lenient((d / ref).with_suffix(".json"), None)
    return made_record(side, _bytes_sha(d / ref), d, ref)


def screenshot_urls(d: Path, md: str) -> dict:
    """The page each embedded screenshot shows: what made its bytes, when the sidecar knows (a
    picture made again as another kind keeps its file's name, and shows no page, R058-03); else the
    sidecar's spec, else the run's visuals record."""
    urls = {}
    run = store.read_json_lenient(d / "_run.json", {}) or {}
    for v in run.get("visuals") or []:
        if isinstance(v, dict) and v.get("type") == "screenshot" and v.get("path") and v.get("url"):
            urls[v["path"]] = v["url"]
    for ref in set(_ASSET_REF.findall(md)):
        rec = _made(d, ref)
        if rec is not None and rec.get("producer"):
            url = (rec.get("spec") or {}).get("url") if rec.get("producer") == "screenshot" else None
            if url:
                urls[ref] = url
            else:
                urls.pop(ref, None)
            continue
        side = store.read_json_lenient((d / ref).with_suffix(".json"), {}) or {}
        url = ((side.get("spec") or {}) if isinstance(side, dict) else {}).get("url")
        if url:
            urls[ref] = url
    return urls

def _blocks(body: str) -> list[str]:
    """Paragraph blocks, with a picture and its caption always a block of their own: some
    articles run the caption straight into the next paragraph ("*caption*\nCompare the…"), and
    moving that picture carried the paragraph with it."""
    out = []
    for b in re.split(r"\n\s*\n", body.strip("\n")):
        lines = b.split("\n")
        cur: list[str] = []
        k = 0
        while k < len(lines):
            ln = lines[k]
            if _PIC.match(ln.strip()):
                if cur:
                    out.append("\n".join(cur))
                    cur = []
                pic = [ln]
                if k + 1 < len(lines) and _CAP.match(lines[k + 1].strip()):
                    pic.append(lines[k + 1])
                    k += 1
                out.append("\n".join(pic))
            else:
                cur.append(ln)
            k += 1
        if cur:
            out.append("\n".join(cur))
    return out


def _kind(block: str) -> str:
    first = block.strip().splitlines()[0] if block.strip() else ""
    if not first:
        return "blank"
    if first.startswith("#"):
        return "heading"
    if _PIC.match(first):
        return "picture"
    if all(_CAP.match(ln.strip()) for ln in block.strip().splitlines()):
        return "caption"
    return "text"


def _link_own_captions(d: Path, blocks: list[str], md: str, brand, changes: list[str]) -> None:
    urls = screenshot_urls(d, md)
    for i, b in enumerate(blocks):
        m = _PIC.match(b.strip())
        if not m or not brand.is_own(urls.get(m.group(1), "")):
            continue
        url = urls[m.group(1)]
        lines = b.strip().splitlines()
        alt = re.match(r"^!\[([^\]]*)\]", lines[0]).group(1)
        cap_i = 1 if len(lines) > 1 and _CAP.match(lines[1].strip()) else None
        cap = lines[1].strip()[1:-1] if cap_i else alt
        if f"]({url})" in cap:
            continue
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cap)       # an older link to somewhere else
        new_cap = f"*{brand.caption_for(text, url)}*"
        lines = [lines[0], new_cap] + lines[2 if cap_i else 1:]
        blocks[i] = "\n".join(lines)
        changes.append(f"linked the caption of {m.group(1)} to {url}")


def _drop_empty_charts(d: Path, blocks: list[str], changes: list[str]) -> list[str]:
    out = []
    skip_caption = False
    for b in blocks:
        m = _PIC.match(b.strip())
        # a chart by what made its bytes, not by its file's name (R058-03: a chart made again as
        # a screenshot keeps chart_1.png, and has no numbers to plot); a sidecar that names no
        # producer (an old one) goes by the name, as before
        rec = (_made(d, m.group(1)) or {}) if m else {}
        if m and (rec.get("producer") == "chart" if rec.get("producer") else "chart_" in m.group(1)):
            spec = rec.get("spec")
            if isinstance(spec, dict) and not plotted_values(spec):
                changes.append(f"took out {m.group(1)}: a chart with nothing to plot")
                skip_caption = len(b.strip().splitlines()) == 1
                continue
        if skip_caption and _kind(b) == "caption":
            skip_caption = False
            continue
        skip_caption = False
        out.append(b)
    return out


def _pairs(blocks: list[str]) -> list[tuple[int, int]]:
    """(first, second) block indexes of two pictures with no text between them: a caption or a
    heading does not separate them (no two pictures in a row)."""
    out, last = [], None
    for i, b in enumerate(blocks):
        k = _kind(b)
        if k == "picture":
            if last is not None:
                out.append((last, i))
            last = i
        elif k not in ("caption", "heading", "blank"):
            last = None
    return out


def _group(blocks: list[str], i: int) -> list[int]:
    """A picture's block and its caption's, when the caption stands on its own."""
    return [i] + ([i + 1] if i + 1 < len(blocks) and _kind(blocks[i + 1]) == "caption" else [])


def _moves(blocks: list[str], first: int, second: int):
    """The moves that could separate one pair, inside the section: the second picture below the
    paragraph right after it, else the first above the paragraph right before it."""
    g = _group(blocks, second)
    j = g[-1] + 1
    if j < len(blocks) and _kind(blocks[j]) == "text":
        yield (blocks[:g[0]] + [blocks[j]] + [blocks[x] for x in g] + blocks[j + 1:],
               f"moved {_PIC.match(blocks[second].strip()).group(1)} below the next paragraph")
    g = _group(blocks, first)
    t = g[0] - 1
    if t >= 0 and _kind(blocks[t]) == "text":
        yield (blocks[:t] + [blocks[x] for x in g] + [blocks[t]] + blocks[g[-1] + 1:],
               f"moved {_PIC.match(blocks[first].strip()).group(1)} above the paragraph before it")


def _separate_pictures(blocks: list[str], changes: list[str], left: list[str]) -> list[str]:
    """Separate pictures that stand together by moving one past a paragraph of its own section.
    A move is made only when it leaves fewer such pairs than before: moving a picture from one
    neighbour into another undid the last move, and three pictures moved back and forth until a
    guard stopped them (review R499-06). So it ends, and a second run changes nothing; what no
    move can separate is listed for the person."""
    while True:
        pairs = _pairs(blocks)
        best = next(((new, what) for a, b in pairs for new, what in _moves(blocks, a, b)
                     if len(_pairs(new)) < len(pairs)), None)
        if best is None:
            break
        blocks, what = best
        changes.append(what)
    for a, b in _pairs(blocks):
        left.append(f"{_PIC.match(blocks[b].strip()).group(1)} stands right after "
                    f"{_PIC.match(blocks[a].strip()).group(1)}: no paragraph of their section can go between them")
    return blocks


def clean_pictures(art: store.Article) -> dict:
    """Repair what code can in `art`'s pictures, under the article's lock. Returns
    {"changes": [...], "left": [...]} and writes the article only when something changed."""
    from . import versions

    brand = _brand()
    with art.lock(wait=False, holder="Clean up pictures"), \
            versions.change(art, "the Studio", "Pictures cleaned up", kind="picture"):
        md = art.text()
        block, body = store.split_frontmatter(md)
        d = art.dir
        changes: list[str] = []
        left: list[str] = []
        blocks = _blocks(body)
        _link_own_captions(d, blocks, md, brand, changes)
        blocks = _drop_empty_charts(d, blocks, changes)
        blocks = _separate_pictures(blocks, changes, left)
        new_body = "\n\n".join(blocks) + "\n"
        from PIL import Image

        for ref in sorted(set(_ASSET_REF.findall(new_body))):
            from .agents.interactive import picture_kind

            if (d / ref).is_file() and picture_kind(d, ref) == "screenshot":
                try:
                    with Image.open(d / ref) as im:
                        w, h = im.size
                    if h > w * 0.8:
                        left.append(f"{ref} is {w}×{h}, taller than a screen: Regenerate it")
                except OSError:
                    pass
        if not store.frontmatter_fields(block).get("cover"):
            left.append("no cover: Generate cover")
        if changes:
            art.write_article(block, new_body)
            run = art.run()
            if run:
                run.setdefault("log", []).append("[cleanup] " + "; ".join(changes))
                art.save_run(run)
    return {"changes": changes, "left": left}


def describe(res: dict) -> str:
    parts = [f"{len(res['changes'])} change(s)" + (": " + "; ".join(res["changes"]) if res["changes"] else "")]
    if res["left"]:
        parts.append("still to do: " + "; ".join(res["left"]))
    return ". ".join(parts)
