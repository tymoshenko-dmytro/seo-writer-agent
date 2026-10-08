"""The project's own texts, editable from the Studio and the setup: the values behind
{{project_name}}, {{audience}}, {{brand_voice}}, {{product_facts}} and {{editorial_rules}} in the
agents' prompts. Everything about one product lives here, never in the prompts themselves.

The shipped values live in the project folder (`project/profile.yaml` for the name and the
audience, `project/brand.md`, `project/products.md`, `project/editorial_rules.md`; all empty in a
new install), inside the image, so an edit written there would be lost on the next release. The
Studio saves an edit to `project/output/_project/<key>.md` (the article volume, like the prompt
edits), `load_project` prefers it, and "Restore original" deletes it. {{today}} and {{current_year}} are the date, filled by the code, and not editable.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from .store import atomic_write_text

TEXTS_DIR = "_project"
_ROOT = Path(__file__).resolve().parents[1]

# key (= the slot it fills), label, what it is, where the shipped value lives, one line or not
FIELDS = [
    {"key": "project_name", "label": "Project name", "line": True, "source": "project/profile.yaml → name",
     "help": "The brand the articles are written for and about. Fills {{project_name}} in the prompts."},
    {"key": "audience", "label": "Audience", "line": False, "source": "project/profile.yaml → audience",
     "help": "Who the articles are for. Fills {{audience}}."},
    {"key": "brand_voice", "label": "Brand voice", "line": False, "source": "project/brand.md",
     "help": "How the brand writes: tone, words to use and avoid. Fills {{brand_voice}} in the writer's and the "
             "editor's prompts."},
    {"key": "product_facts", "label": "Product facts", "line": False, "source": "project/products.md",
     "help": "Everything the agents may say about the products: plans, prices, features, limits, links. Fills "
             "{{product_facts}}; its numbers count as attested, so write only current facts."},
    {"key": "editorial_rules", "label": "Editorial rules", "line": False, "source": "project/editorial_rules.md",
     "help": "Your own rules for what the articles may say and how: claims to avoid, how to name customers, "
             "comparisons you allow or forbid, how much technical detail readers get. The writer follows them "
             "and the review checks them. Fills {{editorial_rules}}; may be left empty."},
]
_KEYS = {f["key"]: f for f in FIELDS}
# the slots the code fills itself: shown on the prompt pages, not editable
CODE_SLOTS = {"today": "today's date, set by the code", "current_year": "this year, set by the code",
              "own_domain": "our main domain, from the project profile (brand.domains)",
              "min_product_links": "product-page links an article needs, from the project profile (links)",
              "words_per_link": "words per internal link, from the project profile (links)"}


def _check(key: str) -> str:
    if key not in _KEYS:
        raise ValueError(f"unknown project text '{key}'")
    return key


def _project_dir() -> Path:
    from .config import project_root

    return project_root()


def shipped(key: str, project_dir: Path | None = None) -> str:
    """The value that ships with the app, read from the project folder."""
    _check(key)
    d = Path(project_dir) if project_dir else _project_dir()
    profile = yaml.safe_load((d / "profile.yaml").read_text(encoding="utf-8")) or {}
    if key == "project_name":
        return str(profile.get("name") or "")
    if key == "audience":
        return str(profile.get("audience") or "")
    name = {"brand_voice": profile.get("brand_file", "brand.md"),
            "product_facts": profile.get("products_file", "products.md"),
            "editorial_rules": profile.get("editorial_rules_file", "editorial_rules.md")}[key]
    p = d / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def override_path(output_dir, key: str) -> Path:
    return Path(output_dir) / TEXTS_DIR / f"{_check(key)}.md"


def is_overridden(output_dir, key: str) -> bool:
    return override_path(output_dir, key).is_file()


def edited(output_dir, key: str) -> str | None:
    """The team's edit, or None."""
    p = override_path(output_dir, key)
    if not p.is_file():
        return None
    text = p.read_text(encoding="utf-8")
    return text.strip() if _KEYS[key]["line"] else text


def current(output_dir, key: str, project_dir: Path | None = None) -> str:
    e = edited(output_dir, key)
    return e if e is not None else shipped(key, project_dir)


def save(output_dir, key: str, text: str, project_dir: Path | None = None) -> None:
    """Store an edit. Saving the shipped value unchanged removes the edit instead of pinning a
    copy. Raises ValueError, storing nothing, for an empty value, a line break in the name, or a
    {{slot}} inside a value (the slots are filled once; a slot in a value would reach the model
    as literal braces)."""
    text = (text or "").replace("\r\n", "\n")
    f = _KEYS[_check(key)]
    if not text.strip():
        raise ValueError(f"{f['label']}: an empty value cannot be saved; use Restore original instead")
    if f["line"]:
        if "\n" in text.strip():
            raise ValueError(f"{f['label']}: one line only")
        text = text.strip()
    if "{{" in text:
        raise ValueError(f"{f['label']}: the value cannot contain {{{{…}}}} slots")
    if text.strip() == shipped(key, project_dir).strip():
        reset(output_dir, key)
        return
    p = override_path(output_dir, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, text)


def reset(output_dir, key: str) -> None:
    override_path(output_dir, key).unlink(missing_ok=True)


def clear(output_dir, key: str, project_dir: Path | None = None) -> None:
    """Empty a longer text (the brand voice, the product facts, the editorial rules), as the
    setup does when its field is emptied: the prompts then say none was given. Raises ValueError
    for the name and the audience, which cannot be empty."""
    f = _KEYS[_check(key)]
    if f["line"]:
        raise ValueError(f"{f['label']}: it cannot be empty")
    if not shipped(key, project_dir).strip():
        reset(output_dir, key)          # the shipped text is empty already
        return
    p = override_path(output_dir, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, "")


def active(output_dir) -> list[str]:
    """The project texts running as edited in the Studio."""
    return [f["key"] for f in FIELDS if is_overridden(output_dir, f["key"])]
