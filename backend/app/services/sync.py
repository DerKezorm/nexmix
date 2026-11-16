"""Every playlist of nexmix is a playlist on its source server (``Playlist.source_server_id``), and every change on
either side reaches the other one (Plexamp and Plex Web on one side, nexmix on the other). The server is asked only
through its connector (``sources``); this module speaks nexmix's numbers of tracks (``media.py``) and turns the server's
keys into them and back.

**nexmix to the server.** A change in nexmix marks the playlist due two seconds later (``mark``: more changes in those
two seconds go along). The sync then reads the server's entries, merges them with nexmix's against the last state both
agreed on (``merge.py``), and writes only the difference: entries taken out by their entry id, entries added at the end
in parts, then the moves (to the start without ``after``, else after an entry of the state just read). It reads the
entries again right away (they are right at once) and keeps them as the new common state. A deleted playlist goes from
the server at once; brought back from the trash, it is made there anew.

**The server to nexmix.** Every 15 seconds the list of the server's playlists (``take_in``): a playlist whose
``updatedAt``, ``leafCount`` or name differs from what was seen is read and merged; one changed within two seconds
before the list was asked for (the server's clock, from its ``Date``) is read once more at the next look, because a
second change in the same second would not show. The list tells of entries added and taken out, and of a new name, but
not of entries moved (and asking Plex for the playlist itself would set its time anew, measured on Plex 1.43): so a
playlist open in nexmix is read at every look, and every other one within two minutes, ten at a look (with more than
80 the round takes longer; the Plex card says how long, ``order_seconds``). A sync takes the playlist's name and time
from the last look instead of asking for the list again. ``updatedAt`` only says when to look; whether anything changed
says the comparison of the entries with the common state, so nexmix's own writes are never taken for somebody else's.
Plex's stream of events (``plex_events.py``) makes a look at once when a playlist is made, renamed or deleted there. A
playlist new on the server comes to nexmix and belongs to the operator; a smart one is only read (its entries as the
server lists them, the number from ``totalSize``). Deleted on the server, the playlist goes to nexmix's trash, but only
when the server's list, read again, lacks it, no other of nexmix's playlists is missing from that list, and the server
says of the playlist itself that it is gone: a 404 alone (a proxy, a changed base path), a Jellyfin user gone, several
playlists missing at once never move anything to the trash; the playlist stays, its sync fails with a code and is tried
again. A track the server has but the catalogue does not know yet is shown with what the server says of it, and a
partial run of the catalogue is asked for.

**Nothing is lost.** Before an entry taken out on the server is taken out in nexmix too, the server is asked whether
its track is still in its library (``existing``, one question per sync, in parts; never nexmix's catalogue, which may
be older or newer). A track the server's library no longer has stays in nexmix (it cannot go there), out of the common
state, and its entry says it is missing there (``HINT_MISSING``): Jellyfin drops an entry without a word when the file
of its track is renamed or moved, or the library is removed, and that is no entry taken out. After the next run of the
catalogue the entry finds its track again (``relink.py``) and is written back. When a playlist on the server loses many
entries at once and some of their tracks left the library with them, none of it is taken: the entries stay with the
same note, the playlist says how many (``loss``), and they go back after the next run of the catalogue, unless
somebody takes the loss over in nexmix. A playlist emptied on the server while its tracks are all still there was
emptied on purpose, and that is taken. When the question fails (the server away, too slow), nothing is taken, and the
sync is tried again later. A
track the server did not take when it was added is offered again after a minute, five, fifteen and sixty, never taken
for one taken out there; after the fifth try it stays in nexmix only, and its entry says so.

**Jellyfin** names an entry by its track (``Capabilities.entries``): nexmix's number of the track stands for the
entry's id, an entry taken out and put in again there between two looks is no change, and an open write cannot be
told to have reached it from the ids (``_sent``): there, adding wins. Its list carries an ``Etag`` that changes with
every change, the order too; after a write nexmix takes its own new ``Etag`` before it reads the entries a last time,
so a change made there right after still shows. Copies of a track made in Jellyfin count as one.

**Music Assistant** names an entry by its place only (``Capabilities.moves`` ``tail``): nexmix names it by its track,
as for Jellyfin. Entries go out by their places in a reading of the moment, and before that the sync keeps what must
still be there afterwards (``writing["restore"]``): a step broken off half way is put right at the next sync
(``_restore``), never read as entries taken out there. When the playlist changed in Music Assistant meanwhile, the
connector puts back what it may have taken by mistake and the sync merges again (``Raced``) instead of taking out by
place once more. Nothing goes by place unless the reading right before is the one merged and nobody else's task at the
playlist is under way; an entry added there while a step ran that may be gone is told (``note["concurrent"]``). A task
nobody saw end (``TaskOpen``) is kept (``writing["tasks"]``): until Music Assistant says it ended, nothing is written to
the playlist, nothing taken from it (``_await_tasks``). Only a token of an admin there writes by place. A new order
takes out everything from the first entry out of place and puts it back in order. A new name makes a new playlist,
copies everything over (what Music Assistant passes over stays in nexmix as missing there) and deletes the old one
(``_rename_copy``); while that runs,
``writing["renaming"]`` names both, so that neither is ever taken for a playlist deleted there or made there, and a copy
broken off half way is cleaned up (``_finish_rename``). Changes made there come as events at once (``ma_events``); the
list is looked at every minute.

**Another server.** Each copy of a playlist on a server has a row of its own (``ServerPlaylist``). When the playlists
move to another server (``moved``: the operator connected another Plex in the place of the one before), the copies on
the server before stay there with their common state, and the entries are found on the new server (``match.py``: by
the recording's id, else by title, album and length; never by the number alone, nor by the song or the title alone):
what is found nowhere stays in nexmix only, and its entry says so. A copy left on a server is taken up again when the
playlist comes back there, but only when the server still lists it under its key (``resume``); else it is made anew (a
smart playlist never: it rests until it is back on its own server). A playlist deleted for good leaves its copies' rows
behind until each is gone from its server.

**One at a time.** Every sync of a copy and every look at a server's list is a job of the lane ``playlists``
(``jobs.py``): never two at once, and a stopped server goes on where it was. A write that a stop or a fault broke off
is found by the next sync (``writing``: the state it was to bring with the ids the server had, what the server was to
lose and to get) and finished: an entry still to be taken out is taken out, not read as one added there; one still to
be added is added, not read as one taken out there; one the server lacks of the rest was taken out there, and goes. A
track counts as added once the server answered for it (``writing`` is narrowed after every part it took whole, and
after reading it again), or once Plex holds two tracks of the same request that show it arrived (the answer was lost:
``_sent``): taken out on the server after that, it stays out. Where that cannot be told, it stays named as to be added,
and adding wins. The server's order counts for nothing until then (it may be half of nexmix's). A large new order goes
in parts of 200 moves, the lane free for other playlists in between.

**When the server is away** the changes stay in nexmix, the playlist says it waits for the server, and the sync is
tried again after 5 seconds, then 10, 30, a minute and so on up to ten minutes; a look at the list that reaches the
server again tries at once.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

from sqlalchemy import delete, exists, select, true, update
from sqlalchemy.orm import Session

from .. import clock
from ..db import SessionLocal
from ..models import OPERATOR, Account, Job, MediaServer, Playlist, PlaylistItem, ServerPlaylist
from . import jobs, match, media, merge, mirrors, plex_link, sources
from .events import hub, playlist_topic
from .jellyfin import AWAY as JELLYFIN_AWAY
from .music_assistant import AWAY as MA_AWAY
from .music_assistant import TASK_OPEN, MaError, TaskOpen
from .plex import PlexError
from .sources.base import PLAYLISTS_NOT_FOUND, Entry, Listed
from .sources.plex import CHUNK

logger = logging.getLogger("nexmix.sync")

LANE = "playlists"
ROUND = "server_round"
POLL = "server_poll"
KINDS = (ROUND, POLL, mirrors.TRANSFER)
#: A change in nexmix waits this long for more before it goes to the server.
GATHER = timedelta(seconds=2)
POLL_EVERY = timedelta(seconds=15)
#: A server that tells of every change of a playlist at once (Music Assistant): its list only every minute.
POLL_EVERY_TOLD = timedelta(seconds=60)
#: A smart playlist's entries change with the library, not its ``updatedAt``: read again this often.
SMART_EVERY = timedelta(minutes=15)
#: A playlist changed this close before the server's clock at the look is looked at again the next time.
RECENT = 2
#: Moves on the server do not show in the list: every playlist is read once it is this long unread, at the next look
#: (so within two minutes), at most this many at one look, the ones longest unread first; one open in nexmix is read at
#: every look. With more than 80 playlists a round takes longer (``order_seconds``): the server is spared, and the Plex
#: card says how long.
CHECK_EVERY = timedelta(seconds=105)
CHECK_PER_LOOK = 10
#: The waits after failed tries in a row, in seconds; the last one stays.
BACKOFF = (5, 10, 30, 60, 120, 300, 600)
#: Codes that mean the server was not reached: the playlist waits for it.
AWAY = frozenset({"plex_unreachable", "plex_timeout", "plex_busy", *JELLYFIN_AWAY, *MA_AWAY, TASK_OPEN})
#: The most tasks of Music Assistant kept as open for one copy (``writing["tasks"]``).
TASKS_KEPT = 50
#: Codes with which the server says it does not know the playlist (or the entry). A sign only: whether the playlist
#: is gone there says ``Round._vanished``.
GONE = frozenset({"plex_not_found", "jellyfin_not_found", "ma_not_found"})
#: The server lists the playlist but will not give its entries, or says it still has it: the sync fails with this code,
#: the playlist stays where it is.
UNREADABLE = "playlist_unreadable"
#: So many playlists of a server missing from its list at once are no deletion: nothing goes to the trash, the server
#: says how many it lacks (``overview``), and the list is looked at again and again.
MASS_GONE = 2
#: How often the clock of the sync looks for due playlists.
TICK_SECONDS = 1.0
#: How many times one sync merges again when somebody changed the playlist in nexmix while it merged.
MERGE_TRIES = 4
#: How many times the moves are planned again when the server's order is not the wanted one after them.
MOVE_TRIES = 3
#: The most moves one sync makes: a large new order goes in parts, the lane free for others in between (the write
#: stays open, ``writing``, and the next part follows at once).
MOVE_PART = 200
#: The waits before a track the server refused is offered again, in seconds; after the last try it stays in nexmix
#: only, and its entry says so (``plex_hint`` ``refused``).
REFUSED_WAITS = (60, 300, 900, 3600)
REFUSED_TRIES = len(REFUSED_WAITS) + 1
#: What an entry says of its place on the server: the server refused its track, or the track is not on this server
#: (the playlist came from another server and nothing here matched it).
HINT_REFUSED, HINT_ELSEWHERE = "refused", "elsewhere"
#: The server's playlist lost the entry while its track left the library (a file renamed), or with many others at
#: once: kept in nexmix, written back after the next run of the catalogue.
HINT_MISSING = "missing"
#: Many entries lost at once: at least this many, while some of their tracks left the library too.
MASS_LOSS = 5
#: Who added an entry that came through the server, by its kind.
PLEX_NAME = "Plex"
VIA_NAMES = {"plex": PLEX_NAME, "jellyfin": "Jellyfin", "music_assistant": "Music Assistant"}
#: What a copy is to settle when the playlist came from another server: every entry, or the ones put in before the
#: server was connected.
SETTLE_ALL, SETTLE_SINCE = "all", "since"

SYNCED, PENDING, WAITING, ERROR = "synced", "pending", "waiting", "error"
#: The playlist changed on the server while nexmix wrote it by place, so many times in a row that the sync gives up
#: for a while (``Raced``); before that it merges again a second later.
RACED, RACED_TRIES = "playlist_changed_meanwhile", 5
#: How long a playlist made under a name nobody else had (``unique_names``) may wait to be taken as the one made when
#: the answer was lost.
ADOPT_UNIQUE = 3600
#: The playlist lives in nexmix only: its source server takes no playlists from nexmix.
LOCAL = "local"
#: The note of entries added on the server while nexmix wrote by place, which may be gone (Music Assistant).
CONCURRENT = "concurrent"
#: Why a smart playlist rests on a server it was not made on: its rules live on its own server.
SMART_NOT_HERE = "smart_not_here"


class Stale(Exception):
    """The server was let go of (disconnected, another server or library) while the sync was under way."""


def _watch(server_id: int, started: int) -> Callable[[], bool]:
    def stop() -> bool:
        if media.epoch(server_id) != started:
            raise Stale
        return jobs.stopping.is_set()

    return stop


@dataclass(frozen=True)
class Item:
    """An entry of a playlist on the server, by nexmix's numbers: its id there (None in a smart playlist), the track,
    and what the server says of the track."""

    item: int | None
    track: int
    title: str
    artist: str
    album: str
    album_id: int
    year: int | None
    duration: int
    guid: str = ""
    #: How the server's requests name the entry (Plex: its ``playlistItemID``; Jellyfin: the track's id).
    ref: str = ""


# --- Numbers and keys ------------------------------------------------------------------------------------------------


def _numbered(server_id: int, entries: Sequence[Entry], *, by_track: bool = False) -> list[Item]:
    """The server's entries by nexmix's numbers; the numbers of tracks and albums new to nexmix are made now, in a
    transaction of their own (whoever calls holds no write). ``by_track``: the server names an entry by its track, and
    the track's number stands for the entry's id."""
    with SessionLocal() as own:
        tracks = media.numbers(own, server_id, media.TRACK, (entry.track for entry in entries))
        albums = media.numbers(own, server_id, media.ALBUM, (entry.album_key for entry in entries if entry.album_key))
        own.commit()
    return [Item(item=tracks[entry.track] if by_track else entry.item, track=tracks[entry.track], title=entry.title,
                 artist=entry.artist, album=entry.album, album_id=albums.get(entry.album_key, 0), year=entry.year,
                 duration=entry.duration, guid=entry.guid,
                 ref=entry.ref or (str(entry.item) if entry.item is not None else ""))
            for entry in entries]


def _keyed(server_id: int, tracks: Iterable[int]) -> dict[int, str]:
    """The server's keys of tracks; a number that is no track of the server is left out."""
    with SessionLocal() as own:
        return media.keys(own, server_id, media.TRACK, tracks)


# --- What nexmix tells the sync ---------------------------------------------------------------------------------------


def copy_of(db: Session, playlist_id: int, server_id: int | None) -> ServerPlaylist | None:
    if server_id is None:
        return None
    return db.scalars(select(ServerPlaylist).where(ServerPlaylist.playlist_id == playlist_id,
                                                   ServerPlaylist.server_id == server_id)
                      .execution_options(populate_existing=True)).first()


def mirror_of(db: Session, playlist_id: int) -> ServerPlaylist | None:
    """The copy on the playlist's source server."""
    return copy_of(db, playlist_id, media.source_of(db, playlist_id))


def _due(row: ServerPlaylist, now: datetime, moment: datetime, at_once: bool) -> None:
    if row.state == WAITING and not at_once and row.due_at is not None and row.due_at > now:
        return
    row.state = PENDING
    if row.due_at is None or row.due_at > moment:
        row.due_at = moment


def mark(db: Session, playlist_id: int, *, at_once: bool = False, but: int | None = None) -> None:
    """The playlist changed in nexmix: due in two seconds (``at_once``: now), unless it is due sooner already, or it
    waits for its server (the changes gather until the next try); its mirrors too. In the caller's transaction. A
    playlist without a source server waits for one (``moved``). ``but``: the copy that brought the change (it has
    it)."""
    now = clock.now()
    moment = now if at_once else now + GATHER
    for row in mirrors.mirror_rows(db, playlist_id):
        if row.id != but and not row.gone_there:
            _due(row, now, moment, at_once)
    source = media.source_of(db, playlist_id)
    if source is None or not keeps_playlists(media.get(db, source)):
        return
    row = copy_of(db, playlist_id, source)
    if row is None:
        db.add(ServerPlaylist(playlist_id=playlist_id, server_id=source, state=PENDING, due_at=moment))
        return
    if row.smart or row.id == but:
        return
    _due(row, now, moment, at_once)


def restored(db: Session, playlist_id: int) -> None:
    """Back from the trash: made on the server anew (a smart one comes back as an ordinary playlist with its last
    entries)."""
    row = mirror_of(db, playlist_id)
    if row is not None:
        row.smart, row.smart_total, row.gone_there, row.note = False, None, False, None
        # ``mark`` reads the row afresh: what was set here must be in the transaction by then.
        db.flush()
    mark(db, playlist_id, at_once=True)


def dropping(db: Session, playlist_id: int) -> None:
    """The playlist goes for good: a copy still on a server is deleted there first (the row stays without the playlist
    until then, so the server's list never brings it back as a new one). A copy on a server that rests goes once that
    server is back and still lists it (``Round.run``). A mirror let go of (``mirrors.LET_GO``) is never deleted
    there."""
    for row in db.scalars(select(ServerPlaylist).where(ServerPlaylist.playlist_id == playlist_id)
                          .execution_options(populate_existing=True)).all():
        if row.ext_key is None or (row.gone_there and row.resume):
            # Never made there, or deleted there already (a playlist made there since under the same key is not ours).
            db.delete(row)
            continue
        row.due_at = clock.now()
        row.state = PENDING


def moved(db: Session, playlist_ids: Sequence[int], server_id: int, *, settle: str = SETTLE_ALL) -> None:
    """The playlists take their tracks from ``server_id`` from now on: each gets its copy there (a copy left there
    before is taken up again only when the server still lists it, ``resume``), and its entries are found there first
    (``settle``). A smart playlist stays one: its copy there is smart too, read when it is that server's own, else it
    rests (``Round.run``). In the caller's transaction."""
    now = clock.now()
    keeps = keeps_playlists(media.get(db, server_id))
    for playlist_id in playlist_ids:
        before = mirror_of(db, playlist_id)
        smart = before is not None and bool(before.smart)
        db.execute(update(Playlist).where(Playlist.id == playlist_id).values(source_server_id=server_id))
        if not keeps:
            continue
        row = copy_of(db, playlist_id, server_id)
        if row is None:
            db.add(ServerPlaylist(playlist_id=playlist_id, server_id=server_id, state=PENDING, due_at=now,
                                  settle=settle, smart=smart))
            continue
        if smart and row.ext_key is None:
            row.smart = True
        row.settle, row.state, row.due_at, row.tries, row.error = settle, PENDING, now, 0, ""
    db.flush()
    if playlist_ids:
        logger.info("Playlists moved to another server count=%s server=%s", len(playlist_ids), server_id)


def is_smart(db: Session, playlist_id: int) -> bool:
    row = mirror_of(db, playlist_id)
    return bool(row is not None and row.smart)


def _usable(server: MediaServer | None) -> bool:
    return server is not None and bool(server.active) and bool(server.section_key)


def keeps_playlists(server: MediaServer | None) -> bool:
    """Whether nexmix keeps its playlists on the server (Plex and Jellyfin do)."""
    return server is not None and sources.capabilities(server.kind).playlists


def view(db: Session, playlist_id: int, row: ServerPlaylist | None = None) -> dict[str, Any]:
    """How the copy on the source server stands, for the interface: the server's ``kind``; ``off`` without a server,
    ``local`` when the server takes no playlists from nexmix (no kind of server today), else ``synced``, ``pending``,
    ``waiting`` (server not reached) or ``error`` with its code; whether it is a smart playlist and how many entries
    it has; the note of an order sorted on the server and in nexmix at the same time, the note of many entries the
    server lost at once (``loss``), and the note of entries added in Music Assistant while nexmix wrote there by place,
    which may be gone (``concurrent``)."""
    source = media.source_of(db, playlist_id)
    row = copy_of(db, playlist_id, source) if row is None else row
    state = row.state if row is not None else PENDING
    server = media.get(db, source)
    if not _usable(server):
        state = "off"
    elif not keeps_playlists(server):
        # The playlist lives in nexmix only: its server takes no playlists from nexmix (yet).
        state, row = LOCAL, None
    note = row.note if row is not None and isinstance(row.note, dict) else None
    loss = note.get("loss") if note is not None and isinstance(note.get("loss"), dict) else None
    concurrent = _concurrent_note(db, playlist_id)
    return {
        "state": state,
        "kind": server.kind if server is not None else None,
        "error": row.error if row is not None and state in (ERROR, WAITING) and row.error else None,
        "smart": bool(row is not None and row.smart),
        "total": row.smart_total if row is not None and row.smart else None,
        "synced_at": row.synced_at.isoformat() if row is not None and row.synced_at else None,
        "gone_in_plex": bool(row is not None and row.gone_there),
        "note": {"at": note.get("at"), "count": len(note.get("order") or [])} if note and note.get("order") else None,
        "loss": {"at": loss.get("at"), "count": int(loss.get("count") or 0)} if loss else None,
        "concurrent": concurrent,
    }


def _concurrent_note(db: Session, playlist_id: int) -> dict[str, Any] | None:
    """Entries added in Music Assistant while nexmix wrote there by place, which may be gone (``Round._concurrent``):
    of every copy of the playlist, the source's and the mirrors', how many and when last."""
    count, at = 0, None
    for note in db.scalars(select(ServerPlaylist.note).where(ServerPlaylist.playlist_id == playlist_id)):
        found = note.get(CONCURRENT) if isinstance(note, dict) else None
        if isinstance(found, dict):
            count += int(found.get("count") or 0)
            at = max(at or "", str(found.get("at") or "")) or None
    return {"at": at, "count": count} if count else None


def _publish(db: Session, playlist_id: int | None, row: ServerPlaylist | None = None) -> None:
    if playlist_id is None:
        return
    if row is not None and row.mirror:
        # A mirror tells of all the playlist's mirrors; the source's state stays as it is.
        hub.publish(playlist_topic(playlist_id), "mirrors", {"mirrors": mirrors.view(db, playlist_id)})
        return
    hub.publish(playlist_topic(playlist_id), "plex", view(db, playlist_id, row))


# --- What the interface asks for --------------------------------------------------------------------------------------


def request(db: Session, playlist_id: int) -> dict[str, Any]:
    """Now ("sync now", "try again"): the server is read at once and nexmix's changes are written."""
    row = mirror_of(db, playlist_id)
    if row is None:
        mark(db, playlist_id, at_once=True)
    else:
        row.due_at = clock.now()
        if row.state in (ERROR, WAITING):
            row.state, row.tries = PENDING, 0
        _offer_now(row)
    for copy in mirrors.mirror_rows(db, playlist_id):
        if not copy.gone_there:
            copy.due_at = clock.now()
            if copy.state in (ERROR, WAITING):
                copy.state, copy.tries = PENDING, 0
            _offer_now(copy)
    db.commit()
    _publish(db, playlist_id)
    return view(db, playlist_id)


def request_all(db: Session, server_id: int | None = None) -> None:
    """Every playlist now, and a look at the list of every server (``server_id``: of that one)."""
    chosen = (ServerPlaylist.server_id == server_id) if server_id is not None else true()
    db.execute(update(ServerPlaylist).where(chosen).values(due_at=clock.now()))
    db.execute(update(ServerPlaylist).where(chosen, ServerPlaylist.state.in_((ERROR, WAITING)))
               .values(state=PENDING, tries=0))
    look_now(server_id)
    db.commit()


def look_now(server_id: int | None = None) -> None:
    """A look at the list of a server (None: of every one) at the next tick (Plex's stream of events said a playlist
    was made, renamed or deleted there)."""
    if server_id is None:
        _polled_at.clear()
    else:
        _polled_at.pop(int(server_id), None)


def plex_order(db: Session, playlist_id: int) -> list[int] | None:
    """The order the server had when both sides sorted at the same time (the note's undo), or None: there is no
    note."""
    row = mirror_of(db, playlist_id)
    note = row.note if row is not None and isinstance(row.note, dict) else None
    if note is None or not note.get("order"):
        return None
    return [int(track) for track in note.get("order") or [] if isinstance(track, int)]


def drop_note(db: Session, playlist_id: int, part: str = "order") -> None:
    """The note of an order sorted on both sides (``order``), of entries lost on the server at once (``loss``), or of
    entries added in Music Assistant meanwhile that may be gone (``concurrent``, of every copy) goes."""
    if part == CONCURRENT:
        rows = list(db.scalars(select(ServerPlaylist).where(ServerPlaylist.playlist_id == playlist_id)))
    else:
        found = mirror_of(db, playlist_id)
        rows = [found] if found is not None else []
    changed = None
    for row in rows:
        note = row.note if isinstance(row.note, dict) else None
        if note is None:
            continue
        rest = {key: value for key, value in note.items() if key not in ((part, "at") if part == "order" else (part,))}
        if rest == note:
            continue
        # The time of an order's note goes with it; what is left of other notes stays.
        row.note = rest if any(key != "at" for key in rest) else None
        changed = row
    if changed is None:
        return
    db.commit()
    if part == CONCURRENT:
        hub.publish(playlist_topic(playlist_id), "plex", view(db, playlist_id))
    else:
        _publish(db, playlist_id, changed)


def missing_tracks(db: Session, playlist_id: int) -> list[int]:
    """The entries the server lost while their track left the library, or with many others at once (they wait for
    the next run of the catalogue), by their ids."""
    return list(db.scalars(select(PlaylistItem.id).where(
        PlaylistItem.playlist_id == playlist_id, PlaylistItem.removed_at.is_(None),
        PlaylistItem.plex_hint == HINT_MISSING).order_by(PlaylistItem.position, PlaylistItem.id)))


def after_catalogue(db: Session, server_id: int) -> int:
    """After a run of the catalogue of ``server_id``: the entries the server lost (``HINT_MISSING``) whose track the
    catalogue has (again, or under its new number since ``relink``) go back to the server. Returns how many."""
    mirrors.after_catalogue(db, server_id)
    generation = media.generation_of(db, server_id)
    owned = list(db.scalars(select(Playlist.id).where(
        Playlist.source_server_id == server_id, Playlist.deleted_at.is_(None),
        exists().where(PlaylistItem.playlist_id == Playlist.id, PlaylistItem.removed_at.is_(None),
                       PlaylistItem.plex_hint == HINT_MISSING)).order_by(Playlist.id)))
    db.rollback()
    if not owned or not generation:
        return 0
    from ..models import CatalogTrack

    back = 0
    for playlist_id in owned:
        # The write lock first: nothing changes the entries meanwhile.
        db.execute(update(Playlist).where(Playlist.id == playlist_id)
                   .values(version=Playlist.version + 1, updated_at=clock.now()))
        items = list(db.scalars(select(PlaylistItem).where(
            PlaylistItem.playlist_id == playlist_id, PlaylistItem.removed_at.is_(None),
            PlaylistItem.plex_hint == HINT_MISSING)))
        known = set(db.scalars(select(CatalogTrack.id).where(
            CatalogTrack.server_id == server_id, CatalogTrack.generation == generation,
            CatalogTrack.id.in_([item.track_id for item in items]))))
        found = [item for item in items if item.track_id in known]
        if not found:
            db.rollback()
            continue
        for item in found:
            item.plex_hint = ""
        row = mirror_of(db, playlist_id)
        if row is not None and isinstance(row.note, dict) and row.note.get("loss") and len(found) == len(items):
            rest = {key: value for key, value in row.note.items() if key != "loss"}
            row.note = rest if any(key != "at" for key in rest) else None
        db.flush()
        mark(db, playlist_id, at_once=True)
        db.commit()
        back += len(found)
        logger.info("Entries the server lost go back to it after the catalogue run playlist=%s count=%s",
                    playlist_id, len(found))
        publish_items(db, playlist_id, [], changed=found)
        _publish(db, playlist_id)
    return back


def order_seconds(count: int) -> int:
    """How long an order changed on the server takes at most to reach nexmix, with ``count`` ordinary playlists: each
    is read again when it is ``CHECK_EVERY`` unread, at a look every ``POLL_EVERY``, ``CHECK_PER_LOOK`` at a look."""
    looks = math.ceil(max(count, 1) / CHECK_PER_LOOK)
    every = int(POLL_EVERY.total_seconds())
    return max(int(CHECK_EVERY.total_seconds()) + every, looks * every)


def overview(db: Session, server_id: int | None = None) -> dict[str, Any]:
    """The sync of the playlists of a server (None: the first one connected), for the server's card: how many have the
    server as their source and how many are mirrors there, how they stand, the smart ones, the last look at the
    server."""
    server = media.get(db, server_id) if server_id is not None else media.primary(db)
    ident = server.id if server is not None else -1
    source = Playlist.source_server_id == ServerPlaylist.server_id
    rows = db.execute(select(ServerPlaylist.state, ServerPlaylist.smart, source).join(
        Playlist, (Playlist.id == ServerPlaylist.playlist_id) & (source | ServerPlaylist.mirror.is_(True)))
        .where(Playlist.deleted_at.is_(None), ServerPlaylist.server_id == ident)).all()
    counts = {SYNCED: 0, PENDING: 0, WAITING: 0, ERROR: 0}
    smart = owned = 0
    for state, smart_one, is_source in rows:
        counts[state] = counts.get(state, 0) + 1
        smart += 1 if smart_one else 0
        owned += 1 if is_source else 0
    result = _poll_result.get(ident, {})
    at = result.get("at")
    missing = _missing_note.get(ident)
    # Mirrors on this server emptied there and waiting for an answer: several at once say more of the server than of
    # the playlists (a library made anew), the server's card says so.
    emptied = sum(1 for note in db.scalars(select(ServerPlaylist.note).join(
        Playlist, Playlist.id == ServerPlaylist.playlist_id).where(
        ServerPlaylist.server_id == ident, ServerPlaylist.mirror.is_(True), Playlist.deleted_at.is_(None),
        ServerPlaylist.note.is_not(None))) if isinstance(note, dict) and note.get("emptied"))
    # A server whose list carries a mark of every change (Jellyfin's ``Etag``) shows an order changed there at the next
    # look; Plex's only when the playlist is read again.
    marked = server is not None and sources.capabilities(server.kind).entries == "track"
    return {
        "active": _ready(db, ident) is not None,
        "kind": server.kind if server is not None else None,
        # Every playlist kept on the server, the ones it is the source of and the mirrors there (copies of a playlist
        # of another server); the states count both.
        "playlists": len(rows),
        "sources": owned,
        "mirrors": len(rows) - owned,
        **counts,
        "smart": smart,
        "order_minutes": 1 if marked else math.ceil(order_seconds(len(rows) - smart) / 60),
        "polled_at": at.isoformat() if isinstance(at, datetime) else None,
        "poll_error": result.get("error"),
        # Several playlists missing from the server's list at once: none went to the trash, the list is looked at on.
        "missing": int(missing.get("count") or 0) if missing else 0,
        # Mirrors here emptied there, waiting for an answer at their playlist (none taken over).
        "emptied": emptied,
    }


# --- The clock of the sync --------------------------------------------------------------------------------------------

#: When each server's list was last looked at (by server).
_polled_at: dict[int, datetime] = {}
#: The last look at the list of each server: when, and the code it failed with (None: it went through).
_poll_result: dict[int, dict[str, Any]] = {}


def forget() -> None:
    _polled_at.clear()
    _poll_result.clear()
    _listing.clear()
    _whole.clear()
    _not_ours.clear()
    _missing_note.clear()


#: Servers that lack several of nexmix's playlists in their list at once (``MASS_GONE``): how many, since when.
_missing_note: dict[int, dict[str, Any]] = {}


def _note_missing(server_id: int, count: int) -> None:
    before = _missing_note.get(server_id)
    if before is None or before.get("count") != count:
        # Once per change, not at every look.
        logger.warning("Playlists missing from the server's list, none moved to the trash server=%s count=%s",
                       server_id, count)
        _missing_note[server_id] = {"count": count, "at": clock.now()}


def _missing_from(db: Session, server_id: int, there: set[str]) -> set[str]:
    """The keys of nexmix's copies on the server (of playlists it is the source of, and mirrors, not in the trash)
    that its list ``there`` lacks."""
    keys = db.scalars(select(ServerPlaylist.ext_key).join(Playlist, Playlist.id == ServerPlaylist.playlist_id).where(
        ServerPlaylist.server_id == server_id, ServerPlaylist.ext_key.is_not(None), ServerPlaylist.resume.is_(False),
        (Playlist.source_server_id == server_id) | ServerPlaylist.mirror.is_(True), Playlist.deleted_at.is_(None)))
    return {key for key in keys if key is not None and key not in there}


def _ready(db: Session, server_id: int | None) -> MediaServer | None:
    """The server, when the sync can work with it: connected, a library chosen and its catalogue there."""
    row = media.get(db, server_id)
    return row if _usable(row) and row is not None and row.generation else None


def tick(db: Session, now: datetime | None = None) -> int:
    """Puts the due copies in the lane, and the look at each server's list every 15 seconds. Returns how many jobs it
    queued."""
    moment = now or clock.now()
    # Rows of playlists deleted for good that have no copy anywhere: nothing left to do.
    db.execute(delete(ServerPlaylist).where(ServerPlaylist.playlist_id.is_(None), ServerPlaylist.ext_key.is_(None)))
    first = media.primary(db)
    if first is not None:
        # A playlist made while no server was there takes the first one connected.
        homeless = list(db.scalars(select(Playlist.id).where(Playlist.source_server_id.is_(None))))
        if homeless:
            moved(db, homeless, first.id, settle=SETTLE_SINCE)
    ready = [row for row in media.ready(db) if keeps_playlists(row)]
    if not ready:
        db.commit()
        return 0
    ready_ids = [row.id for row in ready]
    # Every playlist has its copy on its source (one made while the server was away, one of an older nexmix).
    for playlist_id, source in db.execute(select(Playlist.id, Playlist.source_server_id).where(
            Playlist.deleted_at.is_(None), Playlist.source_server_id.in_(ready_ids),
            ~exists().where(ServerPlaylist.playlist_id == Playlist.id,
                            ServerPlaylist.server_id == Playlist.source_server_id))).all():
        db.add(ServerPlaylist(playlist_id=playlist_id, server_id=source, state=PENDING, due_at=moment))
    db.flush()
    due = list(db.scalars(select(ServerPlaylist.id).where(
        ServerPlaylist.due_at.is_not(None), ServerPlaylist.due_at <= moment, ServerPlaylist.server_id.in_(ready_ids))
        .order_by(ServerPlaylist.due_at, ServerPlaylist.id)))
    for ident in due:
        jobs.enqueue(db, ROUND, ref=ident)
    if due:
        db.execute(update(ServerPlaylist).where(ServerPlaylist.id.in_(due)).values(due_at=None))
    queued = len(due)
    for server in ready:
        last = _polled_at.get(server.id)
        every = POLL_EVERY_TOLD if sources.capabilities(server.kind).changes == "events" else POLL_EVERY
        if last is None or not timedelta(0) <= moment - last < every:
            jobs.enqueue(db, POLL, ref=server.id)
            _polled_at[server.id] = moment
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
    row = db.get(ServerPlaylist, ident, populate_existing=True)
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
    logger.warning("Playlist sync failed playlist=%s server=%s error=%s try=%s", row.playlist_id, row.server_id, code,
                   row.tries)
    _publish(db, row.playlist_id, row)


def _raced(db: Session, ident: int) -> None:
    """The playlist changed on the server while it was written by place: merged again in a second (the write stays
    open), unless that happened ``RACED_TRIES`` times in a row."""
    db.rollback()
    row = db.get(ServerPlaylist, ident, populate_existing=True)
    if row is None:
        return
    if (row.tries or 0) >= RACED_TRIES:
        _failed(db, ident, RACED)
        return
    row.tries = (row.tries or 0) + 1
    row.state = PENDING
    row.due_at = clock.now() + timedelta(seconds=1)
    db.commit()
    logger.info("Playlist changed on the server while it was written, merged again playlist=%s server=%s try=%s",
                row.playlist_id, row.server_id, row.tries)


def _task_left(db: Session, ident: int, exc: TaskOpen) -> None:
    """A task of Music Assistant nobody saw end (cancelling it was refused, the websocket broke): kept with the open
    write (``writing["tasks"]``), with what it meant to take out, until Music Assistant says it ended. Until then
    nothing is written to the playlist, nothing is taken from it (``Round._await_tasks``)."""
    db.rollback()
    row = db.get(ServerPlaylist, ident, populate_existing=True)
    if row is None:
        return
    writing = dict(row.writing) if isinstance(row.writing, dict) else {}
    tasks = [one for one in writing.get("tasks") or [] if isinstance(one, dict) and one.get("id") != exc.task]
    tasks.append({"id": exc.task, "meant": [str(one) for one in exc.meant][:5000]})
    writing["tasks"] = tasks[-TASKS_KEPT:]
    row.writing = writing
    db.commit()
    logger.warning("Music Assistant task left open, the playlist waits until it ends playlist=%s server=%s",
                   row.playlist_id, row.server_id)
    _failed(db, ident, exc.code)


def run_round(db: Session, job: Job) -> None:
    ident = int(job.ref)
    row = db.get(ServerPlaylist, ident, populate_existing=True)
    if row is None:
        return
    server = _ready(db, row.server_id)
    if server is None:
        if _usable(media.get(db, row.server_id)):
            # The catalogue is not there yet: soon again.
            row.due_at = clock.now() + timedelta(seconds=30)
            db.commit()
        return
    playlist_id = row.playlist_id
    stop = _watch(server.id, media.epoch(server.id))
    try:
        with sources.open_server(db, server, stop=stop, patient=False) as source:
            Round(db, source, server, ident).run()
    except Stale:
        db.rollback()
    except sources.Raced:
        _raced(db, ident)
    except TaskOpen as exc:
        _task_left(db, ident, exc)
    except plex_link.Unreadable:
        _failed(db, ident, "token_unreadable")
    except PlexError as exc:
        if exc.code == "stopped":
            db.rollback()
            raise jobs.Interrupted from exc
        _failed(db, ident, exc.code)
    except Exception:
        logger.exception("Playlist sync failed playlist=%s server=%s", playlist_id, row.server_id)
        _failed(db, ident, "internal_error")


def settle(db: Session) -> None:
    """At the start of the lane: a copy waiting for a sync that no job carries any more is due now."""
    carried = exists().where(Job.kind == ROUND, Job.ref == ServerPlaylist.id, Job.state.in_(("queued", "running")))
    db.execute(update(ServerPlaylist).where(ServerPlaylist.due_at.is_(None), ServerPlaylist.state != SYNCED, ~carried)
               .values(due_at=clock.now()))
    db.commit()


# --- One sync of one copy ---------------------------------------------------------------------------------------------


@dataclass
class Plan:
    """What one merge decided."""

    merged: merge.Merged
    title: str
    #: nexmix's order after the merge: the merged one, with the entries the server cannot hold where they were.
    local_order: list[int]
    #: Tracks the server cannot hold (gone from its library, refused by it, not on this server): they stay in nexmix
    #: and out of the common state.
    held: set[int] = field(default_factory=set)
    #: The id nexmix gave each track of the common state the merge started from (a removal still to be written keeps
    #: it, so that the track put in again here counts as added).
    base_local: dict[int, int | None] = field(default_factory=dict)
    #: Entries whose hint goes: the server holds their track now.
    unhint: set[int] = field(default_factory=set)
    #: Tracks the server lost that stay in nexmix with ``HINT_MISSING`` (gone from its library, or lost with many
    #: others at once), and how many were lost at once when that was the reason (0: it was not).
    missing: set[int] = field(default_factory=set)
    loss: int = 0
    #: A mirror: what its merge brings back to the entries (``mirrors.Back``), and the server's entries it merged.
    back: mirrors.Back | None = None
    remote: list[Item] = field(default_factory=list)


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
    with the ids the server had for its entries when it began, and the entries it had still to take out of the server
    with theirs (the server still holding one under that id is no add there: it is taken out now)."""
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
        # ``[track, nexmix's id, the server's id]``; an entry still to be taken out: ``[track, the server's id,
        # nexmix's id]``.
        found.append(merge.Kept(track, third, _maybe(one[1])) if removing else merge.Kept(track, _maybe(one[1]), third))

    for one in stored.get("items") or []:
        take(one, removing=False)
    for one in (writing or {}).get("remove") or []:
        take(one, removing=True)
    return found


def _unwritten(writing: dict[str, Any] | None) -> set[int]:
    """The tracks an open write had still to put into the server. A write of an older form names none apart: every
    entry without an id of the server counts as one."""
    if writing is None:
        return set()
    if "add" in writing:
        return {int(track) for track in writing.get("add") or [] if isinstance(track, int)}
    return {int(one[0]) for one in writing.get("items") or []
            if isinstance(one, list | tuple) and one and (len(one) < 3 or one[2] is None)}


def _sent(writing: dict[str, Any] | None, remote_items: dict[int, int | None], chunk: int = CHUNK) -> set[int]:
    """The tracks of an open write that surely reached Plex although no answer said so (it was lost, or the server
    stopped before it could note it). The tracks to add go to Plex in requests of ``chunk``, in their order, and Plex
    puts the tracks of one request at the end, each with the next ``playlistItemID``. Two tracks of one request that
    Plex holds, whose ids lie exactly as far apart as their places in the write, show that the request reached Plex:
    every track of it was sent. Of those, one Plex lacks was taken out there (or is gone from the library, which
    ``_plan`` asks Plex about); it is not still to be added. One track alone proves nothing (somebody may have added it
    in Plex meanwhile), and two that do not line up neither."""
    if writing is None:
        return set()
    order = [track for track in writing.get("add") or [] if isinstance(track, int)]
    held = [(place, remote_items[track]) for place, track in enumerate(order) if remote_items.get(track) is not None]
    reached: set[int] = set()
    for (first, first_id), (second, second_id) in pairwise(held):
        same = first // chunk == second // chunk
        if same and first_id is not None and second_id is not None and second_id - first_id == second - first:
            reached.add(first // chunk)
    return {track for place, track in enumerate(order) if place // chunk in reached}


class Round:
    """One sync of one copy, its steps as methods. Reads go in transactions of their own that end before a write
    begins: a reader's snapshot never turns into a writer (SQLite would refuse it when somebody wrote in between)."""

    def __init__(self, db: Session, source: sources.PlexSource, server: MediaServer, ident: int) -> None:
        self.db = db
        self.source = source
        self.server_id = server.id
        self.kind = server.kind
        self.connected_at = server.connected_at
        self.ident = ident
        self.unknown = False
        self.added: set[int] = set()
        self.adding_keys: dict[str, int] = {}
        self.entry_ids: dict[int, int] = {}
        #: The playlist as the server answered when this sync made it (it is not in the last look's list yet).
        self.made: Listed | None = None
        #: The server names an entry by its track (Jellyfin): the track's number is the entry's id.
        self.by_track = sources.capabilities(server.kind).entries == "track"
        #: The playlist as the list gave it right after this sync wrote, before the entries were read a last time
        #: (Jellyfin's ``Etag`` changes at once): taken as seen, so the next look does not read it again for nothing.
        self.seen_after: Listed | None = None
        #: What the server said of tracks this sync asked about: still in its library or not (``_alive``).
        self.asked: dict[int, bool] = {}
        #: This copy is a mirror (``mirrors.py``): its entries are the source's, each by the track of this server that
        #: stands for it (``side``); tracks put in here that the source has no match for are held out (``held_out``).
        self.mirror = False
        self.side: mirrors.Side | None = None
        self.held_out: set[int] = set()
        #: Every track the last read of the server's entries held, the held-out ones too.
        self.raw_tracks: set[int] = set()
        #: The playlist changed role while this sync ran (moved to another server): it starts again.
        self.role_changed = False
        #: Tracks a new name by copy left behind (Music Assistant passed them over): kept in nexmix as missing there;
        #: and how the server names them.
        self.left_in_copy: set[int] = set()
        self.left_keys: set[str] = set()
        #: How the server names the entries of its playlist as last read (every one, held out or not), in order.
        self.raw_order: list[str] = []

    @property
    def via(self) -> str:
        return VIA_NAMES.get(self.kind, PLEX_NAME)

    @property
    def caps(self) -> sources.Capabilities:
        return sources.capabilities(self.kind)

    def _same_title(self, wanted: str, there: str) -> bool:
        """Whether the server's name of the playlist is the one nexmix wants: the same, or (where nexmix makes names
        unique there) the same made unique."""
        if self.caps.unique_names:
            return bool(self.source.same_title(wanted, there))
        return wanted == there

    @property
    def row(self) -> ServerPlaylist:
        row = self.db.get(ServerPlaylist, self.ident)
        if row is None:
            raise Stale
        return row

    def fresh_row(self) -> ServerPlaylist:
        row = self.db.get(ServerPlaylist, self.ident, populate_existing=True)
        if row is None:
            raise Stale
        return row

    def _items(self, key: str) -> list[Item]:
        found = _numbered(self.server_id, self.source.items(key), by_track=self.by_track)
        self.raw_tracks = {item.track for item in found}
        self.raw_order = [item.ref for item in found]
        if self.held_out:
            # Tracks put into the mirror that the source has no match for: neither taken over nor taken out.
            found = [item for item in found if item.track not in self.held_out]
        return found

    def run(self) -> None:
        db = self.db
        row = self.fresh_row()
        playlist = db.get(Playlist, row.playlist_id, populate_existing=True) if row.playlist_id is not None else None
        if row.resume and row.gone_there and playlist is not None and playlist.deleted_at is not None:
            # Deleted on the server and in the trash here: nothing to do until it is brought back.
            row.due_at, row.state = None, SYNCED
            db.commit()
            return
        if row.resume:
            self._resume(row)
            db.commit()
        if playlist is None:
            if row.settle == mirrors.LET_GO and not row.mirror:
                # A mirror let go of, its playlist gone for good: the copy there is the server's, never deleted ("it
                # stays there as it is"). The row stays without the playlist, so that the server's list never brings
                # the copy in as a new playlist.
                row.due_at, row.state = None, SYNCED
                db.commit()
                return
            if row.ext_key is not None:
                self.source.delete(row.ext_key)
                logger.info("Playlist deleted on the server server=%s", self.server_id)
            db.delete(row)
            db.commit()
            return
        if playlist.source_server_id != self.server_id:
            if row.mirror and not row.gone_there and not row.smart and playlist.deleted_at is None:
                # A mirror: kept in step with the playlist both ways.
                self.mirror = True
                self._sync(playlist.id)
                return
            # A copy on a server that is not the playlist's source: kept as it is until the playlist comes back here
            # (a mirror of a playlist in the trash: until it is brought back; one deleted on its server says so).
            if not (row.mirror and row.gone_there):
                row.state = SYNCED
            row.due_at = None
            db.commit()
            return
        if row.settle:
            self._settle_tracks(playlist.id, switched=row.settle != SETTLE_SINCE)
            row = self.fresh_row()
        if playlist.deleted_at is not None:
            if row.ext_key is not None:
                self._rescue(playlist.id)
                self.source.delete(self.row.ext_key or "")
                logger.info("Playlist deleted on the server playlist=%s", playlist.id)
            row = self.fresh_row()
            self._forget_copy(row)
            self._done(None)
            return
        if row.smart:
            if row.ext_key is None:
                # A smart playlist the server it was made on fills from its rules; it is never made here as an ordinary
                # one. It rests with its entries, and says why, until it is back on its own server.
                self._rests_smart()
            else:
                self._smart(playlist.id)
            return
        self._sync(playlist.id)

    def _rests_smart(self) -> None:
        row = self.row
        row.state, row.error, row.tries, row.due_at = ERROR, SMART_NOT_HERE, 0, None
        self.db.commit()
        logger.info("Smart playlist not on this server, not made here playlist=%s server=%s", row.playlist_id,
                    self.server_id)
        _publish(self.db, row.playlist_id, row)

    def _resume(self, row: ServerPlaylist) -> None:
        """A copy left on this server before: taken up with the state both sides last agreed on when the server still
        lists it under its key (and its ``guid``), as an ordinary playlist when it was one, and no other copy here has
        it; else forgotten (an ordinary playlist is made anew here, a smart one rests)."""
        key, guid, smart = row.ext_key, row.guid, row.smart
        base = row.base if isinstance(row.base, dict) else None
        self._forget_copy(row)
        row.resume, row.creating_at, row.creating_title = False, None, ""
        if key is None:
            return
        there = next((one for one in self.source.listing() if one.key == key), None)
        if there is None or there.smart != smart or (guid and there.guid and there.guid != guid):
            # A smart one stays smart: it is never made anew as an ordinary playlist (``run``).
            return
        row.ext_key, row.guid = key, there.guid or guid
        row.base = base if base is not None else {"title": there.title, "items": []}
        logger.info("Playlist copy on this server taken up again playlist=%s server=%s", row.playlist_id,
                    self.server_id)

    @staticmethod
    def _forget_copy(row: ServerPlaylist) -> None:
        row.ext_key, row.guid, row.base, row.writing, row.seen, row.note = None, "", None, None, None, None
        row.refused = None

    def _done(self, seen: Listed | None, *, recent: bool = False) -> None:
        row = self.row
        # A change in nexmix while this sync ran is due already: the playlist is not done with until it went too.
        due = self.db.scalar(select(ServerPlaylist.due_at).where(ServerPlaylist.id == self.ident))
        row.state = PENDING if due is not None else SYNCED
        row.error, row.tries = "", 0
        row.synced_at = clock.now()
        if seen is not None:
            row.seen = {"updated_at": seen.updated_at, "leaf_count": seen.leaf_count, "title": seen.title,
                        "etag": seen.etag}
        if recent:
            row.recheck = True
        self.db.commit()
        _publish(self.db, row.playlist_id, row)

    def _recent(self, seen: Listed) -> bool:
        now = self.source.clock
        return now is not None and seen.updated_at >= now - RECENT

    def _info(self, key: str) -> Listed:
        """The playlist as the server's list gives it: from the last look while that is fresh (a sync asks for the
        whole list only when the look did not have it), else asked."""
        if self.made is not None and self.made.key == key:
            return self.made
        found = _listed(self.server_id, key)
        return found if found is not None else self.source.meta(key)

    def _rescue(self, playlist_id: int) -> None:
        """Before the copy on the server goes with a playlist moved to the trash: what was put in there since the last
        sync comes along into the trash (at the end), so that bringing it back brings everything back."""
        from . import playlists

        db = self.db
        key = self.row.ext_key
        assert key is not None
        try:
            remote = self._items(key)
        except PlexError as exc:
            if exc.code in GONE:
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
        logger.info("Entries added on the server kept with the playlist in the trash playlist=%s count=%s",
                    playlist_id, len(items))

    # --- Another server

    def _settle_tracks(self, playlist_id: int, *, switched: bool) -> None:
        """The entries' tracks as this server knows them. A number names a thing of one server only, and an entry that
        came from another server holds that server's number. Checked are all entries after a change of server, else
        the ones put in before the server was connected (they may come from another server). Each keeps its track when
        this server's catalogue has it as the same track (the same ``guid``, or the same title and album); else it is
        found by a sure step of ``match.py`` (the recording's id, MusicBrainz, title with album or with length); what
        is found nowhere, or only by the song or the title, stays in nexmix only, and its entry says so. What a server
        refused before is offered to this one anew. The row's ``settle`` goes in the same step."""
        from . import playlists

        db = self.db
        db.rollback()
        # The write lock first: nothing changes the entries while they are settled.
        db.execute(update(Playlist).where(Playlist.id == playlist_id)
                   .values(version=Playlist.version + 1, updated_at=clock.now()))
        items = playlists.live_items(db, playlist_id)
        since = self.connected_at
        checked = [item for item in items if switched or (since is not None and item.added_at < since)]
        row = self.fresh_row()
        if not checked and not any(item.plex_hint == HINT_REFUSED for item in items):
            row.settle = ""
            db.commit()
            return
        catalogue = _catalogue(db, self.server_id)
        kept: set[int] = set()
        moving: dict[int, match.Candidate] = {}
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
            found = catalogue.find(_wanted(item), used)
            # Only a sure step takes another track here: by the song or the title alone it may be somebody else's song
            # of the same name, which would then go to the server unasked. Those stay with the hint.
            if found is not None and found.sure:
                used.add(found.track.id)
                moving[item.id] = found.track
            else:
                hints[item.id] = HINT_ELSEWHERE
        by_id = {item.id: item for item in items}
        for item in items:
            # Found here, or kept: nothing to say; refused by the other server: offered to this one.
            hints.setdefault(item.id, "" if item.plex_hint == HINT_REFUSED or item.id in moving
                             or item.track_id in kept else item.plex_hint)
        hints = {ident: hint for ident, hint in hints.items() if by_id[ident].plex_hint != hint}
        row.settle = ""
        if not moving and not hints:
            db.commit()
            return
        if moving:
            # Two steps: a track another entry still holds is let go of first (one track once per playlist).
            for ident in moving:
                by_id[ident].track_id = -ident
            db.flush()
            for ident, found in moving.items():
                item = by_id[ident]
                item.track_id, item.album_id, item.guid = found.id, catalogue.albums.get(found.id, 0), found.guid
        for ident, hint in hints.items():
            by_id[ident].plex_hint = hint
        db.commit()
        logger.info("Playlist entries settled on this server playlist=%s server=%s found=%s not_here=%s", playlist_id,
                    self.server_id, len(moving), sum(1 for hint in hints.values() if hint == HINT_ELSEWHERE))
        publish_items(db, playlist_id, [], changed=[by_id[ident] for ident in {*moving, *hints}])

    # --- Deleted on the server

    def _vanished(self, playlist_id: int) -> None:
        """The server answered 404 for the playlist or its entries: deleted there? Only when its list, read again now
        (200), lacks it, no other playlist of this server is missing from that list (``MASS_GONE``: a server that lost
        its playlists, a user gone, a library made anew is no deletion), and the server says of the playlist itself
        that it is gone (``gone``). Else the sync fails with a code and the playlist stays as it is: in nexmix, with
        its invitations, its copy on the server known by its key (never taken in again as a new one)."""
        key = self.row.ext_key
        assert key is not None
        self.db.rollback()
        # A 404 to the list itself, or a user gone, fails here with its own code.
        listed = self.source.listing()
        _remember(self.server_id, listed)
        there = {one.key for one in listed}
        if key in there:
            raise PlexError(UNREADABLE)
        missing = _missing_from(self.db, self.server_id, there)
        self.db.rollback()
        if len(missing) >= MASS_GONE:
            _note_missing(self.server_id, len(missing))
            raise PlexError(PLAYLISTS_NOT_FOUND, 404)
        if not self.source.gone(key):
            raise PlexError(UNREADABLE)
        self._gone(playlist_id)

    def _gone(self, playlist_id: int) -> None:
        """Deleted on the server (``_vanished`` made sure): to the trash. The copy keeps the key it had there as one
        left behind (``resume``): the server's list never brings it in again as a new playlist, and brought back from
        the trash it is taken up again when the server has it after all, else made anew."""
        db = self.db
        db.rollback()
        if self.mirror:
            # A mirror deleted on its server: only there. The playlist stays, the mirror says so.
            row = self.fresh_row()
            self._forget_copy(row)
            row.gone_there, row.resume, row.state, row.error, row.due_at = True, False, ERROR, mirrors.MIRROR_GONE, None
            db.commit()
            logger.info("Mirror deleted on its server playlist=%s server=%s", playlist_id, self.server_id)
            _publish(db, playlist_id, row)
            mirrors._tell(db, playlist_id, "mirror_gone")
            return
        now = clock.now()
        taken = db.execute(update(Playlist).where(Playlist.id == playlist_id, Playlist.deleted_at.is_(None))
                           .values(version=Playlist.version + 1, deleted_at=now, updated_at=now)).rowcount
        row = self.fresh_row()
        key, guid = row.ext_key, row.guid
        self._forget_copy(row)
        row.ext_key, row.guid, row.resume = key, guid, key is not None
        # A smart one keeps the entries nexmix has of it: those come back if it is brought back.
        row.gone_there, row.smart_total = True, None
        self._done(None)
        if taken:
            version = db.scalar(select(Playlist.version).where(Playlist.id == playlist_id)) or 0
            logger.info("Playlist deleted on the server, moved to the trash playlist=%s", playlist_id)
            hub.publish(playlist_topic(playlist_id), "playlist",
                        {"version": version, "op": "deleted", "by": None, "plex": True})
            # As in the trash from nexmix: its invitation for guests ends, brought back it lets nobody in again.
            from . import guests

            guests.end_with_playlist(db, playlist_id)

    # --- Smart playlists: read only

    def _smart(self, playlist_id: int) -> None:
        from . import playlists

        db = self.db
        key = self.row.ext_key
        assert key is not None
        try:
            info = self._info(key)
            found, total = self.source.smart_items(key)
        except PlexError as exc:
            if exc.code in GONE:
                self._vanished(playlist_id)
                return
            raise
        entries = _numbered(self.server_id, found, by_track=self.by_track)
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
        """Made on the server, empty; or, when the answer of an earlier try was lost (a fault or a stop between the
        server making it and nexmix keeping its key), the one made then: empty, not smart, of the name it was made
        with, of a key and ``guid`` no copy on this server has, the connected user's own (Jellyfin lists other users'
        public playlists too: ``owned``), and made since that try (``_made_then``). Never anybody else's."""
        db = self.db
        row = self.fresh_row()
        name = db.scalar(select(Playlist.name).where(Playlist.id == playlist_id)) or self.via
        adopted: Listed | None = None
        if row.creating_at is not None:
            asked = (row.creating_title or name)[:200]
            waited = (clock.now() - row.creating_at).total_seconds()
            known = db.execute(select(ServerPlaylist.ext_key, ServerPlaylist.guid).where(
                ServerPlaylist.server_id == self.server_id, ServerPlaylist.ext_key.is_not(None))).all()
            keys = {key for key, _guid in known}
            guids = {guid for _key, guid in known if guid}
            db.rollback()
            listed = self.source.listing()
            server_now = self.source.clock
            # A name nobody else had when it was asked for (``unique_names``) says it is the one, without a time.
            unique = self.caps.unique_names and waited <= ADOPT_UNIQUE
            chosen = [one for one in listed
                      if not one.smart and one.title == asked and one.leaf_count == 0
                      and one.key not in keys and one.guid not in guids
                      and (_made_then(one, server_now, waited) or (unique and not one.created))]
            if self.caps.unique_names and chosen:
                # Music Assistant's list tells no count: only a playlist read empty now is the one made then, and only
                # when there is just one (somebody may have made one of that name meanwhile).
                chosen = [one for one in chosen if self._empty_there(one.key)]
                if len(chosen) != 1:
                    chosen = []
            ours = set(self.source.owned([one.key for one in chosen])) if chosen else set()
            adopted = next((one for one in chosen if one.key in ours), None)
            row = self.fresh_row()
        if adopted is None:
            title = self.source.unique_title(name) if self.caps.unique_names else name
            row = self.fresh_row()
            row.creating_at = clock.now()
            row.creating_title = title[:200]
            db.commit()
            adopted = self.source.create(title)
            logger.info("Playlist made on the server playlist=%s server=%s", playlist_id, self.server_id)
        else:
            logger.info("Playlist made on the server earlier taken playlist=%s server=%s", playlist_id,
                        self.server_id)
        row = self.row
        row.ext_key, row.guid, row.creating_at, row.creating_title = adopted.key, adopted.guid, None, ""
        # A name made unique there stands for nexmix's own (``_same_title``).
        row.base = {"title": name if self.caps.unique_names else (adopted.title or name), "items": []}
        row.writing = None
        self.made = adopted
        db.commit()

    def _empty_there(self, key: str) -> bool:
        """Whether the server's playlist holds nothing, read now (gone meanwhile: not one to take)."""
        try:
            return bool(self.source.empty(key))
        except PlexError as exc:
            if exc.code in GONE:
                return False
            raise

    def _read(self, playlist_id: int) -> tuple[Listed, list[Item]] | None:
        key = self.row.ext_key
        assert key is not None
        try:
            info = self._info(key)
            entries = self._items(key)
        except PlexError as exc:
            if exc.code in GONE:
                self._vanished(playlist_id)
                return None
            raise
        return info, entries

    def _snapshot(self, playlist_id: int) -> Snapshot | None:
        from . import playlists

        playlist = self.db.get(Playlist, playlist_id, populate_existing=True)
        if playlist is None or playlist.deleted_at is not None:
            return None
        if (playlist.source_server_id == self.server_id) == self.mirror:
            # The playlist moved to another server while this sync ran: the copy plays another part now.
            self.role_changed = True
            return None
        items = playlists.live_items(self.db, playlist_id)
        if self.mirror:
            assert playlist.source_server_id is not None
            side = self.side = mirrors.side(self.db, self.ident, items, int(playlist.source_server_id))
            mapped = [(side.track_of[item.id], item.id) for item in items if side.track_of.get(item.id) is not None]
            return Snapshot(version=playlist.version, name=playlist.name,
                            entries=[(int(track), ident) for track, ident in mapped if track is not None],
                            hints={int(side.track_of[item.id] or 0): side.hint_of[item.id] for item in items
                                   if item.id in side.hint_of and side.track_of.get(item.id) is not None})
        return Snapshot(version=playlist.version, name=playlist.name,
                        entries=[(item.track_id, item.id) for item in items],
                        hints={item.track_id: item.plex_hint for item in items if item.plex_hint})

    def _sync(self, playlist_id: int) -> None:
        db = self.db
        if self.row.ext_key is None:
            self._make(playlist_id)
        self._await_tasks()
        self._finish_rename()
        self._restore()
        if self.mirror:
            mirrors.prepare(self, playlist_id)
        else:
            # Tracks dropped from a mirror that became the source before they were taken out there.
            mirrors.drop_discarded(self, playlist_id)
        found = self._read(playlist_id)
        if found is None:
            return
        info, remote = found
        if info.smart and self.mirror:
            # Made a smart playlist on the mirror's server meanwhile: no mirror can be kept there.
            raise PlexError("mirror_smart")
        if self.mirror and self._emptied(playlist_id):
            return
        if info.smart:
            # Made a smart playlist on the server meanwhile: read only from now on.
            self.row.smart = True
            db.commit()
            self._smart(playlist_id)
            return
        plan: Plan | None = None
        for _attempt in range(MERGE_TRIES):
            db.rollback()
            snapshot = self._snapshot(playlist_id)
            if snapshot is None:
                # Deleted meanwhile (the next sync takes it off the server), or moved to another server meanwhile (the
                # next sync takes it in its new part).
                db.rollback()
                mark(db, playlist_id, at_once=True)
                if self.role_changed:
                    self.row.due_at, self.row.state = clock.now(), PENDING
                db.commit()
                return
            plan = self._plan(snapshot, info, remote)
            if self.held_out:
                # Tracks only in the mirror: out of everything this sync writes.
                remote = [entry for entry in remote if entry.track not in self.held_out]
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

    def _emptied(self, playlist_id: int) -> bool:
        """A mirror emptied on its server (it holds nothing, the state both sides last agreed on held entries nexmix
        still has): never taken over by itself, neither into nexmix nor to the source or the other mirrors. The mirror
        waits with its note (``emptied``) until somebody answers "empty it here too" or "keep" (``mirrors.emptied``);
        meanwhile nothing is written there. A mirror filled again there goes on as before. Returns whether it waits."""
        from . import playlists

        db = self.db
        row = self.fresh_row()
        note = dict(row.note) if isinstance(row.note, dict) else {}
        if self.raw_tracks:
            if note.pop("emptied", None) is not None:
                row.note = note or None
                db.commit()
                logger.info("Mirror filled again on its server playlist=%s server=%s", playlist_id, self.server_id)
                mirrors._tell(db, playlist_id, "mirror_filled")
            return False
        new = note.get("emptied") is None
        if new:
            writing = row.writing if isinstance(row.writing, dict) else None
            agreed = {kept.track for kept in _base_of(None, row.base)} - _unwritten(writing)
            items = playlists.live_items(db, playlist_id)
            playlist = db.get(Playlist, playlist_id)
            source = int(playlist.source_server_id or 0) if playlist is not None else 0
            side = mirrors.side(db, self.ident, items, source)
            here = {track for track in side.track_of.values() if track is not None}
            lost = len(agreed & here)
            db.rollback()
            if not lost:
                return False
            row = self.fresh_row()
            note["emptied"] = {"at": clock.now().isoformat(), "count": lost}
            row.note = note
            logger.warning("Mirror emptied on its server, nothing taken over playlist=%s server=%s count=%s",
                           playlist_id, self.server_id, lost)
        row.state, row.error, row.tries, row.due_at, row.synced_at = SYNCED, "", 0, None, clock.now()
        db.commit()
        if new:
            mirrors._tell(db, playlist_id, "mirror_emptied")
        return True

    def _held_back(self, snapshot: Snapshot, remote_tracks: set[int]) -> set[int]:
        """Tracks of nexmix not offered to the server this time: refused by it and not due to be tried again, refused
        for good, or not on this server. One the server holds after all is not held back."""
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

    def _alive(self, tracks: Sequence[int]) -> set[int]:
        """Which of the tracks the server's library still has, asked of the server itself (one question per sync for
        each track, in parts: ``existing``), never taken from nexmix's catalogue, which may be older or newer. A fault
        of the question is raised: then nothing is taken, and the sync is tried again later."""
        asking = [track for track in dict.fromkeys(tracks) if track not in self.asked]
        if asking:
            keys = _keyed(self.server_id, asking)
            there = self.source.existing(keys.values()) if keys else set()
            for track in asking:
                self.asked[track] = track in keys and keys[track] in there
        return {track for track in tracks if self.asked.get(track)}

    def _plan(self, snapshot: Snapshot, info: Listed, remote: list[Item]) -> Plan:
        from . import playlists

        row = self.row
        # A write is open when it names what it was to bring (a step by place alone keeps only what must stay).
        writing = row.writing if isinstance(row.writing, dict) and "items" in row.writing else None
        base = _base_of(writing, row.base)
        local_tracks = snapshot.tracks
        remote_tracks = {entry.track for entry in remote}
        # Only ids the server gives in order show that a request reached it; a track's own id (Jellyfin) says nothing.
        sent = set() if self.by_track else _sent(writing, {entry.track: entry.item for entry in remote})
        unwritten = _unwritten(writing) - sent
        known = playlists.catalogue_of(self.db, local_tracks, server_id=self.server_id)
        # Tracks nexmix holds that the server cannot: unknown to its catalogue and not in its playlist, and the ones
        # held back (refused by it, not on this server, missing there already).
        unknown = {track for track in local_tracks if track not in known and track not in remote_tracks}
        held_back = self._held_back(snapshot, remote_tracks)
        base_ids = {kept.track for kept in base}
        # Taken out on the server, or gone from its library? The server is asked, whatever nexmix's catalogue says of
        # the track: what is gone there stays in nexmix. (A track an open write had still to add is neither: the
        # server never had it.)
        lost = [track for track in local_tracks if track in base_ids and track not in remote_tracks
                and track not in held_back and track not in unwritten]
        missing: set[int] = set()
        loss = 0
        if lost:
            alive = self._alive(lost)
            gone = {track for track in lost if track not in alive}
            missing |= gone
            if len(lost) - len(gone) and _mass(len(lost), len(gone)):
                # Many at once while the library lost tracks too: nothing of it is taken.
                missing |= set(lost)
                loss = len(lost)
        held = (unknown - set(lost)) | held_back | missing
        local_entries = [entry for entry in snapshot.entries if entry[0] not in held]
        merged = merge.merge(
            [kept for kept in base if kept.track not in held], local_entries,
            [(entry.track, entry.item) for entry in remote],
            remote_order_counts=writing is None, remote_unwritten=unwritten,
        )
        back: mirrors.Back | None = None
        if self.mirror:
            # Tracks put into the mirror: entries where the source has them (by a sure step), else only there.
            back = mirrors.back(self, int(row.playlist_id or 0), remote, merged.local_adds)
            if back.extras or back.links:
                extra = {entry.track for entry in back.extras}
                self.held_out |= extra
                remote = [entry for entry in remote if entry.track not in extra]
                local_entries = [*local_entries, *((track, entry) for entry, (track, _how) in back.links.items())]
                merged = merge.merge(
                    [kept for kept in base if kept.track not in held and kept.track not in extra], local_entries,
                    [(entry.track, entry.item) for entry in remote],
                    remote_order_counts=writing is None, remote_unwritten=unwritten,
                )
        stored = writing if writing is not None else (row.base if isinstance(row.base, dict) else {})
        base_title = stored.get("title") if isinstance(stored.get("title"), str) else None
        remote_title = info.title
        if base_title is not None and remote_title != base_title and self._same_title(base_title, remote_title):
            # The name made unique there is nexmix's own.
            remote_title = base_title
        if writing is not None and base_title is not None and remote_title == writing.get("from_title"):
            # The server has the name it had when the open write began: the new one may not have gone yet.
            remote_title = base_title
        title = merge.name(base_title, snapshot.name, remote_title)
        local_order = merge.place(merged.order, [track for track in local_tracks if track in held], local_tracks)
        return Plan(merged=merged, title=title, local_order=local_order, held=held,
                    base_local={kept.track: kept.local for kept in base},
                    unhint={track for track in snapshot.hints if track in remote_tracks},
                    missing={track for track in missing if snapshot.hints.get(track) != HINT_MISSING}, loss=loss,
                    back=back, remote=list(remote))

    def _new_items(self, playlist_id: int, by_track: dict[int, Item], tracks: Sequence[int]) -> list[PlaylistItem]:
        """Entries for tracks that came through the server: what the catalogue says of them, else what the server
        said."""
        from . import playlists

        known = playlists.catalogue_of(self.db, tracks, server_id=self.server_id)
        now = clock.now()
        new: list[PlaylistItem] = []
        for track in tracks:
            entry = by_track[track]
            found = known.get(track)
            item = PlaylistItem(
                playlist_id=playlist_id, track_id=track, position=0, added_by_user=None, added_by_name=self.via,
                added_at=now, from_plex=True, title=(found.title if found else entry.title)[:500],
                artist=(found.artist if found else entry.artist)[:500],
                album=(found.album if found else entry.album)[:500],
                album_id=(found.album_id if found else entry.album_id) or 0,
                year=found.year if found else entry.year, duration=found.duration if found else entry.duration,
                guid=((found.guid if found else "") or entry.guid)[:200],
            )
            self.db.add(item)
            new.append(item)
        if new:
            self.db.flush()
        if any(track not in known for track in tracks):
            self.unknown = True
        return new

    def _apply(self, playlist_id: int, snapshot: Snapshot, info: Listed, remote: list[Item], plan: Plan) -> bool:
        """nexmix's side of the merge, in one transaction that holds only when nobody changed the playlist since it was
        read (else False: merged again). What is to be written goes along (``writing``): the order, what the server is
        to lose and to get, and the ids it had, so that a write that breaks off is finished by the next sync instead of
        being read as changes made on the server."""
        from . import playlists

        if self.mirror:
            return self._apply_mirror(playlist_id, snapshot, info, remote, plan)
        db = self.db
        merged = plan.merged
        changes = bool(merged.local_adds or merged.local_removes or plan.local_order != snapshot.tracks
                       or plan.title != snapshot.name or plan.unhint or plan.missing)
        entries_change = bool(merged.local_adds or merged.local_removes or plan.local_order != snapshot.tracks
                              or plan.title != snapshot.name)
        needs_write = merged.order != [entry.track for entry in remote] or \
            not self._same_title(plan.title, info.title)
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
            for track in plan.missing:
                if track in by_track and by_track[track].plex_hint != HINT_MISSING:
                    by_track[track].plex_hint = HINT_MISSING
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
            row.note = {**(row.note if isinstance(row.note, dict) else {}), "order": merged.conflict,
                        "at": now.isoformat()}
        if plan.loss:
            row.note = {**(row.note if isinstance(row.note, dict) else {}),
                        "loss": {"count": plan.loss, "at": now.isoformat()}}
        row.writing = self._open(plan, ids, remote, info.title) if needs_write else None
        # A refusal is forgotten once the server holds the track or nexmix no longer does.
        remote_tracks = {entry.track for entry in remote}
        keep = set(plan.local_order) - remote_tracks
        records = row.refused if isinstance(row.refused, dict) else {}
        if any(_maybe(key) not in keep for key in records):
            row.refused = {key: value for key, value in records.items() if _maybe(key) in keep} or None
        self.entry_ids = ids
        db.commit()
        if changes:
            publish_items(db, playlist_id, new, removed=[item.id for item in going], changed=changed)
            if entries_change:
                # The mirrors get the change.
                mark(db, playlist_id, but=self.ident)
                db.commit()
            if self.unknown:
                from . import catalog

                catalog.request_partial(db, "playlist", self.server_id)
        if merged.conflict or plan.loss:
            _publish(db, playlist_id, self.row)
        if plan.missing:
            logger.info("Entries the server lost kept in nexmix playlist=%s count=%s at_once=%s", playlist_id,
                        len(plan.missing), plan.loss)
        return True

    def _apply_mirror(self, playlist_id: int, snapshot: Snapshot, info: Listed, remote: list[Item],
                      plan: Plan) -> bool:
        """A mirror's side of the merge (``mirrors.apply``), and the write to come, kept as for the source."""
        db = self.db
        held, ids = mirrors.apply(self, playlist_id, snapshot, plan)
        if not held:
            return False
        merged = plan.merged
        needs_write = merged.order != [entry.track for entry in remote] or \
            not self._same_title(plan.title, info.title)
        db.rollback()
        row = self.fresh_row()
        if plan.loss:
            row.note = {**(row.note if isinstance(row.note, dict) else {}),
                        "loss": {"count": plan.loss, "at": clock.now().isoformat()}}
        row.writing = self._open(plan, ids, remote, info.title) if needs_write else None
        remote_tracks = {entry.track for entry in remote}
        keep = set(merged.order) - remote_tracks
        records = row.refused if isinstance(row.refused, dict) else {}
        if any(_maybe(key) not in keep for key in records):
            row.refused = {key: value for key, value in records.items() if _maybe(key) in keep} or None
        self.entry_ids = ids
        db.commit()
        return True

    def _open(self, plan: Plan, ids: dict[int, int], read: list[Item], from_title: str, *,
              before: dict[int, int | None] | None = None) -> dict[str, Any]:
        """The write about to begin, as it is kept until it is done: the name and order meant, each entry with the id
        the server has for it (none: the server is to get it, ``add``), and the entries the server is to lose with
        their ids (``remove``). ``before``: the server's ids when the sync read it; a track the server had then and
        lacks now, though this sync did not add it, was taken out there meanwhile and keeps its old id."""
        order = plan.merged.order
        wanted = set(order)
        there = {entry.track: entry.item for entry in read}
        earlier = before or {}

        def server_id_of(track: int) -> int | None:
            if track in there:
                return there[track]
            return earlier.get(track) if track not in self.added else None

        return {
            "title": plan.title,
            "from_title": from_title,
            "items": [[track, ids.get(track), server_id_of(track)] for track in order],
            "remove": [[entry.track, entry.item, plan.base_local.get(entry.track)] for entry in read
                       if entry.track not in wanted and entry.item is not None],
            "add": [track for track in order if server_id_of(track) is None],
            "order": list(order),
        }

    def _write(self, info: Listed, remote: list[Item], plan: Plan) -> tuple[list[Item], bool, bool]:
        """The server's side: the name, out what goes, in what comes (at the end), then the moves. Read again after
        each part, and the moves planned again from what was read until the order is the wanted one. At most
        ``MOVE_PART`` moves: the third value says more are to come (the write stays open)."""
        source, key = self.source, self.row.ext_key
        assert key is not None
        target = plan.merged.order
        wanted = set(target)
        wrote = False
        by_place = self.caps.moves == "tail"
        if not self._same_title(plan.title, info.title):
            if self.caps.rename == "copy":
                key = self._rename_copy(key, plan.title)
            else:
                source.rename(key, plan.title)
            wrote = True
        going = [entry.ref for entry in remote if entry.track not in wanted and entry.item is not None and entry.ref]
        if going:
            if by_place:
                source.remove_many(key, going, guard=self._guard, expected=self._expected(),
                                   noticed=self._concurrent)
            else:
                source.remove_many(key, going)
            wrote = True
        present = {entry.track for entry in remote if entry.track in wanted}
        adding = [track for track in target if track not in present]
        self.added = set(adding)
        if adding:
            keys = _keyed(self.server_id, adding)
            self.adding_keys = {key_of: track for track, key_of in keys.items()}
            sending = [keys[track] for track in adding if track in keys]
            if len(sending) < len(adding):
                logger.warning("Tracks without a key on the server left out count=%s server=%s",
                               len(adding) - len(sending), self.server_id)
            took = source.add(key, sending, answered=self._part_added)
            if took < len(adding):
                logger.info("The server took fewer tracks than asked asked=%s took=%s", len(adding), took)
            wrote = True
        fresh = self._items(key) if wrote else list(remote)
        if adding:
            # What the server holds now is no longer to be added, whatever happens next: the server lacking it from
            # here on means it was taken out there.
            self._narrow({entry.track: entry.item for entry in fresh if entry.track in self.added})
        moved_count = 0
        if by_place:
            here = {entry.track for entry in fresh}
            if [entry.track for entry in fresh] != [track for track in target if track in here]:
                refs = {entry.track: entry.ref for entry in fresh}
                source.arrange(key, [refs[track] for track in target if track in refs], guard=self._guard,
                               expected=self._expected(), noticed=self._concurrent)
                wrote = True
                fresh = self._items(key)
        for _attempt in range(0 if by_place else MOVE_TRIES):
            steps = merge.moves([(entry.item, entry.track) for entry in fresh if entry.item is not None], target)
            if not steps:
                break
            wrote = True
            more = moved_count + len(steps) > MOVE_PART
            chosen = steps[:MOVE_PART - moved_count]
            refs = {entry.item: entry.ref for entry in fresh if entry.item is not None}
            source.reorder(key, [(refs[item], refs[after] if after is not None else None) for item, after in chosen])
            moved_count += len(chosen)
            fresh = self._items(key)
            if more:
                return fresh, wrote, bool(merge.moves(
                    [(entry.item, entry.track) for entry in fresh if entry.item is not None], target))
        if wrote and self.by_track:
            # Jellyfin's ``Etag`` is new at once: taken before the last read, so a change made there after this one
            # still differs from it at the next look.
            self.seen_after = source.meta(key)
            fresh = self._items(key)
        return fresh, wrote, False

    def _expected(self) -> list[str]:
        """The server's playlist as this sync last read it (what a step by place starts from), without what a new name
        by copy left behind."""
        return [ref for ref in self.raw_order if ref not in self.left_keys]

    def _concurrent(self, count: int) -> None:
        """Somebody changed the server's playlist while it was written by place, and ``count`` entries added there
        meanwhile may be gone (Music Assistant names no entry, nexmix never saw them): the playlist says so
        (``note["concurrent"]``) until somebody looked (``drop_note``). Never lost without a word."""
        db = self.db
        db.rollback()
        row = self.fresh_row()
        note = dict(row.note) if isinstance(row.note, dict) else {}
        before = note.get(CONCURRENT) if isinstance(note.get(CONCURRENT), dict) else {}
        note[CONCURRENT] = {"at": clock.now().isoformat(), "count": int(before.get("count") or 0) + max(count, 1)}
        row.note = note
        db.commit()
        logger.warning("Entries added on the server while nexmix wrote by place may be gone playlist=%s server=%s "
                       "count=%s", row.playlist_id, self.server_id, count)
        if row.playlist_id is not None:
            hub.publish(playlist_topic(row.playlist_id), "plex", view(db, row.playlist_id))

    def _writing_part(self, name: str, value: Any) -> None:
        """One part of the open write set (None: taken away), in a transaction of its own, committed at once."""
        db = self.db
        db.rollback()
        row = self.fresh_row()
        writing = dict(row.writing) if isinstance(row.writing, dict) else {}
        if value is None:
            if name not in writing:
                db.rollback()
                return
            writing.pop(name, None)
        else:
            writing[name] = value
        row.writing = writing or None
        db.commit()

    def _guard(self, entries: list[Any] | None) -> None:
        """What must still be in the server's playlist after a step that takes entries out by place (None: the step is
        done). Kept with the open write until then, so that a step broken off half way is put right (``_restore``)."""
        self._writing_part("restore", list(entries) if entries is not None else None)

    def _await_tasks(self) -> None:
        """Tasks of Music Assistant nobody saw end (``_task_left``): each asked again. While one may still run,
        nothing is written to the playlist (it would take out by place whatever stands there when it runs) and nothing
        is taken from it into nexmix: what the playlist holds now joins what must be there afterwards (``restore``,
        except what the task meant to take out), and the sync waits (``ma_task_open``). Once all ended, ``_restore``
        puts back whatever they took that was not meant: never read as taken out there."""
        row = self.fresh_row()
        writing = dict(row.writing) if isinstance(row.writing, dict) else {}
        tasks = [dict(one) for one in writing.get("tasks") or [] if isinstance(one, dict) and one.get("id")]
        key = row.ext_key
        playlist_id = row.playlist_id
        self.db.rollback()
        if not tasks:
            return
        still = [one for one in tasks if not self.source.task_ended(str(one["id"]))]
        if not still:
            self._writing_part("tasks", None)
            logger.info("Music Assistant tasks left open have ended playlist=%s server=%s", playlist_id,
                        self.server_id)
            return
        meant = {str(name) for one in still for name in one.get("meant") or []}
        try:
            there = self.source.kept_now(key) if key is not None else []
        except PlexError as exc:
            if exc.code not in GONE:
                raise
            there = []
        kept = [one for one in writing.get("restore") or [] if isinstance(one, list | tuple) and len(one) == 2]
        known = {str(one[0]) for one in kept}
        kept += [list(one) for one in there if str(one[0]) not in known and str(one[0]) not in meant]
        self._writing_part("restore", kept)
        self._writing_part("tasks", still)
        raise MaError(TASK_OPEN, 503)

    def _restore(self) -> None:
        """A step by place broken off half way: whatever of what had to stay the playlist lacks is put back."""
        row = self.fresh_row()
        writing = row.writing if isinstance(row.writing, dict) else None
        kept = writing.get("restore") if writing is not None else None
        key = row.ext_key
        self.db.rollback()
        if not isinstance(kept, list) or key is None:
            return
        put = self.source.restore(key, kept)
        if put:
            logger.warning("Entries a broken-off step had taken out put back playlist=%s server=%s count=%s",
                           row.playlist_id, self.server_id, put)
        self._guard(None)

    def _rename_copy(self, old: str, title: str) -> str:
        """A new name where the server cannot rename (Music Assistant): a new playlist under the new name (unique
        there), everything copied over in order, the copy's key taken, the old one deleted. ``writing["renaming"]``
        names both meanwhile (``take_in`` takes neither for a new one or a deleted one). Returns the new key."""
        source = self.source
        unique = source.unique_title(title)
        self._writing_part("renaming", {"from": old, "title": unique, "key": None})
        made = source.create(unique)
        self._writing_part("renaming", {"from": old, "title": unique, "key": made.key})
        # What Music Assistant passes over (a file it no longer knows) stays behind; never tried again.
        lacking = source.copy(old, made.key)
        passed = {one.ident for one in lacking}
        # Added in Music Assistant to the old one meanwhile: along into the new one, before the old one goes.
        source.top_up(old, made.key, passed)
        db = self.db
        db.rollback()
        row = self.fresh_row()
        row.ext_key = made.key
        writing = dict(row.writing) if isinstance(row.writing, dict) else {}
        writing["renaming"] = {"from": old, "title": unique, "key": made.key, "swapped": True}
        row.writing = writing
        db.commit()
        self._left_behind([one.track for one in lacking if one.track is not None])
        self._delete_after_top_up(old, made.key, passed)
        self._writing_part("renaming", None)
        logger.info("Playlist renamed on the server by a copy playlist=%s server=%s", row.playlist_id, self.server_id)
        return made.key

    def _left_behind(self, keys: list[str]) -> None:
        """Tracks of the library a new name by copy left behind (Music Assistant passed them over): they stay in nexmix
        with the note that the server lacks them (``HINT_MISSING``), out of the common state, never written again by
        this sync (written back after the next run of the catalogue, as any entry the server lost)."""
        if not keys:
            return
        db = self.db
        db.rollback()
        numbers = media.known_numbers(db, self.server_id, media.TRACK, keys)
        tracks = sorted({int(number) for number in numbers.values()})
        db.rollback()
        if not tracks:
            return
        self.left_in_copy |= set(tracks)
        self.left_keys |= {str(key) for key, number in numbers.items() if int(number) in self.left_in_copy}
        row = self.fresh_row()
        playlist_id = row.playlist_id
        logger.warning("Tracks the server passed over in a new name by copy kept as missing playlist=%s count=%s",
                       playlist_id, len(tracks))
        if self.mirror:
            mirrors.missing(db, self.ident, tracks)
            return
        if playlist_id is None:
            return
        taken = db.execute(update(PlaylistItem).where(
            PlaylistItem.playlist_id == playlist_id, PlaylistItem.track_id.in_(tracks),
            PlaylistItem.removed_at.is_(None)).values(plex_hint=HINT_MISSING)).rowcount
        if not taken:
            db.rollback()
            return
        db.execute(update(Playlist).where(Playlist.id == playlist_id)
                   .values(version=Playlist.version + 1, updated_at=clock.now()))
        db.commit()
        changed = list(db.scalars(select(PlaylistItem).where(
            PlaylistItem.playlist_id == playlist_id, PlaylistItem.track_id.in_(tracks),
            PlaylistItem.removed_at.is_(None))))
        publish_items(db, playlist_id, [], changed=changed)

    def _delete_after_top_up(self, going: str, staying: str, passed: Iterable[str] = ()) -> None:
        """``going`` deleted on the server, after what it holds that ``staying`` lacks went into ``staying`` (looked at
        once more right before: somebody may add to it in Music Assistant until it is gone; ``passed``: what Music
        Assistant passed over in the copy, not tried again). One gone already is fine."""
        try:
            for _try in range(3):
                if not self.source.top_up(going, staying, passed):
                    break
        except PlexError as exc:
            if exc.code not in GONE:
                raise
        self.source.delete(going)

    def _finish_rename(self) -> None:
        """A new name by copy broken off half way (``_rename_copy``): after the swap, the old playlist goes; before it,
        the copy goes and the old one stands (the name is written again by this sync). A copy whose key was never
        heard of goes only when it is empty (it is found by its name, which was nobody else's)."""
        row = self.fresh_row()
        writing = row.writing if isinstance(row.writing, dict) else None
        marker = writing.get("renaming") if writing is not None else None
        current = row.ext_key
        known = set(self.db.scalars(select(ServerPlaylist.ext_key).where(
            ServerPlaylist.server_id == self.server_id, ServerPlaylist.ext_key.is_not(None))))
        self.db.rollback()
        if not isinstance(marker, dict):
            return
        old, new = str(marker.get("from") or ""), str(marker.get("key") or "")
        if marker.get("swapped"):
            if old and old != current and current is not None:
                self._delete_after_top_up(old, current)
        elif new and new != current and current is not None:
            # The old one stands: whatever was added to the half copy in Music Assistant goes into it first.
            self._delete_after_top_up(new, current)
        elif not new:
            title = str(marker.get("title") or "")
            for one in self.source.listing():
                if one.title == title and one.key not in known and not self.source.raw(one.key):
                    self.source.delete(one.key)
        self._writing_part("renaming", None)
        logger.info("A new name by copy broken off was finished playlist=%s server=%s", row.playlist_id,
                    self.server_id)

    def _part_added(self, part: list[str], took: int | None) -> None:
        """The server answered a part of the tracks to add. When it says it took all of them, they are there: the open
        write no longer names them as to be added (a write broken off after this, and the track taken out on the
        server before the next sync, is a track taken out there, not one still to add). Took it fewer, which ones is
        not known until the server is read again (``_narrow``)."""
        if took is not None and took >= len(part):
            self._narrow(dict.fromkeys(self.adding_keys[key] for key in part if key in self.adding_keys))

    def _narrow(self, there: dict[int, int | None]) -> None:
        """The open write without ``there`` among the tracks still to add; where the server's id is known, the entry
        keeps it. In a transaction of its own, committed at once."""
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

    def _go_on(self, plan: Plan, remote: list[Item], fresh: list[Item]) -> None:
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
        _remember_title(self.server_id, row.ext_key, plan.title)
        logger.info("Playlist order written in part playlist=%s", row.playlist_id)
        _publish(db, row.playlist_id, row)

    def _keep(self, plan: Plan, fresh: list[Item], info: Listed, wrote: bool) -> None:
        """The new common state: what was meant, with the server's ids as just read. A track just added that the
        server did not take stays out of it and is offered again later, less and less often (``REFUSED_WAITS``), then
        no more; one the server lost since it was read stays in (it was taken out there)."""
        db = self.db
        db.rollback()
        there = {entry.track: entry.item for entry in fresh}
        refused = [track for track in self.added if track not in there]
        alive = set(refused)
        if refused:
            alive = self._alive(refused)
            if alive != set(refused):
                # The server no longer has them: the catalogue learns it now, then they stay in nexmix only.
                from . import catalog

                catalog.request_partial(db, "playlist", self.server_id)
        row = self.fresh_row()
        items = [[track, self.entry_ids.get(track), there.get(track)] for track in plan.merged.order
                 if (track in there or track not in self.added) and track not in self.left_in_copy]
        row.base = {"title": plan.title, "items": items}
        row.writing = None
        given_up = self._refused(row, [track for track in refused if track in alive])
        key = row.ext_key
        if wrote:
            # Plex sets its own time a moment later: the next look reads the entries once more and finds them equal.
            # Jellyfin's ``Etag`` was taken right after the write.
            self._done(self.seen_after)
        else:
            self._done(info, recent=self._recent(info))
        _remember_title(self.server_id, key, plan.title)
        if given_up and row.playlist_id is not None:
            if self.mirror:
                mirrors.refused(self.db, self.ident, given_up)
            else:
                self._hint(row.playlist_id, given_up)

    def _refused(self, row: ServerPlaylist, tracks: list[int]) -> list[int]:
        """Counts the tries of the tracks the server refused just now and says when to offer each again; returns the
        ones tried for the last time."""
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
        logger.info("The server refused tracks playlist=%s count=%s", row.playlist_id, len(tracks))
        return last

    def _hint(self, playlist_id: int, tracks: list[int]) -> None:
        """The entries of tracks the server refused for good say so."""
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
        logger.info("The server refused tracks for good playlist=%s count=%s", playlist_id, taken)
        changed = [item for item in db.scalars(select(PlaylistItem).where(
            PlaylistItem.playlist_id == playlist_id, PlaylistItem.track_id.in_(tracks),
            PlaylistItem.removed_at.is_(None)))]
        publish_items(db, playlist_id, [], changed=changed)


def _mass(lost: int, gone: int) -> bool:
    """Whether ``lost`` entries lost at once on the server are a loss nexmix does not take: many, while ``gone`` of
    their tracks left the library too. A playlist emptied on the server while its tracks are all still there was
    emptied on purpose, and that is taken."""
    return lost >= MASS_LOSS and gone > 0


#: How far the server's clock and nexmix's may lie apart when a playlist made after a lost answer is looked for.
ADOPT_SLACK = 120


def _made_then(one: Listed, server_now: int | None, waited: float) -> bool:
    """Whether the server made ``one`` within the time since nexmix asked it to make a playlist (``waited`` seconds
    ago), on the server's own clock and with ``ADOPT_SLACK`` either way. A server that does not say when it made it:
    not taken."""
    if not one.created or server_now is None:
        return False
    return -ADOPT_SLACK <= server_now - one.created <= waited + ADOPT_SLACK


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

#: Each server's list as the last look had it: a sync takes its playlist's name, kind and time from there instead of
#: asking for the whole list once more. A name nexmix wrote since goes in at once. ``{server: {"at", "by_key"}}``.
_listing: dict[int, dict[str, Any]] = {}
#: How long the last look counts as fresh.
LISTING_FRESH = POLL_EVERY * 2


def _listed(server_id: int, key: str) -> Listed | None:
    seen = _listing.get(server_id)
    at = seen.get("at") if seen else None
    if seen is None or not isinstance(at, datetime):
        return None
    if not timedelta(0) <= clock.now() - at <= LISTING_FRESH:
        return None
    found = seen["by_key"].get(key)
    return found if isinstance(found, Listed) else None


def _remember(server_id: int, listed: list[Listed]) -> None:
    _listing[server_id] = {"at": clock.now(), "by_key": {one.key: one for one in listed}}


def _remember_title(server_id: int, key: str | None, title: str) -> None:
    seen = _listing.get(server_id)
    found = seen["by_key"].get(key) if seen is not None and key is not None else None
    if isinstance(found, Listed) and found.title != title and seen is not None:
        seen["by_key"][key] = replace(found, title=title)


# --- The catalogue, for entries to be found on another server ---------------------------------------------------------


class _Catalogue(match.Finder):
    """The live catalogue of a server as ``match.py`` searches it, with each track's album."""

    def __init__(self, candidates: list[match.Candidate], albums: dict[int, int]) -> None:
        super().__init__(candidates, pause=mirrors.breathe)
        self.albums = albums


#: The catalogue of each server's live generation, kept while it is the live one and in use (another server: every
#: playlist of a move or a preview asks the same), and let go of ``KEEP_IDLE`` after its last use: with 50,000 tracks
#: it holds some 50 MB. ``{server: (generation, catalogue, last use on clock.monotonic)}``.
_whole: dict[int, tuple[int, _Catalogue, float]] = {}
KEEP_IDLE = 120.0
_whole_lock = threading.Lock()


def forget_catalogue(server_id: int) -> None:
    """The catalogue kept for ``server_id`` goes (a partial run keeps its generation, so it may be older)."""
    with _whole_lock:
        _whole.pop(server_id, None)


def release_idle(now: float | None = None) -> int:
    """The catalogues not used for ``KEEP_IDLE`` go (asked at every look at a server's list, and before a catalogue
    is made). Returns how many went."""
    moment = clock.monotonic() if now is None else now
    with _whole_lock:
        idle = [server for server, (_generation, _found, used) in _whole.items() if moment - used >= KEEP_IDLE]
        for server in idle:
            del _whole[server]
        empty = not _whole
    if idle and empty:
        # The names ``match.py`` made comparable go with the last catalogue (they are made again on the next use).
        match.forget_keys()
    return len(idle)


def _catalogue(db: Session, server_id: int) -> _Catalogue:
    from ..models import CatalogAlbum, CatalogArtist, CatalogTrack

    generation = media.generation_of(db, server_id)
    now = clock.monotonic()
    with _whole_lock:
        kept = _whole.get(server_id)
        if kept is not None and kept[0] == generation:
            _whole[server_id] = (generation, kept[1], now)
            return kept[1]
        _whole.pop(server_id, None)
    release_idle(now)
    albums = {row.id: (row.title, row.artist_id) for row in db.execute(
        select(CatalogAlbum.id, CatalogAlbum.title, CatalogAlbum.artist_id).where(
            CatalogAlbum.server_id == server_id, CatalogAlbum.generation == generation))}
    artists = dict(db.execute(select(CatalogArtist.id, CatalogArtist.title).where(
        CatalogArtist.server_id == server_id, CatalogArtist.generation == generation)).all())
    candidates: list[match.Candidate] = []
    album_of: dict[int, int] = {}
    for row in db.execute(select(CatalogTrack.id, CatalogTrack.title, CatalogTrack.album_id, CatalogTrack.artist,
                                 CatalogTrack.artist_id, CatalogTrack.duration, CatalogTrack.song_key,
                                 CatalogTrack.shown, CatalogTrack.guid, CatalogTrack.mbid_track,
                                 CatalogTrack.mbid_recording, CatalogTrack.number, CatalogTrack.disc).where(
            CatalogTrack.server_id == server_id, CatalogTrack.generation == generation)):
        if len(candidates) % 2000 == 0:
            # Made for a preview in the background: a pause now and then (else nothing).
            mirrors.breathe()
        album_title, album_artist = albums.get(row.album_id, ("", row.artist_id))
        candidates.append(match.Candidate(
            id=int(row.id), title=str(row.title or ""), album=str(album_title or ""),
            album_artist=str(artists.get(album_artist) or artists.get(row.artist_id) or ""),
            artist=str(row.artist or ""), duration=int(row.duration or 0), song_key=str(row.song_key or ""),
            shown=bool(row.shown), guid=str(row.guid or ""), mbid=str(row.mbid_track or ""),
            recording=str(row.mbid_recording or ""), number=int(row.number) if row.number else None,
            disc=int(row.disc) if row.disc else None))
        album_of[int(row.id)] = int(row.album_id or 0)
    found = _Catalogue(candidates, album_of)
    with _whole_lock:
        _whole[server_id] = (generation, found, clock.monotonic())
    return found


def _wanted(item: PlaylistItem, mbid: str = "") -> match.Wanted:
    """What an entry knows of its track; ``mbid``: the MusicBrainz id of its track, where the catalogue has it."""
    return match.Wanted(title=str(item.title or ""), artist=str(item.artist or ""), album=str(item.album or ""),
                        duration=int(item.duration or 0), guid=str(item.guid or ""), mbid=mbid)


def _same_track(item: PlaylistItem, here: match.Candidate) -> bool:
    """Whether the catalogue's track of the entry's number is the track the entry was made with."""
    from . import songs

    if item.guid and here.guid:
        return str(item.guid) == here.guid
    return songs.norm(item.title) == songs.norm(here.title) and songs.norm(item.album) == songs.norm(here.album)


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
        known = playlists.catalogue_of(db, (item.track_id for item in ordered), server_id=playlist.source_server_id)
        people = playlists.people_of(db, (item.added_by_user for item in ordered))
        told = {item.id for item in new}
        by_id = {item.id: item for item in ordered}
        new = [*new, *(by_id[item.id] for item in changed if item.id in by_id and item.id not in told)]
        found = playlists.summary(db, ordered, known, people, moods=True, server_id=playlist.source_server_id)
        total = db.scalar(select(ServerPlaylist.smart_total).where(
            ServerPlaylist.playlist_id == playlist_id, ServerPlaylist.server_id == playlist.source_server_id,
            ServerPlaylist.smart.is_(True)))
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


# --- The look at the list of a server ---------------------------------------------------------------------------------


def _owner(db: Session) -> int | None:
    return db.scalar(select(Account.id).where(Account.role == OPERATOR, Account.blocked_at.is_(None))
                     .order_by(Account.id).limit(1))


def run_poll(db: Session, job: Job) -> None:
    server = _ready(db, int(job.ref or 0))
    if server is None:
        return
    stop = _watch(server.id, media.epoch(server.id))
    try:
        with sources.open_server(db, server, stop=stop, patient=False) as source:
            listed = source.listing()
            now_server = source.clock
            owned = _owned(db, server, source, listed)
    except Stale:
        return
    except plex_link.Unreadable:
        _poll_result[server.id] = {"at": clock.now(), "error": "token_unreadable"}
        return
    except PlexError as exc:
        if exc.code == "stopped":
            raise jobs.Interrupted from exc
        _poll_result[server.id] = {"at": clock.now(), "error": exc.code}
        return
    _poll_result[server.id] = {"at": clock.now(), "error": None}
    _remember(server.id, listed)
    # A catalogue kept for finding entries on another server goes when nobody used it for a while.
    release_idle()
    take_in(db, server, listed, now_server, owned)
    # The looks leave no trail in the list of jobs (one every 15 seconds per server).
    db.execute(delete(Job).where(Job.kind == POLL, Job.state == "done", Job.id != job.id))
    db.commit()


#: Playlists of a server's list that are not its chosen user's own (Jellyfin lists other users' public ones too), or
#: whose owner could not be told: not asked about again for ``NOT_OURS_FOR``, nor until the server is connected anew.
#: ``{server: ((epoch of the server, when it was connected), {key: when it was asked})}``.
_not_ours: dict[int, tuple[tuple[int, Any], dict[str, datetime]]] = {}
NOT_OURS_FOR = timedelta(hours=1)


def _owned(db: Session, server: MediaServer, source: Any, listed: list[Listed]) -> set[str]:
    """The keys of the list nexmix may take in as new playlists: of the ones it has no row for, those the server
    says are its user's own. Asked once per playlist and hour."""
    known = set(db.scalars(select(ServerPlaylist.ext_key).where(ServerPlaylist.server_id == server.id,
                                                                ServerPlaylist.ext_key.is_not(None))))
    db.rollback()
    now = clock.now()
    epoch = (media.epoch(server.id), server.connected_at)
    kept = _not_ours.get(server.id)
    skipped: dict[str, datetime] = {}
    if kept is not None and kept[0] == epoch:
        skipped = {key: at for key, at in kept[1].items() if timedelta(0) <= now - at < NOT_OURS_FOR}
    _not_ours[server.id] = (epoch, skipped)
    fresh = [one.key for one in listed if one.key not in known and one.key not in skipped]
    if not fresh:
        return set()
    found = set(source.owned(fresh))
    for key in set(fresh) - found:
        skipped[key] = now
    return found


def take_in(db: Session, server: MediaServer, listed: list[Listed], now_server: int | None,
            owned: set[str] | None = None) -> None:
    """What the list of a server says: new playlists come to nexmix (of them, the ones in ``owned``, None: every
    one), changed ones are due, missing ones are due (their sync finds out whether they are gone), and every one
    waiting for the server is tried now that it answered."""
    now = clock.now()
    rows = list(db.scalars(select(ServerPlaylist).where(ServerPlaylist.server_id == server.id)
                           .execution_options(populate_existing=True)))
    sources_of = dict(db.execute(select(Playlist.id, Playlist.source_server_id)).all())
    trashed = set(db.scalars(select(Playlist.id).where(Playlist.deleted_at.is_not(None))))
    mine = [row for row in rows if row.ext_key is not None and not row.resume]
    by_key = {row.ext_key: row for row in mine}
    # The copies playlists left on this server before, and the ones deleted there whose playlist is in nexmix's trash:
    # never taken for new ones; their sync takes them up again (brought back from the trash).
    foreign = {row.ext_key for row in rows if row.ext_key is not None and row.resume}
    # The two playlists of a new name by copy under way (``Round._rename_copy``): neither new nor deleted.
    for row in rows:
        marker = row.writing.get("renaming") if isinstance(row.writing, dict) else None
        if isinstance(marker, dict):
            foreign |= {str(marker[name]) for name in ("from", "key") if marker.get(name)}
    # Playlists nexmix asked the server to make whose answer it has not got: the one the server made is theirs (it is
    # empty and has the name it was made with), never a new one of the server.
    making: set[str] = set()
    for row in rows:
        if row.ext_key is None and row.creating_at is not None and row.playlist_id is not None:
            name = row.creating_title or db.scalar(select(Playlist.name).where(Playlist.id == row.playlist_id))
            if name:
                making.add(name[:200])
    owner = _owner(db)
    seen_keys: set[str] = set()
    stale: list[ServerPlaylist] = []
    watched = {int(topic.split(":", 1)[1]) for topic in hub.topics() if topic.startswith("playlist:")}

    def due(row: ServerPlaylist) -> None:
        if row.due_at is None or row.due_at > now:
            row.due_at = now

    def here(row: ServerPlaylist) -> bool:
        """The copy of a playlist whose source this server is, or a mirror."""
        return row.playlist_id is not None and (sources_of.get(row.playlist_id) == server.id or bool(row.mirror))

    for one in listed:
        seen_keys.add(one.key)
        row = by_key.get(one.key)
        if row is None:
            orphan = one.title in making and not one.smart and not one.leaf_count
            if orphan or owner is None or one.key in foreign or (owned is not None and one.key not in owned):
                continue
            _take_new(db, server, one, owner, now)
            continue
        if not here(row):
            continue
        if row.state == ERROR and row.error in (PLAYLISTS_NOT_FOUND, UNREADABLE):
            # Missing from the list before, there again now: at once.
            due(row)
        last = row.seen if isinstance(row.seen, dict) else None
        if row.smart:
            if (last is None or last.get("updated_at") != one.updated_at or last.get("title") != one.title
                    or row.synced_at is None or now - row.synced_at >= SMART_EVERY):
                due(row)
            continue
        changed = last is None or any(last.get(name) != value for name, value in (
            ("updated_at", one.updated_at), ("leaf_count", one.leaf_count), ("title", one.title))) or \
            (last.get("etag") or "") != one.etag
        if changed or row.recheck or row.playlist_id in watched:
            due(row)
        elif row.synced_at is None or now - row.synced_at >= CHECK_EVERY:
            stale.append(row)
        row.recheck = now_server is not None and one.updated_at >= now_server - RECENT
    # What the list does not tell (entries moved): the ones longest unread, a few at a look.
    stale.sort(key=lambda row: row.synced_at or datetime.min.replace(tzinfo=now.tzinfo))
    for row in stale[:CHECK_PER_LOOK]:
        due(row)
    # Missing from the list: its sync finds out whether it is deleted there (``Round._vanished``). Several of nexmix's
    # at once are no deletion: none is asked about, the server says how many it lacks.
    absent = [row for key, row in by_key.items() if key not in seen_keys and here(row)]
    lacking = [row for row in absent if row.playlist_id not in trashed]
    if len(lacking) >= MASS_GONE:
        _note_missing(server.id, len(lacking))
        absent = [row for row in absent if row.playlist_id in trashed]
    else:
        _missing_note.pop(server.id, None)
    for row in absent:
        due(row)
    for row in rows:
        if row.state == WAITING or (here(row) and _offer_again(row, now)):
            due(row)
    db.commit()


def _offer_now(row: ServerPlaylist) -> None:
    """Tracks the server refused are offered again now (somebody asked), however long they had still to wait; one
    refused for good stays so."""
    records = row.refused if isinstance(row.refused, dict) else {}
    if any(isinstance(record, dict) and "next" in record for record in records.values()):
        now = clock.now().isoformat()
        row.refused = {key: ({**record, "next": now} if isinstance(record, dict) and "next" in record else record)
                       for key, record in records.items()}


def _offer_again(row: ServerPlaylist, now: datetime) -> bool:
    """Whether a track the server refused is due to be offered again."""
    records = row.refused if isinstance(row.refused, dict) else {}
    return any(isinstance(record, dict) and isinstance(record.get("next"), str) and not _after(record["next"], now)
               for record in records.values())


def _take_new(db: Session, server: MediaServer, one: Listed, owner: int, now: datetime) -> None:
    """A playlist made on the server: in nexmix, the operator's, read at once (a smart one only read, ever)."""
    playlist = Playlist(owner_id=owner, name=(one.title or VIA_NAMES.get(server.kind, PLEX_NAME))[:200],
                        description="", version=1, created_at=now, updated_at=now, source_server_id=server.id)
    db.add(playlist)
    db.flush()
    db.add(ServerPlaylist(playlist_id=playlist.id, server_id=server.id, ext_key=one.key, guid=one.guid,
                          smart=one.smart, state=PENDING, due_at=now,
                          base=None if one.smart else {"title": one.title, "items": []}))
    db.flush()
    logger.info("Playlist taken in from the server playlist=%s server=%s smart=%s", playlist.id, server.id, one.smart)


jobs.register(ROUND, run_round, settle)
jobs.register(POLL, run_poll)
jobs.LANES[LANE] = KINDS
