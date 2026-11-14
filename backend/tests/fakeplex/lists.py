"""The playlists of the stand-in Plex, with every quirk measured on a real server (Plex Media Server 1.43):

* a track is in a playlist at most once: a second time is left out without a word (``leafCountAdded`` 0);
* ``updatedAt`` is whole seconds of Plex's clock, and set only a moment after the change (here: after ``lag`` more
  requests to the server), while the entries are right at once; it changes when entries are added or taken out, not
  when they are moved, and not when the playlist is renamed (its ``title`` does);
* ``move`` without ``after`` goes to the start, ``after=0`` and an ``after`` that is gone put the entry after the first
  one and answer 200; an item that is gone answers 404 (``text/html``, 85 bytes);
* deleting answers 204, renaming an empty body, making one without ``uri`` 400;
* a smart playlist has no ``playlistItemID`` and a stale ``leafCount`` in the list; ``totalSize`` is right;
* the stream of events tells of a playlist made (state 0), renamed (3, then 5) and deleted (9), nothing of entries;
* asking for a playlist itself (``GET /playlists/<key>``) sets its ``updatedAt`` anew; the list and the entries do not.

Tests change a playlist the way Plexamp would through the same methods (``add``, ``remove``, ``move``, ``rename``,
``delete``, ``make``).
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from . import FakePlex

TRACK = 10
NOT_FOUND = "<html><head><title>Not Found</title></head><body><h1>404 Not Found</h1></body></html>"[:85]


@dataclass
class FakeList:
    key: int
    title: str
    smart: bool = False
    #: ``(playlistItemID, track)`` in order; a smart playlist's entries come from ``rule``.
    items: list[tuple[int, int]] = field(default_factory=list)
    rule: list[int] = field(default_factory=list)
    updated_at: int = 0
    added_at: int = 0
    guid: str = field(default_factory=lambda: f"com.plexapp.agents.none://{uuid.uuid4()}")
    summary: str = ""
    #: What a smart playlist's ``leafCount`` says in the list: stale on purpose.
    stale_count: int = 0

    def tracks(self) -> list[int]:
        return list(self.rule) if self.smart else [track for _item, track in self.items]


@dataclass
class PlaylistStore:
    fake: FakePlex
    lists: dict[int, FakeList] = field(default_factory=dict)
    next_key: int = 90_001
    next_item: int = 70_001
    #: Requests to the server until a changed ``updatedAt`` shows.
    lag: int = 1
    #: ``[requests left, key, stamp]``.
    pending: list[list[int]] = field(default_factory=list)
    #: Events of the stream not yet sent: ``(event, data)``.
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)
    #: Every write of nexmix: ``(method, path, params)``.
    writes: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    #: How often a playlist itself was asked for (which sets its ``updatedAt``).
    meta_reads: int = 0
    #: Tracks Plex leaves out when they are added, without a word, though its library has them.
    refuse: set[int] = field(default_factory=set)

    # --- Changing, as Plex does it (and as Plexamp would ask it) -------------------------------------------------

    def _touched(self, playlist: FakeList) -> None:
        self.pending.append([self.lag, playlist.key, self.fake.now])

    def _event(self, playlist: FakeList, state: int, meta: str | None) -> None:
        entry: dict[str, Any] = {"identifier": "com.plexapp.plugins.library", "itemID": str(playlist.key),
                                 "sectionID": "-1", "state": state, "title": playlist.title, "type": 15,
                                 "updatedAt": self.fake.now}
        if meta:
            entry["metadataState"] = meta
        self.events.append(("timeline", {"TimelineEntry": entry}))

    def settle(self) -> None:
        """Every ``updatedAt`` still to come is there now."""
        with self.lock:
            for _left, key, stamp in self.pending:
                if key in self.lists:
                    self.lists[key].updated_at = stamp
            self.pending.clear()

    def request_seen(self) -> None:
        with self.lock:
            for one in self.pending:
                one[0] -= 1
            for _left, key, stamp in [one for one in self.pending if one[0] < 0]:
                if key in self.lists:
                    self.lists[key].updated_at = stamp
            self.pending = [one for one in self.pending if one[0] >= 0]

    def make(self, title: str, tracks: list[int] | None = None, *, smart: bool = False) -> FakeList:
        with self.lock:
            playlist = FakeList(key=self.next_key, title=title, smart=smart, updated_at=self.fake.now,
                                added_at=self.fake.now)
            self.next_key += 1
            self.lists[playlist.key] = playlist
            if smart:
                playlist.rule = list(tracks or [])
                playlist.stale_count = max(0, len(playlist.rule) - 3)
            else:
                self.add(playlist.key, tracks or [], quiet=True)
            self._event(playlist, 0, "created")
            return playlist

    def add(self, key: int, tracks: list[int], *, quiet: bool = False) -> tuple[int, int]:
        """At the end, in order; a track already there (or twice in the request), or not in the library, is left
        out. Returns (added, requested)."""
        with self.lock:
            playlist = self.lists[key]
            there = {track for _item, track in playlist.items}
            added = 0
            for track in tracks:
                found = self.fake.items.get(track)
                if track in there or found is None or found.kind != TRACK or track in self.refuse:
                    continue
                playlist.items.append((self.next_item, track))
                self.next_item += 1
                there.add(track)
                added += 1
            if not quiet:
                self._touched(playlist)
            return added, len(tracks)

    def remove(self, key: int, item: int) -> bool:
        with self.lock:
            playlist = self.lists[key]
            kept = [one for one in playlist.items if one[0] != item]
            if len(kept) == len(playlist.items):
                return False
            playlist.items = kept
            self._touched(playlist)
            return True

    def remove_track(self, key: int, track: int) -> bool:
        item = next((one for one, found in self.lists[key].items if found == track), None)
        return item is not None and self.remove(key, item)

    def move(self, key: int, item: int, after: int | None) -> bool:
        """Plex's move: no ``after`` to the start; ``after`` 0 or gone: after the first entry (and still 200)."""
        with self.lock:
            playlist = self.lists[key]
            entry = next((one for one in playlist.items if one[0] == item), None)
            if entry is None:
                return False
            rest = [one for one in playlist.items if one[0] != item]
            if after is None:
                at = 0
            else:
                place = next((number for number, one in enumerate(rest) if one[0] == after), None)
                at = place + 1 if place is not None else min(1, len(rest))
            rest.insert(at, entry)
            playlist.items = rest
            # Moved: the list of playlists shows nothing of it.
            return True

    def move_track(self, key: int, track: int, after_track: int | None) -> bool:
        items = self.lists[key].items
        item = next(one for one, found in items if found == track)
        after = None if after_track is None else next(one for one, found in items if found == after_track)
        return self.move(key, item, after)

    def rename(self, key: int, title: str) -> None:
        with self.lock:
            playlist = self.lists[key]
            # Renamed: the title in the list changes, its updatedAt does not.
            playlist.title = title
            self._event(playlist, 3, "loading")
            self._event(playlist, 5, None)

    def delete(self, key: int) -> bool:
        with self.lock:
            playlist = self.lists.pop(key, None)
            if playlist is None:
                return False
            self._event(playlist, 9, "deleted")
            return True

    def tracks(self, key: int) -> list[int]:
        return self.lists[key].tracks()

    def by_title(self, title: str) -> FakeList | None:
        return next((one for one in self.lists.values() if one.title == title), None)

    # --- HTTP --------------------------------------------------------------------------------------------------

    def _meta(self, playlist: FakeList) -> dict[str, Any]:
        tracks = playlist.tracks()
        duration = sum(self.fake.items[track].duration for track in tracks if track in self.fake.items)
        return {
            "ratingKey": str(playlist.key), "key": f"/playlists/{playlist.key}/items", "guid": playlist.guid,
            "type": "playlist", "title": playlist.title, "titleSort": playlist.title, "summary": playlist.summary,
            "smart": playlist.smart, "playlistType": "audio", "composite": f"/playlists/{playlist.key}/composite/1",
            "icon": "playlist://image.smart" if playlist.smart else "", "viewCount": 0, "duration": duration,
            "leafCount": playlist.stale_count if playlist.smart else len(tracks), "addedAt": playlist.added_at,
            "updatedAt": playlist.updated_at,
        }

    def _entry(self, playlist: FakeList, item: int | None, track: int) -> dict[str, Any] | None:
        found = self.fake.items.get(track)
        if found is None:
            return None
        entry = self.fake.entry(found)
        if item is not None:
            entry["playlistItemID"] = item
        return entry

    def handle(self, request: httpx.Request, path: str, params: dict[str, str]) -> httpx.Response | None:
        method = request.method
        parts = [part for part in path.split("/") if part]
        if not parts or parts[0] != "playlists":
            return None
        with self.lock:
            if method != "GET":
                self.writes.append((method, path, dict(params)))
            if parts == ["playlists"]:
                if method == "GET":
                    chosen = [self._meta(one) for one in self.lists.values()
                              if params.get("playlistType", "audio") == "audio"]
                    return _json({"MediaContainer": {"size": len(chosen), "Metadata": chosen}})
                if method == "POST":
                    return self._create(params)
                return _missing()
            if not parts[1].isdigit():
                return httpx.Response(400, text="bad")
            playlist = self.lists.get(int(parts[1]))
            if playlist is None:
                return _missing()
            if len(parts) == 2:
                if method == "GET":
                    # Asking for the playlist itself sets its updatedAt anew (measured, smart ones too).
                    answer = _json({"MediaContainer": {"size": 1, "Metadata": [self._meta(playlist)]}})
                    self._touched(playlist)
                    self.meta_reads += 1
                    return answer
                if method == "PUT":
                    if "title" in params:
                        self.rename(playlist.key, params["title"])
                    if "summary" in params:
                        playlist.summary = params["summary"]
                        self._touched(playlist)
                    return httpx.Response(200, content=b"")
                if method == "DELETE":
                    self.delete(playlist.key)
                    return httpx.Response(204, content=b"")
                return _missing()
            if parts[2] != "items":
                return _missing()
            if len(parts) == 3:
                if method == "GET":
                    return self._items(playlist, params)
                if method == "PUT":
                    tracks = _tracks_of(params.get("uri", ""), self.fake)
                    if tracks is None:
                        return httpx.Response(400, text="bad")
                    if playlist.smart:
                        return httpx.Response(400, text="bad")
                    added, requested = self.add(playlist.key, tracks)
                    return _json({"MediaContainer": {"leafCountAdded": added, "leafCountRequested": requested,
                                                     "size": 1, "Metadata": [self._meta(playlist)]}})
                return _missing()
            if not parts[3].isdigit():
                return _missing()
            item = int(parts[3])
            if len(parts) == 4 and method == "DELETE":
                if not self.remove(playlist.key, item):
                    return _missing()
                return _json({"MediaContainer": {"size": 1, "Metadata": [self._meta(playlist)]}})
            if len(parts) == 5 and parts[4] == "move" and method == "PUT":
                after_text = params.get("after")
                after = int(after_text) if after_text not in (None, "") and after_text.isdigit() else None
                if after_text is not None and after is None:
                    return httpx.Response(400, text="bad")
                if not self.move(playlist.key, item, after):
                    return _missing()
                return _json({"MediaContainer": {"size": 1, "Metadata": [self._meta(playlist)]}})
            return _missing()

    def _create(self, params: dict[str, str]) -> httpx.Response:
        if "uri" not in params or params.get("type") != "audio":
            return httpx.Response(400, text="<html><body>400 Bad Request</body></html>",
                                  headers={"content-type": "text/html"})
        tracks = _tracks_of(params["uri"], self.fake)
        if tracks is None:
            return httpx.Response(400, text="bad")
        playlist = self.make(params.get("title", ""), tracks)
        return _json({"MediaContainer": {"size": 1, "Metadata": [self._meta(playlist)]}})

    def _items(self, playlist: FakeList, params: dict[str, str]) -> httpx.Response:
        if playlist.smart:
            pairs: list[tuple[int | None, int]] = [(None, track) for track in playlist.rule]
        else:
            pairs = [(item, track) for item, track in playlist.items]
        entries = [one for one in (self._entry(playlist, item, track) for item, track in pairs) if one is not None]
        container: dict[str, Any] = {"title": playlist.title, "smart": playlist.smart,
                                     "leafCount": playlist.stale_count if playlist.smart else len(entries)}
        if "X-Plex-Container-Size" in params:
            start = int(params.get("X-Plex-Container-Start", 0))
            size = int(params["X-Plex-Container-Size"])
            container["totalSize"] = len(entries)
            entries = entries[start:start + size]
        container.update({"size": len(entries), "Metadata": entries})
        return _json({"MediaContainer": container})

    def stream(self) -> httpx.Response:
        """The stream of events: what is waiting, then a ping, then the end (the client connects again)."""
        with self.lock:
            waiting, self.events = self.events, []
        body = "".join(f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in waiting)
        body += 'event: ping\ndata: {"PingEntry": {}}\n\n'
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def _tracks_of(text: str, fake: FakePlex) -> list[int] | None:
    # The server with the music (a test may make it another one: another machine id, other keys).
    prefix = f"server://{fake.servers[0].machine}/com.plexapp.plugins.library"
    if not text.startswith(prefix):
        return None
    rest = text[len(prefix):]
    if rest in ("", "/"):
        return []
    marker = "/library/metadata/"
    if not rest.startswith(marker):
        return None
    keys = rest[len(marker):].split(",")
    if not all(key.isdigit() for key in keys):
        return None
    return [int(key) for key in keys]


def _json(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(data).encode(), headers={"content-type": "application/json"})


def _missing() -> httpx.Response:
    return httpx.Response(404, text=NOT_FOUND, headers={"content-type": "text/html"})
