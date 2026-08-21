"""Gemini vision: turn a camera frame into a list of identified items.

This is the multimodal half of Workflow 1.  The frontend grabs a still from the
Camera API, the browser posts it as a data URL, and this module asks Gemini what
is actually in the frame.

The model is asked for *structured* output rather than prose, because the answer
feeds a set-difference against the user's learned routine — we need names we can
compare, not a paragraph.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from typing import Any

from google.genai import types
from pydantic import BaseModel, Field

from utils.config import get_config
from utils.db import normalize_subject
from utils.errors import is_quota_error, retry_after_seconds
from utils.gemini import get_client
from utils.logger import get_logger

log = get_logger(__name__)

_DATA_URL = re.compile(r"^data:(?P<mime>image/[a-zA-Z0-9.+-]+);base64,(?P<payload>.+)$", re.DOTALL)

MAX_IMAGE_BYTES = 8 * 1024 * 1024


class _Item(BaseModel):
    name: str = Field(description="Short common name of the object, e.g. 'AirPods', 'laptop', 'keys'.")
    confidence: float = Field(description="0.0-1.0 confidence that this object is present.")
    detail: str = Field(description="Where in the scene it is, e.g. 'on the kitchen counter'.")


class _SceneReport(BaseModel):
    scene: str = Field(description="One short phrase naming the place, e.g. 'kitchen counter'.")
    items: list[_Item] = Field(description="Every portable personal item visible in the frame.")


_PROMPT = """You are the eyes of a personal context agent for someone with ADHD.

Look at this frame and list EVERY portable personal item you can see — the kinds
of things a person carries or misplaces: phone, keys, wallet, earbuds/AirPods and
their case, laptop, charger, cables, bag, backpack, water bottle, badge, glasses,
notebook, umbrella, medication, headphones.

Rules:
- Use short, common names a person would say out loud ("AirPods", not "wireless
  in-ear audio device").
- Only list what you can actually see. Do NOT guess at things that might be
  off-frame or inside a closed bag. A missed item is far better than a
  hallucinated one: this list decides whether we tell the user they are about to
  leave something behind.
- confidence reflects how sure you are the object is present and correctly named.
- Ignore furniture, walls, floors and other fixtures.
"""


@dataclass
class DetectedItem:
    name: str
    confidence: float
    detail: str = ""

    @property
    def key(self) -> str:
        return normalize_subject(self.name)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "key": self.key, "confidence": self.confidence, "detail": self.detail}


@dataclass
class VisionResult:
    items: list[DetectedItem] = field(default_factory=list)
    scene: str = ""
    available: bool = True
    note: str = ""

    @property
    def keys(self) -> set[str]:
        return {i.key for i in self.items}

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene": self.scene,
            "items": [i.to_dict() for i in self.items],
            "available": self.available,
            "note": self.note,
        }


class ImageDecodeError(ValueError):
    """Raised when the frontend sends something that is not a usable image."""


def decode_data_url(data_url: str) -> tuple[bytes, str]:
    """Split a `data:image/jpeg;base64,...` URL into raw bytes and mime type."""
    match = _DATA_URL.match((data_url or "").strip())
    if not match:
        raise ImageDecodeError("expected a data URL of the form 'data:image/jpeg;base64,...'")
    try:
        raw = base64.b64decode(match.group("payload"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImageDecodeError(f"image payload is not valid base64: {exc}") from exc
    if not raw:
        raise ImageDecodeError("image payload is empty")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ImageDecodeError(
            f"image is {len(raw) // 1024}KB, over the {MAX_IMAGE_BYTES // 1024}KB limit"
        )
    return raw, match.group("mime")


async def scan_frame(data_url: str) -> VisionResult:
    """Identify the personal items visible in one camera frame."""
    client = get_client()
    if client is None:
        return VisionResult(
            available=False,
            note="Vision is unavailable: no Gemini credentials configured.",
        )

    image_bytes, mime = decode_data_url(data_url)
    cfg = get_config()

    try:
        response = await client.aio.models.generate_content(
            model=cfg.vision_model,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime),
                types.Part.from_text(text=_PROMPT),
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_SceneReport,
                temperature=0.0,  # item identification should be repeatable
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a blind scan is better than a crash
        if is_quota_error(exc):
            wait = retry_after_seconds(exc)
            log.warning("vision quota exhausted", extra={"retry_after": wait})
            return VisionResult(
                available=False,
                note=f"Vision is rate-limited right now; try again in about {wait:.0f} seconds.",
            )
        log.exception("vision call failed")
        return VisionResult(available=False, note="The camera scan failed. Try again.")

    report: _SceneReport | None = response.parsed
    if report is None:
        log.warning("vision returned unparseable output", extra={"text": (response.text or "")[:400]})
        return VisionResult(available=True, note="The vision model returned no usable result.")

    items = [
        DetectedItem(name=i.name.strip(), confidence=max(0.0, min(1.0, i.confidence)), detail=i.detail)
        for i in report.items
        if i.name and i.name.strip()
    ]
    # Dedupe on normalised key, keeping the most confident sighting of each.
    best: dict[str, DetectedItem] = {}
    for item in sorted(items, key=lambda i: i.confidence, reverse=True):
        best.setdefault(item.key, item)

    log.info(
        "vision scan complete",
        extra={"scene": report.scene, "item_count": len(best), "bytes": len(image_bytes)},
    )
    return VisionResult(items=list(best.values()), scene=report.scene.strip())
