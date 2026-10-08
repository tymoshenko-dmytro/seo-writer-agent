"""Whether the install's accounts work, asked of the services themselves. Each test is free: it
reads an account, a list or a limit, or sends a request the service refuses before doing any work;
it never runs a model, spends a credit or writes anything. Used by Configure > Connections, the
setup, `cli.py setup` / `doctor`, and before a run starts.

A test answers {"ok": bool, "detail": what it found or the exact reason}; the WordPress test also
reads the site's name, the user's role, the categories and the authors, for the pickers.

All seven services are required: a run uses every one of them. A test's answer is kept on the
data volume with a fingerprint of the key it tested (`_project.json`, "tests"), so the setup and
the pages show it until the key changes, and a run starts only when every one has passed."""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests

TIMEOUT = 20


def _answer(r, ok_detail: str) -> dict:
    if r.status_code == 200:
        return {"ok": True, "detail": ok_detail}
    if r.status_code in (401, 403):
        return {"ok": False, "detail": "the key was refused: check that it is copied whole and still active"}
    return {"ok": False, "detail": f"the service answered {r.status_code}"}


def _anthropic(key: str) -> dict:
    r = requests.get("https://api.anthropic.com/v1/models", timeout=TIMEOUT,
                     headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    return _answer(r, "the key works: its models are listed")


def _openai(key: str) -> dict:
    r = requests.get("https://api.openai.com/v1/models", timeout=TIMEOUT, headers={"Authorization": f"Bearer {key}"})
    return _answer(r, "the key works: its models are listed")


def _parallel(key: str) -> dict:
    """A task run that does not exist, asked for: a key that works gets "not found", free."""
    r = requests.get("https://api.parallel.ai/v1/tasks/runs/trun_0000000000000000", timeout=TIMEOUT,
                     headers={"x-api-key": key})
    if r.status_code == 404:
        return {"ok": True, "detail": "the key works: the account answers"}
    return _answer(r, "the key works: the account answers")


def _ahrefs(key: str) -> dict:
    """The subscription's limits and usage, read through the MCP server (a call that costs no
    API units)."""
    url, headers = "https://api.ahrefs.com/mcp/mcp", {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                                      "Accept": "application/json"}
    with requests.Session() as s:
        init = s.post(url, headers=headers, timeout=TIMEOUT, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "seo-writer-agent", "version": "1"}}})
        if init.status_code != 200:
            return _answer(init, "")
        r = s.post(url, headers=headers, timeout=TIMEOUT, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "subscription-info-limits-and-usage", "arguments": {}}})
    if r.status_code != 200:
        return _answer(r, "")
    body = r.json() or {}
    result = body.get("result") if isinstance(body.get("result"), dict) else {}
    if body.get("error") or result.get("isError"):
        return {"ok": False, "detail": "the key was refused: use an Ahrefs MCP key (Account settings > API keys)"}
    try:
        info = json.loads(result["content"][0]["text"]).get("limits_and_usage")
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        info = None
    if not isinstance(info, dict) or not info:
        # an answer that is not the subscription confirms nothing about the key
        return {"ok": False, "detail": "Ahrefs answered, but not with the subscription's limits: the key is not confirmed"}
    used, limit = info.get("units_usage_workspace"), info.get("units_limit_workspace")
    if isinstance(used, int) and isinstance(limit, int) and limit:
        return {"ok": True, "detail": f"the key works: {used:,} of {limit:,} API units used this month"}
    return {"ok": True, "detail": "the key works: the subscription is read"}


def _scrapfly(key: str) -> dict:
    r = requests.get("https://api.scrapfly.io/account", params={"key": key}, timeout=TIMEOUT)
    return _answer(r, "the key works: the account is read")


def _winston(key: str) -> dict:
    """A text below the service's minimum length: a key that works gets "too short" before any
    credit is spent."""
    r = requests.post("https://api.gowinston.ai/v2/plagiarism", timeout=TIMEOUT, json={"text": "test", "language": "en"},
                      headers={"Authorization": f"Bearer {key}"})
    if r.status_code == 400:
        return {"ok": True, "detail": "the key works: the account answers"}
    return _answer(r, "the key works: the account answers")


TESTS = {"ANTHROPIC_API_KEY": _anthropic, "OPENAI_API_KEY": _openai, "PARALLEL_API_KEY": _parallel,
         "AHREFS_API_KEY": _ahrefs, "SCRAPFLY_KEY": _scrapfly, "WINSTON_API_KEY": _winston}
# every service a run uses; WordPress (WP_BASE_URL, WP_USERNAME, WP_APP_PASSWORD) is the seventh
REQUIRED = tuple(TESTS)
WORDPRESS = "WordPress"


def test_key(env: str, value: str | None = None) -> dict:
    """The key `env` (the one in force, or `value`) asked of its service."""
    key = value if value is not None else os.getenv(env, "")
    if not key:
        return {"ok": False, "detail": "not set"}
    fn = TESTS.get(env)
    if fn is None:
        return {"ok": None, "detail": "no test for this value"}
    try:
        return fn(key)
    except (requests.RequestException, ValueError) as e:
        return {"ok": False, "detail": f"the service could not be reached ({type(e).__name__})"}


def test_wordpress(base_url: str | None = None, username: str | None = None, app_password: str | None = None) -> dict:
    """The site, the user and the application password, tried: the site's name, the user and
    their role, the categories and the users who can write posts; or the exact error."""
    from .tools.wp_publisher import NotSent, WPPublisher

    wp = WPPublisher(base_url, username, app_password)
    if not wp.base_url or not wp._auth[0] or not wp._auth[1]:
        return {"ok": False, "detail": "the site's address, the user and the application password are all needed"}
    if wp.refused:
        return {"ok": False, "detail": wp.refused}
    try:
        root = requests.get(f"{wp.base_url}/wp-json", timeout=TIMEOUT)
        if root.status_code != 200:
            return {"ok": False, "detail": f"no WordPress REST API at {wp.base_url} (it answered {root.status_code})"}
        site = (root.json() or {}).get("name") or wp.base_url
        me = requests.get(f"{wp.api}/users/me", params={"context": "edit"}, auth=wp.auth, timeout=TIMEOUT)
        if me.status_code in (401, 403):
            return {"ok": False, "site": site, "detail": "the user or the application password is wrong"}
        if me.status_code != 200:
            return {"ok": False, "site": site, "detail": f"WordPress answered {me.status_code} for the user"}
        user = me.json() or {}
        roles = user.get("roles") or []
        cats = requests.get(f"{wp.api}/categories", params={"per_page": 100}, auth=wp.auth, timeout=TIMEOUT)
        categories = [c.get("name") for c in (cats.json() if cats.status_code == 200 else []) if c.get("name")]
        users = requests.get(f"{wp.api}/users", params={"per_page": 100, "context": "edit"}, auth=wp.auth, timeout=TIMEOUT)
        authors = [{"id": u.get("id"), "name": u.get("name")} for u in (users.json() if users.status_code == 200 else [])
                   if u.get("id") and set(u.get("roles") or []) & {"administrator", "editor", "author"}]
    except NotSent as e:
        return {"ok": False, "detail": str(e)}
    except (requests.RequestException, ValueError) as e:
        return {"ok": False, "detail": f"the site could not be reached ({type(e).__name__})"}
    can = bool(set(roles) & {"administrator", "editor", "author"})
    return {"ok": can, "site": site, "user": user.get("name") or user.get("slug"), "roles": roles,
            "categories": categories, "authors": authors,
            "detail": (f"connected to {site} as {user.get('name') or user.get('slug')} ({', '.join(roles) or 'no role'})"
                       if can else f"connected, but the user's role ({', '.join(roles) or 'none'}) cannot publish posts")}


# ---------------------------------------------------------------------------------------------
# the answers kept: shown until the key changes, and required before a run
# ---------------------------------------------------------------------------------------------
def _envs(name: str) -> tuple:
    from . import api_keys

    return tuple(api_keys.WP_GROUP) if name == WORDPRESS else (name,)


def _fingerprint(output_dir, name: str) -> str:
    from . import api_keys

    return api_keys.fingerprint(output_dir, _envs(name))


def snapshot(output_dir, name: str) -> tuple[list[str], str, str]:
    """The credentials in force for a key (or "WordPress", its three), their fingerprint and why
    the WordPress password may not be sent, taken together from one reading before a test: the
    test asks about exactly these values and its answer is kept for them, so a key replaced while
    the service answers stays untested, and a site saved meanwhile never gets another's password."""
    from . import api_keys

    return api_keys.snapshot(output_dir, _envs(name))


def _run(name: str, snap: tuple) -> dict:
    vals, _, refusal = snap
    if name != WORDPRESS:
        return test_key(name, vals[0])
    if refusal:                       # checked here: a password passed on is trusted as given
        return {"ok": False, "detail": refusal}
    return test_wordpress(*vals)


def record(output_dir, name: str, res: dict, tested_for: str | None = None) -> dict:
    """Keep a key's test answer with the fingerprint of the key it tested (`tested_for`, taken
    before the test; else the key in force now)."""
    from . import project_settings

    entry = {"ok": res.get("ok"), "detail": res.get("detail") or "",
             "for": tested_for if tested_for is not None else _fingerprint(output_dir, name),
             "checked_at": time.strftime("%Y-%m-%d %H:%M")}
    data = project_settings.load(output_dir)
    data["tests"] = {**(data.get("tests") or {}), name: entry}
    project_settings._write(output_dir, data)
    return entry


def recorded(output_dir) -> dict:
    """The kept answers still about the credentials in force, by key name and "WordPress": a
    key replaced or removed since is not tested yet."""
    from . import project_settings

    tests = project_settings.load(output_dir).get("tests") or {}
    out = {n: t for n, t in tests.items() if isinstance(t, dict) and t.get("for") and t["for"] == _fingerprint(output_dir, n)}
    site = project_settings.wp_site(output_dir)
    if site:
        out[WORDPRESS] = {"ok": bool(site.get("ok")), "detail": site.get("detail") or "",
                          "checked_at": site.get("checked_at") or ""}
    return out


CHANGED = "it changed during the test, so it is not tested: test it again"


def fresh(output_dir, answers: dict) -> dict:
    """Answers (by key name, or "WordPress") as they hold for the credentials in force now: one
    about a key replaced while the service answered says so instead of its old result."""
    now = recorded(output_dir)
    return {n: a if n in now else {**a, "ok": None, "detail": CHANGED} for n, a in answers.items()}


def keep_wordpress(output_dir, res: dict, tested_for: str | None = None) -> dict:
    """The WordPress test's answer kept: what it read from the site, for the pickers."""
    from . import project_settings

    site = {"ok": bool(res.get("ok")), "site": res.get("site"), "detail": res.get("detail") or "",
            "categories": res.get("categories") or [], "authors": res.get("authors") or [],
            "checked_at": time.strftime("%Y-%m-%d %H:%M")}
    project_settings.save_wordpress(output_dir, site=site, tested_for=tested_for)
    return site


def _keep(output_dir, name: str, res: dict, tested_for: str) -> dict:
    return (keep_wordpress(output_dir, res, tested_for) if name == WORDPRESS
            else record(output_dir, name, res, tested_for))


def check(output_dir, name: str) -> dict:
    """Test one key (or "WordPress") and keep the answer."""
    from . import api_keys

    api_keys.apply(output_dir)
    snap = snapshot(output_dir, name)
    return _keep(output_dir, name, _run(name, snap), snap[1])


def check_all(output_dir, only_untested: bool = False) -> dict:
    """Every required credential that is set, tested at once (free calls), the answers kept.
    `only_untested` skips the ones that already passed for the key in force."""
    import os

    from . import api_keys

    api_keys.apply(output_dir)
    done = recorded(output_dir)
    names = [env for env in REQUIRED if os.getenv(env)]
    if all(os.getenv(e) for e in api_keys.WP_GROUP):
        names.append(WORDPRESS)
    if only_untested:
        names = [n for n in names if (done.get(n) or {}).get("ok") is not True]
    if not names:
        return {}
    snaps = {n: snapshot(output_dir, n) for n in names}
    with ThreadPoolExecutor(max_workers=len(names)) as ex:
        answers = list(ex.map(lambda n: (n, _run(n, snaps[n])), names))
    # kept one by one: _project.json is written by this thread only
    return {n: _keep(output_dir, n, res, snaps[n][1]) for n, res in answers}
