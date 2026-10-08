"""Config + project-profile loading.

Two layers:
  - core `config.yaml`  -> engine defaults (models, pipeline knobs, paths)
  - `project/profile.yaml` + brand.md + products.md -> the active product

Each instance has exactly ONE `project/` dir, so the active product is unambiguous
and products never cross (isolation by design).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent  # repo root


@dataclass
class Project:
    slug: str
    name: str
    audience: str
    brand_voice: str       # full text of project/brand.md
    product_facts: str     # full text of project/products.md (facts + guardrails)
    publish: dict
    models: dict
    defaults: dict
    root: Path
    chart_style: str | None = None
    links: dict = field(default_factory=dict)   # internal-link inventory config
    seo: dict = field(default_factory=dict)     # SERP/prototype/LSI knobs (see config.yaml)
    brand: "Brand" = None                       # domains, logo, palette (profile `brand:`, core/brand.py)
    # what the profile (and the Studio's settings) change in the core config's pipeline, models
    # and seo blocks: laid over whatever config a caller runs with (runtime_config)
    overrides: dict = field(default_factory=dict)
    editorial_rules: str = ""   # the project's own rules for the writer and the review (project/editorial_rules.md)


def load_core_config(path: Path | None = None) -> dict:
    path = path or ROOT / "config.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


def project_root(core: dict | None = None) -> Path:
    """The active project folder: $CONTENT_PROJECT_DIR, else config.yaml `paths.project`."""
    import os

    env = (os.getenv("CONTENT_PROJECT_DIR") or "").strip()
    if env:
        return Path(env)
    core = core or load_core_config()
    return ROOT / core["paths"]["project"]


def merged(base: dict, over: dict | None) -> dict:
    """`over` layered on `base`, nested blocks key by key: a profile that sets `qa: {reads: 1}`
    changes the number of reads and keeps the rest of the core's qa block. Lists and scalars
    are replaced whole."""
    out = dict(base or {})
    for k, v in (over or {}).items():
        out[k] = merged(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def runtime_config(config: dict, project: "Project | None") -> dict:
    """`config` with the project's own settings laid over its pipeline, models and seo blocks:
    what the agents and the routers read. `load_project` merged the profile into
    `project.defaults`, but every agent kept reading `config["pipeline"]`, so a profile's
    `qa: {reads: 5}` changed nothing (review RV-14). A caller's own config (a test's, a run's)
    stays the base. Idempotent."""
    over = getattr(project, "overrides", None) or {}
    if not over:
        return config
    return {**config, **{k: merged(config.get(k) or {}, over.get(k))
                          for k in ("pipeline", "models", "seo", "pricing") if k in over or k in config}}


def load_project(project_dir: Path | None = None, core: dict | None = None,
                 apply_user_settings: bool = True) -> Project:
    core = core or load_core_config()
    project_dir = project_dir or project_root(core)
    profile = yaml.safe_load((project_dir / "profile.yaml").read_text(encoding="utf-8")) or {}
    if apply_user_settings:
        # the logo, the colours, the website, the competitors and the author saved in the
        # Studio (Configure > Project, Connections), laid over the profile (core/project_settings.py)
        from . import project_settings
        from .store import output_root as _out

        profile = project_settings.apply(profile, _out(core))

    def _read(name: str) -> str:
        p = project_dir / name
        return p.read_text(encoding="utf-8") if p.exists() else ""

    defaults = merged(core.get("pipeline", {}), profile.get("defaults", {}))
    models = merged(core.get("models", {}), profile.get("models", {}))
    seo = merged(core.get("seo", {}), profile.get("seo", {}))
    # Overrides the team saved from the Studio. They live on the persistent volume because the
    # YAML above ships inside the container image and would be discarded by the next release.
    # generate_article() calls load_project() per run, so a saved change applies immediately.
    # The settings PAGE passes apply_user_settings=False, because it needs the untouched
    # defaults to compare against — otherwise re-saving a default would pin it as an override.
    from .store import output_root

    seo_over = profile.get("seo") or {}
    if apply_user_settings:
        from . import settings as user_settings

        seo = user_settings.effective(seo, output_root(core))
        seo_over = user_settings.effective(seo_over, output_root(core))
    # API keys saved in the Studio (Settings > API keys) go into the environment the agents read,
    # on every run, so a team can use its own accounts (core/api_keys.py)
    from . import api_keys

    api_keys.apply(output_root(core))
    texts = {"project_name": str(profile.get("name") or ""), "audience": str(profile.get("audience") or ""),
             "brand_voice": _read(profile.get("brand_file", "brand.md")),
             "product_facts": _read(profile.get("products_file", "products.md")),
             "editorial_rules": _read(profile.get("editorial_rules_file", "editorial_rules.md"))}
    if apply_user_settings:
        # the name, audience, brand voice, product facts and editorial rules edited in the Studio
        # (Configure > Project, the setup) replace the shipped ones, like the prompt edits
        # (core/project_texts.py)
        from . import project_texts

        for key in texts:
            e = project_texts.edited(output_root(core), key)
            if e is not None:
                texts[key] = e

    return Project(
        slug=str(profile.get("slug") or "project"),
        name=texts["project_name"],
        audience=texts["audience"],
        brand_voice=texts["brand_voice"],
        product_facts=texts["product_facts"],
        publish=profile.get("publish", {}),
        models=models,
        defaults=defaults,
        root=project_dir,
        chart_style=profile.get("chart_style"),
        links=profile.get("links", {}),
        seo=seo,
        brand=_brand_from(profile, project_dir),
        editorial_rules=texts["editorial_rules"],
        overrides={"pipeline": profile.get("defaults") or {}, "models": profile.get("models") or {},
                   "seo": seo_over, "pricing": profile.get("pricing") or {}},
    )


def _brand_from(profile: dict, project_dir: Path):
    from .brand import from_profile

    return from_profile(profile, project_dir)
