"""A stand-in for plex.tv and a Plex Media Server, at the level of HTTP: the real client of nexmix talks to it through
``httpx.MockTransport`` (``FakePlex.handle``), or over a socket when ``serve.py`` runs it as a server for the
browser tests. Its answers follow the shape real Plex gives (``MediaContainer``, ``Metadata``, ``Directory``,
``totalSize``, the ``>>=`` filters, the many-at-once metadata, ``photo/:/transcode``); everything in it is made up.

The library (``library.py``) holds one of each case the song rules know: a remaster, a best-of, a live and a karaoke
version, a twelve-second intro next to the song, part 1 and part 2, "feat.", a spaced dash, a compilation, the same
title by another artist.

The playlists of the account and Plex's stream of events are in ``lists.py``.

Faults on demand: a wrong token (401), a busy server (503), a request that times out, a connection that breaks off,
a host that cannot be reached, each after a number of requests (``fail_after``).
"""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import formatdate
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from . import sound
from .library import Item, made_up_library
from .lists import PlaylistStore

ARTIST, ALBUM, TRACK = 8, 9, 10
PLEX_TV = "plex.tv"
SECTION = "3"
#: What the stand-in calls its server (machine id, name): made up.
MACHINE = "fake-machine-studio"
SHARED_MACHINE = "fake-machine-den"


def _token() -> str:
    import secrets

    return "fk" + secrets.token_hex(10)


@dataclass
class ServerSpec:
    """A server of the stand-in: which hosts reach it, its identity, its libraries."""

    machine: str
    name: str
    token: str
    hosts: tuple[str, ...]
    music: bool = True


@dataclass
class FakePlex:
    items: dict[int, Item] = field(default_factory=made_up_library)
    #: Plex's own clock, in seconds; the tests move it.
    now: int = 1_790_000_000
    #: The token plex.tv hands out at the end of the sign-in, and the one of the shared server.
    account_token: str = field(default_factory=_token)
    shared_token: str = field(default_factory=_token)
    #: Hosts that cannot be reached at all.
    down: set[str] = field(default_factory=set)
    #: After this many requests to a media server, every further one fails with ``fail_mode``.
    fail_after: int | None = None
    fail_mode: str = "503"
    #: The library's switch for the sonic analysis.
    analysis: bool | None = False
    #: Every request seen: ``(method, host, path, params)``.
    calls: list[tuple[str, str, str, dict[str, str]]] = field(default_factory=list)
    pins: dict[int, dict[str, Any]] = field(default_factory=dict)
    #: The sign-in at Plex agreed to (``approve``).
    approved: bool = False
    #: The addresses ``/resources`` gives for the own server (the serve mode puts its own there).
    connections: list[dict[str, Any]] = field(default_factory=lambda: [
        {"uri": "https://relay.example.com:8443", "local": False, "relay": True},
        {"uri": "https://plex-remote.example.com:32400", "local": False, "relay": False},
        {"uri": "http://plex-local.example.com:32400", "local": True, "relay": False},
        {"uri": "https://plex-local.example.com:32400", "local": True, "relay": False},
    ])
    #: The addresses of the shared server. Over a socket (``serve.py``) one that needs no name lookup.
    shared_connections: list[dict[str, Any]] = field(default_factory=lambda: [
        {"uri": "https://den.example.com:32400", "local": False, "relay": False},
    ])
    library_requests: int = 0
    #: Called with the number of each request to the media server, before it is answered: a test acts mid-run.
    hook: Callable[[int, str], None] | None = None
    #: The converter (``sound.py``): its answer, an own stream of it, what started and stopped; ``nearest`` by hand.
    sound_bytes: int = 3_000_000
    sound_status: int = 200
    sound_stream: Callable[[], Any] | None = None
    #: Answers of the converter for the next starts, before the ones above: ``(status, body)`` or an error to raise.
    sound_queue: list[Any] = field(default_factory=list)
    starts: list[dict[str, str]] = field(default_factory=list)
    stops: list[str] = field(default_factory=list)
    near: dict[int, list[tuple[int, float]]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        #: The playlists of the account (``lists.py``).
        self.lists = PlaylistStore(self)
        self.servers = [
            ServerSpec(MACHINE, "Studio", self.account_token,
                       ("plex-local.example.com", "plex-remote.example.com", "relay.example.com", "127.0.0.1",
                        "localhost")),
            ServerSpec(SHARED_MACHINE, "Den", self.shared_token, ("den.example.com",), music=False),
        ]

    # --- Changing the library, as Plex would ------------------------------------------------------------------

    def tick(self, seconds: int = 1) -> int:
        self.now += seconds
        return self.now

    def touch(self, key: int, **values: Any) -> None:
        item = self.items[key]
        for name, value in values.items():
            setattr(item, name, value)
        item.updated = self.tick()

    def add(self, item: Item) -> None:
        item.added = item.updated = self.tick()
        self.items[item.key] = item

    def remove(self, key: int) -> None:
        self.items.pop(key, None)
        for child in [one.key for one in self.items.values() if one.parent == key]:
            self.remove(child)

    def approve(self) -> None:
        self.approved = True

    def library_calls(self) -> list[tuple[str, str, dict[str, str]]]:
        return [(method, path, params) for method, host, path, params in self.calls if host != PLEX_TV]

    # --- HTTP ---------------------------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path
        params = dict(parse_qsl(request.url.query.decode("ascii") if isinstance(request.url.query, bytes)
                                else str(request.url.query), keep_blank_values=True))
        with self.lock:
            self.calls.append((request.method, host, path, params))
        if host in self.down:
            raise httpx.ConnectError("unreachable", request=request)
        if host == PLEX_TV or path.startswith("/api/v2/"):
            return self._plex_tv(request, path, params)
        server = next((one for one in self.servers if host in one.hosts), None)
        if server is None:
            raise httpx.ConnectError("unknown host", request=request)
        answer = self._media(request, server, path, params)
        # Every answer of a media server says the time on its own clock, as an HTTP date.
        answer.headers["Date"] = formatdate(self.now, usegmt=True)
        return answer

    def _media(self, request: httpx.Request, server: ServerSpec, path: str, params: dict[str, str]) -> httpx.Response:
        if path == "/identity":
            return _json({"MediaContainer": {"machineIdentifier": server.machine, "size": 0}})
        if request.headers.get("x-plex-token") != server.token:
            return httpx.Response(401, text="<html><body>401 Unauthorized</body></html>",
                                  headers={"content-type": "text/html"})
        with self.lock:
            self.library_requests += 1
            count = self.library_requests
        if self.hook is not None:
            self.hook(count, path)
        if self.fail_after is not None and count > self.fail_after:
            return self._fail(request)
        # A changed playlist shows its new ``updatedAt`` only a moment later.
        self.lists.request_seen()
        if not server.music and path.startswith("/library/sections") and path != "/library/sections":
            return httpx.Response(404, text="<html>404</html>", headers={"content-type": "text/html"})
        return self._server(request, server, path, params)

    def _fail(self, request: httpx.Request) -> httpx.Response:
        if self.fail_mode == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if self.fail_mode == "drop":
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)
        if self.fail_mode == "401":
            return httpx.Response(401, text="<html>401</html>", headers={"content-type": "text/html"})
        return httpx.Response(503, text="<html>503</html>", headers={"content-type": "text/html"})

    # --- plex.tv ------------------------------------------------------------------------------------------

    def _plex_tv(self, request: httpx.Request, path: str, params: dict[str, str]) -> httpx.Response:
        if path == "/api/v2/pins" and request.method == "POST":
            number = 4000 + len(self.pins) + 1
            self.pins[number] = {"code": f"code{number}"}
            return _json({"id": number, "code": f"code{number}", "authToken": None}, status=201)
        if path.startswith("/api/v2/pins/"):
            number = int(path.rsplit("/", 1)[-1])
            pin = self.pins.get(number)
            if pin is None or params.get("code") != pin["code"]:
                return _json({"errors": [{"code": 1020}]}, status=404)
            return _json({"id": number, "code": pin["code"], "authToken": self.account_token if self.approved else None})
        if path == "/api/v2/resources":
            if request.headers.get("x-plex-token") != self.account_token:
                return _json({"errors": [{"code": 1001}]}, status=401)
            return _json([
                {"name": "Studio", "product": "Plex Media Server", "provides": "server", "owned": True,
                 "clientIdentifier": MACHINE, "accessToken": self.account_token, "connections": self.connections},
                {"name": "Den", "product": "Plex Media Server", "provides": "server", "owned": False,
                 "clientIdentifier": SHARED_MACHINE, "accessToken": self.shared_token,
                 "connections": self.shared_connections},
                {"name": "Phone", "product": "Plexamp", "provides": "client,player", "owned": True,
                 "clientIdentifier": "fake-phone", "connections": []},
            ])
        return _json({"errors": [{"code": 404}]}, status=404)

    # --- The media server ---------------------------------------------------------------------------------

    def _server(self, request: httpx.Request, server: ServerSpec, path: str, params: dict[str, str]) -> httpx.Response:
        if path == "/":
            return _json({"MediaContainer": {"machineIdentifier": server.machine, "friendlyName": server.name,
                                             "version": "1.43.4.0000-fake"}})
        if path == "/library/sections":
            directories = [{"key": "1", "type": "movie", "title": "Films"}]
            if server.music:
                directories.append({"key": SECTION, "type": "artist", "title": "Music", "contentChangedAt": 77})
            return _json({"MediaContainer": {"size": len(directories), "Directory": directories}})
        if path == f"/library/sections/{SECTION}/prefs":
            settings = [{"id": "enableAutoPhotoTags", "value": False}]
            if self.analysis is not None:
                settings.append({"id": "musicAnalysis", "value": self.analysis, "type": "bool"})
            return _json({"MediaContainer": {"Setting": settings}})
        if path == f"/library/sections/{SECTION}/all":
            return self._list(params)
        if path in (f"/library/sections/{SECTION}/mood", f"/library/sections/{SECTION}/genre",
                    f"/library/sections/{SECTION}/style"):
            return self._values(path.rsplit("/", 1)[-1], int(params.get("type", TRACK)))
        if sound.handles(path):
            return sound.answer(self, path, params, request.headers)
        if path.startswith("/library/metadata/"):
            keys = path[len("/library/metadata/"):].split(",")
            if not all(key.isdigit() for key in keys):
                return httpx.Response(400, text="bad")
            found = [self.entry(self.items[int(key)], full=True) for key in keys if int(key) in self.items]
            if not found:
                return httpx.Response(404, text="<html>404</html>", headers={"content-type": "text/html"})
            return _json({"MediaContainer": {"size": len(found), "Metadata": found}})
        if path == "/photo/:/transcode":
            return self._photo(params)
        if path == "/:/eventsource/notifications":
            return self.lists.stream()
        found = self.lists.handle(request, path, params)
        if found is not None:
            return found
        return httpx.Response(404, text="<html>404</html>", headers={"content-type": "text/html"})

    def _list(self, params: dict[str, str]) -> httpx.Response:
        kind = int(params.get("type", TRACK))
        chosen = [item for item in self.items.values() if item.kind == kind]
        for name, value in params.items():
            if name in ("updatedAt>>", "updatedAt>"):
                chosen = [item for item in chosen if (item.updated or 0) > int(value)]
            elif name in ("addedAt>>", "addedAt>"):
                chosen = [item for item in chosen if item.added > int(value)]
            elif name == "mood":
                mood = self._value_name("mood", value)
                chosen = [item for item in chosen if mood in item.moods]
            elif name == "genre":
                genre = self._value_name("genre", value)
                chosen = [item for item in chosen if genre in item.genres]
        chosen.sort(key=lambda item: item.key)
        start = int(params.get("X-Plex-Container-Start", 0))
        size = int(params.get("X-Plex-Container-Size", 50))
        page = chosen[start:start + size]
        light = "excludeElements" in params
        return _json({"MediaContainer": {
            "size": len(page), "totalSize": len(chosen), "offset": start,
            "Metadata": [self.entry(item, light=light) for item in page],
        }})

    def _all_values(self, kind: str) -> list[str]:
        names: set[str] = set()
        for item in self.items.values():
            names.update(getattr(item, kind + "s"))
        return sorted(names)

    def _value_name(self, kind: str, key: str) -> str:
        values = self._all_values(kind)
        index = int(key) - 500 if key.isdigit() else -1
        return values[index] if 0 <= index < len(values) else ""

    def _values(self, kind: str, of_type: int) -> httpx.Response:
        values = self._all_values(kind)
        used = {name for item in self.items.values() if item.kind == of_type for name in getattr(item, kind + "s")}
        directories = [{"key": str(500 + index), "title": name, "fastKey": f"/library/sections/{SECTION}/all?{kind}="}
                       for index, name in enumerate(values) if name in used]
        return _json({"MediaContainer": {"size": len(directories), "Directory": directories}})

    def entry(self, item: Item, *, full: bool = False, light: bool = False) -> dict[str, Any]:
        """An entry as Plex writes it: a list leaves moods and styles out, the many-at-once answer has them."""
        out: dict[str, Any] = {"ratingKey": str(item.key), "key": f"/library/metadata/{item.key}/children",
                               "type": {ARTIST: "artist", ALBUM: "album", TRACK: "track"}[item.kind],
                               "title": item.title, "addedAt": item.added}
        if item.updated is not None:
            out["updatedAt"] = item.updated
        if item.kind == ARTIST:
            out["thumb"] = f"/library/metadata/{item.key}/thumb/{item.added}"
        if item.kind == ALBUM:
            artist = self.items.get(item.parent)
            out.update({"parentRatingKey": str(item.parent), "parentTitle": artist.title if artist else "",
                        "year": item.year, "thumb": f"/library/metadata/{item.key}/thumb/{item.added}"})
            if item.year:
                out["originallyAvailableAt"] = f"{item.year}-05-01"
        if item.kind == TRACK:
            if not light:
                out["guid"] = item.guid or f"plex://track/{item.key:024x}"
            album = self.items.get(item.parent)
            artist = self.items.get(album.parent) if album else None
            out.update({
                "parentRatingKey": str(item.parent), "grandparentRatingKey": str(album.parent if album else 0),
                "parentTitle": album.title if album else "", "grandparentTitle": artist.title if artist else "",
                "parentYear": album.year if album else None, "duration": item.duration, "index": item.index,
                "parentIndex": item.disc, "ratingCount": item.rating,
                "parentThumb": f"/library/metadata/{item.parent}/thumb/{album.added if album else 0}",
                "Media": [{"audioCodec": "flac", "Part": [{"key": f"/library/parts/{item.key}/file.flac"}]}],
            })
            if item.views:
                out["viewCount"] = item.views
            if item.original:
                out["originalTitle"] = item.original
            if full and item.analysed:
                out["musicAnalysisVersion"] = "1"
        if light:
            for name in ("thumb", "parentThumb", "key", "Media"):
                out.pop(name, None)
            return out
        if item.genres and item.kind != ARTIST:
            out["Genre"] = [{"tag": name} for name in item.genres]
        if full:
            if item.genres:
                out["Genre"] = [{"tag": name} for name in item.genres]
            if item.moods:
                out["Mood"] = [{"tag": name} for name in item.moods]
            if item.styles:
                out["Style"] = [{"tag": name} for name in item.styles]
        return out

    def _photo(self, params: dict[str, str]) -> httpx.Response:
        from PIL import Image

        size = int(params.get("width", "80"))
        key = int(params.get("url", "/library/metadata/0/thumb/0").split("/")[3])
        shade = (key * 37) % 200 + 30
        picture = Image.new("RGB", (size, size), (shade, 40, 230 - shade // 2))
        buffer = io.BytesIO()
        picture.save(buffer, "JPEG", quality=80)
        return httpx.Response(200, content=buffer.getvalue(), headers={"content-type": "image/jpeg"})


def _json(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(data).encode(), headers={"content-type": "application/json"})


def query_of(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
