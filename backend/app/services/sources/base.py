"""What every connector to a media server offers (``plex.py``, ``jellyfin.py``): the shapes it answers in and what
it can do.

A connector speaks the server's own keys (text: Plex's ``ratingKey`` as digits, another server's ids as they are);
nexmix's numbers (``services/media.py``) are given by whoever calls it. It never holds a database session: what it
needs of the database (the address, the token, the library) it gets when it is opened.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Capabilities:
    """What a kind of server can do, for the interface and for the parts of nexmix that ask it."""

    #: Moods and styles on tracks, albums and artists (filters, "fill with a mood").
    moods: bool
    #: "Sounds alike" from an analysis of the sound (else that voice of the suggestions stays silent).
    sound: bool
    #: Playlists filled by the server's own rules, only read.
    smart_playlists: bool
    #: A track may stand in a playlist more than once (nexmix keeps it to once either way).
    duplicates: bool
    #: How changes of playlists made on the server reach nexmix: ``poll`` (by looking), ``events`` (told at once) or
    #: ``both`` (told of some, the rest by looking).
    changes: str
    #: nexmix keeps its playlists there (else a playlist with that source lives in nexmix only).
    playlists: bool = True
    #: How the server names an entry of a playlist: ``own`` (an id of its own, given in order as entries are added:
    #: Plex's ``playlistItemID``) or ``track`` (the track's id: Jellyfin's ``PlaylistItemId`` is the track's ``Id``).
    entries: str = "own"

    def view(self) -> dict[str, object]:
        return {"moods": self.moods, "sound": self.sound, "smart_playlists": self.smart_playlists,
                "duplicates": self.duplicates, "changes": self.changes, "playlists": self.playlists}


@dataclass(frozen=True)
class Listed:
    """A playlist as the server's list of playlists gives it."""

    key: str
    guid: str
    title: str
    smart: bool
    #: How many entries the list says it has (stale for a smart playlist of Plex).
    leaf_count: int
    #: When the server last changed it, whole seconds on its own clock (0: the server does not say).
    updated_at: int
    #: A mark that changes with every change of the playlist, its entries' order too (Jellyfin's ``Etag``; "": none).
    etag: str = ""
    #: When it was made, whole seconds on the server's clock (0: the server does not say).
    created: int = 0


@dataclass(frozen=True)
class Entry:
    """An entry of a playlist on the server: its id there (None in a smart playlist, and where the server names an
    entry by its track: ``Capabilities.entries``), the track's key, and what the server says of the track (for an entry
    whose track the catalogue does not know yet). ``ref`` is how the server's requests name the entry (``str(item)``,
    or the track's key)."""

    item: int | None
    track: str
    title: str
    artist: str
    album: str
    album_key: str
    year: int | None
    duration: int
    guid: str = ""
    ref: str = ""


#: The server's list of playlists answered 404 (a proxy in front of it, a changed base path): nothing there counts as
#: deleted. Also the code of a playlist sync while several playlists are missing from the list at once.
PLAYLISTS_NOT_FOUND = "playlists_not_found"


class NotSupported(Exception):
    """The server cannot do this (no analysis of the sound, for one)."""
