"""The playlists of the Plex account, at the level of single requests (``services/plex_sync.py`` puts them together).

What they rely on, as Plex Media Server 1.43 answered:

* ``GET /playlists?playlistType=audio`` lists them with ``ratingKey``, ``guid``, ``title``, ``smart``, ``leafCount``
  and ``updatedAt`` (whole seconds on Plex's clock, set 0.1 to 0.7 seconds after a change; ``leafCount`` of a smart
  playlist is stale, its ``totalSize`` is not). Entries added or taken out change ``updatedAt`` there, a new name
  changes ``title`` only, entries moved change nothing in the list.
* ``GET /playlists/<key>/items`` gives the entries in order, each a track with its ``playlistItemID``; at once, right
  after a change. A smart playlist's entries have no ``playlistItemID``.
* ``POST /playlists?type=audio&smart=0&title=..&uri=server://<machine>/com.plexapp.plugins.library`` makes an empty one
  (without ``uri`` Plex says 400). ``PUT /playlists/<key>/items?uri=../library/metadata/<k1,k2,..>`` adds at the end,
  in that order, and answers ``leafCountAdded`` and ``leafCountRequested``: a track already in it is left out without
  a word. Taken out and put in again, an entry gets a new ``playlistItemID``.
* ``DELETE /playlists/<key>/items/<item>``; ``PUT /playlists/<key>/items/<item>/move`` without ``after`` to the start,
  with ``after=<item>`` right after it. ``after=0`` is not the start, and an ``after`` that is gone puts the entry after
  the first one, answering 200: a place is only ever an item of the state just read. An item that is gone: 404.
* ``PUT /playlists/<key>?title=..`` answers with an empty body; ``DELETE /playlists/<key>`` with 204.
* ``GET /playlists/<key>`` sets the playlist's ``updatedAt`` to now (a smart one's too): never asked (``meta``).

Every key and id that goes into an address is a number checked here; the machine id of the server only letters,
digits and dashes.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .plex import PlexError, PlexServer

#: Tracks per request when adding: the address stays under 3,000 characters and Plex answers within 0.15 s.
CHUNK = 300
MACHINE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")
LIBRARY = "com.plexapp.plugins.library"
#: The entries of a smart playlist nexmix shows at most.
SMART_LIMIT = 5000


def _int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number < 10**12 else None


def _key(value: int) -> int:
    number = _int(value)
    if number is None:
        raise PlexError("bad_path", 400)
    return number


def _machine(machine: str) -> str:
    if not MACHINE.fullmatch(machine or ""):
        raise PlexError("plex_error")
    return machine


def uri(machine: str, tracks: Sequence[int] = ()) -> str:
    """The address of tracks of the server, as Plex wants it for a playlist (none: the library itself)."""
    base = f"server://{_machine(machine)}/{LIBRARY}"
    if not tracks:
        return base
    return f"{base}/library/metadata/{','.join(str(_key(track)) for track in tracks)}"


@dataclass(frozen=True)
class Listed:
    """A playlist as the list of Plex gives it."""

    rating_key: int
    guid: str
    title: str
    smart: bool
    leaf_count: int
    updated_at: int


@dataclass(frozen=True)
class Item:
    """An entry of a playlist in Plex: its id there (None in a smart playlist), the track, and what Plex says of the
    track (for an entry whose track the catalogue does not know yet)."""

    item: int | None
    track: int
    title: str
    artist: str
    album: str
    album_id: int
    year: int | None
    duration: int
    #: The track's ``guid`` in Plex.
    guid: str = ""


def _listed(entry: dict[str, Any]) -> Listed | None:
    key = _int(entry.get("ratingKey"))
    if key is None or str(entry.get("playlistType") or "audio") != "audio":
        return None
    return Listed(
        rating_key=key, guid=str(entry.get("guid") or "")[:200], title=str(entry.get("title") or "").strip()[:200],
        smart=bool(entry.get("smart")) and str(entry.get("smart")).lower() not in ("0", "false"),
        leaf_count=_int(entry.get("leafCount")) or 0, updated_at=_int(entry.get("updatedAt")) or 0,
    )


def listing(server: PlexServer) -> list[Listed]:
    container = server.get("/playlists", {"playlistType": "audio"}, timeout=30.0)
    found = []
    for entry in container.get("Metadata") or []:
        if isinstance(entry, dict) and str(entry.get("type") or "playlist") == "playlist":
            one = _listed(entry)
            if one is not None:
                found.append(one)
    return found


def meta(server: PlexServer, rating_key: int) -> Listed:
    """One playlist, taken from the list of them; ``PlexError("plex_not_found")`` when Plex has it no longer.

    Never ``GET /playlists/<key>``: asking for the playlist itself sets its ``updatedAt`` anew (measured on Plex 1.43,
    smart and ordinary ones alike), and a sync that asked so would find it changed at every look. The list and the
    entries leave it as it is. A playlist missing from the list is gone when its entries answer 404 too."""
    key = _key(rating_key)
    for one in listing(server):
        if one.rating_key == key:
            return one
    container = server.get(f"/playlists/{key}/items", start=0, size=0, timeout=30.0)
    return Listed(rating_key=key, guid="", title=str(container.get("title") or "").strip()[:200],
                  smart=bool(container.get("smart")) and str(container.get("smart")).lower() not in ("0", "false"),
                  leaf_count=_int(container.get("leafCount")) or 0, updated_at=0)


def _item(entry: dict[str, Any], smart: bool) -> Item | None:
    track = _int(entry.get("ratingKey"))
    item = _int(entry.get("playlistItemID"))
    if track is None or (item is None and not smart) or str(entry.get("type") or "track") != "track":
        return None
    own = str(entry.get("originalTitle") or "").strip()
    return Item(
        item=item, track=track, title=str(entry.get("title") or "").strip()[:500],
        artist=(own or str(entry.get("grandparentTitle") or "").strip())[:500],
        album=str(entry.get("parentTitle") or "").strip()[:500], album_id=_int(entry.get("parentRatingKey")) or 0,
        year=_int(entry.get("parentYear")), duration=max(0, _int(entry.get("duration")) or 0),
        guid=str(entry.get("guid") or "")[:200],
    )


def items(server: PlexServer, rating_key: int) -> list[Item]:
    """Every entry in order, in one answer (pages could each see another state of a list being changed)."""
    container = server.get(f"/playlists/{_key(rating_key)}/items", timeout=120.0)
    found: list[Item] = []
    seen: set[int] = set()
    for entry in container.get("Metadata") or []:
        if isinstance(entry, dict):
            one = _item(entry, smart=False)
            if one is not None and one.track not in seen:
                seen.add(one.track)
                found.append(one)
    return found


def smart_items(server: PlexServer, rating_key: int, limit: int | None = None) -> tuple[list[Item], int]:
    """The first ``limit`` entries of a smart playlist (``SMART_LIMIT``) and how many it has in all (``totalSize``)."""
    limit = SMART_LIMIT if limit is None else limit
    path = f"/playlists/{_key(rating_key)}/items"
    total = _int(server.get(path, start=0, size=0, timeout=60.0).get("totalSize")) or 0
    container = server.get(path, start=0, size=limit, timeout=120.0) if total else {}
    found: list[Item] = []
    seen: set[int] = set()
    for entry in container.get("Metadata") or []:
        if isinstance(entry, dict):
            one = _item(entry, smart=True)
            if one is not None and one.track not in seen:
                seen.add(one.track)
                found.append(one)
    return found[:limit], max(total, len(found))


def _container(answer: Any) -> dict[str, Any]:
    try:
        payload = answer.json()
    except ValueError:
        return {}
    container = payload.get("MediaContainer") if isinstance(payload, dict) else None
    return container if isinstance(container, dict) else {}


def create(server: PlexServer, machine: str, title: str) -> Listed:
    """An empty playlist named ``title``."""
    answer = server._send("POST", "/playlists",
                          {"type": "audio", "title": title[:200], "smart": 0, "uri": uri(machine)}, timeout=30.0)
    for entry in _container(answer).get("Metadata") or []:
        if isinstance(entry, dict):
            one = _listed(entry)
            if one is not None:
                return one
    raise PlexError("plex_error")


def add(server: PlexServer, machine: str, rating_key: int, tracks: Sequence[int],
        answered: Callable[[list[int], int | None], None] | None = None) -> int:
    """The tracks at the end, in their order; how many Plex took (one already in it is left out without a word).
    ``answered``: called after every part Plex answered, with the part and how many of it Plex says it took."""
    added = 0
    for start in range(0, len(tracks), CHUNK):
        part = list(tracks[start:start + CHUNK])
        answer = server._send("PUT", f"/playlists/{_key(rating_key)}/items", {"uri": uri(machine, part)},
                              timeout=60.0)
        container = _container(answer)
        took = _int(container.get("leafCountAdded"))
        added += took if took is not None else 0
        if answered is not None:
            answered(part, took)
    return added


def remove(server: PlexServer, rating_key: int, item: int) -> bool:
    """False: the entry was gone already."""
    try:
        server._send("DELETE", f"/playlists/{_key(rating_key)}/items/{_key(item)}", timeout=30.0)
    except PlexError as exc:
        if exc.code == "plex_not_found":
            return False
        raise
    return True


def move(server: PlexServer, rating_key: int, item: int, after: int | None) -> bool:
    """The entry right after ``after`` (an item of the state just read), None: to the start (never ``after=0``).
    False: the entry was gone."""
    params = {} if after is None else {"after": _key(after)}
    try:
        server._send("PUT", f"/playlists/{_key(rating_key)}/items/{_key(item)}/move", params, timeout=30.0)
    except PlexError as exc:
        if exc.code == "plex_not_found":
            return False
        raise
    return True


def rename(server: PlexServer, rating_key: int, title: str) -> None:
    server._send("PUT", f"/playlists/{_key(rating_key)}", {"title": title[:200]}, timeout=30.0)


def delete(server: PlexServer, rating_key: int) -> bool:
    """False: Plex had it no longer."""
    try:
        server._send("DELETE", f"/playlists/{_key(rating_key)}", timeout=30.0)
    except PlexError as exc:
        if exc.code == "plex_not_found":
            return False
        raise
    return True


def existing(server: PlexServer, tracks: Sequence[int]) -> set[int]:
    """Which of the tracks the library of Plex still has (asked a hundred at a time)."""
    found: set[int] = set()
    chosen = sorted({_key(track) for track in tracks})
    for start in range(0, len(chosen), 100):
        try:
            entries = server.bulk(chosen[start:start + 100])
        except PlexError as exc:
            if exc.code != "plex_not_found":
                raise
            entries = []
        for entry in entries:
            key = _int(entry.get("ratingKey"))
            if key is not None and str(entry.get("type") or "track") == "track":
                found.add(key)
    return found
