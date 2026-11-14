"""Covers kept on disk (``<data_dir>/covers``): a cover is fetched from Plex once per size and then served from here.

The name of a file is a hash of the server, the cover's path at Plex (which carries a time stamp that changes with
the picture) and the size; so a changed cover is a new file, and the ETag is that name. When the folder grows past
``NEXMIX_COVER_CACHE_MB`` the files used least recently go first.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import threading
import weakref
from collections.abc import Callable
from pathlib import Path

import anyio
import anyio.to_thread

from .. import clock
from ..config import get_settings

logger = logging.getLogger("nexmix.covers")

#: How many covers are fetched from Plex at once: a page full of covers must not become a hundred requests on Plex.
PLACES = 6
#: How long a request waits for one of those places, on the event loop (no thread is held meanwhile). Then it is
#: answered ``503`` with ``Retry-After``: the browser shows the grey square, and the rest of nexmix never waits.
WAIT_SECONDS = 1.0
#: After Plex failed to deliver a cover (busy, unreachable, too slow, an error), no cover asks it again for this long:
#: a Plex in trouble gets no more requests from every open page, and the log one line per outage, not one per cover.
QUIET_SECONDS = 30
#: What says that Plex itself is in trouble (and not that one cover is missing).
OUTAGE = frozenset({"plex_busy", "plex_unreachable", "plex_timeout", "plex_error", "plex_refused"})

_places = threading.BoundedSemaphore(PLACES)
#: The threads the fetches run in: their own few, never the server's shared pool (one per event loop; a test client
#: starts a loop of its own).
_limiters: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, anyio.CapacityLimiter] = weakref.WeakKeyDictionary()
_state_lock = threading.Lock()
#: Until when (``clock.monotonic``) Plex is left alone, and the code that said why; 0.0 while all is well.
_quiet_until = 0.0
_outage: str | None = None


class Unavailable(Exception):
    """No cover now: Plex is in trouble or all places are taken. ``retry`` is the number of seconds to wait."""

    def __init__(self, code: str, retry: int) -> None:
        super().__init__(code)
        self.code = code
        self.retry = retry


def quiet_for() -> int:
    """Seconds left in which no cover asks Plex; 0 when covers may ask."""
    left = _quiet_until - clock.monotonic()
    return int(left) + 1 if left > 0 else 0


def plex_failed(code: str) -> None:
    """Plex did not deliver a cover for a reason of its own: leave it alone for a while. One line in the log for the
    outage, however many covers it touches."""
    global _quiet_until, _outage
    with _state_lock:
        first = _outage is None
        _quiet_until = clock.monotonic() + QUIET_SECONDS
        _outage = code
    if first:
        logger.warning("Plex does not deliver covers (%s); covers wait %d s before asking it again", code,
                       QUIET_SECONDS)


def plex_delivered() -> None:
    global _quiet_until, _outage
    with _state_lock:
        was = _outage
        _quiet_until = 0.0
        _outage = None
    if was is not None:
        logger.info("Plex delivers covers again")


def reset() -> None:
    """Forget an outage (a new Plex connection, the tests)."""
    global _quiet_until, _outage
    with _state_lock:
        _quiet_until = 0.0
        _outage = None


def _limiter() -> anyio.CapacityLimiter:
    loop = asyncio.get_running_loop()
    found = _limiters.get(loop)
    if found is None:
        found = _limiters[loop] = anyio.CapacityLimiter(PLACES)
    return found


async def fetch[T](work: Callable[[], T]) -> T:
    """Run ``work`` (a request to Plex for a cover) in one of the few places for covers. Waiting for a place happens
    on the event loop and ends after ``WAIT_SECONDS``; while Plex is left alone nothing waits at all. Raises
    ``Unavailable`` instead of waiting longer."""
    left = quiet_for()
    if left:
        raise Unavailable(_outage or "plex_busy", left)
    waited = 0.0
    while not _places.acquire(blocking=False):
        if waited >= WAIT_SECONDS:
            raise Unavailable("plex_busy", 2)
        await anyio.sleep(0.05)
        waited += 0.05
    try:
        # Asked again: an outage may have begun while this one waited.
        left = quiet_for()
        if left:
            raise Unavailable(_outage or "plex_busy", left)
        return await anyio.to_thread.run_sync(work, limiter=_limiter())
    finally:
        _places.release()

_lock = threading.Lock()
_written = 0
#: How many writes between two looks at the size of the folder.
CHECK_EVERY = 100


def folder() -> Path:
    return get_settings().data_dir / "covers"


def name_of(server: str, thumb: str, size: int) -> str:
    return hashlib.sha256(f"{server}\n{thumb}\n{size}".encode()).hexdigest()[:32]


def path_of(name: str) -> Path:
    return folder() / name[:2] / f"{name}.img"


def read(name: str) -> tuple[bytes, str] | None:
    path = path_of(name)
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 2:
        return None
    try:
        os.utime(path)
    except OSError:
        pass
    # The first byte says which: JPEG starts with 0xFF, PNG with 0x89.
    return data, "image/png" if data[:1] == b"\x89" else "image/jpeg"


def write(name: str, data: bytes) -> None:
    global _written
    path = path_of(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{threading.get_ident()}.part")
        temporary.write_bytes(data)
        os.replace(temporary, path)
    except OSError:
        logger.warning("A cover could not be kept on disk")
        return
    with _lock:
        _written += 1
        due = _written % CHECK_EVERY == 0
    if due:
        prune()


def prune() -> None:
    """The least recently used files go until the folder is below four fifths of its limit."""
    limit = max(1, get_settings().cover_cache_mb) * 1_000_000
    files: list[tuple[float, int, Path]] = []
    total = 0
    for path in folder().glob("*/*.img"):
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append((stat.st_mtime, stat.st_size, path))
        total += stat.st_size
    if total <= limit:
        return
    for _moment, size, path in sorted(files):
        if total <= limit * 0.8:
            break
        try:
            path.unlink()
            total -= size
        except OSError:
            continue


def forget_all() -> None:
    shutil.rmtree(folder(), ignore_errors=True)
    reset()
