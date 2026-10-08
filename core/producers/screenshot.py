"""Website / product screenshots via Playwright (headless Chromium, sync API).

Defaults tuned for in-article product shots: 1440×900 viewport, device_scale_factor=2
(retina-crisp), animations disabled, common cookie/consent overlays hidden, short
settle wait. `selector` captures one element; `full_page=True` captures the whole
scroll height. Returns the path or None — a failed screenshot must never fail the
article (the caller drops the visual).

A capture that shows the wrong thing is refused before any file is written: a bot challenge, a
cookie wall the hide-CSS missed, a modal overlay, a login page, an empty page or an HTTP error.
Pass `diag={}` to learn why (`diag["reason"]` is one of `REASONS`, `diag["detail"]` says what was
seen); the return value stays "path or None" for every existing caller.

One-time setup: `.venv/bin/playwright install chromium`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Why a capture was refused. One vocabulary for the producer, the visuals manifest and the vision
# check, so a failure carries the same name wherever it is reported. WRONG_LANGUAGE is produced by
# the page-language check.
REASONS = ("CHALLENGE", "COOKIE_WALL", "OVERLAY", "LOGIN", "BLANK", "WRONG_LANGUAGE", "ERROR")

_CHALLENGE_TITLE = re.compile(
    r"just a moment|verify you are human|performing security verification|attention required"
    r"|checking your browser|access denied|are you a robot|security check|bot verification", re.I)
_LOGIN_URL = re.compile(
    r"accounts\.google\.com|//(login|signin|auth|sso)\.|/(log-?in|sign-?in|auth|sso)(/|\?|#|$)", re.I)
_MIN_TEXT = 80   # characters of visible text below which a page did not render

# One DOM pass: a challenge widget, the amount of visible text, and the largest visible dialog or
# fixed layer covering a real share of the viewport. It runs AFTER the hide-CSS, so a consent
# banner it still finds is one the CSS missed (the DeepL wall: HTTP 200, a normal title).
_PAGE_PROBE = r"""() => {
  const vw = innerWidth, vh = innerHeight, area = vw * vh;
  const text = ((document.body && document.body.innerText) || '').trim();
  // A captcha WIDGET alone is not a challenge page - plenty of normal pages carry one in a
  // newsletter or comment form (measured: digiday). It counts only on a page with almost no other
  // text. The interstitial's own wording counts anywhere: an in-page challenge modal sits over a
  // full page of text (measured: the DeepL translator), and "Verify you are human" itself lives
  // inside the cross-origin widget iframe, invisible to innerText.
  const widget = !!document.querySelector(
    'iframe[src*="challenges.cloudflare.com"], .cf-turnstile, #challenge-form, #cf-challenge-running,'
    + ' iframe[src*="hcaptcha.com"], iframe[src*="recaptcha"]');
  const phrase = /checking if the connection is secure|performing security verification|security service to protect against malicious bots|checking your browser|verify you are human|complete the security check/i.test(text);
  const challenge = phrase || (widget && text.length < 600);
  const rgba = (c) => { const m = /rgba?\(([^)]+)\)/.exec(c || ''); if (!m) return [0, 0, 0, 0];
                        const v = m[1].split(',').map(parseFloat); return [v[0], v[1], v[2], v.length > 3 ? v[3] : 1]; };
  let best = null;
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) continue;
    const dialog = el.matches('[role=dialog],[role=alertdialog],[aria-modal=true],dialog[open]');
    if (!dialog && cs.position !== 'fixed') continue;
    const r = el.getBoundingClientRect();
    const w = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
    const h = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
    const share = (w * h) / area;
    if (share < 0.25) continue;
    const t = (el.innerText || '').slice(0, 3000);
    const consent = /cookie|consent|privacy (preferences|settings|choices)|we use (cookies|technologies)/i;
    // A consent wall opens with its notice and carries little else; a docs sidebar or a mega-header
    // carries the word deep inside a long link list ("Cookie Policy", "Manage cookies"). Outside a
    // dialog only the first shape counts - docs and pricing pages are what the pipeline shoots.
    // and a real wall always carries an accept/reject control, which nav chrome never does
    const action = /\b(accept|agree|allow|reject|decline|got it|i understand|ok)\b/i;
    const cookie = dialog ? consent.test(t)
                          : (consent.test(t.slice(0, 200)) && t.length < 800 && action.test(t));
    // A translucent layer over (nearly) the whole viewport is a modal backdrop when it DIMS the
    // page (dark) or takes clicks, whether or not the modal is its child. Effective alpha includes
    // the element's own opacity (backdrop: #000 at opacity .5). Pointer events alone are not a
    // tell: the NewsGuard consent backdrop has pointer-events:none and still greys the page.
    const [cr, cg, cb, ca] = rgba(cs.backgroundColor);
    const a = ca * parseFloat(cs.opacity || '1');
    const dark = (0.299 * cr + 0.587 * cg + 0.114 * cb) < 128;
    const backdrop = !dialog && share > 0.8 && a > 0.15 && a < 0.95 && (dark || cs.pointerEvents !== 'none');
    if (!(cookie || dialog || backdrop)) continue;
    const hit = {cookie, dialog, backdrop, share: Math.round(share * 100) / 100,
                 text: t.replace(/\s+/g, ' ').slice(0, 100)};
    if (cookie) { best = hit; break; }
    if (!best || hit.share > best.share) best = hit;
  }
  return {textLen: text.length, challenge, phrase, widget, overlay: best};
}"""

# A consent manager's backdrop outlives its dialog: the hide-CSS removes the banner and the empty
# dimming layer stays (measured: NewsGuard / Ketch, rgba(0,0,0,0.24) over the whole page). When no
# dialog is visible any more, an empty dark translucent full-viewport layer is that orphan - hide it
# the same way the CSS hides the banner (nothing is clicked or accepted). A visible dialog means the
# backdrop belongs to something the capture must refuse, so nothing is touched then.
_DROP_ORPHAN_BACKDROPS = r"""() => {
  const vw = innerWidth, vh = innerHeight, area = vw * vh;
  const shown = (el) => { const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden' && parseFloat(cs.opacity) > 0; };
  for (const d of document.querySelectorAll('[role=dialog],[role=alertdialog],[aria-modal=true],dialog[open]')) {
    const r = d.getBoundingClientRect();
    if (shown(d) && r.width > 0 && r.height > 0) return 0;
  }
  let n = 0;
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed' || !shown(el)) continue;
    const r = el.getBoundingClientRect();
    const w = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
    const h = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
    if (w * h < area * 0.8 || (el.innerText || '').trim().length > 0) continue;
    const m = /rgba?\(([^)]+)\)/.exec(cs.backgroundColor || '');
    if (!m) continue;
    const v = m[1].split(',').map(parseFloat);
    const a = (v.length > 3 ? v[3] : 1) * parseFloat(cs.opacity || '1');
    if (a > 0.15 && a < 0.95 && (0.299 * v[0] + 0.587 * v[1] + 0.114 * v[2]) < 128) {
      el.style.setProperty('display', 'none', 'important'); n++;
    }
  }
  return n;
}"""

# The largest class of bad screenshot in the audit was the right URL with the wrong part of the
# page in view (an LLM price table under a caption about audio prices). Given the caption's terms,
# find the block that carries the most of them and bring it to the top of the viewport when the
# current view shows fewer. Nothing is clicked; the page is only scrolled, as a reader would.
_FOCUS_PROBE = r"""(terms, vhRef, heading) => {
  // vhRef: a fixed reference height for the remote capture, whose script-time window is random
  const vh = vhRef || innerHeight;
  // a named section first: the producer reads the page's outline and names the heading of the
  // block to show. Counting the caption's words alone kept the page's header in the shot, because
  // the header said "$0.04/min" and "credits" too (2026-10-06)
  if (heading) {
    const want = heading.toLowerCase().replace(/\s+/g, ' ').trim();
    for (const h of document.querySelectorAll('h1, h2, h3, h4, h5, [role=heading], th, summary, dt, legend, strong, b')) {
      const r = h.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      if (!(h.innerText || '').toLowerCase().replace(/\s+/g, ' ').includes(want)) continue;
      // the frame is the heading and what follows it: the largest block around the heading that
      // starts at it and fits a screen and a third, taken whole; when that is the heading and
      // little else (its section is taller), one screen from the heading down, laid over the
      // heading's block so it follows the layout at the shutter, as wide as the block around it.
      // The heading alone was the frame when its section was taller than a screen: four
      // re-captures came back as an 832x116 shot of the heading's words (2026-10-06)
      const top = h.getBoundingClientRect().top;
      let box = h;
      for (let a = h.parentElement; a && a !== document.body && a !== document.documentElement; a = a.parentElement) {
        const ar = a.getBoundingClientRect();
        if (ar.height > vh * 1.35 || ar.top < top - vh * 0.3) break;
        box = a;
      }
      let mark = box;
      const b = box.getBoundingClientRect();
      if (b.height < vh * 0.5) {
        const up = box.parentElement && box.parentElement !== document.documentElement ? box.parentElement : box;
        const hr = up.getBoundingClientRect();
        if (getComputedStyle(box).position === 'static') box.style.position = 'relative';
        const lay = document.createElement('div');
        lay.style.cssText = 'position:absolute;pointer-events:none;background:transparent;height:100vh;'
          + 'top:' + Math.round(top - b.top - 24) + 'px;left:' + Math.round(Math.min(0, hr.left - b.left))
          + 'px;right:' + Math.round(Math.min(0, b.right - hr.right)) + 'px';
        box.appendChild(lay);
        mark = lay;
      }
      mark.setAttribute('data-pb-focus', 'exact');     // framed as it is: never widened
      const max = Math.max(0, document.documentElement.scrollHeight - vh);
      const y = h.getBoundingClientRect().top + scrollY - 80;
      window.scrollTo(0, Math.min(Math.max(0, y), max));
      return {terms: terms.length, before: 0, after: terms.length, moved: true, heading: true};
    }
  }
  const count = (t) => { t = (t || '').toLowerCase(); let n = 0; for (const w of terms) if (t.includes(w)) n++; return n; };
  const visible = () => {           // terms in what the viewport actually shows right now
    let txt = '';
    for (const el of document.querySelectorAll('body *')) {
      if (el.children.length) continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0 || r.bottom <= 0 || r.top >= vh) continue;
      txt += ' ' + (el.innerText || el.textContent || '');
    }
    return count(txt);
  };
  const before = visible();
  let best = null, bestN = before;
  for (const el of document.querySelectorAll('section, article, table, div, ul, ol, dl')) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0 || r.height > vh * 1.2) continue;   // a block that fits in view
    const n = count(el.innerText);
    if (n > bestN) { best = el; bestN = n; }
  }
  const moved = !!best && bestN >= 2;
  if (moved) {
    best.setAttribute('data-pb-focus', '1');   // the remote capture frames this block (no visual effect)
    // leave room for a sticky header, but never scroll past the end of the page: near the bottom a
    // fixed offset would push the block back out of view
    const max = Math.max(0, document.documentElement.scrollHeight - vh);
    const y = best.getBoundingClientRect().top + scrollY - 80;
    window.scrollTo(0, Math.min(Math.max(0, y), max));
  }
  return {terms: terms.length, before, after: visible(), moved, heading: false};
}"""


def _framed(page, out_path: str) -> bool:
    """A shot of the block the probe marked for a named heading; False (the viewport is shot
    instead) when there is none or the shot fails."""
    try:
        mark = page.locator('[data-pb-focus="exact"]').first
        if not mark.count():
            return False
        mark.screenshot(path=out_path)
        return True
    except Exception:  # noqa: BLE001 — framing is best effort, the viewport is still a picture
        return False


_OUTLINE = r"""() => Array.from(document.querySelectorAll('h1, h2, h3, h4')).filter((h) => {
  const r = h.getBoundingClientRect(); return r.width > 0 && r.height > 0 && (h.innerText || '').trim();
}).slice(0, 40).map((h) => {
  let next = h.nextElementSibling, text = '';
  while (next && !text) { text = (next.innerText || '').trim(); next = next.nextElementSibling; }
  return {heading: h.innerText.trim().replace(/\s+/g, ' ').slice(0, 120), text: text.replace(/\s+/g, ' ').slice(0, 160)};
})"""


def _try(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001 — a hint, never a failure
        return None


def _keep_outline(diag: dict, got) -> None:
    """The page's sections as the capture saw them (`_OUTLINE`), for the look at the shot that
    follows (visual_director.look_at_shot): what it may name to frame instead, read on the same
    page load, local or remote."""
    if isinstance(got, list):
        rows = [x for x in got if isinstance(x, dict) and x.get("heading")]
        if rows:
            diag["outline"] = rows


def outline(url: str, timeout_ms: int = 20000) -> list[dict]:
    """The page's headings, each with the start of the text under it: what a producer reads to
    name the section a screenshot should show. [] when the page cannot be read (best effort)."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(locale="en-US", viewport={"width": 1440, "height": 900})
                page.goto(_in_english(url), wait_until="load", timeout=timeout_ms)
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:  # noqa: BLE001
                    pass
                got = page.evaluate(_OUTLINE)
            finally:
                browser.close()
        return [x for x in got if isinstance(x, dict) and x.get("heading")]
    except Exception as e:  # noqa: BLE001 — an outline is a hint, never a failure
        print(f"[screenshot] {url}: no outline ({type(e).__name__}: {str(e)[:120]})")
        return []

# Icon fonts draw a glyph for a word: <span class="material-symbols">arrow_forward</span>. When the
# font does not arrive before the shutter, the word itself is on the image (measured through
# Scrapfly on Google's Meet page: "arrow_forward" around a carousel, four "close" buttons, with
# document.fonts already reporting "loaded"). Ask for each icon font in use, give it a moment,
# then hide whatever still shows as a word: a missing arrow reads as nothing, the word reads as a
# broken page. A glyph that did render is 1em wide; the same word in a fallback font is wider.
_ICON_FIX = r"""async () => {
  const ICON = /material|symbols|icons/i, WORD = /^[a-z][a-z0-9_]{2,}$/;
  const words = () => Array.from(document.querySelectorAll('body *')).filter((el) => {
    if (el.children.length) return false;
    if (!WORD.test((el.textContent || '').trim())) return false;
    const cs = getComputedStyle(el);
    if (!ICON.test(cs.fontFamily)) return false;
    const r = el.getBoundingClientRect();
    return r.height > 0 && r.width > parseFloat(cs.fontSize) * 1.3;
  });
  const first = words();
  if (!first.length) return {found: 0, hidden: 0};
  const fams = new Set(first.map((el) => getComputedStyle(el).fontFamily.split(',')[0].trim()));
  if (document.fonts) {
    await Promise.race([Promise.all([...fams].map((f) => document.fonts.load('24px ' + f).catch(() => null))),
                        new Promise((r) => setTimeout(r, 1500))]);
    await new Promise((r) => setTimeout(r, 200));
  }
  const left = words();
  for (const el of left) el.style.setProperty('visibility', 'hidden', 'important');
  return {found: first.length, hidden: left.length, fonts: [...fams].slice(0, 4)};
}"""

_HIDE_CSS = """
#onetrust-banner-sdk, #onetrust-consent-sdk, #CybotCookiebotDialog,
[id*="cookie-banner"], [class*="cookie-banner"], [class*="cookie-consent"],
[id*="consent"], [class*="consent-banner"], [aria-label*="cookie" i]
{ display: none !important; }
.goog-te-banner-frame, iframe.skiptranslate, #goog-gt-tt, .goog-te-balloon-frame
{ display: none !important; }
body { top: 0 !important; }
* { animation: none !important; transition: none !important; }
"""


def _in_english(url: str) -> str:
    """Google picks a page's language from the visitor's IP location, whatever the browser asks
    for: captured from a machine abroad, Meet's product page came out in Vietnamese and English
    blog posts carried a Vietnamese "translate this page" bar (hidden by _HIDE_CSS). hl=en asks
    Google for English explicitly; a URL that already names a language is left alone."""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if not (host == "google.com" or host.endswith(".google.com")):
        return url
    if any(k == "hl" for k, _ in parse_qsl(p.query, keep_blank_values=True)):
        return url
    return _force_en(url)


def _force_en(url: str) -> str:
    """The same URL with `hl=en`, the parameter every Google property honours over geo-IP. Unlike
    `_in_english` it overrides a non-English `hl`: it is the retry after a page came back foreign."""
    p = urlparse(url)
    query = parse_qsl(p.query, keep_blank_values=True)
    if any(k == "hl" and str(v).lower().startswith("en") for k, v in query):
        return url
    return urlunparse(p._replace(query=urlencode([(k, v) for k, v in query if k != "hl"] + [("hl", "en")])))


def _page_lang(page) -> str:
    """`<html lang>` as the page itself reports it, lowercased. Empty when unset."""
    try:
        return (page.evaluate("document.documentElement.lang") or "").strip().lower()
    except Exception:  # noqa: BLE001 — a page that will not run JS tells us nothing
        return ""


def _cap_height(png: Path, max_ratio: float, diag: dict) -> None:
    """Crop a capture taller than `max_ratio` × its width to its top part. A page shot must read
    like one screen in the article: the Zoom article carried two support pages captured whole
    (2880×8742 and 2880×11390), each a wall several screens tall. An element capture may be as
    tall as it is wide."""
    try:
        from PIL import Image

        with Image.open(png) as im:
            w, h = im.size
            limit = int(w * max_ratio)
            if h <= limit * 1.05:
                return
            im.crop((0, 0, w, limit)).save(png)
        diag["cropped"] = f"{w}x{h} -> {w}x{limit}"
    except Exception as e:  # noqa: BLE001 — an uncropped shot is still a shot
        diag["crop_error"] = str(e)[:120]


def capture(
    url: str,
    out_path: str,
    selector: str | None = None,
    full_page: bool = False,
    viewport: tuple[int, int] = (1440, 900),
    timeout_ms: int = 30000,
    diag: dict | None = None,
    focus: list[str] | None = None,
    remote: dict | None = None,
    heading: str | None = None,
) -> str | None:
    """Capture `url` into `out_path`. `remote` ({"country": ...}, from core.tools.scrapfly.remote)
    opens the page in Scrapfly's browser in that country instead of the local one; the same
    checks run either way.

    The shot is taken into a temp file next to `out_path` and replaces it only once every check
    passed: re-capturing an existing screenshot used to write over it first, and a refused or
    failed capture then deleted it, so the article lost a working picture (RF-09)."""
    diag = diag if diag is not None else {}
    target = Path(out_path)
    tmp = target.with_name(f".{target.stem}.capture{target.suffix or '.png'}")
    try:
        got = _capture_into(url, str(tmp), selector, full_page, viewport, timeout_ms, diag, focus, remote, heading)
        if got and tmp.exists():
            if not full_page:
                _cap_height(tmp, max_ratio=(viewport[1] / viewport[0]) if not selector else 1.0, diag=diag)
            os.replace(tmp, target)
            return str(target)
        return None
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _capture_into(url: str, out_path: str, selector: str | None, full_page: bool,
                  viewport: tuple[int, int], timeout_ms: int, diag: dict, focus: list[str] | None,
                  remote: dict | None, heading: str | None = None) -> str | None:
    if remote:
        return _capture_remote(url, out_path, selector, full_page, diag, focus, remote, heading)

    def refuse(reason: str, detail: str, browser=None) -> None:
        diag["reason"], diag["detail"] = reason, detail
        print(f"[screenshot] {url}: refused — {reason} ({detail})")
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
        return None

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(
                viewport={"width": viewport[0], "height": viewport[1]},
                device_scale_factor=2,
                locale="en-US",
                user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
            )
            page = context.new_page()
            url = _in_english(url)
            response = page.goto(url, wait_until="load", timeout=timeout_ms)
            if why := _refusal(page, response, url, probe_dom=False):
                return refuse(*why, browser=browser)
            # A page can still come back in the WRONG LANGUAGE with nothing in the request to argue
            # with (measured 2026-09-10: html lang='vi' on a plain Google URL, 'en' with ?hl=en).
            # Retry once with the language forced; if it is STILL not English, refuse: a missing
            # visual becomes a finding the operator sees, a foreign screenshot ships silently.
            lang = _page_lang(page)
            if lang and not lang.startswith("en"):
                retry = _force_en(url)
                if retry != url:
                    print(f"[screenshot] {url} rendered as '{lang}' — retrying with hl=en")
                    url = retry
                    response = page.goto(url, wait_until="load", timeout=timeout_ms)
                    lang = _page_lang(page)
                if lang and not lang.startswith("en"):
                    return refuse("WRONG_LANGUAGE", f"html lang '{lang}'", browser=browser)
            try:
                page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass  # noisy pages never go idle — shoot anyway
            try:
                page.add_style_tag(content=_HIDE_CSS)
                page.evaluate("document.fonts && document.fonts.ready")
            except Exception:
                pass
            try:
                icons = page.evaluate(_ICON_FIX)
                if icons.get("found"):
                    diag["icons"] = icons
            except Exception:  # noqa: BLE001 — cosmetic, never a failure
                pass
            page.wait_for_timeout(800)  # let lazy images/fonts settle
            try:
                diag["title"] = " ".join((page.title() or "").split())  # names a substitute page
            except Exception:
                pass
            _keep_outline(diag, _try(lambda: page.evaluate(_OUTLINE)))
            try:
                if dropped := page.evaluate(_DROP_ORPHAN_BACKDROPS):
                    diag["dropped_backdrops"] = dropped
            except Exception:
                pass
            # again after the settle wait and the hide-CSS: challenges that swap in late, login
            # redirects, and overlays the CSS did not remove are only visible now
            if why := _refusal(page, response, url, probe_dom=True):
                return refuse(*why, browser=browser)
            # bring the caption's content into view (viewport shots only: a full page or a named
            # element is already framed); diag["focus"] says how many caption terms are in view
            if (focus or heading) and not full_page and not selector:
                try:
                    diag["focus"] = page.evaluate("([t, h]) => (" + _FOCUS_PROBE + ")(t, null, h)",
                                                  [[t.lower() for t in focus or []], heading or None])
                    if diag["focus"].get("moved"):
                        page.wait_for_timeout(500)
                        if why := _refusal(page, response, url, probe_dom=True):
                            return refuse(*why, browser=browser)
                except Exception:  # noqa: BLE001 — framing is best effort, never a failure
                    pass

            if selector:
                el = page.locator(selector).first
                el.scroll_into_view_if_needed(timeout=5000)
                el.screenshot(path=out_path)
            elif not full_page and (diag.get("focus") or {}).get("heading") and _framed(page, out_path):
                pass        # the named section as the probe framed it: the heading and what follows it
            else:
                page.screenshot(path=out_path, full_page=full_page)
            # and once more AFTER the shot: a challenge or modal that swapped in during the
            # capture is on the image even though the check just before it was clean
            if why := _refusal(page, response, url, probe_dom=True):
                try:
                    os.remove(out_path)
                except OSError:
                    pass
                return refuse(*why, browser=browser)
            browser.close()

        # blank-page guard: a JS-gated/blocked site can render an empty white page —
        # a near-uniform image is a failure, not a screenshot
        try:
            from PIL import Image, ImageStat
            std = ImageStat.Stat(Image.open(out_path).convert("L")).stddev[0]
            if std < 6:
                os.remove(out_path)
                return refuse("BLANK", f"near-uniform image, std={std:.1f}")
        except Exception:
            pass
        return out_path
    except Exception as e:  # noqa: BLE001 — never fail the article over a visual
        return refuse("ERROR", str(e)[:200])


# Scrapfly runs one script after the page settles: the hide-CSS, the orphan-backdrop drop, the
# caption framing and the page probe, and hands their results back. Images are loaded (a headless
# render skips them otherwise) and the page's own consent banners are blocked as well.
#
# The frame is an ELEMENT the script marks, not coordinates: Scrapfly resizes the window between
# the script and the shutter (measured: the script saw 1387x715, the shot was laid out ~1920 wide),
# so a full-page shot cropped at the script's scrollY landed in a footer on pages whose sections are
# sized in viewport units. An element's box is recomputed at the shutter, whatever the window. For
# a caption, the block carrying its terms, widened to the largest ancestor that still fits one
# screen (the section around it, with its heading); without one, a transparent layer over the
# first screen (100vh, so it follows the shutter's window).
_REMOTE_FLAGS = ("load_images", "block_banners", "high_quality")
_REMOTE_FRAME = r"""(vhRef) => {
  let el = document.querySelector('[data-pb-focus]');
  if (el) {
    let a = el.parentElement;
    while (el.getAttribute('data-pb-focus') !== 'exact' && a && a !== document.body && a !== document.documentElement
           && a.getBoundingClientRect().height <= vhRef * 0.95) { el = a; a = a.parentElement; }
    el.setAttribute('data-pb-frame', 'block');
    return 'block';
  }
  el = document.createElement('div');
  el.setAttribute('data-pb-frame', 'top');
  el.style.cssText = 'position:absolute;top:0;left:0;right:0;height:100vh;pointer-events:none;background:transparent';
  document.body.appendChild(el);
  return 'top';
}"""
_FRAME_SELECTOR = "[data-pb-frame]"
# Scrapfly's script-time window is random (measured 673-715 px high) and replaced at the shutter
# (~1080), so which block fits "one screen" is judged against the local viewport height instead:
# the same page then yields the same frame whatever window the script happened to get.
_REMOTE_VH = 900


def _remote_script(focus_terms: list[str] | None, frame: bool = True, heading: str | None = None) -> str:
    """The page script; `frame` marks the element the shot is taken of (viewport shots only)."""
    # smooth scrolling would leave the page between positions when the probe reads it
    css = _HIDE_CSS + "html, body { scroll-behavior: auto !important; }\n"
    focus = (f"({_FOCUS_PROBE})({json.dumps([t.lower() for t in focus_terms or []])}, {_REMOTE_VH}, "
             f"{json.dumps(heading or None)})" if (focus_terms or heading) else "null")
    # an async body: Scrapfly awaits the returned promise (measured), so the checks run after the
    # page's fonts have settled, as the local capture's document.fonts.ready wait does
    return "\n".join([
        "return (async () => {",
        "if (document.fonts) { try { await document.fonts.ready; } catch (e) {} }",
        f"const icons = await ({_ICON_FIX})();",
        "const css = document.createElement('style');",
        f"css.textContent = {json.dumps(css)};",
        "(document.head || document.documentElement).appendChild(css);",
        f"const dropped = ({_DROP_ORPHAN_BACKDROPS})();",
        f"const focus = {focus};",
        f"const probe = ({_PAGE_PROBE})();",
        f"const outline = ({_OUTLINE})();",
        f"const frame = {'(' + _REMOTE_FRAME + f')({_REMOTE_VH})' if frame else 'null'};",
        # bring the frame into view and let lazy images around it load before the shutter measures
        # it: measured, an image loading above a marked section pushed it down after its box was
        # taken, and the shot came out half the section above
        "const fr = frame ? document.querySelector('[data-pb-frame]') : null;",
        "if (fr) { fr.scrollIntoView({block: 'start'}); await new Promise((r) => setTimeout(r, 1000)); }",
        "return {title: document.title, href: location.href, probe: probe, dropped: dropped,",
        "        lang: (document.documentElement.lang || '').trim().toLowerCase(), icons: icons,",
        "        focus: focus, frame: frame, outline: outline, scrollY: window.scrollY, innerWidth: innerWidth,",
        "        innerHeight: innerHeight, docHeight: document.documentElement.scrollHeight};",
        "})();",
    ])


def _capture_remote(url: str, out_path: str, selector: str | None, full_page: bool, diag: dict,
                    focus: list[str] | None, remote: dict, heading: str | None = None) -> str | None:
    from ..tools import scrapfly

    country = scrapfly.country_code(remote.get("country"))
    diag.update(via="scrapfly", country=country)

    def refuse(reason: str, detail: str) -> None:
        diag["reason"], diag["detail"] = reason, detail
        print(f"[screenshot] {url}: refused via scrapfly ({country}) — {reason} ({detail})")
        return None

    try:
        url = _in_english(url)
        frame = not (selector or full_page)
        target = selector or ("fullpage" if full_page else _FRAME_SELECTOR)
        res = scrapfly.scrape(url, country, js=_remote_script(focus if frame else None, frame=frame,
                                                              heading=heading if frame else None),
                              screenshots={"main": target}, flags=_REMOTE_FLAGS, rendering_wait=2000)
        diag["credits"] = res.get("credits") or 0
        jr = res.get("js_result") if isinstance(res.get("js_result"), dict) else {}
        if jr.get("title"):
            diag["title"] = " ".join(str(jr["title"]).split())
        if jr.get("dropped"):
            diag["dropped_backdrops"] = jr["dropped"]
        if frame and (focus or heading) and isinstance(jr.get("focus"), dict):
            diag["focus"] = jr["focus"]
        if frame and jr.get("frame"):
            diag["frame"] = jr["frame"]   # "block": the caption's section; "top": the first screen
        _keep_outline(diag, jr.get("outline"))
        if isinstance(jr.get("icons"), dict) and jr["icons"].get("found"):
            diag["icons"] = jr["icons"]   # icon words found, and how many still showed and were hidden
        diag["checked"] = bool(jr)
        if why := _remote_refusal(res, url):
            return refuse(*why)
        if not jr:
            # Locally the probe fails open only after the title, status and redirect were read
            # from the live page. Here a missing script result means none of the page checks ran
            # (no title, no consent-wall or overlay probe, no framing), so the capture would ship
            # unchecked. Refused as transient: the retry and the fallback chain take over.
            return refuse("ERROR", "page script did not run")
        shot = (res.get("screenshots") or {}).get("main")
        if not shot:
            return refuse("ERROR", "Scrapfly returned no screenshot")
        from PIL import Image, ImageStat

        with open(out_path, "wb") as f:
            f.write(scrapfly.download(shot))
        with Image.open(out_path) as im:
            im = im.convert("RGB")
            diag["size"] = list(im.size)
            im.save(out_path, format="PNG")
            std = ImageStat.Stat(im.convert("L")).stddev[0]
        if std < 6:
            os.remove(out_path)
            return refuse("BLANK", f"near-uniform image, std={std:.1f}")
        return out_path
    except Exception as e:  # noqa: BLE001 — never fail the article over a visual
        try:
            os.remove(out_path)
        except OSError:
            pass
        return refuse("ERROR", scrapfly._clean(e)[:200])


def _remote_refusal(res: dict, url: str) -> tuple[str, str] | None:
    """`_refusal`'s checks, read from a Scrapfly result: the target's status and headers, the final
    address, and the page script's probe. Without a script result only the first ones can run;
    the caller refuses such a capture."""
    status = res.get("status")
    headers = res.get("headers") or {}
    jr = res.get("js_result") if isinstance(res.get("js_result"), dict) else {}
    title = str(jr.get("title") or "")
    final = str(jr.get("href") or res.get("url") or "")
    probe = jr.get("probe") if isinstance(jr.get("probe"), dict) else {}
    if (str(headers.get("cf-mitigated") or "")).lower() == "challenge":
        return "CHALLENGE", f"cf-mitigated header, HTTP {status}"
    if _CHALLENGE_TITLE.search(title):
        return "CHALLENGE", f"title {title!r}"
    if _LOGIN_URL.search(final) and not _LOGIN_URL.search(url):
        return "LOGIN", f"redirected to {final}"
    if probe.get("challenge"):
        return "CHALLENGE", ("challenge-page wording in the visible text" if probe.get("phrase")
                             else "captcha widget on a page with almost no other text")
    if isinstance(status, int) and status >= 400:
        return ("CHALLENGE" if status in (403, 429, 503) and "cf-ray" in headers else "ERROR",
                f"HTTP {status}")
    if err := res.get("error"):
        return "ERROR", f"{err.get('code')}: {err.get('message')}"
    lang = str(jr.get("lang") or "")
    if lang and not lang.startswith("en"):
        # the page opened in the market's country and still reports another language
        return "WRONG_LANGUAGE", f"html lang '{lang}'"
    ov = probe.get("overlay")
    if ov and ov.get("cookie"):
        return "COOKIE_WALL", f"{ov.get('share', 0):.0%} of the viewport: {ov.get('text', '')!r}"
    if ov:
        return "OVERLAY", f"{'dialog' if ov.get('dialog') else 'backdrop'} over {ov.get('share', 0):.0%} of the viewport"
    if probe and (probe.get("textLen") or 0) < _MIN_TEXT:
        return "BLANK", f"{probe.get('textLen', 0)} characters of visible text"
    return None


def _refusal(page, response, url: str, probe_dom: bool) -> tuple[str, str] | None:
    """(reason, detail) when the page is not the page we came for, else None. Never raises."""
    try:
        status = response.status if response is not None else None
        headers = {k.lower(): v for k, v in (response.headers if response is not None else {}).items()}
        title = page.title() or ""
        if (headers.get("cf-mitigated") or "").lower() == "challenge":
            return "CHALLENGE", f"cf-mitigated header, HTTP {status}"
        if _CHALLENGE_TITLE.search(title):
            return "CHALLENGE", f"title {title!r}"
        if _LOGIN_URL.search(page.url or "") and not _LOGIN_URL.search(url):
            return "LOGIN", f"redirected to {page.url}"
        if probe_dom:
            probe = page.evaluate(_PAGE_PROBE) or {}
            if probe.get("challenge"):
                return "CHALLENGE", ("challenge-page wording in the visible text" if probe.get("phrase")
                                     else "captcha widget on a page with almost no other text")
        if status is not None and status >= 400:
            return ("CHALLENGE" if status in (403, 429, 503) and "cf-ray" in headers else "ERROR",
                    f"HTTP {status}")
        if probe_dom:
            ov = probe.get("overlay")
            if ov and ov.get("cookie"):
                return "COOKIE_WALL", f"{ov['share']:.0%} of the viewport: {ov.get('text', '')!r}"
            if ov:
                return "OVERLAY", f"{'dialog' if ov.get('dialog') else 'backdrop'} over {ov['share']:.0%} of the viewport"
            if (probe.get("textLen") or 0) < _MIN_TEXT:
                return "BLANK", f"{probe.get('textLen', 0)} characters of visible text"
    except Exception:  # noqa: BLE001 — a probe failure must not block a capture
        return None
    return None
