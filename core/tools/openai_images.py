"""OpenAI image generation (gpt-image-2): the cover backgrounds and the AI photos.

Called directly rather than through the `img` CLI (bin/img, still there for people): the API's
response carries the call's token usage, which the CLI threw away, so an article's images had no
cost. Every call is recorded in the cost ledger (core/usage.py) with its tokens, or, when the API
returns none, as one image of its quality and size (priced by the estimate table).

Returns the image bytes, or None: a failed image never fails the caller. The reason is printed
and kept for this thread (`last_error()`), so a caller can write it down: the Zoom article's cover
failed and nothing on record said why.
"""
from __future__ import annotations

import base64
import mimetypes
import os
import threading
from pathlib import Path

API = "https://api.openai.com/v1"
MODEL = "gpt-image-2"
TIMEOUT_S = 360
_last = threading.local()


def last_error() -> str | None:
    """Why this thread's last image call returned None (None after a success)."""
    return getattr(_last, "error", None)


def _fail(what: str) -> None:
    _last.error = what
    print(f"[images] {what}")


def _record(quality: str, size: str, data: dict) -> None:
    from .. import usage

    u = data.get("usage") or {}
    det = u.get("input_tokens_details") or {}
    units: dict = {"images": len(data.get("data") or []) or 1, "quality": quality}
    try:
        w, h = (int(x) for x in size.split("x"))
        units["size"] = [w, h]
    except ValueError:
        pass
    if u:
        units.update({"text_input_tokens": int(det.get("text_tokens") or 0)
                      or max(0, int(u.get("input_tokens") or 0) - int(det.get("image_tokens") or 0)),
                      "image_input_tokens": int(det.get("image_tokens") or 0),
                      "image_output_tokens": int(u.get("output_tokens") or 0)})
    usage.record("openai", data.get("model") or MODEL, units, agent="images")


def _key() -> str | None:
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if key:
        return key
    p = Path.home() / ".config" / "openai" / "api-key"      # the img CLI's key file, on a laptop
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def generate(prompt: str, quality: str = "medium", size: str = "1536x1024") -> bytes | None:
    import requests

    key = _key()
    if not key:
        _fail("OPENAI_API_KEY is not set")
        return None
    try:
        r = requests.post(f"{API}/images/generations", timeout=TIMEOUT_S,
                          headers={"Authorization": f"Bearer {key}"},
                          json={"model": MODEL, "prompt": prompt, "size": size, "quality": quality,
                                "n": 1, "output_format": "png"})
        if r.status_code >= 400:
            _fail(f"generate failed: HTTP {r.status_code} {r.text[:200]}")
            return None
        data = r.json()
        _record(quality, size, data)
        b64 = ((data.get("data") or [{}])[0]).get("b64_json")
        if not b64:
            _fail("the answer carried no image")
            return None
        _last.error = None
        return base64.b64decode(b64)
    except Exception as e:  # noqa: BLE001
        _fail(f"generate failed: {e}")
        return None


def edit(image_path: str, prompt: str, quality: str = "medium", size: str = "auto") -> bytes | None:
    import requests

    key = _key()
    if not key:
        _fail("OPENAI_API_KEY is not set")
        return None
    p = Path(image_path)
    try:
        mime = mimetypes.guess_type(str(p))[0] or "image/png"
        with open(p, "rb") as fh:
            r = requests.post(f"{API}/images/edits", timeout=TIMEOUT_S,
                              headers={"Authorization": f"Bearer {key}"},
                              data={"model": MODEL, "prompt": prompt, "quality": quality, "size": size,
                                    "n": "1", "output_format": "png"},
                              files={"image": (p.name, fh, mime)})
        if r.status_code >= 400:
            _fail(f"edit failed: HTTP {r.status_code} {r.text[:200]}")
            return None
        data = r.json()
        _record(quality, size if size != "auto" else "1024x1024", data)
        b64 = ((data.get("data") or [{}])[0]).get("b64_json")
        if not b64:
            _fail("the answer carried no image")
            return None
        _last.error = None
        return base64.b64decode(b64)
    except Exception as e:  # noqa: BLE001
        _fail(f"edit failed: {e}")
        return None
