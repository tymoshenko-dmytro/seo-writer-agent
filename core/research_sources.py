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
        # added after the research in the one view every agent reads; "" for nothing
    def leaves_out(article_dir, url) -> bool
        # optional: a source or a screenshot target this article must not use

A source that fails to load or to answer is logged and left out: it never fails a run.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType

DIR = "research_sources"
_log = logging.getLogger(__name__)
_LOADED: dict[tuple[str, tuple], list[ModuleType]] = {}


def _root(project) -> Path | None:
    root = getattr(project, "root", None)
    return Path(root) if root else None


def sources(project) -> list[ModuleType]:
    """The project's sources, loaded once per folder and its files' times."""
    root = _root(project)
    folder = root / DIR if root else None
    if not folder or not folder.is_dir():
        return []
    files = sorted(p for p in folder.glob("*.py") if not p.name.startswith("_"))
    key = (str(folder.resolve()), tuple((p.name, p.stat().st_mtime_ns) for p in files))
    if key not in _LOADED:
        loaded = []
        for p in files:
            name = f"research_source_{p.stem}"
            try:
                spec = importlib.util.spec_from_file_location(name, p)
                mod = importlib.util.module_from_spec(spec)
                # registered before it runs, as an import is: a dataclass (or anything else that
                # looks its module up) in the source needs it
                sys.modules[name] = mod
                spec.loader.exec_module(mod)
            except Exception as e:  # noqa: BLE001 — a broken source is left out, never a failed run
                sys.modules.pop(name, None)
                _log.warning("research source %s could not be loaded: %s", p.name, e)
                continue
            if not callable(getattr(mod, "gate", None)) or not callable(getattr(mod, "corpus_block", None)):
                _log.warning("research source %s has no gate() and corpus_block(): left out", p.name)
                continue
            loaded.append(mod)
        _LOADED[key] = loaded
    return _LOADED[key]


def name_of(src: ModuleType) -> str:
    return str(getattr(src, "NAME", "") or src.__name__.removeprefix("research_source_"))


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


def keep_files(project) -> tuple[str, ...]:
    """The files a rerun copies from its source folder, for every source."""
    out: list[str] = []
    for src in sources(project):
        for name in getattr(src, "KEEP", ()) or ():
            name = str(name)
            if name and "/" not in name and "\\" not in name and name not in (".", "..") and name not in out:
                out.append(name)
    return tuple(out)
