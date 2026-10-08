"""API keys set from the Studio, so anyone running the app can use their own accounts.

Stored on the persistent volume (`project/output/_secrets.json`, owner-only permissions, never in
git or an image), applied to the process environment before a run, and never shown back in full:
the Studio displays only whether a key is set, where it comes from and its last four characters.

Precedence: a key saved in the Studio overrides the environment (.env or the deployment's
secrets), so a new team can use its own accounts without touching the server.

The WordPress site, user and application password are ONE credential. A password is sent only
to the site it was entered for: changing the site or the user needs the password in the same
save, the site is recorded with the password, and the publisher refuses any other host. Without
that, saving only a new site URL sent the server's own password to that site on the next
publish (review of 5fb183c).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

SECRETS_FILE = "_secrets.json"
_ORIGINAL: dict[str, str | None] = {}     # the environment's own value, before a Studio key replaced it
_APPLIED: set[str] = set()                # the names this process took from the file
_STATE = {"dir": None}                    # the output folder apply() last read

KEYS = [
    {"env": "ANTHROPIC_API_KEY", "label": "Anthropic", "secret": True,
     "help": "Claude models: research planning, the writer, the review panel, the fixer."},
    {"env": "OPENAI_API_KEY", "label": "OpenAI", "secret": True,
     "help": "The GPT read in the review panel, the cover and illustrations."},
    {"env": "PARALLEL_API_KEY", "label": "Parallel", "secret": True,
     "help": "Deep research and the web check of claims the research does not cover."},
    {"env": "AHREFS_API_KEY", "label": "Ahrefs", "secret": True,
     "help": "Keyword research and search results: an Ahrefs MCP key (Account settings > API keys)."},
    {"env": "SCRAPFLY_KEY", "label": "Scrapfly", "secret": True,
     "help": "Screenshots and competitor pages opened from the article's country."},
    {"env": "WINSTON_API_KEY", "label": "Winston AI", "secret": True,
     "help": "The uniqueness check, 2 credits a word."},
    {"env": "WP_BASE_URL", "label": "WordPress site URL", "secret": False,
     "help": "The blog the articles are published to, e.g. https://blog.example.com. Changing it needs "
             "the application password in the same save."},
    {"env": "WP_USERNAME", "label": "WordPress user", "secret": False,
     "help": "The user the application password belongs to. Changing it needs the application password "
             "in the same save."},
    {"env": "WP_APP_PASSWORD", "label": "WordPress application password", "secret": True,
     "help": "Created in WordPress under Users > Profile > Application Passwords. It is sent only to the "
             "site it was entered for."},
]
_ENVS = {k["env"] for k in KEYS}
WP_GROUP = ("WP_BASE_URL", "WP_USERNAME", "WP_APP_PASSWORD")
_SPACES_ALLOWED = {"WP_APP_PASSWORD"}     # WordPress shows application passwords in groups of four


def label(env: str) -> str:
    """A key's name as the pages show it ("WordPress" for the connection)."""
    return next((k["label"] for k in KEYS if k["env"] == env), env)


def file_path(output_dir) -> Path:
    return Path(output_dir) / SECRETS_FILE


def _read(output_dir) -> dict:
    try:
        data = json.loads(file_path(output_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load(output_dir) -> dict:
    return _stored(_read(output_dir))


def _meta(output_dir) -> dict:
    m = _read(output_dir).get("_meta")
    return m if isinstance(m, dict) else {}


def _host(url: str | None) -> str:
    return (urlparse(str(url or "")).hostname or "").lower()


def _environment(env: str) -> str | None:
    """The server's own value, whatever the Studio put over it."""
    return _ORIGINAL[env] if env in _ORIGINAL else os.environ.get(env)


def clean(env: str, value) -> str:
    """A pasted value as it should be stored: surrounding whitespace and quotes removed. Raises
    ValueError for a value that cannot be a key (a line break, spaces inside, a plain-http blog)."""
    v = str(value or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        v = v[1:-1].strip()
    label = next(k["label"] for k in KEYS if k["env"] == env)
    if re.search(r"[\x00-\x1f\x7f]", v):
        raise ValueError(f"{label}: the value contains a line break or a control character")
    if " " in v and env not in _SPACES_ALLOWED:
        raise ValueError(f"{label}: the value contains spaces; paste the key alone")
    if env == "WP_BASE_URL" and v:
        u = urlparse(v)
        if u.scheme != "https" or not u.hostname:
            raise ValueError("WordPress site URL: use the full https:// address of the blog")
        v = v.rstrip("/")
    return v


def save(output_dir, updates: dict, clear=()) -> list[str]:
    """Merge new values into the stored keys; `clear` removes some. An empty value keeps what is
    stored. Returns the names changed. Raises ValueError, changing nothing, for a value that
    cannot be stored or a WordPress change that would send the password somewhere new.
    The file is written owner-only."""
    raw = _read(output_dir)
    stored = {k: v for k, v in raw.items() if k in _ENVS and isinstance(v, str) and v}
    meta = dict(raw.get("_meta") or {}) if isinstance(raw.get("_meta"), dict) else {}
    new = {}
    for env, value in (updates or {}).items():
        if env not in _ENVS:
            continue
        v = clean(env, value)
        if v and stored.get(env) != v:
            new[env] = v
    clear = [e for e in (clear or ()) if e in stored]
    if any(e in WP_GROUP for e in clear):
        # one credential: removing any part of the Studio's WordPress set removes all of it, so the
        # server's own site, user and password apply together again
        clear = list(dict.fromkeys(clear + [e for e in WP_GROUP if e in stored]))
        new = {k: v for k, v in new.items() if k not in WP_GROUP}
    current = {e: stored.get(e) or _environment(e) for e in WP_GROUP}
    moves = [e for e in ("WP_BASE_URL", "WP_USERNAME") if e in new and new[e] != current[e]]
    if moves and "WP_APP_PASSWORD" not in new:
        raise ValueError("To change the WordPress site or user, enter its application password in the "
                         "same save: a password is sent only to the site it was entered for.")
    changed = list(new)
    stored.update(new)
    if "WP_APP_PASSWORD" in new:
        meta["wp_host"] = _host(new.get("WP_BASE_URL") or current["WP_BASE_URL"])
        meta["wp_user"] = new.get("WP_USERNAME") or current["WP_USERNAME"] or ""
    for env in clear:
        del stored[env]
        changed.append(env)
    if "WP_APP_PASSWORD" not in stored:
        meta.pop("wp_host", None)
        meta.pop("wp_user", None)
    for env in clear:
        _restore(env)
    out = dict(stored)
    if meta:
        out["_meta"] = meta
    from .store import write_json

    write_json(file_path(output_dir), out, mode=0o600)    # owner-only from the first byte
    return changed


def _restore(env: str) -> None:
    """This process goes back to the environment's own value (or none)."""
    if env in _ORIGINAL:
        if _ORIGINAL[env] is None:
            os.environ.pop(env, None)
        else:
            os.environ[env] = _ORIGINAL[env]
    _APPLIED.discard(env)


def apply(output_dir) -> list[str]:
    """Put the stored keys into the environment the agents read. A key another process removed
    from the file goes back to the environment's own value. Returns the names applied."""
    _STATE["dir"] = Path(output_dir)
    stored = load(output_dir)
    for env in list(_APPLIED - set(stored)):
        _restore(env)
    for env, value in stored.items():
        _ORIGINAL.setdefault(env, os.environ.get(env))
        os.environ[env] = value
        _APPLIED.add(env)
    return sorted(stored)


def _default_dir():
    """The folder apply() read, else the configured output folder (a process that never loaded
    the project still checks the file the Studio writes)."""
    if _STATE["dir"] is not None:
        return _STATE["dir"]
    try:
        from .config import load_core_config
        from .store import output_root

        return output_root(load_core_config())
    except Exception:  # noqa: BLE001 — no config, no Studio keys
        return None


def wp_refusal(base_url: str | None, username: str | None = None, output_dir=None) -> str:
    """Why the WordPress password must not be sent to `base_url` as `username` (empty when it
    may). The password goes only to the site and user it was entered with: a Studio password to
    the host recorded with it, the server's password only while the site and user are the
    server's own."""
    d = output_dir if output_dir is not None else _default_dir()
    if d is None:
        return ""
    return _refusal(_read(d), base_url, username)


def _stored(raw: dict) -> dict:
    return {k: v for k, v in raw.items() if k in _ENVS and isinstance(v, str) and v}


def _refusal(raw: dict, base_url: str | None, username: str | None) -> str:
    """wp_refusal on one reading of the saved keys (their values and the site they are bound to)."""
    stored = _stored(raw)
    meta = raw.get("_meta") if isinstance(raw.get("_meta"), dict) else {}
    host = _host(base_url)
    if "WP_APP_PASSWORD" in stored:
        bound = meta.get("wp_host") or ""
        if bound != host:
            return (f"the WordPress password saved in the Studio was entered for {bound or 'another site'}, "
                    f"not {host or 'this site'}: enter the site URL and its password together in Configure > Connections")
        if username is not None and meta.get("wp_user") and meta["wp_user"] != username:
            return ("the WordPress user changed after the password was saved: enter the user and its "
                    "password together in Configure > Connections")
        return ""
    moved = [e for e in ("WP_BASE_URL", "WP_USERNAME") if e in stored and stored[e] != _environment(e)]
    if moved:
        return ("the WordPress site or user was changed in the Studio without its password, so the "
                "server's password is not sent: enter the password in Configure > Connections too")
    return ""


def mask(value: str | None, secret: bool = True) -> str:
    if not value:
        return ""
    if not secret:
        return value
    return "•••• " + value[-4:] if len(value) > 8 else "••••"


def current(output_dir, env: str) -> str:
    """The value in force for `env` as the Studio shows it: the saved one, else the server's."""
    return load(output_dir).get(env) or _environment(env) or ""


def fingerprint(output_dir, envs) -> str:
    """Which values a connection test was of, without the values: a short hash of what `envs`
    hold now ("" when none is set). A test result kept with another fingerprint is of other
    credentials, whoever changed them (a page, the CLI, another session)."""
    return snapshot(output_dir, envs)[1]


def snapshot(output_dir, envs) -> tuple[list[str], str, str]:
    """The values in force for `envs`, their fingerprint and, when they are the WordPress
    connection, why its password may not be sent ("" when it may): all from ONE reading of the
    saved keys, so a connection saved meanwhile is never mixed into them."""
    raw = _read(output_dir)
    stored = _stored(raw)
    vals = [stored.get(e) or _environment(e) or "" for e in envs]
    refusal = _refusal(raw, vals[0], vals[1]) if tuple(envs) == tuple(WP_GROUP) else ""
    return vals, fingerprint_of(envs, vals), refusal


def wp_credentials(output_dir, base_url: str | None = None, username: str | None = None) -> tuple[str, str, str, str]:
    """The WordPress site, user and password in force (a site or user given instead of the saved
    ones), and why the password may not be sent there: all from one reading of the saved keys."""
    raw = _read(output_dir)
    stored = _stored(raw)
    vals = [stored.get(e) or _environment(e) or "" for e in WP_GROUP]
    site, user = base_url or vals[0], username or vals[1]
    return site, user, vals[2], _refusal(raw, site, user)


def fingerprint_of(envs, vals) -> str:
    """The same fingerprint, of values already read (a test takes them before it asks)."""
    vals = [v or "" for v in vals]
    if not any(vals):
        return ""
    return hashlib.sha256("\0".join([*envs, *vals]).encode()).hexdigest()[:16]


def status(output_dir) -> list[dict]:
    """For the Studio: each key, whether it is set and where from, masked."""
    stored, meta = load(output_dir), _meta(output_dir)
    rows = []
    for k in KEYS:
        env = k["env"]
        if env in stored:
            source, value = "studio", stored[env]
        elif _environment(env):
            source, value = "environment", _environment(env)
        else:
            source, value = "missing", ""
        row = {**k, "source": source, "shown": mask(value, k["secret"])}
        if env == "WP_APP_PASSWORD" and source == "studio" and meta.get("wp_host"):
            row["shown"] += f" (for {meta['wp_host']})"
        rows.append(row)
    return rows
