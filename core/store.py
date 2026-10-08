"""Article storage: every file under the output root is read and written through here.

The output root (`paths.output` in config.yaml, or $CONTENT_OUTPUT_DIR) holds one folder per
article plus the install's own files (`_settings.json`, `_secrets.json`, `_prompts/`, ...).

Three rules this module exists to enforce:

  1. **A write never leaves a half-written file.** Everything is written to a temp file in the
     same folder, flushed, then renamed over the target (`atomic_write_*`). A reader sees the
     old file or the new one, never an empty or truncated JSON: before this, every write was a
     plain truncate-then-write, and the dashboard could read `_run.json` mid-write.
  2. **One writer per article at a time.** A background job (generation, Apply QA fixes, Fix,
     uniqueness) holds the article's lock for its whole run; a Studio request that would change
     the article while a job holds it is refused (`Busy`) instead of being overwritten minutes
     later by the job's older copy. The lock is a `flock` on `<article>/.lock`, so it holds
     across gunicorn threads, the job thread and a CLI run on the same volume, and it is
     re-entrant within one thread.
  3. **One frontmatter format.** `article.md` starts with a block of `key: <json>` lines between
     `---` fences (finalize writes it that way); seven hand-rolled parsers with slightly
     different rules are replaced by `parse_frontmatter` / `render_frontmatter`.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import posixpath
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote

from .config import ROOT

OUTPUT_ENV = "CONTENT_OUTPUT_DIR"
SCHEMA = 2          # _run.json schema: 2 = written by this module (1 = everything before it)
ARTICLE_RECORD = "_article.json"


def output_root(config: dict | None = None) -> Path:
    """Where the articles live: $CONTENT_OUTPUT_DIR, else config.yaml `paths.output` (relative
    to the repo)."""
    env = (os.getenv(OUTPUT_ENV) or "").strip()
    if env:
        return Path(env)
    if config is None:
        from .config import load_core_config

        config = load_core_config()
    return ROOT / config["paths"]["output"]


def cache_path(name: str) -> Path:
    """A cache the app rebuilds at runtime (the site's link index): on the
    output volume, `_cache/<name>`, so a release keeps it and a read-only image is never written.
    A copy in the project folder is only the starting point (RF-27)."""
    return output_root() / "_cache" / name


# ---------------------------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------------------------
class CorruptFile(ValueError):
    """A file exists but does not parse. Never read as "empty": an unreadable run record is not
    a run without findings."""

    def __init__(self, path: Path, why: str):
        super().__init__(f"{path.name} is unreadable: {why}")
        self.path = path


def atomic_write_bytes(path: Path, data: bytes, mode: int | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        elif path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, mode: int | None = None) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def write_json(path: Path, data, mode: int | None = None) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n", mode=mode)


def read_json(path: Path, default=None):
    """The parsed file, `default` when it does not exist, CorruptFile when it does not parse."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    try:
        return json.loads(raw)
    except ValueError as e:
        raise CorruptFile(path, str(e)) from None


def read_json_lenient(path: Path, default=None):
    """For display paths that must never fail over one file (the dashboard listing): corrupt
    reads as `default`. Decisions (the gate, publishing) use `read_json`."""
    try:
        return read_json(path, default)
    except CorruptFile:
        return default


def revision_of(path: Path) -> str:
    """The version of a file's bytes ("" when it does not exist): what a derived report was
    computed from, so a reader can tell it is stale."""
    try:
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()[:16]
    except FileNotFoundError:
        return ""


def append_jsonl(path: Path, record: dict) -> None:
    """One JSON line, appended with O_APPEND: a single write of one line, so concurrent appends
    never interleave inside a record."""
    line = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            line = b"\n" + line          # a torn last line (a crash mid-write) stays on its own
        os.write(fd, line)
    finally:
        os.close(fd)


def read_jsonl(path: Path) -> list[dict]:
    """Every parseable line; a torn or hand-mangled line is skipped, never fatal."""
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except FileNotFoundError:
        pass
    return out


# ---------------------------------------------------------------------------------------------
# frontmatter: `---` / one `key: <json>` line per field / `---`
# ---------------------------------------------------------------------------------------------
FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_FM = FRONTMATTER
_FM_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):[ \t]*(.*)$")


def split_frontmatter(text: str) -> tuple[str, str]:
    """(the frontmatter block exactly as written, fences included; the body). The block is ""
    when the text has none. The body keeps its bytes except the blank lines after the block."""
    m = _FM.match(text or "")
    return (m.group(0), (text or "")[m.end():].lstrip("\n")) if m else ("", text or "")


def frontmatter_fields(block: str) -> dict:
    """The fields of a frontmatter block (with or without its fences). Values are JSON; a value
    typed by hand without quotes reads as the plain string."""
    inner = block
    m = _FM.match(block or "")
    if m:
        inner = m.group(1)
    out: dict = {}
    for line in (inner or "").splitlines():
        lm = _FM_LINE.match(line.strip())
        if not lm:
            continue
        key, raw = lm.group(1), lm.group(2).strip()
        try:
            out[key] = json.loads(raw)
        except ValueError:
            out[key] = raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] == '"' else raw
    return out


def parse_frontmatter(text: str) -> tuple[dict, str]:
    block, body = split_frontmatter(text)
    return frontmatter_fields(block), body


def render_frontmatter(fields: dict) -> str:
    return "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fields.items()) + "\n---\n"


def set_frontmatter_field(block: str, key: str, value) -> str:
    """The block with one field replaced (or appended), every other line byte for byte: a hand
    edit elsewhere in the frontmatter survives an automated FAQ or cover update."""
    line = f"{key}: {json.dumps(value, ensure_ascii=False)}"
    if not block:
        return render_frontmatter({key: value})
    pat = re.compile(rf"^{re.escape(key)}:.*$", re.M)
    if pat.search(block):
        return pat.sub(lambda _m: line, block, count=1)
    head, sep, tail = block.rpartition("\n---")
    return head + "\n" + line + sep + tail


def strip_frontmatter(text: str) -> str:
    return _FM.sub("", text or "", count=1)


# ---------------------------------------------------------------------------------------------
# the article's own pictures, as the page shows them
# ---------------------------------------------------------------------------------------------
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _nested_brackets(depth: int) -> str:
    """Text with escaped characters and balanced pairs of brackets nested up to `depth` deep."""
    inner = r"(?:\\.|[^\[\]\\])*"
    for _ in range(depth):
        inner = r"(?:\\.|[^\[\]\\]|\[" + inner + r"\])*"
    return inner


# an image's alt text in Markdown, `![<this>](...)`: escaped characters ("Old \[pricing", RFF2-01)
# and balanced pairs of brackets, as a page title written as a stand-in caption has ("Setup guide
# [2026]", R923-01), up to four deep. Only for a picture inside a sentence: a picture on a line of
# its own is read by the parser (picture_alone)
IMAGE_ALT = _nested_brackets(4)


def picture_alone(line: str, asset: str) -> bool:
    """Whether `line` is the picture `asset` and nothing else, `![alt](asset)`, as the page's
    parser reads it, whatever the alt holds: escapes, brackets at any depth, code (RC8C-01: a
    pattern for the alt disagreed with the parser one case at a time)."""
    if not (line.startswith("![") and line.endswith(f"]({asset})")):
        return False
    from .tools.markdown_render import parse

    inline = [t for t in parse(line) if t.type == "inline"]
    kids = (inline[0].children or []) if len(inline) == 1 else []
    return len(kids) == 1 and kids[0].type == "image" and local_asset(kids[0].attrGet("src")) == asset


def caption_alone(line: str) -> bool:
    """Whether `line` is one emphasised run and nothing else, `*...*`, as the page's parser reads
    it: the caption under a picture, whatever it holds (R39B-01: a page title with a literal `*`,
    "C* API reference", is a caption the Studio writes)."""
    if not (len(line) > 2 and line.startswith("*") and line.endswith("*")):
        return False
    from .tools.markdown_render import parse

    inline = [t for t in parse(line) if t.type == "inline"]
    kids = (inline[0].children or []) if len(inline) == 1 else []
    if len(kids) < 3 or kids[0].type != "em_open" or kids[-1].type != "em_close":
        return False
    depth = 0
    for i, k in enumerate(kids):                # the first run closes at the end, not before
        depth += {"em_open": 1, "em_close": -1}.get(k.type, 0)
        if depth == 0 and i < len(kids) - 1:
            return False
    return True


def caption_of(image: str, line: str) -> bool:
    """Whether `line`, right under the picture's line `image`, is that picture's caption: one
    emphasised run as the page's parser reads it (caption_alone), or the picture's own words
    wrapped as the Studio writes a caption, `*<its alt>*`, which an earlier writer left under a
    title as it read ("*Old C* API reference*" under "![Old C* API reference](...)", R83D-01). The
    words are the proof it belongs to the picture: other text that starts and ends with an
    asterisk is not taken for it."""
    line = line.rstrip(" \t")
    if caption_alone(line):
        return True
    image = image.rstrip(" \t")
    alt = image[2:image.rfind("](")] if image.startswith("![") and "](" in image else ""
    return bool(alt.strip()) and line == f"*{alt}*"


def reads_as_picture(md: str, path: str) -> bool:
    """Whether `md`, one or two lines, reads as the picture `path` on a line of its own and, when
    there is a second line, its caption, as one paragraph of the page (no code span or emphasis
    reaching from one into the other): the check of what a writer is about to write (R83D-02)."""
    from .tools.markdown_render import parse

    lines = md.split("\n")
    if not (1 <= len(lines) <= 2 and picture_alone(lines[0], path)):
        return False
    if len(lines) == 1:
        return True
    inline = [t for t in parse(md) if t.type == "inline"]
    kids = (inline[0].children or []) if len(inline) == 1 else []
    return (caption_of(lines[0], lines[1]) and len(kids) > 2 and kids[0].type == "image"
            and kids[1].type == "softbreak")


# what the generation's writer takes out of a page title before it writes it as a picture's words
# (agents/visual_director), and a backslash
_UNSAFE_WORDS = re.compile(r"[\[\]*<>`\\]+")


def safe_words(text: str) -> str:
    """Words for a picture's alt text or caption, as the page's parser will read them: `text` when
    `![text](...)` with `*text*` (or `*[text](...)*`) under it reads as the picture and its caption,
    else `text` without brackets, asterisks, angle brackets, backticks and backslashes, as the
    generation's writer cleans a page title. A page title written as it reads (a stand-in caption)
    carried an unpaired bracket, a backtick or a trailing backslash into the Markdown, and the page
    no longer showed the picture (the sweep of 2026-10-07)."""
    text = " ".join(str(text or "").split())
    path = "assets/x.png"
    if not text or all(_reads_as_picture(f"![{text}]({path})\n*{c}*", path)
                       for c in (text, f"[{text}](https://example.com/)")):
        return text
    return " ".join(_UNSAFE_WORDS.sub(" ", text).split())


def _reads_as_picture(md: str, path: str) -> bool:
    """reads_as_picture of two lines whose caption is one emphasised run (what safe_words asks)."""
    lines = md.split("\n")
    return len(lines) == 2 and caption_alone(lines[1]) and reads_as_picture(md, path)


def picture_line(body: str, asset: str) -> int | None:
    """The index of the one line of `body` that is the picture `asset` alone (picture_alone), or
    None unless exactly one line shows it and that line is the picture alone."""
    lines = body.split("\n")
    shown = [i for i, ln in enumerate(lines) if f"]({asset})" in ln]
    return shown[0] if len(shown) == 1 and picture_alone(lines[shown[0]], asset) else None


def local_asset(src) -> str | None:
    """An image address as the article's own file, `assets/<path>`, or None for anything else
    (a URL, an absolute path, a path out of assets/). The query and the fragment are not part of
    the file, and the address is URL-decoded: `assets/chart%20one.png?v=3#detail` is the file
    `assets/chart one.png`, the way the Studio's asset route reads it."""
    s = str(src or "").strip()
    if not s or _SCHEME.match(s) or s.startswith("/"):
        return None
    s = unquote(s.split("#", 1)[0].split("?", 1)[0])
    s = posixpath.normpath(s) if s else s
    return s if s.startswith("assets/") and ".." not in s.split("/") else None


def shown_image_refs(body: str) -> list[tuple[str, str]]:
    """(alt text, file) of every image of the article's own the page shows, in order, a picture
    shown twice listed twice: read with the parser the Studio and WordPress render with
    (core/tools/markdown_render), so a title, a reference-style image, a query, a fragment or an
    encoded name is found as the page finds it, and an image written inside a code block is not one."""
    from .tools.markdown_render import parse

    out: list[tuple[str, str]] = []

    def walk(tokens):
        for t in tokens:
            if t.type == "image":
                p = local_asset(t.attrGet("src"))
                if p:
                    out.append((t.content or "", p))
            if t.children:
                walk(t.children)

    walk(parse(body or ""))
    return out


def shown_image_sources(body: str) -> list[str]:
    """The address of every image the page shows, the article's own files and remote ones alike,
    as the page's parser reads them (the person's paragraph adds or removes none of them)."""
    from .tools.markdown_render import parse

    out: list[str] = []

    def walk(tokens):
        for t in tokens:
            if t.type == "image":
                out.append(str(t.attrGet("src") or ""))
            if t.children:
                walk(t.children)

    walk(parse(body or ""))
    return out


def shown_images(body: str) -> list[str]:
    """The files of `shown_image_refs`, in order, duplicates kept."""
    return [p for _, p in shown_image_refs(body)]


def cover_asset(fields: dict, folder: Path | None) -> str | None:
    """The cover as the publisher picks it: the frontmatter's `cover` (a path in the article
    folder, `./assets/x.png` as good as `assets/x.png`), else assets/cover.png when it exists
    in `folder` (not looked for without one). A cover outside assets/ is not sent, so it is none."""
    c = (fields or {}).get("cover")
    if not (isinstance(c, str) and c):
        return "assets/cover.png" if folder is not None and (Path(folder) / "assets" / "cover.png").is_file() else None
    n = posixpath.normpath(c)
    return n if n.startswith("assets/") and ".." not in n.split("/") else None


def compose_article(block: str, body: str) -> str:
    """article.md bytes: the frontmatter block, a blank line, the body with one trailing newline
    (appending a newline to a body that already ended with one grew the file on every write)."""
    body = (body or "").strip("\n") + "\n"
    return (block.rstrip("\n") + "\n\n" + body) if block else body


# ---------------------------------------------------------------------------------------------
# locks
# ---------------------------------------------------------------------------------------------
class Busy(RuntimeError):
    """The article is locked by a job (or another process)."""

    def __init__(self, slug: str, holder: str = ""):
        super().__init__(f"'{slug}' is being worked on by {holder or 'another job'}; try again when it finishes")
        self.slug, self.holder = slug, holder


class Conflict(RuntimeError):
    """The article changed since the version the caller started from."""


class UnsafePath(ValueError):
    """A record names a file that is not the article's own: an absolute path, a way out with
    `..`, a link, or a place the action never writes. A record that came in an import is data,
    not a list of places to write (review RC77-01)."""


_held = threading.local()      # {lock path: [fd, depth]} held by this thread


@contextmanager
def _flock(path: Path, slug: str, wait: bool, holder: str, timeout: float | None):
    held = getattr(_held, "locks", None)
    if held is None:
        held = _held.locks = {}
    key = str(path)
    if key in held:                       # re-entrant within one thread
        held[key][1] += 1
        try:
            yield
        finally:
            held[key][1] -= 1
        return
    if not path.parent.is_dir():
        # a job queued before a Delete must not bring the folder back as an empty article
        raise FileNotFoundError(f"'{slug}' no longer exists")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait and deadline is None else fcntl.LOCK_NB))
                break
            except BlockingIOError:
                if not wait or (deadline is not None and time.monotonic() >= deadline):
                    try:
                        who = os.pread(fd, 400, 0).decode("utf-8", "replace").strip()
                    except OSError:
                        who = ""
                    raise Busy(slug, who) from None
                time.sleep(0.2)
        try:
            moved = os.fstat(fd).st_ino != os.stat(path).st_ino
        except FileNotFoundError:
            moved = True
        if moved:
            # the folder was moved (Delete puts it in _trash) while we waited for its lock
            fcntl.flock(fd, fcntl.LOCK_UN)
            raise FileNotFoundError(f"'{slug}' was moved or deleted")
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{holder or 'a job'} (pid {os.getpid()}, since {time.strftime('%H:%M:%S')})".encode(), 0)
        held[key] = [fd, 1]
        try:
            yield
        finally:
            del held[key]
            try:
                os.ftruncate(fd, 0)
            except OSError:
                pass
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------------------------
# article folders
# ---------------------------------------------------------------------------------------------
def slugify(text: str) -> str:
    """A folder/WordPress slug. A topic with no ASCII letters (Cyrillic, for one) used to become
    'article' for every such topic; it now keeps a short hash of the topic."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    s = s[:80].rstrip("-")
    return s or "article-" + hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:6]


_SLUG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,160}")


def valid_slug(name: str) -> bool:
    """A folder name under the output root: letters, digits, `-` and `_` only, not hidden. The
    name goes into URLs and HTML attributes, so nothing else is let in: an imported folder named
    `article" onerror="…` put an event handler on the article's images (review RV-01)."""
    return bool(name) and bool(_SLUG.fullmatch(name))


def listed_slug(name: str) -> bool:
    """An article the Studio lists and acts on: a valid name that is not one of the install's own
    folders or a hidden test article (`_prompts`, `_trash`, `_qatest-…`)."""
    return valid_slug(name) and not name.startswith("_")


class Article:
    """One article folder."""

    def __init__(self, root: Path, slug: str):
        self.root = Path(root)
        self.slug = slug
        self.dir = self.root / slug

    def __repr__(self) -> str:
        return f"Article({self.slug!r})"

    # -- paths --
    def path(self, name: str) -> Path:
        return self.dir / name

    @property
    def assets(self) -> Path:
        return self.dir / "assets"

    def own_file(self, rel: str, under: tuple[str, ...] = ("article.md", "assets/")) -> Path:
        """The file `rel` of this article, when a record may name it: a plain relative path, no
        `..`, no link on the way, the exact name or inside the folder (ending in "/") of one of
        `under`. UnsafePath otherwise, before anything is read or written."""
        r = str(rel or "")
        parts = r.split("/")
        if (not r or r.startswith("/") or "\\" in r or "\0" in r or any(x in ("", ".", "..") for x in parts)
                or not any(r == u or (u.endswith("/") and r.startswith(u) and len(r) > len(u)) for u in under)):
            raise UnsafePath(f"'{r}' is not a file of '{self.slug}' this action may touch")
        p = self.dir
        for part in parts:
            p = p / part
            if p.is_symlink():
                raise UnsafePath(f"'{r}' goes through a link")
        return p

    @property
    def source(self) -> Path | None:
        """article.md, else the writer's draft; None when neither exists yet."""
        for name in ("article.md", "04_draft.md"):
            p = self.dir / name
            if p.is_file():
                return p
        return None

    # -- text --
    def text(self) -> str:
        src = self.source
        return src.read_text(encoding="utf-8") if src else ""

    def read(self) -> tuple[dict, str, str]:
        """(frontmatter fields, body, the frontmatter block as written)."""
        block, body = split_frontmatter(self.text())
        return frontmatter_fields(block), body, block

    def revision(self) -> str:
        """The version of the text on disk: what an editor started from, to detect that it
        changed underneath them, and what every derived report records it was computed from."""
        src = self.source
        return revision_of(src) if src else ""

    def cover(self, text: str | None = None) -> str | None:
        """The cover as the publisher picks it (`cover_asset`)."""
        return cover_asset(frontmatter_fields(split_frontmatter(self.text() if text is None else text)[0]), self.dir)

    def pictures(self, text: str | None = None) -> dict[str, str | None]:
        """Every picture the article ships, with a hash of its bytes (None when the file is
        missing): each image the page shows (`shown_images`) and the cover (`cover_asset`)."""
        block, body = split_frontmatter(self.text() if text is None else text)
        refs = set(shown_images(body))
        cover = cover_asset(frontmatter_fields(block), self.dir)
        if cover:
            refs.add(cover)
        out = {}
        for ref in sorted(refs):
            p = self.dir / ref
            try:
                inside = (self.dir / "assets").resolve() in p.resolve().parents
            except OSError:
                inside = False
            out[ref] = (revision_of(p) or None) if inside and p.is_file() else None
        return out

    def version(self) -> str:
        """One token over the article's text, its frontmatter and the hashes of its pictures:
        what a person accepts, what a send carries, what an action is checked against. "" when
        there is no text yet."""
        src = self.source
        if src is None:
            return ""
        raw = src.read_bytes()          # one read: the text hashed is the text the pictures come from
        pics = self.pictures(raw.decode("utf-8"))
        parts = [f"{src.name}:{hashlib.sha1(raw).hexdigest()[:16]}"] + [f"{ref}:{h or '-'}" for ref, h in pics.items()]
        return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]

    def write_article(self, block: str, body: str, base_revision: str | None = None, who: str | None = None,
                      what: str = "Saved in the editor", kind: str = "edit", exact: bool = False) -> str:
        """Write article.md (keeping 04_draft.md, the writer's draft, as it was). With
        `base_revision`, refuse when the text changed since that version. With `who` (a person's
        save), the write is a version of its own (core/versions.py); a machine change is recorded
        by its change group. `exact` (the editor): the body as given and the blank lines after
        the frontmatter as they are, so a save changes only the lines the person changed. Returns
        the new revision."""
        with self.lock(wait=False, holder="a Studio edit"):
            if base_revision is not None and base_revision != self.revision():
                raise Conflict(f"'{self.slug}' changed since you opened it")
            text = compose_article(block, body)
            src = self.dir / "article.md"
            if exact and not block:
                text = body                     # no frontmatter: the text is the body, as given (RF9C-05)
            elif exact and src.is_file():
                cur = src.read_text(encoding="utf-8")
                cur_block, cur_body = split_frontmatter(cur)
                sep = cur[len(cur_block):len(cur) - len(cur_body)] if cur_block else "\n"
                text = block + (sep or "\n") + body
            if who:
                from .versions import change

                with change(self, who, what, kind=kind):
                    atomic_write_text(src, text)
            else:
                atomic_write_text(src, text)
            return self.revision()

    # -- records --
    def run(self) -> dict:
        """The run record, migrated to the current schema in memory. {} when there is none;
        CorruptFile when it does not parse."""
        run = read_json(self.dir / "_run.json", default={})
        if not isinstance(run, dict):
            raise CorruptFile(self.dir / "_run.json", "not a JSON object")
        return migrate_run(run) if run else run

    def save_run(self, run: dict) -> None:
        # a record already in a newer schema (core/qa_migration.py) keeps it
        write_json(self.dir / "_run.json", {**run, "schema": max(SCHEMA, schema_of(run))})

    def record(self) -> dict:
        """The article's own record (`_article.json`): `accepted`, a person's mark on one
        version, and `published`, what the Studio knows of the WordPress post (core/qa_model).
        Facts about the article, not about one run, so a regeneration that rewrites `_run.json`
        keeps them. {} when there is none; CorruptFile when it does not parse."""
        rec = read_json(self.dir / ARTICLE_RECORD, default={})
        if not isinstance(rec, dict):
            raise CorruptFile(self.dir / ARTICLE_RECORD, "not a JSON object")
        return rec

    def save_record(self, rec: dict) -> None:
        write_json(self.dir / ARTICLE_RECORD, {**rec, "schema": 1})

    def json(self, name: str, default=None):
        return read_json(self.dir / name, default)

    def write_json(self, name: str, data) -> None:
        write_json(self.dir / name, data)

    def write_text(self, name: str, text: str) -> None:
        atomic_write_text(self.dir / name, text)

    # -- lock --
    def lock(self, wait: bool = True, holder: str = "", timeout: float | None = None):
        """Exclusive, re-entrant within one thread. `wait=False` raises Busy at once."""
        return _flock(self.dir / ".lock", self.slug, wait=wait, holder=holder, timeout=timeout)

    def busy(self) -> str | None:
        """Who holds the lock right now (None when free). A peek: never blocks."""
        p = self.dir / ".lock"
        if not p.exists():
            return None
        held = getattr(_held, "locks", None) or {}
        if str(p) in held:
            return "this job"
        fd = os.open(p, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return None
        except BlockingIOError:
            return os.pread(fd, 400, 0).decode("utf-8", "replace").strip() or "a job"
        finally:
            os.close(fd)

    # -- what the folder holds --
    def status(self) -> str:
        """article | draft | running | aborted | failed | empty. `running` means the run record
        says so; whether a job is actually alive is the job runner's business."""
        try:
            run = read_json(self.dir / "_run.json", default={}) or {}
        except CorruptFile:
            run = {"status": "corrupt"}
        st = run.get("status") if isinstance(run, dict) else None
        if (self.dir / "article.md").is_file():
            return "article"
        if st in ("aborted", "failed", "running"):
            return st
        if (self.dir / "04_draft.md").is_file():
            return "draft"
        return "empty"


def migrate_run(run: dict) -> dict:
    """A run record written by an older version, read as the current one. Unknown fields are
    kept as they are; nothing is dropped.

    1 -> 2: no field changed meaning. The QA round loop's own fields (`qa_state.capped`,
    `max_rounds`, `mode`) are no longer read; a loop-era ledger keeps its `provisional` entries
    (core/findings.Ledger.blocking_serious).

    2 -> 3 is core/qa_migration.py (the old ledger to `legacy_findings`, the new findings under
    `review`). It needs the article folder, not just this record, and is not applied here; a
    record already at 3 is returned as it is, never relabelled."""
    if schema_of(run) >= SCHEMA:
        return run
    return {**run, "schema": SCHEMA}


def schema_of(run: dict) -> int:
    try:
        return int(run.get("schema") or 1)
    except (TypeError, ValueError):
        return 1


class Repository:
    """The output root: the article folders in it."""

    def __init__(self, root: Path | None = None, config: dict | None = None):
        self.root = Path(root) if root is not None else output_root(config)

    def get(self, slug: str, listed_only: bool = False) -> Article:
        """The article folder `slug`. `listed_only` (the Studio's routes) also refuses the
        install's own folders and hidden test articles; the CLI may act on those."""
        if not (listed_slug(slug) if listed_only else valid_slug(slug)):
            raise KeyError(slug)
        a = Article(self.root, slug)
        if not a.dir.is_dir():
            raise KeyError(slug)
        return a

    def articles(self) -> list[Article]:
        if not self.root.is_dir():
            return []
        return [Article(self.root, p.name) for p in sorted(self.root.iterdir())
                if p.is_dir() and listed_slug(p.name)]

    def reserve(self, base: str) -> Article:
        """A NEW folder for a run, never an existing one: a second run of the same topic used to
        write into the first run's folder, keeping its WordPress binding and stale reports.
        `base`, else `base-2`, `base-3`, … (mkdir is atomic, so two runs cannot both win)."""
        base = base if listed_slug(base) else slugify(base)
        self.root.mkdir(parents=True, exist_ok=True)
        for n in range(1, 1000):
            name = base if n == 1 else f"{base[:76].rstrip('-')}-{n}"
            try:
                (self.root / name).mkdir()
                return Article(self.root, name)
            except FileExistsError:
                continue
        raise RuntimeError(f"no free folder name for '{base}'")
