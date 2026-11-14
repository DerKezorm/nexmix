"""Every playlist of nexmix is a playlist in the connected Plex account, and every change on either side reaches the
other one (Plexamp and Plex Web on one side, nexmix on the other).

**nexmix to Plex.** A change in nexmix marks the playlist due two seconds later (``mark``: more changes in those two
seconds go along). The sync then reads Plex's entries, merges them with nexmix's against the last state both agreed on
(``merge.py``), and writes only the difference: entries taken out by their ``playlistItemID``, entries added at the end
in parts of 300, then the moves (to the start without ``after``, else after an entry of the state just read). It reads
the entries again right away (they are right at once) and keeps them as the new common state. A deleted playlist goes
from Plex at once; brought back from the trash, it is made there anew.

**Plex to nexmix.** Every 15 seconds the list of Plex's playlists (``take_in``): a playlist whose ``updatedAt``,
``leafCount`` or name differs from what was seen is read and merged; one changed within two seconds before the list was
asked for (Plex's clock, from its ``Date``) is read once more at the next look, because a second change in the same
second would not show. The list tells of entries added and taken out, and of a new name, but not of entries moved (and
asking for the playlist itself would set its time anew, measured on Plex 1.43): so a playlist open in nexmix is read at
every look, and every other one within two minutes, ten at a look (with more than 80 the round takes longer; the Plex
card says how long, ``order_seconds``). A sync takes the playlist's name and time from the last look instead of
asking for the list again. ``updatedAt`` only says when to look;
whether anything changed says the comparison of the entries with the common state, so nexmix's own writes are never
taken for somebody else's. Plex's stream of events
(``plex_events.py``) makes a look at once when a playlist is made, renamed or deleted there. A playlist new in Plex
comes to nexmix and belongs to the operator; a smart one is only read (its entries as Plex lists them, the number from
``totalSize``). Deleted in Plex, the playlist goes to nexmix's trash. A track Plex has but the catalogue does not know
yet is shown with what Plex says of it, and a partial run of the catalogue is asked for.

**Nothing is lost.** A track Plex's library no longer has stays in nexmix (it cannot go into Plex), and out of the
common state. A track Plex did not take when it was added is offered again after a minute, five, fifteen and sixty,
never taken for one taken out in Plex; after the fifth try it stays in nexmix only, and its entry says so. Connected to
another server, an entry finds its track there by the track's guid, else by title, album and length, else by its
song, never by the key alone (another server gives the same number to another track); found nowhere, it stays in nexmix
only, and its entry says so.

**One at a time.** Every sync of a playlist and every look at the list is a job of the lane ``playlists``
(``jobs.py``): never two at once, and a stopped server goes on where it was. A write to Plex that a stop or a fault
broke off is found by the next sync (``writing``: the state it was to bring with the ids Plex had, what Plex was to
lose and to get) and finished: an entry still to be taken out is taken out, not read as one added in Plex; one still
to be added is added, not read as one taken out there; one Plex lacks of the rest was taken out there, and goes. A
track counts as added once Plex answered for it (``writing`` is narrowed after every part Plex took whole, and after
reading Plex again), or once Plex holds two tracks of the same request that show it arrived (the answer was lost:
``_sent``): taken out in Plex after that, it stays out. Where that cannot be told (Plex holds one track of the request
or none), it stays named as to be added, and adding wins. Plex's order counts for nothing until then (it may be half
of nexmix's). A large new order goes in parts of 200 moves, the lane free for other playlists in between.

**When Plex is away** the changes stay in nexmix, the playlist says it waits for Plex, and the sync is tried again after
5 seconds, then 10, 30, a minute and so on up to ten minutes; a look at the list that reaches Plex again tries at once.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

from sqlalchemy import bindparam, delete, exists, select, text, update
from sqlalchemy.orm import Session

from .. import clock
from ..db import SessionLocal
from ..models import OPERATOR, Account, Job, Playlist, PlaylistItem, PlexConnection, PlexPlaylist
from . import jobs, merge, plex_link
from . import plex_playlists as plexlists
from .events import hub, playlist_topic
from .plex import PlexError, PlexServer

logger = logging.getLogger("nexmix.plexsync")

LANE = "playlists"
ROUND = "plex_round"
POLL = "plex_poll"
KINDS = (ROUND, POLL)
#: A change in nexmix waits this long for more before it goes to Plex.
GATHER = timedelta(seconds=2)
POLL_EVERY = timedelta(seconds=15)
#: A smart playlist's entries change with the library, not its ``updatedAt``: read again this often.
SMART_EVERY = timedelta(minutes=15)
#: A playlist changed this close before Plex's clock at the look is looked at again the next time.
RECENT = 2
#: Moves in Plex do not show in the list: every playlist is read once it is this long unread, at the next look (so
#: within two minutes), at most this many at one look, the ones longest unread first; one open in nexmix is read at
#: every look. With more than 80 playlists a round takes longer (``order_seconds``): Plex is spared, and the Plex card
#: says how long.
CHECK_EVERY = timedelta(seconds=105)
CHECK_PER_LOOK = 10
#: The waits after failed tries in a row, in seconds; the last one stays.
BACKOFF = (5, 10, 30, 60, 120, 300, 600)
#: Codes that mean Plex was not reached: the playlist waits for Plex.
AWAY = frozenset({"plex_unreachable", "plex_timeout", "plex_busy"})
#: How often the clock of the sync looks for due playlists.
TICK_SECONDS = 1.0
#: How many times one sync merges again when somebody changed the playlist in nexmix while it merged.
MERGE_TRIES = 4
#: How many times the moves are planned again when Plex's order is not the wanted one after them.
MOVE_TRIES = 3
#: The most moves one sync makes: a large new order goes in parts, the lane free for others in between (the write
#: stays open, ``writing``, and the next part follows at once).
MOVE_PART = 200
#: The waits before a track Plex refused is offered again, in seconds; after the last try it stays in nexmix only, and
#: its entry says so (``plex_hint`` ``refused``).
REFUSED_WAITS = (60, 300, 900, 3600)
REFUSED_TRIES = len(REFUSED_WAITS) + 1
#: What an entry says of its place in Plex: Plex refused its track, or the track is not on this server (another
#: server was connected and nothing there matched it).
HINT_REFUSED, HINT_ELSEWHERE = "refused", "elsewhere"
#: Who added an entry that came through Plex.
PLEX_NAME = "Plex"

SYNCED, PENDING, WAITING, ERROR = "synced", "pending", "waiting", "error"


class Stale(Exception):
    """Plex was let go of (disconnected, another server or library) while the sync was under way."""


def _watch(started: int) -> Callable[[], bool]:
    def stop() -> bool:
        if plex_link.epoch() != started:
            raise Stale
        return jobs.stopping.is_set()

    return stop


# --- What nexmix tells the sync ---------------------------------------------------------------------------------------


def mirror_of(db: Session, playlist_id: int) -> PlexPlaylist | None:
    return db.scalars(select(PlexPlaylist).where(PlexPlaylist.playlist_id == playlist_id)
                      .execution_options(populate_existing=True)).first()


def mark(db: Session, playlist_id: int, *, at_once: bool = False) -> None:
    """The playlist changed in nexmix: due in two seconds (``at_once``: now), unless it is due sooner already, or it
    waits for Plex (the changes gather until the next try). In the caller's transaction."""
    now = clock.now()
    moment = now if at_once else now + GATHER
    row = mirror_of(db, playlist_id)
    if row is None:
        db.add(PlexPlaylist(playlist_id=playlist_id, machine="", state=PENDING, due_at=moment))
        return
    if row.smart:
        return
    if row.state == WAITING and not at_once and row.due_at is not None and row.due_at > now:
        return
    row.state = PENDING
    if row.due_at is None or row.due_at > moment:
        row.due_at = moment


def restored(db: Session, playlist_id: int) -> None:
    """Back from the trash: made in Plex anew (a smart one comes back as an ordinary playlist with its last entries)."""
    row = mirror_of(db, playlist_id)
    if row is not None:
        row.smart, row.smart_total, row.gone_in_plex, row.note = False, None, False, None
        # ``mark`` reads the row afresh: what was set here must be in the transaction by then.
        db.flush()
    mark(db, playlist_id, at_once=True)


def dropping(db: Session, playlist_id: int) -> None:
    """The playlist goes for good: a copy still in Plex is deleted there first (the row stays without the playlist
    until then, so the list of Plex never brings it back as a new one). A row that remembers copies on servers
    connected before stays too: those go once their server is back (``Round.run``)."""
    row = mirror_of(db, playlist_id)
    if row is None:
        return
    if row.rating_key is None:
        if not row.elsewhere:
            db.delete(row)
        return
    row.due_at = clock.now()
    row.state = PENDING


def is_smart(db: Session, playlist_id: int) -> bool:
    return bool(db.scalar(select(PlexPlaylist.smart).where(PlexPlaylist.playlist_id == playlist_id)))


def _connection(db: Session) -> PlexConnection | None:
    row = plex_link.current(db)
    return row if row is not None and row.section_key else None


def view(db: Session, playlist_id: int, row: PlexPlaylist | None = None) -> dict[str, Any]:
    """How the copy in Plex stands, for the interface: ``off`` without Plex, else ``synced``, ``pending``, ``waiting``
    (Plex not reached) or ``error`` with its code; whether it is a smart playlist and how many entries it has; the note
    of an order sorted in Plex and in nexmix at the same time."""
    row = mirror_of(db, playlist_id) if row is None else row
    state = row.state if row is not None else PENDING
    if _connection(db) is None:
        state = "off"
    note = row.note if row is not None and isinstance(row.note, dict) else None
    return {
        "state": state,
        "error": row.error if row is not None and state in (ERROR, WAITING) and row.error else None,
        "smart": bool(row is not None and row.smart),
        "total": row.smart_total if row is not None and row.smart else None,
        "synced_at": row.synced_at.isoformat() if row is not None and row.synced_at else None,
        "gone_in_plex": bool(row is not None and row.gone_in_plex),
        "note": {"at": note.get("at"), "count": len(note.get("order") or [])} if note else None,
    }


def _publish(db: Session, playlist_id: int | None, row: PlexPlaylist | None = None) -> None:
    if playlist_id is not None:
        hub.publish(playlist_topic(playlist_id), "plex", view(db, playlist_id, row))


# --- What the interface asks for --------------------------------------------------------------------------------------


def request(db: Session, playlist_id: int) -> dict[str, Any]:
    """Now ("sync now", "try again"): Plex is read at once and nexmix's changes are written."""
    row = mirror_of(db, playlist_id)
    if row is None:
        mark(db, playlist_id, at_once=True)
    else:
        row.due_at = clock.now()
        if row.state in (ERROR, WAITING):
            row.state, row.tries = PENDING, 0
        _offer_now(row)
    db.commit()
    _publish(db, playlist_id)
    return view(db, playlist_id)


def request_all(db: Session) -> None:
    """Every playlist now, and a look at the list of Plex."""
    global _polled_at
    db.execute(update(PlexPlaylist).values(due_at=clock.now()))
    db.execute(update(PlexPlaylist).where(PlexPlaylist.state.in_((ERROR, WAITING))).values(state=PENDING, tries=0))
    _polled_at = None
    db.commit()


def look_now() -> None:
    """A look at the list of Plex at the next tick (Plex's stream of events said a playlist was made, renamed or
    deleted there)."""
    global _polled_at
    _polled_at = None


def plex_order(db: Session, playlist_id: int) -> list[int] | None:
    """The order Plex had when both sides sorted at the same time (the note's undo), or None: there is no note."""
    row = mirror_of(db, playlist_id)
    note = row.note if row is not None and isinstance(row.note, dict) else None
    if note is None:
        return None
    return [int(track) for track in note.get("order") or [] if isinstance(track, int)]


def drop_note(db: Session, playlist_id: int) -> None:
    row = mirror_of(db, playlist_id)
    if row is not None and row.note is not None:
        row.note = None
        db.commit()
        _publish(db, playlist_id, row)


def order_seconds(count: int) -> int:
    """How long an order changed in Plex takes at most to reach nexmix, with ``count`` ordinary playlists: each is
    read again when it is ``CHECK_EVERY`` unread, at a look every ``POLL_EVERY``, ``CHECK_PER_LOOK`` at a look."""
    looks = math.ceil(max(count, 1) / CHECK_PER_LOOK)
    every = int(POLL_EVERY.total_seconds())
    return max(int(CHECK_EVERY.total_seconds()) + every, looks * every)


def overview(db: Session) -> dict[str, Any]:
    """The sync of the playlists, for the Plex card: how many stand how, the smart ones, the last look at Plex."""
    rows = db.execute(select(PlexPlaylist.state, PlexPlaylist.smart).join(
        Playlist, Playlist.id == PlexPlaylist.playlist_id).where(Playlist.deleted_at.is_(None))).all()
    counts = {SYNCED: 0, PENDING: 0, WAITING: 0, ERROR: 0}
    smart = 0
    for state, smart_one in rows:
        counts[state] = counts.get(state, 0) + 1
        smart += 1 if smart_one else 0
    at = _poll_result["at"]
    return {
        "active": _ready(db) is not None,
        "playlists": len(rows),
        **counts,
        "smart": smart,
        "order_minutes": math.ceil(order_seconds(len(rows) - smart) / 60),
        "polled_at": at.isoformat() if isinstance(at, datetime) else None,
        "poll_error": _poll_result["error"],
    }


# --- The clock of the sync --------------------------------------------------------------------------------------------

_polled_at: datetime | None = None
#: The last look at the list of Plex: when, and the code it failed with (None: it went through).
_poll_result: dict[str, Any] = {"at": None, "error": None}


def forget() -> None:
    global _polled_at
    _polled_at = None
    _poll_result.update({"at": None, "error": None})
    _listing.update({"at": None, "machine": "", "by_key": {}})
    _whole.update({"generation": None, "tracks": None})


def _ready(db: Session) -> PlexConnection | None:
    """The connection, when the sync can work: a library chosen and its catalogue there."""
    row = _connection(db)
    return row if row is not None and row.generation else None


def tick(db: Session, now: datetime | None = None) -> int:
    """Puts the due playlists in the lane, and the look at the list every 15 seconds. Returns how many jobs it
    queued."""
    global _polled_at
    moment = now or clock.now()
    # Rows of playlists deleted for good that have no copy in Plex (and none left on a server connected before):
    # nothing left to do.
    db.execute(delete(PlexPlaylist).where(PlexPlaylist.playlist_id.is_(None), PlexPlaylist.rating_key.is_(None),
                                          PlexPlaylist.elsewhere.is_(None)))
    connection = _ready(db)
    if connection is None:
        db.commit()
        return 0
    # Every playlist has its row (one made while Plex was away, one of an older nexmix).
    for playlist_id in db.scalars(select(Playlist.id).where(
            Playlist.deleted_at.is_(None), ~exists().where(PlexPlaylist.playlist_id == Playlist.id))).all():
        db.add(PlexPlaylist(playlist_id=playlist_id, machine="", state=PENDING, due_at=moment))
    # Another server since the last sync (or none yet): every playlist goes there.
    db.execute(update(PlexPlaylist).where(PlexPlaylist.machine != connection.machine_id, PlexPlaylist.due_at.is_(None))
               .values(due_at=moment))
    db.flush()
    due = list(db.scalars(select(PlexPlaylist.id).where(PlexPlaylist.due_at.is_not(None), PlexPlaylist.due_at <= moment)
                          .order_by(PlexPlaylist.due_at, PlexPlaylist.id)))
    for ident in due:
        jobs.enqueue(db, ROUND, ref=ident)
    if due:
        db.execute(update(PlexPlaylist).where(PlexPlaylist.id.in_(due)).values(due_at=None))
    queued = len(due)
    if _polled_at is None or not timedelta(0) <= moment - _polled_at < POLL_EVERY:
        jobs.enqueue(db, POLL)
        _polled_at = moment
        queued += 1
    db.commit()
    return queued


def _tick_once() -> None:
    # The session belongs to the thread that uses it: a thread that outlives a cancelled wait closes it itself.
    with SessionLocal() as db:
        tick(db)


async def run_forever(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(_tick_once)
        except Exception:
            logger.exception("Playlist sync clock failed")
        try:
            await asyncio.wait_for(stop.wait(), TICK_SECONDS)
        except TimeoutError:
            continue


# --- The jobs ---------------------------------------------------------------------------------------------------------


def _failed(db: Session, ident: int, code: str) -> None:
    db.rollback()
    row = db.get(PlexPlaylist, ident, populate_existing=True)
    if row is None:
        return
    row.tries = (row.tries or 0) + 1
    row.state = WAITING if code in AWAY else ERROR
    row.error = code[:40]
    later = clock.now() + timedelta(seconds=BACKOFF[min(row.tries, len(BACKOFF)) - 1])
    if row.due_at is None or row.due_at > later:
        row.due_at = later
    db.commit()
    # The code alone: never an address, a token or a name.
    logger.warning("Playlist sync failed playlist=%s error=%s try=%s", row.playlist_id, code, row.tries)
    _publish(db, row.playlist_id, row)


def run_round(db: Session, job: Job) -> None:
    ident = int(job.ref)
    row = db.get(PlexPlaylist, ident, populate_existing=True)
    if row is None:
        return
    connection = _ready(db)
    if connection is None:
        if _connection(db) is not None:
            # The catalogue is not there yet: soon again.
            row.due_at = clock.now() + timedelta(seconds=30)
            db.commit()
        return
    playlist_id = row.playlist_id
    stop = _watch(plex_link.epoch())
    try:
        with plex_link.server_of(db, connection, stop=stop) as server:
            Round(db, server, connection, ident).run()
    except Stale:
        db.rollback()
    except plex_link.Unreadable:
        _failed(db, ident, "token_unreadable")
    except PlexError as exc:
        if exc.code == "stopped":
            db.rollback()
            raise jobs.Interrupted from exc
        _failed(db, ident, exc.code)
    except Exception:
        logger.exception("Playlist sync failed playlist=%s", playlist_id)
        _failed(db, ident, "internal_error")


def settle(db: Session) -> None:
    """At the start of the lane: a playlist waiting for a sync that no job carries any more is due now."""
    carried = exists().where(Job.kind == ROUND, Job.ref == PlexPlaylist.id, Job.state.in_(("queued", "running")))
    db.execute(update(PlexPlaylist).where(PlexPlaylist.due_at.is_(None), PlexPlaylist.state != SYNCED, ~carried)
               .values(due_at=clock.now()))
    db.commit()


# --- One sync of one playlist -----------------------------------------------------------------------------------------


@dataclass
class Plan:
    """What one merge decided."""

    merged: merge.Merged
    title: str
    #: nexmix's order after the merge: the merged one, with the entries Plex cannot hold where they were.
    local_order: list[int]
    #: Tracks Plex cannot hold (gone from its library, refused by it, not on this server): they stay in nexmix and out
    #: of the common state.
    held: set[int] = field(default_factory=set)
    #: The id nexmix gave each track of the common state the merge started from (a removal still to be written keeps
    #: it, so that the track put in again here counts as added).
    base_local: dict[int, int | None] = field(default_factory=dict)
    #: Entries whose hint goes: Plex holds their track now.
    unhint: set[int] = field(default_factory=set)


@dataclass
class Snapshot:
    """nexmix's side as it was read for a merge."""

    version: int
    name: str
    entries: list[tuple[int, int]]
    #: The hints of the entries that have one (``HINT_REFUSED``, ``HINT_ELSEWHERE``), by track.
    hints: dict[int, str] = field(default_factory=dict)

    @property
    def tracks(self) -> list[int]:
        return [track for track, _entry in self.entries]


def _base_of(writing: dict[str, Any] | None, base: Any) -> list[merge.Kept]:
    """The common state a merge starts from. While a write is open (``writing``): the state it was meant to bring,
    with the ids Plex had for its entries when it began, and the entries it had still to take out of Plex with theirs
    (Plex still holding one under that id is no add of Plex: it is taken out now)."""
    stored = writing if writing is not None else (base if isinstance(base, dict) else {"items": []})
    found: list[merge.Kept] = []
    seen: set[int] = set()

    def take(one: Any, *, removing: bool) -> None:
        if not isinstance(one, list | tuple) or len(one) < 2 or _maybe(one[0]) is None:
            return
        track = int(one[0])
        if track in seen:
            return
        seen.add(track)
        third = _maybe(one[2]) if len(one) > 2 else None
        # ``[track, nexmix's id, Plex's id]``; an entry still to be taken out: ``[track, Plex's id, nexmix's id]``.
        found.append(merge.Kept(track, third, _maybe(one[1])) if removing else merge.Kept(track, _maybe(one[1]), third))

    for one in stored.get("items") or []:
        take(one, removing=False)
    for one in (writing or {}).get("remove") or []:
        take(one, removing=True)
    return found


def _unwritten(writing: dict[str, Any] | None) -> set[int]:
    """The tracks an open write had still to put into Plex. A write of an older form names none apart: every entry
    without an id of Plex counts as one."""
    if writing is None:
        return set()
    if "add" in writing:
        return {int(track) for track in writing.get("add") or [] if isinstance(track, int)}
    return {int(one[0]) for one in writing.get("items") or []
            if isinstance(one, list | tuple) and one and (len(one) < 3 or one[2] is None)}


def _sent(writing: dict[str, Any] | None, remote_items: dict[int, int | None]) -> set[int]:
    """The tracks of an open write that surely reached Plex although no answer said so (it was lost, or the server
    stopped before it could note it). The tracks to add go to Plex in requests of ``plexlists.CHUNK``, in their order,
    and Plex puts the tracks of one request at the end, each with the next ``playlistItemID``. Two tracks of one
    request that Plex holds, whose ids lie exactly as far apart as their places in the write, show that the request
    reached Plex: every track of it was sent. Of those, one Plex lacks was taken out there (or is gone from the
    library, which ``_plan`` asks Plex about); it is not still to be added. One track alone proves nothing (somebody may
    have added it in Plex meanwhile), and two that do not line up neither."""
    if writing is None:
        return set()
    order = [track for track in writing.get("add") or [] if isinstance(track, int)]
    held = [(place, remote_items[track]) for place, track in enumerate(order) if remote_items.get(track) is not None]
    reached: set[int] = set()
    for (first, first_id), (second, second_id) in pairwise(held):
        same = first // plexlists.CHUNK == second // plexlists.CHUNK
        if same and first_id is not None and second_id is not None and second_id - first_id == second - first:
            reached.add(first // plexlists.CHUNK)
    return {track for place, track in enumerate(order) if place // plexlists.CHUNK in reached}


class Round:
    """One sync of one playlist, its steps as methods. Reads go in transactions of their own that end before a write
    begins: a reader's snapshot never turns into a writer (SQLite would refuse it when somebody wrote in between)."""

    def __init__(self, db: Session, server: PlexServer, connection: PlexConnection, ident: int) -> None:
        self.db = db
        self.server = server
        self.machine = connection.machine_id
        self.ident = ident
        self.unknown = False
        self.added: set[int] = set()
        self.entry_ids: dict[int, int] = {}
        #: The playlist as Plex answered when this sync made it (it is not in the last look's list yet).
        self.made: plexlists.Listed | None = None

    @property
    def row(self) -> PlexPlaylist:
        row = self.db.get(PlexPlaylist, self.ident)
        if row is None:
            raise Stale
        return row

    def fresh_row(self) -> PlexPlaylist:
        row = self.db.get(PlexPlaylist, self.ident, populate_existing=True)
        if row is None:
            raise Stale
        return row

    def run(self) -> None:
        db = self.db
        row = self.fresh_row()
        if row.machine != self.machine:
            # Its key is one of another server (or of none yet): its copy there is remembered, and the copy it left
            # here before is taken up again; else it is made anew here. Its tracks are found on this server.
            before = row.machine
            self._switch(row, before)
            db.commit()
            if row.playlist_id is not None:
                self._settle_tracks(row.playlist_id, switched=bool(before))
        playlist = db.get(Playlist, row.playlist_id, populate_existing=True) if row.playlist_id is not None else None
        if playlist is None:
            if row.rating_key is not None:
                plexlists.delete(self.server, row.rating_key)
                logger.info("Playlist deleted in Plex")
            if row.elsewhere:
                # Copies on other servers: the row stays (without a playlist) so that they are taken out there once
                # that server is connected again, never taken in as new playlists.
                self._forget_plex(row)
                row.state, row.due_at, row.error, row.tries = SYNCED, None, "", 0
            else:
                db.delete(row)
            db.commit()
            return
        if playlist.deleted_at is not None:
            if row.rating_key is not None:
                self._rescue(playlist.id)
                plexlists.delete(self.server, self.row.rating_key or 0)
                logger.info("Playlist deleted in Plex playlist=%s", playlist.id)
            row = self.fresh_row()
            self._forget_plex(row)
            self._done(None)
            return
        if row.smart:
            self._smart(playlist.id)
            return
        self._sync(playlist.id)

    def _switch(self, row: PlexPlaylist, before: str) -> None:
        """Connected to another server than the one the row's key belongs to. The copy there is remembered with the
        state both sides last agreed on (``elsewhere``); a copy this server holds from before is taken up again with
        its own common state, when Plex still lists it under its key (and its ``guid``) and no other row has it."""
        elsewhere = dict(row.elsewhere) if isinstance(row.elsewhere, dict) else {}
        if before and row.rating_key is not None:
            elsewhere[before] = {"key": row.rating_key, "guid": row.guid,
                                 "base": row.base if isinstance(row.base, dict) else None}
        back = elsewhere.pop(self.machine, None)
        self._forget_plex(row)
        row.creating_at, row.creating_title = None, ""
        row.machine = self.machine
        row.elsewhere = elsewhere or None
        key = _maybe(back.get("key")) if isinstance(back, dict) else None
        if key is None:
            return
        taken = self.db.scalar(select(PlexPlaylist.id).where(PlexPlaylist.rating_key == key,
                                                             PlexPlaylist.id != row.id))
        guid = str(back.get("guid") or "")
        there = next((one for one in plexlists.listing(self.server) if one.rating_key == key), None)
        if taken is not None or there is None or there.smart or (guid and there.guid and there.guid != guid):
            return
        row.rating_key, row.guid = key, there.guid or guid
        base = back.get("base")
        row.base = base if isinstance(base, dict) else {"title": there.title, "items": []}
        logger.info("Playlist copy on this server taken up again playlist=%s", row.playlist_id)

    @staticmethod
    def _forget_plex(row: PlexPlaylist) -> None:
        row.rating_key, row.guid, row.base, row.writing, row.seen, row.note = None, "", None, None, None, None
        row.refused = None

    def _done(self, seen: plexlists.Listed | None, *, recent: bool = False) -> None:
        row = self.row
        # A change in nexmix while this sync ran is due already: the playlist is not done with until it went too.
        due = self.db.scalar(select(PlexPlaylist.due_at).where(PlexPlaylist.id == self.ident))
        row.state = PENDING if due is not None else SYNCED
        row.error, row.tries = "", 0
        row.synced_at = clock.now()
        if seen is not None:
            row.seen = {"updated_at": seen.updated_at, "leaf_count": seen.leaf_count, "title": seen.title}
        if recent:
            row.recheck = True
        self.db.commit()
        _publish(self.db, row.playlist_id, row)

    def _recent(self, seen: plexlists.Listed) -> bool:
        now = self.server.clock
        return now is not None and seen.updated_at >= now - RECENT

    def _info(self, key: int) -> plexlists.Listed:
        """The playlist as the list of Plex gives it: from the last look while that is fresh (a sync asks for the whole
        list only when the look did not have it), else asked."""
        if self.made is not None and self.made.rating_key == key:
            return self.made
        found = _listed(self.machine, key)
        return found if found is not None else plexlists.meta(self.server, key)

    def _rescue(self, playlist_id: int) -> None:
        """Before the copy in Plex goes with a playlist moved to the trash: what was put in there since the last sync
        comes along into the trash (at the end), so that bringing it back brings everything back."""
        from . import playlists

        db = self.db
        key = self.row.rating_key
        assert key is not None
        try:
            remote = plexlists.items(self.server, key)
        except PlexError as exc:
            if exc.code == "plex_not_found":
                return
            raise
        stored = self.row.base if isinstance(self.row.base, dict) else {}
        known = {int(one[0]) for one in stored.get("items") or [] if isinstance(one, list | tuple) and one}
        there = {item.track_id for item in playlists.live_items(db, playlist_id)}
        new = [entry for entry in remote if entry.track not in known and entry.track not in there]
        db.rollback()
        if not new:
            return
        db.execute(update(Playlist).where(Playlist.id == playlist_id)
                   .values(version=Playlist.version + 1, updated_at=clock.now()))
        before = playlists.live_items(db, playlist_id)
        items = self._new_items(playlist_id, {entry.track: entry for entry in new}, [entry.track for entry in new])
        playlists.reorder(db, [*before, *items])
        db.commit()
        logger.info("Entries added in Plex kept with the playlist in the trash playlist=%s count=%s", playlist_id,
                    len(items))

    # --- Another server

    def _settle_tracks(self, playlist_id: int, *, switched: bool) -> None:
        """The entries' tracks as this server knows them. A key alone names a track on one server only: another server
        may give the same number to another song. Checked are all entries after a change of server, else the ones put
        in before Plex was connected as it is now (they may come from another server). Each keeps its track when the
        catalogue has it as the same track (the same ``guid``, or the same title and album); else it is found by its
        ``guid``, then by its title, album and length, then by its song; what is found nowhere stays in nexmix only,
        and its entry says so. What Plex refused before is offered to this server anew."""
        from . import playlists

        db = self.db
        db.rollback()
        # The write lock first: nothing changes the entries while they are settled.
        db.execute(update(Playlist).where(Playlist.id == playlist_id)
                   .values(version=Playlist.version + 1, updated_at=clock.now()))
        items = playlists.live_items(db, playlist_id)
        since = db.scalar(select(PlexConnection.connected_at).where(PlexConnection.machine_id == self.machine))
        checked = [item for item in items if switched or (since is not None and item.added_at < since)]
        if not checked and not any(item.plex_hint == HINT_REFUSED for item in items):
            db.rollback()
            return
        catalogue = _Catalogue(db, playlists._live_generation(db))
        catalogue.load([item.track_id for item in checked])
        kept: set[int] = set()
        moving: dict[int, _Track] = {}
        hints: dict[int, str] = {}
        for item in checked:
            here = catalogue.by_id.get(item.track_id)
            if here is not None and _same_track(item, here) and item.track_id not in kept:
                kept.add(item.track_id)
        checked_ids = {item.id for item in checked}
        used = kept | {item.track_id for item in items if item.id not in checked_ids}
        for item in checked:
            if item.track_id in kept:
                continue
            found = catalogue.find(item, used)
            if found is not None:
                used.add(found.id)
                moving[item.id] = found
            else:
                hints[item.id] = HINT_ELSEWHERE
        by_id = {item.id: item for item in items}
        for item in items:
            # Found here, or kept: nothing to say; refused by the other server: offered to this one.
            hints.setdefault(item.id, "" if item.plex_hint == HINT_REFUSED or item.id in moving
                             or item.track_id in kept else item.plex_hint)
        hints = {ident: hint for ident, hint in hints.items() if by_id[ident].plex_hint != hint}
        if not moving and not hints:
            db.rollback()
            return
        if moving:
            # Two steps: a track another entry still holds is let go of first (one track once per playlist).
            for ident in moving:
                by_id[ident].track_id = -ident
            db.flush()
            for ident, found in moving.items():
                item = by_id[ident]
                item.track_id, item.album_id, item.guid = found.id, found.album_id, found.guid
        for ident, hint in hints.items():
            by_id[ident].plex_hint = hint
        db.commit()
        logger.info("Playlist entries settled on this server playlist=%s found=%s not_here=%s", playlist_id,
                    len(moving), sum(1 for hint in hints.values() if hint == HINT_ELSEWHERE))
        publish_items(db, playlist_id, [], changed=[by_id[ident] for ident in {*moving, *hints}])

    # --- Deleted in Plex

    def _gone(self, playlist_id: int) -> None:
        db = self.db
        db.rollback()
        now = clock.now()
        taken = db.execute(update(Playlist).where(Playlist.id == playlist_id, Playlist.deleted_at.is_(None))
                           .values(version=Playlist.version + 1, deleted_at=now, updated_at=now)).rowcount
        row = self.fresh_row()
        self._forget_plex(row)
        # A smart one keeps the entries nexmix has of it: those come back if it is brought back.
        row.gone_in_plex, row.smart_total = True, None
        self._done(None)
        if taken:
            version = db.scalar(select(Playlist.version).where(Playlist.id == playlist_id)) or 0
            logger.info("Playlist deleted in Plex, moved to the trash playlist=%s", playlist_id)
            hub.publish(playlist_topic(playlist_id), "playlist",
                        {"version": version, "op": "deleted", "by": None, "plex": True})
            # As in the trash from nexmix: its invitation for guests ends, brought back it lets nobody in again.
            from . import guests

            guests.end_with_playlist(db, playlist_id)

    # --- Smart playlists: read only

    def _smart(self, playlist_id: int) -> None:
        from . import playlists

        db = self.db
        key = self.row.rating_key
        assert key is not None
        try:
            info = self._info(key)
            entries, total = plexlists.smart_items(self.server, key)
        except PlexError as exc:
            if exc.code == "plex_not_found":
                self._gone(playlist_id)
                return
            raise
        current = [item.track_id for item in playlists.live_items(db, playlist_id)]
        name = db.scalar(select(Playlist.name).where(Playlist.id == playlist_id)) or ""
        wanted = [entry.track for entry in entries]
        title = info.title or name
        db.rollback()
        if current != wanted or title != name:
            db.execute(update(Playlist).where(Playlist.id == playlist_id)
                       .values(version=Playlist.version + 1, updated_at=clock.now(), name=title))
            db.execute(delete(PlaylistItem).where(PlaylistItem.playlist_id == playlist_id))
            new = self._new_items(playlist_id, {entry.track: entry for entry in entries}, wanted)
            playlists.reorder(db, new)
            self.row.smart_total = total
            db.commit()
            publish_items(db, playlist_id, new)
        self.row.smart_total = total
        self._done(info)

    # --- Ordinary playlists

    def _make(self, playlist_id: int) -> None:
        """Made in Plex, empty; or, when the answer of an earlier try was lost (a fault or a stop between Plex making it
        and nexmix keeping its key), the one made then: empty, not smart, of the name it was made with, and of a key
        and ``guid`` no playlist here has."""
        db = self.db
        row = self.fresh_row()
        name = db.scalar(select(Playlist.name).where(Playlist.id == playlist_id)) or PLEX_NAME
        adopted: plexlists.Listed | None = None
        if row.creating_at is not None:
            asked = (row.creating_title or name)[:200]
            known = db.execute(select(PlexPlaylist.rating_key, PlexPlaylist.guid)
                               .where(PlexPlaylist.rating_key.is_not(None))).all()
            keys = {key for key, _guid in known}
            guids = {guid for _key, guid in known if guid}
            adopted = next((one for one in plexlists.listing(self.server)
                            if not one.smart and one.title == asked and one.leaf_count == 0
                            and one.rating_key not in keys and one.guid not in guids), None)
        if adopted is None:
            row.creating_at = clock.now()
            row.creating_title = name[:200]
            db.commit()
            adopted = plexlists.create(self.server, self.machine, name)
            logger.info("Playlist made in Plex playlist=%s", playlist_id)
        else:
            logger.info("Playlist made in Plex earlier taken playlist=%s", playlist_id)
        row = self.row
        row.rating_key, row.guid, row.creating_at, row.creating_title = adopted.rating_key, adopted.guid, None, ""
        row.base = {"title": adopted.title or name, "items": []}
        row.writing = None
        self.made = adopted
        db.commit()

    def _read(self, playlist_id: int) -> tuple[plexlists.Listed, list[plexlists.Item]] | None:
        key = self.row.rating_key
        assert key is not None
        try:
            info = self._info(key)
            entries = plexlists.items(self.server, key)
        except PlexError as exc:
            if exc.code == "plex_not_found":
                self._gone(playlist_id)
                return None
            raise
        return info, entries

    def _snapshot(self, playlist_id: int) -> Snapshot | None:
        from . import playlists

        playlist = self.db.get(Playlist, playlist_id, populate_existing=True)
        if playlist is None or playlist.deleted_at is not None:
            return None
        items = playlists.live_items(self.db, playlist_id)
        return Snapshot(version=playlist.version, name=playlist.name,
                        entries=[(item.track_id, item.id) for item in items],
                        hints={item.track_id: item.plex_hint for item in items if item.plex_hint})

    def _sync(self, playlist_id: int) -> None:
        db = self.db
        if self.row.rating_key is None:
            self._make(playlist_id)
        found = self._read(playlist_id)
        if found is None:
            return
        info, remote = found
        if info.smart:
            # Made a smart playlist in Plex meanwhile: read only from now on.
            self.row.smart = True
            db.commit()
            self._smart(playlist_id)
            return
        plan: Plan | None = None
        for _attempt in range(MERGE_TRIES):
            db.rollback()
            snapshot = self._snapshot(playlist_id)
            if snapshot is None:
                # Deleted meanwhile: the next sync takes it out of Plex.
                db.rollback()
                mark(db, playlist_id, at_once=True)
                db.commit()
                return
            plan = self._plan(snapshot, info, remote)
            # The reading ends before the writing begins.
            db.rollback()
            if self._apply(playlist_id, snapshot, info, remote, plan):
                break
            plan = None
        if plan is None:
            # nexmix changed it again and again while it was merged: in a moment.
            db.rollback()
            mark(db, playlist_id)
            db.commit()
            return
        fresh, wrote, more = self._write(info, remote, plan)
        if more:
            self._go_on(plan, remote, fresh)
            return
        self._keep(plan, fresh, info, wrote)

    def _held_back(self, snapshot: Snapshot, remote_tracks: set[int]) -> set[int]:
        """Tracks of nexmix not offered to Plex this time: refused by Plex and not due to be tried again, refused for
        good, or not on this server. One Plex holds after all is not held back."""
        row = self.row
        now = clock.now()
        held = {track for track, hint in snapshot.hints.items() if track not in remote_tracks}
        for key, record in (row.refused if isinstance(row.refused, dict) else {}).items():
            track = _maybe(key)
            if track is None or track in remote_tracks or not isinstance(record, dict):
                continue
            if int(record.get("tries") or 0) >= REFUSED_TRIES or _after(record.get("next"), now):
                held.add(track)
        return held

    def _plan(self, snapshot: Snapshot, info: plexlists.Listed, remote: list[plexlists.Item]) -> Plan:
        from . import playlists

        row = self.row
        writing = row.writing if isinstance(row.writing, dict) else None
        base = _base_of(writing, row.base)
        local_tracks = snapshot.tracks
        remote_tracks = {entry.track for entry in remote}
        unwritten = _unwritten(writing) - _sent(writing, {entry.track: entry.item for entry in remote})
        known = playlists.catalogue_of(self.db, local_tracks)
        # Tracks nexmix holds that Plex cannot: unknown to the catalogue and not in Plex's playlist, and the ones held
        # back (refused by Plex, not on this server).
        held = {track for track in local_tracks if track not in known and track not in remote_tracks}
        held |= self._held_back(snapshot, remote_tracks)
        base_ids = {kept.track for kept in base}
        # Taken out in Plex, or gone from Plex's library? What is gone there stays in nexmix. (A track an open write
        # had still to add is neither: Plex never had it.)
        lost = [track for track in local_tracks
                if track in base_ids and track not in remote_tracks and track not in held and track not in unwritten]
        if lost:
            alive = plexlists.existing(self.server, lost)
            held |= {track for track in lost if track not in alive}
        merged = merge.merge(
            [kept for kept in base if kept.track not in held],
            [entry for entry in snapshot.entries if entry[0] not in held],
            [(entry.track, entry.item) for entry in remote],
            remote_order_counts=writing is None, remote_unwritten=unwritten,
        )
        stored = writing if writing is not None else (row.base if isinstance(row.base, dict) else {})
        base_title = stored.get("title") if isinstance(stored.get("title"), str) else None
        remote_title = info.title
        if writing is not None and base_title is not None and remote_title == writing.get("from_title"):
            # Plex has the name it had when the open write began: the new one may not have gone yet.
            remote_title = base_title
        title = merge.name(base_title, snapshot.name, remote_title)
        local_order = merge.place(merged.order, [track for track in local_tracks if track in held], local_tracks)
        return Plan(merged=merged, title=title, local_order=local_order, held=held,
                    base_local={kept.track: kept.local for kept in base},
                    unhint={track for track in snapshot.hints if track in remote_tracks})

    def _new_items(self, playlist_id: int, by_track: dict[int, plexlists.Item],
                   tracks: Sequence[int]) -> list[PlaylistItem]:
        """Entries for tracks that came through Plex: what the catalogue says of them, else what Plex said."""
        from . import playlists

        known = playlists.catalogue_of(self.db, tracks)
        guids = _guids_of(self.db, tracks)
        now = clock.now()
        new: list[PlaylistItem] = []
        for track in tracks:
            entry = by_track[track]
            found = known.get(track)
            item = PlaylistItem(
                playlist_id=playlist_id, track_id=track, position=0, added_by_user=None, added_by_name=PLEX_NAME,
                added_at=now, from_plex=True, title=(found.title if found else entry.title)[:500],
                artist=(found.artist if found else entry.artist)[:500],
                album=(found.album if found else entry.album)[:500],
                album_id=(found.album_id if found else entry.album_id) or 0,
                year=found.year if found else entry.year, duration=found.duration if found else entry.duration,
                guid=(guids.get(track) or entry.guid)[:200],
            )
            self.db.add(item)
            new.append(item)
        if new:
            self.db.flush()
        if any(track not in known for track in tracks):
            self.unknown = True
        return new

    def _apply(self, playlist_id: int, snapshot: Snapshot, info: plexlists.Listed, remote: list[plexlists.Item],
               plan: Plan) -> bool:
        """nexmix's side of the merge, in one transaction that holds only when nobody changed the playlist since it was
        read (else False: merged again). What is to be written goes along (``writing``): the order, what Plex is to
        lose and to get, and the ids Plex had, so that a write that breaks off is finished by the next sync instead of
        being read as changes made in Plex."""
        from . import playlists

        db = self.db
        merged = plan.merged
        changes = bool(merged.local_adds or merged.local_removes or plan.local_order != snapshot.tracks
                       or plan.title != snapshot.name or plan.unhint)
        needs_write = merged.order != [entry.track for entry in remote] or plan.title != info.title
        now = clock.now()
        new: list[PlaylistItem] = []
        going: list[PlaylistItem] = []
        changed: list[PlaylistItem] = []
        ids = dict(snapshot.entries)
        if changes:
            taken = db.execute(update(Playlist).where(Playlist.id == playlist_id, Playlist.version == snapshot.version,
                                                      Playlist.deleted_at.is_(None))
                               .values(version=Playlist.version + 1, updated_at=now)).rowcount
            if taken != 1:
                db.rollback()
                return False
            local = playlists.live_items(db, playlist_id)
            by_track = {item.track_id: item for item in local}
            going = [by_track[track] for track in merged.local_removes if track in by_track]
            for item in going:
                item.removed_at = now
            for track in plan.unhint:
                if track in by_track and by_track[track].plex_hint:
                    by_track[track].plex_hint = ""
                    changed.append(by_track[track])
            if merged.local_adds:
                new = self._new_items(playlist_id, {entry.track: entry for entry in remote}, merged.local_adds)
            live = {item.track_id: item for item in [*local, *new] if item.removed_at is None}
            db.flush()
            playlists.reorder(db, [live[track] for track in plan.local_order if track in live])
            if plan.title != snapshot.name:
                db.execute(update(Playlist).where(Playlist.id == playlist_id).values(name=plan.title))
            ids = {track: item.id for track, item in live.items()}
        row = self.row
        if merged.conflict:
            row.note = {"order": merged.conflict, "at": now.isoformat()}
        row.writing = self._open(plan, ids, remote, info.title) if needs_write else None
        # A refusal is forgotten once Plex holds the track or nexmix no longer does.
        remote_tracks = {entry.track for entry in remote}
        keep = set(plan.local_order) - remote_tracks
        records = row.refused if isinstance(row.refused, dict) else {}
        if any(_maybe(key) not in keep for key in records):
            row.refused = {key: value for key, value in records.items() if _maybe(key) in keep} or None
        self.entry_ids = ids
        db.commit()
        if changes:
            publish_items(db, playlist_id, new, removed=[item.id for item in going], changed=changed)
            if self.unknown:
                from . import catalog

                catalog.request_partial(db, "playlist")
        if merged.conflict:
            _publish(db, playlist_id, self.row)
        return True

    def _open(self, plan: Plan, ids: dict[int, int], read: list[plexlists.Item], from_title: str, *,
              before: dict[int, int | None] | None = None) -> dict[str, Any]:
        """The write about to begin, as it is kept until it is done: the name and order meant, each entry with the id
        Plex has for it (none: Plex is to get it, ``add``), and the entries Plex is to lose with their ids
        (``remove``). ``before``: Plex's ids when the sync read it; a track Plex had then and lacks now, though this
        sync did not add it, was taken out there meanwhile and keeps its old id."""
        order = plan.merged.order
        wanted = set(order)
        there = {entry.track: entry.item for entry in read}
        earlier = before or {}

        def plex_id(track: int) -> int | None:
            if track in there:
                return there[track]
            return earlier.get(track) if track not in self.added else None

        return {
            "title": plan.title,
            "from_title": from_title,
            "items": [[track, ids.get(track), plex_id(track)] for track in order],
            "remove": [[entry.track, entry.item, plan.base_local.get(entry.track)] for entry in read
                       if entry.track not in wanted and entry.item is not None],
            "add": [track for track in order if plex_id(track) is None],
            "order": list(order),
        }

    def _write(self, info: plexlists.Listed, remote: list[plexlists.Item],
               plan: Plan) -> tuple[list[plexlists.Item], bool, bool]:
        """Plex's side: the name, out what goes, in what comes (at the end), then the moves. Read again after each
        part, and the moves planned again from what was read until the order is the wanted one. At most ``MOVE_PART``
        moves: the third value says more are to come (the write stays open)."""
        server, key = self.server, self.row.rating_key
        assert key is not None
        target = plan.merged.order
        wanted = set(target)
        wrote = False
        if plan.title != info.title:
            plexlists.rename(server, key, plan.title)
            wrote = True
        for entry in remote:
            if entry.track not in wanted and entry.item is not None:
                plexlists.remove(server, key, entry.item)
                wrote = True
        present = {entry.track for entry in remote if entry.track in wanted}
        adding = [track for track in target if track not in present]
        self.added = set(adding)
        if adding:
            took = plexlists.add(server, self.machine, key, adding, answered=self._part_added)
            if took < len(adding):
                logger.info("Plex took fewer tracks than asked asked=%s took=%s", len(adding), took)
            wrote = True
        fresh = plexlists.items(server, key) if wrote else list(remote)
        if adding:
            # What Plex holds now is no longer to be added, whatever happens next: Plex lacking it from here on means
            # it was taken out there.
            self._narrow({entry.track: entry.item for entry in fresh if entry.track in self.added})
        moved = 0
        for _attempt in range(MOVE_TRIES):
            steps = merge.moves([(entry.item, entry.track) for entry in fresh if entry.item is not None], target)
            if not steps:
                break
            wrote = True
            more = moved + len(steps) > MOVE_PART
            for item, after in steps[:MOVE_PART - moved]:
                if not plexlists.move(server, key, item, after):
                    break
                moved += 1
            fresh = plexlists.items(server, key)
            if more:
                return fresh, wrote, bool(merge.moves(
                    [(entry.item, entry.track) for entry in fresh if entry.item is not None], target))
        return fresh, wrote, False

    def _part_added(self, part: list[int], took: int | None) -> None:
        """Plex answered a part of the tracks to add. When it says it took all of them, they are in Plex: the open
        write no longer names them as to be added (a write broken off after this, and the track taken out in Plex
        before the next sync, is a track taken out there, not one still to add). Took it fewer, which ones is not
        known until Plex is read again (``_narrow``)."""
        if took is not None and took >= len(part):
            self._narrow(dict.fromkeys(part))

    def _narrow(self, there: dict[int, int | None]) -> None:
        """The open write without ``there`` among the tracks still to add; where Plex's id is known, the entry keeps
        it. In a transaction of its own, committed at once."""
        db = self.db
        db.rollback()
        row = self.fresh_row()
        writing = row.writing if isinstance(row.writing, dict) else None
        if writing is None or "add" not in writing:
            db.rollback()
            return
        items = []
        for one in writing.get("items") or []:
            if (isinstance(one, list | tuple) and len(one) >= 3 and one[2] is None and _maybe(one[0]) is not None
                    and there.get(int(one[0])) is not None):
                one = [one[0], one[1], there[int(one[0])]]
            items.append(one)
        add = [track for track in writing.get("add") or [] if not (isinstance(track, int) and track in there)]
        row.writing = {**writing, "items": items, "add": add}
        db.commit()

    def _go_on(self, plan: Plan, remote: list[plexlists.Item], fresh: list[plexlists.Item]) -> None:
        """A part of a large new order is written: the write stays open, as it now stands, and the next part follows
        at once, after whatever else waits in the lane."""
        db = self.db
        db.rollback()
        row = self.fresh_row()
        row.writing = self._open(plan, self.entry_ids, fresh, plan.title,
                                 before={entry.track: entry.item for entry in remote})
        row.state = PENDING
        row.due_at = clock.now()
        db.commit()
        _remember_title(self.machine, row.rating_key, plan.title)
        logger.info("Playlist order written in part playlist=%s", row.playlist_id)
        _publish(db, row.playlist_id, row)

    def _keep(self, plan: Plan, fresh: list[plexlists.Item], info: plexlists.Listed, wrote: bool) -> None:
        """The new common state: what was meant, with Plex's ids as just read. A track just added that Plex did not
        take stays out of it and is offered again later, less and less often (``REFUSED_WAITS``), then no more; one Plex
        lost since it was read stays in (it was taken out there)."""
        db = self.db
        db.rollback()
        there = {entry.track: entry.item for entry in fresh}
        refused = [track for track in self.added if track not in there]
        alive = set(refused)
        if refused:
            alive = plexlists.existing(self.server, refused)
            if alive != set(refused):
                # Plex no longer has them: the catalogue learns it now, then they stay in nexmix only.
                from . import catalog

                catalog.request_partial(db, "playlist")
        row = self.fresh_row()
        items = [[track, self.entry_ids.get(track), there.get(track)] for track in plan.merged.order
                 if track in there or track not in self.added]
        row.base = {"title": plan.title, "items": items}
        row.writing = None
        given_up = self._refused(row, [track for track in refused if track in alive])
        key = row.rating_key
        if wrote:
            # Plex sets its own time a moment later: the next look reads the entries once more and finds them equal.
            self._done(None)
        else:
            self._done(info, recent=self._recent(info))
        _remember_title(self.machine, key, plan.title)
        if given_up and row.playlist_id is not None:
            self._hint(row.playlist_id, given_up)

    def _refused(self, row: PlexPlaylist, tracks: list[int]) -> list[int]:
        """Counts the tries of the tracks Plex refused just now and says when to offer each again; returns the ones
        tried for the last time."""
        if not tracks:
            return []
        now = clock.now()
        records = dict(row.refused) if isinstance(row.refused, dict) else {}
        last: list[int] = []
        for track in tracks:
            record = records.get(str(track))
            tries = (int(record.get("tries") or 0) if isinstance(record, dict) else 0) + 1
            if tries >= REFUSED_TRIES:
                records[str(track)] = {"tries": tries}
                last.append(track)
            else:
                wait = timedelta(seconds=REFUSED_WAITS[tries - 1])
                records[str(track)] = {"tries": tries, "next": (now + wait).isoformat()}
        row.refused = records
        logger.info("Plex refused tracks playlist=%s count=%s", row.playlist_id, len(tracks))
        return last

    def _hint(self, playlist_id: int, tracks: list[int]) -> None:
        """The entries of tracks Plex refused for good say so."""
        db = self.db
        db.rollback()
        taken = db.execute(update(PlaylistItem).where(
            PlaylistItem.playlist_id == playlist_id, PlaylistItem.track_id.in_(tracks),
            PlaylistItem.removed_at.is_(None)).values(plex_hint=HINT_REFUSED)).rowcount
        if not taken:
            db.rollback()
            return
        db.execute(update(Playlist).where(Playlist.id == playlist_id)
                   .values(version=Playlist.version + 1, updated_at=clock.now()))
        db.commit()
        logger.info("Plex refused tracks for good playlist=%s count=%s", playlist_id, taken)
        changed = [item for item in db.scalars(select(PlaylistItem).where(
            PlaylistItem.playlist_id == playlist_id, PlaylistItem.track_id.in_(tracks),
            PlaylistItem.removed_at.is_(None)))]
        publish_items(db, playlist_id, [], changed=changed)


def _maybe(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _after(value: Any, now: datetime) -> bool:
    """Whether a time kept as text lies after ``now``."""
    if not isinstance(value, str):
        return False
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return False
    return moment > now


# --- The last look, for the syncs that follow it ----------------------------------------------------------------------

#: The list of Plex as the last look had it: a sync takes its playlist's name, kind and time from there instead of
#: asking for the whole list once more. A name nexmix wrote since goes in at once.
_listing: dict[str, Any] = {"at": None, "machine": "", "by_key": {}}
#: How long the last look counts as fresh.
LISTING_FRESH = POLL_EVERY * 2


def _listed(machine: str, key: int) -> plexlists.Listed | None:
    at = _listing["at"]
    if not isinstance(at, datetime) or _listing["machine"] != machine:
        return None
    if not timedelta(0) <= clock.now() - at <= LISTING_FRESH:
        return None
    found = _listing["by_key"].get(key)
    return found if isinstance(found, plexlists.Listed) else None


def _remember(machine: str, listed: list[plexlists.Listed]) -> None:
    _listing.update({"at": clock.now(), "machine": machine, "by_key": {one.rating_key: one for one in listed}})


def _remember_title(machine: str, key: int | None, title: str) -> None:
    found = _listing["by_key"].get(key) if key is not None and _listing["machine"] == machine else None
    if isinstance(found, plexlists.Listed) and found.title != title:
        _listing["by_key"][key] = replace(found, title=title)


# --- The catalogue, for entries to be found on another server ---------------------------------------------------------


@dataclass(frozen=True)
class _Track:
    id: int
    guid: str
    title: str
    album: str
    album_id: int
    duration: int
    song_key: str
    shown: bool


_TRACK_ROWS = (
    "SELECT t.id, t.guid, t.title, t.duration, t.song_key, t.shown, t.album_id, a.title AS album FROM tracks t "
    "LEFT JOIN albums a ON a.generation = t.generation AND a.id = t.album_id WHERE t.generation = :g"
)
#: The tracks of one generation, kept while it is the live one (another server: every playlist asks the same).
_whole: dict[str, Any] = {"generation": None, "tracks": None}


def _track(row: Any) -> _Track:
    return _Track(id=int(row.id), guid=str(row.guid or ""), title=str(row.title or ""), album=str(row.album or ""),
                  album_id=int(row.album_id or 0), duration=int(row.duration or 0), song_key=str(row.song_key or ""),
                  shown=bool(row.shown))


class _Catalogue:
    """The live catalogue as far as one settling of entries needs it."""

    def __init__(self, db: Session, generation: int) -> None:
        self.db = db
        self.generation = generation
        self.by_id: dict[int, _Track] = {}
        self._all: list[_Track] | None = None

    def load(self, ids: Sequence[int]) -> None:
        query = text(_TRACK_ROWS + " AND t.id IN :ids").bindparams(bindparam("ids", expanding=True))
        wanted = sorted(set(ids))
        for start in range(0, len(wanted), 500):
            for row in self.db.execute(query, {"g": self.generation, "ids": wanted[start:start + 500]}):
                self.by_id[int(row.id)] = _track(row)

    def _every(self) -> list[_Track]:
        if self._all is None:
            if _whole["generation"] != self.generation or _whole["tracks"] is None:
                rows = self.db.execute(text(_TRACK_ROWS), {"g": self.generation})
                _whole.update({"generation": self.generation, "tracks": [_track(row) for row in rows]})
            self._all = list(_whole["tracks"])
        return self._all

    def find(self, item: PlaylistItem, used: set[int]) -> _Track | None:
        """The entry's track on this server: by its ``guid``, else the same title, album and length, else a version
        of the same song (the one shown first); None: not there."""
        from . import songs

        every = [track for track in self._every() if track.id not in used]
        guid = str(item.guid or "")
        # A guid of Plex's own agents names the same recording on every server; a ``local://`` one only on its own.
        if guid and not guid.startswith("local://"):
            same = [track for track in every if track.guid == guid]
            if same:
                return same[0]
        title, album = songs.norm(item.title), songs.norm(item.album)
        exact = [track for track in every if songs.norm(track.title) == title and songs.norm(track.album) == album
                 and abs(track.duration - int(item.duration or 0)) <= 3000]
        if len(exact) == 1:
            return exact[0]
        first = songs.first_name(str(item.artist or ""))
        keys = {songs.song_key(0, item.title, item.artist), songs.song_key(0, item.title, first)} - {"#0"}
        versions = sorted((track for track in every if track.song_key in keys), key=lambda track: not track.shown)
        return versions[0] if versions else None


def _same_track(item: PlaylistItem, here: _Track) -> bool:
    """Whether the catalogue's track of the entry's key is the track the entry was made with."""
    from . import songs

    if item.guid and here.guid:
        return str(item.guid) == here.guid
    return songs.norm(item.title) == songs.norm(here.title) and songs.norm(item.album) == songs.norm(here.album)


def _guids_of(db: Session, tracks: Sequence[int]) -> dict[int, str]:
    from . import playlists

    generation = playlists._live_generation(db)
    if not generation or not tracks:
        return {}
    found = _Catalogue(db, generation)
    found.load(list(tracks))
    return {track: row.guid for track, row in found.by_id.items() if row.guid}


def publish_items(db: Session, playlist_id: int, new: list[PlaylistItem], *, removed: list[int] | None = None,
                  changed: Sequence[PlaylistItem] = ()) -> None:
    """Tells the browsers that have the playlist open what the sync changed: one event with the new version, the name
    and the whole order, as a change in nexmix does (``changed``: entries that stay, with something new to show). Its
    reading ends with it."""
    from . import playlists

    try:
        playlist = db.get(Playlist, playlist_id, populate_existing=True)
        if playlist is None:
            return
        ordered = playlists.live_items(db, playlist_id)
        known = playlists.catalogue_of(db, (item.track_id for item in ordered))
        people = playlists.people_of(db, (item.added_by_user for item in ordered))
        told = {item.id for item in new}
        by_id = {item.id: item for item in ordered}
        new = [*new, *(by_id[item.id] for item in changed if item.id in by_id and item.id not in told)]
        found = playlists.summary(db, ordered, known, people, moods=True)
        total = db.scalar(select(PlexPlaylist.smart_total).where(PlexPlaylist.playlist_id == playlist_id,
                                                                PlexPlaylist.smart.is_(True)))
        if total is not None:
            # A smart playlist shows its first entries; the number is all of them.
            found["count"] = total
        payload = {
            "version": playlist.version,
            "op": "sync",
            "by": None,
            "entries": [playlists.item_view(item, known.get(item.track_id), people) for item in new],
            "order": [item.id for item in ordered],
            "summary": found,
            "removed": removed or [],
            "name": playlist.name,
        }
        hub.publish(playlist_topic(playlist_id), "items", payload)
    finally:
        db.rollback()


# --- The look at the list of Plex -------------------------------------------------------------------------------------


def _owner(db: Session) -> int | None:
    return db.scalar(select(Account.id).where(Account.role == OPERATOR, Account.blocked_at.is_(None))
                     .order_by(Account.id).limit(1))


def run_poll(db: Session, job: Job) -> None:
    connection = _ready(db)
    if connection is None:
        return
    stop = _watch(plex_link.epoch())
    try:
        with plex_link.server_of(db, connection, stop=stop) as server:
            listed = plexlists.listing(server)
            now_plex = server.clock
    except Stale:
        return
    except plex_link.Unreadable:
        _poll_result.update({"at": clock.now(), "error": "token_unreadable"})
        return
    except PlexError as exc:
        if exc.code == "stopped":
            raise jobs.Interrupted from exc
        _poll_result.update({"at": clock.now(), "error": exc.code})
        return
    _poll_result.update({"at": clock.now(), "error": None})
    _remember(connection.machine_id, listed)
    take_in(db, connection, listed, now_plex)
    # The looks leave no trail in the list of jobs (one every 15 seconds).
    db.execute(delete(Job).where(Job.kind == POLL, Job.state == "done", Job.id != job.id))
    db.commit()


def take_in(db: Session, connection: PlexConnection, listed: list[plexlists.Listed], now_plex: int | None) -> None:
    """What the list of Plex says: new playlists come to nexmix, changed ones are due, missing ones are due (their sync
    finds out whether they are gone), and every one waiting for Plex is tried now that Plex answered."""
    now = clock.now()
    rows = list(db.scalars(select(PlexPlaylist).execution_options(populate_existing=True)))
    mine = [row for row in rows if row.rating_key is not None and row.machine == connection.machine_id]
    by_key = {row.rating_key: row for row in mine}
    # Keys of another server: never taken for this one's. Nor the copies playlists left on this server before another
    # one was connected: their sync takes them up again.
    foreign = {row.rating_key for row in rows if row.rating_key is not None} - set(by_key)
    for row in rows:
        left = row.elsewhere.get(connection.machine_id) if isinstance(row.elsewhere, dict) else None
        if isinstance(left, dict) and _maybe(left.get("key")) is not None:
            foreign.add(int(left["key"]))
    # Playlists nexmix asked Plex to make whose answer it has not got: the one Plex made is theirs (it is empty and
    # has the name it was made with), never a new one of Plex.
    making: set[str] = set()
    for row in rows:
        if row.rating_key is None and row.creating_at is not None and row.playlist_id is not None:
            name = row.creating_title or db.scalar(select(Playlist.name).where(Playlist.id == row.playlist_id))
            if name:
                making.add(name[:200])
    owner = _owner(db)
    seen_keys: set[int] = set()
    stale: list[PlexPlaylist] = []
    watched = {int(topic.split(":", 1)[1]) for topic in hub.topics() if topic.startswith("playlist:")}

    def due(row: PlexPlaylist) -> None:
        if row.due_at is None or row.due_at > now:
            row.due_at = now

    for one in listed:
        seen_keys.add(one.rating_key)
        row = by_key.get(one.rating_key)
        if row is None:
            orphan = one.title in making and not one.smart and not one.leaf_count
            if orphan or owner is None or one.rating_key in foreign:
                continue
            _take_new(db, connection, one, owner, now)
            continue
        if row.playlist_id is None:
            continue
        last = row.seen if isinstance(row.seen, dict) else None
        if row.smart:
            if (last is None or last.get("updated_at") != one.updated_at or last.get("title") != one.title
                    or row.synced_at is None or now - row.synced_at >= SMART_EVERY):
                due(row)
            continue
        changed = last is None or any(last.get(name) != value for name, value in (
            ("updated_at", one.updated_at), ("leaf_count", one.leaf_count), ("title", one.title)))
        if changed or row.recheck or row.playlist_id in watched:
            due(row)
        elif row.synced_at is None or now - row.synced_at >= CHECK_EVERY:
            stale.append(row)
        row.recheck = now_plex is not None and one.updated_at >= now_plex - RECENT
    # What the list does not tell (entries moved): the ones longest unread, a few at a look.
    stale.sort(key=lambda row: row.synced_at or datetime.min.replace(tzinfo=now.tzinfo))
    for row in stale[:CHECK_PER_LOOK]:
        due(row)
    for key, row in by_key.items():
        if key not in seen_keys and row.playlist_id is not None:
            due(row)
    for row in rows:
        if row.state == WAITING or (row.playlist_id is not None and _offer_again(row, now)):
            due(row)
    db.commit()


def _offer_now(row: PlexPlaylist) -> None:
    """Tracks Plex refused are offered again now (somebody asked), however long they had still to wait; one refused
    for good stays so."""
    records = row.refused if isinstance(row.refused, dict) else {}
    if any(isinstance(record, dict) and "next" in record for record in records.values()):
        now = clock.now().isoformat()
        row.refused = {key: ({**record, "next": now} if isinstance(record, dict) and "next" in record else record)
                       for key, record in records.items()}


def _offer_again(row: PlexPlaylist, now: datetime) -> bool:
    """Whether a track Plex refused is due to be offered again."""
    records = row.refused if isinstance(row.refused, dict) else {}
    return any(isinstance(record, dict) and isinstance(record.get("next"), str) and not _after(record["next"], now)
               for record in records.values())


def _take_new(db: Session, connection: PlexConnection, one: plexlists.Listed, owner: int, now: datetime) -> None:
    """A playlist made in Plex: in nexmix, the operator's, read at once (a smart one only read, ever)."""
    playlist = Playlist(owner_id=owner, name=(one.title or PLEX_NAME)[:200], description="", version=1, created_at=now,
                        updated_at=now)
    db.add(playlist)
    db.flush()
    db.add(PlexPlaylist(playlist_id=playlist.id, machine=connection.machine_id, rating_key=one.rating_key,
                        guid=one.guid, smart=one.smart, state=PENDING, due_at=now,
                        base=None if one.smart else {"title": one.title, "items": []}))
    db.flush()
    logger.info("Playlist taken in from Plex playlist=%s smart=%s", playlist.id, one.smart)


jobs.register(ROUND, run_round, settle)
jobs.register(POLL, run_poll)
jobs.LANES[LANE] = KINDS
