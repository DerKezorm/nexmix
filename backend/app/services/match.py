"""Finding a track on another server: what an entry of a playlist knows about its track (title, artist, album, length,
its place on the album, the server's ids for it) against the catalogue of the other server. Pure: no database, no
server, no clock.

The steps, in this order; the first that finds something decides, and says how (``how``):

1. ``guid``: the same id of the recording, where both sides have one that names it on every server (a ``guid`` of
   Plex's agents, never a ``local://`` one, which only its own server knows).
2. ``mbid``: the same MusicBrainz id. Plex's ``mbid://`` is the id of the *track* (measured: it meets Jellyfin's
   ``MusicBrainzTrack``, never ``…Recording``), so both sides must give the track id.
3. ``album``: the same title on the same album by the same artist (the artist is the album's or the track's own). Of
   several, the one closest in length, which must then lie within ``TOLERANCE``.
4. ``edition``: the same, on an album whose name differs only in a note of its edition ("Remastered", "Deluxe
   Edition"). Not the same file (``relink.py`` does not take it), but the same recording for a playlist.
5. ``length``: the same title by the same artist, within ``TOLERANCE`` of the length; the closest.
6. ``renamed``: the album under another name (a server's own matching renames albums): an album of the same album
   artist whose tracks lie at the same places with the same lengths as the tracks of the entry's album, for most of
   them (``SHAPE_SHARE``, at least ``SHAPE_LEAST``); the track at the entry's place there, within ``TOLERANCE``.
7. ``song``: a version of the same song (``songs.song_key``: core of the title and artist), the version shown first,
   then the closest in length.
8. ``title``: the same title alone, within ``TITLE_TOLERANCE``; the closest.

Steps 1 to 6 count as sure, 7 and 8 as worth a look (``UNSURE``): measured on 52,600 tracks from Plex to Jellyfin, the
steps without ``renamed`` found 84.6 % sure, another 12.6 % worth a look, 2.8 % nothing. No track of the other server is
ever given to two entries (``used``). Every finding names its step (``Found.how``, ``Found.sure``): the caller decides
what it takes; the sync takes the sure ones only, the unsure ones are for a person to confirm.

**Written alike** (``title_key``, ``album_key``, ``person_key``): case, accents and signs do not count; a title loses a
guest ("feat. …", "ft. …", "featuring …", in brackets or not) and a note of its edition ("Remastered 2011", "- 2009
Remaster", "Album Version", "Mono", "Explicit"), never what makes another version ("Live", "Acoustic", "Remix", "Part
2"); an album loses its edition the same way; a person loses a leading "the", and of several the first counts too;
"Various Artists" names nobody.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache

from . import songs

#: How far apart in length two tracks may lie and still be one (milliseconds).
TOLERANCE = 3000
#: The same, for the last step on the title alone.
TITLE_TOLERANCE = 2000
#: An album counts as the same under another name when at least this share of the entry's album, and at least
#: ``SHAPE_LEAST`` tracks, lie at the same places with the same lengths.
SHAPE_SHARE = 0.6
SHAPE_LEAST = 3

GUID, MBID, ALBUM, EDITION, LENGTH, RENAMED, SONG, TITLE = (
    "guid", "mbid", "album", "edition", "length", "renamed", "song", "title")
#: The steps in their order.
STEPS = (GUID, MBID, ALBUM, EDITION, LENGTH, RENAMED, SONG, TITLE)
#: Found, but by a rule that may take another version: worth a look.
UNSURE = frozenset({SONG, TITLE})
#: Not by a rule: chosen by a person, or taken as it was (``hand``), and nothing found (``none``).
HAND, NONE = "hand", "none"

#: Notes of an edition: they say nothing about which recording it is.
_EDITION = re.compile(
    r"^(?:(?:\d{4}\s+)?(?:digital(?:ly)?\s+)?remaster(?:ed)?(?:\s+(?:version|edition|\d{4}))*(?:\s+\d{4})?"
    r"|(?:super\s+)?deluxe(?:\s+(?:edition|version))?|expanded(?:\s+edition)?|anniversary(?:\s+edition)?"
    r"|\d+(?:st|nd|rd|th)\s+anniversary(?:\s+edition)?|special\s+edition|bonus\s+track(?:\s+version)?"
    r"|(?:album|lp|single|original|stereo|mono)\s+version|mono|stereo|explicit|explicit\s+version)$"
)
_GUEST_INSIDE = re.compile(r"^(?:feat|ft|featuring|with)\b")
_BRACKETS = re.compile(r"\s*[(\[]([^()\[\]]*)[)\]]")
_DASH = re.compile(r"\s+[-‐–—]\s+")
_GUEST_AFTER = re.compile(r"\s+(?:feat\.?|ft\.|featuring)\s.*\Z", re.IGNORECASE | re.DOTALL)


def _strip_notes(text: str, *, guests: bool) -> str:
    """``text`` without the brackets and the parts after a spaced dash that note an edition (and, ``guests``, a
    guest); what names another version stays."""
    raw = str(text or "")

    def bracket(found: re.Match[str]) -> str:
        inside = songs.norm(found.group(1))
        if not inside or _EDITION.match(inside) or (guests and _GUEST_INSIDE.match(inside)):
            return " "
        return f" ({found.group(1)}) "

    rest = _BRACKETS.sub(bracket, raw)
    if guests:
        rest = _GUEST_AFTER.sub("", rest)
    parts = _DASH.split(rest)
    if len(parts) > 1 and songs.norm(parts[0]):
        rest = " - ".join([parts[0], *(part for part in parts[1:] if not _EDITION.match(songs.norm(part)))])
    return rest


def title_key(title: str) -> str:
    """A title written alike: without guest and edition, in ``songs.norm``."""
    found = songs.norm(_strip_notes(title, guests=True))
    return found or songs.norm(title)


@lru_cache(maxsize=65_536)
def album_key(album: str) -> str:
    """An album written alike: without the note of its edition."""
    found = songs.norm(_strip_notes(album, guests=False))
    return found or songs.norm(album)


@lru_cache(maxsize=65_536)
def person_key(name: str) -> str:
    return songs.norm(name).removeprefix("the ")


@lru_cache(maxsize=65_536)
def people_keys(*names: str) -> frozenset[str]:
    """The names a track goes by: each as it is and the first of each list of performers ("A feat. B": A)."""
    found = {person_key(name) for name in names if name}
    found |= {person_key(songs.first_name(name)) for name in names if name}
    # "Various Artists" names nobody: two tracks of two compilations are not by the same artist for it.
    return frozenset(name for name in found if name and name not in songs.COMPILATIONS)


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
    #: Its place on the album (None: not known).
    number: int | None = None
    disc: int | None = None


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
    #: Its place on its album, and the places and lengths of all tracks of that album (disc, number, milliseconds):
    #: what the step ``renamed`` compares. Empty: that step is left out.
    number: int | None = None
    disc: int | None = None
    shape: tuple[tuple[int, int, int], ...] = field(default=())


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


def _closest(found: list[Candidate], duration: int, limit: int | None) -> Candidate | None:
    if not found:
        return None
    best = min(found, key=lambda one: (abs(one.duration - duration), one.id))
    if limit is not None and abs(best.duration - duration) > limit:
        return None
    return best


def _place(disc: int | None, number: int | None) -> tuple[int, int] | None:
    if number is None or number <= 0:
        return None
    return (disc if disc and disc > 0 else 1, number)


class Finder:
    """The catalogue searched in, with its indexes made once."""

    def __init__(self, candidates: Iterable[Candidate]) -> None:
        self.by_id: dict[int, Candidate] = {}
        self.by_guid: dict[str, list[Candidate]] = {}
        self.by_mbid: dict[str, list[Candidate]] = {}
        self.by_title: dict[str, list[Candidate]] = {}
        self.by_song: dict[str, list[Candidate]] = {}
        #: Per track: the names it goes by, its album exactly and written alike.
        self._people: dict[int, frozenset[str]] = {}
        self._album: dict[int, tuple[str, str]] = {}
        #: The albums by their album artist: ``{artist: {album exactly: {(disc, number): [tracks]}}}``.
        self._albums: dict[str, dict[str, dict[tuple[int, int], list[Candidate]]]] = {}
        #: The album found for an album of the entries under another name (``_renamed``), by what was asked.
        self._shapes: dict[tuple[str, str, tuple[tuple[int, int, int], ...]], str | None] = {}
        for one in candidates:
            self.by_id[one.id] = one
            if lasting(one.guid):
                self.by_guid.setdefault(one.guid, []).append(one)
            if one.mbid:
                self.by_mbid.setdefault(one.mbid, []).append(one)
            self.by_title.setdefault(title_key(one.title), []).append(one)
            if one.song_key and not one.song_key.startswith("#"):
                self.by_song.setdefault(one.song_key, []).append(one)
            self._people[one.id] = people_keys(one.album_artist, one.artist)
            exact = songs.norm(one.album)
            self._album[one.id] = (exact, album_key(one.album))
            place = _place(one.disc, one.number)
            owner = person_key(one.album_artist)
            if place is not None and owner and exact and owner not in songs.COMPILATIONS:
                self._albums.setdefault(owner, {}).setdefault(exact, {}).setdefault(place, []).append(one)

    def _renamed(self, wanted: Wanted, owner: str) -> str | None:
        """The album of ``owner`` that is the entry's album under another name: most of its places hold a track of
        the same length (the one with the most such places; None: none holds enough)."""
        shape = tuple(sorted({(disc or 1, number, duration) for disc, number, duration in wanted.shape
                              if number and number > 0}))
        asked = (owner, songs.norm(wanted.album), shape)
        if asked in self._shapes:
            return self._shapes[asked]
        best: tuple[int, str] | None = None
        need = max(SHAPE_LEAST, math.ceil(SHAPE_SHARE * len(shape)))
        for name, places in self._albums.get(owner, {}).items():
            if name == asked[1]:
                continue
            hits = sum(1 for disc, number, duration in shape
                       if any(abs(one.duration - duration) <= TOLERANCE for one in places.get((disc, number), ())))
            if hits >= need and (best is None or (hits, name) > best):
                best = (hits, name)
        found = best[1] if best is not None else None
        self._shapes[asked] = found
        return found

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
        title = title_key(wanted.title)
        named = free(self.by_title.get(title, ())) if title else []
        people = people_keys(wanted.artist, wanted.album_artist)
        by_them = [one for one in named if self._people[one.id] & people]
        exact, alike = songs.norm(wanted.album), album_key(wanted.album)
        for how, on_album in ((ALBUM, [one for one in by_them if exact and self._album[one.id][0] == exact]),
                              (EDITION, [one for one in by_them if alike and self._album[one.id][1] == alike])):
            if len(on_album) == 1:
                return Found(on_album[0], how)
            found = _closest(on_album, wanted.duration, TOLERANCE)
            if found is not None:
                return Found(found, how)
        found = _closest(by_them, wanted.duration, TOLERANCE)
        if found is not None:
            return Found(found, LENGTH)
        place = _place(wanted.disc, wanted.number)
        owner = person_key(wanted.album_artist or wanted.artist)
        if place is not None and wanted.shape and owner and owner in self._albums:
            name = self._renamed(wanted, owner)
            if name is not None:
                found = _closest(free(self._albums[owner][name].get(place, ())), wanted.duration, TOLERANCE)
                if found is not None:
                    return Found(found, RENAMED)
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


def assign(wanted: Sequence[Wanted], candidates: Iterable[Candidate] | Finder,
           taken: Iterable[int] = ()) -> list[Found | None]:
    """Each of ``wanted`` its track, in their order, never one track twice (and none of ``taken``). A sure finding
    comes before an unsure one: an entry found by its title alone never takes the track another entry is sure of."""
    finder = candidates if isinstance(candidates, Finder) else Finder(candidates)
    used = set(taken)
    found: list[Found | None] = [None] * len(wanted)
    later: list[int] = []
    for index, one in enumerate(wanted):
        hit = finder.find(one, used)
        if hit is not None and not hit.sure:
            later.append(index)
            continue
        if hit is not None:
            used.add(hit.track.id)
        found[index] = hit
    for index in later:
        hit = finder.find(wanted[index], used)
        if hit is not None:
            used.add(hit.track.id)
        found[index] = hit
    return found
