"""Third-party pages through Scrapfly's Web Scraping API, opened in the market's own country.

Where a page is opened decides what it shows. Captured from a machine in Vietnam, Google served
Vietnamese product pages and pricing pages quoted local prices. A Scrapfly request opens the page
in a browser in the country the SEO settings name (`seo.country`, the market Ahrefs reads), so a
screenshot or a competitor audit shows what a reader there sees.

The Unblocker is enabled on every scrape, including plain page reads and screenshots. Scrapfly
handles anti-bot challenges before our page checks and fallback chain run. Needs SCRAPFLY_KEY
in the environment; without it every caller keeps the local browser.

    remote(config, country)            # {"country": "us"} when captures go through Scrapfly, else None
    scrape(url, country, js=..., screenshots={"main": "fullpage"}, flags=(...))
    download(result["screenshots"]["main"])
"""
from __future__ import annotations

import base64
import json
import os
import re

API = "https://api.scrapfly.io/scrape"
# Start with datacenter proxies. Unblocker may upgrade the pool for protected targets,
# but respects the country explicitly supplied on every request.
POOL = "public_datacenter_pool"


def api_key() -> str | None:
    return (os.environ.get("SCRAPFLY_KEY") or "").strip() or None


def country_code(country) -> str:
    """ISO 3166 alpha-2, lower case, as Scrapfly takes it ("uk" is what people type for gb)."""
    c = str(country or "").strip().lower()
    if c == "uk":
        return "gb"
    return c if re.fullmatch(r"[a-z]{2}", c) else "us"


def remote(config: dict | None, country) -> dict | None:
    """{"country": ...} when third-party pages go through Scrapfly, else None (the local browser).

    `browser.backend: scrapfly` asks for it; a missing key keeps the local browser, so a machine
    or a test without the key behaves exactly as before.
    """
    backend = (((config or {}).get("browser") or {}).get("backend") or "local").lower()
    if backend != "scrapfly" or not api_key():
        return None
    return {"country": country_code(country)}


def _clean(text) -> str:
    """A message without the key: requests puts the full request URL, key included, into its errors."""
    s = str(text or "")
    k = api_key()
    return s.replace(k, "***") if k else s


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def scrape(url: str, country, *, render_js: bool = True, js: str | None = None,
           js_scenario: list | None = None, screenshots: dict | None = None,
           flags: tuple = (), rendering_wait: int | None = None, timeout_s: int = 180) -> dict:
    """One Web Scraping API call. Never raises.

    Returns {status, url, content, headers, js_result, screenshots: {name: url}, credits, error};
    `error` is None on success, else {code, message, retryable}. `status` is the TARGET page's HTTP
    status, `url` its final address after redirects.
    """
    import requests

    params: dict[str, str] = {"key": api_key() or "", "url": url, "country": country_code(country),
                              "proxy_pool": POOL, "unblocker": "true"}
    if render_js:
        params["render_js"] = "true"
        if rendering_wait:
            params["rendering_wait"] = str(int(rendering_wait))
        if js:
            params["js"] = _b64(js)
        if js_scenario:
            params["js_scenario"] = _b64(json.dumps(js_scenario))
        for name, target in (screenshots or {}).items():
            params[f"screenshots[{name}]"] = target
        if screenshots and flags:
            params["screenshot_flags"] = ",".join(flags)
    out = {"status": None, "url": url, "content": "", "headers": {}, "js_result": None,
           "screenshots": {}, "credits": 0, "error": None}
    try:
        r = requests.get(API, params=params, timeout=timeout_s)
        data = r.json()
    except Exception as e:  # noqa: BLE001 — network failure or a non-JSON answer
        out["error"] = {"code": "NETWORK", "message": _clean(e)[:200], "retryable": True}
        return out
    if not isinstance(data, dict):
        # "never raises" held only for a JSON object: an array or a bare value reached .get()
        out["error"] = {"code": f"HTTP_{r.status_code}", "retryable": False,
                        "message": f"unexpected answer: a JSON {type(data).__name__}, not an object"}
        return out
    res = data.get("result") if isinstance(data.get("result"), dict) else {}
    ctx = data.get("context") if isinstance(data.get("context"), dict) else {}
    cost = ctx.get("cost")
    credits = cost.get("total") if isinstance(cost, dict) else cost
    try:
        credits = int(credits or 0)
    except (TypeError, ValueError):
        credits = 0
    from .. import usage

    usage.record("scrapfly", "scrape", {"credits": credits, "calls": 1},
                 agent="screenshots" if screenshots else "page_reads")
    bd = res.get("browser_data") if isinstance(res.get("browser_data"), dict) else {}
    out.update(status=res.get("status_code"), url=res.get("url") or url, content=res.get("content") or "",
               headers={str(k).lower(): v for k, v in (res.get("response_headers") or {}).items()},
               js_result=bd.get("javascript_evaluation_result"), credits=credits,
               screenshots={n: (s or {}).get("url") for n, s in (res.get("screenshots") or {}).items()
                            if (s or {}).get("url")})
    # a failed scrape carries its error under result; an API-level refusal (bad key, quota) at the top
    err = res.get("error") or (data if r.status_code >= 400 and data.get("code") else None)
    if err or r.status_code >= 400:
        err = err or {}
        out["error"] = {"code": str(err.get("code") or f"HTTP_{r.status_code}"),
                        "message": _clean(err.get("message") or err.get("description") or "")[:200],
                        "retryable": bool(err.get("retryable"))}
    return out


def download(shot_url: str, timeout_s: int = 60) -> bytes:
    """A screenshot's bytes. Its URL needs the key, like every Scrapfly call."""
    import requests

    try:
        r = requests.get(shot_url, params={"key": api_key() or ""}, timeout=timeout_s)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(_clean(e)[:200]) from None
    if r.status_code != 200 or not r.content:
        raise RuntimeError(f"screenshot download: HTTP {r.status_code}")
    return r.content
