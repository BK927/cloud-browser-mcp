import base64
import io

import pytest
from PIL import Image

from cloud_browser.image_privacy import mask_frames
from cloud_browser.models import BrowserError


def encoded_image():
    stream = io.BytesIO()
    Image.new("RGB", (100, 100), (255, 0, 0)).save(stream, format="JPEG")
    return base64.b64encode(stream.getvalue()).decode()


def test_masking_hides_frame_with_padding_and_returns_lossless_image():
    image, regions = mask_frames(encoded_image(), [
        {"x": 40, "y": 40, "width": 20, "height": 20, "mask_safe": True}
    ], {"width": 100, "height": 100})
    decoded = Image.open(io.BytesIO(base64.b64decode(image)))
    assert decoded.format == "PNG"
    assert decoded.getpixel((50, 50)) == (0, 0, 0)
    assert decoded.getpixel((25, 25)) == (0, 0, 0)
    assert decoded.getpixel((0, 0))[0] > 200
    assert regions == [{"x": 24, "y": 24, "width": 52, "height": 52}]


@pytest.mark.parametrize("region", [
    {"x": 0, "y": 0, "width": 1, "height": 1, "mask_safe": False},
    {"x": float("nan"), "y": 0, "width": 1, "height": 1, "mask_safe": True},
    {"x": 0, "y": 0, "width": -1, "height": 1, "mask_safe": True},
])
def test_unsafe_geometry_is_blocked(region):
    with pytest.raises(BrowserError) as exc:
        mask_frames(encoded_image(), [region], {"width": 100, "height": 100})
    assert exc.value.code == "SENSITIVE_SCREEN"


def test_geometry_mismatch_is_not_guessed():
    with pytest.raises(BrowserError):
        mask_frames(encoded_image(), [], {"width": 200, "height": 100})
