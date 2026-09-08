"""Conservative opt-in iframe masking. Images never touch the filesystem."""

import base64
import io
import math

from PIL import Image, ImageDraw

from .models import BrowserError


def mask_frames(encoded: str, regions: list[dict], viewport: dict) -> tuple[str, list[dict]]:
    if len(regions) > 100 or any(not r.get("mask_safe", False) for r in regions):
        raise BrowserError(
            "SENSITIVE_SCREEN", "Embedded content cannot be safely bounded for masking", "blocked"
        )
    image = Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGB")
    if image.size != (viewport["width"], viewport["height"]):
        raise BrowserError("SENSITIVE_SCREEN", "Capture geometry does not match viewport", "blocked")
    draw = ImageDraw.Draw(image)
    masked = []
    for region in regions:
        values = [region.get(k) for k in ("x", "y", "width", "height")]
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
            raise BrowserError("SENSITIVE_SCREEN", "Invalid embedded content bounds", "blocked")
        x, y, width, height = values
        if width < 0 or height < 0:
            raise BrowserError("SENSITIVE_SCREEN", "Invalid embedded content bounds", "blocked")
        # Include JPEG block boundaries; return PNG so no new lossy edge bleed occurs.
        left = max(0, math.floor(x) - 16)
        top = max(0, math.floor(y) - 16)
        right = min(image.width, math.ceil(x + width) + 16)
        bottom = min(image.height, math.ceil(y + height) + 16)
        if left >= right or top >= bottom:
            continue
        draw.rectangle((left, top, right - 1, bottom - 1), fill=(0, 0, 0))
        masked.append({"x": left, "y": top, "width": right - left, "height": bottom - top})
    output = io.BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii"), masked
