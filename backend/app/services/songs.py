"""One song, many tracks: the same recording on the album, a best-of and a reissue, the live and the karaoke version.

Pure functions, no database. The rules come from a library of some 52,000 tracks looked at by hand:

**The song key** is ``<core of the title>|<artist>``.

* The core of the title loses what is added in brackets, except what names another piece: ``Part``/``Pt.``/``Teil``/
  ``Nr.``/``No.``/``Op.``, ``Reprise``, ``Intro``, ``Interlude``, ``Outro`` and a bare number (arabic or roman). "Song
  (Intro)" is a twelve-second piece of its own, not "Song".
* It loses a guest at the end ("feat. ...", "ft. ..." with their dot, "featuring ..."; "A Feat of Clay" keeps its
  word), and whatever follows a spaced dash (`` - ``, `` ‐ ``, `` – ``, `` — ``), except, as in brackets, what names
  another piece: "Night Drive - Part 1" and "Night Drive - Part 2" are two.
* A general title ("Intro", "Skit", "Untitled" ...) gets a key of its own for each track: the intros of five albums
  are five pieces. So does a track without a title.
* The artist is the album's artist without a leading "the"; on a compilation ("Various Artists") the first name of
  the track's own artist. The same title by two artists stays two songs.

**The version shown** for a song is never a side version (instrumental, karaoke, a cappella, live, remix or mix,
demo, acoustic, edit, clean, take, sped up; nor anything on an album called karaoke or instrumental) while a main
version is there; then the earliest album year, then the number of listeners, then the plays. The number of listeners
alone does not choose: a karaoke version has nearly as many as the original.

**The original year** of a song is the earliest album year among its versions; Plex has no field for it.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

#: Titles that are no song of their own: every track that bears one is a piece of its own.
GENERIC = frozenset({
    "intro", "outro", "interlude", "skit", "prelude", "reprise", "untitled", "bonus track", "bonus", "hidden track",
})
#: Album artists that stand for "many": the track's own artist names the song there.
COMPILATIONS = frozenset({
    "various artists", "various", "va", "v a", "verschiedene interpreten", "verschiedene kunstler", "diverse",
    "diverse interpreten", "artistes divers", "varios artistas", "artisti vari", "compilation",
})

_BRACKET = re.compile(r"\s*[(\[]([^()\[\]]*)[)\]]")
_DASH = re.compile(r"\s+[-‐–—]\s+")
#: A guest outside brackets: "feat." and "ft." with their dot, "featuring" as a word. "A Feat of Clay" names no guest.
_GUEST = r"(?:feat\.|ft\.|featuring(?=\s))"
_FEAT = re.compile(r"\s+" + _GUEST + r".*\Z", re.IGNORECASE | re.DOTALL)
_ROMAN = r"(?:i{1,3}|iv|v|vi{1,3}|ix|x|xi{1,3}|xiv|xv|xx)"
#: What may stay in brackets: it names a piece of its own.
_KEEP = re.compile(
    r"^(?:(?:part|pt|teil|nr|no|op)(?:\s.*)?|reprise|intro|interlude|outro|[0-9]+|" + _ROMAN + r")$"
)
#: The words of a side version, in what a title adds (brackets, after a dash, "feat.").
_SIDE = re.compile(
    r"\b(?:instrumentals?|karaoke|a cappella|acappella|a capella|acapella|live|remix(?:es|ed)?|mix|demo|"
    r"acoustic|akustik|edit|clean|take|sped up|slowed)\b"
)
#: The same, for other modules (``match.similar``).
SIDE = _SIDE
#: An album that holds nothing but side versions.
_SIDE_ALBUM = re.compile(r"\b(?:karaoke|instrumentals?)\b")
#: Where a name list of performers is cut for the first name.
_PEOPLE = re.compile(r"\s+" + _GUEST + r"\s*|\s+(?:with|vs\.?|x)\s+|\s*[&,/;+]\s*", re.IGNORECASE)


def norm(text: str) -> str:
    """Lower case, accents gone, ß as ss, anything but letters and digits a single space."""
    folded = unicodedata.normalize("NFKD", str(text or "").lower().replace("ß", "ss"))
    plain = "".join(char for char in folded if not unicodedata.combining(char))
    return " ".join("".join(char if char.isalnum() else " " for char in plain).split())


@dataclass(frozen=True)
class Title:
    """A title taken apart: its core, and what was added to it (brackets, after a dash, "feat.")."""

    core: str
    added: str


def split_title(title: str) -> Title:
    text = str(title or "")
    added: list[str] = []
    kept: list[str] = []

    def bracket(match: re.Match[str]) -> str:
        inside = norm(match.group(1))
        if inside and _KEEP.match(inside):
            kept.append(inside)
        elif inside:
            added.append(inside)
        return " "

    rest = _BRACKET.sub(bracket, text)
    feat = _FEAT.search(rest)
    if feat:
        added.append(norm(feat.group(0)))
        rest = rest[: feat.start()]
    dash = _DASH.search(rest)
    if dash and rest[: dash.start()].strip():
        # After a dash as in brackets: "Night Drive - Part 1" is part 1, "Night Drive - Remastered" the song.
        for part in _DASH.split(rest[dash.end():]):
            piece = norm(part)
            if piece and _KEEP.match(piece):
                kept.append(piece)
            elif piece:
                added.append(piece)
        rest = rest[: dash.start()]
    core = " ".join(part for part in [norm(rest), *kept] if part)
    if not core:
        # A title that is nothing but brackets or signs: the whole of it.
        core = norm(text) or text.strip().casefold()
    return Title(core=core, added=" ".join(part for part in added if part))


def artist_part(album_artist: str, track_artist: str = "") -> str:
    """The artist of the key: the album's, without "the"; on a compilation the first name of the track's own."""
    name = norm(album_artist)
    if name in COMPILATIONS or not name:
        own = str(track_artist or "").strip()
        if own:
            name = norm(_PEOPLE.split(own, maxsplit=1)[0]) or norm(own)
    name = name.removeprefix("the ")
    return name


def first_name(performers: str) -> str:
    """The first of a list of performers ("A feat. B", "A & B"): A."""
    return _PEOPLE.split(str(performers or "").strip(), maxsplit=1)[0]


def song_key(track_id: int, title: str, album_artist: str, track_artist: str = "") -> str:
    core = split_title(title).core
    # A general title, and a track without any title: a piece of its own each.
    if not core or core in GENERIC:
        return f"#{int(track_id)}"
    return f"{core}|{artist_part(album_artist, track_artist)}"


def is_side_version(title: str, album_title: str = "") -> bool:
    """A version that is never shown while the song has a main one."""
    if _SIDE.search(split_title(title).added):
        return True
    return bool(_SIDE_ALBUM.search(norm(album_title)))


@dataclass(frozen=True)
class Version:
    id: int
    title: str
    album_title: str = ""
    album_artist: str = ""
    track_artist: str = ""
    year: int | None = None
    rating_count: int = 0
    view_count: int = 0


@dataclass(frozen=True)
class Song:
    key: str
    side: bool
    shown: bool
    original_year: int | None


def _order(version: Version, side: bool) -> tuple[int, int, int, int, int]:
    return (1 if side else 0, version.year if version.year else 99999, -version.rating_count, -version.view_count,
            version.id)


def songs(versions: Iterable[Version]) -> dict[int, Song]:
    """For every track: its song key, whether it is a side version, whether it is the version shown, and the song's
    original year."""
    groups: dict[str, list[tuple[Version, bool]]] = {}
    for version in versions:
        key = song_key(version.id, version.title, version.album_artist, version.track_artist)
        groups.setdefault(key, []).append((version, is_side_version(version.title, version.album_title)))
    found: dict[int, Song] = {}
    for key, members in groups.items():
        shown = min(members, key=lambda member: _order(*member))[0].id
        years = [version.year for version, _side in members if version.year]
        original = min(years) if years else None
        for version, side in members:
            found[version.id] = Song(key=key, side=side, shown=version.id == shown, original_year=original)
    return found
