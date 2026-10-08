"""The sample article: a finished article with its review, its pictures, its versions and its
costs, to look at before any key is set. It ships in `samples/sample-article/`, made by
`bin/make_sample.py` from a stand-in run (the engine's own code, every paid service answered
locally), so it costs nothing and holds nothing about any product.

`add()` puts a copy among the articles, through the same import an exported article goes
through, and only once: an install that has it keeps the copy it has (the person may have
changed it)."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from .config import ROOT
from .store import Repository
from .transfer import FORMAT, _skipped, import_article

SOURCE = ROOT / "samples" / "sample-article"
SLUG = "sample-meta-description"


def available() -> bool:
    return (SOURCE / "article.md").is_file()


def add(output_dir) -> str:
    """The sample among the articles (added now, or the copy already there); its slug."""
    repo = Repository(Path(output_dir))
    if (Path(output_dir) / SLUG / "article.md").is_file():
        return SLUG
    if not available():
        raise FileNotFoundError("the sample article is not in this install (samples/sample-article)")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        files = 0
        for p in sorted(SOURCE.rglob("*")):
            rel = p.relative_to(SOURCE).as_posix()
            if p.is_file() and not p.is_symlink() and not _skipped(rel):
                z.write(p, f"sample-article/{rel}")
                files += 1
        z.writestr("_export.json", json.dumps({"format": FORMAT, "slug": "sample-article", "files": files}))
    return import_article(repo, buf.getvalue(), slug=SLUG).slug
