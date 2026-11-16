"""Music Assistant's events: what changes there reaches nexmix at once, on the server's one websocket
(``music_assistant.Connection``).

* ``media_item_updated`` of a playlist (``library://playlist/<n>``; entries added, taken out, a new order, also from
  Music Assistant's own interface): the copy of that playlist is due now. The event says nothing of what changed: the
  sync reads the playlist and compares it with the state both sides agreed on, so nexmix's own writes, which bring the
  same events, are found to change nothing.
* ``media_item_added`` and ``media_item_deleted`` of a playlist: a look at the list now (a playlist made or deleted
  there).
* ``music_sync_completed``: Music Assistant read its providers again (it says nothing of single tracks then): a full
  run of the catalogue, at most every ``SCANS_EVERY``; one that comes sooner is made up for once that time is over
  (``catch_up``), so that a server that says it again and again never keeps nexmix busy. Single tracks or albums
  changed outside such a run: a full run too, at most every ``TRACKS_EVERY``.
* The websocket broke (Music Assistant restarted, the network): whatever happened meanwhile is not told, so every
  playlist of that server is due and its list is looked at as soon as it answers again.

Every ready Music Assistant server has a thread that keeps its websocket open (opened again after a pause when it
breaks) and hands its events on. The tests hand them on themselves (``handle``).
"""

from __future__ import annotations

import asyncio
import logging
import queue
import re
import threading
from typing import Any

from sqlalchemy import update

from .. import clock
from ..db import SessionLocal
from ..models import SERVER_MA, ServerPlaylist
from . import ma_link, media, plex_link, sync
from .music_assistant import MaError

logger = logging.getLogger("nexmix.musicassistant")

PLAYLIST = re.compile(r"\Alibrary://playlist/([1-9][0-9]{0,15})\Z")
TRACK_OR_ALBUM = re.compile(r"\Alibrary://(?:track|album)/[1-9][0-9]{0,15}\Z")
#: How often single changes of tracks or albums ask for a run of the catalogue at the most (a favourite set in Music
#: Assistant is such a change).
TRACKS_EVERY = 300.0
#: The waits before the websocket is opened again after tries in a row that failed; the last one stays.
WAITS = (2.0, 5.0, 10.0, 15.0, 30.0)
#: How long to wait while there is no Music Assistant to listen to.
IDLE = 15.0

#: How often the end of a scan in Music Assistant asks for a run of the catalogue at the most.
SCANS_EVERY = 300.0

_tracks_asked: dict[int, float] = {}
#: When the end of a scan last asked for a run, by server, and the servers whose last scan still waits for its run.
_scan_asked: dict[int, float] = {}
_scan_owed: set[int] = set()


def forget() -> None:
    _tracks_asked.clear()
    _scan_asked.clear()
    _scan_owed.clear()


def _scan(server_id: int) -> bool:
    """A run of the catalogue for the end of a scan, unless one was asked for less than ``SCANS_EVERY`` ago: then it is
    owed and made up later (``catch_up``). True: asked for now."""
    now = clock.monotonic()
    if now - _scan_asked.get(server_id, -SCANS_EVERY) < SCANS_EVERY:
        _scan_owed.add(server_id)
        return False
    _scan_asked[server_id] = now
    _scan_owed.discard(server_id)
    _full(server_id, "changes")
    return True


def catch_up(server_id: int) -> str:
    """The run of the catalogue a scan that came too soon still waits for, once ``SCANS_EVERY`` is over (the listener
    asks every second, the tests themselves). Returns what was done."""
    if server_id not in _scan_owed:
        return ""
    return "catalogue" if _scan(server_id) else ""


def _due(server_id: int, key: str) -> int:
    """The copies of the playlist ``key`` on the server are due now. Returns how many."""
    with SessionLocal() as db:
        taken = db.execute(update(ServerPlaylist).where(
            ServerPlaylist.server_id == server_id, ServerPlaylist.ext_key == key,
            ServerPlaylist.playlist_id.is_not(None)).values(due_at=clock.now())).rowcount
        db.commit()
    return int(taken or 0)


def _all_due(server_id: int) -> None:
    with SessionLocal() as db:
        db.execute(update(ServerPlaylist).where(ServerPlaylist.server_id == server_id,
                                                ServerPlaylist.ext_key.is_not(None))
                   .values(due_at=clock.now()))
        db.commit()


def _full(server_id: int, reason: str) -> None:
    from . import catalog

    with SessionLocal() as db:
        catalog.request_full(db, reason, server_id)


def handle(server_id: int, message: dict[str, Any]) -> str:
    """What one event means for nexmix; returns what was done (for the log and the tests)."""
    kind = str(message.get("event") or "")
    target = str(message.get("object_id") or "")
    found = PLAYLIST.fullmatch(target)
    if kind == "media_item_updated" and found:
        _due(server_id, found.group(1))
        return "due"
    if kind in ("media_item_added", "media_item_deleted") and found:
        _due(server_id, found.group(1))
        sync.look_now(server_id)
        return "look"
    if kind == "music_sync_completed":
        return "catalogue" if _scan(server_id) else "later"
    if kind in ("media_item_added", "media_item_updated", "media_item_deleted") and TRACK_OR_ALBUM.fullmatch(target):
        now = clock.monotonic()
        if now - _tracks_asked.get(server_id, -TRACKS_EVERY) >= TRACKS_EVERY:
            _tracks_asked[server_id] = now
            _full(server_id, "changes")
            return "catalogue"
        return ""
    if kind == "_closed":
        # Whatever happened while it was closed is not told: everything is looked at again once it answers.
        _all_due(server_id)
        sync.look_now(server_id)
        return "again"
    return ""


def handle_pending(server_id: int) -> list[str]:
    """Every event that came on the server's websocket and was not handed on yet (the tests ask this)."""
    from . import music_assistant

    connection = music_assistant.current(server_id)
    done: list[str] = []
    if connection is None:
        return done
    while True:
        try:
            message = connection.events.get_nowait()
        except queue.Empty:
            return done
        done.append(handle(server_id, message))


class Listener:
    """The thread that keeps the websocket of one Music Assistant server open and hands its events on."""

    def __init__(self, server_id: int) -> None:
        self.server_id = server_id
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"ma-events-{self.server_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _open(self) -> Any:
        with SessionLocal() as db:
            row = sync._ready(db, self.server_id)
            if row is None or row.kind != SERVER_MA:
                return None
            return ma_link.connection(row)

    def _session(self) -> bool | None:
        """One time open, handed on until it breaks. None: nothing to listen to; True: it was open."""
        started = media.epoch(self.server_id)
        connection = self._open()
        if connection is None:
            return None
        # Opened anew: what happened while it was closed is looked at.
        sync.look_now(self.server_id)
        while not self._stop.is_set() and media.epoch(self.server_id) == started:
            try:
                catch_up(self.server_id)
            except Exception as exc:  # noqa: BLE001 - tried again at the next pass
                logger.warning("Music Assistant catalogue not asked for server=%s error=%s", self.server_id,
                               type(exc).__name__)
            try:
                message = connection.events.get(timeout=1.0)
            except queue.Empty:
                if not connection.alive:
                    break
                continue
            try:
                handle(self.server_id, message)
            except Exception as exc:  # noqa: BLE001 - one event that failed ends nothing
                logger.warning("Music Assistant event failed server=%s error=%s", self.server_id, type(exc).__name__)
            if message.get("event") == "_closed":
                break
        return True

    def _loop(self) -> None:
        tries = 0
        while not self._stop.is_set():
            try:
                good = self._session()
            except (plex_link.Unreadable, MaError):
                good = False
            except Exception as exc:  # noqa: BLE001 - the events are a help, never a reason to stop
                logger.warning("Music Assistant events failed server=%s error=%s", self.server_id,
                               type(exc).__name__)
                good = False
            if good is None:
                tries = 0
                self._stop.wait(IDLE)
                continue
            tries = 0 if good else tries + 1
            self._stop.wait(WAITS[min(tries, len(WAITS) - 1)])


def _wanted() -> set[int]:
    with SessionLocal() as db:
        return {row.id for row in media.ready(db) if row.kind == SERVER_MA}


async def run_forever(stop: asyncio.Event) -> None:
    """A listener for every ready Music Assistant server, looked over every ``IDLE`` seconds."""
    listeners: dict[int, Listener] = {}
    try:
        while not stop.is_set():
            try:
                wanted = await asyncio.to_thread(_wanted)
            except Exception:  # noqa: BLE001 - the events are a help, never a reason to stop
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
        from . import music_assistant

        await asyncio.to_thread(music_assistant.forget)
