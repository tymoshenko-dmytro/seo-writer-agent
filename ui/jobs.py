"""Background jobs for the Studio.

One worker thread runs jobs one at a time (Ahrefs/API rate limits make parallel runs pointless,
and an article's lock would serialize them anyway). The job list persists to JOBS_FILE so a
restart keeps the history; a job that was queued or running when the process stopped is shown
as interrupted. The callables themselves live in memory only, so an interrupted job is not
re-run on its own: it may already have paid for part of its work (the article's cost ledger and
its `running`/`failed` run record say how far it got).

Anything that talks to the network belongs here rather than in a request handler: behind an
ingress and Cloudflare a slow synchronous handler just returns 502 with no clue why.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

def _jobs_file() -> Path:
    """The job list, on the article volume unless JOBS_FILE says otherwise: next to ui/ it was
    lost on every release."""
    if os.getenv("JOBS_FILE"):
        return Path(os.environ["JOBS_FILE"])
    from core.store import output_root

    return output_root() / "_jobs.json"


JOBS_FILE = _jobs_file()
KEEP = 200          # finished jobs kept in the history

def human_error(e: Exception) -> str:
    """A failed job's reason as the person reads it. A missing or refused key came through as the
    SDK's own words ("Could not resolve authentication method. Expected one of api_key…",
    2026-10-05); it says which key and where to set it."""
    text, name = str(e), type(e).__name__
    if "Could not resolve authentication method" in text or ("anthropic" in type(e).__module__ and name == "AuthenticationError"):
        return "The Anthropic key is missing or was not accepted: set it in Configure > Connections."
    if ("openai" in type(e).__module__ and name == "AuthenticationError") or "OPENAI_API_KEY is not set" in text:
        return "The OpenAI key is missing or was not accepted: set it in Configure > Connections."
    if name == "AuthenticationError":
        return "A key was not accepted: check it in Configure > Connections."
    return text

_LOCK = threading.Lock()
_CURRENT = threading.local()        # the job the worker runs now (its record), for submit_on_article
# Guards the check-and-start of the worker thread: two first submits on different gunicorn
# threads both saw "no worker" and started two loops, running jobs in parallel.
_WORKER_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class JobRunner:
    def __init__(self, path: Path = JOBS_FILE):
        self.path = Path(path)
        self.jobs: dict[str, dict] = {}
        self._queue: list[str] = []
        self._fns: dict[str, callable] = {}   # in-memory: callables can't be persisted
        self._worker = None
        self.boots = 1
        self._load()

    def _load(self):
        """Read the history; nothing is written until the first job (importing the Studio must
        not touch the volume)."""
        try:
            self.jobs = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.jobs = {}
        meta = self.jobs.pop("_boots", None) or {}
        self.boots = int(meta.get("n", 0)) + 1
        for j in self.jobs.values():
            if j.get("status") in ("queued", "running"):
                j["status"] = "interrupted"
                j["step"] = "interrupted by a restart"

    def _save(self):
        from core.store import write_json

        real = sorted((j for j in self.jobs.values() if j.get("status") != "meta"),
                      key=lambda j: j.get("created") or "", reverse=True)
        keep = {j["id"]: j for j in real[:KEEP]}
        keep["_boots"] = {"id": "_boots", "n": self.boots, "status": "meta", "created": _now(), "at": _now()}
        try:
            write_json(self.path, keep)
        except OSError:
            pass

    def _add(self, job: dict, fn=None) -> str:
        with _LOCK:
            # the order of submission: `created` has a second's precision, and jobs made in one
            # second must still list in the order the worker takes them (review RA7C-01)
            job["seq"] = max((j.get("seq") or 0 for j in self.jobs.values()), default=0) + 1
            self.jobs[job["id"]] = job
            if fn is not None:
                self._fns[job["id"]] = fn
            self._queue.append(job["id"])
            self._save()
        self._ensure_worker()
        return job["id"]

    def submit(self, topic: str, use_ahrefs: bool = True, processor: str | None = None, **overrides) -> str:
        """A new article. `processor` None = the profile's default research depth. `overrides`
        go to generate_article as they are: the target length, the market's country, a run again
        from an article's cached research (reuse_from, reuse_in_place)."""
        return self._add({"id": uuid.uuid4().hex[:8], "topic": topic, "kind": "article",
                          "status": "queued", "step": "queued", "use_ahrefs": use_ahrefs,
                          "processor": processor, "overrides": {k: v for k, v in overrides.items() if v not in (None, "")},
                          "created": _now(), "finished": None,
                          "slug": overrides.get("reuse_from") if overrides.get("reuse_in_place") else None,
                          "error": None, "result": None})

    def submit_task(self, label: str, fn, kind: str = "task", slug: str | None = None,
                    asset: str | None = None, **extra) -> str:
        """Queue a task. `fn(status_callback)` returns a summary string or a dict for the UI.
        `asset`: the picture it makes again, so the page can say so on that picture; `extra`
        (findings, after, version) goes into the record as it is."""
        return self._add({"id": uuid.uuid4().hex[:8], "topic": label, "kind": kind, "status": "queued",
                          "step": "queued", "created": _now(), "finished": None, "slug": slug,
                          "error": None, "result": None, "asset": asset, **extra}, fn)

    def on_article(self, slug: str) -> list[dict]:
        """The jobs queued or running on one article, in the order the worker takes them."""
        return sorted((j for j in self.jobs.values() if j.get("slug") == slug and j.get("status") in ("queued", "running")),
                      key=lambda j: (j["created"], j.get("seq") or 0))

    def submit_on_article(self, art, label: str, fn, kind: str, version: str, asset: str | None = None,
                          findings: list[str] | None = None) -> str:
        """A change to an article while other jobs work on it: it waits behind them and runs on the
        version they leave, as long as nothing else changed the article. A Redraw that was running
        made every other change on the page answer "a job is already working on this article"
        (2026-10-06). Each job records the version it found and the one it left; the engine takes
        the page's version for the one the jobs before it left (core/qa_changes.following)."""
        after = [j["id"] for j in self.on_article(art.slug)]

        def run(cb):
            from core.qa_changes import following

            me = getattr(_CURRENT, "job", None)
            left = self.left_by(after, version)
            self._note(me, v_from=art.version())
            try:
                with following(version, left):
                    return fn(cb)
            finally:
                self._note(me, v_to=art.version())

        jid = self.submit_task(label, run, kind=kind, slug=art.slug, **({"asset": asset} if asset else {}))
        with _LOCK:     # what it works on, for the page (the queue itself is in `run`)
            if jid in self.jobs:
                self.jobs[jid].update(findings=list(findings or []), after=after, version=version)
                self._save()
        return jid

    def left_by(self, after: list[str], version: str) -> str:
        """The version the jobs `after` left, starting from `version`: each one that started on it
        moves it on to the version it ended at."""
        v = version
        for jid in after:
            j = self.jobs.get(jid) or {}
            if v and j.get("v_from") == v and j.get("v_to"):
                v = j["v_to"]
        return v

    def _note(self, job: dict | None, **fields):
        if job is None:
            return
        with _LOCK:
            job.update(fields)
            self._save()

    def latest(self, kind: str, slug: str | None = None) -> dict | None:
        jobs = [j for j in self.jobs.values() if j.get("kind") == kind
                and (slug is None or j.get("slug") == slug)]
        return max(jobs, key=lambda j: j["created"]) if jobs else None

    def active(self, kind: str, slug: str | None = None) -> dict | None:
        j = self.latest(kind, slug)
        return j if j and j.get("status") in ("queued", "running") else None

    def _ensure_worker(self):
        with _WORKER_LOCK:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._loop, daemon=True)
                self._worker.start()

    def _loop(self):
        while True:
            with _LOCK:
                jid = self._queue.pop(0) if self._queue else None
            if jid is None:
                time.sleep(0.2)
                continue
            job = self.jobs[jid]
            with _LOCK:
                job["status"] = "running"
                self._save()

            def cb(step, _jid=jid):
                with _LOCK:
                    self.jobs[_jid]["step"] = str(step)
                    self._save()

            _CURRENT.job = job
            try:
                fn = self._fns.pop(jid, None)
                if fn is not None:
                    summary = fn(cb)
                    with _LOCK:
                        job["status"] = "done"
                        job["result"] = summary
                        job["step"] = (summary.get("step", "finished") if isinstance(summary, dict)
                                       else str(summary or "finished"))
                else:
                    from core.orchestrator import generate_article

                    kw = {"use_ahrefs": job.get("use_ahrefs", True)}
                    if job.get("processor"):
                        kw["processor"] = job["processor"]
                    kw.update(job.get("overrides") or {})
                    def on_folder(slug, _job=job):
                        with _LOCK:
                            _job["slug"] = slug
                            self._save()

                    res = generate_article(job["topic"], status_callback=cb, on_folder=on_folder, **kw)
                    with _LOCK:
                        job["status"] = "done" if res.get("status") in ("success", "needs_review") \
                            else res.get("status", "done")
                        job["slug"] = res.get("slug")
                        job["step"] = f"finished: {res.get('status')}"
            except Exception as e:  # noqa: BLE001
                with _LOCK:
                    job["status"] = "failed"
                    job["error"] = human_error(e)[:400]
                    job["step"] = "failed"
            finally:
                _CURRENT.job = None
                with _LOCK:
                    job["finished"] = _now()
                    self._save()

    def get(self, jid: str) -> dict | None:
        return self.jobs.get(jid)

    def all(self) -> list[dict]:
        """Newest first, by the time and the order they were submitted."""
        real = [j for j in self.jobs.values() if j.get("status") != "meta"]
        return sorted(real, key=lambda j: (j["created"], j.get("seq") or 0), reverse=True)


runner = JobRunner()
