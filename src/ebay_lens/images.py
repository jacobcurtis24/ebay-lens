"""Image helpers: resolution upgrades, resizing, contact sheets, zoom crops."""

from __future__ import annotations

import io
import re

from PIL import Image as PILImage, ImageDraw, ImageFont, ImageOps

_SIZE_RE = re.compile(r"/s-l\d+\.")


def at_size(url: str, px: int) -> str:
    """eBay CDN URLs encode the long-edge size as s-l<px>; request a specific one."""
    return _SIZE_RE.sub(f"/s-l{px}.", url) if _SIZE_RE.search(url) else url


def open_image(data: bytes) -> PILImage.Image:
    im = PILImage.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)
    return im.convert("RGB")


def to_jpeg(im: PILImage.Image, max_px: int, quality: int = 85) -> bytes:
    im = im.copy()
    im.thumbnail((max_px, max_px), PILImage.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def crop_fraction(im: PILImage.Image, box: tuple[float, float, float, float]) -> PILImage.Image:
    x0, y0, x1, y1 = (max(0.0, min(1.0, float(v))) for v in box)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("box must be [x0, y0, x1, y1] fractions with x1>x0 and y1>y0")
    w, h = im.size
    return im.crop((int(x0 * w), int(y0 * h), int(x1 * w), int(y1 * h)))


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def contact_sheet(thumbs: list[tuple[int, bytes | None]], cell: int = 256, cols: int = 5) -> bytes:
    """Grid of numbered thumbnails so Claude can visually screen a whole result page in one image."""
    rows = max(1, -(-len(thumbs) // cols))
    sheet = PILImage.new("RGB", (cols * cell, rows * cell), "white")
    draw = ImageDraw.Draw(sheet)
    font = _font(max(14, cell // 9))
    for i, (label, data) in enumerate(thumbs):
        x, y = (i % cols) * cell, (i // cols) * cell
        if data:
            try:
                im = open_image(data)
                im.thumbnail((cell - 6, cell - 6), PILImage.LANCZOS)
                sheet.paste(im, (x + (cell - im.width) // 2, y + (cell - im.height) // 2))
            except Exception:
                draw.text((x + 10, y + cell // 2), "no image", fill="gray", font=font)
        else:
            draw.text((x + 10, y + cell // 2), "no image", fill="gray", font=font)
        tag = f"{label}"
        tw, th = draw.textbbox((0, 0), tag, font=font)[2:]
        draw.rectangle((x, y, x + tw + 10, y + th + 8), fill="black")
        draw.text((x + 5, y + 3), tag, fill="white", font=font)
        draw.rectangle((x, y, x + cell - 1, y + cell - 1), outline="#ccc")
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=80, optimize=True)
    return buf.getvalue()
