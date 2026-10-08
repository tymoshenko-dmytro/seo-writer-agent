"""No message of the interface shows where the files live on the server (principle 6; reviews
RD4B-01 and the interrupted round of e8422ec): such a path names the person's account and the
layout of the machine, and helps nobody using the interface. The full text stays in the server's
log and in the job and run records.

Only diagnostics are cleaned, where they are shown: a toast, a job's error and progress, a run's
error and log, a failed send's detail, an error in JSON. What a person wrote or the machine
produced as content (a topic, an article, an AI edit's text) is never touched: a blanket filter
over every page once rewrote a URL inside the editor's text, and the editor saved it back.

`scrub` replaces, in this order: the install's own folders (the articles, the app, the home
folder) wherever they appear as a path; the file named in an OS error ("[Errno 13] Permission
denied: '/srv/team files/x'"), quoted, so spaces do not hide it; and any other absolute path
under a folder only a server has (/Users, /home, /etc, /srv, /var, /tmp…), even a short one.
`scrub_mixed` (an exception's text in a toast) cleans the first two only; `scrub_errors` (a
report quoting the article) only the file an OS error names.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

PLACEHOLDER = "(a file on the server)"
_SERVER_DIRS = ("Users", "home", "root", "private", "var", "tmp", "etc", "srv", "opt", "app", "mnt", "media",
                "Volumes", "usr", "Library", "System", "data", "workspace", "proc", "run", "ms-playwright")
_PATH_END = r"[^\s'\"<>()\[\],;]*"
_ERRNO = re.compile(r"(\[Errno -?\d+\] [^:'\"\n]+): (?:'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\")"
                    r"(?: -> (?:'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"))?")
_ROOTED = re.compile(r"(?<![\w.:/~-])/(?:" + "|".join(map(re.escape, _SERVER_DIRS)) + r")(?![\w.-])" + _PATH_END)
# the diagnostic fields of a job's result (an AI edit's `replacement` is content, never cleaned)
_DIAGNOSTIC_KEYS = ("step", "error", "detail", "message", "note", "reason")


def _install_roots() -> list[str]:
    roots: set[str] = set()
    try:
        from flask import current_app

        roots.add(str(current_app.config.get("OUTPUT_ROOT") or ""))
    except RuntimeError:                     # outside a request: the app's own folders only
        pass
    from core.config import ROOT

    roots |= {str(ROOT), str(Path.home())}
    out = set()
    for r in roots:
        if r and len(r) > 1:
            out.add(r.rstrip("/"))
            out.add(os.path.realpath(r).rstrip("/"))
    return sorted(out, key=len, reverse=True)       # the longest first: a folder inside another


def _roots_and_errors(text: str) -> str:
    for root in _install_roots():
        if root in text:
            # the install's folder where it stands as a path: not inside a URL or a longer name
            text = re.sub(r"(?<![\w.:/~-])" + re.escape(root) + r"(?![\w.-])" + _PATH_END, PLACEHOLDER, text)
    return _ERRNO.sub(lambda m: f"{m.group(1)}: {PLACEHOLDER}", text)


def scrub(text):
    """A diagnostic (an error, a job's progress) without the server's paths: the install's
    folders, the file an OS error names, any absolute path under a server-only folder. For a
    text that is only a diagnostic; anything not a string comes back as it is."""
    if not isinstance(text, str) or "/" not in text:
        return text
    return _ROOTED.sub(PLACEHOLDER, _roots_and_errors(text))


def scrub_mixed(text):
    """A text where a diagnostic sits among content (a report of the fixer's work quoting the
    article, a message naming the person's topic): only the file an OS error names and the
    install's own folders standing as a path are cleaned (review R4F5-01, R4F5-02). A route, a
    URL or an example path the person or the article wrote is kept as written."""
    if not isinstance(text, str) or "/" not in text:
        return text
    return _roots_and_errors(text)


def scrub_errors(text):
    """A report of the machine's work (the checklist, the review's reads): only the file an OS
    error names ("[Errno 13] Permission denied: '/srv/x'") is cleaned, since that shape is a
    tool's error and nothing else. A path the article or its author wrote is kept as written,
    even one under the home folder (review RC77-07)."""
    if not isinstance(text, str) or "/" not in text:
        return text
    return _ERRNO.sub(lambda m: f"{m.group(1)}: {PLACEHOLDER}", text)


def scrub_job(job: dict) -> dict:
    """A job as the interface shows it: its error and progress without paths. Its topic is what
    the person typed and its result's content is the machine's work: both as they are."""
    out = dict(job)
    for k in ("error", "step"):
        out[k] = scrub(out.get(k))
    res = out.get("result")
    if isinstance(res, str):
        out["result"] = scrub(res)
    elif isinstance(res, dict):
        out["result"] = {k: (scrub(v) if k in _DIAGNOSTIC_KEYS else v) for k, v in res.items()}
    return out
