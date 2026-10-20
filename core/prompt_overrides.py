"""Team-editable agent prompts, stored on the persistent volume.

The shipped prompts live in `core/prompts/*.md`, inside the image, so an edit written there would
be lost on the next release. The Studio saves an edited prompt to `project/output/_prompts/`
instead (the same volume as `_settings.json`), `BaseAgent.load_prompt` prefers it, and
"Restore original" deletes it, so the shipped version is always one click away.
"""
from __future__ import annotations

import re
from pathlib import Path
from .store import atomic_write_text

PROMPTS_DIR = "_prompts"
_ROOT = Path(__file__).resolve().parents[1]

# the prompt file each agent loads (its `name`), in pipeline order, with what it does
AGENTS = [
    ("keyword_researcher", "Keyword research", "Reads the Ahrefs data and the ranking pages; picks the "
                                               "primary keyword, clusters, length and format."),
    ("brief_generator", "Brief", "Turns the keyword research into the SEO brief: the outline, the keywords per section and "
                                 "research questions."),
    ("copywriter", "Copywriter", "Writes the article from the brief, the research and the product facts."),
    ("visual_director", "Visual director", "Plans the charts, infographics and screenshots and where they go."),
    ("visual_producer", "Visual producer", "Turns one visual plan into a chart or infographic spec."),
    ("editor", "Editor (QA review)", "The review each model of the panel runs: what counts as a defect and how "
                                     "serious it is."),
    ("finding_matcher", "Finding matcher", "Decides which review findings describe the same problem."),
    ("visual_checker", "Picture check", "Looks at each picture with its caption and the text around it: "
                                        "broken, unreadable, covered, or not what the caption says."),
    ("edit_verifier", "Edit check", "Checks the fixer's edits after the review: is each problem gone, did an "
                                    "edit break anything."),
    ("ai_editor", "Studio AI editor", "The 'edit with AI' box on an article page."),
]
_NAMES = {a[0] for a in AGENTS}
_LABELS = {a[0]: a[1] for a in AGENTS}
# exactly the form BaseAgent._inject fills: "{{ product_facts }}" with spaces is never filled, so
# it must not count as kept
_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")
# The output format the code parses. It sits between these markers in the prompts whose failure
# would read as something else (a matcher that silently falls back, an edit check that marks
# every edit unconfirmed, review reads that look like an outage), and it always runs as shipped.
CONTRACT_START = "<!-- output-contract"
CONTRACT_END = "<!-- /output-contract -->"
_CONTRACT = re.compile(re.escape(CONTRACT_START) + r".*?" + re.escape(CONTRACT_END), re.DOTALL)


def _check(name: str) -> str:
    if name not in _NAMES:
        raise ValueError(f"unknown prompt '{name}'")
    return name


def shipped_path(name: str, prompts_dir: str = "core/prompts") -> Path:
    return _ROOT / prompts_dir / f"{_check(name)}.md"


def override_path(output_dir, name: str) -> Path:
    return Path(output_dir) / PROMPTS_DIR / f"{_check(name)}.md"


def shipped(name: str) -> str:
    p = shipped_path(name)
    return p.read_text(encoding="utf-8") if p.is_file() else ""


def is_overridden(output_dir, name: str) -> bool:
    return override_path(output_dir, name).is_file()


def current(output_dir, name: str) -> str:
    """What the agent will use: the team's edit if there is one (its output format as shipped, the
    slots a later release added put in, `as_run`), else the shipped prompt."""
    p = override_path(output_dir, name)
    return as_run(p.read_text(encoding="utf-8"), shipped(name)) if p.is_file() else shipped(name)


def as_run(text: str, shipped_text: str) -> str:
    """An edited prompt as the agent runs it and its page shows it: the edit's own words, every
    {{slot}} of the shipped prompt it lacks (one a later release added: an edit saved before it
    never had it), and the shipped output format."""
    return with_shipped_contract(with_new_slots(text, shipped_text), shipped_text)


# how a slot a release added is written into an edit saved before it; another slot is its shipped
# line (with the heading right above it)
_SLOT_TEXT = {"editorial_rules": "## Editorial rules ({{project_name}}'s own; follow every one)\n{{editorial_rules}}",
              "audience": "The readers: {{audience}}."}


def _slot_text(shipped_text: str, name: str) -> str:
    if name in _SLOT_TEXT:
        return _SLOT_TEXT[name]
    lines = shipped_text.splitlines()
    at = next(i for i, ln in enumerate(lines) if "{{" + name + "}}" in ln)
    above = lines[at - 1] if at and lines[at - 1].lstrip().startswith("#") else ""
    return (above + "\n" if above else "") + lines[at]


def with_new_slots(text: str, shipped_text: str) -> str:
    """`text` with each {{slot}} the shipped prompt fills and the edit lacks put in before its
    output format (or at its end): an edit saved before a release added the project's editorial
    rules keeps its own words and gets the rules, and its page can be saved again. A slot the edit
    has is never added twice."""
    missing = sorted(placeholders(shipped_text) - placeholders(text))
    if not missing:
        return text
    block = "\n\n".join(_slot_text(shipped_text, m) for m in missing)
    c = contract(text)
    if c:
        i = text.index(c)
        return text[:i].rstrip("\n") + "\n\n" + block + "\n\n" + text[i:]
    return text.rstrip("\n") + "\n\n" + block + "\n"


def placeholders(text: str) -> set[str]:
    """The {{slots}} the pipeline fills in (product facts, brand voice, today's date…)."""
    return set(_PLACEHOLDER.findall(text or ""))


def contract(text: str) -> str | None:
    """The output-format block of a prompt, markers included, or None."""
    m = _CONTRACT.search(text or "")
    return m.group(0) if m else None


def with_shipped_contract(text: str, shipped_text: str) -> str:
    """An edited prompt as the agent runs it: its output-format block is the shipped one, so an
    edit (or a release that changes the format) can never leave the code parsing a format the
    prompt no longer asks for."""
    block = contract(shipped_text)
    if not block:
        return text
    if contract(text):
        seen = []

        def one(_m):        # the first block becomes the shipped one; any further copy goes
            seen.append(1)
            return block if len(seen) == 1 else ""

        return _CONTRACT.sub(one, text)
    return text.rstrip("\n") + "\n\n" + block + "\n"


_MARKER_LINE = re.compile(r"^[ \t]*(?:" + re.escape(CONTRACT_START) + r"[^\n]*-->|" + re.escape(CONTRACT_END)
                          + r")[ \t]*\n?", re.MULTILINE)


def strip_markers(text: str) -> str:
    """The prompt as a model reads it: the marker comments are for saving and restoring only."""
    return _MARKER_LINE.sub("", text or "")


def _norm(s: str | None) -> str:
    return " ".join((s or "").split())


def save(output_dir, name: str, text: str) -> list[str]:
    """Store an edited prompt. Saving the shipped text unchanged removes the override instead of
    pinning a copy. Raises ValueError, storing nothing, for an edit that drops a {{slot}} the
    shipped prompt fills (the agent would run without the product facts, the brand voice or
    today's date) or that changes the output format the code reads."""
    text = (text or "").replace("\r\n", "\n")
    if not text.strip():
        raise ValueError("an empty prompt cannot be saved; use Restore original instead")
    ship = shipped(name)
    if text.strip() == ship.strip():
        reset(output_dir, name)
        return []
    missing = sorted(placeholders(ship) - placeholders(text))
    if missing:
        raise ValueError("Not saved: the edit drops " + ", ".join("{{" + m + "}}" for m in missing)
                         + ". The app fills these with the article's data; keep each one exactly as written.")
    if contract(ship) and (_norm(contract(text)) != _norm(contract(ship)) or len(_CONTRACT.findall(text)) > 1):
        raise ValueError("Not saved: the output format between the output-contract markers is what the code "
                         "reads, so it stays as shipped, once. Edit the rest of the prompt.")
    p = override_path(output_dir, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, text)
    return []


def reset(output_dir, name: str) -> None:
    override_path(output_dir, name).unlink(missing_ok=True)


def active(output_dir) -> list[str]:
    """The prompts running as edited in the Studio, in pipeline order."""
    return [n for n, _l, _h in AGENTS if is_overridden(output_dir, n)]


def edited_hint(output_dir, name: str) -> str:
    """For a failure note: say so when the failing agent runs an edited prompt."""
    if name in _NAMES and is_overridden(output_dir, name):
        return (f" The '{_LABELS[name]}' prompt is edited in Configure > Prompts: if this keeps happening, "
                f"restore the original there.")
    return ""
