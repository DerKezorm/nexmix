"""Jellyfin's websocket (``/socket``): a playlist made, renamed or deleted in Jellyfin shows there as
``LibraryChanged`` (``ItemsAdded``, ``ItemsUpdated``, ``ItemsRemoved``), gathered and about 30 seconds late. Entries
added, taken out or moved do not show there at all; those come with the look at the list every 15 seconds and its
``Etag``. Without the websocket nothing is missed, only later.

The websocket only says "look now" (``sync.look_now`` for that server): what changed is read from Jellyfin as at every
look, never taken from the message. Every ready Jellyfin server has its own thread that keeps it open. Jellyfin asks
for a ``KeepAlive`` at least every 30 seconds (``ForceKeepAlive``, with its time out in seconds); one silent for longer
than ``SILENCE`` counts as dead. It is opened again after it ends or fails, waiting a little longer each time up to a
minute (Jellyfin restarting closes it and answers 503 for a while), and anew when the server is let go of or connected
again. Whatever goes wrong in the thread (the database, a message nobody expected) ends only that try: it is opened
again after the same growing waits, never left closed until nexmix restarts. The token goes in the ``Authorization``
header, as with every request to Jellyfin, and never reaches the log (``logs.SILENT_LOGGERS``); the connection is made
to the checked address and follows no redirect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

from websockets.exceptions import WebSocketException
from websockets.sync.client import ClientConnection, connect

from ..db import SessionLocal
from ..models import SERVER_JELLYFIN
from . import jellyfin_link, media, outbound, plex_link, sync
from .jellyfin import authorization

logger = logging.getLogger("nexmix.jellyfinevents")

PATH = "/socket"
#: Seconds without a message (Jellyfin answers every ``KeepAlive``) before the socket counts as dead.
SILENCE = 75.0
#: How often nexmix says it is there, at the most (Jellyfin asks for it within its time out, 60 seconds measured).
KEEP_ALIVE = 30.0
#: The waits before opening it again, after tries in a row that brought nothing; the last one stays.
WAITS = (1, 2, 5, 10, 30, 60)
#: A socket open this long counts as a good one, messages or not.
STEADY = 60.0
#: How long to wait while there is no Jellyfin to listen to.
IDLE = 15.0
#: The lists of a ``LibraryChanged`` that may name a playlist made, renamed or deleted.
LISTS = ("ItemsAdded", "ItemsUpdated", "ItemsRemoved")


def tells_of_changes(message: Any) -> bool:
    """Whether a message is a ``LibraryChanged`` that names anything (a playlist among it, maybe)."""
    if not isinstance(message, dict) or message.get("MessageType") != "LibraryChanged":
        return False
    data = message.get("Data")
    return isinstance(data, dict) and any(isinstance(data.get(name), list) and data[name] for name in LISTS)


def keep_alive_every(message: Any) -> float | None:
    """For a ``ForceKeepAlive``: how often to say ``KeepAlive`` (half its time out, at most ``KEEP_ALIVE``)."""
    if not isinstance(message, dict) or message.get("MessageType") != "ForceKeepAlive":
        return None
    try:
        seconds = float(message.get("Data") or 60)
    except (TypeError, ValueError):
        seconds = 60.0
    return max(5.0, min(KEEP_ALIVE, seconds / 2))


def read(socket_: Any, on_changes: Callable[[], None], stop: Callable[[], bool],
         clock: Callable[[], float] = time.monotonic) -> int:
    """The messages of an open socket until it closes or ``stop`` says so: ``on_changes`` for each ``LibraryChanged``
    that names something, a ``KeepAlive`` as often as Jellyfin asks. Returns how many such changes came."""
    every = KEEP_ALIVE
    said = clock()
    heard = clock()
    count = 0
    while not stop():
        try:
            raw = socket_.recv(timeout=1.0)
        except TimeoutError:
            raw = None
        now = clock()
        if raw is not None:
            heard = now
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                message = None
            asked = keep_alive_every(message)
            if asked is not None:
                every = asked
            elif tells_of_changes(message):
                count += 1
                on_changes()
        if now - heard > SILENCE:
            break
        if now - said >= every:
            socket_.send(json.dumps({"MessageType": "KeepAlive"}))
            said = now
    return count


def socket_address(url: str) -> tuple[str, str, int, bool]:
    """The websocket's address for the server's checked address: ``(ws url, host, port, tls)``."""
    parts = urlsplit(url)
    secure = parts.scheme == "https"
    host = parts.hostname or ""
    port = parts.port or (443 if secure else 80)
    return f"{'wss' if secure else 'ws'}://{parts.netloc}{PATH}", host, port, secure


@contextmanager
def open_socket(url: str, token: str, device: str) -> Iterator[ClientConnection]:
    """The websocket of the server at ``url`` (a checked address), signed in by the header, open while the ``with``
    lasts. The connection is made here, so a redirect is never followed (websockets follows none with a socket given);
    no proxy from the environment."""
    address, host, port, secure = socket_address(url)
    raw = socket.create_connection((host, port), timeout=10.0)
    try:
        with connect(address, sock=raw, ssl=outbound.tls() if secure else None,
                     server_hostname=host if secure else None, proxy=None, open_timeout=10.0,
                     additional_headers={"Authorization": authorization(device, token)}, compression=None,
                     ping_interval=None, max_size=2**22, user_agent_header="nexmix", legacy=False) as opened:
            yield opened
    finally:
        raw.close()


class Listener:
    """The thread that keeps the websocket of one Jellyfin server open."""

    def __init__(self, server_id: int) -> None:
        self.server_id = server_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: ClientConnection | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"jellyfin-events-{self.server_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            found = self._socket
        if found is not None:
            try:
                found.close()
            except Exception:  # noqa: BLE001 - it is going anyway
                logger.debug("Jellyfin websocket closed while in use")

    def _where(self) -> tuple[str, str, str] | None:
        """The server's address, token and device id; None while there is no ready Jellyfin to listen to."""
        with SessionLocal() as db:
            row = sync._ready(db, self.server_id)
            if row is None or row.kind != SERVER_JELLYFIN:
                return None
            return row.url, jellyfin_link.token_of(row), jellyfin_link.device_id(db, row.machine_id)

    def _session(self) -> bool | None:
        """One time open, read until it ends. None: nothing to listen to; True: a good one (it brought changes, or
        lasted ``STEADY``); False: it ended at once."""
        started = media.epoch(self.server_id)
        found = self._where()
        if found is None:
            return None
        since = time.monotonic()
        got = 0
        with open_socket(*found) as opened:
            with self._lock:
                self._socket = opened
            try:
                got = read(opened, lambda: sync.look_now(self.server_id),
                           lambda: self._stop.is_set() or media.epoch(self.server_id) != started)
            except (OSError, WebSocketException):
                # Closed by Jellyfin (a restart) or the network: how long it lasted says whether it was a good one.
                logger.debug("Jellyfin websocket ended server=%s", self.server_id)
            finally:
                with self._lock:
                    self._socket = None
                # Whatever happened while it was closed: the next look finds it.
                sync.look_now(self.server_id)
        return bool(got) or time.monotonic() - since >= STEADY

    def _loop(self) -> None:
        tries = 0
        while not self._stop.is_set():
            try:
                good = self._session()
            except (plex_link.Unreadable, OSError, WebSocketException, TimeoutError):
                logger.debug("Jellyfin websocket ended server=%s", self.server_id)
                good = False
            except Exception as exc:  # noqa: BLE001 - the websocket is a help, never a reason to stop
                # The kind of fault only: its text may hold the address.
                logger.warning("Jellyfin websocket failed server=%s error=%s", self.server_id, type(exc).__name__)
                good = False
            if good is None:
                tries = 0
                self._stop.wait(IDLE)
                continue
            tries = 0 if good else tries + 1
            self._stop.wait(WAITS[min(tries, len(WAITS) - 1)])


def _wanted() -> set[int]:
    with SessionLocal() as db:
        return {row.id for row in media.ready(db) if row.kind == SERVER_JELLYFIN and row.user_id}


async def run_forever(stop: asyncio.Event) -> None:
    """A listener for every ready Jellyfin server, looked over every ``IDLE`` seconds."""
    listeners: dict[int, Listener] = {}
    try:
        while not stop.is_set():
            try:
                wanted = await asyncio.to_thread(_wanted)
            except Exception:  # noqa: BLE001 - the websockets are a help, never a reason to stop
                wanted = set(listeners)
            for ident in set(listeners) - wanted:
                listeners.pop(ident).stop()
            for ident in wanted - set(listeners):
                listeners[ident] = Listener(ident)
                listeners[ident].start()
            try:
                await asyncio.wait_for(stop.wait(), IDLE)
            except TimeoutError:
                continue
    finally:
        for listener in listeners.values():
            listener.stop()
