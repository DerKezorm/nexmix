"""Finding a track on another server: what an entry of a playlist knows about its track (title, artist, album, length,
the server's ids for it) against the catalogue of the other server. Pure: no database, no server, no clock.

The steps, in this order; the first that finds something decides, and says how (``how``):

1. ``guid``: the same id of the recording, where both sides have one that names it on every server (a ``guid`` of
   Plex's agents, never a ``local://`` one, which only its own server knows).
2. ``mbid``: the same MusicBrainz id. Plex's ``mbid://`` is the id of the *track* (measured: it meets Jellyfin's
   ``MusicBrainzTrack``, never ``…Recording``), so both sides must give the track id.
3. ``album``: the same title on the same album by the same artist (written alike, ``songs.norm``; the artist is the
   album's or the track's own). Of several, the one closest in length, which must then lie within ``TOLERANCE``.
4. ``length``: the same title by the same artist, within ``TOLERANCE`` of the length; the closest.
5. ``song``: a version of the same song (``songs.song_key``: core of the title and artist), the version shown first,
   then the closest in length.
6. ``title``: the same title alone, within ``TITLE_TOLERANCE``; the closest.

Steps 1 to 4 count as sure, 5 and 6 as worth a look (``UNSURE``): measured on 52,600 tracks from Plex to Jellyfin,
1 to 4 found 84.6 %, 5 and 6 another 12.6 %, 2.8 % nothing. No track of the other server is ever given to two entries
(``used``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from . import songs

#: How far apart in length two tracks may lie and still be one (milliseconds).
TOLERANCE = 3000
#: The same, for the last step on the title alone.
TITLE_TOLERANCE = 2000

GUID, MBID, ALBUM, LENGTH, SONG, TITLE = "guid", "mbid", "album", "length", "song", "title"
#: The steps in their order.
STEPS = (GUID, MBID, ALBUM, LENGTH, SONG, TITLE)
#: Found, but by a rule that may take another version: worth a look.
UNSURE = frozenset({SONG, TITLE})


@dataclass(frozen=True)
class Candidate:
    """A track of the catalogue searched in."""

    id: int
    title: str
    album: str
    album_artist: str
    #: The track's own artist, where it differs from the album's (empty: the album's).
    artist: str = ""
    #: Milliseconds.
    duration: int = 0
    song_key: str = ""
    #: The version the search shows for its song.
    shown: bool = True
    guid: str = ""
    mbid: str = ""


@dataclass(frozen=True)
class Wanted:
    """What is known of the track to be found."""

    title: str
    artist: str
    album: str
    duration: int = 0
    guid: str = ""
    mbid: str = ""
    #: The album's artist, where it is known apart from ``artist``.
    album_artist: str = ""


@dataclass(frozen=True)
class Found:
    track: Candidate
    how: str

    @property
    def sure(self) -> bool:
        return self.how not in UNSURE


def lasting(guid: str) -> bool:
    """Whether a ``guid`` names the recording on every server (not one only its own server knows)."""
    return bool(guid) and not guid.startswith("local://")


def _people(candidate: Candidate) -> set[str]:
    names = {songs.norm(candidate.album_artist), songs.norm(candidate.artist)}
    names |= {songs.norm(songs.first_name(name)) for name in (candidate.album_artist, candidate.artist) if name}
    return names - {""}


def _closest(found: list[Candidate], duration: int, limit: int | None) -> Candidate | None:
    if not found:
        return None
    best = min(found, key=lambda one: (abs(one.duration - duration), one.id))
    if limit is not None and abs(best.duration - duration) > limit:
        return None
    return best


class Finder:
    """The catalogue searched in, with its indexes made once."""

    def __init__(self, candidates: Iterable[Candidate]) -> None:
        self.by_id: dict[int, Candidate] = {}
        self.by_guid: dict[str, list[Candidate]] = {}
        self.by_mbid: dict[str, list[Candidate]] = {}
        self.by_title: dict[str, list[Candidate]] = {}
        self.by_song: dict[str, list[Candidate]] = {}
        for one in candidates:
            self.by_id[one.id] = one
            if lasting(one.guid):
                self.by_guid.setdefault(one.guid, []).append(one)
            if one.mbid:
                self.by_mbid.setdefault(one.mbid, []).append(one)
            self.by_title.setdefault(songs.norm(one.title), []).append(one)
            if one.song_key and not one.song_key.startswith("#"):
                self.by_song.setdefault(one.song_key, []).append(one)

    def find(self, wanted: Wanted, used: set[int] | frozenset[int] = frozenset()) -> Found | None:
        """The track of this catalogue that is ``wanted``, by the first step that finds one not ``used``."""

        def free(found: Iterable[Candidate]) -> list[Candidate]:
            return sorted((one for one in found if one.id not in used), key=lambda one: one.id)

        if lasting(wanted.guid):
            same = free(self.by_guid.get(wanted.guid, ()))
            if same:
                return Found(same[0], GUID)
        if wanted.mbid:
            same = free(self.by_mbid.get(wanted.mbid, ()))
            if same:
                return Found(_closest(same, wanted.duration, None) or same[0], MBID)
        title = songs.norm(wanted.title)
        named = free(self.by_title.get(title, ())) if title else []
        people = {songs.norm(wanted.artist), songs.norm(wanted.album_artist),
                  songs.norm(songs.first_name(wanted.artist))} - {""}
        by_them = [one for one in named if _people(one) & people]
        album = songs.norm(wanted.album)
        on_album = [one for one in by_them if album and songs.norm(one.album) == album]
        if len(on_album) == 1:
            return Found(on_album[0], ALBUM)
        found = _closest(on_album, wanted.duration, TOLERANCE)
        if found is not None:
            return Found(found, ALBUM)
        found = _closest(by_them, wanted.duration, TOLERANCE)
        if found is not None:
            return Found(found, LENGTH)
        keys = {songs.song_key(0, wanted.title, wanted.album_artist or wanted.artist, wanted.artist),
                songs.song_key(0, wanted.title, wanted.artist),
                songs.song_key(0, wanted.title, songs.first_name(wanted.artist))} - {"#0"}
        versions = free(one for key in sorted(keys) for one in self.by_song.get(key, ()))
        if versions:
            best = min(versions, key=lambda one: (not one.shown, abs(one.duration - wanted.duration), one.id))
            return Found(best, SONG)
        found = _closest(named, wanted.duration, TITLE_TOLERANCE)
        if found is not None:
            return Found(found, TITLE)
        return None


def assign(wanted: Sequence[Wanted], candidates: Iterable[Candidate],
           taken: Iterable[int] = ()) -> list[Found | None]:
    """Each of ``wanted`` its track, in their order, never one track twice (and none of ``taken``)."""
    finder = Finder(candidates)
    used = set(taken)
    found: list[Found | None] = []
    for one in wanted:
        hit = finder.find(one, used)
        if hit is not None:
            used.add(hit.track.id)
        found.append(hit)
    return found
