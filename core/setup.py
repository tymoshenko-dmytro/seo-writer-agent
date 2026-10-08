"""The first run: the project, its texts, the keys, WordPress; each step can be skipped and is
offered until the install can write. The setup screen and `cli.py setup` write the same things
through the same modules, and `cli.py doctor` reads the same state.

`state()` says what is still missing; `apply()` takes what was given (a name, a logo, the site,
the project's texts, keys, the WordPress connection), saves each, tests what was given (free
calls), and returns one report:
{"saved": [...], "tests": {name: {"ok", "detail"}}, "errors": [...], "state": state()}."""
from __future__ import annotations

import os
from pathlib import Path

from . import api_keys, connections, project_settings, project_texts

LABELS = {k["env"]: k["label"] for k in api_keys.KEYS}
LABELS[connections.WORDPRESS] = connections.WORDPRESS


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def state(output_dir) -> dict:
    """What the install still lacks before it can write, read without asking any service:

    - the project's name and its site's domain (the prompts speak for them; the link index and
      the uniqueness check read the domain);
    - every one of the seven services set (`connections.REQUIRED` and WordPress);
    - their tests: a run starts only when each has passed for the key in force (`ready_to_write`
      tests what is untested first, free).

    `can_write` is everything set and nothing known to fail; `done` is that and every test
    passed."""
    api_keys.apply(output_dir)
    tests = connections.recorded(output_dir)
    needs = []
    if not project_texts.current(output_dir, "project_name").strip():
        needs.append("the project's name")
    if not _domains(output_dir):
        needs.append("the site's domain")
    services = [*connections.REQUIRED, connections.WORDPRESS]
    is_set = {env: bool(os.getenv(env)) for env in connections.REQUIRED}
    is_set[connections.WORDPRESS] = all(os.getenv(k) for k in api_keys.WP_GROUP)
    missing = [LABELS[n] for n in services if not is_set[n]]
    failing = [LABELS[n] for n in services if is_set[n] and (tests.get(n) or {}).get("ok") is False]
    untested = [LABELS[n] for n in services if is_set[n] and (tests.get(n) or {}).get("ok") is None]
    parts = []
    if needs + missing:
        parts.append(_and(needs + missing))
    if failing:
        parts.append(f"{_and(failing)} to pass {'its' if len(failing) == 1 else 'their'} test")
    can_write = not (needs or missing or failing)
    return {"done": can_write and not untested, "can_write": can_write,
            "required_missing": needs + missing, "failing": failing, "untested": untested,
            "reason": ("Writing needs " + "; and ".join(parts) + ".") if parts else "",
            "project": not needs, "wordpress": is_set[connections.WORDPRESS],
            "wordpress_tested": bool((tests.get(connections.WORDPRESS) or {}).get("ok"))}


def _domains(output_dir) -> tuple:
    import yaml

    from .brand import from_profile
    from .config import project_root

    root = project_root()
    profile = yaml.safe_load((root / "profile.yaml").read_text(encoding="utf-8")) or {}
    return from_profile(project_settings.apply(profile, output_dir), root).domains


def ready_to_write(output_dir) -> str:
    """Before a run starts: every required credential that has not passed its test is tested now
    (free calls), and the reason a run may not start is returned ("" when it may)."""
    st = state(output_dir)
    if st["can_write"] and st["untested"]:
        connections.check_all(output_dir, only_untested=True)
        st = state(output_dir)
    if not st["can_write"]:
        return st["reason"]
    if st["untested"]:
        return f"Writing needs {_and(st['untested'])} to pass {'its' if len(st['untested']) == 1 else 'their'} test."
    return ""


def apply(output_dir, *, name: str | None = None, logo: Path | None = None, domain: str | None = None,
          blog: str | None = None, audience: str | None = None, texts: dict | None = None, keys: dict | None = None,
          wp_url: str | None = None, wp_user: str | None = None, wp_password: str | None = None,
          author_id=None, category: str | None = None, test: bool = True) -> dict:
    """Save what was given, test it, say what is still missing. `texts` holds the project's longer
    texts by key (brand_voice, product_facts, editorial_rules): a text given empty is cleared, a
    key not given is left as it is."""
    saved, tests, errors = [], {}, []

    def attempt(label, fn):
        try:
            fn()
            saved.append(label)
        except ValueError as e:                  # each module refuses with the field and the rule
            errors.append(f"{label}: {e}")

    if name:
        attempt("name", lambda: project_texts.save(output_dir, "project_name", name))
    if audience:
        attempt("audience", lambda: project_texts.save(output_dir, "audience", audience))
    for key, text in (texts or {}).items():
        if (text or "").strip():
            attempt(key.replace("_", " "), lambda key=key, text=text: project_texts.save(output_dir, key, text))
        else:
            attempt(key.replace("_", " "), lambda key=key: project_texts.clear(output_dir, key))
    if logo:
        attempt("logo", lambda: project_settings.save_logo(output_dir, Path(logo).read_bytes(), Path(logo).name))
    if domain or blog:
        attempt("website", lambda: project_settings.save_website(output_dir, domains=domain or None, blog_base_url=blog or None))
    updates = {k: v for k, v in (keys or {}).items() if v}
    if wp_url or wp_user or wp_password:
        updates.update({"WP_BASE_URL": wp_url or "", "WP_USERNAME": wp_user or "", "WP_APP_PASSWORD": wp_password or ""})
    if updates:
        attempt("keys", lambda: api_keys.save(output_dir, updates, []))
        api_keys.apply(output_dir)
    if test:
        answers = {env: connections.check(output_dir, env) for env in updates if env in connections.TESTS}
        if any(k in updates for k in api_keys.WP_GROUP) or author_id is not None or category is not None:
            answers[connections.WORDPRESS] = connections.check(output_dir, connections.WORDPRESS)
        # an answer about a key replaced during its test is reported as such, not as its result
        tests = {LABELS[n]: {"ok": a.get("ok"), "detail": a.get("detail")}
                 for n, a in connections.fresh(output_dir, answers).items()}
    if author_id is not None:                    # "": the user the password belongs to
        attempt("author", lambda: project_settings.save_wordpress(output_dir, author_id=author_id))
    if category is not None:                     # "": no default category
        from . import settings as user_settings
        from .config import load_core_config, load_project

        base = load_project(core=load_core_config(), apply_user_settings=False).seo
        errs = user_settings.save_values(output_dir, {"publishing.category": category}, base)
        errors += errs
        if not errs:
            saved.append("category")
    return {"saved": saved, "tests": tests, "errors": errors, "state": state(output_dir)}
