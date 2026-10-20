"""A project's own research sources: data the project reads by itself that the research step
cannot find (a leaderboard drawn by JavaScript, an internal price list), added to the research
every agent reads, for the articles it is meant for.

A source is a plain Python module in the project folder, `<project>/research_sources/<name>.py`
(a file whose name starts with "_" is skipped). The project folder is the operator's own
configuration, trusted like the code; the output volume and the Studio's settings never hold one.
A project without the folder, as every new install, sees nothing of this: no note, no block, no
setting.

    NAME = "board"                      # how notes and the log name it (default: the file's name)
    KEEP = ("_board_snapshot.json",)    # optional: files a rerun copies from the source folder

    def gate(article_dir, topic, keyword, project) -> str
        # "on", "off" or "pinned". Called before the writer's first view of the research and
        # when a review rebuilds an article's state; the source keeps its decision in the
        # article's folder, so every later pass reads what the writer read
    def corpus_block(article_dir, project) -> str
        # added after the research in the one view every agent reads; "" for nothing. A URL
        # written in it as "[source: https://…]", the research's own mark, may be cited and
        # linked by the fixer like a researched source

    def leaves_out(article_dir, url) -> bool
        # optional: a source or a screenshot target this article must not use

A source that fails to load or to answer, or names files it may not keep, is logged and left out:
it never fails a run. Each load runs the file's current text (never a cached bytecode).
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from types import ModuleType

DIR = "research_sources"
_log = logging.getLogger(__name__)
_LOADED: dict[tuple[str, int, int], ModuleType | None] = {}


def _root(project) -> Path | None:
    root = getattr(project, "root", None)
    return Path(root) if root else None


def sources(project) -> list[ModuleType]:
    """The project's sources, each file loaded once per its time and size."""
    root = _root(project)
    folder = root / DIR if root else None
    try:
        if not folder or not folder.is_dir():
            return []
        files = sorted(p for p in folder.glob("*.py") if not p.name.startswith("_"))
    except OSError as e:
        _log.warning("research sources could not be listed: %s", e)
        return []
    out = []
    for p in files:
        try:
            st = p.stat()
        except OSError as e:          # a dangling link, a file gone meanwhile: that one is left out
            _log.warning("research source %s could not be read: %s", p.name, e)
            continue
        key = (str(p.resolve()), st.st_mtime_ns, st.st_size)
        if key not in _LOADED:
            # one entry per file: a file whose text changed replaces its older entry, and a
            # neighbour added or removed never runs an unchanged file again
            for old in [k for k in _LOADED if k[0] == key[0]]:
                del _LOADED[old]
            _LOADED[key] = _load(p)
        mod = _LOADED[key]
        if mod is not None:
            sys.modules[mod.__name__] = mod      # the registration is always the module in use
            out.append(mod)
    return out


def _load(p: Path) -> ModuleType | None:
    """One source, from its file's current text: compiled here, so a bytecode cache that matches
    an older text by its time and size is never run instead."""
    # one name per project folder and file: two projects' sources of the same file name in one
    # process never take each other's place in sys.modules
    import hashlib

    folder = hashlib.sha1(str(p.parent.resolve()).encode("utf-8")).hexdigest()[:10]
    name = f"research_source_{folder}_{p.stem}"
    mod = ModuleType(name)
    mod.__file__ = str(p)
    # registered before it runs, as an import is: a dataclass (or anything else that looks its
    # module up) in the source needs it
    sys.modules[name] = mod
    try:
        exec(compile(p.read_text(encoding="utf-8"), str(p), "exec"), mod.__dict__)
    except Exception as e:  # noqa: BLE001 — a broken source is left out, never a failed run
        sys.modules.pop(name, None)
        _log.warning("research source %s could not be loaded: %s", p.name, e)
        return None
    if not callable(getattr(mod, "gate", None)) or not callable(getattr(mod, "corpus_block", None)):
        _log.warning("research source %s has no gate() and corpus_block(): left out", p.name)
        return None
    return mod


def name_of(src: ModuleType) -> str:
    return str(getattr(src, "NAME", "") or Path(getattr(src, "__file__", "") or src.__name__).stem)


def gate_all(article_dir, topic: str, keyword: str, project) -> list[str]:
    """Each source decides, once per article, whether the article is in its scope; returns the
    names of those that said "off" (a note each)."""
    off = []
    for src in sources(project):
        try:
            if src.gate(Path(article_dir), topic or "", keyword or "", project) == "off":
                off.append(name_of(src))
        except Exception as e:  # noqa: BLE001
            _log.warning("research source %s: gate failed: %s", name_of(src), e)
    return off


def decide(article_dir, project) -> list[str]:
    """`gate_all` for an article from its own record (the run's topic and primary keyword): what the
    Studio does before its first read of an article written or imported before a source decided,
    so a picture made there never reads, or pins, what the article's topic leaves out."""
    import json

    if not article_dir or not sources(project):
        return []
    try:
        run = json.loads((Path(article_dir) / "_run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        run = {}
    run = run if isinstance(run, dict) else {}
    topic = str(run.get("topic") or (run.get("brief") or {}).get("title") or "")
    keyword = str((run.get("keyword_research") or {}).get("primary_keyword") or "")
    return gate_all(article_dir, topic, keyword, project)


def corpus_blocks(article_dir, project) -> str:
    """What the sources add to the research of this article, in their files' order."""
    if not article_dir:
        return ""
    out = []
    for src in sources(project):
        try:
            block = src.corpus_block(Path(article_dir), project)
        except Exception as e:  # noqa: BLE001
            _log.warning("research source %s: corpus_block failed: %s", name_of(src), e)
            continue
        if block and str(block).strip():
            out.append(str(block).strip())
    return "\n\n".join(out)


def leaves_out(article_dir, url, project) -> bool:
    """Whether a source says this article must not use `url` (as a source or a screenshot)."""
    if not article_dir or not url:
        return False
    for src in sources(project):
        fn = getattr(src, "leaves_out", None)
        try:
            if callable(fn) and fn(Path(article_dir), str(url)):
                return True
        except Exception as e:  # noqa: BLE001
            _log.warning("research source %s: leaves_out failed: %s", name_of(src), e)
    return False


_CITED = re.compile(r"\[source:\s*(https?://[^\]\s]+)\s*\]")


def cited_urls(article_dir, project) -> set[str]:
    """The URLs the sources' blocks for this article mark "[source: …]": what the fixer may link,
    as it may link a researched source (the review asked for the citation it saw)."""
    return set(_CITED.findall(corpus_blocks(article_dir, project)))


def keep_files(project) -> tuple[str, ...]:
    """The files a rerun copies from its source folder, for every source: plain names only (a
    source whose KEEP is not a list of names is logged and its KEEP left out)."""
    out: list[str] = []
    for src in sources(project):
        keep = getattr(src, "KEEP", ()) or ()
        if isinstance(keep, str) or not isinstance(keep, (tuple, list)):
            _log.warning("research source %s: KEEP is not a list of file names: left out", name_of(src))
            continue
        for name in keep:
            if not isinstance(name, str):
                _log.warning("research source %s: KEEP holds %r, not a file name: left out", name_of(src), name)
                continue
            if name and "/" not in name and "\\" not in name and name not in (".", "..") and name not in out:
                out.append(name)
    return tuple(out)


def copy_kept(src_dir, dst_dir, project) -> list[str]:
    """A rerun's copy of what the sources keep: each a regular file inside the source folder (not a
    link, not a folder), copied as its bytes; what cannot be copied is logged, never a failed run."""
    import shutil

    src_dir, dst_dir, done = Path(src_dir), Path(dst_dir), []
    base = src_dir.resolve()
    for name in keep_files(project):
        p = src_dir / name
        try:
            if not p.exists() and not p.is_symlink():
                continue
            if p.is_symlink() or not p.is_file() or p.resolve().parent != base:
                _log.warning("research sources: %s in %s is not a plain file of the article: not copied", name, src_dir.name)
                continue
            shutil.copyfile(p, dst_dir / name, follow_symlinks=False)
            done.append(name)
        except OSError as e:
            _log.warning("research sources: %s could not be copied: %s", name, e)
    return done
