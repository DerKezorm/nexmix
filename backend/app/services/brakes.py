"""Brakes on actions, per person and minute: a stolen session or a stuck script must not keep the server busy. Far above
what a person does by hand.

Kept in memory, per process: a restart forgets them, which is fine for a brake. The blocks that add searches
and the like add their own kinds here.
"""

from __future__ import annotations

import threading
from collections import deque

from .. import clock
from ..errors import error

#: Per person and minute.
LIMITS = {
    #: Signing a device up for Web Push (its push service is looked up), a probe (it goes out to every device).
    "push": 20,
    "push_test": 10,
    #: Asking Plex which tracks sound alike (what is kept does not count).
    "sonic": 60,
}
#: The code and the sentence of the answer.
CODES = {
    "push": ("push_too_often", "Too many at once. Wait a minute."),
    "push_test": ("push_too_often", "Too many at once. Wait a minute."),
    "sonic": ("slow_down", "Too many requests. Wait a minute."),
}

_lock = threading.Lock()
_seen: dict[tuple[str, int], deque[float]] = {}


def take(kind: str, account_id: int) -> None:
    """One more of ``kind`` for this person, or ``429`` with ``Retry-After`` when the minute is full."""
    moment = clock.monotonic()
    with _lock:
        seen = _seen.setdefault((kind, account_id), deque())
        while seen and moment - seen[0] > 60:
            seen.popleft()
        if len(seen) >= LIMITS[kind]:
            code, text = CODES[kind]
            exc = error(code, text, 429)
            # Until the oldest of the minute drops out.
            exc.headers = {"Retry-After": str(max(1, int(60 - (moment - seen[0])) + 1))}
            raise exc
        seen.append(moment)


def forget() -> None:
    """For the tests."""
    with _lock:
        _seen.clear()
