"""Looks at every picture of the finished article, the way a reader will see it.

Until 2026-09-30 nothing in the pipeline looked at a rendered picture: the capture-time checks read
the page's DOM, the chart producer checked its numbers, and the review read captions and alt
text. So an empty chart ("Feature Comparison (Removed)"), a support page captured 11 000 px tall
and a block of customer testimonials under a caption about the product all reached the Studio as
finished. One vision call per picture now judges it with its caption and the text around it
(prompts/visual_checker.md); what it finds joins the review's findings, so the fixer redraws or
removes the picture and the checklist shows it like any other finding.

A call that fails says so and finds nothing: a picture is never reported broken because the
check could not run.

The answer is data, not an instruction (review R499-05): the model names a defect and an action
from fixed lists, and the code writes the fix, always about the one file it showed the model. A
problem that talks about another file, or an answer outside the lists, is dropped. A screenshot of
someone else's page can carry text written at a reviewer; the prompt says so, and whatever the
model makes of it, it can only ask to redraw, re-capture or remove the picture it was shown.
Removing is never automatic: the finding waits for a person, whose Fix removes it (R499-04).
"""
from __future__ import annotations

import base64
import hashlib
import io
import re
from pathlib import Path

from .base import BaseAgent

_CAPTION = re.compile(r"^\*([^*\n].*)\*$")
_KINDS = {"chart": "chart", "infographic": "infographic", "screenshot": "screenshot of a web page",
          "image": "illustration", "cover": "cover image (the headline over a photo)"}
MAX_PICTURES = 24
COLUMN_PX, SCREEN_PX = 720, 800      # the blog's text column and one laptop screen of it
SEVERITIES = ("Critical", "Major", "Minor")
DEFECTS = ("broken", "off_caption", "unreadable", "too_long", "covered", "contradicts",
           "cover_text", "cover_photo", "flaw")
# what each kind of picture can be asked for: a cover is recomposed, never removed (every article
# has one); a screenshot is captured again, a drawn picture redrawn
ACTIONS = {"cover": ("recompose",), "screenshot": ("recapture", "remove")}
DRAWN = ("redraw", "remove")
_SAME = {"redraw": "recapture", "recapture": "redraw"}
# a file named in the prose, with its folder or without ("chart_2.png", "chart_2"), the way the
# fixer's own routing reads a name (review R3B-03)
_ASSET = re.compile(r"(?i)(?:assets/)?\b((?:chart|screenshot|infographic|image)[ _-]?\d+(?:_v\d+)?|cover(?:_bg)?(?=\.))"
                    r"(?:\.(?:png|jpe?g|webp|svg))?\b")


def _plain(text: str, limit: int) -> str:
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text or "")
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = " ".join(t.split())
    return t if len(t) <= limit else t[:limit - 1] + "…"


def words_of(pic: dict) -> str:
    """The words a look reads with a picture (its alt text and its caption) as one short key: the
    look's word holds for these words only (RA3C-02: a Restore of old words on the same pixels
    took a later look's pass)."""
    return hashlib.sha1(f"{pic.get('alt') or ''}\n{pic.get('caption') or ''}".encode("utf-8")).hexdigest()[:16]


def pictures(body: str, cover: str | None = None, article_dir=None) -> list[dict]:
    """Every picture the article shows: path, kind, alt, caption, and the text before and after.
    Found with the page's own parser (as store.shown_image_refs reads it), so a picture with a title, a
    reference-style one or one with a query in its address is looked at too; each is placed in
    the text at its `![` within its block, for the caption under it and the text around it."""
    from ..store import cover_asset, local_asset
    from ..tools.markdown_render import parse

    body = body or ""
    out = []
    c = cover_asset({"cover": cover}, None) if cover else None
    if c:
        out.append({"path": c, "kind": "cover", "alt": "", "caption": "", "before": "", "after": ""})
    starts = [0]
    for line in body.split("\n"):
        starts.append(starts[-1] + len(line) + 1)
    at = 0
    for t in parse(body):
        if t.type != "inline" or not t.map or not any(ch.type == "image" for ch in t.children or []):
            continue
        block_end = starts[min(t.map[1], len(starts) - 1)]
        at = max(at, starts[t.map[0]])
        for ch in t.children:
            if ch.type != "image" or not local_asset(ch.attrGet("src")):
                continue
            alt, path = ch.content or "", local_asset(ch.attrGet("src"))
            pos = body.find("![", at, block_end)
            pos = at if pos < 0 else pos
            at = pos + 2
            line_end = body.find("\n", pos)
            line_end = len(body) if line_end < 0 else line_end
            rest = body[line_end:]
            line = rest.lstrip("\n").split("\n", 1)[0].strip() if rest.startswith("\n") else ""
            cap = _CAPTION.match(line)
            name = path.rsplit("/", 1)[-1]
            # what made it, by its sidecar, when the article's folder is known: a picture made again
            # as another kind keeps its file's name (the operator, 2026-10-07)
            if article_dir is not None:
                from .interactive import picture_kind

                made_as = picture_kind(article_dir, path)
                made_as = "image" if made_as in ("illustration", "photo") else made_as
            else:
                made_as = name.split("_")[0]
            out.append({"path": path, "kind": _KINDS.get(made_as, "picture"), "made_as": made_as,
                        "alt": alt, "caption": _plain(cap.group(1), 300) if cap else "",
                        "before": _plain(body[max(0, pos - 900):pos], 600),
                        "after": _plain(rest[len(line) + 1 if cap else 0:][:900], 600)})
    return out[:MAX_PICTURES]



def _clip(text: str, n: int) -> str:
    """`text` cut to at most `n` characters at a sentence's end, else at a word, with "…". A cut
    in the middle of a word ran into the next part: "…so t The new one must show:" (2026-10-06)."""
    if len(text) <= n:
        return text
    head = text[:n]
    end = max(head.rfind(". "), head.rfind("; "))
    if end > n // 2:
        return head[:end + 1]
    return head[:head.rfind(" ")].rstrip(",;:") + "…" if " " in head else head + "…"

class VisualChecker(BaseAgent):
    name = "visual_checker"
    model_role = "worker"

    def check_one(self, article_dir, pic: dict) -> dict:
        """{"path", "ok", "problems": [...], "error", "verdict"}: one vision call on one picture.

        `verdict` is what the answer establishes: "right" (the model said ok, named no problem and
        nothing had to be dropped), "problems" (it named a problem the contract accepts) or
        "unclear" (a refusal, a call that failed, an answer outside the contract, or one whose
        every problem was dropped). Only "right" may confirm a fix (review R9AD-01); `ok` stays
        "nothing to report" for the review, where an unclear look finds nothing."""
        f = Path(article_dir) / pic["path"]
        res = {"path": pic["path"], "ok": True, "problems": [], "error": None, "verdict": "unclear",
               # what made it, carried to as_findings, which checks the answer again (R9F0-03)
               **({"made_as": pic["made_as"]} if pic.get("made_as") else {}),
               "words": words_of(pic)}
        try:
            from PIL import Image

            with Image.open(f) as im:
                w, h = im.size
                im = im.convert("RGB")
                im.thumbnail((1568, 1568))          # the model's own working size: fewer tokens
                buf = io.BytesIO()
                im.save(buf, format="JPEG", quality=85)
            img = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                               "data": base64.b64encode(buf.getvalue()).decode()}}
            # the size in the reader's terms: scaled to a thumbnail, an 11 000 px page looks like
            # a thin strip and the model called it fine (test run, zoom screenshot_5)
            shown_h = round(h * COLUMN_PX / w) if w else 0
            size = (f"SIZE: {w}×{h} px. In the blog's {COLUMN_PX} px column it is {shown_h} px tall, "
                    f"about {shown_h / SCREEN_PX:.1f} screens.")
            text = (f"FILE: {pic['path']} — a {pic['kind']}\n{size}\n"
                    f"ALT TEXT: {pic.get('alt') or '(none)'}\n"
                    f"CAPTION UNDER IT: {pic.get('caption') or '(none)'}\n"
                    f"TEXT BEFORE IT: {pic.get('before') or '(start of the article)'}\n"
                    f"TEXT AFTER IT: {pic.get('after') or '(end of the article)'}\n\n"
                    "Judge this picture. Return ONLY the JSON.")
            data, _ = self.call_claude_json([img, {"type": "text", "text": text}],
                                            system=self.system_prompt(), max_tokens=3000)
        except Exception as e:  # noqa: BLE001 — a check that cannot run finds nothing
            res["error"] = f"{type(e).__name__}: {str(e)[:160]}"
            return res
        if not isinstance(data, dict):
            res["error"] = "the answer did not parse"
            return res
        raw = data.get("problems")
        res["problems"], res["dropped"] = self.accept(pic, raw)
        res["ok"] = not res["problems"]
        if res["problems"]:
            res["verdict"] = "problems"
        elif data.get("ok") is True and raw in (None, []) and not res["dropped"]:
            res["verdict"] = "right"
        return res

    @staticmethod
    def accept(pic: dict, problems) -> tuple[list[dict], list[str]]:
        """The problems the answer may raise about THIS picture: a severity, a defect and an action
        from the fixed lists, the action one this kind of picture allows, and prose that names no
        other file. What does not fit is dropped and said why."""
        # what made it (R058-05), else its file's name: cover.png, chart_2.png
        kind = pic.get("made_as") or re.split(r"[_.]", pic["path"].rsplit("/", 1)[-1])[0]
        allowed = ACTIONS.get(kind, DRAWN)
        out, dropped = [], []
        for p in problems if isinstance(problems, list) else []:
            if not isinstance(p, dict):
                dropped.append("not an object")
                continue
            sev = str(p.get("severity") or "").strip().capitalize()
            defect = str(p.get("defect") or "").strip().lower()
            action = str(p.get("action") or "").strip().lower()
            action = ("recompose" if kind == "cover" and action in ("redraw", "recapture", "recompose", "remove")
                      else action if action in allowed else _SAME.get(action, ""))
            problem = _clip(" ".join(str(p.get("problem") or "").split()), 300)
            what = _clip(" ".join(str(p.get("what") or "").split()), 300)
            own = pic["path"].rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
            others = {n for n in (re.sub(r"[ -]", "_", a.lower()) for a in _ASSET.findall(problem + " " + what))
                      if n != own}
            why = ("severity" if sev not in SEVERITIES else "defect" if defect not in DEFECTS
                   else "action" if action not in allowed else "no problem stated" if not problem
                   else f"names another file ({', '.join(sorted(others))})" if others else None)
            if why:
                dropped.append(why)
                continue
            out.append({"severity": sev, "defect": defect, "action": action, "problem": problem, "what": what})
        return out, dropped

    def check(self, article_dir, body: str, cover: str | None = None) -> list[dict]:
        return [self.check_one(article_dir, p) for p in pictures(body, cover, article_dir)
                if (Path(article_dir) / p["path"]).is_file()]

    @staticmethod
    def fix_text(path: str, p: dict) -> str:
        """The instruction the fixer gets, written here, about the file that was looked at."""
        what = f" The new one must show: {p['what']}" if p.get("what") else ""
        return {"redraw": f"Redraw {path}: {p['problem']}{what}",
                "recapture": f"Re-capture {path}: {p['problem']}{what}",
                "recompose": f"Recompose {path}: {p['problem']}{what}",
                "remove": f"Remove {path} and its caption: {p['problem']}"}[p["action"]]

    @staticmethod
    def as_findings(results: list[dict], sections: dict | None = None) -> list[dict]:
        """The problems as review findings: the fix names the file, so a change sends it to the
        producer. A removal is the person's choice (qa_model routes `remove_picture` to them)."""
        out = []
        for r in results:
            # checked again here: whatever built `results`, only an answer inside the contract
            # becomes an instruction to the fixer
            for p in VisualChecker.accept({"path": r["path"], "made_as": r.get("made_as")}, r.get("problems"))[0]:
                out.append({"fix": VisualChecker.fix_text(r["path"], p),
                            "severity": p["severity"], "check": "visuals", "source": "vision",
                            "voice": "visual check", "section": (sections or {}).get(r["path"]),
                            "asset": r["path"], "action": p["action"]})
        return out
