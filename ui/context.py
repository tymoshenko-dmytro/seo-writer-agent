"""What every Studio view needs: the engine config, the project as a run sees it now, and the
article folders (through core/store)."""
from __future__ import annotations

from flask import abort, current_app

from core import store
from core.config import load_core_config, load_project

CONFIG = load_core_config()


def out():
    """The output root this app serves (app.config['OUTPUT_ROOT'], set by create_app)."""
    return current_app.config["OUTPUT_ROOT"]


def repo() -> store.Repository:
    return store.Repository(out())


def project():
    """The project as a run sees it now: the name, audience, brand voice, product facts and
    research settings edited in the Studio apply to the next action without a restart."""
    return load_project(core=CONFIG)


def article(slug: str) -> store.Article:
    """The article folder behind a route, or 404. The app's own folders (_prompts, _trash) and
    hidden test articles are never articles: an article action on one moved every prompt edit
    to the trash (review of 5fb183c)."""
    try:
        return repo().get(slug, listed_only=True)
    except KeyError:
        abort(404)
