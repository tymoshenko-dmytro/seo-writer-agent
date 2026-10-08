"""Versions: every state of an article's content is kept, so a person can go back when an edit
went wrong.

A version is what the version token covers (`store.Article.version()`, contract 1): the text
with its frontmatter (`article.md`) and the bytes of every picture the page shows and the cover.
Each file is kept once, by the SHA-256 of its bytes, in `_versions/blobs/`; `_versions/index.json`
lists the versions in order:

    {"n", "token", "at", "who", "what", "detail", "kind", "files": {"article.md": sha,
     "assets/chart_1.png": sha, ...}, "group"?, "restored_from"?}

Every write of the content goes through `change` (a save in the editor or the SEO tab, each
change group of a review, a revert, an applied proposal, a person's own paragraph, a picture, a
restore) or calls `record` after it (the pipeline's first article), under the article's lock. A
version is added only when the token differs from the last one. Before a write, `ensure` keeps
the state it starts from when no version holds it yet: the first state of an article from before
versions, or one changed without a record (by hand on the disk), as "Changed outside the
Studio". So the list never skips a state the article was in.

`restore` writes a version back as the newest one, under the lock and the version check: the
page must have shown the current version. The article returns exactly as it was: its text, its
pictures, and a picture or cover it did not have is gone again (its bytes stay in the version
before the restore). It changes the content only: the acceptance, the WordPress facts, the
review's records and the job history stay what they are, and a restore is neither an acceptance
nor a send.

The index is data: an imported article brings its own, so a restore writes only the article's
text and pictures (`store.Article.own_file`), from blobs whose bytes match their hash, and checks
all of them before it writes anything (review RC77-01).
"""
from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager
from datetime import datetime, timezone

from . import store

DIR = "_versions"
INDEX = "index.json"
_HASH = re.compile(r"[0-9a-f]{64}")
KINDS = ("pipeline", "review", "proposal", "fix", "revert", "edit", "seo", "picture", "restore",
         "import", "outside")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def index(art: store.Article) -> list[dict]:
    """The versions, oldest first ([] when none was recorded)."""
    data = store.read_json_lenient(art.path(f"{DIR}/{INDEX}"), {}) or {}
    return list(data.get("versions") or [])


def _save_index(art: store.Article, versions: list[dict]) -> None:
    store.write_json(art.path(f"{DIR}/{INDEX}"), {"schema": 1, "versions": versions})


def _keep(art: store.Article, data: bytes) -> str:
    """The bytes kept once, by their hash; their hash."""
    h = _sha(data)
    p = art.path(f"{DIR}/blobs/{h}")
    if not p.exists():
        store.atomic_write_bytes(p, data)
    return h


class Damaged(ValueError):
    """A version's record or kept bytes are not what they claim: it is not restored."""


def blob(art: store.Article, sha: str) -> bytes:
    """The bytes kept under `sha`, when they are those bytes (Damaged otherwise)."""
    if not isinstance(sha, str) or not _HASH.fullmatch(sha):
        raise Damaged(f"'{sha}' is not the name of a kept file")
    p = art.path(f"{DIR}/blobs/{sha}")
    if p.is_symlink() or not p.is_file():
        raise Damaged(f"the kept file {sha[:12]} is missing")
    data = p.read_bytes()
    if _sha(data) != sha:
        raise Damaged(f"the kept file {sha[:12]} does not match its hash")
    return data


def _snapshot(art: store.Article) -> dict[str, str] | None:
    """The content as it is now, its files kept: {"article.md": sha, picture: sha}; None when
    the article has no text."""
    src = art.path("article.md")
    if not src.is_file():
        return None
    raw = src.read_bytes()
    files = {"article.md": _keep(art, raw)}
    for ref, h in art.pictures(raw.decode("utf-8")).items():
        if h:                                   # a picture the page names but the disk lacks: nothing to keep
            files[ref] = _keep(art, art.path(ref).read_bytes())
    return files


def _add(art: store.Article, versions: list[dict], token: str, who: str, what: str, detail: str,
         kind: str, **extra) -> dict:
    files = _snapshot(art)
    rec = {"n": (versions[-1]["n"] + 1) if versions else 1, "token": token, "at": _now(), "who": who,
           "what": what, "detail": detail, "kind": kind, "files": files or {}, **extra}
    versions.append(rec)
    return rec


def ensure(art: store.Article) -> None:
    """Before a write, under the article's lock: the content as it is now is held by a version
    (the first one kept, or a state nobody recorded), so the write never loses the state it
    started from."""
    token = art.version()
    if not token:
        return
    versions = index(art)
    if not versions:
        _add(art, versions, token, "the Studio", "The first version kept",
             "the article as it was when versions began", "outside")
    elif versions[-1]["token"] != token:
        _add(art, versions, token, "someone outside the Studio", "Changed outside the Studio",
             "the files changed without a record", "outside")
    else:
        return
    _save_index(art, versions)


def record(art: store.Article, who: str, what: str, detail: str = "", kind: str = "edit",
           **extra) -> dict | None:
    """After a write, under the article's lock: the content as it is now, as a new version when
    it differs from the last one. Returns the new version (None when nothing changed)."""
    token = art.version()
    if not token:
        return None
    versions = index(art)
    if versions and versions[-1]["token"] == token:
        return None
    rec = _add(art, versions, token, who, what, detail, kind if kind in KINDS else "edit", **extra)
    _save_index(art, versions)
    return rec


@contextmanager
def change(art: store.Article, who: str, what: str, detail: str = "", kind: str = "edit", **extra):
    """Around a write of the content, under the article's lock: the state before is kept (when
    no version holds it), the state after becomes a version. The block may set `what`, `detail`
    or `extra` on the yielded dict once it knows them; after it, `version` holds the record."""
    ensure(art)
    box: dict = {}
    yield box
    box["version"] = record(art, who, box.get("what") or what, box.get("detail") or detail, kind,
                            **{**extra, **(box.get("extra") or {})})


def listing(art: store.Article) -> list[dict]:
    """The versions, newest first, the current one marked; a state nobody recorded is caught up
    when the article is free (a job holding it records its own)."""
    token = art.version()
    versions = index(art)
    if token and (not versions or versions[-1]["token"] != token):
        try:
            with art.lock(wait=False, holder="the versions list"):
                ensure(art)
                versions = index(art)
        except store.Busy:
            pass
    return [{**v, "current": v["token"] == token} for v in reversed(versions)]


def get(art: store.Article, n: int) -> dict:
    v = next((x for x in index(art) if x["n"] == n), None)
    if v is None:
        raise KeyError(f"no version {n} of '{art.slug}'")
    return v


def text_of(art: store.Article, n: int) -> str:
    v = get(art, n)
    if "article.md" not in (v.get("files") or {}):
        raise KeyError(f"version {n} of '{art.slug}' was not kept")
    return blob(art, v["files"]["article.md"]).decode("utf-8")


def compare(art: store.Article, n: int, m: int | None = None) -> dict:
    """What changed from version n to version m (None: the article now): the blocks of the body
    that differ, as before → after in the article's order, whether the frontmatter changed, and
    the pictures that changed, appeared or went."""
    import difflib

    a = text_of(art, n)
    b = art.path("article.md").read_text(encoding="utf-8") if m is None else text_of(art, m)
    (fa, ba), (fb, bb) = store.split_frontmatter(a), store.split_frontmatter(b)
    blocks_a = [x for x in ba.split("\n\n") if x.strip()]
    blocks_b = [x for x in bb.split("\n\n") if x.strip()]
    changed = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, blocks_a, blocks_b, autojunk=False).get_opcodes():
        if op != "equal":
            changed.append({"before": "\n\n".join(blocks_a[i1:i2]), "after": "\n\n".join(blocks_b[j1:j2])})
    pa = {k: v for k, v in get(art, n)["files"].items() if k != "article.md"}
    if m is None:
        pb = {}
        for ref, h in art.pictures().items():
            if h:
                pb[ref] = _sha(art.path(ref).read_bytes())
    else:
        pb = {k: v for k, v in get(art, m)["files"].items() if k != "article.md"}
    pictures = sorted(k for k in set(pa) | set(pb) if pa.get(k) != pb.get(k))
    return {"from": n, "to": m, "blocks": changed, "frontmatter_changed": fa != fb, "pictures": pictures}


def _fits(art: store.Article, paths: list) -> None:
    """The files can be written where they go: no file where a folder is needed, no folder where
    a file goes, no file named twice or inside another (Damaged otherwise), before any write."""
    root = art.dir.resolve()
    seen = {p.resolve() for p in paths}
    if len(seen) != len(paths):
        raise Damaged("a file is named twice")
    for p in paths:
        q = p.resolve()
        if q.exists() and not q.is_file():
            raise Damaged(f"'{q.relative_to(root)}' is a folder, not a file")
        for parent in q.parents:
            if parent == root:
                break
            if parent in seen or (parent.exists() and not parent.is_dir()):
                raise Damaged(f"'{q.relative_to(root)}' would need '{parent.relative_to(root)}' to be a folder")


def restore(art: store.Article, n: int, version: str, who: str) -> dict:
    """Version n written back as the newest version, when `version` (the version the page
    showed) is the current one; Stale otherwise. The text and the pictures of n return, and a
    picture or cover n did not show is taken away (kept in the version before). Every file is
    checked before the first write: a version naming a file outside its article, or kept bytes
    that do not match, is refused and nothing changes. Returns the new version."""
    from .qa_changes import Refused, Stale

    with art.lock(wait=False, holder=f"restore of version {n}"):
        if not version or version != art.version():
            raise Stale(f"'{art.slug}' changed after the page was opened: load it again and decide on what is there now")
        v = get(art, n)
        files = v.get("files") or {}
        if not isinstance(files, dict) or "article.md" not in files:
            raise KeyError(f"version {n} of '{art.slug}' was not kept, so it cannot be restored")
        try:
            plan = [(ref, art.own_file(ref), blob(art, sha)) for ref, sha in files.items()]
            text = dict((r, d) for r, _, d in plan)["article.md"]
            _fits(art, [p for _, p, _ in plan])
            # a picture the restored text shows (or the cover it falls back to) that version n
            # did not have goes again, so the article is exactly version n: what goes is decided
            # and checked here too, before anything is written (review RB32-01). Writing the
            # version's own files changes no other file, so the disk now says what will be there.
            gone = [art.own_file(ref) for ref in _shown_by(art, text.decode("utf-8"))
                    if ref not in files and (art.path(ref).is_file() or art.path(ref).is_symlink())]
        except (store.UnsafePath, Damaged) as e:
            raise Refused(f"version {n} cannot be restored: {e}") from None
        except UnicodeDecodeError:
            raise Refused(f"version {n} cannot be restored: its text is not text") from None
        with change(art, who, f"Restored v{n}", v.get("what") or "", kind="restore", restored_from=n) as box:
            for ref, path, data in plan:
                if ref != "article.md":
                    store.atomic_write_bytes(path, data)
            store.atomic_write_bytes(art.path("article.md"), text)
            for path in gone:
                path.unlink()
        return box["version"] or {**index(art)[-1], "unchanged": True}


def _shown_by(art: store.Article, text: str) -> list[str]:
    """The pictures a text shows and the cover it names or falls back to, as files of the
    article (store.Article.pictures without reading their bytes)."""
    block, body = store.split_frontmatter(text)
    refs = set(store.shown_images(body))
    cover = store.cover_asset(store.frontmatter_fields(block), art.dir)
    if cover:
        refs.add(cover)
    return sorted(refs)
