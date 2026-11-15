"""The made-up music of the stand-in Plex (``fakeplex/library.py``) in the stand-in Jellyfin as well: each track on an album
of the same name by the same artist, at the same place and of the same length, but some left out, so that a playlist
of one server finds most of its tracks on the other, a few nowhere. For the tests of mirrors, copying and moving."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..fakeplex.library import TRACK, Item
from . import Album, Track, make_id

if TYPE_CHECKING:
    from . import FakeJellyfin

#: Plex's track the browser tests leave out of Jellyfin ("Op. 3 (No. 2)" by Dax Orlo).
LEFT_OUT = frozenset({3021})


def twin(jellyfin: FakeJellyfin, plex_items: dict[int, Item], leave_out: frozenset[int] = frozenset(),
         retitled: dict[int, str] | None = None) -> dict[int, str]:
    """Jellyfin gets the tracks of Plex's library but ``leave_out`` (Plex's keys); ``retitled``: a title written
    otherwise. Returns Plex's key → Jellyfin's id."""
    found: dict[int, str] = {}
    for key, item in sorted(plex_items.items()):
        if item.kind != TRACK or key in leave_out:
            continue
        album = plex_items[item.parent]
        artist = plex_items[album.parent]
        album_id = make_id("album", artist.title, album.title)
        if album_id not in jellyfin.albums:
            jellyfin.albums[album_id] = Album(album_id, album.title, artist.title, album.year, list(album.genres))
        title = (retitled or {}).get(key, item.title)
        track = Track(f"/music/{artist.title}/{album.title}/{item.index:02d} {title}.flac", title, album_id,
                      [item.original or artist.title], item.duration // 1000, album.year, list(album.genres),
                      number=item.index, disc=item.disc)
        jellyfin.tracks[track.id] = track
        found[key] = track.id
    return found


def untwin(jellyfin: FakeJellyfin, twins: dict[int, str]) -> None:
    """The twins gone again (their albums too, where nothing else is on them)."""
    gone = set(twins.values())
    albums = {jellyfin.tracks[ident].album for ident in gone if ident in jellyfin.tracks}
    for ident in gone:
        jellyfin.tracks.pop(ident, None)
    for album in albums:
        if not any(one.album == album for one in jellyfin.tracks.values()):
            jellyfin.albums.pop(album, None)
