"""Branded infographics — HTML/CSS templates rendered to PNG via Playwright.

Five typed templates (content must come from the article/research — the caller
guarantees that; this module only renders):

  flow      {"steps": [{"label", "desc"?}]}           — pipeline with arrows
  decision  {"branches": [{"if", "then", "detail"?}]} — decision guide rows
  cards     {"items": [{"title", "desc"}]}            — taxonomy grid
  checklist {"items": ["..."]}                        — numbered checklist
  stats     {"items": [{"value", "label"}]}           — big-number callouts

Common keys: type, title, subtitle?, source?.
Colours come from the project's brand palette (profile `brand.palette`, core/brand.py); the logo
sits in the footer.
"""
from __future__ import annotations

import base64
import html as _html
from pathlib import Path



def _esc(s) -> str:
    return _html.escape(str(s or ""))


def _body(spec: dict) -> str:
    t = spec.get("type")
    if t == "flow":
        steps = spec.get("steps") or []
        cells = []
        for i, s in enumerate(steps):
            cells.append(
                f'<div class="step"><div class="chip">{i+1}</div>'
                f'<div class="slabel">{_esc(s.get("label"))}</div>'
                + (f'<div class="sdesc">{_esc(s["desc"])}</div>' if s.get("desc") else "")
                + "</div>"
            )
        arrow = '<div class="arr">→</div>'
        return f'<div class="flow">{arrow.join(cells)}</div>'

    if t == "decision":
        rows = []
        for b in spec.get("branches") or []:
            rows.append(
                f'<div class="drow"><div class="dif"><span class="dtag">If</span> {_esc(b.get("if"))}</div>'
                f'<div class="darr">→</div>'
                f'<div class="dthen"><b>{_esc(b.get("then"))}</b>'
                + (f'<div class="ddet">{_esc(b["detail"])}</div>' if b.get("detail") else "")
                + "</div></div>"
            )
        return f'<div class="dec">{"".join(rows)}</div>'

    if t == "cards":
        items = spec.get("items") or []
        cells = []
        for it in items:
            if not isinstance(it, dict):
                it = {"title": str(it), "desc": ""}
            cells.append(f'<div class="card"><div class="ctitle">{_esc(it.get("title"))}</div>'
                         f'<div class="cdesc">{_esc(it.get("desc"))}</div></div>')
        return f'<div class="grid">{"".join(cells)}</div>'

    if t == "checklist":
        items = spec.get("items") or []
        rows = []
        for i, it in enumerate(items):
            if isinstance(it, dict):  # tolerate {label, desc} objects from the LLM
                text = _esc(it.get("label") or it.get("title") or "")
                if it.get("desc"):
                    text += f' <span class="cdim">· {_esc(it["desc"])}</span>'
            else:
                text = _esc(it)
            rows.append(f'<div class="crow"><div class="cnum">{i+1}</div><div class="ctext">{text}</div></div>')
        return f'<div class="check">{"".join(rows)}</div>'

    if t == "stats":
        items = spec.get("items") or []
        cells = [
            f'<div class="stat"><div class="sval">{_esc(it.get("value"))}</div>'
            f'<div class="slbl">{_esc(it.get("label"))}</div></div>'
            for it in items
        ]
        return f'<div class="stats">{"".join(cells)}</div>'

    raise ValueError(f"unknown infographic type: {t}")


def _css(pal: dict) -> str:
    INK, MUT, ACC, BLU, LINE, SOFT = (pal[k] for k in ("ink", "muted", "accent", "accent_2", "line", "soft"))
    return f"""
*{{box-sizing:border-box;margin:0}}
body{{background:#fff;font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
     -webkit-font-smoothing:antialiased;color:{INK}}}
#card{{width:1160px;padding:52px 56px 40px;background:#fff}}
.eyebrow{{font-size:13px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;color:{ACC};margin-bottom:12px}}
h1{{font-size:34px;line-height:1.15;font-weight:760;letter-spacing:-.015em;max-width:22ch}}
.sub{{margin-top:10px;font-size:16.5px;color:{MUT};max-width:60ch;line-height:1.5}}
.bodywrap{{margin-top:36px}}
.foot{{margin-top:40px;padding-top:18px;border-top:1px solid {LINE};display:flex;align-items:center;justify-content:space-between}}
.src{{font-size:12.5px;color:{MUT}}}
.logo{{height:22px}}

.flow{{display:flex;align-items:stretch;gap:14px}}
.step{{flex:1;background:{SOFT};border:1px solid {LINE};border-radius:14px;padding:20px 18px}}
.chip{{width:30px;height:30px;border-radius:999px;background:{ACC};color:#fff;font-weight:700;font-size:15px;
      display:flex;align-items:center;justify-content:center;margin-bottom:12px}}
.slabel{{font-size:17.5px;font-weight:680;line-height:1.25}}
.sdesc{{font-size:13.5px;color:{MUT};margin-top:7px;line-height:1.45}}
.arr{{align-self:center;font-size:24px;color:{MUT};flex:0 0 auto}}

.dec{{display:flex;flex-direction:column;gap:12px}}
.drow{{display:flex;align-items:center;gap:16px}}
.dif{{flex:5;background:{SOFT};border:1px solid {LINE};border-radius:12px;padding:16px 20px;font-size:16.5px;line-height:1.4}}
.dtag{{display:inline-block;background:#fff;border:1px solid {LINE};border-radius:6px;color:{MUT};
      font-size:11.5px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;padding:2px 7px;margin-right:8px}}
.darr{{font-size:22px;color:{MUT}}}
.dthen{{flex:4;background:#fff;border:2px solid {ACC};border-radius:12px;padding:16px 20px;font-size:16.5px;line-height:1.4}}
.ddet{{font-size:13.5px;color:{MUT};margin-top:5px;font-weight:400}}

.grid{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.card{{background:{SOFT};border:1px solid {LINE};border-radius:14px;padding:22px 24px;border-top:3px solid {ACC}}}
.ctitle{{font-size:18.5px;font-weight:700;line-height:1.25}}
.cdesc{{font-size:14.5px;color:{MUT};margin-top:8px;line-height:1.5}}

.check{{display:flex;flex-direction:column;gap:0}}
.crow{{display:flex;gap:16px;align-items:flex-start;padding:14px 4px;border-bottom:1px solid {LINE}}}
.crow:last-child{{border-bottom:none}}
.cnum{{flex:0 0 30px;width:30px;height:30px;border-radius:999px;background:{ACC};color:#fff;font-weight:700;
      font-size:15px;display:flex;align-items:center;justify-content:center;margin-top:1px}}
.ctext{{font-size:17px;line-height:1.5;padding-top:3px;font-weight:620}}
.cdim{{color:{MUT};font-weight:400;font-size:15px}}

.stats{{display:flex;gap:16px}}
.stat{{flex:1;background:{SOFT};border:1px solid {LINE};border-radius:14px;padding:26px 24px;text-align:left}}
.sval{{font-size:46px;font-weight:780;letter-spacing:-.02em;color:{BLU};line-height:1.05}}
.slbl{{font-size:14.5px;color:{MUT};margin-top:10px;line-height:1.45}}
"""


def render(spec: dict, out_path: str, logo_png: str | None = None, eyebrow: str = "",
           palette: dict | None = None) -> str | None:
    try:
        from ..brand import NEUTRAL_PALETTE
        from ..brand import default as _brand

        pal = {**NEUTRAL_PALETTE, **(palette or _brand().palette)}
        body = _body(spec)
        logo_tag = ""
        if logo_png and Path(logo_png).exists():
            b64 = base64.b64encode(Path(logo_png).read_bytes()).decode()
            logo_tag = f'<img class="logo" src="data:image/png;base64,{b64}">'
        sub = f'<div class="sub">{_esc(spec["subtitle"])}</div>' if spec.get("subtitle") else ""
        src = f'<span class="src">{_esc(spec["source"])}</span>' if spec.get("source") else "<span></span>"
        eyebrow_tag = f'<div class="eyebrow">{_esc(eyebrow)}</div>' if eyebrow else ""
        page = f"""<!doctype html><html><head><meta charset="utf-8"><style>{_css(pal)}</style></head>
<body><div id="card">{eyebrow_tag}<h1>{_esc(spec.get("title"))}</h1>{sub}
<div class="bodywrap">{body}</div>
<div class="foot">{src}{logo_tag}</div></div></body></html>"""

        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(device_scale_factor=2, viewport={"width": 1280, "height": 900})
            pg.set_content(page)
            pg.locator("#card").screenshot(path=out_path)
            b.close()
        return out_path
    except Exception as e:  # noqa: BLE001 — a visual never fails the article
        print(f"[infographic] error: {e}")
        return None
