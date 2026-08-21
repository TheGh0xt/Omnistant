"""Domain errors that the API layer maps onto HTTP status codes."""

from __future__ import annotations

import re

_QUOTA_MARKERS = ("RESOURCE_EXHAUSTED", "429", "quota", "rate limit")
_RETRY_HINT = re.compile(r"retry in ([0-9.]+)s", re.IGNORECASE)


class ModelQuotaError(RuntimeError):
    """The Gemini API refused the call because we are over quota.

    Worth its own type: it is the one failure mode that is entirely expected on
    a free-tier key, it is temporary, and the useful response is "wait N
    seconds", not a stack trace.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def is_quota_error(exc: BaseException) -> bool:
    """True when an exception (or any exception it wraps) is a quota refusal."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = f"{type(current).__name__} {current}"
        if any(marker.lower() in text.lower() for marker in _QUOTA_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


def retry_after_seconds(exc: BaseException, default: float = 30.0) -> float:
    """Pull the API's suggested wait out of the error text."""
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if match := _RETRY_HINT.search(str(current)):
            return round(float(match.group(1)), 1)
        current = current.__cause__ or current.__context__
    return default
