"""The catalogue: the music library of Plex, kept in nexmix so that searching and suggesting never wait for Plex.

**Full run** (``catalog_full``): the lists of artists, albums and tracks in pages of 5,000 (the genres come with the
lists), moods and styles of albums and artists by asking for many at once (``/library/metadata/<k1,k2,...>``), the
moods of the tracks through the library's mood filter (one list per mood), a sample of tracks for the sonic analysis,
then the songs (``services/songs.py``). Everything is written into a new *generation*; the last step makes it the live
one in a single statement. A run that breaks off leaves the old catalogue as it was.

**Readers** never see half a catalogue: a way of reading that takes more than one query begins with ``pin``, which
holds one state of the database for the rest of its session and says which generation is live in it. The generation
before stays on disk until the next full run begins, for whoever is still reading it.

**Partial run** (``catalog_part``), every 15 minutes: per type what Plex changed or added since the last run began
(``updatedAt>>=``/``addedAt>>=``, 60 seconds back for what Plex writes a little late), always on Plex's clock: the
``Date`` of its answers, never ours. A time Plex gives an entry is never trusted beyond that clock, so one entry from
the future does not make the runs after it blind. When more than 5,000 entries are new since then (Plex sometimes
touches tens of thousands at once), a full run instead; what was already there in the minute looked back does not
count, and a new entry counts once. What is gone shows in the totals; when they differ, the light list of keys says
which. All of it is written in one transaction, and only the songs it touched are worked out again.

**Nightly**, at ``catalog_full_at`` on the server's clock, a full run; it sets the marks anew.

Both kinds run in the lane ``catalog`` (``services/jobs.py``): one at a time, never two, and a stopped server takes
up again where the job was waiting. Letting go of Plex (``plex_link.drop_catalog``) stops a run under way at its next
request to Plex. Runs and finished jobs older than 30 days go, and never more than 1,000 of each stay.
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
    AlbumTag,
    ArtistTag,
    Base,
    CatalogAlbum,
    CatalogArtist,
    CatalogTag,
    CatalogTrack,
    Job,
    PlexConnection,
    SyncRun,
    TrackTag,
)
from . import catalog_view, jobs, plex_link, settings_service, songs
from .plex import ALBUM, ARTIST, BULK, THUMB, TRACK, PlexError, PlexServer, tags_of

logger = logging.getLogger("nexmix.catalog")

LANE = "catalog"
FULL = "catalog_full"
PART = "catalog_part"
KINDS = (FULL, PART)
PARTIAL_EVERY = timedelta(minutes=15)
#: How far back a partial run looks beyond where the last run began.
OVERLAP = 60
#: More changes than this in a partial run: a full run instead.
PARTIAL_LIMIT = 5000
#: The key of ``marks`` that holds Plex's clock when the last run began.
CLOCK = "clock"
#: Runs and finished jobs: none older than this, never more than this many.
KEEP_DAYS = 30
KEEP_RUNS = 1000
SWEEP_EVERY = timedelta(hours=1)
SAMPLE = 200
ANALYSIS_EVERY = timedelta(hours=1)
#: How long the loop waits between two looks at the clock.
TICK_SECONDS = 60
TYPES = (ARTIST, ALBUM, TRACK)

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


def _watch(started: int) -> Callable[[], bool]:
    """What a run asks before each request to Plex: stop for good when the catalogue was let go of meanwhile
    (``Stale``), stop for now when the server is stopping (True)."""

    def stop() -> bool:
        if plex_link.epoch() != started:
            raise Stale
        return jobs.stopping.is_set()

    return stop


# --- Reading -------------------------------------------------------------------------------------------------------


def pin(db: Session) -> int:
    """One state of the database for the rest of the session's transaction (SQLite holds it for a reader in WAL mode),
    and the generation that is live in it. Whoever reads the catalogue in more than one query starts here: a full run
    that switches over meanwhile, or a partial run that writes, changes nothing of what this session sees. The
    transaction ends with the session (or ``rollback``); it only reads, so it keeps nobody waiting."""
    connection = db.connection()
    raw = connection.connection.dbapi_connection
    if raw is not None and not raw.in_transaction:
        connection.exec_driver_sql("BEGIN")
    return int(db.scalar(select(PlexConnection.generation).order_by(PlexConnection.id).limit(1)) or 0)


# --- Asking for runs -----------------------------------------------------------------------------------------------


def _busy(db: Session, kinds: Iterable[str]) -> bool:
    found = db.scalar(select(Job.id).where(Job.kind.in_(tuple(kinds)), Job.state.in_(("queued", "running"))).limit(1))
    return found is not None


def request_full(db: Session, reason: str) -> bool:
    """A full run, unless one is waiting or running already (a double click starts one). False: nothing new. A run
    that is done with Plex and only prepares the search and the packed catalogue (a second or two, and the status says
    "done" already) does not count: asked then, the next run waits behind it."""
    row = plex_link.current(db)
    if row is None or not row.section_key:
        return False
    waiting = db.scalar(select(Job.id).where(Job.kind == FULL, Job.state == "queued").limit(1))
    running = db.scalar(select(SyncRun.id).where(SyncRun.kind == "full", SyncRun.state == "running").limit(1))
    if waiting is not None or running is not None:
        return False
    return _enqueue(db, FULL, {"serial": row.serial, "reason": reason})


def request_partial(db: Session, reason: str = "schedule") -> bool:
    row = plex_link.current(db)
    if row is None or not row.section_key or row.generation == 0:
        return False
    if _busy(db, KINDS):
        return False
    return _enqueue(db, PART, {"serial": row.serial, "reason": reason})


def _enqueue(db: Session, kind: str, payload: dict[str, Any]) -> bool:
    """The job, unless one of the kind waits already (the unique index decides between two asking at once). True:
    this call put it there."""
    made = db.execute(sqlite_insert(Job).values(kind=kind, ref=0, payload=payload, state="queued",
                                                created_at=clock.now()).on_conflict_do_nothing())
    db.commit()
    return bool(made.rowcount)


# --- Values out of Plex's entries --------------------------------------------------------------------------------


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _key(entry: dict[str, Any]) -> int | None:
    key = _int(entry.get("ratingKey"))
    return key if key is not None and 0 < key < 10**12 else None


def _thumb(value: Any) -> str:
    text = str(value or "")
    return text if THUMB.match(text) else ""


def _text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _moment(entry: dict[str, Any]) -> int:
    """The last time Plex touched the entry; ``addedAt`` where it has no ``updatedAt``."""
    return max(_int(entry.get("updatedAt")) or 0, _int(entry.get("addedAt")) or 0)


def artist_row(generation: int, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "generation": generation, "id": _key(entry), "title": _text(entry.get("title")),
        "thumb": _thumb(entry.get("thumb")), "added_at": _int(entry.get("addedAt")) or 0,
        "updated_at": _int(entry.get("updatedAt")) or 0,
    }


def album_row(generation: int, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "generation": generation, "id": _key(entry), "artist_id": _int(entry.get("parentRatingKey")) or 0,
        "title": _text(entry.get("title")), "year": _int(entry.get("year")) or None,
        "thumb": _thumb(entry.get("thumb")), "added_at": _int(entry.get("addedAt")) or 0,
        "updated_at": _int(entry.get("updatedAt")) or 0,
    }


def track_row(generation: int, entry: dict[str, Any]) -> dict[str, Any]:
    album_artist = _text(entry.get("grandparentTitle"))
    own = _text(entry.get("originalTitle"))
    return {
        "generation": generation, "id": _key(entry), "album_id": _int(entry.get("parentRatingKey")) or 0,
        "artist_id": _int(entry.get("grandparentRatingKey")) or 0, "title": _text(entry.get("title")),
        # Kept only where it says something the album's artist does not.
        "artist": own if own and own != album_artist else "",
        "duration": max(0, _int(entry.get("duration")) or 0),
        "rating_count": max(0, _int(entry.get("ratingCount")) or 0),
        "view_count": max(0, _int(entry.get("viewCount")) or 0), "year": _int(entry.get("parentYear")) or None,
        "disc": _int(entry.get("parentIndex")), "number": _int(entry.get("index")),
        "added_at": _int(entry.get("addedAt")) or 0, "updated_at": _int(entry.get("updatedAt")) or 0,
        "song_key": "", "variant": False, "shown": True, "original_year": None,
        "guid": _text(entry.get("guid"))[:200],
    }


_MAKERS: dict[int, Callable[[int, dict[str, Any]], dict[str, Any]]] = {
    ARTIST: artist_row, ALBUM: album_row, TRACK: track_row,
}
_PLEX_TYPE = {ARTIST: "artist", ALBUM: "album", TRACK: "track"}


# --- Tags ------------------------------------------------------------------------------------------------------------


class Tags:
    """The tags of one generation: their ids by kind and name, and the links found, written in bulk."""

    def __init__(self, db: Session, generation: int) -> None:
        self.generation = generation
        self.ids: dict[tuple[str, str], int] = {}
        self.new: list[dict[str, Any]] = []
        self.links: dict[int, set[tuple[int, int]]] = {ARTIST: set(), ALBUM: set(), TRACK: set()}
        for tag in db.scalars(select(CatalogTag).where(CatalogTag.generation == generation)):
            self.ids[(tag.kind, tag.name)] = tag.id
        self.next = max(self.ids.values(), default=0) + 1

    def id_of(self, kind: str, name: str) -> int:
        key = (kind, name)
        found = self.ids.get(key)
        if found is None:
            found = self.ids[key] = self.next
            self.next += 1
            self.new.append({"generation": self.generation, "id": found, "kind": kind, "name": name})
        return found

    def take(self, of_type: int, item: int, entry: dict[str, Any], kinds: Iterable[str]) -> None:
        for kind in kinds:
            for name in tags_of(entry, kind.capitalize()):
                self.links[of_type].add((item, self.id_of(kind, name)))

    def link(self, of_type: int, item: int, kind: str, name: str) -> None:
        self.links[of_type].add((item, self.id_of(kind, name)))

    def write(self, db: Session) -> None:
        if self.new:
            db.execute(insert(CatalogTag), self.new)
            self.new = []
        for of_type, pairs in self.links.items():
            if pairs:
                rows = [{"generation": self.generation, "item_id": item, "tag_id": tag} for item, tag in pairs]
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
        """Between two pieces of work that ask Plex nothing: the same question as before each request."""
        if self.stop():
            raise PlexError("stopped", 503)


# --- The full run ----------------------------------------------------------------------------------------------------


def _drop_generation(db: Session, generation: int) -> None:
    for model in _TABLES:
        table = _table(model)
        db.execute(delete(table).where(table.c.generation == generation))


def _drop_other_generations(db: Session, keep: int) -> None:
    for model in _TABLES:
        table = _table(model)
        db.execute(delete(table).where(table.c.generation != keep))


def _drop_unborn_generations(db: Session, live: int) -> None:
    """The rows of runs that never went live (a run that died with the server). The generation before the live one
    stays: somebody may still be reading it (``pin``)."""
    for model in _TABLES:
        table = _table(model)
        db.execute(delete(table).where(table.c.generation > live))


def _explain(server: PlexServer, section: str, exc: PlexError) -> PlexError:
    """Plex knew a path of the library no longer: when the library itself is gone from the server, say that."""
    if exc.code != "plex_not_found":
        return exc
    try:
        sections = server.music_sections()
    except PlexError:
        return exc
    if all(one.key != section for one in sections):
        return PlexError("library_gone", 409)
    return exc


def _cap(marks: dict[str, int], now: int | None) -> dict[str, int]:
    """Plex's times are never trusted beyond Plex's own clock: an entry dated ten years ahead must not move a mark."""
    if now is None:
        return marks
    return {key: min(int(value), now) for key, value in marks.items()}


def _list(db: Session, server: PlexServer, section: str, of_type: int, generation: int, tags: Tags,
          progress: Progress, marks: dict[str, int]) -> dict[int, dict[str, Any]]:
    """One type, page by page, into the generation; returns what the songs need of each entry."""
    found: dict[int, dict[str, Any]] = {}
    model = _ROWS[of_type]
    taken = 0
    for entries, total in server.pages(section, of_type):
        rows = []
        for entry in entries:
            if entry.get("type") not in (None, _PLEX_TYPE[of_type]):
                continue
            row = _MAKERS[of_type](generation, entry)
            if row["id"] is None or row["id"] in found:
                continue
            rows.append(row)
            found[row["id"]] = row
            marks[str(of_type)] = max(marks.get(str(of_type), 0), _moment(entry))
            if of_type in (ALBUM, TRACK):
                tags.take(of_type, row["id"], entry, ("genre",))
        if rows:
            db.execute(insert(_table(model)), rows)
        taken += len(entries)
        progress.share(taken / total if total else 1.0)
        progress.check()
    db.commit()
    return found


def _bulk_tags(db: Session, server: PlexServer, of_type: int, keys: list[int], tags: Tags, progress: Progress,
               kinds: tuple[str, ...]) -> None:
    for start in range(0, len(keys), BULK):
        for entry in server.bulk(keys[start:start + BULK]):
            key = _key(entry)
            if key is not None:
                tags.take(of_type, key, entry, kinds)
        progress.share((start + BULK) / len(keys) if keys else 1.0)
        progress.check()


def _track_moods(server: PlexServer, section: str, tracks: dict[int, dict[str, Any]], tags: Tags,
                 progress: Progress) -> None:
    values = server.tag_values(section, "mood", TRACK)
    for number, (key, name) in enumerate(values, 1):
        for item in server.keys(section, TRACK, {"mood": key}):
            if item in tracks:
                tags.link(TRACK, item, "mood", name)
        progress.share(number / len(values))
        progress.check()


def measure_analysis(server: PlexServer, section: str, track_ids: list[int]) -> dict[str, Any]:
    """The share of tracks Plex analysed, from an evenly spread sample: there is no filter for it."""
    ordered = sorted(track_ids)
    step = max(1, len(ordered) // SAMPLE)
    sample = ordered[::step][:SAMPLE]
    found = 0
    for start in range(0, len(sample), BULK):
        found += sum(1 for entry in server.bulk(sample[start:start + BULK]) if entry.get("musicAnalysisVersion"))
    return {"analysis_enabled": server.music_analysis_enabled(section), "analysis_sampled": len(sample),
            "analysis_found": found, "analysis_at": clock.now()}


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


def _tracks_where(db: Session, generation: int, column: Any, values: Iterable[Any]) -> list[Any]:
    found: list[Any] = []
    for part in _chunks(values):
        found.extend(db.execute(select(*_TRACK_FIELDS).where(CatalogTrack.generation == generation,
                                                             column.in_(part))).all())
    return found


class _Names:
    """Titles and artists of the albums and the names of the artists a set of tracks needs, read once."""

    def __init__(self, db: Session, generation: int) -> None:
        self.db = db
        self.generation = generation
        self.albums: dict[int, tuple[str, int]] = {}
        self.artists: dict[int, str] = {}

    def load(self, rows: list[Any]) -> None:
        wanted = {row.album_id for row in rows} - set(self.albums)
        for part in _chunks(wanted):
            statement = select(CatalogAlbum.id, CatalogAlbum.title, CatalogAlbum.artist_id).where(
                CatalogAlbum.generation == self.generation, CatalogAlbum.id.in_(part))
            for album in _rows(self.db, statement):
                self.albums[album.id] = (album.title, album.artist_id)
        people = ({row.artist_id for row in rows} | {album[1] for album in self.albums.values()}) - set(self.artists)
        for part in _chunks(people):
            statement = select(CatalogArtist.id, CatalogArtist.title).where(
                CatalogArtist.generation == self.generation, CatalogArtist.id.in_(part))
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
                  keys: Iterable[str] = ()) -> None:
    """Song key, side version, version shown and original year; only what changed is written.

    ``tracks`` None: every track of the generation (a full run). Otherwise the songs of these tracks only, as they
    are now and as they were before (``keys``, their song keys of before): the songs they joined and the songs they
    left, each whole, and nothing else."""
    names = _Names(db, generation)
    if tracks is None:
        rows = _rows(db, select(*_TRACK_FIELDS).where(CatalogTrack.generation == generation))
        names.load(rows)
    else:
        rows = _tracks_where(db, generation, CatalogTrack.id, set(tracks))
        names.load(rows)
        mine = {row.id for row in rows}
        wanted = set(keys)
        for row in rows:
            version = names.version(row)
            wanted.add(songs.song_key(version.id, version.title, version.album_artist, version.track_artist))
        # Keys of a single track (``#<id>``) belong to nobody else.
        others = [row for row in _tracks_where(db, generation, CatalogTrack.song_key,
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
            changes.append({"generation": generation, "id": row.id, "song_key": song.key, "variant": song.side,
                            "shown": song.shown, "original_year": song.original_year})
    for start in range(0, len(changes), 2000):
        db.execute(update(CatalogTrack), changes[start:start + 2000])


def count_songs(db: Session, generation: int) -> int:
    return int(db.scalar(select(func.count()).select_from(CatalogTrack)
                         .where(CatalogTrack.generation == generation, CatalogTrack.shown.is_(True))) or 0)


def _full(db: Session, row: PlexConnection, run: SyncRun, stop: Callable[[], bool]) -> None:
    generation = run.generation
    section = row.section_key
    progress = Progress(db, run, stop)
    marks: dict[str, int] = {}
    # Room for the new generation: the one before the live one goes now, whoever read it has long finished.
    _drop_other_generations(db, row.generation)
    db.commit()
    tags = Tags(db, generation)
    with plex_link.server_of(db, row, stop=stop) as server:
        try:
            progress.step("artists")
            artists = _list(db, server, section, ARTIST, generation, tags, progress, marks)
            progress.step("albums")
            albums = _list(db, server, section, ALBUM, generation, tags, progress, marks)
            progress.step("tracks")
            tracks = _list(db, server, section, TRACK, generation, tags, progress, marks)
            progress.step("album_tags")
            _bulk_tags(db, server, ALBUM, list(albums), tags, progress, ("mood", "style", "genre"))
            progress.step("artist_tags")
            _bulk_tags(db, server, ARTIST, list(artists), tags, progress, ("mood", "style", "genre"))
            progress.step("track_moods")
            _track_moods(server, section, tracks, tags, progress)
            progress.step("analysis")
            analysis = measure_analysis(server, section, list(tracks)) if tracks else {}
        except PlexError as exc:
            raise _explain(server, section, exc) from exc
        marks = _cap(marks, server.clock)
        # Whatever Plex had written when the run began was there to be fetched: the next partial run looks from here.
        if server.began is not None:
            marks[CLOCK] = server.began
    tags.write(db)
    db.commit()
    progress.step("songs")
    compute_songs(db, generation)
    run.songs = count_songs(db, generation)
    db.commit()
    progress.step("switch")
    run.artists, run.albums, run.tracks = len(artists), len(albums), len(tracks)
    now = clock.now()
    switched = db.execute(
        update(PlexConnection).where(PlexConnection.id == row.id, PlexConnection.serial == run.serial)
        .values(generation=generation, marks=marks, synced_at=now, full_at=now, **analysis)
    ).rowcount
    if switched != 1:
        db.rollback()
        raise Stale
    progress.done()
    run.state, run.finished_at = "done", now
    db.commit()
    # The generation before stays until the next full run begins: a reader may be in the middle of it (``pin``).


# --- The partial run -------------------------------------------------------------------------------------------------


Entries = dict[int, dict[int, dict[str, Any]]]


def _top(marks: dict[str, int], of_type: int) -> int:
    """Where a partial run looks from: Plex's clock when the last run began (whatever Plex wrote up to a minute before
    was there to be fetched then), else the highest time seen of the type."""
    return int(marks.get(CLOCK) or 0) or int(marks.get(str(of_type)) or 0)


def _count(server: PlexServer, section: str, of_type: int, since: int) -> int:
    """How many entries of a type Plex changed or added after ``since``. A new entry carries both times: of the two
    counts the larger stands for both, so it counts once."""
    return max(server.count(section, of_type, {"updatedAt>>": since}),
               server.count(section, of_type, {"addedAt>>": since}))


def _changed(server: PlexServer, section: str, marks: dict[str, int]) -> tuple[int, Entries]:
    """How many entries Plex changed or added per type since the last run; the entries themselves (with the minute
    before) only when the count stays under the limit. The minute looked back never counts against the limit: a burst
    in it was taken in by the run before."""
    tops: dict[int, int] = {}
    fresh = 0
    for of_type in TYPES:
        top = _top(marks, of_type)
        count = _count(server, section, of_type, top)
        if server.clock is not None and top > server.clock:
            # The answer told Plex's clock, and the mark lies beyond it (Plex's clock went back, or a mark of an older
            # nexmix came from an entry dated ahead): from the clock, never from a time to come.
            top = server.clock
            count = _count(server, section, of_type, top)
        tops[of_type] = top
        fresh += count
    found: Entries = {of_type: {} for of_type in TYPES}
    if fresh > PARTIAL_LIMIT:
        return fresh, found
    since = {of_type: max(0, top - OVERLAP) for of_type, top in tops.items()}
    if sum(_count(server, section, of_type, since[of_type]) for of_type in TYPES) > PARTIAL_LIMIT:
        # A burst in the minute looked back: the full run after it has it already, it is not fetched again.
        since = tops
    for of_type in TYPES:
        for one in ({"updatedAt>>": since[of_type]}, {"addedAt>>": since[of_type]}):
            for entries, _total in server.pages(section, of_type, one):
                for entry in entries:
                    key = _key(entry)
                    if key is not None and entry.get("type") in (None, _PLEX_TYPE[of_type]):
                        found[of_type][key] = entry
    return fresh, found


def _replace(db: Session, generation: int, of_type: int, entries: dict[int, dict[str, Any]], tags: Tags) -> None:
    """Entries of one type taken in afresh, with all their tags."""
    if not entries:
        return
    model, link = _table(_ROWS[of_type]), _table(_LINKS[of_type])
    keys = list(entries)
    for start in range(0, len(keys), 500):
        part = keys[start:start + 500]
        db.execute(delete(model).where(model.c.generation == generation, model.c.id.in_(part)))
        db.execute(delete(link).where(link.c.generation == generation, link.c.item_id.in_(part)))
    db.execute(insert(model), [_MAKERS[of_type](generation, entry) for entry in entries.values()])
    for key, entry in entries.items():
        tags.take(of_type, key, entry, ("mood", "style", "genre"))


def _remove(db: Session, generation: int, of_type: int, keys: set[int]) -> None:
    model, link = _table(_ROWS[of_type]), _table(_LINKS[of_type])
    chosen = list(keys)
    for start in range(0, len(chosen), 500):
        part = chosen[start:start + 500]
        db.execute(delete(model).where(model.c.generation == generation, model.c.id.in_(part)))
        db.execute(delete(link).where(link.c.generation == generation, link.c.item_id.in_(part)))


def _ours(db: Session, generation: int, of_type: int) -> set[int]:
    model = _table(_ROWS[of_type])
    return set(db.scalars(select(model.c.id).where(model.c.generation == generation)))


def _touched(db: Session, generation: int, changed: Entries, gone: dict[int, set[int]]) -> dict[int, str]:
    """The tracks whose song may change with what a partial run takes in, with their song keys as they stand: the
    tracks themselves, the tracks of changed or gone albums, and of changed or gone artists."""
    tracks = set(changed[TRACK]) | gone.get(TRACK, set())
    albums = set(changed[ALBUM]) | gone.get(ALBUM, set())
    artists = set(changed[ARTIST]) | gone.get(ARTIST, set())
    for part in _chunks(artists):
        albums.update(db.scalars(select(CatalogAlbum.id).where(CatalogAlbum.generation == generation,
                                                               CatalogAlbum.artist_id.in_(part))))
    found: dict[int, str] = {}
    for column, values in ((CatalogTrack.id, tracks), (CatalogTrack.album_id, albums),
                           (CatalogTrack.artist_id, artists)):
        for row in _tracks_where(db, generation, column, values):
            found[row.id] = row.song_key
    return found


def _partial(db: Session, row: PlexConnection, run: SyncRun, stop: Callable[[], bool]) -> None:
    generation = row.generation
    section = row.section_key
    marks = {str(key): int(value) for key, value in dict(row.marks or {}).items()}
    progress = Progress(db, run, stop)
    tags = Tags(db, generation)
    removed = 0
    with plex_link.server_of(db, row, stop=stop) as server:
        try:
            progress.step("changes")
            fresh, changed = _changed(server, section, marks)
            if fresh > PARTIAL_LIMIT:
                raise TooMany
            gone, analysis = _rest_of_partial(db, server, row, changed, progress)
        except PlexError as exc:
            raise _explain(server, section, exc) from exc
        now_plex, begun = server.clock, server.began
    progress.step("write")
    if any(changed.values()) or any(gone.values()):
        before = _touched(db, generation, changed, gone)
        for of_type in TYPES:
            _replace(db, generation, of_type, changed[of_type], tags)
            for entry in changed[of_type].values():
                marks[str(of_type)] = max(marks.get(str(of_type), 0), _moment(entry))
        for of_type, keys in gone.items():
            _remove(db, generation, of_type, keys)
            removed += len(keys)
        # The year of a track is its album's: a changed album changes it on its tracks at once.
        for album in changed[ALBUM].values():
            year = _int(album.get("year")) or None
            db.execute(update(CatalogTrack).where(CatalogTrack.generation == generation,
                                                  CatalogTrack.album_id == _key(album),
                                                  CatalogTrack.year.is_not(year)).values(year=year))
        tags.write(db)
        after = _touched(db, generation, changed, gone)
        compute_songs(db, generation, tracks=set(before) | set(after), keys=set(before.values()))
    marks = _cap(marks, now_plex)
    if begun is not None:
        marks[CLOCK] = begun
    else:
        marks.pop(CLOCK, None)
    now = clock.now()
    switched = db.execute(
        update(PlexConnection).where(PlexConnection.id == row.id, PlexConnection.serial == run.serial,
                                     PlexConnection.generation == generation)
        .values(marks=marks, synced_at=now, **analysis)
    ).rowcount
    if switched != 1:
        db.rollback()
        raise Stale
    run.changed = sum(len(entries) for entries in changed.values())
    run.removed = removed
    progress.done()
    run.state, run.finished_at = "done", now
    db.commit()
    # The numbers of the run are read after the write: counting holds no lock that a writer would wait for.
    numbers = counts(db, generation)
    run.artists, run.albums, run.tracks, run.songs = (numbers["artists"], numbers["albums"], numbers["tracks"],
                                                      numbers["songs"])
    db.commit()


def _rest_of_partial(db: Session, server: PlexServer, row: PlexConnection, changed: Entries,
                     progress: Progress) -> tuple[dict[int, set[int]], dict[str, Any]]:
    """The tags of what changed (from the request that brings many at once), what is gone from Plex, and the sonic
    analysis when it is due again. Returns what is gone per type and the sample of the analysis."""
    generation = row.generation
    section = row.section_key
    for of_type in TYPES:
        keys = list(changed[of_type])
        for start in range(0, len(keys), BULK):
            for entry in server.bulk(keys[start:start + BULK]):
                key = _key(entry)
                if key is not None and key in changed[of_type]:
                    changed[of_type][key] = {**changed[of_type][key], **_tag_part(entry)}
    progress.step("removed")
    gone: dict[int, set[int]] = {}
    missing: dict[int, set[int]] = {}
    for of_type in TYPES:
        ours = _ours(db, generation, of_type) | set(changed[of_type])
        if server.count(section, of_type) == len(ours):
            continue
        theirs = server.keys(section, of_type)
        gone[of_type] = ours - theirs
        missing[of_type] = theirs - ours
    for of_type, keys in missing.items():
        chosen = sorted(keys)
        for start in range(0, len(chosen), BULK):
            for entry in server.bulk(chosen[start:start + BULK]):
                key = _key(entry)
                if key is not None:
                    changed[of_type][key] = entry
    analysis: dict[str, Any] = {}
    if row.analysis_at is None or clock.now() - row.analysis_at >= ANALYSIS_EVERY:
        progress.step("analysis")
        ids = list(_ours(db, generation, TRACK) | set(changed[TRACK]))
        analysis = measure_analysis(server, section, ids) if ids else {}
    return gone, analysis


def _tag_part(entry: dict[str, Any]) -> dict[str, Any]:
    return {name: entry[name] for name in ("Mood", "Style", "Genre") if name in entry}


class TooMany(Exception):
    """More changed than a partial run takes."""


# --- The jobs --------------------------------------------------------------------------------------------------------


def _start(db: Session, job: Job, kind: str) -> tuple[PlexConnection, SyncRun, Callable[[], bool]] | None:
    # Read before the connection: a catalogue let go of in between stops the run at once.
    stop = _watch(plex_link.epoch())
    row = plex_link.current(db)
    payload = job.payload if isinstance(job.payload, dict) else {}
    if row is None or not row.section_key:
        return None
    if payload.get("serial") not in (None, row.serial):
        return None
    run = SyncRun(kind=kind, reason=str(payload.get("reason") or "")[:16], state="running", serial=row.serial,
                  generation=row.generation + 1 if kind == "full" else row.generation, started_at=clock.now())
    db.add(run)
    db.commit()
    return row, run, stop


def _fail(db: Session, run: SyncRun, code: str, *, state: str = "failed") -> None:
    """The run ends without its catalogue: for a full one, the generation it wrote goes; the live one stays."""
    db.rollback()
    if run.kind == "full":
        live = db.scalar(select(PlexConnection.generation).limit(1))
        if live != run.generation:
            _drop_generation(db, run.generation)
    db.execute(update(SyncRun).where(SyncRun.id == run.id)
               .values(state=state, error=code[:40], finished_at=clock.now()))
    db.commit()


def _run(db: Session, job: Job, kind: str,
         work: Callable[[Session, PlexConnection, SyncRun, Callable[[], bool]], None]) -> None:
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
        request_full(db, "many")
    except Stale:
        # Let go of meanwhile: Plex disconnected, or another server or library chosen.
        db.rollback()
        _fail(db, run, "disconnected" if plex_link.current(db) is None else "replaced", state="stopped")
        logger.info("Catalogue run stopped kind=%s step=%s", kind, run.step)
    except plex_link.Unreadable as exc:
        _fail(db, run, "token_unreadable")
        raise jobs.GiveUp("token_unreadable") from exc
    except PlexError as exc:
        if exc.code == "stopped":
            _fail(db, run, "stopped", state="stopped")
            raise jobs.Interrupted from exc
        # The code alone reaches the log: never an address, never the token.
        logger.warning("Catalogue run failed kind=%s step=%s error=%s", kind, run.step, exc.code)
        _fail(db, run, exc.code)
        raise jobs.GiveUp(exc.code) from exc
    except Exception as exc:
        logger.exception("Catalogue run failed kind=%s step=%s", kind, run.step)
        _fail(db, run, "internal_error")
        raise jobs.GiveUp("internal_error") from exc
    else:
        logger.info("Catalogue run done kind=%s artists=%s albums=%s tracks=%s songs=%s changed=%s removed=%s",
                    kind, run.artists, run.albums, run.tracks, run.songs, run.changed, run.removed)
        # The search index and the browser's catalogue follow the new state at once.
        try:
            catalog_view.reindex(db)
        except Exception:
            db.rollback()
            logger.exception("Search index could not be made")
        catalog_view.warm()


def run_full(db: Session, job: Job) -> None:
    _run(db, job, "full", _full)


def run_partial(db: Session, job: Job) -> None:
    row = plex_link.current(db)
    if row is not None and row.section_key and row.generation == 0:
        # Nothing to bring up to date: the whole catalogue first.
        request_full(db, "first")
        return
    _run(db, job, "partial", _partial)


def settle(db: Session) -> None:
    """At the start of the lane and after a run given up: a run left ``running`` by a stopped server is over, and the
    rows of a generation that never went live go. The one before the live one stays for its readers."""
    db.execute(update(SyncRun).where(SyncRun.state == "running")
               .values(state="stopped", error="interrupted", finished_at=clock.now()))
    live = db.scalar(select(PlexConnection.generation).limit(1))
    _drop_unborn_generations(db, live or 0)
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
    """What the clock asks for now: ``full`` (the first one, or the nightly one), ``partial``, or nothing. Once an
    hour it clears away old runs and jobs, too."""
    global _swept_at
    moment = now or clock.now()
    if _swept_at is None or not timedelta(0) <= moment - _swept_at < SWEEP_EVERY:
        sweep(db, moment)
        _swept_at = moment
    row = plex_link.current(db)
    if row is None or not row.section_key or _busy(db, KINDS):
        return None
    last = db.scalars(select(SyncRun).order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).limit(1)).first()
    recent = last is not None and moment - last.started_at < PARTIAL_EVERY
    if row.generation == 0:
        if recent:
            return None
        return "full" if request_full(db, "first") else None
    slot = _slot(str(settings_service.get(db, "catalog_full_at") or ""), moment, local_zone())
    if slot is not None and moment >= slot:
        last_full = db.scalars(select(SyncRun).where(SyncRun.kind == "full")
                               .order_by(SyncRun.started_at.desc()).limit(1)).first()
        if last_full is None or last_full.started_at < slot:
            return "full" if request_full(db, "nightly") else None
    if recent:
        return None
    return "partial" if request_partial(db) else None


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


def counts(db: Session, generation: int) -> dict[str, int]:
    if generation == 0:
        return {"artists": 0, "albums": 0, "tracks": 0, "songs": 0, "moods": 0}

    def count(statement: Any) -> int:
        return int(db.scalar(statement) or 0)

    return {
        "artists": count(select(func.count()).select_from(CatalogArtist).where(CatalogArtist.generation == generation)),
        "albums": count(select(func.count()).select_from(CatalogAlbum).where(CatalogAlbum.generation == generation)),
        "tracks": count(select(func.count()).select_from(CatalogTrack).where(CatalogTrack.generation == generation)),
        "songs": count(select(func.count()).select_from(CatalogTrack)
                       .where(CatalogTrack.generation == generation, CatalogTrack.shown.is_(True))),
        "moods": count(select(func.count()).select_from(CatalogTag)
                       .where(CatalogTag.generation == generation, CatalogTag.kind == "mood")),
    }


def analysis_of(row: PlexConnection | None) -> dict[str, Any]:
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


def status(db: Session) -> dict[str, Any]:
    # Several queries: one state of the database for all of them.
    pin(db)
    row = plex_link.current(db)
    running = db.scalars(select(SyncRun).where(SyncRun.state == "running").order_by(SyncRun.id.desc()).limit(1)).first()
    last = db.scalars(select(SyncRun).where(SyncRun.state != "running").order_by(SyncRun.id.desc()).limit(1)).first()
    queued = db.scalar(select(Job.kind).where(Job.kind.in_(KINDS), Job.state == "queued").order_by(Job.id).limit(1))
    return {
        "connected": row is not None,
        "library": bool(row and row.section_key),
        "library_title": row.section_title if row else "",
        "ready": bool(row and row.generation),
        "counts": counts(db, row.generation if row else 0),
        "synced_at": _when(row.synced_at) if row else None,
        "full_at": _when(row.full_at) if row else None,
        "running": _run_view(running),
        "queued": {"catalog_full": "full", "catalog_part": "partial"}.get(queued or ""),
        "last": _run_view(last),
        "analysis": analysis_of(row),
        "nightly": str(settings_service.get(db, "catalog_full_at") or ""),
    }
