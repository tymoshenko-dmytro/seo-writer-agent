"""Blog cover generator — matches the live blog template:
photo-style background (gpt-image-2, core/tools/openai_images) + big white bold headline
top-left + the project's logo on a white pill bottom-right. ~2:1 landscape.

Text is composited here with Pillow, never baked into the AI image (gpt-image-2 renders
text poorly). A failed background or missing logo returns None — a cover never fails
the article.
"""
from __future__ import annotations

import io
from pathlib import Path

from ..store import atomic_write_bytes

def headline_case(text: str, max_words: int = 7, acronyms=None) -> str:
    """Title case for the cover, with the project's acronyms upper-cased (profile
    `brand.headline_acronyms` on top of the common ones, core/brand.py)."""
    if acronyms is None:
        from ..brand import default as _brand

        acronyms = _brand().acronyms
    words = text.strip().split()[:max_words]
    out = []
    for w in words:
        out.append(w.upper() if w.lower() in acronyms else (w if w[:1].isupper() else w.capitalize()))
    return " ".join(out)


# where the headline is set: it wraps at 60% of the width from the left edge (generate, step 4),
# so the background keeps its subject out of that part
HEADLINE_ROOM = ("keep the main subject in the right part of the frame: the left 60% stays calm "
                 "and uncluttered, a headline is set over it")
NO_WORDS = "no text, no words, no letters, no logos"


def _bold_font(size: int):
    from PIL import ImageFont
    from matplotlib import font_manager
    path = font_manager.findfont("DejaVu Sans:weight=bold")
    return ImageFont.truetype(path, size)


def _wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines[:3]


def generate(
    headline: str,
    bg_prompt: str,
    out_path: str,
    logo_png: str | None = None,
    quality: str = "medium",
    size: tuple[int, int] = (1200, 600),
    bg_image: str | None = None,
    fresh: bool = False,
) -> str | None:
    """Compose the cover into `out_path`. The background is `bg_image`, else the saved
    cover_bg.png, else a new gpt-image-2 picture saved as cover_bg.png. `fresh` draws a new
    background even when one is saved, and replaces the saved one only after the cover is whole:
    a failed New cover used to delete the background first (review RR-07)."""
    try:
        from PIL import Image, ImageDraw

        W, H = size
        saved_bg = Path(out_path).parent / "cover_bg.png"
        new_bg: bytes | None = None
        if bg_image and Path(bg_image).exists():
            bg = Image.open(bg_image).convert("RGB")
        elif saved_bg.exists() and not fresh:
            bg = Image.open(saved_bg).convert("RGB")
        else:
            from ..tools import openai_images

            prompt = f"{bg_prompt}. Photorealistic editorial photo, clean composition, {HEADLINE_ROOM}, {NO_WORDS}."
            data = openai_images.generate(prompt, quality=quality, size="1536x1024")
            if not data:
                print("[cover] the background image was not generated")
                return None
            bg = Image.open(io.BytesIO(data)).convert("RGB")
            new_bg = data

        # 2) cover-crop to WxH
        scale = max(W / bg.width, H / bg.height)
        bg = bg.resize((round(bg.width * scale), round(bg.height * scale)), Image.LANCZOS)
        left, top = (bg.width - W) // 2, (bg.height - H) // 2
        im = bg.crop((left, top, left + W, top + H))

        # 3) scrim: uniform + stronger left gradient for headline legibility
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 60))
        grad = Image.new("L", (W, 1))
        for x in range(W):
            grad.putpixel((x, 0), max(0, int(120 * (1 - x / (W * 0.7)))))
        overlay.putalpha(Image.eval(grad.resize((W, H)), lambda a: min(255, a + 60)))
        im = Image.alpha_composite(im.convert("RGBA"), overlay)
        draw = ImageDraw.Draw(im)

        # 4) headline — white, bold, left
        fsize = 76
        font = _bold_font(fsize)
        lines = _wrap(draw, headline, font, int(W * 0.60))
        while len(lines) > 2 and fsize > 54:  # prefer <=2 lines, shrink a little if needed
            fsize -= 6
            font = _bold_font(fsize)
            lines = _wrap(draw, headline, font, int(W * 0.60))
        y = int(H * 0.14)
        for ln in lines:
            draw.text((int(W * 0.055), y), ln, font=font, fill=(255, 255, 255, 255))
            y += int(fsize * 1.18)

        # 5) logo pill bottom-right — supersampled corners for clean anti-aliased edges
        if logo_png and Path(logo_png).exists():
            logo = Image.open(logo_png).convert("RGBA")
            lw = int(W * 0.145)
            lh = round(logo.height * lw / logo.width)
            logo = logo.resize((lw, lh), Image.LANCZOS)
            pad_x, pad_y = round(lh * 0.62), round(lh * 0.44)
            pill_w, pill_h = lw + pad_x * 2, lh + pad_y * 2
            SS = 4  # draw at 4x, downscale -> smooth rounded corners
            big = Image.new("RGBA", (pill_w * SS, pill_h * SS), (0, 0, 0, 0))
            ImageDraw.Draw(big).rounded_rectangle(
                [0, 0, pill_w * SS - 1, pill_h * SS - 1],
                radius=(pill_h * SS) // 2, fill=(255, 255, 255, 255))
            pill = big.resize((pill_w, pill_h), Image.LANCZOS)
            # optical centering: the SVG's wordmark sits high (the mark's tail spans the
            # full height) — center on the TEXT block, measured from the alpha channel
            alpha = logo.split()[3]
            text_bbox = alpha.crop((int(lw * 0.30), 0, lw, lh)).getbbox()
            text_mid = (text_bbox[1] + text_bbox[3]) / 2 if text_bbox else lh / 2
            logo_y = max(2, min(pill_h - lh - 2, round(pill_h / 2 - text_mid)))
            pill.alpha_composite(logo, (pad_x, logo_y))
            im.alpha_composite(pill, (W - pill_w - int(W * 0.032), H - pill_h - int(H * 0.064)))

        buf = io.BytesIO()
        im.convert("RGB").save(buf, "PNG")
        atomic_write_bytes(Path(out_path), buf.getvalue())     # the old cover stays until this one is whole
        if new_bg is not None:
            atomic_write_bytes(saved_bg, new_bg)                # and the old background until then too
        return out_path
    except Exception as e:  # noqa: BLE001 — never fail the article over a cover
        print(f"[cover] error: {e}")
        return None
