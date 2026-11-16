"""The playlists of the stand-in Jellyfin, with the quirks a real Jellyfin 12.4 showed (measured on a throwaway server):

* A playlist belongs to a user and is public unless made with ``IsPublic: false``. The list of playlists as a user
  has his own and everybody's public ones; ``/Playlists/<id>/Users`` answers 200 to the owner (and to whom it is
  shared with for changing), 403 to anybody else and 400 to an API key.
* ``PlaylistItemId`` is the track's ``Id``. A track may stand twice; taking out by its id takes out every copy, moving
  moves the first one.
* Adding takes the ids in the address only (``?ids=``); ids in a JSON body are passed over with 204, nothing added.
  An address longer than 8 KiB answers 414. Unknown ids are passed over.
* Taking out and moving an id it does not know: 204, nothing changed. ``Move/<n>`` puts the entry at place ``n`` of the
  list afterwards; ``n`` past the end answers 500.
* Every change gives the playlist a new ``Etag``; taking out gives it one even when nothing was taken out (measured).
* Its entries asked without a user answer 200 while it is there, 404 once it is deleted; with an API key and no user
  400; another user's private playlist 404, whoever asks.
* A file renamed, moved or deleted drops its track from every playlist without a word; a library made anew empties
  them all and keeps the ids.

Everything in it is made up.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from . import FakeJellyfin

#: Kestrel's limit for the request line.
URL_LIMIT = 8192
ERROR = "Error processing request."


@dataclass
class List:
    id: str
    name: str
    owner: str
    public: bool = True
    entries: list[str] = field(default_factory=list)
    #: Users it is shared with: ``{user: can_edit}``.
    shares: dict[str, bool] = field(default_factory=dict)
    etag: str = field(default_factory=lambda: secrets.token_hex(16))
    #: When it was made, as Jellyfin writes it (``DateCreated``).
    created: str = ""

    def changed(self) -> None:
        self.etag = secrets.token_hex(16)


class Playlists:
    """The part of ``FakeJellyfin`` that keeps playlists; ``self`` is the stand-in."""

    lists: dict[str, List]
    #: Every write to a playlist: ``(method, path, params)``.
    list_writes: list[tuple[str, str, dict[str, str]]]
    #: So many writes to playlists from now on fail as if Jellyfin went away in the middle (after doing them, when
    #: ``after`` is set: the answer is lost).
    fail_writes: int
    fail_after: bool

    def _init_playlists(self) -> None:
        self.lists = {}
        self.list_writes = []
        self.fail_writes = 0
        self.fail_after = False

    # --- What somebody does in Jellyfin -------------------------------------------------------------------------

    def make_list(self: FakeJellyfin, name: str, tracks: list[str] | None = None, *, user: str = "Erika",
                  public: bool = True) -> str:
        owner = next(one.id for one in self.users if one.name == user)
        ident = secrets.token_hex(16)
        self.lists[ident] = List(ident, name, owner, public, list(tracks or []), created=self._made_at())
        return ident

    def _made_at(self: FakeJellyfin) -> str:
        return self.now.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")

    def list_add(self, ident: str, tracks: list[str]) -> None:
        self.lists[ident].entries.extend(tracks)
        self.lists[ident].changed()

    def list_remove(self, ident: str, tracks: list[str]) -> None:
        gone = set(tracks)
        self.lists[ident].entries = [one for one in self.lists[ident].entries if one not in gone]
        self.lists[ident].changed()

    def list_move(self, ident: str, track: str, place: int) -> None:
        entries = self.lists[ident].entries
        entries.remove(track)
        entries.insert(place, track)
        self.lists[ident].changed()

    def list_rename(self, ident: str, name: str) -> None:
        self.lists[ident].name = name
        self.lists[ident].changed()

    def list_delete(self, ident: str) -> None:
        del self.lists[ident]

    def tracks_of(self, ident: str) -> list[str]:
        return list(self.lists[ident].entries)

    def drop_from_lists(self: FakeJellyfin, track: str) -> None:
        """A track left the library: it leaves every playlist, without a word."""
        for one in self.lists.values():
            if track in one.entries:
                one.entries = [entry for entry in one.entries if entry != track]
                one.changed()

    def library_made_anew(self) -> None:
        """The library removed and added again: the ids stay, every playlist is empty."""
        for one in self.lists.values():
            if one.entries:
                one.entries = []
                one.changed()

    # --- HTTP ---------------------------------------------------------------------------------------------------

    def _visible(self, one: List, user: str | None) -> bool:
        return user is None or one.public or one.owner == user or user in one.shares

    def _may_change(self: FakeJellyfin, one: List, user: str | None) -> bool:
        if user is None:
            return True
        return one.owner == user or one.shares.get(user, False)

    def _playlist_entry(self: FakeJellyfin, one: List) -> dict[str, Any]:
        kind = "Audio" if one.entries else "Unknown"
        entry = {"Name": one.name, "ServerId": self.server_id, "Id": one.id, "Etag": one.etag, "IsFolder": True,
                 "Type": "Playlist", "ChildCount": len(one.entries), "MediaType": kind, "LocationType": "FileSystem"}
        if one.created:
            entry["DateCreated"] = one.created
        return entry

    def _listing(self, params: dict[str, str], user: str | None) -> list[dict[str, Any]]:
        viewer = params.get("userId") or user
        return [self._playlist_entry(one) for one in sorted(self.lists.values(), key=lambda one: one.name)
                if self._visible(one, viewer)]

    def _by_ids(self: FakeJellyfin, params: dict[str, str]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for ident in params.get("Ids", "").split(","):
            if ident in self.tracks:
                found.append(self._track(self.tracks[ident], params))
            elif ident in self.lists:
                found.append(self._playlist_entry(self.lists[ident]))
            elif ident in self.albums:
                found.append(self._album(self.albums[ident]))
        return found

    def _list_write(self: FakeJellyfin, method: str, path: str, params: dict[str, str],
                    run: Any) -> httpx.Response:
        """A write to a playlist; while ``fail_writes`` lasts, Jellyfin is away (before or after doing it)."""
        self.list_writes.append((method, path, params))
        if self.fail_writes > 0:
            self.fail_writes -= 1
            if self.fail_after:
                run()
            raise httpx.ConnectError("gone in the middle")
        return run()

    def _playlists(self: FakeJellyfin, request: httpx.Request, method: str, path: str, params: dict[str, str],
                   user: str | None) -> httpx.Response:
        if len(str(request.url)) > URL_LIMIT:
            return self._answer(414, content=b"")
        parts = path.strip("/").split("/")
        if method == "DELETE" and len(parts) == 2 and parts[0] == "Items":
            one = self.lists.get(parts[1])
            if one is None:
                return self._answer(404, text=ERROR)
            if user is not None and one.owner != user:
                return self._answer(401, text=ERROR)

            def drop() -> httpx.Response:
                del self.lists[parts[1]]
                return self._answer(204)

            return self._list_write(method, path, params, drop)
        if parts == ["Playlists"] and method == "POST":
            return self._create(request, user, params, path)
        if len(parts) < 2 or parts[0] != "Playlists":
            return self._answer(404, text=ERROR)
        one = self.lists.get(parts[1])
        if one is None:
            return self._answer(404, text=ERROR)
        rest = parts[2:]
        if not rest and method == "GET":
            if user is None:
                return self._answer(400, text=ERROR)
            return self._answer(200, {"OpenAccess": one.public, "ItemIds": list(one.entries),
                                      "Shares": [{"UserId": who, "CanEdit": edit} for who, edit in one.shares.items()]})
        if not rest and method == "POST":
            if not self._may_change(one, user):
                return self._answer(403, text=ERROR)
            try:
                body = json.loads(request.content or b"{}")
            except ValueError:
                return self._answer(400, text=ERROR)

            def update() -> httpx.Response:
                if isinstance(body.get("Name"), str):
                    one.name = body["Name"]
                if isinstance(body.get("IsPublic"), bool):
                    one.public = body["IsPublic"]
                one.changed()
                return self._answer(204)

            return self._list_write(method, path, params, update)
        if rest == ["Users"] and method == "GET":
            if user is None:
                return self._answer(400, text=ERROR)
            if one.owner != user and not one.shares.get(user, False):
                return self._answer(403, text=ERROR)
            return self._answer(200, [{"UserId": who, "CanEdit": edit} for who, edit in one.shares.items()])
        if rest == ["Items"] and method == "GET":
            viewer = params.get("userId") or user
            if viewer is None:
                # An API key has no user: without one named, 400 (measured).
                return self._answer(400, text=ERROR)
            if not self._visible(one, viewer):
                # Another user's private playlist: 404, as if it were not there (measured, an administrator too).
                return self._answer(404, text=ERROR)
            entries = [self._track(self.tracks[ident], params) for ident in one.entries if ident in self.tracks]
            for entry in entries:
                entry["PlaylistItemId"] = entry["Id"]
            limit = params.get("Limit")
            page = entries if limit is None else entries[:int(limit)]
            return self._answer(200, {"Items": page, "TotalRecordCount": len(entries), "StartIndex": 0})
        if rest == ["Items"] and method == "POST":
            if not self._may_change(one, user):
                return self._answer(403, text=ERROR)
            # A body is passed over: only the address counts.
            wanted = [ident for ident in params.get("ids", params.get("Ids", "")).split(",") if ident in self.tracks]

            def add() -> httpx.Response:
                if wanted:
                    one.entries.extend(wanted)
                    one.changed()
                return self._answer(204)

            return self._list_write(method, path, params, add)
        if rest == ["Items"] and method == "DELETE":
            if not self._may_change(one, user):
                return self._answer(403, text=ERROR)
            gone = set(params.get("entryIds", "").split(","))

            def remove() -> httpx.Response:
                one.entries = [entry for entry in one.entries if entry not in gone]
                # A new Etag even when none of the ids was in it (measured).
                one.changed()
                return self._answer(204)

            return self._list_write(method, path, params, remove)
        if len(rest) == 4 and rest[0] == "Items" and rest[2] == "Move" and method == "POST":
            if not self._may_change(one, user):
                return self._answer(403, text=ERROR)
            track, place = rest[1], int(rest[3])

            def move() -> httpx.Response:
                if track not in one.entries:
                    return self._answer(204)
                if place > len(one.entries):
                    return self._answer(500, text=ERROR)
                one.entries.remove(track)
                one.entries.insert(min(place, len(one.entries)), track)
                one.changed()
                return self._answer(204)

            return self._list_write(method, path, params, move)
        return self._answer(404, text=ERROR)

    def _create(self: FakeJellyfin, request: httpx.Request, user: str | None, params: dict[str, str],
                path: str) -> httpx.Response:
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            return self._answer(400, text=ERROR)
        owner = body.get("UserId") or user
        if owner is None:
            return self._answer(400, text=ERROR)
        if user is not None and owner != user and not any(one.id == user and one.admin for one in self.users):
            return self._answer(403, text=ERROR)
        ident = secrets.token_hex(16)

        def make() -> httpx.Response:
            self.lists[ident] = List(ident, str(body.get("Name") or ""), owner, bool(body.get("IsPublic", True)),
                                     [one for one in body.get("Ids") or [] if one in self.tracks],
                                     created=self._made_at())
            return self._answer(200, {"Id": ident})

        return self._list_write("POST", path, params, make)
