"""AI images (OpenAI gpt-image-2, core/tools/openai_images).

generate(): a new image from a prompt. edit(): modify an existing image with a feedback prompt.
Quality: low=draft, medium=working (default), high=final. The file is replaced only once the new
image has arrived whole, so a failed call leaves the previous picture as it was. Returns the
path or None: a failed image never fails the caller.
"""
from __future__ import annotations

from pathlib import Path

from ..store import atomic_write_bytes
from ..tools import openai_images


def generate(prompt: str, out_path: str, quality: str = "medium", size: str = "1536x1024") -> str | None:
    data = openai_images.generate(prompt, quality=quality, size=size)
    if not data:
        return None
    atomic_write_bytes(Path(out_path), data)
    return out_path


def edit(image_path: str, prompt: str, out_path: str | None = None, quality: str = "medium") -> str | None:
    data = openai_images.edit(image_path, prompt, quality=quality)
    if not data:
        return None
    atomic_write_bytes(Path(out_path or image_path), data)
    return out_path or image_path
