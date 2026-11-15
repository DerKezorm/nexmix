"""The catalogue: the music library of every connected media server, kept in nexmix so that searching and suggesting
never wait for a server. Each server has its own (``server_id`` in every row); what is special about fetching from a
kind of server is its connector's (``sources/plex.py``), what is written and when is here.

**Full run** (``catalog_full``): the connector fills a new *generation* through a ``Writer`` (rows, tags, progress),
then the songs (``services/songs.py``) are worked out, and the last step makes the generation the live one of the
server in a single statement. A run that breaks off leaves the old catalogue as it was.

**Readers** never see half a catalogue: a way of reading that takes more than one query begins with ``pin``, which
holds one state of the database for the rest of its session and says which generation is live in it. The generation
before stays on disk until the next full run begins, for whoever is still reading it.

**Partial run** (``catalog_part``), every 15 minutes per server: the connector says what changed since the last run
(or that a full run is needed, ``TooMany``); all of it is written in one transaction, and only the songs it touched are
worked out again.

**Nightly**, at ``catalog_full_at`` on the server's clock, a full run of every server; it sets the marks anew.

Both kinds run in the lane ``catalog`` (``services/jobs.py``): one at a time, never two, and a stopped server takes
up again where the job was waiting. A job names its media server (``ref``). Letting go of a server
(``media.drop_catalog``) stops a run under way at its next request. Runs and finished jobs older than 30 days go, and
never more than 1,000 of each stay.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta, tzinfo
from typing import Any

from sqlalchemy import Table, delete, func, insert, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .. import clock
from ..db import SessionLocal
from ..models import (
    SERVER_JELLYFIN,
    AlbumTag,
    ArtistTag,
    Base,
    CatalogAlbum,
    CatalogArtist,
    CatalogTag,
    CatalogTrack,
    Job,
    MediaServer,
    SyncRun,
    TrackTag,
)
from . import catalog_view, jobs, media, plex_link, relink, settings_service, songs, sources
from .plex import ALBUM, ARTIST, TRACK, PlexError

logger = logging.getLogger("nexmix.catalog")

LANE = "catalog"
FULL = "catalog_full"
PART = "catalog_part"
KINDS = (FULL, PART)
PARTIAL_EVERY = timedelta(minutes=15)
#: Runs and finished jobs: none older than this, never more than this many.
KEEP_DAYS = 30
KEEP_RUNS = 1000
SWEEP_EVERY = timedelta(hours=1)
ANALYSIS_EVERY = timedelta(hours=1)
#: How long the loop waits between two looks at the clock.
TICK_SECONDS = 60
TYPES = (ARTIST, ALBUM, TRACK)
#: A server that is still starting (Jellyfin answers 503 for up to a minute when it starts), after the connector
#: waited for it: the run stops without counting as failed.
STARTING = frozenset({"jellyfin_starting"})

#: The share of a full run each step takes, for the progress the interface shows.
WEIGHTS = {"artists": 2, "albums": 6, "tracks": 25, "album_tags": 10, "artist_tags": 4, "track_moods": 40,
           "analysis": 3, "songs": 6, "switch": 4}

_TABLES: tuple[type[Base], ...] = (TrackTag, AlbumTag, ArtistTag, CatalogTag, CatalogTrack, CatalogAlbum, CatalogArtist)
_LINKS: dict[int, type[Base]] = {ARTIST: ArtistTag, ALBUM: AlbumTag, TRACK: TrackTag}
_ROWS: dict[int, type[Base]] = {ARTIST: CatalogArtist, ALBUM: CatalogAlbum, TRACK: CatalogTrack}


def local_zone() -> tzinfo:
    """The server's own time zone, for the nightly run. The tests set it."""
    zone = datetime.now().astimezone().tzinfo
    assert zone is not None
    return zone


class Stale(Exception):
    """The server or the library changed while the run was under way: its catalogue is not the one wanted any more."""


class TooMany(Exception):
    """More changed than a partial run takes."""


def _watch(server_id: int, started: int) -> Callable[[], bool]:
    """What a run asks before each request to its server: stop for good when the catalogue was let go of meanwhile
    (``Stale``), stop for now when nexmix is stopping (True)."""

    def stop() -> bool:
        if media.epoch(server_id) != started:
            raise Stale
        return jobs.stopping.is_set()

    return stop


# --- Reading -------------------------------------------------------------------------------------------------------


def hold(db: Session) -> None:
    """One state of the database for the rest of the session's transaction (SQLite holds it for a reader in WAL
    mode): a full run that switches over meanwhile, or a partial run that writes, changes nothing of what this session
    sees. The transaction ends with the session (or ``rollback``); it only reads, so it keeps nobody waiting."""
    connection = db.connection()
    raw = connection.connection.dbapi_connection
    if raw is not None and not raw.in_transaction:
        connection.exec_driver_sql("BEGIN")


def pin(db: Session, server_id: int | None = None) -> int:
    """``hold``, and the generation live in that state: of ``server_id``, or of the first connected server."""
    hold(db)
    if server_id is None:
        row = media.primary(db)
        return int(row.generation) if row is not None else 0
    return media.generation_of(db, server_id)


def live(db: Session) -> dict[int, int]:
    """``hold``, and the live generation of every active server that has one."""
    hold(db)
    return {row.id: int(row.generation) for row in media.active(db) if row.generation}


# --- Asking for runs -----------------------------------------------------------------------------------------------


def _busy(db: Session, server_id: int) -> bool:
    found = db.scalar(select(Job.id).where(Job.kind.in_(KINDS), Job.ref == server_id,
                                           Job.state.in_(("queued", "running"))).limit(1))
    return found is not None


def _target(db: Session, server_id: int | None) -> MediaServer | None:
    row = media.get(db, server_id) if server_id is not None else media.primary(db)
    return row if row is not None and row.active and row.section_key else None


def request_full(db: Session, reason: str, server_id: int | None = None) -> bool:
    """A full run of the server (None: the first one connected), unless one is waiting or running already (a double
    click starts one). False: nothing new. A run that is done with the server and only prepares the search and the
    packed catalogue (a second or two, and the status says "done" already) does not count: asked then, the next run
    waits behind it."""
    row = _target(db, server_id)
    if row is None:
        return False
    waiting = db.scalar(select(Job.id).where(Job.kind == FULL, Job.ref == row.id, Job.state == "queued").limit(1))
    running = db.scalar(select(SyncRun.id).where(SyncRun.kind == "full", SyncRun.server_id == row.id,
                                                 SyncRun.state == "running").limit(1))
    if waiting is not None or running is not None:
        return False
    return _enqueue(db, FULL, row.id, {"serial": row.serial, "reason": reason})


def request_partial(db: Session, reason: str = "schedule", server_id: int | None = None) -> bool:
    row = _target(db, server_id)
    if row is None or row.generation == 0:
        return False
    if _busy(db, row.id):
        return False
    return _enqueue(db, PART, row.id, {"serial": row.serial, "reason": reason})


def _enqueue(db: Session, kind: str, server_id: int, payload: dict[str, Any]) -> bool:
    """The job, unless one of the kind waits already for the server (the unique index decides between two asking at
    once). True: this call put it there."""
    made = db.execute(sqlite_insert(Job).values(kind=kind, ref=server_id, payload=payload, state="queued",
                                                created_at=clock.now()).on_conflict_do_nothing())
    db.commit()
    return bool(made.rowcount)


# --- Tags ------------------------------------------------------------------------------------------------------------


class Tags:
    """The tags of one generation of one server: their ids by kind and name, and the links found, written in bulk."""

    def __init__(self, db: Session, server_id: int, generation: int) -> None:
        self.server_id = server_id
        self.generation = generation
        self.ids: dict[tuple[str, str], int] = {}
        self.new: list[dict[str, Any]] = []
        self.links: dict[int, set[tuple[int, int]]] = {ARTIST: set(), ALBUM: set(), TRACK: set()}
        for tag in db.scalars(select(CatalogTag).where(CatalogTag.server_id == server_id,
                                                       CatalogTag.generation == generation)):
            self.ids[(tag.kind, tag.name)] = tag.id
        self.next = max(self.ids.values(), default=0) + 1

    def id_of(self, kind: str, name: str) -> int:
        key = (kind, name)
        found = self.ids.get(key)
        if found is None:
            found = self.ids[key] = self.next
            self.next += 1
            self.new.append({"server_id": self.server_id, "generation": self.generation, "id": found, "kind": kind,
                             "name": name})
        return found

    def link(self, of_type: int, item: int, kind: str, name: str) -> None:
        self.links[of_type].add((item, self.id_of(kind, name)))

    def write(self, db: Session) -> None:
        if self.new:
            db.execute(insert(CatalogTag), self.new)
            self.new = []
        for of_type, pairs in self.links.items():
            if pairs:
                rows = [{"server_id": self.server_id, "generation": self.generation, "item_id": item, "tag_id": tag}
                        for item, tag in pairs]
                db.execute(insert(_table(_LINKS[of_type])).prefix_with("OR IGNORE"), rows)
            self.links[of_type] = set()


def _table(model: type[Base]) -> Table:
    table = model.__table__
    assert isinstance(table, Table)
    return table


# --- Progress ------------------------------------------------------------------------------------------------------


class Progress:
    def __init__(self, db: Session, run: SyncRun, stop: Callable[[], bool]) -> None:
        self.db = db
        self.run = run
        self.stop = stop
        self.finished = 0
        self.total = sum(WEIGHTS.values())
        self.timings: dict[str, float] = {}
        self._step_started = time.monotonic()
        self._last = 0.0

    def step(self, name: str) -> None:
        if self.run.step and self.run.step in WEIGHTS:
            self.finished += WEIGHTS[self.run.step]
            self.timings[self.run.step] = round(time.monotonic() - self._step_started, 2)
        self._step_started = time.monotonic()
        self.run.step = name
        self.share(0.0, force=True)

    def share(self, fraction: float, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last < 0.5:
            return
        self._last = now
        current = WEIGHTS.get(self.run.step, 0) * min(1.0, max(0.0, fraction))
        self.run.progress = int(1000 * (self.finished + current) / self.total)
        self.db.commit()

    def done(self) -> None:
        if self.run.step:
            self.timings[self.run.step] = round(time.monotonic() - self._step_started, 2)
        self.run.timings = dict(self.timings)
        self.run.progress = 1000

    def check(self) -> None:
        """Between two pieces of work that ask the server nothing: the same question as before each request."""
        if self.stop():
            raise PlexError("stopped", 503)


class Writer:
    """What a connector writes a run through: rows of a generation of one server by nexmix's numbers, tags, the
    progress, and the numbers of the server's keys (``media.Numbers``)."""

    def __init__(self, db: Session, server_id: int, generation: int, progress: Progress) -> None:
        self.db = db
        self.server_id = server_id
        self.generation = generation
        self.progress = progress
        self.tags = Tags(db, server_id, generation)
        self.numbers = media.Numbers(db, server_id)

    def step(self, name: str) -> None:
        self.progress.step(name)

    def share(self, fraction: float) -> None:
        self.progress.share(fraction)

    def check(self) -> None:
        self.progress.check()

    def commit(self) -> None:
        self.db.commit()

    def rows(self, of_type: int, rows: list[dict[str, Any]]) -> None:
        if rows:
            self.db.execute(insert(_table(_ROWS[of_type])),
                            [{**row, "server_id": self.server_id, "generation": self.generation} for row in rows])

    def link(self, of_type: int, item: int, kind: str, name: str) -> None:
        self.tags.link(of_type, item, kind, name)

    def ours(self, of_type: int) -> set[int]:
        """The numbers the generation holds of a type."""
        model = _table(_ROWS[of_type])
        return set(self.db.scalars(select(model.c.id).where(model.c.server_id == self.server_id,
                                                            model.c.generation == self.generation)))


# --- The full run ----------------------------------------------------------------------------------------------------


def _drop_generation(db: Session, server_id: int, generation: int) -> None:
    for model in _TABLES:
        table = _table(model)
        db.execute(delete(table).where(table.c.server_id == server_id, table.c.generation == generation))


def _drop_other_generations(db: Session, server_id: int, keep: int) -> None:
    for model in _TABLES:
        table = _table(model)
        db.execute(delete(table).where(table.c.server_id == server_id, table.c.generation != keep))


def _drop_unborn_generations(db: Session, server_id: int, live_one: int) -> None:
    """The rows of runs that never went live (a run that died with the server). The generation before the live one
    stays: somebody may still be reading it (``pin``)."""
    for model in _TABLES:
        table = _table(model)
        db.execute(delete(table).where(table.c.server_id == server_id, table.c.generation > live_one))


_TRACK_FIELDS = (
    CatalogTrack.id, CatalogTrack.title, CatalogTrack.album_id, CatalogTrack.artist_id, CatalogTrack.artist,
    CatalogTrack.year, CatalogTrack.rating_count, CatalogTrack.view_count, CatalogTrack.song_key,
    CatalogTrack.variant, CatalogTrack.shown, CatalogTrack.original_year,
)
#: Ids in one ``IN (...)``: well below SQLite's limit of bound values.
_CHUNK = 500


def _rows(db: Session, statement: Any) -> list[Any]:
    return list(db.execute(statement).all())


def _chunks(values: Iterable[Any]) -> Iterable[list[Any]]:
    chosen = list(values)
    for start in range(0, len(chosen), _CHUNK):
        yield chosen[start:start + _CHUNK]


def _tracks_where(db: Session, server_id: int, generation: int, column: Any, values: Iterable[Any]) -> list[Any]:
    found: list[Any] = []
    for part in _chunks(values):
        found.extend(db.execute(select(*_TRACK_FIELDS).where(
            CatalogTrack.server_id == server_id, CatalogTrack.generation == generation, column.in_(part))).all())
    return found


class _Names:
    """Titles and artists of the albums and the names of the artists a set of tracks needs, read once."""

    def __init__(self, db: Session, server_id: int, generation: int) -> None:
        self.db = db
        self.server_id = server_id
        self.generation = generation
        self.albums: dict[int, tuple[str, int]] = {}
        self.artists: dict[int, str] = {}

    def load(self, rows: list[Any]) -> None:
        wanted = {row.album_id for row in rows} - set(self.albums)
        for part in _chunks(wanted):
            statement = select(CatalogAlbum.id, CatalogAlbum.title, CatalogAlbum.artist_id).where(
                CatalogAlbum.server_id == self.server_id, CatalogAlbum.generation == self.generation,
                CatalogAlbum.id.in_(part))
            for album in _rows(self.db, statement):
                self.albums[album.id] = (album.title, album.artist_id)
        people = ({row.artist_id for row in rows} | {album[1] for album in self.albums.values()}) - set(self.artists)
        for part in _chunks(people):
            statement = select(CatalogArtist.id, CatalogArtist.title).where(
                CatalogArtist.server_id == self.server_id, CatalogArtist.generation == self.generation,
                CatalogArtist.id.in_(part))
            for artist in _rows(self.db, statement):
                self.artists[artist.id] = artist.title

    def version(self, row: Any) -> songs.Version:
        album_title, album_artist_id = self.albums.get(row.album_id, ("", row.artist_id))
        return songs.Version(
            id=row.id, title=row.title, album_title=album_title,
            album_artist=self.artists.get(row.artist_id) or self.artists.get(album_artist_id) or "",
            # The year is the album's; a partial run that changes an album writes it on its tracks first.
            track_artist=row.artist, year=row.year, rating_count=row.rating_count, view_count=row.view_count,
        )


def compute_songs(db: Session, generation: int, tracks: Iterable[int] | None = None,
                  keys: Iterable[str] = (), *, server_id: int | None = None) -> None:
    """Song key, side version, version shown and original year; only what changed is written. Songs are a server's
    own: versions on two servers are never one song here.

    ``tracks`` None: every track of the generation (a full run). Otherwise the songs of these tracks only, as they
    are now and as they were before (``keys``, their song keys of before): the songs they joined and the songs they
    left, each whole, and nothing else."""
    if server_id is None:
        row = media.primary(db)
        server_id = row.id if row is not None else 0
    names = _Names(db, server_id, generation)
    if tracks is None:
        rows = _rows(db, select(*_TRACK_FIELDS).where(CatalogTrack.server_id == server_id,
                                                      CatalogTrack.generation == generation))
        names.load(rows)
    else:
        rows = _tracks_where(db, server_id, generation, CatalogTrack.id, set(tracks))
        names.load(rows)
        mine = {row.id for row in rows}
        wanted = set(keys)
        for row in rows:
            version = names.version(row)
            wanted.add(songs.song_key(version.id, version.title, version.album_artist, version.track_artist))
        # Keys of a single track (``#<id>``) belong to nobody else.
        others = [row for row in _tracks_where(db, server_id, generation, CatalogTrack.song_key,
                                               {key for key in wanted if not key.startswith("#")})
                  if row.id not in mine]
        names.load(others)
        rows += others
    if not rows:
        return
    found = songs.songs(names.version(row) for row in rows)
    changes = []
    for row in rows:
        song = found[row.id]
        if (row.song_key, bool(row.variant), bool(row.shown), row.original_year) != (
                song.key, song.side, song.shown, song.original_year):
            changes.append({"server_id": server_id, "generation": generation, "id": row.id, "song_key": song.key,
                            "variant": song.side, "shown": song.shown, "original_year": song.original_year})
    for start in range(0, len(changes), 2000):
        db.execute(update(CatalogTrack), changes[start:start + 2000])


def count_songs(db: Session, server_id: int, generation: int) -> int:
    return int(db.scalar(select(func.count()).select_from(CatalogTrack).where(
        CatalogTrack.server_id == server_id, CatalogTrack.generation == generation,
        CatalogTrack.shown.is_(True))) or 0)


def _full(db: Session, row: MediaServer, run: SyncRun, stop: Callable[[], bool]) -> None:
    generation = run.generation
    progress = Progress(db, run, stop)
    # Room for the new generation: the one before the live one goes now, whoever read it has long finished.
    _drop_other_generations(db, row.id, row.generation)
    db.commit()
    writer = Writer(db, row.id, generation, progress)
    with sources.open_server(db, row, stop=stop) as source:
        result = source.full(writer)
    writer.tags.write(db)
    db.commit()
    progress.step("songs")
    compute_songs(db, generation, server_id=row.id)
    run.songs = count_songs(db, row.id, generation)
    db.commit()
    progress.step("switch")
    numbers = result["counts"]
    run.artists, run.albums, run.tracks = numbers["artists"], numbers["albums"], numbers["tracks"]
    now = clock.now()
    switched = db.execute(
        update(MediaServer).where(MediaServer.id == row.id, MediaServer.serial == run.serial,
                                  MediaServer.active.is_(True))
        .values(generation=generation, marks=result["marks"], synced_at=now, full_at=now, **result["analysis"])
    ).rowcount
    if switched != 1:
        db.rollback()
        raise Stale
    progress.done()
    run.state, run.finished_at = "done", now
    db.commit()
    # The generation before stays until the next full run begins: a reader may be in the middle of it (``pin``).


# --- The partial run -------------------------------------------------------------------------------------------------


def _replace(db: Session, server_id: int, generation: int, of_type: int, rows: dict[int, dict[str, Any]],
             tags: dict[int, dict[str, list[str]]], writer: Writer) -> None:
    """Entries of one type taken in afresh, with all their tags."""
    if not rows:
        return
    model, link = _table(_ROWS[of_type]), _table(_LINKS[of_type])
    keys = list(rows)
    for start in range(0, len(keys), 500):
        part = keys[start:start + 500]
        db.execute(delete(model).where(model.c.server_id == server_id, model.c.generation == generation,
                                       model.c.id.in_(part)))
        db.execute(delete(link).where(link.c.server_id == server_id, link.c.generation == generation,
                                      link.c.item_id.in_(part)))
    writer.rows(of_type, list(rows.values()))
    for number, found in tags.items():
        for kind, names in found.items():
            for name in names:
                writer.link(of_type, number, kind, name)


def _remove(db: Session, server_id: int, generation: int, of_type: int, numbers: set[int]) -> None:
    model, link = _table(_ROWS[of_type]), _table(_LINKS[of_type])
    chosen = list(numbers)
    for start in range(0, len(chosen), 500):
        part = chosen[start:start + 500]
        db.execute(delete(model).where(model.c.server_id == server_id, model.c.generation == generation,
                                       model.c.id.in_(part)))
        db.execute(delete(link).where(link.c.server_id == server_id, link.c.generation == generation,
                                      link.c.item_id.in_(part)))


def _touched(db: Session, server_id: int, generation: int, changed: dict[int, dict[int, Any]],
             gone: dict[int, set[int]]) -> dict[int, str]:
    """The tracks whose song may change with what a partial run takes in, with their song keys as they stand: the
    tracks themselves, the tracks of changed or gone albums, and of changed or gone artists."""
    tracks = set(changed[TRACK]) | gone.get(TRACK, set())
    albums = set(changed[ALBUM]) | gone.get(ALBUM, set())
    artists = set(changed[ARTIST]) | gone.get(ARTIST, set())
    for part in _chunks(artists):
        albums.update(db.scalars(select(CatalogAlbum.id).where(
            CatalogAlbum.server_id == server_id, CatalogAlbum.generation == generation,
            CatalogAlbum.artist_id.in_(part))))
    found: dict[int, str] = {}
    for column, values in ((CatalogTrack.id, tracks), (CatalogTrack.album_id, albums),
                           (CatalogTrack.artist_id, artists)):
        for row in _tracks_where(db, server_id, generation, column, values):
            found[row.id] = row.song_key
    return found


def _partial(db: Session, row: MediaServer, run: SyncRun, stop: Callable[[], bool]) -> None:
    generation = row.generation
    marks = {str(key): int(value) for key, value in dict(row.marks or {}).items()}
    progress = Progress(db, run, stop)
    writer = Writer(db, row.id, generation, progress)
    due = row.analysis_at is None or clock.now() - row.analysis_at >= ANALYSIS_EVERY
    with sources.open_server(db, row, stop=stop) as source:
        found = source.partial(writer, marks, due)
    rows, tags, gone = found["rows"], found["tags"], found["gone"]
    progress.step("write")
    removed = 0
    if any(rows.values()) or any(gone.values()):
        before = _touched(db, row.id, generation, rows, gone)
        for of_type in TYPES:
            _replace(db, row.id, generation, of_type, rows[of_type], tags[of_type], writer)
        for of_type, numbers in gone.items():
            _remove(db, row.id, generation, of_type, numbers)
            removed += len(numbers)
        # The year of a track is its album's: a changed album changes it on its tracks at once.
        for album in rows[ALBUM].values():
            db.execute(update(CatalogTrack).where(
                CatalogTrack.server_id == row.id, CatalogTrack.generation == generation,
                CatalogTrack.album_id == album["id"], CatalogTrack.year.is_not(album["year"]))
                .values(year=album["year"]))
        writer.tags.write(db)
        after = _touched(db, row.id, generation, rows, gone)
        compute_songs(db, generation, tracks=set(before) | set(after), keys=set(before.values()), server_id=row.id)
    now = clock.now()
    switched = db.execute(
        update(MediaServer).where(MediaServer.id == row.id, MediaServer.serial == run.serial,
                                  MediaServer.generation == generation, MediaServer.active.is_(True))
        .values(marks=found["marks"], synced_at=now, **found["analysis"])
    ).rowcount
    if switched != 1:
        db.rollback()
        raise Stale
    run.changed = sum(len(entries) for entries in rows.values())
    run.removed = removed
    progress.done()
    run.state, run.finished_at = "done", now
    db.commit()
    # The numbers of the run are read after the write: counting holds no lock that a writer would wait for.
    numbers = counts(db, generation, row.id)
    run.artists, run.albums, run.tracks, run.songs = (numbers["artists"], numbers["albums"], numbers["tracks"],
                                                      numbers["songs"])
    db.commit()


# --- The jobs --------------------------------------------------------------------------------------------------------


def _server_of(db: Session, job: Job) -> int:
    """The server a job is for; a job without one (of an older nexmix) is for the first one connected."""
    if job.ref:
        return int(job.ref)
    first = media.primary(db)
    return first.id if first is not None else 0


def _start(db: Session, job: Job, kind: str) -> tuple[MediaServer, SyncRun, Callable[[], bool]] | None:
    server_id = _server_of(db, job)
    # Read before the server: a catalogue let go of in between stops the run at once.
    stop = _watch(server_id, media.epoch(server_id))
    row = media.get(db, server_id)
    payload = job.payload if isinstance(job.payload, dict) else {}
    if row is None or not row.active or not row.section_key:
        return None
    if payload.get("serial") not in (None, row.serial):
        return None
    run = SyncRun(kind=kind, reason=str(payload.get("reason") or "")[:16], state="running", serial=row.serial,
                  generation=row.generation + 1 if kind == "full" else row.generation, started_at=clock.now(),
                  server_id=row.id)
    db.add(run)
    db.commit()
    return row, run, stop


def _fail(db: Session, run: SyncRun, code: str, *, state: str = "failed") -> None:
    """The run ends without its catalogue: for a full one, the generation it wrote goes; the live one stays."""
    db.rollback()
    if run.kind == "full":
        live_one = media.generation_of(db, run.server_id)
        if live_one != run.generation:
            _drop_generation(db, run.server_id, run.generation)
    db.execute(update(SyncRun).where(SyncRun.id == run.id)
               .values(state=state, error=code[:40], finished_at=clock.now()))
    db.commit()


def _run(db: Session, job: Job, kind: str,
         work: Callable[[Session, MediaServer, SyncRun, Callable[[], bool]], None]) -> None:
    started = _start(db, job, kind)
    if started is None:
        return
    row, run, stop = started
    try:
        work(db, row, run, stop)
    except TooMany:
        _fail(db, run, "", state="done")
        db.execute(update(SyncRun).where(SyncRun.id == run.id).values(error="", step="many"))
        db.commit()
        request_full(db, "many", row.id)
    except Stale:
        # Let go of meanwhile: the server let go of, or another one or another library chosen.
        db.rollback()
        still = media.get(db, run.server_id)
        _fail(db, run, "disconnected" if still is None or not still.active else "replaced", state="stopped")
        logger.info("Catalogue run stopped kind=%s step=%s server=%s", kind, run.step, run.server_id)
    except plex_link.Unreadable as exc:
        _fail(db, run, "token_unreadable")
        raise jobs.GiveUp("token_unreadable") from exc
    except PlexError as exc:
        if exc.code == "stopped":
            _fail(db, run, "stopped", state="stopped")
            raise jobs.Interrupted from exc
        if exc.code in STARTING:
            # Waited for long enough while the server started: no fault of the catalogue. The run
            # stops, the live catalogue stays, and the clock asks again at the next quarter of an hour.
            _fail(db, run, exc.code, state="stopped")
            logger.info("Catalogue run waits for the server kind=%s server=%s", kind, run.server_id)
            return
        # The code alone reaches the log: never an address, never the token.
        logger.warning("Catalogue run failed kind=%s step=%s error=%s server=%s", kind, run.step, exc.code,
                       run.server_id)
        _fail(db, run, exc.code)
        raise jobs.GiveUp(exc.code) from exc
    except Exception as exc:
        logger.exception("Catalogue run failed kind=%s step=%s server=%s", kind, run.step, run.server_id)
        _fail(db, run, "internal_error")
        raise jobs.GiveUp("internal_error") from exc
    else:
        logger.info("Catalogue run done kind=%s server=%s artists=%s albums=%s tracks=%s songs=%s changed=%s "
                    "removed=%s", kind, run.server_id, run.artists, run.albums, run.tracks, run.songs, run.changed,
                    run.removed)
        # The search index and the browser's catalogue follow the new state at once.
        try:
            catalog_view.reindex(db, run.server_id)
        except Exception:
            db.rollback()
            logger.exception("Search index could not be made")
        catalog_view.warm(run.server_id)
        # Entries whose track went with the run (a file renamed in Jellyfin) find it again.
        try:
            relink.after_run(db, run.server_id)
        except Exception:
            db.rollback()
            logger.exception("Entries could not be found again after the run server=%s", run.server_id)


def run_full(db: Session, job: Job) -> None:
    _run(db, job, "full", _full)


def run_partial(db: Session, job: Job) -> None:
    row = media.get(db, _server_of(db, job))
    if row is not None and row.active and row.section_key and row.generation == 0:
        # Nothing to bring up to date: the whole catalogue first.
        request_full(db, "first", row.id)
        return
    _run(db, job, "partial", _partial)


def settle(db: Session) -> None:
    """At the start of the lane and after a run given up: a run left ``running`` by a stopped server is over, and the
    rows of a generation that never went live go. The one before the live one stays for its readers."""
    db.execute(update(SyncRun).where(SyncRun.state == "running")
               .values(state="stopped", error="interrupted", finished_at=clock.now()))
    for row in media.every(db):
        _drop_unborn_generations(db, row.id, row.generation or 0)
    db.commit()


jobs.register(FULL, run_full, settle)
jobs.register(PART, run_partial, settle)
jobs.LANES[LANE] = KINDS


# --- The clock -------------------------------------------------------------------------------------------------------


def _slot(text: str, now: datetime, zone: tzinfo) -> datetime | None:
    """Today's moment of the nightly run, on the server's clock; None when it is switched off."""
    try:
        hours, minutes = (int(part) for part in str(text or "").split(":"))
    except ValueError:
        return None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        return None
    local = now.astimezone(zone)
    return local.replace(hour=hours, minute=minutes, second=0, microsecond=0)


_swept_at: datetime | None = None


def forget() -> None:
    global _swept_at
    _swept_at = None


def sweep(db: Session, now: datetime) -> None:
    """Runs and finished jobs older than ``KEEP_DAYS`` go, and of the rest only the newest ``KEEP_RUNS`` runs and
    ``jobs.KEEP_FINISHED`` jobs stay: a partial run every 15 minutes is some 35,000 of each a year. A run under way
    and a job that waits are never touched."""
    done = SyncRun.state != "running"
    db.execute(delete(SyncRun).where(done, SyncRun.started_at < now - timedelta(days=KEEP_DAYS)))
    oldest_kept = (select(SyncRun.id).where(done).order_by(SyncRun.id.desc())
                   .offset(KEEP_RUNS - 1).limit(1).scalar_subquery())
    db.execute(delete(SyncRun).where(done, SyncRun.id < oldest_kept))
    jobs.prune(db, now)
    db.commit()


def tick(db: Session, now: datetime | None = None) -> str | None:
    """What the clock asks for now, server by server: ``full`` (the first one, or the nightly one), ``partial``, or
    nothing; returns what it asked of the first server that needed something. Once an hour it clears away old runs and
    jobs, too."""
    global _swept_at
    moment = now or clock.now()
    if _swept_at is None or not timedelta(0) <= moment - _swept_at < SWEEP_EVERY:
        sweep(db, moment)
        _swept_at = moment
    asked: str | None = None
    for row in media.active(db):
        found = _tick_server(db, row, moment)
        asked = asked or found
    return asked


def _tick_server(db: Session, row: MediaServer, moment: datetime) -> str | None:
    if not row.section_key or _busy(db, row.id):
        return None
    if row.kind == SERVER_JELLYFIN and not row.user_id:
        # The user's play counts come with the catalogue: the first run waits until the operator chose the user.
        return None
    last = db.scalars(select(SyncRun).where(SyncRun.server_id == row.id)
                      .order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).limit(1)).first()
    recent = last is not None and moment - last.started_at < PARTIAL_EVERY
    if row.generation == 0:
        if recent:
            return None
        return "full" if request_full(db, "first", row.id) else None
    slot = _slot(str(settings_service.get(db, "catalog_full_at") or ""), moment, local_zone())
    if slot is not None and moment >= slot:
        last_full = db.scalars(select(SyncRun).where(SyncRun.kind == "full", SyncRun.server_id == row.id)
                               .order_by(SyncRun.started_at.desc()).limit(1)).first()
        if last_full is None or last_full.started_at < slot:
            return "full" if request_full(db, "nightly", row.id) else None
    if recent:
        return None
    return "partial" if request_partial(db, server_id=row.id) else None


def _tick_once() -> None:
    # The session is opened and closed in the thread that uses it. Opened around the wait instead, a stop cancelled
    # the wait and closed the session from the loop while the thread was still in it (an error in the log).
    with SessionLocal() as db:
        tick(db)


async def run_forever(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(_tick_once)
        except Exception:
            logger.exception("Catalogue clock failed")
        try:
            await asyncio.wait_for(stop.wait(), TICK_SECONDS)
        except TimeoutError:
            continue


# --- What the interface shows ----------------------------------------------------------------------------------------


def counts(db: Session, generation: int, server_id: int | None = None) -> dict[str, int]:
    if server_id is None:
        row = media.primary(db)
        server_id = row.id if row is not None else 0
    if generation == 0:
        return {"artists": 0, "albums": 0, "tracks": 0, "songs": 0, "moods": 0}

    def count(model: Any, *where: Any) -> int:
        return int(db.scalar(select(func.count()).select_from(model).where(
            model.server_id == server_id, model.generation == generation, *where)) or 0)

    return {
        "artists": count(CatalogArtist),
        "albums": count(CatalogAlbum),
        "tracks": count(CatalogTrack),
        "songs": count(CatalogTrack, CatalogTrack.shown.is_(True)),
        "moods": count(CatalogTag, CatalogTag.kind == "mood"),
    }


def analysis_of(row: MediaServer | None) -> dict[str, Any]:
    """``off``, ``running`` with the share analysed, ``on``, or ``unknown`` before the first sample."""
    if row is None or (row.analysis_sampled == 0 and row.analysis_enabled is None):
        return {"state": "unknown", "percent": None}
    if row.analysis_enabled is False:
        return {"state": "off", "percent": None}
    sampled, found = row.analysis_sampled, row.analysis_found
    if sampled and found >= sampled:
        return {"state": "on", "percent": 100}
    if row.analysis_enabled or found:
        return {"state": "running", "percent": int(found * 100 / sampled) if sampled else 0}
    return {"state": "off", "percent": None}


def _when(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _run_view(run: SyncRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {"kind": run.kind, "reason": run.reason, "state": run.state, "error": run.error or None,
            "step": run.step, "progress": run.progress, "started_at": _when(run.started_at),
            "finished_at": _when(run.finished_at), "artists": run.artists, "albums": run.albums, "tracks": run.tracks,
            "songs": run.songs, "changed": run.changed, "removed": run.removed}


def status(db: Session, server_id: int | None = None) -> dict[str, Any]:
    """How the catalogue of a server stands (None: the first one connected)."""
    # Several queries: one state of the database for all of them.
    hold(db)
    row = media.get(db, server_id) if server_id is not None else media.primary(db)
    if row is not None and not row.active:
        row = None
    ident = row.id if row is not None else -1
    running = db.scalars(select(SyncRun).where(SyncRun.state == "running", SyncRun.server_id == ident)
                         .order_by(SyncRun.id.desc()).limit(1)).first()
    last = db.scalars(select(SyncRun).where(SyncRun.state != "running", SyncRun.server_id == ident)
                      .order_by(SyncRun.id.desc()).limit(1)).first()
    queued = db.scalar(select(Job.kind).where(Job.kind.in_(KINDS), Job.ref == ident, Job.state == "queued")
                       .order_by(Job.id).limit(1))
    if queued is None and running is None:
        # A job the lane has taken whose run has not begun yet still waits: a page that stopped asking in that moment
        # would wait for ever. (One whose run is done and only prepares the search is done.)
        claimed = db.scalars(select(Job).where(Job.kind.in_(KINDS), Job.ref == ident, Job.state == "running")
                             .order_by(Job.id).limit(1)).first()
        if claimed is not None and claimed.started_at is not None and db.scalar(
                select(SyncRun.id).where(SyncRun.server_id == ident, SyncRun.started_at >= claimed.started_at)
                .limit(1)) is None:
            queued = claimed.kind
    return {
        "server": row.id if row is not None else None,
        "connected": row is not None,
        "library": bool(row and row.section_key),
        "library_title": row.section_title if row else "",
        "ready": bool(row and row.generation),
        "counts": counts(db, row.generation if row else 0, ident),
        "synced_at": _when(row.synced_at) if row else None,
        "full_at": _when(row.full_at) if row else None,
        "running": _run_view(running),
        "queued": {"catalog_full": "full", "catalog_part": "partial"}.get(queued or ""),
        "last": _run_view(last),
        "analysis": analysis_of(row),
        "nightly": str(settings_service.get(db, "catalog_full_at") or ""),
    }
