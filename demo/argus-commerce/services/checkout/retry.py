"""Retry policy for the checkout call path (ARGUS demo)."""

from __future__ import annotations

#: How many times a transient inventory failure is retried.
RETRY_ATTEMPTS = 7

#: Base delay for linear backoff, in seconds.
RETRY_BACKOFF_SECONDS = 0.4


def backoff_seconds(attempt: int) -> float:
    """Linear backoff: attempt 0 waits one base delay, attempt 1 two, and so on."""
    return RETRY_BACKOFF_SECONDS * (attempt + 1)
