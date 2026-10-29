"""HTTP helpers shared by provider adapters."""

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

MAX_RETRY_AFTER = 3600.0


def retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    value = value.strip()
    # str.isdigit() accepts characters such as superscripts that float() rejects; ASCII only.
    if re.fullmatch(r"[0-9]{1,10}", value):
        seconds = float(value)
    else:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)
