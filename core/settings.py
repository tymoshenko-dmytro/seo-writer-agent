"""Team-editable settings, stored on the persistent volume.

`config.yaml` and `project/profile.yaml` ship INSIDE the container image, so anything written
there is discarded by the next release. The PVC is mounted at `project/output`, which makes it
the only place a setting changed from the Studio can survive a deploy — so overrides live in
`project/output/_settings.json` and are layered over the YAML defaults at load time.

Precedence, lowest to highest:
    core/config.yaml  ->  project/profile.yaml  ->  project/output/_settings.json

`load_project()` applies them, and the orchestrator calls `load_project()` on every run, so a
saved change takes effect on the next generation without restarting the app.

FIELDS is the single source of truth for the form, the validation and the defaults — adding a
knob here makes it appear in the Studio with no template change.
"""
from __future__ import annotations

import json
from pathlib import Path
from .store import atomic_write_text

SETTINGS_FILE = "_settings.json"

# Ahrefs takes any ISO 3166-1 alpha-2 code; these are the markets worth one click. "Other"
# is deliberately absent — a free-text country field is how you end up with a whole month of
# research quietly pulled for the wrong SERP.
COUNTRIES = [
    ("us", "United States"), ("gb", "United Kingdom"), ("ca", "Canada"),
    ("au", "Australia"), ("de", "Germany"), ("fr", "France"), ("es", "Spain"),
    ("it", "Italy"), ("nl", "Netherlands"), ("br", "Brazil"), ("mx", "Mexico"),
    ("in", "India"), ("jp", "Japan"), ("kr", "South Korea"), ("ae", "United Arab Emirates"),
]

def _profile_categories() -> list[str]:
    """The WordPress categories the profile maps (content.categories), read straight from the
    YAML (the full project loader applies these settings, so it cannot be used here)."""
    try:
        import yaml

        from .config import project_root

        prof = yaml.safe_load((project_root() / "profile.yaml").read_text(encoding="utf-8")) or {}
        return [str(c) for c in ((prof.get("content") or {}).get("categories") or []) if c]
    except Exception:  # noqa: BLE001 — a settings page must render without the profile
        return []


# group, key (dotted = nested), type, default, label, help, bounds/choices
FIELDS = [
    {"group": "Search market", "key": "country", "type": "choice", "default": "us",
     "choices": COUNTRIES, "label": "Country",
     "help": "Every Ahrefs figure is country-specific: search volume, which pages rank, and "
             "the competitors we tear down. Set this to the market the article is FOR. "
             "Researching a UK-targeted piece against the US SERP gives you the wrong "
             "competitors and the wrong target length."},

    {"group": "Competitors", "key": "prototypes_min", "type": "int", "default": 3, "min": 1, "max": 7,
     "label": "Prototype competitors — at least", "help":
         "How many ranking articles get the full teardown, at least. The highest-ranking one is "
         "always the benchmark; the rest are the articles with the fewest links pointing at the "
         "page, i.e. the realistic targets. Three is the SEO team's standard. When the primary "
         "SERP has fewer qualified articles, pages are borrowed from neighbouring keywords."},
    {"group": "Competitors", "key": "prototypes_max", "type": "int", "default": 7, "min": 1, "max": 12,
     "label": "Prototype competitors — at most", "help":
         "More qualified articles than this are not torn down: each one costs page reads and time."},
    {"group": "Competitors", "key": "serp_top", "type": "int", "default": 20, "min": 10, "max": 30,
     "label": "SERP positions to read", "help":
         "AI overviews, People-Also-Ask and discussion blocks occupy positions too, so asking "
         "for 10 can return as few as 7 real organic results. 20 leaves room to find three "
         "article competitors."},
    {"group": "Competitors", "key": "audit_max_pages", "type": "int", "default": 14, "min": 3, "max": 24,
     "label": "Competitor pages to open", "help":
         "Total pages fetched. The prototypes get the full teardown (and a browser render when "
         "the page needs JavaScript); the rest give up only their outline. Higher is slower."},

    {"group": "Keywords & LSI", "key": "lsi.also_talk_about", "type": "int", "default": 60,
     "min": 0, "max": 100, "label": "LSI terms to request", "help":
         "Terms the ranking pages frequently MENTION — the vocabulary an article needs in "
         "order to read as on-topic, whether or not anyone searches for it. 0 turns the LSI "
         "pass off."},
    {"group": "Keywords & LSI", "key": "lsi.also_rank_for", "type": "int", "default": 40,
     "min": 0, "max": 100, "label": "Sibling keywords to request", "help":
         "Keywords the ranking pages ALSO rank for. These widen the keyword pool that becomes "
         "the article's clusters and sections."},
    {"group": "Keywords & LSI", "key": "lsi.keep", "type": "int", "default": 40, "min": 5, "max": 80,
     "label": "LSI terms to keep", "help":
         "How many survive filtering and reach the writer. Terms already contained in the "
         "primary keyword, editorial filler and bare generic adjectives are dropped first."},

    {"group": "Meta tags", "key": "meta.title_min", "type": "int", "default": 50, "min": 20, "max": 90,
     "label": "Title tag — minimum characters", "help": ""},
    {"group": "Meta tags", "key": "meta.title_max", "type": "int", "default": 70, "min": 30, "max": 120,
     "label": "Title tag — maximum characters", "help":
         "Checked in code and sent back for repair until it fits, so a narrow range costs "
         "extra model rounds."},
    {"group": "Meta tags", "key": "meta.description_min", "type": "int", "default": 130,
     "min": 50, "max": 200, "label": "Meta description — minimum characters", "help": ""},
    {"group": "Meta tags", "key": "meta.description_max", "type": "int", "default": 150,
     "min": 80, "max": 320, "label": "Meta description — maximum characters", "help": ""},

    {"group": "Keyword coverage", "key": "keywords.primary_min_total", "type": "int",
     "default": 3, "min": 1, "max": 20, "label": "Primary keyword — minimum occurrences",
     "help": "How many times the primary keyword must appear across the H1, the headings and "
             "the body. This is the real floor on coverage; the density figure below is only a "
             "ceiling, because a long-tail phrase legitimately lands few times."},
    {"group": "Keyword coverage", "key": "keywords.primary_in_headings_min", "type": "int",
     "default": 1, "min": 0, "max": 6, "label": "Primary keyword — minimum H2/H3s carrying it",
     "help": "On top of the H1."},
    {"group": "Keyword coverage", "key": "keywords.primary_in_first_words", "type": "int",
     "default": 100, "min": 0, "max": 400, "label": "Primary keyword — appear within N opening words",
     "help": "0 turns the check off."},
    {"group": "Keyword coverage", "key": "keywords.primary_density_max", "type": "float",
     "default": 2.5, "min": 0.5, "max": 10.0, "step": "0.1",
     "label": "Primary keyword — density ceiling (%)",
     "help": "Above this the article reads as keyword stuffing and the report fails. Density "
             "counts a multi-word phrase by its length, the way SEO tools do."},
    {"group": "Keyword coverage", "key": "keywords.secondary_coverage_min", "type": "int",
     "default": 50, "min": 0, "max": 100, "label": "Secondary targets — coverage (%)",
     "help": "Share of the research stage's secondary keywords that must appear at least once. "
             "The ranked keyword pool is deliberately NOT checked: it holds 60-130 long-tail "
             "phrases an article will never carry verbatim, so any bar would warn every run."},
    {"group": "Keyword coverage", "key": "keywords.section_keyword_coverage_min", "type": "int",
     "default": 50, "min": 0, "max": 100, "label": "Brief section keywords — coverage (%)",
     "help": "Share of the keywords the brief assigned to sections that made it into the text."},
    {"group": "Keyword coverage", "key": "keywords.lsi_coverage_min", "type": "int",
     "default": 30, "min": 0, "max": 100, "label": "LSI vocabulary — coverage (%)",
     "help": "Share of the LSI terms present. These are single words and short phrases, so a "
             "well-written article clears this without trying."},

    {"group": "Uniqueness", "key": "plagiarism.warn_below", "type": "int", "default": 90,
     "min": 50, "max": 100, "label": "Target uniqueness (%)",
     "help": "Below this the uniqueness report warns. Our own domains are excluded from "
             "matching, so reusing phrasing from our own site does not count against it."},
    {"group": "Uniqueness", "key": "plagiarism.fail_below", "type": "int", "default": 80,
     "min": 0, "max": 100, "label": "Failing uniqueness (%)",
     "help": "Below this the report fails outright. It does not block publishing on its own — "
             "a human still decides."},
    {"group": "Publishing", "key": "publishing.on_accept", "type": "choice", "default": "none",
     "choices": [("none", "Nothing: Accept is only the mark"), ("draft", "Send or update the WordPress draft"),
                 ("publish", "Publish it on the blog")],
     "label": "When you accept a version", "help":
         "Accept is your mark that you looked at this version and it is good. This says what the "
         "button also does; its label says it too (Accept this version, Accept and send to the draft, "
         "Accept and publish). A post that is already live is only ever updated by a button named "
         "Update the live post. Nothing is sent when the checks pass, or by any other step."},
    {"group": "Publishing", "key": "publishing.category", "type": "choice", "default": "",
     "choices": [("", "No default category")] + [(c, c) for c in _profile_categories()],
     "label": "Category for posts sent from the Studio", "help":
         "The WordPress category a new post is filed under when the Studio sends it (by Accept or "
         "by a send). Without one, WordPress files it under its own default category."},

    {"group": "Uniqueness", "key": "plagiarism.min_passage_words", "type": "int", "default": 8,
     "min": 3, "max": 30, "label": "Shortest matched passage worth listing (words)",
     "help": "A four-word match is common phrasing, not copied text. The provider still counts "
             "it in the percentage; this only controls what appears in the rewrite list."},
]

GROUPS = list(dict.fromkeys(f["group"] for f in FIELDS))


def get_path(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set(d: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    for part in parts[:-1]:
        d = d.setdefault(part, {})
    d[parts[-1]] = value


def file_path(output_dir) -> Path:
    return Path(output_dir) / SETTINGS_FILE


def load(output_dir) -> dict:
    """Saved overrides, or {} when nothing has been changed from the UI.

    `prototypes` (saved before 2026-09-28) was shown as saved but never reached the keyword
    research, which reads a minimum and a maximum (RF-17); it is read as the minimum."""
    try:
        data = json.loads(file_path(output_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if "prototypes" in data:
        data.setdefault("prototypes_min", data.pop("prototypes"))
    if isinstance(data.get("publishing"), dict) and "on_ready" in data["publishing"]:
        # the retired "when an article passes every check": the choice on Accept starts at
        # nothing whatever it was, and the old value is kept once for Settings to say
        # (core/qa_migration.migrate_settings; plan contract 5)
        from .qa_migration import migrate_settings

        data, _ = migrate_settings(data)
        try:
            atomic_write_text(file_path(output_dir), json.dumps(data, indent=2, sort_keys=True) + "\n")
        except OSError:
            pass                      # read-only: the retired value is still not applied
    return data


def migrated_notice(output_dir) -> dict | None:
    """What the upgrade retired, for Settings to say once (until dismissed): {"on_ready", "at"}."""
    note = load(output_dir).get(MIGRATED)
    if not isinstance(note, dict) or note.get("dismissed_at") or "publishing.on_ready" not in note:
        return None
    return {"on_ready": note["publishing.on_ready"], "at": note.get("at")}


def dismiss_notice(output_dir) -> None:
    from datetime import datetime, timezone

    data = load(output_dir)
    note = data.get(MIGRATED)
    if isinstance(note, dict):
        note["dismissed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        atomic_write_text(file_path(output_dir), json.dumps(data, indent=2, sort_keys=True) + "\n")


def effective(base_seo: dict, output_dir) -> dict:
    """`base_seo` (from the YAML) with the saved overrides layered on top."""
    merged = json.loads(json.dumps(base_seo or {}))          # deep copy, plain data only
    for field in FIELDS:
        val = get_path(load(output_dir), field["key"])
        if val is not None:
            _set(merged, field["key"], val)
    return merged


def resolved(base_seo: dict, output_dir) -> dict:
    """Flat {dotted_key: value} of what is actually in force — for rendering the form."""
    eff = effective(base_seo, output_dir)
    return {f["key"]: (v if (v := get_path(eff, f["key"])) is not None else f["default"])
            for f in FIELDS}


def validate(form: dict, base_seo: dict | None = None) -> tuple[dict, list[str]]:
    """Coerce and range-check a submitted form. Returns (clean_overrides, errors).

    Only values that actually DIFFER from the shipped default are stored, so the file stays a
    short record of deliberate choices, the "changed" badge means something, and a later change
    to a default is picked up instead of being silently pinned by a redundant override.
    """
    clean: dict = {}
    errors: list[str] = []
    flat: dict = {}

    for field in FIELDS:
        key = field["key"]
        raw = form.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        if key == "publishing.category":
            # picked from the site's own categories (Connections > Test the connection), so any
            # one name; matched exactly against WordPress when a post is sent
            name = " ".join(str(raw).split())
            if len(name) > 120:
                errors.append(f"{field['label']}: a category name is at most 120 characters.")
                continue
            flat[key] = name
            continue
        if field["type"] == "choice":
            allowed = {c[0] for c in field["choices"]}
            if str(raw) not in allowed:
                errors.append(f"{field['label']}: '{raw}' is not one of the available options.")
                continue
            flat[key] = str(raw)
        elif field["type"] == "float":
            try:
                n = round(float(str(raw).strip().replace(",", ".")), 2)
            except ValueError:
                errors.append(f"{field['label']}: '{raw}' is not a number.")
                continue
            if not (field["min"] <= n <= field["max"]):
                errors.append(f"{field['label']}: {n} is outside {field['min']}-{field['max']}.")
                continue
            flat[key] = n
        else:
            try:
                n = int(str(raw).strip())
            except ValueError:
                errors.append(f"{field['label']}: '{raw}' is not a whole number.")
                continue
            if not (field["min"] <= n <= field["max"]):
                errors.append(f"{field['label']}: {n} is outside {field['min']}-{field['max']}.")
                continue
            flat[key] = n

    # cross-field rules, on what would be in force (a field not given is its default) — a min
    # above its max would make the meta repair loop unsatisfiable and spin the model for three
    # rounds on every article
    def _value(key):
        v = flat.get(key)
        return v if v is not None else (get_path(base_seo or {}, key)
                                        if get_path(base_seo or {}, key) is not None
                                        else next(f["default"] for f in FIELDS if f["key"] == key))

    for lo_key, hi_key, what in (("meta.title_min", "meta.title_max", "Title tag"),
                                 ("meta.description_min", "meta.description_max", "Meta description")):
        lo, hi = _value(lo_key), _value(hi_key)
        if isinstance(lo, int) and isinstance(hi, int) and lo >= hi:
            errors.append(f"{what}: the minimum ({lo}) must be below the maximum ({hi}).")
    lo, hi, pages = _value("prototypes_min"), _value("prototypes_max"), _value("audit_max_pages")
    if lo > hi:
        errors.append(f"Prototype competitors: at least ({lo}) cannot be more than at most ({hi}).")
    if hi > pages:
        errors.append(f"Prototype competitors ({hi}) cannot exceed competitor pages to open "
                      f"({pages}) — the prototypes are fetched from that budget.")

    if errors:
        return {}, errors
    for field in FIELDS:
        key = field["key"]
        if key not in flat:
            continue
        default = get_path(base_seo or {}, key)
        if default is None:
            default = field["default"]
        if flat[key] != default:
            _set(clean, key, flat[key])
    return clean, []


def save_values(output_dir, values: dict, base_seo: dict, keys: list[str] | None = None) -> list[str]:
    """Save `values` for the fields `keys` (default: the keys given), every other saved value kept:
    one Configure page, or `cli.py settings set`, changes its own fields only. A field among
    `keys` without a value goes back to its default. The rules between fields are checked on the
    settings as they would be after the save (the saved ones with these changed). Returns the
    errors (nothing saved then)."""
    keys = list(keys if keys is not None else values)
    unknown = [k for k in keys if k not in {f["key"] for f in FIELDS}]
    if unknown:
        return [f"No setting named {', '.join(unknown)}."]
    saved = load(output_dir)
    candidate = {f["key"]: get_path(saved, f["key"]) for f in FIELDS if f["key"] not in keys}
    candidate.update({k: values.get(k) for k in keys})
    clean, errors = validate({k: v for k, v in candidate.items() if v is not None}, base_seo)
    if errors:
        return errors
    kept: dict = {}
    for f in FIELDS:
        v = get_path(clean, f["key"]) if f["key"] in keys else get_path(saved, f["key"])
        if v is not None:
            _set(kept, f["key"], v)
    save(output_dir, kept)
    return []


MIGRATED = "_migrated"      # what an upgrade retired (core/qa_migration.migrate_settings)


def save(output_dir, overrides: dict) -> None:
    """Write the form's overrides. The upgrade's note of a retired setting is kept: Settings says
    it once, and a save of the form is not that."""
    p = file_path(output_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    note = load(output_dir).get(MIGRATED)
    data = {**overrides, **({MIGRATED: note} if isinstance(note, dict) and MIGRATED not in overrides else {})}
    atomic_write_text(p, json.dumps(data, indent=2, sort_keys=True) + "\n")


def reset(output_dir) -> None:
    """Drop every override and fall back to the YAML defaults (the upgrade's note stays, as on a
    save)."""
    if isinstance(load(output_dir).get(MIGRATED), dict):
        save(output_dir, {})
        return
    try:
        file_path(output_dir).unlink()
    except OSError:
        pass
