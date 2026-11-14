"""Plex's stream of events (``/:/eventsource/notifications``): a playlist made, renamed or deleted in Plex shows there
at once, as a ``timeline`` entry of type 15 with state 0 (made), 5 (renamed, after 3) or 9 (deleted). Entries added,
taken out or moved do not show there at all.

The stream only says "look now" (``plex_sync.look_now``): what changed is read from Plex as at every look, never taken
from the event. A thread of its own keeps it open (Plex sends a ping about every ten seconds, so a stream silent for
45 seconds is opened anew); it is opened again after it ends or fails, waiting a little longer each time up to a minute,
and anew when Plex is connected to another server. The waits start over only after a stream that brought an event or
stayed open a while: one that ends after every ping (a proxy may do that) is not opened again every second. The token
goes in a header, as with every request to Plex.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

import httpx

from ..db import SessionLocal
from . import plex_link, plex_sync
from .plex import PlexError, PlexServer

logger = logging.getLogger("nexmix.plexevents")

PATH = "/:/eventsource/notifications"
PLAYLIST = 15
#: Made, renamed, deleted.
STATES = frozenset({0, 5, 9})
#: Seconds without a line (not even a ping) before the stream counts as dead.
SILENCE = 45.0
#: The waits before opening it again, after tries in a row that brought nothing; the last one stays.
WAITS = (1, 2, 5, 10, 30, 60)
#: A stream open this long counts as a good one, events or not.
STEADY = SILENCE
#: How long to wait while there is no Plex to listen to.
IDLE = 15.0


def _entries(data: Any) -> Iterable[dict[str, Any]]:
    found = data.get("TimelineEntry") if isinstance(data, dict) else None
    if isinstance(found, dict):
        yield found
    elif isinstance(found, list):
        yield from (one for one in found if isinstance(one, dict))


def tells_of_playlists(data: Any) -> bool:
    """Whether an event's data says a playlist was made, renamed or deleted."""
    for entry in _entries(data):
        try:
            kind, state = int(entry.get("type", -1)), int(entry.get("state", -1))
        except (TypeError, ValueError):
            continue
        if kind == PLAYLIST and state in STATES:
            return True
    return False


def read(lines: Iterable[str], on_playlists: Callable[[], None], stop: Callable[[], bool]) -> int:
    """The lines of the stream until it ends or ``stop`` says so; ``on_playlists`` for each event about playlists.
    Returns how many events came (pings left out: they only say the stream is open)."""
    event = ""
    count = 0
    for raw in lines:
        if stop():
            break
        line = raw.rstrip("\r")
        if line.startswith("event:"):
            event = line[6:].strip()
            count += 0 if event == "ping" else 1
        elif line.startswith("data:") and event == "timeline":
            try:
                data = json.loads(line[5:])
            except ValueError:
                continue
            if tells_of_playlists(data):
                on_playlists()
        elif not line:
            event = ""
    return count


def listen(server: PlexServer, stop: Callable[[], bool]) -> int:
    """One stream of one server, until it ends: the number of events it brought (pings left out)."""
    timeout = httpx.Timeout(SILENCE, connect=8.0)
    headers = server._headers({"Accept": "text/event-stream"})
    try:
        with server._client.stream("GET", f"{server.url}{PATH}", headers=headers, timeout=timeout) as answer:
            if answer.status_code in (401, 403):
                raise PlexError("plex_refused")
            if answer.status_code != 200:
                raise PlexError("plex_error")
            return read(answer.iter_lines(), plex_sync.look_now, stop)
    except httpx.TimeoutException as exc:
        raise PlexError("plex_timeout", 504) from exc
    except httpx.HTTPError as exc:
        raise PlexError("plex_unreachable") from exc


class Listener:
    """The thread that keeps the stream open."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._server: PlexServer | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="plex-events", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            server = self._server
        if server is not None:
            # Breaks off a stream that waits for its next line.
            try:
                server.close()
            except Exception:  # noqa: BLE001 - it is going anyway
                logger.debug("Plex event stream closed while in use")

    def _open(self) -> PlexServer | None:
        with SessionLocal() as db:
            connection = plex_sync._ready(db)
            if connection is None:
                return None
            return plex_link.server_of(db, connection, timeout=SILENCE)

    def _loop(self) -> None:
        tries = 0
        while not self._stop.is_set():
            started = plex_link.epoch()
            try:
                server = self._open()
            except plex_link.Unreadable:
                server = None
            if server is None:
                self._stop.wait(IDLE)
                continue
            with self._lock:
                self._server = server
            opened = time.monotonic()
            try:
                got = listen(server, lambda since=started: self._stop.is_set() or plex_link.epoch() != since)
                tries = next_tries(tries, got, time.monotonic() - opened)
            except PlexError as exc:
                tries += 1
                logger.debug("Plex event stream ended error=%s", exc.code)
            except Exception:  # noqa: BLE001 - the stream is a help, never a reason to stop
                tries += 1
                logger.debug("Plex event stream failed")
            finally:
                with self._lock:
                    self._server = None
                server.close()
            # Whatever happened while it was closed: the next look finds it.
            plex_sync.look_now()
            self._stop.wait(WAITS[min(tries, len(WAITS) - 1)])


def next_tries(tries: int, events: int, seconds: float) -> int:
    """The count of tries in a row that brought nothing, after a stream that ended: it starts over after a stream that
    brought an event or stayed open ``STEADY`` seconds, else it grows (and with it the wait before the next)."""
    return 0 if events or seconds >= STEADY else tries + 1


async def run_forever(stop: asyncio.Event) -> None:
    listener = Listener()
    listener.start()
    try:
        await stop.wait()
    finally:
        listener.stop()
