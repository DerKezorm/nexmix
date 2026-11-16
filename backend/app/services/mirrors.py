"""Mirrors: a playlist kept on more servers than its source, and playlists copied and moved between servers.

**A mirror** is a copy of a playlist on a server that is not its source (``ServerPlaylist.mirror``), kept in step both
ways like the source (``sync.py``: every copy has its own common state and its own writes, so nothing goes back and
forth). The entries belong to the source; for every mirror each entry has the track of the mirror's server that stands
for it (``MirrorItem``: found by ``match.py``, chosen by hand, or none). An entry without one is not in the mirror and
says so ("not on <server>"); it never counts as taken out there.

* A change in nexmix goes to the source and to every mirror, each in its own sync.
* A change in a mirror comes back through the entries: a track taken out or moved there takes its entry out or moves
  it; a track put in there is looked for in the source's library: found by a sure step it becomes an entry ("via
  <server>"), else it is shown at the playlist as "only in <server>" (``MirrorExtra``) until somebody takes it over as
  a track of the source or drops it. Such a track is neither taken over nor taken out meanwhile.
* A new entry finds its track on every mirror by a sure step at the mirror's next sync; one found by nothing stays
  without, and is looked for again after each run of that server's catalogue.
* A mirror deleted on its server stays a row that says so (``MIRROR_GONE``); the playlist is untouched. A mirror of a
  playlist in the trash rests until the playlist is brought back; deleted for good, the playlist takes its mirrors
  with it from their servers. A mirror let go of ("it stays there as it is", ``LET_GO``) is never deleted there.
* Never one track of a mirror for an entry and "only in <server>" at once: an entry matched by hand to such a track,
  or found as it, takes it over.

**Copy** ("copy to …") makes a mirror, after a preview: "N of M found, K of them worth a look", with every entry not
found or found by an unsure step, each to be matched by hand or left out. "Copy once" instead makes a new playlist
with the target as its source and no tie to the first.

**Move** ("move to …"): the target becomes the source. Its entries take the target's tracks; an entry without one
keeps its track and says it is not on the new source (``sync.HINT_ELSEWHERE``) until the old server is let go of. The
copy on the server before stays a mirror (the default) or is deleted there. Guests, shares, the trash and the
suggestions follow the source.

**Take all over** (``Transfer``): the same over every playlist of the other servers, one after another in the lane of
the playlists' sync, with progress, stopped on request after the playlist under way, and gone on with after a restart.

**Letting go of a server** works only while no playlist has it as its source; its mirrors are forgotten, not deleted
(they rest there as copies, never taken in again as new playlists).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.orm import Session

from .. import clock
from ..errors import error
from ..models import (
    Account,
    CatalogAlbum,
    CatalogArtist,
    CatalogTrack,
    Job,
    MediaServer,
    MirrorExtra,
    MirrorItem,
    Playlist,
    PlaylistItem,
    ServerPlaylist,
    Transfer,
)
from . import jobs, match, media, merge
from .events import hub, playlist_topic
from .plex import PlexError

if TYPE_CHECKING:
    from .sync import Item, Round, Snapshot

logger = logging.getLogger("nexmix.mirrors")

#: The mirror was deleted on its server: the playlist stays, the mirror says so.
MIRROR_GONE = "mirror_gone"
#: An entry left out of a mirror on purpose.
SKIP = "skip"
#: A mirror let go of (``ServerPlaylist.settle``): the copy there is never deleted by nexmix.
LET_GO = "let_go"
#: An entry that was its own track before (the copy on the server it came from, when the playlist moved).
SAME = "same"
#: Mirror hints: the mirror's server refused the track, or lost it while it left its library.
HINT_REFUSED, HINT_MISSING = "refused", "missing"
#: The most entries a preview names one by one (the counts are always whole).
PREVIEW_ROWS = 2000
#: How long one job of "take all over" works before it lets the syncs of the lane go first.
TRANSFER_SLICE = 2.0
TRANSFER = "server_transfer"
RUNNING, DONE, STOPPED, FAILED = "running", "done", "stopped", "failed"
MODES = ("mirror", "move")
OLD = ("keep", "delete")


# --- What a server's catalogue says of a track ------------------------------------------------------------------------


def describe(db: Session, server_id: int, tracks: Iterable[int]) -> dict[int, match.Wanted]:
    """What the live catalogue of ``server_id`` says of the tracks, as ``match.py`` asks for it: title, artists, album,
    length, MusicBrainz id, place on the album and the places and lengths of all tracks of that album."""
    generation = media.generation_of(db, server_id)
    wanted = sorted({int(track) for track in tracks})
    if not generation or not wanted:
        return {}
    rows: dict[int, Any] = {}
    for start in range(0, len(wanted), 500):
        for row in db.execute(select(CatalogTrack.id, CatalogTrack.title, CatalogTrack.artist, CatalogTrack.album_id,
                                     CatalogTrack.artist_id, CatalogTrack.duration, CatalogTrack.guid,
                                     CatalogTrack.mbid_track, CatalogTrack.number, CatalogTrack.disc).where(
                CatalogTrack.server_id == server_id, CatalogTrack.generation == generation,
                CatalogTrack.id.in_(wanted[start:start + 500]))):
            rows[int(row.id)] = row
        breathe()
    album_ids = sorted({int(row.album_id or 0) for row in rows.values()} - {0})
    albums: dict[int, tuple[str, int]] = {}
    shapes: dict[int, list[tuple[int, int, int]]] = {}
    for start in range(0, len(album_ids), 500):
        part = album_ids[start:start + 500]
        for ident, title, artist in db.execute(select(CatalogAlbum.id, CatalogAlbum.title, CatalogAlbum.artist_id)
                                               .where(CatalogAlbum.server_id == server_id,
                                                      CatalogAlbum.generation == generation,
                                                      CatalogAlbum.id.in_(part))):
            albums[int(ident)] = (str(title or ""), int(artist or 0))
        for album, disc, number, duration in db.execute(
                select(CatalogTrack.album_id, CatalogTrack.disc, CatalogTrack.number, CatalogTrack.duration).where(
                    CatalogTrack.server_id == server_id, CatalogTrack.generation == generation,
                    CatalogTrack.album_id.in_(part))):
            if number:
                shapes.setdefault(int(album), []).append((int(disc or 1), int(number), int(duration or 0)))
        breathe()
    people = sorted(({artist for _title, artist in albums.values()}
                     | {int(row.artist_id or 0) for row in rows.values()}) - {0})
    names: dict[int, str] = {}
    for start in range(0, len(people), 500):
        names.update({int(ident): str(title or "") for ident, title in db.execute(
            select(CatalogArtist.id, CatalogArtist.title).where(
                CatalogArtist.server_id == server_id, CatalogArtist.generation == generation,
                CatalogArtist.id.in_(people[start:start + 500])))})
    found: dict[int, match.Wanted] = {}
    for count, (ident, row) in enumerate(rows.items()):
        if count % 2000 == 0:
            breathe()
        album_title, album_artist = albums.get(int(row.album_id or 0), ("", int(row.artist_id or 0)))
        owner = str(names.get(album_artist) or names.get(int(row.artist_id or 0)) or "")
        found[ident] = match.Wanted(
            title=str(row.title or ""), artist=str(row.artist or owner), album=album_title,
            duration=int(row.duration or 0), guid=str(row.guid or ""), mbid=str(row.mbid_track or ""),
            album_artist=owner, number=int(row.number) if row.number else None,
            disc=int(row.disc) if row.disc else None, shape=tuple(sorted(shapes.get(int(row.album_id or 0), ()))))
    return found


def _wanted_of(item: PlaylistItem, described: dict[int, match.Wanted]) -> match.Wanted:
    found = described.get(int(item.track_id))
    if found is not None:
        return found
    return match.Wanted(title=str(item.title or ""), artist=str(item.artist or ""), album=str(item.album or ""),
                        duration=int(item.duration or 0), guid=str(item.guid or ""))


def _finder(db: Session, server_id: int) -> match.Finder:
    from . import sync

    return sync._catalogue(db, server_id)


def _live_tracks(db: Session, server_id: int) -> set[int]:
    generation = media.generation_of(db, server_id)
    return set(db.scalars(select(CatalogTrack.id).where(CatalogTrack.server_id == server_id,
                                                        CatalogTrack.generation == generation)))


# --- Decisions: each entry's track on another server ------------------------------------------------------------------


@dataclass
class Decision:
    """An entry's track on the target: found (``track``, ``how``), or none (``how`` ``none`` or ``skip``)."""

    track: int | None
    how: str

    @property
    def sure(self) -> bool:
        return self.track is not None and self.how not in match.UNSURE


def _plex_mbids(db: Session, source_id: int, tracks: Sequence[int]) -> dict[int, str]:
    """The MusicBrainz ids of tracks of a Plex server, asked of the server (the catalogue does not hold them, see
    ``PlexSource.mbids``); kept for a while, so that the copy after its preview does not ask again. Nothing when the
    server is no Plex or cannot be asked: the steps by name stand."""
    from ..models import SERVER_PLEX
    from . import plex_link, sources

    server = media.get(db, source_id)
    if server is None or server.kind != SERVER_PLEX or not server.active or not tracks:
        return {}
    found = {track: _mbid_cache[(source_id, track)] for track in tracks if (source_id, track) in _mbid_cache}
    asking = [track for track in tracks if (source_id, track) not in _mbid_cache]
    if asking:
        keys = media.keys(db, source_id, media.TRACK, asking)
        try:
            with sources.open_server(db, server) as source:
                answered = source.mbids(keys.values())
        except (plex_link.Unreadable, PlexError) as exc:
            code = exc.code if isinstance(exc, PlexError) else UNREADABLE
            logger.warning("MusicBrainz ids not asked of the server server=%s error=%s", source_id, code)
            return found
        if len(_mbid_cache) > MBID_CACHE:
            _mbid_cache.clear()
        for track in asking:
            key = keys.get(track)
            _mbid_cache[(source_id, track)] = answered.get(key, "") if key is not None else ""
            if _mbid_cache[(source_id, track)]:
                found[track] = _mbid_cache[(source_id, track)]
    return {track: mbid for track, mbid in found.items() if mbid}


#: The code logged when the server's key cannot be opened.
UNREADABLE = "key_unreadable"
#: MusicBrainz ids asked of a Plex server: ``{(server, track): id or ""}``.
_mbid_cache: dict[tuple[int, int], str] = {}
MBID_CACHE = 200_000


def forget_asked() -> None:
    """For the tests: nothing asked of a server is kept."""
    _mbid_cache.clear()


def decide(db: Session, items: Sequence[PlaylistItem], source_id: int, target_id: int,
           choices: dict[int, int | None] | None = None, *, kept: dict[int, Decision] | None = None,
           described: dict[int, match.Wanted] | None = None, taken: Iterable[int] = (),
           ask: bool = False) -> dict[int, Decision]:
    """Each entry's track on ``target_id``, by entry: a person's choice first (by the entry's track: a track of the
    target, or None to leave it out), then what a mirror there holds already (``kept``), then ``match.py``. Never one
    track of the target for two entries, nor one of ``taken``. ``ask``: an entry of a Plex server its name did not
    find surely is looked for once more with its MusicBrainz id, asked of the server."""
    choices = choices or {}
    kept = kept or {}
    live = _live_tracks(db, target_id) if choices else set()
    found: dict[int, Decision] = {}
    used: set[int] = set(taken)
    for item in items:
        if int(item.track_id) in choices:
            chosen = choices[int(item.track_id)]
            if chosen is None:
                found[item.id] = Decision(None, SKIP)
            elif int(chosen) in live and int(chosen) not in used:
                found[item.id] = Decision(int(chosen), match.HAND)
                used.add(int(chosen))
    for item in items:
        if item.id in found:
            continue
        before = kept.get(item.id)
        if before is not None and (before.track is None or before.track not in used):
            found[item.id] = before
            if before.track is not None:
                used.add(before.track)
    rest = [item for item in items if item.id not in found]
    if rest:
        if described is None:
            described = describe(db, source_id, (item.track_id for item in rest))
        wanted = [_wanted_of(item, described) for item in rest]
        finder = _finder(db, target_id)
        hits = match.assign(wanted, finder, used)
        weak = [item.track_id for item, hit, one in zip(rest, hits, wanted, strict=True)
                if (hit is None or not hit.sure) and not one.mbid]
        more = _plex_mbids(db, source_id, weak) if ask and weak else {}
        if more:
            wanted = [replace(one, mbid=more.get(int(item.track_id), one.mbid))
                      for item, one in zip(rest, wanted, strict=True)]
            hits = match.assign(wanted, finder, used)
        for item, hit in zip(rest, hits, strict=True):
            found[item.id] = Decision(hit.track.id, hit.how) if hit is not None else Decision(None, match.NONE)
    return found


def taken(one: Decision, unsure: bool) -> bool:
    """Whether a decision gives the entry a track: a sure finding, a choice by hand, and an unsure finding only when
    ``unsure`` (copying one playlist takes those, moving and taking all over only offer them)."""
    return one.track is not None and (unsure or one.sure or one.how == match.HAND)


def _track_view(db: Session, server_id: int, tracks: Iterable[int]) -> dict[int, dict[str, Any]]:
    from . import playlists

    known = playlists.catalogue_of(db, tracks, server_id=server_id)
    return {track: {"track": track, "title": one.title, "artist": one.artist, "album": one.album,
                    "duration": round(one.duration / 1000)} for track, one in known.items()}


# --- Checks -----------------------------------------------------------------------------------------------------------


def _target(db: Session, server_id: int) -> MediaServer:
    from . import sync

    row = media.get(db, server_id)
    if row is None or not row.active:
        raise error("not_found", "Not found.", 404)
    if not sync.keeps_playlists(row) or not (row.section_key and row.generation):
        raise error("server_not_ready", "The server has no catalogue yet.", 409)
    return row


def _movable(db: Session, playlist: Playlist, target: MediaServer) -> int:
    """The playlist's source, when the playlist may go to ``target``: not in the trash, not a smart playlist, not
    there already."""
    from . import sync

    if playlist.deleted_at is not None:
        raise error("in_trash", "The playlist is in the trash.", 409)
    if sync.is_smart(db, playlist.id):
        raise error("smart_stays", "A smart playlist stays on its own server.", 409)
    source = playlist.source_server_id
    if source is None:
        raise error("server_not_ready", "The playlist has no server yet.", 409)
    if int(source) == target.id:
        raise error("same_server", "The playlist takes its tracks from this server already.", 409)
    if media.generation_of(db, source) == 0:
        raise error("server_not_ready", "The server has no catalogue yet.", 409)
    return int(source)


def mirror_row(db: Session, playlist_id: int, server_id: int) -> ServerPlaylist | None:
    return db.scalars(select(ServerPlaylist).where(ServerPlaylist.playlist_id == playlist_id,
                                                   ServerPlaylist.server_id == server_id,
                                                   ServerPlaylist.mirror.is_(True))
                      .execution_options(populate_existing=True)).first()


def mirror_rows(db: Session, playlist_id: int) -> list[ServerPlaylist]:
    return list(db.scalars(select(ServerPlaylist).where(ServerPlaylist.playlist_id == playlist_id,
                                                        ServerPlaylist.mirror.is_(True))
                           .order_by(ServerPlaylist.server_id).execution_options(populate_existing=True)))


def kept_of(db: Session, copy_id: int) -> dict[int, Decision]:
    return {int(row.item_id): Decision(int(row.track_id) if row.track_id is not None else None, row.how)
            for row in db.scalars(select(MirrorItem).where(MirrorItem.copy_id == copy_id))}


# --- The preview ------------------------------------------------------------------------------------------------------


def preview(db: Session, playlist_ids: Sequence[int], target_id: int,
            choices: dict[int, int | None] | None = None,
            progress: Callable[[int], None] | None = None, *, preselect: bool = True) -> dict[str, Any]:
    """What copying or moving the playlists to ``target_id`` would find: how many entries, how many found, how many of
    them by an unsure step, and the entries not found or found unsure (by their track, the same track in several
    playlists once), each with what was found. Nothing is written. ``progress`` hears how many playlists are done
    after each one (it may raise to stop). ``preselect``: an unsure finding counts as found (copying a mirror); else
    (moving, taking all over) it is only offered, and counts once chosen (``choices``)."""
    from . import playlists

    total = found_count = unsure = 0
    done = 0
    rows: dict[int, dict[str, Any]] = {}
    by_source: dict[int, list[tuple[Playlist, list[PlaylistItem]]]] = {}
    for playlist_id in playlist_ids:
        playlist = db.get(Playlist, playlist_id)
        if playlist is None or playlist.source_server_id is None:
            done += 1
            continue
        by_source.setdefault(int(playlist.source_server_id), []).append(
            (playlist, playlists.live_items(db, playlist_id)))
        breathe()
    for source_id, group in by_source.items():
        described = describe(db, source_id, (item.track_id for _p, items in group for item in items))
        for playlist, items in group:
            if progress is not None:
                progress(done)
            done += 1
            existing = mirror_row(db, playlist.id, target_id)
            kept = kept_of(db, existing.id) if existing is not None else None
            decided = decide(db, items, source_id, target_id, choices, kept=kept, described=described, ask=True)
            for item in items:
                one = decided[item.id]
                total += 1
                if taken(one, preselect):
                    found_count += 1
                if one.track is not None and one.how in match.UNSURE:
                    unsure += 1
                if one.sure or one.how == match.HAND:
                    continue
                row = rows.get(int(item.track_id))
                if row is None:
                    wanted = _wanted_of(item, described)
                    row = rows[int(item.track_id)] = {
                        "track": int(item.track_id), "title": wanted.title, "artist": wanted.artist,
                        "album": wanted.album, "duration": round(wanted.duration / 1000), "how": one.how,
                        "found": one.track, "count": 0}
                row["count"] += 1
    listed = list(rows.values())[:PREVIEW_ROWS]
    views = _track_view(db, target_id, [row["found"] for row in listed if row["found"] is not None])
    for row in listed:
        row["found"] = views.get(row["found"]) if row["found"] is not None else None
    listed.sort(key=lambda row: (row["how"] != match.NONE, row["artist"].casefold(), row["title"].casefold()))
    db.rollback()
    return {"server": target_id, "entries": total, "found": found_count, "unsure": unsure,
            "missing": total - found_count, "rows": listed, "more": max(0, len(rows) - len(listed)),
            "preselect": preselect}


# --- The preview of "take all over", in the background ----------------------------------------------------------------
#
# Over every playlist of a large library it takes seconds: it runs in a thread of its own, never in the thread of a
# request, with a short pause after each playlist so that the requests of others keep their pace. The interface asks
# for it, then for its progress and its result (``GET``); a person's choices by hand are counted in the browser, not by
# a new preview.

#: A preview pauses ``PREVIEW_PAUSE`` seconds after every ``PREVIEW_WORK`` seconds of work (``breathe``): a thread that
#: works without a pause makes every other thread wait for Python's lock at each of its steps (measured: other requests
#: up to 1.2 s instead of 0.04 s while a preview over 40,000 entries built its catalogue).
PREVIEW_WORK = 0.02
PREVIEW_PAUSE = 0.005
_breather = threading.local()


def breathe() -> None:
    """In the thread of a preview: a short pause once ``PREVIEW_WORK`` went by since the last one (elsewhere nothing).
    Called in the long loops a preview runs through."""
    since = getattr(_breather, "since", None)
    if since is None:
        return
    if time.perf_counter() - since >= PREVIEW_WORK:
        time.sleep(PREVIEW_PAUSE)
        _breather.since = time.perf_counter()


class _Stopped(Exception):
    """A preview asked anew, or nexmix stopping: the one under way ends."""


@dataclass
class Looking:
    """One preview of "take all over" to a server: what was asked, how far it got, and its result."""

    asked: tuple[Any, ...]
    total: int
    smart: int
    done: int = 0
    state: str = RUNNING
    result: dict[str, Any] | None = None
    error: str | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


#: The last preview of each target server.
_looking: dict[int, Looking] = {}
_looking_lock = threading.Lock()


def look_view(one: Looking | None) -> dict[str, Any] | None:
    if one is None:
        return None
    return {"state": one.state, "done": one.done, "total": one.total, "error": one.error,
            "result": one.result if one.state == DONE else None}


def start_look(db: Session, target_id: int, mode: str, source_id: int | None,
               choices: dict[int, int | None] | None = None) -> dict[str, Any]:
    """The preview of "take all over" to ``target_id`` begins in the background (the same one under way already is
    not begun twice; another one under way ends). Returns how it stands."""
    target = _target(db, target_id)
    chosen, smart = candidates(db, target.id, mode, source_id)
    db.rollback()
    picked = dict(choices or {})
    asked = (mode, source_id, tuple(sorted(picked.items(), key=lambda pair: pair[0])))
    with _looking_lock:
        now = _looking.get(target.id)
        if now is not None and now.asked == asked and now.state == RUNNING:
            return look_view(now) or {}
        if now is not None:
            now.stop.set()
        one = Looking(asked=asked, total=len(chosen), smart=smart)
        _looking[target.id] = one
        one.thread = threading.Thread(target=_look, args=(target.id, one, chosen, picked), daemon=True,
                                      name=f"preview-{target.id}")
        one.thread.start()
    return look_view(one) or {}


def look_of(target_id: int) -> dict[str, Any] | None:
    with _looking_lock:
        return look_view(_looking.get(target_id))


def wait_look(target_id: int, timeout: float = 60.0) -> dict[str, Any] | None:
    """For the tests: the preview under way of ``target_id`` at its end."""
    with _looking_lock:
        one = _looking.get(target_id)
    if one is not None and one.thread is not None:
        one.thread.join(timeout)
    return look_view(one)


def forget_looks() -> None:
    """For the tests, and when nexmix stops: every preview under way ends, none is kept."""
    with _looking_lock:
        found = list(_looking.values())
        _looking.clear()
    for one in found:
        one.stop.set()
    for one in found:
        if one.thread is not None and one.thread is not threading.current_thread():
            one.thread.join(5)


def _look(target_id: int, one: Looking, chosen: list[int], choices: dict[int, int | None]) -> None:
    from ..db import SessionLocal

    def progress(done: int) -> None:
        one.done = done
        if one.stop.is_set() or jobs.stopping.is_set():
            raise _Stopped
        breathe()

    _breather.since = time.perf_counter()
    try:
        with SessionLocal() as db:
            # Taking all over offers the unsure findings, it never takes them unasked.
            found = preview(db, chosen, target_id, choices, progress=progress, preselect=False)
        one.result = {**found, "playlists": len(chosen), "smart": one.smart}
        one.done, one.state = len(chosen), DONE
    except _Stopped:
        one.state = STOPPED
    except Exception:
        logger.exception("Preview of a transfer failed server=%s", target_id)
        one.state, one.error = FAILED, "internal_error"
    finally:
        _breather.since = None


# --- Making a mirror, a copy, a move ----------------------------------------------------------------------------------


def _lock(db: Session, playlist_id: int) -> Playlist:
    """The playlist under the write lock, its version counted up: a sync that read it before merges again."""
    db.execute(update(Playlist).where(Playlist.id == playlist_id)
               .values(version=Playlist.version + 1, updated_at=clock.now()))
    playlist = db.get(Playlist, playlist_id, populate_existing=True)
    if playlist is None:
        raise error("not_found", "Not found.", 404)
    return playlist


def _set_mapping(db: Session, copy_id: int, decided: dict[int, Decision]) -> None:
    db.execute(delete(MirrorItem).where(MirrorItem.copy_id == copy_id))
    db.flush()
    for item_id, one in sorted(decided.items()):
        db.add(MirrorItem(copy_id=copy_id, item_id=item_id, track_id=one.track, how=one.how[:8]))
    db.flush()


def _tell(db: Session, playlist_id: int, op: str) -> None:
    """Every browser with the playlist open hears of its mirrors (and of a new version: it reads the playlist anew)."""
    version = db.scalar(select(Playlist.version).where(Playlist.id == playlist_id)) or 0
    hub.publish(playlist_topic(playlist_id), "playlist", {"version": version, "op": op, "by": None})
    hub.publish(playlist_topic(playlist_id), "mirrors", {"mirrors": view(db, playlist_id)})
    db.rollback()


def make_mirror(db: Session, playlist_id: int, target_id: int, choices: dict[int, int | None] | None = None,
                *, commit: bool = True, unsure: bool = True) -> ServerPlaylist:
    """The playlist mirrored on ``target_id``: a mirror row with each entry's track there, due at once. ``unsure``:
    an entry found only by an unsure step takes that track (copying one playlist, after its preview); else it has
    none there unless chosen (taking all over)."""
    from . import playlists, sync

    target = _target(db, target_id)
    playlist = db.get(Playlist, playlist_id, populate_existing=True)
    if playlist is None:
        raise error("not_found", "Not found.", 404)
    source_id = _movable(db, playlist, target)
    row = db.scalars(select(ServerPlaylist).where(ServerPlaylist.playlist_id == playlist_id,
                                                  ServerPlaylist.server_id == target.id)
                     .execution_options(populate_existing=True)).first()
    if row is not None and row.mirror and not row.gone_there:
        raise error("already_mirrored", "The playlist is mirrored there already.", 409)
    items = playlists.live_items(db, playlist_id)
    decided = decide(db, items, source_id, target.id, choices, ask=True)
    if not unsure:
        decided = {ident: one if taken(one, False) else Decision(None, match.NONE) for ident, one in decided.items()}
    _lock(db, playlist_id)
    now = clock.now()
    if row is None:
        row = ServerPlaylist(playlist_id=playlist_id, server_id=target.id, state=sync.PENDING, due_at=now,
                             mirror=True)
        db.add(row)
        db.flush()
    else:
        # A copy left there before is taken up again when the server still lists it (``resume``); one deleted there
        # is made anew.
        if row.gone_there:
            sync.Round._forget_copy(row)
            row.gone_there, row.resume = False, False
        elif row.ext_key is not None:
            row.resume = True
        row.mirror, row.smart, row.settle = True, False, ""
        row.state, row.error, row.tries, row.due_at = sync.PENDING, "", 0, now
        db.execute(delete(MirrorExtra).where(MirrorExtra.copy_id == row.id))
    _set_mapping(db, row.id, decided)
    if commit:
        db.commit()
        logger.info("Playlist mirrored playlist=%s server=%s found=%s of=%s", playlist_id, target.id,
                    sum(1 for one in decided.values() if one.track is not None), len(decided))
        _tell(db, playlist_id, "mirrored")
    return row


def copy_once(db: Session, playlist_id: int, target_id: int, choices: dict[int, int | None] | None = None) -> Playlist:
    """A new playlist on ``target_id`` with the entries found there, in their order, each still saying who put it in;
    no tie to the first one. The entries found nowhere stay out."""
    from . import playlists, sync

    target = _target(db, target_id)
    playlist = db.get(Playlist, playlist_id, populate_existing=True)
    if playlist is None:
        raise error("not_found", "Not found.", 404)
    if playlist.deleted_at is not None:
        raise error("in_trash", "The playlist is in the trash.", 409)
    source = playlist.source_server_id
    if source is None or int(source) == target.id or media.generation_of(db, source) == 0:
        raise error("same_server" if source is not None and int(source) == target.id else "server_not_ready",
                    "The playlist cannot be copied there.", 409)
    items = playlists.live_items(db, playlist_id)
    decided = decide(db, items, int(source), target.id, choices, ask=True)
    owner_count = int(db.scalar(select(func.count()).select_from(Playlist).where(
        Playlist.owner_id == playlist.owner_id, Playlist.deleted_at.is_(None))) or 0)
    if owner_count >= playlists.MAX_PLAYLISTS:
        raise error("too_many_playlists", "Too many playlists.", 409)
    now = clock.now()
    copy = Playlist(owner_id=playlist.owner_id, name=playlist.name, description=playlist.description, version=1,
                    created_at=now, updated_at=now, source_server_id=target.id)
    db.add(copy)
    db.flush()
    chosen = [(item, decided[item.id].track) for item in items if decided[item.id].track is not None]
    known = playlists.catalogue_of(db, (track for _item, track in chosen if track is not None), server_id=target.id)
    for position, (item, track) in enumerate(chosen):
        assert track is not None
        there = known.get(track)
        db.add(PlaylistItem(
            playlist_id=copy.id, track_id=track, position=position, added_by_user=item.added_by_user,
            added_by_name=item.added_by_name, added_at=item.added_at, from_plex=item.from_plex,
            title=(there.title if there else item.title)[:500], artist=(there.artist if there else item.artist)[:500],
            album=(there.album if there else item.album)[:500], album_id=there.album_id if there else 0,
            year=there.year if there else item.year, duration=there.duration if there else item.duration,
            guid=(there.guid if there else "")[:200]))
    db.flush()
    sync.mark(db, copy.id, at_once=True)
    db.commit()
    logger.info("Playlist copied once playlist=%s copy=%s server=%s found=%s of=%s", playlist_id, copy.id, target.id,
                len(chosen), len(items))
    return copy


def move(db: Session, playlist_id: int, target_id: int, choices: dict[int, int | None] | None = None,
         old: str = "keep", *, commit: bool = True) -> dict[str, Any]:
    """The playlist takes its tracks from ``target_id`` from now on (see the module's text)."""
    from . import playlists, sync

    if old not in OLD:
        raise error("invalid", "Invalid value.", 422)
    target = _target(db, target_id)
    playlist = db.get(Playlist, playlist_id, populate_existing=True)
    if playlist is None:
        raise error("not_found", "Not found.", 404)
    source_id = _movable(db, playlist, target)
    items = playlists.live_items(db, playlist_id)
    existing = mirror_row(db, playlist_id, target.id)
    kept = kept_of(db, existing.id) if existing is not None and not existing.gone_there else None
    decided = decide(db, items, source_id, target.id, choices, kept=kept, ask=True)
    hints = {}
    if existing is not None:
        hints = {int(item): hint for item, hint in db.execute(
            select(MirrorItem.item_id, MirrorItem.hint).where(MirrorItem.copy_id == existing.id,
                                                              MirrorItem.hint != ""))}
    _lock(db, playlist_id)
    now = clock.now()
    source_row = sync.copy_of(db, playlist_id, source_id)
    # The copy on the server before: a mirror of the entries as they are (each its own track), or deleted there.
    if source_row is not None:
        db.execute(delete(MirrorItem).where(MirrorItem.copy_id == source_row.id))
        # Tracks "only there" belong to the part of a mirror: a copy that changes its part starts without (else a
        # track that came in as an entry since would be held out of it, and taken for one taken out there).
        db.execute(delete(MirrorExtra).where(MirrorExtra.copy_id == source_row.id))
        if old == "delete":
            if source_row.ext_key is None:
                db.delete(source_row)
            else:
                source_row.playlist_id, source_row.mirror, source_row.due_at = None, False, now
                source_row.state = sync.PENDING
        else:
            source_row.mirror, source_row.state, source_row.due_at, source_row.tries = True, sync.PENDING, now, 0
            db.flush()
            for item in items:
                here = item.plex_hint != sync.HINT_ELSEWHERE
                db.add(MirrorItem(copy_id=source_row.id, item_id=item.id, track_id=item.track_id if here else None,
                                  how=SAME if here else match.NONE,
                                  hint=item.plex_hint if item.plex_hint in (HINT_REFUSED, HINT_MISSING) else ""))
    db.flush()
    # The entries take the target's tracks; one without keeps its own and says it is not on the new source.
    # Only sure findings and choices by hand: an entry found by an unsure step keeps its track and says it is not on
    # the new source, never another song unasked.
    moving = {item.id: decided[item.id].track for item in items if taken(decided[item.id], False)}
    known = playlists.catalogue_of(db, (track for track in moving.values() if track is not None), server_id=target.id)
    for item in items:
        if item.id in moving:
            item.track_id = -item.id
    db.flush()
    for item in items:
        track = moving.get(item.id)
        if track is None:
            item.plex_hint = sync.HINT_ELSEWHERE
            continue
        there = known.get(track)
        item.track_id, item.album_id = track, (there.album_id if there else 0)
        item.guid = (there.guid if there else "")[:200]
        if there is not None:
            # What the entry knows of its track is the new source's from now on (chosen by hand: another track).
            item.title, item.artist, item.album = there.title[:500], there.artist[:500], there.album[:500]
            item.year, item.duration = there.year, there.duration
        item.plex_hint = hints.get(item.id, "") if hints.get(item.id) in (HINT_REFUSED, HINT_MISSING) else ""
    # The copy on the target: the mirror there becomes the source, with its common state; else one is made.
    if existing is not None:
        db.execute(delete(MirrorItem).where(MirrorItem.copy_id == existing.id))
        # The tracks only there become entries at the source's first sync (they are in its list, not in the common
        # state): as a source, the copy holds nothing out. The ones somebody dropped stay to be taken out there first.
        db.execute(delete(MirrorExtra).where(MirrorExtra.copy_id == existing.id, MirrorExtra.discard.is_(False)))
        if existing.gone_there:
            sync.Round._forget_copy(existing)
            existing.gone_there = False
        existing.mirror, existing.state, existing.due_at, existing.tries, existing.error = (
            False, sync.PENDING, now, 0, "")
        existing.settle = ""
    else:
        stale = db.scalars(select(ServerPlaylist).where(ServerPlaylist.playlist_id == playlist_id,
                                                        ServerPlaylist.server_id == target.id)).first()
        if stale is not None:
            db.execute(delete(MirrorExtra).where(MirrorExtra.copy_id == stale.id))
            stale.settle, stale.state, stale.due_at, stale.tries, stale.error = "", sync.PENDING, now, 0, ""
            stale.mirror = False
            if stale.ext_key is not None:
                stale.resume = True
        else:
            db.add(ServerPlaylist(playlist_id=playlist_id, server_id=target.id, state=sync.PENDING, due_at=now))
    playlist.source_server_id = target.id
    db.flush()
    found = len(moving)
    if commit:
        db.commit()
        logger.info("Playlist moved to another server playlist=%s server=%s found=%s of=%s old=%s", playlist_id,
                    target.id, found, len(items), old)
        _tell(db, playlist_id, "moved")
    return {"found": found, "entries": len(items)}


def forget(db: Session, playlist_id: int, server_id: int) -> None:
    """The mirror on ``server_id`` is let go of: the playlist there stays as it is, nexmix no longer keeps it in step
    (it rests as a copy, never taken in as a new playlist)."""
    row = mirror_row(db, playlist_id, server_id)
    if row is None:
        raise error("not_found", "Not found.", 404)
    _lock(db, playlist_id)
    _rest(db, row)
    db.commit()
    logger.info("Mirror let go of playlist=%s server=%s", playlist_id, server_id)
    _tell(db, playlist_id, "mirror_forgotten")


def _rest(db: Session, row: ServerPlaylist) -> None:
    from . import sync

    db.execute(delete(MirrorItem).where(MirrorItem.copy_id == row.id))
    db.execute(delete(MirrorExtra).where(MirrorExtra.copy_id == row.id))
    row.mirror, row.due_at, row.state, row.error = False, None, sync.SYNCED, ""
    # Let go of: the copy there is the server's from now on. Never deleted there, not even when the playlist goes for
    # good (``Round.run``); the row stays only so that the server's list never brings it in as a new
    # playlist. Made a mirror or the source there again on purpose, it is taken up (``make_mirror``, ``move``).
    row.settle = LET_GO
    if row.gone_there or row.ext_key is None:
        sync.Round._forget_copy(row)
        row.gone_there, row.resume = False, False
    else:
        row.resume = True


def remake(db: Session, playlist_id: int, server_id: int) -> None:
    """A mirror deleted on its server is made there anew."""
    from . import sync

    row = mirror_row(db, playlist_id, server_id)
    if row is None or not row.gone_there:
        raise error("not_found", "Not found.", 404)
    _lock(db, playlist_id)
    sync.Round._forget_copy(row)
    row.gone_there, row.resume, row.state, row.error, row.tries, row.due_at = (
        False, False, sync.PENDING, "", 0, clock.now())
    db.commit()
    _tell(db, playlist_id, "mirror_remade")


def choose(db: Session, playlist_id: int, server_id: int, entry_id: int, track: int | None) -> None:
    """An entry's track on a mirror chosen by hand (None: left out of the mirror)."""
    from . import sync

    row = mirror_row(db, playlist_id, server_id)
    if row is None or row.gone_there:
        raise error("not_found", "Not found.", 404)
    item = db.get(PlaylistItem, entry_id)
    if item is None or item.playlist_id != playlist_id or item.removed_at is not None:
        raise error("not_found", "Not found.", 404)
    if track is not None and track not in _live_tracks(db, server_id):
        raise error("unknown_track", "The track is not in the server's catalogue.", 422)
    _lock(db, playlist_id)
    if track is not None:
        other = db.scalars(select(MirrorItem).where(MirrorItem.copy_id == row.id, MirrorItem.track_id == track,
                                                    MirrorItem.item_id != entry_id)).first()
        if other is not None:
            raise error("track_taken", "Another entry stands for this track there.", 409)
        # A track shown as "only there": the entry stands for it from now on, it is only there no more (else the sync
        # would hold it out and write it there a second time for the entry).
        db.execute(delete(MirrorExtra).where(MirrorExtra.copy_id == row.id, MirrorExtra.track_id == track))
    db.execute(delete(MirrorItem).where(MirrorItem.copy_id == row.id, MirrorItem.item_id == entry_id))
    db.flush()
    db.add(MirrorItem(copy_id=row.id, item_id=entry_id, track_id=track, how=match.HAND if track else SKIP))
    sync.mark(db, playlist_id, at_once=True)
    db.commit()
    _tell(db, playlist_id, "mirror_chosen")


def take_extra(db: Session, account: Account, playlist_id: int, server_id: int, extra_id: int,
               track: int) -> dict[str, Any]:
    """A track only in a mirror taken over as ``track`` of the source: a new entry at the end, standing for the
    mirror's track there."""
    from . import playlists

    row = mirror_row(db, playlist_id, server_id)
    extra = db.get(MirrorExtra, extra_id)
    if row is None or extra is None or extra.copy_id != row.id or extra.discard:
        raise error("not_found", "Not found.", 404)
    playlist = db.get(Playlist, playlist_id)
    assert playlist is not None
    source = playlist.source_server_id
    known = playlists.catalogue_of(db, [track], server_id=source).get(track)
    if known is None:
        raise error("unknown_track", "The track is not in the server's catalogue.", 422)
    taken_track = extra.track_id
    _lock(db, playlist_id)
    if db.scalar(select(PlaylistItem.id).where(PlaylistItem.playlist_id == playlist_id, PlaylistItem.track_id == track,
                                               PlaylistItem.removed_at.is_(None))):
        db.rollback()
        raise error("already_in", "The track is in the playlist already.", 409)
    if db.scalar(select(MirrorItem.id).where(MirrorItem.copy_id == row.id, MirrorItem.track_id == taken_track)):
        db.rollback()
        raise error("track_taken", "Another entry stands for this track there.", 409)
    last = db.scalar(select(func.max(PlaylistItem.position)).where(PlaylistItem.playlist_id == playlist_id,
                                                                   PlaylistItem.removed_at.is_(None)))
    item = PlaylistItem(playlist_id=playlist_id, track_id=track, position=(last if last is not None else -1) + 1,
                        added_by_user=account.id, added_by_name=(account.display_name or account.name)[:80],
                        added_at=clock.now(), title=known.title[:500], artist=known.artist[:500],
                        album=known.album[:500], album_id=known.album_id, year=known.year, duration=known.duration,
                        guid=known.guid[:200])
    db.add(item)
    db.flush()
    db.add(MirrorItem(copy_id=row.id, item_id=item.id, track_id=taken_track, how=match.HAND))
    db.delete(extra)
    db.flush()
    return playlists._changed(db, account, playlist, "add", playlists.live_items(db, playlist_id), [item])


def emptied(db: Session, account: Account, playlist_id: int, server_id: int, empty: bool) -> None:
    """The answer to a mirror emptied on its server. ``empty``: the playlist is emptied everywhere (in nexmix, on the
    source and on every mirror; its entries taken out as by hand). Else it keeps its entries and the mirror is filled
    with them again (the state both sides agreed on is forgotten, so every entry counts as one to put in there)."""
    from . import playlists, sync

    row = mirror_row(db, playlist_id, server_id)
    note = dict(row.note) if row is not None and isinstance(row.note, dict) else {}
    if row is None or row.gone_there or not note.get("emptied"):
        raise error("not_found", "Not found.", 404)
    _lock(db, playlist_id)
    note.pop("emptied", None)
    row.note = note or None
    if not empty:
        title = row.base.get("title") if isinstance(row.base, dict) else None
        row.base, row.writing = {"title": title or "", "items": []}, None
    row.state, row.error, row.tries, row.due_at = sync.PENDING, "", 0, clock.now()
    db.commit()
    logger.info("Mirror emptied on its server answered playlist=%s server=%s empty=%s", playlist_id, server_id, empty)
    if empty:
        entries = [item.id for item in playlists.live_items(db, playlist_id)]
        db.rollback()
        if entries:
            playlists.remove(db, account, playlist_id, entries)
    _tell(db, playlist_id, "mirror_emptied_answered")


def drop_extra(db: Session, playlist_id: int, server_id: int, extra_id: int) -> None:
    """A track only in a mirror dropped: taken out of the mirror at its next sync."""
    from . import sync

    row = mirror_row(db, playlist_id, server_id)
    extra = db.get(MirrorExtra, extra_id)
    if row is None or extra is None or extra.copy_id != row.id:
        raise error("not_found", "Not found.", 404)
    _lock(db, playlist_id)
    extra.discard = True
    row.due_at, row.state = clock.now(), sync.PENDING
    db.commit()
    _tell(db, playlist_id, "mirror_extra_dropped")


# --- What the interface shows -----------------------------------------------------------------------------------------


def view(db: Session, playlist_id: int) -> list[dict[str, Any]]:
    """The mirrors of a playlist: on which server, how the sync stands there (as for the source; ``gone`` when it was
    deleted there), how many entries are not there, and the tracks only there."""
    from . import sync

    found: list[dict[str, Any]] = []
    for row in mirror_rows(db, playlist_id):
        server = media.get(db, row.server_id)
        if server is None:
            continue
        absent = int(db.scalar(select(func.count()).select_from(MirrorItem).join(
            PlaylistItem, PlaylistItem.id == MirrorItem.item_id).where(
            MirrorItem.copy_id == row.id, MirrorItem.track_id.is_(None), PlaylistItem.removed_at.is_(None))) or 0)
        extras = [{"id": one.id, "title": one.title, "artist": one.artist, "album": one.album,
                   "duration": round(one.duration / 1000)}
                  for one in db.scalars(select(MirrorExtra).where(MirrorExtra.copy_id == row.id,
                                                                  MirrorExtra.discard.is_(False))
                                        .order_by(MirrorExtra.id))]
        state = row.state
        if row.gone_there:
            state = "gone"
        elif not (server.active and server.section_key and server.generation):
            state = "off"
        note = row.note if isinstance(row.note, dict) else {}
        emptied = note.get("emptied") if isinstance(note.get("emptied"), dict) else None
        found.append({
            "server": server.id, "kind": server.kind, "name": server.name, "state": state,
            "error": row.error if state in (sync.ERROR, sync.WAITING) and row.error else None,
            "synced_at": row.synced_at.isoformat() if row.synced_at else None,
            "absent": absent, "extras": extras,
            # Emptied there, waiting for "empty it here too" or "keep" (``emptied``).
            "emptied": {"at": emptied.get("at"), "count": int(emptied.get("count") or 0)} if emptied else None,
        })
    return found


def absent_by_entry(db: Session, playlist_id: int) -> dict[int, list[int]]:
    """For each entry, the servers of the playlist's mirrors that have no track for it."""
    found: dict[int, list[int]] = {}
    for item_id, server in db.execute(select(MirrorItem.item_id, ServerPlaylist.server_id).join(
            ServerPlaylist, ServerPlaylist.id == MirrorItem.copy_id).where(
            ServerPlaylist.playlist_id == playlist_id, ServerPlaylist.mirror.is_(True),
            ServerPlaylist.gone_there.is_(False), MirrorItem.track_id.is_(None))
            .order_by(ServerPlaylist.server_id)):
        found.setdefault(int(item_id), []).append(int(server))
    return found


# --- The mirror's side of a sync (``sync.Round``) ---------------------------------------------------------------------


@dataclass
class Side:
    """A mirror's view of the playlist's entries as one sync read them: each live entry's track on the mirror's
    server (None: none there), how it was found and its hint; the entries in order."""

    source_id: int
    track_of: dict[int, int | None]
    hint_of: dict[int, str]
    order: list[int]

    @property
    def item_of(self) -> dict[int, int]:
        return {track: item for item, track in self.track_of.items() if track is not None}


@dataclass
class Back:
    """What a mirror's merge brings back to the entries: tracks put in there that become entries (``new``: the
    mirror's track → the source's track and how it was found), entries that had no track there and now stand for one
    (``links``: entry → the mirror's track), and tracks only there (``extras``)."""

    new: dict[int, tuple[int, str]] = field(default_factory=dict)
    links: dict[int, tuple[int, str]] = field(default_factory=dict)
    extras: list[Item] = field(default_factory=list)


def prepare(sync_round: Round, playlist_id: int) -> None:
    """Before a mirror's sync reads its server: every live entry has its track there (a sure step, or none), an entry
    whose track the mirror's library lost is looked for again, the tracks only there are held out of the sync, and the
    ones somebody dropped are taken out of the mirror first."""
    db = sync_round.db
    db.rollback()
    _map_new(db, sync_round.ident, playlist_id, sync_round.server_id)
    # A track an entry stands for is never "only there" too (it would be held out, and written there again for the
    # entry): such a line goes, also one dropped (the entry keeps the track there).
    doubled = exists().where(MirrorItem.copy_id == sync_round.ident, MirrorItem.track_id == MirrorExtra.track_id)
    if db.execute(delete(MirrorExtra).where(MirrorExtra.copy_id == sync_round.ident, doubled)).rowcount:
        logger.info("Tracks only in the mirror that an entry stands for now playlist=%s", playlist_id)
    db.commit()
    extras = list(db.scalars(select(MirrorExtra).where(MirrorExtra.copy_id == sync_round.ident,
                                                       MirrorExtra.discard.is_(False))))
    db.rollback()
    sync_round.held_out = {int(one.track_id) for one in extras}
    drop_discarded(sync_round, playlist_id)


def drop_discarded(sync_round: Round, playlist_id: int) -> None:
    """The tracks only there that somebody dropped are taken out of the copy before its sync reads it: a mirror's,
    and a copy that became the source since (moved there before its mirror's sync took them out: they never become
    entries)."""
    db = sync_round.db
    dropping = list(db.scalars(select(MirrorExtra).where(MirrorExtra.copy_id == sync_round.ident,
                                                         MirrorExtra.discard.is_(True))))
    db.rollback()
    key = sync_round.row.ext_key
    if not dropping or key is None:
        return
    from . import sync

    there = {entry.track: entry for entry in sync._numbered(sync_round.server_id, sync_round.source.items(key),
                                                              by_track=sync_round.by_track)}
    refs = [there[int(one.track_id)].ref for one in dropping if int(one.track_id) in there]
    if refs:
        sync_round.source.remove_many(key, refs)
        logger.info("Tracks only in the mirror taken out there playlist=%s count=%s", playlist_id, len(refs))
    db.execute(delete(MirrorExtra).where(MirrorExtra.id.in_([one.id for one in dropping])))
    db.commit()


def _map_new(db: Session, copy_id: int, playlist_id: int, server_id: int) -> None:
    """In a write transaction of its own: entries taken out lose their track here (brought back, they are found
    again), new entries and entries found nowhere before are looked for by a sure step, and an entry whose track the
    mirror's server lost from its library (hint ``missing``) while the catalogue no longer has it either is looked for
    again."""
    from . import playlists

    # The write lock first: a choice by hand waits, and nothing changes the entries meanwhile.
    db.execute(update(ServerPlaylist).where(ServerPlaylist.id == copy_id).values(tries=ServerPlaylist.tries))
    items = playlists.live_items(db, playlist_id)
    live_ids = {item.id for item in items}
    rows = {int(row.item_id): row for row in db.scalars(select(MirrorItem).where(MirrorItem.copy_id == copy_id))}
    gone = [row.id for item_id, row in rows.items() if item_id not in live_ids]
    if gone:
        db.execute(delete(MirrorItem).where(MirrorItem.id.in_(gone)))
    lost = any(row.hint == HINT_MISSING and row.track_id is not None for row in rows.values())
    live = _live_tracks(db, server_id) if lost else set()
    asking: list[PlaylistItem] = []
    for item in items:
        row = rows.get(item.id)
        if row is None or (row.track_id is None and row.how == match.NONE) or (
                row.hint == HINT_MISSING and row.track_id is not None and int(row.track_id) not in live):
            asking.append(item)
    if not asking:
        db.commit()
        return
    playlist = db.get(Playlist, playlist_id)
    source = playlist.source_server_id if playlist is not None else None
    asked = {one.id for one in asking}
    used = {int(row.track_id) for item_id, row in rows.items()
            if item_id in live_ids and row.track_id is not None and item_id not in asked}
    # A track shown "only there" may be found for a new entry: the entry stands for it from then on (``prepare`` takes
    # it off the list of tracks only there).
    decided = decide(db, asking, int(source), server_id, taken=used) if source is not None else {}
    changed = 0
    for item in asking:
        one = decided.get(item.id)
        found = one if one is not None and one.sure else None
        row = rows.get(item.id)
        if row is None:
            db.add(MirrorItem(copy_id=copy_id, item_id=item.id, track_id=found.track if found else None,
                              how=found.how if found else match.NONE))
            changed += 1
        elif found is not None:
            row.track_id, row.how, row.hint = found.track, found.how, ""
            changed += 1
        if found is not None and found.track is not None:
            used.add(found.track)
    db.commit()
    if changed:
        logger.info("Mirror entries looked for playlist=%s server=%s count=%s", playlist_id, server_id, changed)


def side(db: Session, copy_id: int, items: Sequence[PlaylistItem], source_id: int) -> Side:
    rows = {int(row.item_id): row for row in db.scalars(select(MirrorItem).where(MirrorItem.copy_id == copy_id)
                                                        .execution_options(populate_existing=True))}
    return Side(source_id=source_id,
                track_of={item.id: (int(rows[item.id].track_id) if item.id in rows and rows[item.id].track_id
                                    is not None else None) for item in items},
                hint_of={item.id: rows[item.id].hint for item in items if item.id in rows and rows[item.id].hint},
                order=[item.id for item in items])


def back(sync_round: Round, playlist_id: int, remote: Sequence[Item], adds: Sequence[int]) -> Back:
    """The tracks a mirror's merge would bring into nexmix, looked for in the source's library: found by a sure step
    and not an entry yet, a new entry; an entry without a track there, linked to it; anything else only there."""
    found = Back()
    if not adds:
        return found
    db = sync_round.db
    assert sync_round.side is not None
    source_id = sync_round.side.source_id
    by_track = {entry.track: entry for entry in remote}
    described = describe(db, sync_round.server_id, adds)
    wanted = []
    for track in adds:
        entry = by_track.get(track)
        wanted.append(described.get(track) or match.Wanted(
            title=entry.title if entry else "", artist=entry.artist if entry else "",
            album=entry.album if entry else "", duration=entry.duration if entry else 0,
            guid=entry.guid if entry else ""))
    hits = match.assign(wanted, _finder(db, source_id))
    from . import playlists

    items = playlists.live_items(db, playlist_id)
    entry_of = {int(item.track_id): item.id for item in items}
    side_now = sync_round.side
    taken: set[int] = set()
    for track, hit in zip(adds, hits, strict=True):
        source_track = hit.track.id if hit is not None and hit.sure else None
        entry = entry_of.get(source_track) if source_track is not None else None
        if source_track is not None and entry is None and source_track not in taken:
            taken.add(source_track)
            found.new[track] = (source_track, hit.how if hit is not None else match.NONE)
        elif entry is not None and side_now.track_of.get(entry) is None and entry not in found.links:
            found.links[entry] = (track, hit.how if hit is not None else match.NONE)
        elif track in by_track:
            found.extras.append(by_track[track])
    db.rollback()
    return found


def apply(sync_round: Round, playlist_id: int, snapshot: Snapshot, plan: Any) -> tuple[bool, dict[int, int]]:
    """nexmix's side of a mirror's merge, in one transaction that holds only when nobody changed the playlist since it
    was read. Returns whether it held, and the entry of each track of the merged order."""
    from . import playlists, sync

    db = sync_round.db
    merged = plan.merged
    back_now: Back = plan.back
    assert sync_round.side is not None
    item_of = {**sync_round.side.item_of, **{track: entry for entry, (track, _how) in back_now.links.items()}}
    going_ids = [item_of[track] for track in merged.local_removes if track in item_of]
    new_tracks = [track for track in merged.local_adds if track in back_now.new]
    # The order of the entries: the mirror's merged order for the entries it holds (a new one by its track, as a
    # negative number until it has its id), every other entry after the one it followed.
    holders = {**item_of, **{track: -track for track in new_tracks}}
    skeleton = [holders[track] for track in merged.order if track in holders]
    in_skeleton = set(skeleton)
    current = [ident for ident in sync_round.side.order if ident not in set(going_ids)]
    order = merge.place(skeleton, [ident for ident in current if ident not in in_skeleton], current)
    title_changed = plan.title != snapshot.name
    extras_now = {int(one.track) for one in back_now.extras}
    entries_change = bool(going_ids or new_tracks or order != current or title_changed)
    hints_change = bool(back_now.links or plan.unhint or plan.missing or extras_now)
    ids = {track: holders[track] for track in merged.order if track in holders}
    if not entries_change and not hints_change:
        _prune_extras(db, sync_round)
        return True, ids
    now = clock.now()
    taken = db.execute(update(Playlist).where(Playlist.id == playlist_id, Playlist.version == snapshot.version,
                                              Playlist.deleted_at.is_(None),
                                              Playlist.source_server_id == sync_round.side.source_id)
                       .values(version=Playlist.version + 1, updated_at=now)).rowcount
    if taken != 1:
        db.rollback()
        return False, {}
    live = {item.id: item for item in playlists.live_items(db, playlist_id)}
    rows = {int(row.item_id): row for row in db.scalars(select(MirrorItem).where(
        MirrorItem.copy_id == sync_round.ident))}
    going = [live[ident] for ident in going_ids if ident in live]
    for item in going:
        item.removed_at = now
    changed: list[PlaylistItem] = []
    for entry, (track, how) in back_now.links.items():
        row = rows.get(entry)
        if row is None:
            db.add(MirrorItem(copy_id=sync_round.ident, item_id=entry, track_id=track, how=how[:8]))
        else:
            row.track_id, row.how, row.hint = track, how[:8], ""
        if entry in live:
            changed.append(live[entry])
    db.flush()
    by_track = {int(row.track_id): row for row in rows.values() if row.track_id is not None}
    for track in plan.unhint:
        if track in by_track and by_track[track].hint:
            by_track[track].hint = ""
            if int(by_track[track].item_id) in live:
                changed.append(live[int(by_track[track].item_id)])
    for track in plan.missing:
        if track in by_track and by_track[track].hint != HINT_MISSING:
            by_track[track].hint = HINT_MISSING
    new: list[PlaylistItem] = []
    #: The placeholder of each new entry in ``order`` (minus its track), and its id.
    made: dict[int, int] = {}
    if new_tracks:
        source_tracks = [back_now.new[track][0] for track in new_tracks]
        known = playlists.catalogue_of(db, source_tracks, server_id=sync_round.side.source_id)
        remote = {entry.track: entry for entry in plan.remote}
        for track in new_tracks:
            source_track, how = back_now.new[track]
            there = known.get(source_track)
            entry = remote.get(track)
            item = PlaylistItem(
                playlist_id=playlist_id, track_id=source_track, position=0, added_by_user=None,
                added_by_name=sync_round.via, added_at=now, from_plex=True,
                title=(there.title if there else entry.title if entry else "")[:500],
                artist=(there.artist if there else entry.artist if entry else "")[:500],
                album=(there.album if there else entry.album if entry else "")[:500],
                album_id=there.album_id if there else 0, year=there.year if there else None,
                duration=there.duration if there else (entry.duration if entry else 0),
                guid=(there.guid if there else "")[:200])
            db.add(item)
            new.append(item)
            db.flush()
            db.add(MirrorItem(copy_id=sync_round.ident, item_id=item.id, track_id=track, how=how[:8]))
            ids[track] = item.id
            made[-track] = item.id
        db.flush()
    final = [made.get(ident, ident) for ident in order]
    everything = {**live, **{item.id: item for item in new}}
    playlists.reorder(db, [everything[ident] for ident in final if ident in everything])
    if title_changed:
        db.execute(update(Playlist).where(Playlist.id == playlist_id).values(name=plan.title))
    known_extras = {int(track) for track in db.scalars(select(MirrorExtra.track_id).where(
        MirrorExtra.copy_id == sync_round.ident))}
    for one in back_now.extras:
        if int(one.track) not in known_extras:
            db.add(MirrorExtra(copy_id=sync_round.ident, track_id=one.track, title=one.title[:500],
                               artist=one.artist[:500], album=one.album[:500], duration=one.duration, found_at=now))
    db.flush()
    _prune_extras(db, sync_round, commit=False)
    db.commit()
    if entries_change or changed:
        sync.publish_items(db, playlist_id, new, removed=[item.id for item in going], changed=changed)
    if entries_change:
        # The source and the other mirrors get the change.
        sync.mark(db, playlist_id, but=sync_round.ident)
        db.commit()
    if extras_now or back_now.links:
        _tell(db, playlist_id, "mirror_changed")
    if new or going or extras_now:
        logger.info("Changes of a mirror taken in playlist=%s server=%s added=%s removed=%s only_there=%s",
                    playlist_id, sync_round.server_id, len(new), len(going), len(extras_now))
    return True, ids


def _prune_extras(db: Session, sync_round: Round, *, commit: bool = True) -> None:
    """Tracks only in the mirror that the mirror no longer holds go from the list (taken out there)."""
    if not sync_round.raw_tracks and not sync_round.held_out:
        return
    gone = [track for track in sync_round.held_out if track not in sync_round.raw_tracks]
    if gone:
        db.execute(delete(MirrorExtra).where(MirrorExtra.copy_id == sync_round.ident,
                                             MirrorExtra.track_id.in_(gone), MirrorExtra.discard.is_(False)))
        if commit:
            db.commit()
        sync_round.held_out -= set(gone)


def refused(db: Session, copy_id: int, tracks: Sequence[int]) -> None:
    """The mirror's server refused the tracks for good: their entries say so for this mirror."""
    db.execute(update(MirrorItem).where(MirrorItem.copy_id == copy_id, MirrorItem.track_id.in_(list(tracks)))
               .values(hint=HINT_REFUSED))
    db.commit()


def after_catalogue(db: Session, server_id: int) -> int:
    """After a run of the catalogue of ``server_id``: the mirrors there with an entry the server lost or found nowhere
    are due (their sync looks for those tracks again). Returns how many."""
    from . import sync

    due = list(db.scalars(select(ServerPlaylist.id).where(
        ServerPlaylist.server_id == server_id, ServerPlaylist.mirror.is_(True),
        ServerPlaylist.gone_there.is_(False), ServerPlaylist.playlist_id.is_not(None),
        exists().where(MirrorItem.copy_id == ServerPlaylist.id,
                       (MirrorItem.hint == HINT_MISSING) | ((MirrorItem.track_id.is_(None))
                                                           & (MirrorItem.how == match.NONE))))))
    if due:
        db.execute(update(ServerPlaylist).where(ServerPlaylist.id.in_(due))
                   .values(due_at=clock.now(), state=sync.PENDING))
    db.commit()
    return len(due)


# --- Letting go of a server -------------------------------------------------------------------------------------------


def sources_on(db: Session, server_id: int) -> int:
    """How many playlists (not in the trash) take their tracks from the server."""
    return int(db.scalar(select(func.count()).select_from(Playlist).where(
        Playlist.source_server_id == server_id, Playlist.deleted_at.is_(None))) or 0)


def may_let_go(db: Session, server: MediaServer) -> None:
    """Refuses (409 ``server_is_source``) while a playlist takes its tracks from the server and another server is
    there to take them over (move them first). A server alone may go as before: its playlists wait for it in nexmix
    (nothing is lost, and there is nowhere to move them)."""
    count = sources_on(db, server.id)
    others = [one for one in media.active(db) if one.id != server.id]
    if count and others:
        raise error("server_is_source", "Playlists take their tracks from this server.", 409, count=count)


def forget_server(db: Session, server_id: int) -> int:
    """The server is let go of: its mirrors are forgotten (they rest there, never taken in as new). Returns how
    many."""
    rows = list(db.scalars(select(ServerPlaylist).where(ServerPlaylist.server_id == server_id,
                                                        ServerPlaylist.mirror.is_(True))))
    for row in rows:
        _rest(db, row)
    db.flush()
    if rows:
        logger.info("Mirrors on a server let go of forgotten server=%s count=%s", server_id, len(rows))
    return len(rows)


# --- Take all over ----------------------------------------------------------------------------------------------------


def candidates(db: Session, target_id: int, mode: str, source_id: int | None = None) -> tuple[list[int], int]:
    """The playlists "take all over" would take: of the other servers (``source_id``: of that one), not in the trash,
    not smart (counted apart), not mirrored on the target already when mirrored. Returns them and how many smart ones
    stay."""
    from . import sync

    query = select(Playlist.id).where(Playlist.deleted_at.is_(None), Playlist.source_server_id.is_not(None),
                                      Playlist.source_server_id != target_id)
    if source_id is not None:
        query = query.where(Playlist.source_server_id == source_id)
    chosen: list[int] = []
    smart = 0
    for playlist_id in db.scalars(query.order_by(Playlist.id)):
        if sync.is_smart(db, playlist_id):
            smart += 1
            continue
        mirrored = mirror_row(db, playlist_id, target_id)
        if mode == "mirror" and mirrored is not None and not mirrored.gone_there:
            continue
        chosen.append(int(playlist_id))
    return chosen, smart


def transfer_view(row: Transfer | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {"id": row.id, "server": row.server_id, "mode": row.mode, "old": row.old, "state": row.state,
            "total": row.total, "done": row.done, "skipped": row.skipped, "stopping": bool(row.stopping),
            "error": row.error or None, "disconnect": row.then_disconnect,
            "started_at": row.started_at.isoformat(), "finished_at": row.finished_at.isoformat()
            if row.finished_at else None}


def latest_transfer(db: Session, server_id: int) -> Transfer | None:
    return db.scalars(select(Transfer).where(Transfer.server_id == server_id).order_by(Transfer.id.desc())
                      .limit(1).execution_options(populate_existing=True)).first()


def start_transfer(db: Session, account: Account, target_id: int, mode: str, *, old: str = "keep",
                   choices: dict[int, int | None] | None = None, source_id: int | None = None,
                   disconnect: int | None = None) -> Transfer:
    if mode not in MODES or old not in OLD:
        raise error("invalid", "Invalid value.", 422)
    target = _target(db, target_id)
    if disconnect is not None and (mode != "move" or disconnect == target.id or media.get(db, disconnect) is None):
        raise error("invalid", "Invalid value.", 422)
    running = db.scalar(select(Transfer.id).where(Transfer.state == RUNNING))
    if running is not None:
        raise error("transfer_running", "Another transfer is under way.", 409)
    chosen, _smart = candidates(db, target.id, mode, disconnect if disconnect is not None else source_id)
    row = Transfer(server_id=target.id, mode=mode, old=old, state=RUNNING, todo=chosen,
                   choices={str(key): value for key, value in (choices or {}).items()}, total=len(chosen),
                   then_disconnect=disconnect, created_by=account.id, started_at=clock.now())
    db.add(row)
    db.flush()
    jobs.enqueue(db, TRANSFER, ref=row.id)
    db.commit()
    logger.info("Transfer started transfer=%s server=%s mode=%s playlists=%s", row.id, target.id, mode, len(chosen))
    return row


def stop_transfer(db: Session, server_id: int) -> Transfer:
    row = latest_transfer(db, server_id)
    if row is None or row.state != RUNNING:
        raise error("not_found", "Not found.", 404)
    row.stopping = True
    db.commit()
    return row


def _choices_of(row: Transfer) -> dict[int, int | None]:
    found: dict[int, int | None] = {}
    for key, value in (row.choices or {}).items():
        try:
            found[int(key)] = int(value) if value is not None else None
        except (TypeError, ValueError):
            continue
    return found


def run_transfer(db: Session, job: Job) -> None:
    """One slice of a transfer: playlists one after another until ``TRANSFER_SLICE`` is up, then the next slice after
    whatever else waits in the lane."""
    from fastapi import HTTPException

    row = db.get(Transfer, int(job.ref), populate_existing=True)
    if row is None or row.state != RUNNING:
        return
    started = clock.monotonic()
    choices = _choices_of(row)
    while True:
        db.rollback()
        row = db.get(Transfer, int(job.ref), populate_existing=True)
        if row is None:
            return
        todo = [int(one) for one in (row.todo or [])]
        if row.stopping:
            row.state, row.finished_at = STOPPED, clock.now()
            db.commit()
            logger.info("Transfer stopped transfer=%s done=%s of=%s", row.id, row.done, row.total)
            return
        if not todo:
            _finish(db, row)
            return
        if jobs.stopping.is_set():
            raise jobs.Interrupted
        playlist_id = todo[0]
        try:
            if row.mode == "move":
                move(db, playlist_id, row.server_id, choices, row.old, commit=False)
            else:
                make_mirror(db, playlist_id, row.server_id, choices, commit=False, unsure=False)
            outcome = "done"
        except HTTPException:
            db.rollback()
            outcome = "skipped"
        except Exception:
            db.rollback()
            logger.exception("Transfer of a playlist failed transfer=%s playlist=%s", row.id, playlist_id)
            outcome = "skipped"
        row = db.get(Transfer, int(job.ref), populate_existing=True)
        assert row is not None
        row.todo = todo[1:]
        if outcome == "done":
            row.done += 1
        else:
            row.skipped += 1
        db.commit()
        if outcome == "done":
            _tell(db, playlist_id, "moved" if row.mode == "move" else "mirrored")
        if clock.monotonic() - started >= TRANSFER_SLICE:
            jobs.enqueue(db, TRANSFER, ref=row.id)
            db.commit()
            return


def _finish(db: Session, row: Transfer) -> None:
    row.state, row.finished_at = DONE, clock.now()
    if row.then_disconnect is not None:
        server = media.get(db, row.then_disconnect)
        if server is not None and server.active:
            if sources_on(db, server.id):
                row.error = "still_source"
            else:
                disconnect(db, server)
    db.commit()
    logger.info("Transfer done transfer=%s done=%s skipped=%s", row.id, row.done, row.skipped)


def disconnect(db: Session, server: MediaServer) -> None:
    """Lets go of a server that is no playlist's source: its mirrors are forgotten, its token and catalogue go."""
    from ..models import SERVER_JELLYFIN
    from . import jellyfin_link, plex_link

    forget_server(db, server.id)
    if server.kind == SERVER_JELLYFIN:
        jellyfin_link.rest(db, server)
    else:
        plex_link.rest(db, server)


def settle_transfers(db: Session) -> None:
    """At the start of the lane: a transfer that runs without a job (the server stopped) goes on."""
    carried = exists().where(Job.kind == TRANSFER, Job.ref == Transfer.id, Job.state.in_(("queued", "running")))
    for ident in db.scalars(select(Transfer.id).where(Transfer.state == RUNNING, ~carried)):
        jobs.enqueue(db, TRANSFER, ref=int(ident))
    db.commit()


jobs.register(TRANSFER, run_transfer, settle_transfers)
