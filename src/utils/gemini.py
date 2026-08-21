"""Shared google-genai client.

One client for the whole process, pointed at either AI Studio (API key) or
Vertex AI (ADC + project), depending on config.
"""

from __future__ import annotations

from functools import lru_cache

from google import genai

from .config import get_config
from .logger import get_logger

log = get_logger(__name__)


@lru_cache(maxsize=1)
def get_client() -> genai.Client | None:
    """Return a configured client, or None when no credentials are present."""
    cfg = get_config()
    if not cfg.genai_available:
        log.warning("no Gemini credentials — model calls will use deterministic stubs")
        return None
    if cfg.use_vertex:
        log.info("gemini via vertex-ai", extra={"project": cfg.gcp_project, "location": cfg.gcp_location})
        return genai.Client(vertexai=True, project=cfg.gcp_project, location=cfg.gcp_location)
    log.info("gemini via ai-studio")
    return genai.Client(api_key=cfg.api_key)
