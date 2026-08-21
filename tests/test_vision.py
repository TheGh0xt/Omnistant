"""Camera frame decoding.

The frontend is the only caller, but it is untrusted input from a browser, so
the decoder is the boundary that has to hold.
"""

from __future__ import annotations

import base64

import pytest

from tools.vision import MAX_IMAGE_BYTES, DetectedItem, ImageDecodeError, VisionResult, decode_data_url


def data_url(payload: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64," + base64.b64encode(payload).decode()


def test_decodes_a_well_formed_frame():
    raw, mime = decode_data_url(data_url(b"\xff\xd8\xff\xe0 jpeg bytes"))
    assert raw == b"\xff\xd8\xff\xe0 jpeg bytes"
    assert mime == "image/jpeg"


@pytest.mark.parametrize("mime", ["image/png", "image/webp", "image/heic"])
def test_accepts_the_formats_browsers_produce(mime):
    _, parsed = decode_data_url(data_url(b"bytes", mime))
    assert parsed == mime


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not a data url",
        "https://example.com/photo.jpg",
        "data:text/plain;base64,aGVsbG8=",       # not an image
        "data:image/jpeg;base64,!!!not base64!!!",
        "data:image/jpeg;base64,",                # empty payload
    ],
)
def test_rejects_anything_that_is_not_a_usable_image(bad):
    with pytest.raises(ImageDecodeError):
        decode_data_url(bad)


def test_rejects_an_oversized_frame():
    with pytest.raises(ImageDecodeError, match="over the"):
        decode_data_url(data_url(b"x" * (MAX_IMAGE_BYTES + 1)))


def test_detected_items_normalise_to_comparable_keys():
    """Set-difference against the routine only works if both sides normalise."""
    assert DetectedItem(name="  My AirPods ", confidence=0.9).key == "airpods"
    assert DetectedItem(name="Water Bottle", confidence=0.9).key == "water bottle"


def test_vision_result_exposes_keys_for_comparison():
    result = VisionResult(items=[DetectedItem("Keys", 0.9), DetectedItem("Laptop", 0.8)])
    assert result.keys == {"keys", "laptop"}
