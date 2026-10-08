"""Helpers the Studio's views share."""
from __future__ import annotations

import os

from flask import jsonify

from core import pricing

from ..context import CONFIG, out, project
from ..jobs import runner


def prices() -> dict:
    """The price table in force: the defaults, `pricing:` of config.yaml and of the project's
    profile, then Settings > Prices."""
    from core.config import runtime_config

    return pricing.table(runtime_config(CONFIG, project()), out())


AI, IMAGES = "ANTHROPIC_API_KEY", "OPENAI_API_KEY"


def missing_keys(*envs: str) -> list[str]:
    """The labels of the keys a paid action needs that this install lacks (the saved keys applied
    first). A Re-capture without the Anthropic key ran and failed with the SDK's own words,
    "Could not resolve authentication method…" (2026-10-05): now it does not start."""
    from core import api_keys

    api_keys.apply(out())
    labels = {k["env"]: k["label"] for k in api_keys.KEYS}
    return [labels.get(e, e) for e in envs if not os.getenv(e)]


def needs_sentence(missing: list[str]) -> str:
    return f"Nothing done: this needs the {' and '.join(missing)} key{'s' if len(missing) > 1 else ''}: add it in Configure > Connections."


def key_state() -> dict:
    """What the article page's paid actions can do here: `ai` (the Anthropic key: reviews,
    changes, charts, screenshots), `images` (the OpenAI key: covers and photos), and the reason
    a disabled action shows."""
    ai, images = not missing_keys(AI), not missing_keys(IMAGES)
    return {"ai": ai, "images": images, "both": ai and images,
            "ai_reason": None if ai else "Needs the Anthropic key: Configure > Connections.",
            "images_reason": None if images else "Needs the OpenAI key: Configure > Connections.",
            "both_reason": None if ai and images else ("Needs the Anthropic and OpenAI keys: Configure > Connections."
                                                       if not ai and not images else
                                                       "Needs the Anthropic key: Configure > Connections." if not ai else
                                                       "Needs the OpenAI key: Configure > Connections.")}


def needs_word(needs: tuple[str, ...]) -> str | None:
    """The page's name for a set of keys: "ai", "images", "both", or None (free)."""
    if not needs:
        return None
    return "both" if AI in needs and IMAGES in needs else ("ai" if AI in needs else "images")


def picture_needs(rel: str, folder=None, as_image: bool = False, kind: str | None = None) -> tuple[str, ...]:
    """The keys making a picture again needs, by what makes it now (interactive.picture_kind, the
    rule edit_visual follows, RD45-02) or the kind it is made as (`kind`; "auto" lets the producer
    choose, and it chooses an illustration only with the image model's key): the image model for a
    cover or a photo; both models for an illustration (the writing model plans it, the image model
    draws it); the writing model (and a browser) for a chart, an infographic or a screenshot."""
    from core.agents.interactive import picture_kind

    name = str(rel or "").rsplit("/", 1)[-1]
    if as_image or kind == "image":
        return (AI, IMAGES)
    if kind in ("auto", "screenshot", "infographic", "chart"):
        return (AI,)
    if folder is None:
        return (IMAGES,) if name == "cover.png" or name.startswith("image_") else (AI,)
    kind = picture_kind(folder, rel)
    return {"cover": (IMAGES,), "photo": (IMAGES,), "illustration": (AI, IMAGES)}.get(kind, (AI,))


def finding_needs(f: dict | None, folder=None) -> tuple[str, ...]:
    """The keys a finding's change needs: none for a picture's removal (code), what making the
    picture again needs for a redraw or a re-capture (picture_needs), the writing model's for the
    rest (RF0D-01, RF0D-02)."""
    f = f or {}
    action, target = f.get("action"), f.get("target") or {}
    if action == "remove_picture":
        return ()
    if action in ("redraw", "recapture") and isinstance(target, dict) and target.get("asset"):
        return picture_needs(target["asset"], folder)
    return (AI,)


def queue_json(label: str, kind: str, fn, slug: str | None = None, needs: tuple[str, ...] = (AI,)):
    """A model call as a background job, answered with its id for the page to poll. Run inline
    they die at the gateway (the 502 on Regenerate); a slow model must not look broken. Without
    a key it `needs`, nothing starts and the page gets why."""
    missing = missing_keys(*needs)
    if missing:
        return jsonify({"error": needs_sentence(missing)}), 409
    jid = runner.submit_task(label, fn, kind=kind, slug=slug)
    return jsonify({"job": jid}), 202

