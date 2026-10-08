"""What the paid services charge: the price table the cost report prices usage with.

Usage is recorded in each service's own units (tokens, images, runs, requests, credits, API
units; core/usage.py), and priced here. Prices change, so the table is data: the defaults below
carry the date they were read and where, `pricing:` in config.yaml or project/profile.yaml can
override any of them, and the Studio's Settings > Prices stores the team's own (for a plan
nobody publishes a per-unit price for: Ahrefs API units come with the subscription, Winston's
API credits with the pack the team bought).

A usage event keeps the cost it was priced at when it happened (a promotion ending later does
not rewrite history); an event recorded while its price was unknown is priced when shown, with
whatever the table holds then.
"""
from __future__ import annotations

import json
from pathlib import Path

AS_OF = "2026-09-28"

# per million tokens. Anthropic: cache reads are 0.1x input, 5-minute cache writes 1.25x, 1-hour
# writes 2x (the QA panel's system prompt is cached for an hour). Source: the Claude API pricing
# table (platform.claude.com/docs pricing), read 2026-09-28; the 5.5 models from their model pages,
# 2026-09-30 (Opus 5.5 reads its cache at 5% of input, not 10%).
ANTHROPIC = {
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25},
    "claude-fable-5": {"input": 10.0, "output": 50.0},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0},
    "claude-opus-5": {"input": 5.0, "output": 25.0},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}

DEFAULTS: dict = {
    "as_of": AS_OF,
    "anthropic": {m: {"input": p["input"], "output": p["output"],
                      "cache_read": p.get("cache_read", round(p["input"] * 0.1, 4)),
                      "cache_write_5m": round(p["input"] * 1.25, 4),
                      "cache_write_1h": round(p["input"] * 2, 4)} for m, p in ANTHROPIC.items()},
    # per million tokens; developers.openai.com/api/docs/pricing, read 2026-09-28
    "openai": {
        "gpt-5.6-sol": {"input": 4.0, "output": 20.0, "cached_input": 0.40},
        "gpt-image-2": {"text_input": 5.0, "image_input": 8.0, "image_output": 30.0,
                        "cached_text_input": 1.25, "cached_image_input": 2.0},
    },
    # when the images API returns no token usage: an estimate per image by quality, 1024x1024
    # (the `img` CLI's table); a 1536x1024 image is scaled by its pixel count
    "openai_image_estimate": {"low": 0.006, "medium": 0.05, "high": 0.21},
    # parallel.ai/pricing, read 2026-09-28: Task API per run by processor, Search per request
    "parallel": {"task": {"lite": 0.005, "base": 0.01, "core": 0.025, "core2x": 0.05, "pro": 0.10,
                          "ultra": 0.30, "ultra2x": 0.60, "ultra4x": 1.20, "ultra8x": 2.40},
                 "search": 0.005},
    # Scrapfly Discovery plan: $30 a month for 200,000 credits (scrapfly.io/pricing, 2026-09-28)
    "scrapfly": {"credit": 30.0 / 200_000},
    # included in a subscription or a pack: no per-unit price unless the team enters one
    "ahrefs": {"unit": None},
    "winston": {"credit": None},
}

_OVERRIDES_FILE = "_prices.json"


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def table(config: dict | None = None, output_dir: Path | None = None) -> dict:
    """The effective price table: defaults, then `pricing:` from the config, then the Studio's
    saved prices (`<output>/_prices.json`)."""
    t = DEFAULTS
    if config and config.get("pricing"):
        t = _merge(t, config["pricing"])
    if output_dir is not None:
        try:
            saved = json.loads((Path(output_dir) / _OVERRIDES_FILE).read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                t = _merge(t, saved)
        except (OSError, ValueError):
            pass
    return t


def save_overrides(output_dir: Path, overrides: dict) -> None:
    from .store import write_json

    write_json(Path(output_dir) / _OVERRIDES_FILE, overrides)


def saved_overrides(output_dir: Path) -> dict:
    try:
        v = json.loads((Path(output_dir) / _OVERRIDES_FILE).read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _model_prices(group: dict, model: str) -> dict | None:
    """Exact id, else the longest known id the given one starts with (a dated snapshot)."""
    if model in group:
        return group[model]
    best = max((m for m in group if model.startswith(m)), key=len, default=None)
    return group.get(best) if best else None


def price(provider: str, item: str, units: dict, t: dict) -> float | None:
    """The cost in USD of `units` of `item`, or None when the table has no price for it."""
    per_m = 1_000_000
    if provider == "anthropic":
        p = _model_prices(t.get("anthropic") or {}, item)
        if not p:
            return None
        return (units.get("input_tokens", 0) * p["input"] + units.get("output_tokens", 0) * p["output"]
                + units.get("cache_read_tokens", 0) * p["cache_read"]
                + units.get("cache_write_5m_tokens", 0) * p["cache_write_5m"]
                + units.get("cache_write_1h_tokens", 0) * p["cache_write_1h"]) / per_m
    if provider == "openai":
        p = _model_prices(t.get("openai") or {}, item)
        if item.startswith("gpt-image"):
            if p and any(k in units for k in ("image_output_tokens", "text_input_tokens")):
                return (units.get("text_input_tokens", 0) * p.get("text_input", 0)
                        + units.get("image_input_tokens", 0) * p.get("image_input", 0)
                        + units.get("image_output_tokens", 0) * p.get("image_output", 0)
                        + units.get("cached_text_input_tokens", 0) * p.get("cached_text_input", 0)
                        + units.get("cached_image_input_tokens", 0) * p.get("cached_image_input", 0)) / per_m
            est = (t.get("openai_image_estimate") or {}).get(str(units.get("quality") or "medium"))
            if est is None:
                return None
            w, h = (units.get("size") or [1024, 1024])[:2]
            return est * units.get("images", 1) * (w * h) / (1024 * 1024)
        if not p:
            return None
        return (units.get("input_tokens", 0) * p["input"] + units.get("output_tokens", 0) * p["output"]
                + units.get("cache_read_tokens", 0) * p.get("cached_input", p["input"])) / per_m
    if provider == "parallel":
        pp = t.get("parallel") or {}
        if item.startswith("task:"):
            rate = (pp.get("task") or {}).get(item.split(":", 1)[1])
            return None if rate is None else rate * units.get("runs", 1)
        rate = pp.get("search")
        return None if rate is None else rate * units.get("requests", 1)
    if provider == "scrapfly":
        rate = (t.get("scrapfly") or {}).get("credit")
        return None if rate is None else rate * units.get("credits", 0)
    if provider == "ahrefs":
        rate = (t.get("ahrefs") or {}).get("unit")
        return None if rate is None else rate * units.get("units", 0)
    if provider == "winston":
        rate = (t.get("winston") or {}).get("credit")
        return None if rate is None else rate * units.get("credits", 0)
    return None
