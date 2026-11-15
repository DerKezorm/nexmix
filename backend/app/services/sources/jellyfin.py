"""The connector to a Jellyfin server: its catalogue, its playlists, its sound and its pictures
(``services/jellyfin.py`` is the HTTP client beneath it).

**The catalogue.** The albums of the library, then its tracks, in pages of 5,000 (a page may take up to ten seconds:
Jellyfin's own time, whatever fields are asked for). Artists are the albums' and tracks' own artists, as Jellyfin names
them with their ids; an artist without one is known by its name. Genres come with the lists. Jellyfin has no moods, no
styles and no analysis of the sound. How well known a track is: its play count for the Jellyfin user the playlists
belong to (Jellyfin keeps play counts per user, and there is no sum over all of them).

**A partial run** asks what Jellyfin saved since the last run began (``MinDateLastSaved``, a minute back, on
Jellyfin's clock: the ``Date`` of its answers). More than 5,000 at once (Jellyfin may save every track anew, measured on
a real server): a full run instead. What is gone shows in the totals; Jellyfin has no light list of ids (all ids cost
as much as everything), so when a total differs, a full run instead.

**Ids** of tracks hang on the path of the file: a file renamed or moved is a track gone and a new one, and Jellyfin
drops it from its playlists without a word. The entries of nexmix's playlists find their track again after the run
(``services/relink.py``).

**Playlists**, as Jellyfin 12.4 answered (measured on a throwaway server):

* They belong to the user chosen when connecting (``UserId``) and are made public (``IsPublic``), so that every
  Jellyfin user sees them. The list (``/Items?IncludeItemTypes=Playlist``, as that user) has every playlist the user
  may see, other users' public ones too; which are the user's own says only ``/Playlists/<id>/Users``, which answers
  200 to the owner (and to whom it is shared with for changing) and 403 to anybody else (``owned``).
* An entry has no id of its own: ``PlaylistItemId`` is the track's ``Id``. A track may stand in a playlist more than
  once; taking out by its id takes out every copy, moving it moves the first one. nexmix keeps a track once; copies
  made in Jellyfin count as one.
* Every change gives the playlist a new ``Etag`` at once (its entries' order too); the list carries it.
* **Adding** takes the ids in the address only (``?ids=``): ids in a JSON body are passed over with 204 and nothing
  added. The address holds about 240 ids (8 KiB, then 414): 200 to a request. Added at the end, in that order.
* **Taking out** names the entries in the address too (``entryIds``, 200 to a request); **moving** names the place the
  entry is to have afterwards (``Move/<index>``, at most the number of entries less one). An id Jellyfin does not know
  is passed over with 204: whether a change took, only reading again says.
* Deleting is ``DELETE /Items/<id>``; a playlist gone answers 404.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
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
    checked_id,
)
from ..plex import ALBUM as ALBUM_TYPE
from ..plex import ARTIST as ARTIST_TYPE
from ..plex import TRACK as TRACK_TYPE
from .base import PLAYLISTS_NOT_FOUND, Capabilities, Entry, Listed, NotSupported

if TYPE_CHECKING:
    from ..catalog import Writer

CAPABILITIES = Capabilities(moods=False, sound=False, smart_playlists=False, duplicates=True, changes="both",
                            entries="track")

#: Ids in one address: about 240 fit (8 KiB), 200 leave room.
CHUNK = 200
PLAYLIST = "Playlist"
#: The user the playlists belong to is gone from Jellyfin (deleted, or made anew with another id): connect again.
USER_GONE = "jellyfin_user_gone"

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


def _items_of(found: Any) -> list[dict[str, Any]]:
    entries = found.get("Items") if isinstance(found, dict) else None
    return [one for one in entries if isinstance(one, dict)] if isinstance(entries, list) else []


def _listed(entry: Any) -> Listed | None:
    if not isinstance(entry, dict) or entry.get("Type") not in (None, PLAYLIST):
        return None
    ident = _id(entry.get("Id"))
    if not ident:
        return None
    return Listed(key=ident, guid="", title=_text(entry.get("Name"), 200), smart=False,
                  leaf_count=max(0, _plain(entry.get("ChildCount")) or 0), updated_at=0,
                  etag=_text(entry.get("Etag"), 64), created=_seconds(entry.get("DateCreated")))


class JellyfinSource:
    """One Jellyfin server, opened for a while (``with``): what nexmix asks of it, in Jellyfin's ids."""

    kind = "jellyfin"
    capabilities = CAPABILITIES

    def __init__(self, url: str, token: str, device_id: str, library: str, user: str, **options: Any) -> None:
        self.server = JellyfinServer(url, token, device_id, **options)
        self.library = library
        self.user = user
        #: Each playlist's whole order as last read (a track twice stands twice), for the places of moves.
        self._order: dict[str, list[str]] = {}
        #: The user nexmix signed in as ("" for an API key), asked once when needed.
        self._me: str | None = None

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

    # --- Playlists ----------------------------------------------------------------------------------------------

    def _playlist_user(self) -> str:
        user = self._user()
        if user is None:
            # No user chosen yet: nexmix keeps no playlist there before one is.
            raise JellyfinError("jellyfin_no_user", 409)
        return user

    def _list_fault(self, exc: JellyfinError) -> JellyfinError:
        """What a 404 to the list of playlists (or a 500 to a playlist's entries) means: the chosen user is gone from
        Jellyfin (``USER_GONE``, measured: ``/Items`` answers 404 for an unknown ``userId``, ``/Playlists/<id>/Items``
        500), or the server's address leads somewhere else (``PLAYLISTS_NOT_FOUND``). Never a playlist deleted."""
        try:
            self.server.public_info()
        except JellyfinError as other:
            if other.code in ("jellyfin_not_found", "not_jellyfin"):
                return JellyfinError(PLAYLISTS_NOT_FOUND, 404)
            return other
        return JellyfinError(USER_GONE, 409) if exc.code == "jellyfin_not_found" else exc

    def listing(self) -> list[Listed]:
        """The playlists the user sees, with their ``Etag`` and when they were made: music ones, and empty ones (an
        empty one has no kind yet). A 404 to the list is never read as playlists deleted (``_list_fault``)."""
        try:
            found = self.server.get("/Items", {"IncludeItemTypes": PLAYLIST, "Recursive": "true",
                                               "userId": self._playlist_user(), "Fields": "ChildCount,Etag,DateCreated",
                                               "EnableImages": "false", "EnableUserData": "false"}, timeout=30.0)
        except JellyfinError as exc:
            if exc.code == "jellyfin_not_found":
                raise self._list_fault(exc) from exc
            raise
        listed: list[Listed] = []
        for entry in _items_of(found):
            one = _listed(entry)
            if one is None:
                continue
            kind = str(entry.get("MediaType") or "")
            if kind == "Audio" or (kind in ("", "Unknown") and not one.leaf_count):
                listed.append(one)
        return listed

    def meta(self, key: str) -> Listed:
        """One playlist as the list gives it, of whatever kind; ``jellyfin_not_found`` once Jellyfin has it no longer
        (not in the list, and its entries answer 404)."""
        ident = checked_id(key)
        found = self.server.get("/Items", {"Ids": ident, "userId": self._playlist_user(), "Fields": "ChildCount,Etag",
                                           "EnableImages": "false", "EnableUserData": "false"}, timeout=30.0)
        for entry in _items_of(found):
            one = _listed(entry)
            if one is not None and one.key == ident:
                return one
        # Not listed: gone, unless its entries still answer (then it is there, of whatever kind). Whether it is gone
        # says only the sync, after it read the whole list again (``sync.Round._vanished``).
        self._entries(f"/Playlists/{ident}/Items", {"userId": self._playlist_user(), "Limit": 0}, timeout=30.0)
        return Listed(key=ident, guid="", title="", smart=False, leaf_count=0, updated_at=0)

    def _entries(self, path: str, params: dict[str, Any], timeout: float) -> Any:
        """A playlist's entries as the user sees them; a 500 there is what an unknown user gets (``_list_fault``)."""
        try:
            return self.server.get(path, params, timeout=timeout)
        except JellyfinError as exc:
            if exc.code != "jellyfin_error":
                raise
            self.listing()
            raise

    def gone(self, key: str) -> bool:
        """Whether Jellyfin has the playlist no longer, asked of its entries without a user (measured on 12.4: 404
        once it is deleted, 200 while it is there). An API key has no user of its own and gets 400 so: then asked as
        the chosen user (404 deleted, 200 there, 500 for a user Jellyfin does not know, which ``_entries`` turns into
        ``USER_GONE``). False when they answer; any other fault is raised."""
        ident = checked_id(key)
        path = f"/Playlists/{ident}/Items"
        quiet = {"Limit": 0, "EnableImages": "false", "EnableUserData": "false"}
        try:
            try:
                self.server.get(path, quiet, timeout=30.0)
            except JellyfinError as exc:
                if exc.code != "jellyfin_bad_request":
                    raise
                self._entries(path, {**quiet, "userId": self._playlist_user()}, timeout=30.0)
        except JellyfinError as exc:
            if exc.code == "jellyfin_not_found":
                return True
            raise
        return False

    def items(self, key: str) -> list[Entry]:
        """Every entry in order, in one answer; a track that stands more than once counts once, at its first place.
        The whole order, copies and all, is kept for the moves that follow (``reorder``)."""
        ident = checked_id(key)
        found = self._entries(f"/Playlists/{ident}/Items", {"userId": self._playlist_user(), "EnableImages": "false",
                                                           "EnableUserData": "false"}, timeout=120.0)
        raw: list[str] = []
        entries: list[Entry] = []
        seen: set[str] = set()
        for entry in _items_of(found):
            track = _id(entry.get("Id"))
            if not track:
                continue
            raw.append(track)
            if track in seen or entry.get("Type") not in (None, AUDIO):
                continue
            seen.add(track)
            _artist_key, artist_name = _artist_of(entry)
            performer = _performer(entry, artist_name)
            entries.append(Entry(
                item=None, track=track, title=_text(entry.get("Name")), artist=(performer or artist_name)[:500],
                album=_text(entry.get("Album")), album_key=_id(entry.get("AlbumId")),
                year=_year(entry.get("ProductionYear")),
                duration=max(0, (_plain(entry.get("RunTimeTicks")) or 0) // TICKS), guid="", ref=track))
        self._order[ident] = raw
        return entries

    def smart_items(self, key: str, limit: int | None = None) -> tuple[list[Entry], int]:
        raise NotSupported("smart playlists")

    def create(self, title: str) -> Listed:
        """An empty playlist named ``title``, the chosen user's, public."""
        found = self.server.post("/Playlists", body={"Name": title[:200], "UserId": self._playlist_user(),
                                                     "MediaType": "Audio", "IsPublic": True}, timeout=30.0)
        ident = _id(found.get("Id")) if isinstance(found, dict) else ""
        if not ident:
            raise JellyfinError("jellyfin_error")
        self._order[ident] = []
        return Listed(key=ident, guid="", title=title[:200], smart=False, leaf_count=0, updated_at=0)

    def add(self, key: str, tracks: Sequence[str], answered: Callable[[list[str], int | None], None] | None = None,
            ) -> int:
        """The tracks at the end, in their order, ``CHUNK`` to a request, in the address (a body is passed over).
        Jellyfin says nothing of what it took: ``answered`` gets None, and the caller reads again."""
        ident = checked_id(key)
        user = self._playlist_user()
        for start in range(0, len(tracks), CHUNK):
            part = [checked_id(track) for track in tracks[start:start + CHUNK]]
            self.server.send("POST", f"/Playlists/{ident}/Items", {"ids": ",".join(part), "userId": user},
                             timeout=60.0)
            if answered is not None:
                answered(part, None)
        return len(tracks)

    def remove_many(self, key: str, refs: Sequence[str]) -> None:
        """The entries out, by their tracks' ids, ``CHUNK`` to a request (every copy of a track goes)."""
        ident = checked_id(key)
        chosen = list(dict.fromkeys(checked_id(ref) for ref in refs))
        for start in range(0, len(chosen), CHUNK):
            self.server.send("DELETE", f"/Playlists/{ident}/Items",
                             {"entryIds": ",".join(chosen[start:start + CHUNK])}, timeout=60.0)
        order = self._order.get(ident)
        if order is not None:
            gone = set(chosen)
            self._order[ident] = [track for track in order if track not in gone]

    def reorder(self, key: str, steps: Sequence[tuple[str, str | None]]) -> bool:
        """The moves in turn, each an entry right after another (None: to the start), as ``Move/<index>``: the place
        the entry has afterwards, counted in the order as last read (copies too) with each move before applied. False:
        an entry is not in that order (the caller reads again and plans anew)."""
        ident = checked_id(key)
        order = list(self._order.get(ident) or [])
        for ref, after in steps:
            track = checked_id(ref)
            behind = checked_id(after) if after is not None else None
            if track not in order or (behind is not None and behind not in order):
                self._order[ident] = order
                return False
            order.remove(track)
            place = 0 if behind is None else order.index(behind) + 1
            self.server.send("POST", f"/Playlists/{ident}/Items/{track}/Move/{place}", timeout=30.0)
            order.insert(place, track)
        self._order[ident] = order
        return True

    def rename(self, key: str, title: str) -> None:
        self.server.post(f"/Playlists/{checked_id(key)}", body={"Name": title[:200]}, timeout=30.0)

    def delete(self, key: str) -> bool:
        """False: Jellyfin had it no longer."""
        try:
            self.server.send("DELETE", f"/Items/{checked_id(key)}", timeout=30.0)
        except JellyfinError as exc:
            if exc.code == "jellyfin_not_found":
                return False
            raise
        return True

    def existing(self, tracks: Iterable[str]) -> set[str]:
        """Which of the tracks the library of Jellyfin still has, asked ``CHUNK`` at a time (``/Items?Ids=``, only
        reading). Any fault is raised: what was not asked is not known."""
        chosen = sorted({checked_id(track) for track in tracks})
        found: set[str] = set()
        for start in range(0, len(chosen), CHUNK):
            answer = self.server.get("/Items", {"Ids": ",".join(chosen[start:start + CHUNK]), "EnableImages": "false",
                                                "EnableUserData": "false"}, timeout=60.0)
            for entry in _items_of(answer):
                ident = _id(entry.get("Id"))
                if ident and entry.get("Type") in (None, AUDIO):
                    found.add(ident)
        return found

    def owned(self, keys: Iterable[str]) -> set[str]:
        """Which of the playlists are the chosen user's own, for taking in those made in Jellyfin. Only the owner (and
        whom it is shared with for changing) may read whom it is shared with; one shared with the user is not his own
        (he is among them). It can be told only when nexmix signed in as that user (Quick Connect by him): with an API
        key or another user's token, nothing is taken in."""
        chosen = [checked_id(key) for key in keys]
        if not chosen:
            return set()
        user = self._playlist_user()
        if self._me is None:
            me = self.server.me()
            self._me = me.id if me is not None else ""
        if self._me != user:
            return set()
        found: set[str] = set()
        for ident in chosen:
            try:
                shares = self.server.get(f"/Playlists/{ident}/Users", timeout=15.0)
            except JellyfinError as exc:
                if exc.code in ("jellyfin_forbidden", "jellyfin_not_found", "jellyfin_bad_request"):
                    continue
                raise
            names = {str(one.get("UserId") or "").replace("-", "").lower()
                     for one in (shares if isinstance(shares, list) else []) if isinstance(one, dict)}
            if user not in names:
                found.add(ident)
        return found

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
