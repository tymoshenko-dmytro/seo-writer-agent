"""Moving an article between installs: the local Studio and the production one, either way.

An export is a zip of the article folder (text, reports, run record, cost ledger, WordPress
binding, pictures) under `<slug>/`, plus `_export.json` saying what it is. The articles are not
in git any more, so this is how one travels.

An import never overwrites: it lands in a new folder (`<slug>`, else `<slug>-2`, …). The archive
is checked before anything is written: every entry must sit inside the one article folder (no
absolute paths, no `..`, no links), and the unpacked size is bounded.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import stat
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .store import Article, Repository, listed_slug, slugify, write_json

FORMAT = "content-studio-article/1"
MAX_UNPACKED = 400 * 1024 * 1024      # an article with every picture is ~5-40 MB
_SKIP = (".lock",)                     # per-install state, never exported


def _skipped(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name in _SKIP or (name.startswith(".") and name.endswith(".tmp")) or ".capture." in name


def export_article(art: Article) -> bytes:
    """The article folder as zip bytes, taken under the article's lock so the text, the records
    and the pictures are one moment's (review RV-12). Busy while a job works on it."""
    with art.lock(wait=False, holder="an export"):
        return _pack(art)


def _pack(art: Article) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        manifest = {"format": FORMAT, "slug": art.slug,
                    "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "files": 0}
        for p in sorted(art.dir.rglob("*")):
            if not p.is_file() or p.is_symlink():
                continue
            rel = p.relative_to(art.dir).as_posix()
            if _skipped(rel):
                continue
            z.write(p, f"{art.slug}/{rel}")
            manifest["files"] += 1
        z.writestr("_export.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return buf.getvalue()


class BadArchive(ValueError):
    pass


def import_article(repo: Repository, data: bytes, slug: str | None = None) -> Article:
    """Unpack an exported article into a NEW folder of `repo`. Returns it."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise BadArchive(f"not a zip archive ({e})") from None
    with z:
        try:
            manifest = json.loads(z.read("_export.json"))
        except (KeyError, ValueError):
            raise BadArchive("not an article export: _export.json is missing") from None
        if manifest.get("format") != FORMAT or not listed_slug(str(manifest.get("slug") or "")):
            raise BadArchive("not an article export this version can read")
        src = manifest["slug"]
        members, total = [], 0
        for info in z.infolist():
            name = info.filename
            if name == "_export.json" or info.is_dir():
                continue
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise BadArchive(f"{name}: links are not allowed")
            parts = name.split("/")
            if name.startswith("/") or "\\" in name or ".." in parts or parts[0] != src or len(parts) < 2:
                raise BadArchive(f"{name}: outside the article folder")
            total += info.file_size
            if total > MAX_UNPACKED:
                raise BadArchive("the archive unpacks to more than 400 MB")
            members.append((info, "/".join(parts[1:])))
        if not any(rel in ("article.md", "_run.json") for _, rel in members):
            raise BadArchive("the archive holds no article (no article.md or _run.json)")
        # unpacked into a hidden staging folder first (the Studio lists no `.…` folder)
        Path(repo.root).mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".import-", dir=repo.root))
        try:
            for info, rel in members:
                if _skipped(rel):
                    continue
                target = (staging / rel).resolve()
                if staging.resolve() not in target.parents:
                    raise BadArchive(f"{info.filename}: outside the article folder")
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as fh:
                    target.write_bytes(fh.read())
            write_json(staging / "_import.json",
                       {"from": src, "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "exported_at": manifest.get("exported_at")})
            # the name is reserved as an empty folder (mkdir is the atomic step), and the finished
            # staging folder is renamed onto it in one step: a reader sees no article or all of
            # it, never a half-moved one (review RR-06); a failed rename takes the reservation
            # back and leaves no folder behind
            art = repo.reserve(slug if slug and listed_slug(slug) else slugify(slug) if slug else src)
            try:
                os.rename(staging, art.dir)
            except OSError:
                try:
                    art.dir.rmdir()
                except OSError:
                    pass
                raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return art
