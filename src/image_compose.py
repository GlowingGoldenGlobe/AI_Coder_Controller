from __future__ import annotations

from pathlib import Path
from typing import Iterable, Tuple

from PIL import Image, ImageDraw, ImageFont


def _load_font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("arial.ttf", size)
    except Exception:
        return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
    words = text.split()
    lines = []
    cur = []
    for w in words:
        trial = (" ".join(cur + [w])).strip()
        w_px = draw.textlength(trial, font=font)
        if w_px <= max_width or not cur:
            cur.append(w)
        else:
            lines.append(" ".join(cur))
            cur = [w]
    if cur:
        lines.append(" ".join(cur))
    return "\n".join(lines)


def compose_card(
    out_path: Path,
    *,
    width: int = 1200,
    height: int = 628,
    title: str = "",
    subtitle: str = "",
    bullets: Iterable[str] | None = None,
    bg_color: str = "#0b132b",
    fg_color: str = "#ffffff",
    accent_color: str = "#5bc0be",
    overlay: Path | None = None,
) -> Path:
    """Compose a simple social-card style PNG with title, subtitle, bullets, and optional overlay image."""
    img = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    # Layout paddings
    pad = 48
    inner_w = width - 2 * pad
    y = pad

    # Title
    title_font = _load_font(44)
    title_text = _wrap_text(draw, title, title_font, inner_w)
    draw.text((pad, y), title_text, font=title_font, fill=fg_color)
    y += int(draw.multiline_textbbox((pad, y), title_text, font=title_font)[3] - y) + 24

    # Subtitle
    if subtitle:
        sub_font = _load_font(26)
        sub_text = _wrap_text(draw, subtitle, sub_font, inner_w)
        draw.text((pad, y), sub_text, font=sub_font, fill=accent_color)
        y += int(draw.multiline_textbbox((pad, y), sub_text, font=sub_font)[3] - y) + 24

    # Bullets
    if bullets:
        b_font = _load_font(24)
        for b in bullets:
            wrapped = _wrap_text(draw, f"• {b}", b_font, inner_w)
            draw.text((pad, y), wrapped, font=b_font, fill=fg_color)
            y += int(draw.multiline_textbbox((pad, y), wrapped, font=b_font)[3] - y) + 12

    # Optional overlay image (bottom-right)
    if overlay and Path(overlay).exists():
        ov = Image.open(overlay).convert("RGBA")
        # Fit overlay to bottom-right quadrant while keeping aspect
        max_ov = (width // 3, height // 3)
        ov.thumbnail(max_ov)
        pos = (width - pad - ov.width, height - pad - ov.height)
        img.paste(ov, pos, ov)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path
