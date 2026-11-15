"""The connector to a Jellyfin server: its catalogue, its sound and its pictures (``services/jellyfin.py`` is the HTTP
client beneath it). Playlists in Jellyfin come later; until then a playlist with a Jellyfin source lives in nexmix only
(``CAPABILITIES.playlists``).

**The catalogue.** The albums of the library, then its tracks, in pages of 5,000 (a page may take up to ten seconds:
Jellyfin's own time, whatever fields are asked for). Artists are the albums' and tracks' own artists, as Jellyfin names
them with their ids; an artist without one is known by its name. Genres come with the lists. Jellyfin has no moods, no
styles and no analysis of the sound. How well known a track is: its play count for the Jellyfin user the playlists
belong to (Jellyfin keeps play counts per user, and there is no sum over all of them).

**A partial run** asks what Jellyfin saved since the last run began (``MinDateLastSaved``, a minute back, on
Jellyfin's clock: the ``Date`` of its answers). More than 5,000 at once (Jellyfin may save every track anew, measured on
a real server): a full run instead. What is gone shows in the totals; Jellyfin has no light list of ids (all ids cost
as much as everything), so when a total differs, a full run instead.

**Ids** of tracks hang on the path of the file: a file renamed or moved is a track gone and a new one. The entries of
the playlists find their track again after the run (``services/relink.py``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Self

from ..jellyfin import (
    ALBUM,
    ALBUM_FIELDS,
    AUDIO,
    ID,
    TRACK_FIELDS,
    JellyfinError,
    JellyfinServer,
)
from ..plex import ALBUM as ALBUM_TYPE
from ..plex import ARTIST as ARTIST_TYPE
from ..plex import TRACK as TRACK_TYPE
from .base import Capabilities, Entry, Listed, NotSupported

if TYPE_CHECKING:
    from ..catalog import Writer

CAPABILITIES = Capabilities(moods=False, sound=False, smart_playlists=False, duplicates=True, changes="poll",
                            playlists=False)

#: How far back a partial run looks beyond where the last run began.
OVERLAP = timedelta(seconds=60)
#: More changes than this in a partial run: a full run instead.
PARTIAL_LIMIT = 5000
#: The key of the marks that holds Jellyfin's clock (seconds) when the last run began.
CLOCK = "clock"
#: An album's picture tag: what makes a changed picture another file in the store of covers.
TAG = re.compile(r"\A[0-9A-Za-z]{1,64}\Z")
UUID = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
VARIOUS = "various artists"
TICKS = 10_000


def _text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number < 10**12 else None


def _plain(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _year(value: Any) -> int | None:
    number = _int(value)
    return number if number is not None and 1000 <= number <= 3000 else None


def _seconds(value: Any) -> int:
    """A moment Jellyfin wrote (``2026-11-15T13:24:49.0262807Z``) in seconds; 0 when there is none."""
    text = str(value or "")
    if not text:
        return 0
    try:
        moment = datetime.fromisoformat(text[:19].replace("Z", "")).replace(tzinfo=UTC)
    except ValueError:
        return 0
    return max(0, int(moment.timestamp()))


def _id(value: Any) -> str:
    text = str(value or "").lower()
    return text if ID.fullmatch(text) else ""


def _mbid(entry: dict[str, Any], name: str) -> str:
    ids = entry.get("ProviderIds") if isinstance(entry.get("ProviderIds"), dict) else {}
    value = str(ids.get(name) or "")
    return value.lower() if UUID.fullmatch(value) else ""


def _names(entry: dict[str, Any], key: str) -> list[str]:
    return [_text(one, 200) for one in entry.get(key) or [] if isinstance(one, str) and _text(one)]


def _artist_of(entry: dict[str, Any]) -> tuple[str, str]:
    """The album's artist of an album or track: its key (Jellyfin's id, else ``name:<name>``) and its name."""
    for one in entry.get("AlbumArtists") or []:
        if isinstance(one, dict):
            name = _text(one.get("Name"), 500)
            ident = _id(one.get("Id"))
            if ident or name:
                return (ident or f"name:{name.lower()}"[:200]), name
    name = _text(entry.get("AlbumArtist"), 500)
    if not name:
        named = _names(entry, "Artists")
        name = named[0] if named else ""
    return (f"name:{name.lower()}"[:200] if name else ""), name


def _performer(entry: dict[str, Any], album_artist: str) -> str:
    """Who performs a track where the album's artist does not say it: on a compilation the first named, else all."""
    named = _names(entry, "Artists")
    if not named:
        return ""
    if album_artist.lower() == VARIOUS:
        return named[0]
    return ", ".join(named)[:500]


def _plays(entry: dict[str, Any]) -> int:
    data = entry.get("UserData") if isinstance(entry.get("UserData"), dict) else {}
    return max(0, _plain(data.get("PlayCount")) or 0)


class JellyfinSource:
    """One Jellyfin server, opened for a while (``with``): what nexmix asks of it, in Jellyfin's ids."""

    kind = "jellyfin"
    capabilities = CAPABILITIES

    def __init__(self, url: str, token: str, device_id: str, library: str, user: str, **options: Any) -> None:
        self.server = JellyfinServer(url, token, device_id, **options)
        self.library = library
        self.user = user

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.server.close()

    @property
    def clock(self) -> int | None:
        return int(self.server.clock.timestamp()) if self.server.clock is not None else None

    @property
    def began(self) -> int | None:
        return int(self.server.began.timestamp()) if self.server.began is not None else None

    # --- What Jellyfin cannot do yet, or at all -----------------------------------------------------------------

    def listing(self) -> list[Listed]:
        raise NotSupported("playlists")

    def items(self, key: str) -> list[Entry]:
        raise NotSupported("playlists")

    def similar(self, track: str, limit: int, max_distance: float) -> list[tuple[str, float]]:
        raise NotSupported("sound")

    # --- Pictures -----------------------------------------------------------------------------------------------

    def cover(self, album: str, size: int) -> tuple[bytes, str]:
        try:
            return self.server.cover(album, size)
        except JellyfinError as exc:
            if exc.code == "jellyfin_bad_request":
                raise JellyfinError("jellyfin_not_found", 404) from exc
            raise

    # --- The catalogue ------------------------------------------------------------------------------------------

    def _user(self) -> str | None:
        return self.user if ID.fullmatch(self.user or "") else None

    def full(self, writer: Writer) -> dict[str, Any]:
        """The whole catalogue into the writer's generation; returns the marks for the next partial run."""
        artists: dict[str, str] = {}
        writer.step("albums")
        total_albums = self.server.count(self.library, ALBUM)
        albums: dict[str, dict[str, Any]] = {}
        for entries in self.server.pages(self.library, ALBUM, ALBUM_FIELDS, images=True):
            self._albums(writer, entries, artists, albums)
            writer.share(len(albums) / total_albums if total_albums else 1.0)
            writer.check()
        writer.commit()
        writer.step("tracks")
        total_tracks = self.server.count(self.library, AUDIO)
        taken = 0
        years = {key: row["year"] for key, row in albums.items()}
        for entries in self.server.pages(self.library, AUDIO, TRACK_FIELDS, user=self._user()):
            rows = self._tracks(writer, entries, artists, years)
            writer.rows(TRACK_TYPE, rows)
            taken += len(rows)
            writer.share(taken / total_tracks if total_tracks else 1.0)
            writer.check()
        writer.commit()
        writer.step("artists")
        writer.rows(ARTIST_TYPE, self._artist_rows(writer, artists))
        writer.commit()
        marks = {CLOCK: self.began} if self.began is not None else {}
        return {"marks": marks, "analysis": {},
                "counts": {"artists": len(artists), "albums": len(albums), "tracks": taken}}

    def _albums(self, writer: Writer, entries: Iterable[dict[str, Any]], artists: dict[str, str],
                into: dict[str, dict[str, Any]], *, write: bool = True) -> list[dict[str, Any]]:
        chosen = [entry for entry in entries if _id(entry.get("Id")) and entry.get("Type") in (None, ALBUM)]
        writer.numbers.ensure("album", (_id(entry.get("Id")) for entry in chosen))
        rows = []
        for entry in chosen:
            key = _id(entry.get("Id"))
            if key in into:
                continue
            artist_key, artist_name = _artist_of(entry)
            if artist_key:
                artists.setdefault(artist_key, artist_name)
                writer.numbers.ensure("artist", [artist_key])
            images = entry.get("ImageTags") if isinstance(entry.get("ImageTags"), dict) else {}
            tag = str(images.get("Primary") or "")
            row = {"id": writer.numbers.of("album", key),
                   "artist_id": (writer.numbers.of("artist", artist_key) or 0) if artist_key else 0,
                   "title": _text(entry.get("Name")), "year": _year(entry.get("ProductionYear")),
                   "thumb": tag if TAG.fullmatch(tag) else "", "added_at": _seconds(entry.get("DateCreated")),
                   "updated_at": 0}
            rows.append(row)
            into[key] = row
            for name in _names(entry, "Genres"):
                writer.link(ALBUM_TYPE, row["id"], "genre", name)
        if write:
            writer.rows(ALBUM_TYPE, rows)
        return rows

    def _tracks(self, writer: Writer, entries: Iterable[dict[str, Any]], artists: dict[str, str],
                years: dict[str, int | None]) -> list[dict[str, Any]]:
        chosen = [entry for entry in entries if _id(entry.get("Id")) and entry.get("Type") in (None, AUDIO)]
        writer.numbers.ensure("track", (_id(entry.get("Id")) for entry in chosen))
        writer.numbers.ensure("album", (key for key in (_id(entry.get("AlbumId")) for entry in chosen) if key))
        rows = []
        seen: set[str] = set()
        for entry in chosen:
            key = _id(entry.get("Id"))
            if key in seen:
                continue
            seen.add(key)
            artist_key, artist_name = _artist_of(entry)
            if artist_key:
                artists.setdefault(artist_key, artist_name)
            album = _id(entry.get("AlbumId"))
            performer = _performer(entry, artist_name)
            plays = _plays(entry)
            year = years.get(album) if album in years else _year(entry.get("ProductionYear"))
            row = {
                "id": writer.numbers.of("track", key),
                "album_id": (writer.numbers.of("album", album) or 0) if album else 0,
                "artist_id": (writer.numbers.of("artist", artist_key) or 0) if artist_key else 0,
                "title": _text(entry.get("Name")),
                "artist": performer if performer and performer != artist_name else "",
                "duration": max(0, (_plain(entry.get("RunTimeTicks")) or 0) // TICKS),
                "rating_count": plays, "view_count": plays,
                "year": year if year is not None else _year(entry.get("ProductionYear")),
                "disc": _plain(entry.get("ParentIndexNumber")), "number": _plain(entry.get("IndexNumber")),
                "added_at": _seconds(entry.get("DateCreated")), "updated_at": 0,
                "song_key": "", "variant": False, "shown": True, "original_year": None, "guid": "",
                "mbid_track": _mbid(entry, "MusicBrainzTrack"), "mbid_recording": _mbid(entry, "MusicBrainzRecording"),
            }
            rows.append(row)
            for name in _names(entry, "Genres"):
                writer.link(TRACK_TYPE, row["id"], "genre", name)
        return rows

    @staticmethod
    def _artist_rows(writer: Writer, artists: dict[str, str]) -> list[dict[str, Any]]:
        writer.numbers.ensure("artist", artists)
        return [{"id": writer.numbers.of("artist", key), "title": _text(name), "thumb": "", "added_at": 0,
                 "updated_at": 0} for key, name in artists.items()]

    def partial(self, writer: Writer, marks: dict[str, int], analysis_due: bool) -> dict[str, Any]:
        """What Jellyfin saved since the last run, ready to be written. ``TooMany`` (from ``catalog``) when a full run
        is needed instead: too much at once, or a total that says something is gone."""
        from ...models import CatalogAlbum
        from ..catalog import TooMany

        mark = int(marks.get(CLOCK) or 0)
        if not mark:
            raise TooMany
        since = datetime.fromtimestamp(mark, UTC) - OVERLAP
        writer.step("changes")
        fresh = self.server.count(self.library, AUDIO, since)
        if self.server.clock is not None and since > self.server.clock:
            # The mark lies ahead of Jellyfin's clock (its clock went back): from its clock, never from a time to come.
            since = self.server.clock - OVERLAP
            fresh = self.server.count(self.library, AUDIO, since)
        if fresh + self.server.count(self.library, ALBUM, since) > PARTIAL_LIMIT:
            raise TooMany
        artists: dict[str, str] = {}
        albums: dict[str, dict[str, Any]] = {}
        for entries in self.server.pages(self.library, ALBUM, ALBUM_FIELDS, since=since, images=True):
            self._albums(writer, entries, artists, albums, write=False)
        changed_tracks: list[dict[str, Any]] = []
        for entries in self.server.pages(self.library, AUDIO, TRACK_FIELDS, since=since, user=self._user()):
            changed_tracks.extend(entries)
        # The year of a track is its album's: of a changed album, else of the album as the catalogue has it.
        years: dict[str, int | None] = {key: row["year"] for key, row in albums.items()}
        wanted = {_id(entry.get("AlbumId")) for entry in changed_tracks} - set(years) - {""}
        numbers = {writer.numbers.of("album", key): key for key in wanted}
        if numbers:
            for ident, year in writer.db.execute(
                    CatalogAlbum.__table__.select().with_only_columns(CatalogAlbum.id, CatalogAlbum.year).where(
                        CatalogAlbum.server_id == writer.server_id, CatalogAlbum.generation == writer.generation,
                        CatalogAlbum.id.in_(list(numbers)))):
                years[numbers[int(ident)]] = year
        tracks = self._tracks(writer, changed_tracks, artists, years)
        writer.step("removed")
        ours_tracks = {writer.numbers.key("track", number) for number in writer.ours(TRACK_TYPE)}
        ours_albums = {writer.numbers.key("album", number) for number in writer.ours(ALBUM_TYPE)}
        new_tracks = {_id(entry.get("Id")) for entry in changed_tracks}
        if self.server.count(self.library, AUDIO) != len((ours_tracks - {None}) | new_tracks) or \
                self.server.count(self.library, ALBUM) != len((ours_albums - {None}) | set(albums)):
            raise TooMany
        rows: dict[int, dict[int, dict[str, Any]]] = {ARTIST_TYPE: {}, ALBUM_TYPE: {}, TRACK_TYPE: {}}
        tags: dict[int, dict[int, dict[str, list[str]]]] = {ARTIST_TYPE: {}, ALBUM_TYPE: {}, TRACK_TYPE: {}}
        for row in self._artist_rows(writer, artists):
            rows[ARTIST_TYPE][row["id"]] = row
            tags[ARTIST_TYPE][row["id"]] = {}
        # The genres the rows above linked (``writer.link``) are taken over by ``catalog._replace``; here they are
        # given again per row, as it wants them.
        names = {ident: name for (kind, name), ident in writer.tags.ids.items() if kind == "genre"}
        for of_type, found in ((ALBUM_TYPE, list(albums.values())), (TRACK_TYPE, tracks)):
            by_item: dict[int, list[str]] = {}
            for item, tag in writer.tags.links[of_type]:
                if tag in names:
                    by_item.setdefault(item, []).append(names[tag])
            for row in found:
                rows[of_type][row["id"]] = row
                tags[of_type][row["id"]] = {"genre": sorted(by_item.get(row["id"], []))}
            writer.tags.links[of_type] = set()
        new_marks = {CLOCK: self.began} if self.began is not None else dict(marks)
        return {"rows": rows, "tags": tags, "gone": {}, "marks": new_marks, "analysis": {}}
