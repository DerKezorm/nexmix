"""A stand-in Music Assistant 2.10.9 for the tests, with every way of its that was measured on a throwaway server
(``tools`` of the project, not in the repository): the greeting on the websocket, ``auth`` with errors 20 and 23, the
answers in parts, the tasks that end later, taking out by place (0 takes the last, -1 the one before, a place past the
end fails the whole task), tracks twice or unknown passed over without a word, a reading without ``force_refresh`` that
stays as it was after entries were taken out, ``music/playlists/remove`` that leaves the file behind (the playlist comes
back at the next scan), a new name that does not take, no events while it scans its providers, error 999 for anything
it does not expect, previews and pictures without signing in.

Its library is made up: by default the made-up music of the stand-in Plex (``fakeplex/library.py``) on a file provider,
"feat." taken out of the titles into the artists and a note in brackets into ``version``, as Music Assistant does, and a
few tracks of its own. Nothing here is real. The websocket is a ``Channel`` in memory (``channel``); ``serve.py`` puts
the same behind a real socket for the browser tests.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import queue
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from ..fakeplex.library import ALBUM, ARTIST, TRACK, made_up_library

FILES = "filesystem_local--a1b2c3d4"
OTHER_FILES = "filesystem_local--e5f6a7b8"
PLEX = "plex--q9r8s7t6"
BUILTIN = "builtin"
SCHEMA = 65
#: Lists longer than this come in parts (``partial``).
PART = 400
_VERSIONS = re.compile(r"\s*\((live[^()]*|acoustic|instrumental|[^()]*remaster[^()]*|radio edit|demo)\)\s*$",
                       re.IGNORECASE)
_FEAT = re.compile(r"\s*\(?\s*feat\.\s+([^()]+?)\)?\s*$", re.IGNORECASE)


def _hex(*parts: object) -> str:
    return hashlib.sha256("\0".join(str(part) for part in parts).encode()).hexdigest()


def make_token(days: float = 365.0, *, now: float | None = None) -> str:
    """A token of the shape Music Assistant gives (a JWT), made now, with ``exp`` ``days`` from now."""
    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    moment = now if now is not None else time.time()
    return ".".join([part({"alg": "HS256", "typ": "JWT"}),
                     part({"sub": secrets.token_hex(8), "exp": int(moment + days * 86400),
                           "jti": secrets.token_hex(8)}),
                     base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")])


@dataclass
class Album:
    id: int
    name: str
    artist: int
    year: int | None
    picture: str = ""
    version: str = ""


@dataclass
class Track:
    id: int
    name: str
    album: int
    artists: list[int]
    seconds: int
    number: int = 1
    disc: int = 1
    version: str = ""
    #: The provider mappings: ``(instance, item id there)``; the first is the one an entry is written with.
    mappings: list[tuple[str, str]] = field(default_factory=list)
    genres: list[int] = field(default_factory=list)
    recording: str = ""


@dataclass
class Playlist:
    id: int
    name: str
    #: The ``builtin`` id: the file's name (a second of the same name is ``<name> (1)``).
    file: str
    editable: bool = True
    dynamic: bool = False
    provider: str = BUILTIN
    #: The file's entries: ``(instance, item id there)``.
    entries: list[tuple[str, str]] = field(default_factory=list)
    #: What a reading without ``force_refresh`` gives (it does not follow entries taken out).
    cached: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Task:
    id: str
    name: str
    work: Any
    #: As measured: "pending" until it runs (it may wait behind other tasks), then how it ended.
    status: str = "pending"
    error: str | None = None
    #: How many more times ``tasks/get`` answers "running" before it runs.
    later: int = 0


class FakeChannel:
    """One websocket in memory: what nexmix sends goes to the stand-in at once, its answers and events wait here."""

    def __init__(self, fake: FakeMA, address: str) -> None:
        self.fake = fake
        self.address = address
        self.inbox: queue.Queue[str | None] = queue.Queue()
        self.closed = False
        self.authed = False

    def send(self, text: str) -> None:
        from app.services.music_assistant import Closed

        if self.closed:
            raise Closed
        self.fake.receive(self, text)

    def recv(self, timeout: float) -> str | None:
        from app.services.music_assistant import Closed

        if self.closed and self.inbox.empty():
            raise Closed
        try:
            found = self.inbox.get(timeout=min(timeout, 0.2))
        except queue.Empty:
            return None
        if found is None:
            self.closed = True
            raise Closed
        return found

    def push(self, message: dict[str, Any]) -> None:
        if not self.closed:
            self.inbox.put(json.dumps(message))

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.inbox.put(None)


class FakeMA:
    def __init__(self, *, twins: bool = True) -> None:
        self.lock = threading.RLock()
        self.server_id = _hex("fake-ma")[:32]
        self.name = "Music Assistant"
        self.status = "running"
        self.schema = SCHEMA
        self.min_schema = 28
        self.tokens: dict[str, bool] = {}
        self.token = self.make_token()
        self.providers: list[dict[str, Any]] = [
            {"type": "music", "domain": "filesystem_local", "instance_id": FILES, "name": "Musik", "available": True},
            {"type": "music", "domain": "filesystem_local", "instance_id": OTHER_FILES, "name": "Hörbuch-Ordner",
             "available": True},
            {"type": "music", "domain": "plex", "instance_id": PLEX, "name": "Plex", "available": True},
            {"type": "music", "domain": BUILTIN, "instance_id": BUILTIN, "name": "Music Assistant", "available": True},
            {"type": "metadata", "domain": "musicbrainz", "instance_id": "musicbrainz", "name": "MusicBrainz",
             "available": True},
        ]
        self.artists: dict[int, str] = {}
        self.albums: dict[int, Album] = {}
        self.tracks: dict[int, Track] = {}
        self.genres: dict[int, str] = {1: "Pop", 2: "Rock", 3: "Electronic", 4: "Jazz"}
        self.playlists: dict[int, Playlist] = {}
        self.tasks: dict[str, Task] = {}
        self.channels: list[FakeChannel] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.previews: dict[str, float] = {}
        self.preview_starts: list[int] = []
        self.pictures: set[str] = set()
        #: So many adding tasks to come take nothing and say "success".
        self.drop_adds = 0
        #: Tasks nexmix asked to run next (``tasks/run``).
        self.pushed: list[str] = []
        #: Every plain request seen: ``(path, params)``.
        self.http: list[tuple[str, dict[str, str]]] = []
        self._ids = itertools.count(1)
        self._playlist_ids = itertools.count(20)
        #: Unreachable: no websocket opens, ``/info`` fails (a restart, the network).
        self.down = False
        #: A command answered with this error code once: ``{command: code}``.
        self.fail_once: dict[str, int] = {}
        #: Commands after which the websocket breaks before the answer goes out (what was asked is done).
        self.break_after: set[str] = set()
        #: Runs right before a task does its work (somebody changes the playlist in Music Assistant meanwhile).
        self.before_task: Any = None
        #: How many times ``tasks/get`` says "running" before a task runs.
        self.task_later = 0
        #: Events are held back while Music Assistant scans its providers (``music/sync``).
        self.scanning = False
        #: Files of playlists removed with ``music/playlists/remove``: back at the next scan.
        self.left_files: list[Playlist] = []
        if twins:
            self._made_up()
        self._dynamic()

    # --- The library ----------------------------------------------------------------------------------------------

    def make_token(self, days: float = 365.0) -> str:
        token = make_token(days)
        self.tokens[token] = True
        return token

    def revoke(self, token: str) -> None:
        self.tokens[token] = False

    def _artist(self, name: str) -> int:
        for ident, known in self.artists.items():
            if known == name:
                return ident
        ident = next(self._ids) + 900
        self.artists[ident] = name
        return ident

    def add_track(self, artist: str, album: str, year: int | None, title: str, seconds: int, *, number: int = 1,
                  provider: str = FILES, genres: list[str] | None = None, recording: str = "",
                  picture: bool = True) -> int:
        """A track of a file of ``provider``: "feat." into the artists, a note in brackets into ``version``."""
        with self.lock:
            owner = self._artist(artist)
            album_id = next((one.id for one in self.albums.values() if one.name == album and one.artist == owner),
                            None)
            if album_id is None:
                album_id = next(self._ids) + 1900
                self.albums[album_id] = Album(album_id, album, owner, year, _hex("picture", album) if picture else "")
                if picture:
                    self.pictures.add(self.albums[album_id].picture)
            name, version = title, ""
            people = [owner]
            found = _FEAT.search(name)
            if found:
                people.append(self._artist(found.group(1).strip()))
                name = name[:found.start()].strip()
            found = _VERSIONS.search(name)
            if found:
                version = found.group(1)
                name = name[:found.start()].strip()
            ident = next(self._ids) + 2900
            path = f"{artist}/{album} ({year or 0})/{number:02d} {title}.flac"
            genre_ids = [next((key for key, value in self.genres.items() if value == one), 0) for one in genres or []]
            self.tracks[ident] = Track(ident, name, album_id, people, seconds, number, 1, version, [(provider, path)],
                                       [one for one in genre_ids if one], recording)
            return ident

    def _made_up(self) -> None:
        items = made_up_library()
        for _key, item in sorted(items.items()):
            if item.kind != TRACK:
                continue
            album = items[item.parent]
            artist = items[album.parent]
            self.add_track(artist.title, album.title, album.year, item.title, item.duration // 1000,
                           number=item.index, genres=[(album.genres or ["Pop"])[0]])
        assert all(one.kind in (ARTIST, ALBUM, TRACK) for one in items.values())
        # Of its own: one Plex has not, and one on a provider nexmix is not told to take.
        self.add_track("Lumen Vale", "Quiet Engines", 2022, "Small Hours", 233, number=1, genres=["Electronic"],
                       recording="0f1e2d3c-4b5a-4968-8776-655443322110")
        self.add_track("Erzählerin", "Kapitel Eins", 2021, "Anfang", 600, provider=OTHER_FILES, picture=False)

    def _dynamic(self) -> None:
        for name in ("Recently added tracks", "All favorited tracks"):
            ident = next(self._playlist_ids)
            self.playlists[ident] = Playlist(ident, name, name.lower().replace(" ", "_"), editable=False, dynamic=True)
        ident = next(self._playlist_ids)
        self.playlists[ident] = Playlist(ident, "500 Random tracks (from library)", "random_tracks", editable=False)

    def track_by_name(self, name: str) -> Track:
        return next(one for one in self.tracks.values() if one.name == name or self.title(one) == name)

    @staticmethod
    def title(track: Track) -> str:
        return f"{track.name} ({track.version})" if track.version else track.name

    def uri(self, track: Track) -> str:
        instance, item = track.mappings[0]
        return f"{instance}://track/{item}"

    def playlist_by_name(self, name: str) -> Playlist:
        return next(one for one in self.playlists.values() if one.name == name)

    def entries(self, playlist: Playlist | int) -> list[int]:
        """The library's tracks of a playlist's entries, in order (an entry outside the library left out)."""
        found = playlist if isinstance(playlist, Playlist) else self.playlists[playlist]
        return [track for track in (self._library_of(*one) for one in found.entries) if track is not None]

    def titles(self, playlist: Playlist | int) -> list[str]:
        return [self.title(self.tracks[one]) for one in self.entries(playlist)]

    def _library_of(self, instance: str, item: str) -> int | None:
        if instance == "library":
            return int(item) if item.isdigit() and int(item) in self.tracks else None
        for track in self.tracks.values():
            if (instance, item) in track.mappings:
                return track.id
        return None

    # --- From Music Assistant's own interface ---------------------------------------------------------------------

    def ui_add(self, playlist: int, track: int) -> None:
        with self.lock:
            entry = self.tracks[track].mappings[0]
            found = self.playlists[playlist]
            if entry not in found.entries:
                found.entries.append(entry)
                found.cached = list(found.entries)
            self._event("media_item_updated", f"library://playlist/{playlist}", self._playlist(found))

    def ui_remove(self, playlist: int, track: int) -> None:
        with self.lock:
            found = self.playlists[playlist]
            entry = self.tracks[track].mappings[0]
            if entry in found.entries:
                found.entries.remove(entry)
            self._event("media_item_updated", f"library://playlist/{playlist}", self._playlist(found))

    def ui_order(self, playlist: int, tracks: list[int]) -> None:
        with self.lock:
            found = self.playlists[playlist]
            found.entries = [self.tracks[one].mappings[0] for one in tracks]
            found.cached = list(found.entries)
            self._event("media_item_updated", f"library://playlist/{playlist}", self._playlist(found))

    def ui_create(self, name: str, tracks: list[int] | None = None) -> int:
        with self.lock:
            made = self._create(name)
            made.entries = [self.tracks[one].mappings[0] for one in tracks or []]
            made.cached = list(made.entries)
            return made.id

    def ui_delete(self, playlist: int) -> None:
        with self.lock:
            found = self.playlists.pop(playlist)
            self._event("media_item_deleted", f"library://playlist/{playlist}", self._playlist(found))

    def rename_file(self, track: int) -> int:
        """A file renamed on disk and scanned: a new library id (the old row gone), the playlists' entries name the old
        path and no longer reach the library."""
        with self.lock:
            old = self.tracks.pop(track)
            ident = next(self._ids) + 2900
            instance, path = old.mappings[0]
            self.tracks[ident] = Track(ident, old.name, old.album, old.artists, old.seconds, old.number, old.disc,
                                       old.version, [(instance, path.replace(".flac", " (neu).flac"))], old.genres,
                                       old.recording)
            return ident

    # --- The socket -----------------------------------------------------------------------------------------------

    def channel(self, address: str) -> FakeChannel:
        """A websocket opened by nexmix (``music_assistant.CHANNEL``); the greeting waits in it."""
        from app.services.music_assistant import Closed

        with self.lock:
            if self.down:
                raise Closed
            made = FakeChannel(self, address)
            self.channels.append(made)
            made.push(self._info())
            return made

    def restart(self) -> None:
        """Music Assistant stops: every websocket closes, nothing answers until ``up``."""
        with self.lock:
            self.down = True
            for one in self.channels:
                one.close()
            self.channels = []

    def up(self) -> None:
        with self.lock:
            self.down = False
            self.status = "running"

    def _info(self) -> dict[str, Any]:
        return {"server_id": self.server_id, "server_version": "2.10.9", "schema_version": self.schema,
                "min_supported_schema_version": self.min_schema, "base_url": "http://172.20.0.3:8095",
                "homeassistant_addon": False, "onboard_done": True, "name": self.name, "status": self.status,
                "internal_url": "http://172.20.0.3:8095", "external_url": None, "has_remote_access": False}

    def _event(self, kind: str, target: str | None, data: Any = None) -> None:
        if self.scanning and kind.startswith("media_item_"):
            return
        for one in list(self.channels):
            if one.authed:
                one.push({"event": kind, "object_id": target, "data": data})

    def receive(self, channel: FakeChannel, text: str) -> None:
        message = json.loads(text)
        ident, command = message.get("message_id"), str(message.get("command") or "")
        args = message.get("args") if isinstance(message.get("args"), dict) else {}
        with self.lock:
            self.calls.append((command, args))
            if command == "auth":
                token = str(args.get("token") or "")
                if self.tokens.get(token) is not True or not _alive(token):
                    channel.push({"message_id": ident, "error_code": 23,
                                  "details": "The access token is invalid or has expired."})
                    return
                channel.authed = True
                channel.push({"message_id": ident, "result": {"authenticated": True,
                                                              "user": {"user_id": "u1", "role": "admin"}},
                              "partial": False})
                return
            if not channel.authed:
                channel.push({"message_id": ident, "error_code": 20, "details": "Authentication is required."})
                return
            if command in self.fail_once:
                code = self.fail_once.pop(command)
                channel.push({"message_id": ident, "error_code": code, "details": "Something went wrong."})
                return
            handler = getattr(self, "_c_" + command.replace("/", "__"), None)
            if handler is None:
                channel.push({"message_id": ident, "error_code": 12, "details": "Invalid or unsupported command."})
                return
            try:
                result = handler(args)
            except _Error as exc:
                channel.push({"message_id": ident, "error_code": exc.code, "details": "Error."})
                return
            except (KeyError, TypeError, ValueError):
                channel.push({"message_id": ident, "error_code": 999, "details": "Value is invalid."})
                return
            if command in self.break_after:
                self.break_after.discard(command)
                channel.close()
                return
            if isinstance(result, list) and len(result) > PART:
                for start in range(0, len(result), PART):
                    part = result[start:start + PART]
                    channel.push({"message_id": ident, "result": part, "partial": start + PART < len(result)})
                return
            channel.push({"message_id": ident, "result": result, "partial": False})

    # --- What the commands give -----------------------------------------------------------------------------------

    def _track(self, track: Track, *, full: bool = False) -> dict[str, Any]:
        album = self.albums[track.album]
        found = {
            "item_id": str(track.id), "provider": "library", "name": track.name, "version": track.version,
            "sort_name": track.name.lower(), "uri": f"library://track/{track.id}", "external_ids": [],
            "is_playable": True, "media_type": "track", "duration": track.seconds, "track_number": track.number,
            "disc_number": track.disc, "favorite": False, "last_played": 0, "available": True,
            "provider_mappings": [{"item_id": item, "provider_domain": instance.split("--")[0],
                                   "provider_instance": instance, "available": True, "in_library": True,
                                   "details": "1794749126"} for instance, item in track.mappings],
            "metadata": {"images": [{"type": "thumb", "path": "cover.png", "provider": track.mappings[0][0],
                                     "remotely_accessible": False, "proxy_id": album.picture}]
                         if album.picture else []},
            "artists": [{"item_id": str(one), "provider": "library", "name": self.artists[one],
                         "uri": f"library://artist/{one}", "media_type": "artist"} for one in track.artists],
            "album": {"item_id": str(album.id), "provider": "library", "name": album.name,
                      "uri": f"library://album/{album.id}", "year": album.year,
                      **({"image": {"type": "thumb", "proxy_id": album.picture}} if album.picture else {})},
        }
        if full:
            found["external_ids"] = [["musicbrainz_recordingid", track.recording]] if track.recording else []
            found["metadata"]["genres"] = [self.genres[one] for one in track.genres]
        return found

    def _entry(self, playlist: Playlist, position: int, entry: tuple[str, str]) -> dict[str, Any]:
        instance, item = entry
        track = self._library_of(instance, item)
        if track is not None:
            known = self.tracks[track]
            found = self._track(known)
            found.update({"item_id": item, "provider": instance, "uri": f"{instance}://track/{item}",
                          "position": position, "track_number": 0, "disc_number": 0,
                          "album": {"item_id": item.rsplit("/", 1)[0], "provider": instance.split("--")[0],
                                    "name": self.albums[known.album].name, "year": None}})
            return found
        return {"item_id": item, "provider": instance, "name": item.rsplit("/", 1)[-1], "version": "",
                "uri": f"{instance}://track/{item}", "position": position, "duration": 100,
                "provider_mappings": [{"item_id": item, "provider_instance": instance}], "artists": [], "album": None}

    def _playlist(self, playlist: Playlist) -> dict[str, Any]:
        return {"item_id": str(playlist.id), "provider": "library", "name": playlist.name, "version": "",
                "uri": f"library://playlist/{playlist.id}", "media_type": "playlist", "external_ids": [],
                "provider_mappings": [{"item_id": playlist.file, "provider_domain": playlist.provider.split("--")[0],
                                       "provider_instance": playlist.provider, "available": True,
                                       "in_library": True}],
                "metadata": {}, "favorite": False, "owner": "Music Assistant", "is_editable": playlist.editable,
                "is_dynamic": playlist.dynamic, "available": True}

    @staticmethod
    def _page(entries: list[Any], args: dict[str, Any]) -> list[Any]:
        limit, offset = args.get("limit"), args.get("offset", 0)
        if limit is not None and not isinstance(limit, int):
            raise TypeError("limit")
        if not isinstance(offset, int):
            raise TypeError("offset")
        if not limit:
            return entries[offset:]
        return entries[offset:offset + limit]

    def _chosen(self, args: dict[str, Any]) -> set[str] | None:
        found = args.get("provider")
        if found is None:
            return None
        return {found} if isinstance(found, str) else {str(one) for one in found}

    def _c_info(self, _args: dict[str, Any]) -> Any:
        return self._info()

    def _c_providers(self, _args: dict[str, Any]) -> Any:
        return [dict(one, lookup_key=one["instance_id"], supported_features=[]) for one in self.providers]

    def _c_auth__tokens(self, _args: dict[str, Any]) -> Any:
        return [{"token_id": "t", "name": "nexmix", "is_long_lived": True, "expires_at": None}]

    def _c_music__tracks__count(self, _args: dict[str, Any]) -> Any:
        return len(self.tracks)

    def _c_music__albums__count(self, _args: dict[str, Any]) -> Any:
        return len(self.albums)

    def _c_music__playlists__count(self, _args: dict[str, Any]) -> Any:
        return len(self.playlists)

    def _c_music__tracks__library_items(self, args: dict[str, Any]) -> Any:
        chosen, genre = self._chosen(args), args.get("genre")
        found = [one for one in sorted(self.tracks.values(), key=lambda one: one.id)
                 if (chosen is None or any(instance in chosen for instance, _item in one.mappings))
                 and (genre is None or int(genre) in one.genres)]
        return [self._track(one) for one in self._page(found, args)]

    def _c_music__albums__library_items(self, args: dict[str, Any]) -> Any:
        chosen = self._chosen(args)
        found = [album for album in sorted(self.albums.values(), key=lambda one: one.id)
                 if chosen is None or any(track.album == album.id and any(i in chosen for i, _p in track.mappings)
                                          for track in self.tracks.values())]
        return [{"item_id": str(one.id), "provider": "library", "name": one.name, "version": one.version,
                 "year": one.year, "uri": f"library://album/{one.id}", "media_type": "album",
                 "artists": [{"item_id": str(one.artist), "name": self.artists[one.artist], "provider": "library"}],
                 "metadata": {"images": [{"type": "thumb", "proxy_id": one.picture}] if one.picture else []}}
                for one in self._page(found, args)]

    def _c_music__genres__library_items(self, args: dict[str, Any]) -> Any:
        return [{"item_id": str(key), "provider": "library", "name": name, "media_type": "genre",
                 "provider_mappings": []} for key, name in self._page(sorted(self.genres.items()), args)]

    def _c_music__tracks__get(self, args: dict[str, Any]) -> Any:
        if args.get("provider_instance_id_or_domain") != "library":
            raise _Error(2)
        track = self.tracks.get(int(args["item_id"]))
        if track is None:
            raise _Error(2)
        return self._track(track, full=True)

    def _c_music__get_library_item(self, args: dict[str, Any]) -> Any:
        if args.get("media_type") != "track":
            raise ValueError("media_type")
        track = self._library_of(str(args["provider_instance_id_or_domain"]), str(args["item_id"]))
        return self._track(self.tracks[track]) if track is not None else None

    def _c_music__tracks__preview(self, args: dict[str, Any]) -> Any:
        # As measured: asked by the library's id the sound breaks off; nexmix asks by the provider's mapping.
        if args.get("provider_instance_id_or_domain") == "library":
            raise _Error(999)
        found = self._library_of(str(args["provider_instance_id_or_domain"]), str(args["item_id"]))
        track = self.tracks.get(found) if found is not None else None
        if track is None:
            raise _Error(2)
        token = secrets.token_urlsafe(16)[:22]
        self.previews[token] = time.monotonic()
        self.preview_starts.append(track.id)
        return f"/preview?token={token}"

    def _c_music__playlists__library_items(self, args: dict[str, Any]) -> Any:
        found = [self._playlist(one) for one in sorted(self.playlists.values(), key=lambda one: one.id)]
        return self._page(found, args)

    def _c_music__playlists__get(self, args: dict[str, Any]) -> Any:
        found = self.playlists.get(int(args["item_id"]))
        if found is None:
            raise _Error(2)
        return self._playlist(found)

    def _create(self, name: str) -> Playlist:
        ident = next(self._playlist_ids)
        files = {one.file for one in self.playlists.values()}
        file = re.sub(r'[<>:"/\\|?*]', "_", name)
        if file in files:
            count = 1
            while f"{file} ({count})" in files:
                count += 1
            file = f"{file} ({count})"
        made = Playlist(ident, name, file)
        self.playlists[ident] = made
        self._event("media_item_added", f"library://playlist/{ident}", self._playlist(made))
        return made

    def _c_music__playlists__create_playlist(self, args: dict[str, Any]) -> Any:
        if args.get("provider_instance_or_domain") not in (None, BUILTIN):
            raise _Error(3)
        return self._playlist(self._create(str(args["name"])))

    def _task(self, name: str, work: Any) -> dict[str, Any]:
        made = Task(secrets.token_hex(16), name, work, later=self.task_later)
        self.tasks[made.id] = made
        return self._task_view(made)

    @staticmethod
    def _task_view(task: Task) -> dict[str, Any]:
        return {"name": task.name, "id": task.id, "status": task.status, "last_error": task.error,
                "progress": None, "metadata": {}}

    def run_tasks(self) -> None:
        """Every task still running does its work now (Music Assistant runs them one after another)."""
        with self.lock:
            for task in list(self.tasks.values()):
                if task.status == "pending":
                    self._run(task)

    def _run(self, task: Task) -> None:
        if self.before_task is not None:
            hook, self.before_task = self.before_task, None
            hook()
        try:
            task.status = task.work() or "success"
        except IndexError:
            task.status, task.error = "failed", "list assignment index out of range"
        self._event("tasks_updated", None, [self._task_view(one) for one in self.tasks.values()])

    def _c_tasks__run(self, args: dict[str, Any]) -> Any:
        task = self.tasks.get(str(args["task_id"]))
        if task is None:
            raise _Error(2)
        self.pushed.append(task.id)
        return self._task_view(task)

    def _c_tasks__cancel(self, args: dict[str, Any]) -> Any:
        task = self.tasks.get(str(args["task_id"]))
        if task is None:
            raise _Error(2)
        if task.status == "pending":
            task.status = "cancelled"
        return self._task_view(task)

    def _c_tasks__get(self, args: dict[str, Any]) -> Any:
        task = self.tasks.get(str(args["task_id"]))
        if task is None:
            raise _Error(2)
        if task.status == "pending":
            if task.later > 0:
                task.later -= 1
            else:
                self._run(task)
        return self._task_view(task)

    def _c_music__playlists__add_playlist_tracks(self, args: dict[str, Any]) -> Any:
        ident = int(args["db_playlist_id"])
        uris = [str(one) for one in args["uris"]]

        def work() -> str:
            found = self.playlists.get(ident)
            if found is None or not found.editable:
                return "failed"
            if self.drop_adds:
                # Says it went through and took nothing (as a provider's URI it did not take, measured once).
                self.drop_adds -= 1
                return "success"
            unknown = False
            for uri in uris:
                entry = self._entry_of(uri)
                if entry is None:
                    unknown = True
                    continue
                if entry in found.entries:
                    continue
                found.entries.append(entry)
            found.cached = list(found.entries)
            self._event("media_item_updated", f"library://playlist/{ident}", self._playlist(found))
            return "partial_success" if unknown else "success"

        return self._task(f"Add items to playlist {ident}", work)

    def _entry_of(self, uri: str) -> tuple[str, str] | None:
        found = re.fullmatch(r"([^:]+)://track/(.+)", uri)
        if found is None:
            return None
        instance, item = found.group(1), found.group(2)
        if instance == "library":
            track = self.tracks.get(int(item)) if item.isdigit() else None
            return track.mappings[0] if track is not None else None
        return (instance, item) if self._library_of(instance, item) is not None or instance in (FILES, OTHER_FILES,
                                                                                                PLEX) else None

    def _c_music__playlists__remove_playlist_tracks(self, args: dict[str, Any]) -> Any:
        ident = int(args["db_playlist_id"])
        places = [int(one) for one in args["positions_to_remove"]]

        def work() -> str:
            found = self.playlists.get(ident)
            if found is None:
                return "failed"
            entries = list(found.entries)
            # As Music Assistant: ``liste[position - 1]`` without a check, the places sorted from the last.
            for place in sorted(places, reverse=True):
                del entries[place - 1]
            found.entries = entries
            # The reading without ``force_refresh`` does not follow (``cached`` stays).
            self._event("media_item_updated", f"library://playlist/{ident}", self._playlist(found))
            return "success"

        return self._task(f"Remove items from playlist {ident}", work)

    def _c_music__playlists__playlist_tracks(self, args: dict[str, Any]) -> Any:
        found = self.playlists.get(int(args["item_id"]))
        if found is None:
            raise _Error(2)
        if args.get("force_refresh") is True:
            found.cached = list(found.entries)
        return [self._entry(found, place, entry) for place, entry in enumerate(found.cached, 1)]

    def _c_music__playlists__update(self, args: dict[str, Any]) -> Any:
        found = self.playlists.get(int(args["item_id"]))
        if found is None:
            raise _Error(2)
        answer = self._playlist(found)
        answer["name"] = str((args.get("update") or {}).get("name") or found.name)
        self._event("media_item_updated", f"library://playlist/{found.id}", self._playlist(found))
        self._event("media_item_updated", f"library://playlist/{found.id}", self._playlist(found))
        return answer

    def _c_music__playlists__remove(self, args: dict[str, Any]) -> Any:
        found = self.playlists.pop(int(args["item_id"]), None)
        if found is None:
            raise _Error(2)
        self.left_files.append(found)
        self._event("media_item_deleted", f"library://playlist/{found.id}", self._playlist(found))
        return None

    def _c_music__library__remove_item(self, args: dict[str, Any]) -> Any:
        if args.get("media_type") != "playlist":
            raise ValueError("media_type")
        found = self.playlists.pop(int(args["library_item_id"]), None)
        if found is None:
            raise _Error(2)
        self._event("media_item_deleted", f"library://playlist/{found.id}", self._playlist(found))
        return None

    def _c_music__sync(self, _args: dict[str, Any]) -> Any:
        self.scan()
        return []

    def scan(self) -> None:
        """Music Assistant scans its providers: files of removed playlists come back under new ids; no event for
        single items, then ``music_sync_completed``."""
        with self.lock:
            self.scanning = True
            for left in self.left_files:
                ident = next(self._playlist_ids)
                self.playlists[ident] = Playlist(ident, left.name, left.file, entries=list(left.entries),
                                                 cached=list(left.entries))
            self.left_files = []
            self.scanning = False
            self._event("providers_updated", None)
            self._event("music_sync_completed", None)

    # --- Plain requests -------------------------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            if self.down:
                raise httpx.ConnectError("down", request=request)
            path = urlsplit(str(request.url)).path
            params = {key: values[0] for key, values in parse_qs(urlsplit(str(request.url)).query).items()}
            self.http.append((path, params))
            if path == "/info":
                return httpx.Response(200, json=self._info())
            if path == "/preview":
                made = self.previews.get(params.get("token", ""))
                if made is None or time.monotonic() - made > 60:
                    return httpx.Response(404, text="Not found")
                # A few frames of ADTS (AAC), made up.
                return httpx.Response(200, content=b"\xff\xf1\x50\x80" + b"\x00" * 4000,
                                      headers={"content-type": "audio/aac"})
            if path.startswith("/imageproxy/"):
                ident = path.rsplit("/", 1)[1]
                if not re.fullmatch(r"[0-9a-f]{64}", ident):
                    return httpx.Response(400, text="Invalid image id")
                size = params.get("size", "0")
                if size not in ("0", "80", "160", "256", "512", "1024"):
                    return httpx.Response(400, text=f"Unsupported size {size}")
                if ident not in self.pictures:
                    return httpx.Response(404, text="Not found")
                return httpx.Response(200, content=_jpeg(int(size)), headers={"content-type": "image/jpeg",
                                                                              "cache-control": "max-age=31536000"})
            return httpx.Response(404, text="Not found")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


class _Error(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


def _alive(token: str) -> bool:
    try:
        claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        return int(claims.get("exp", 0)) > time.time()
    except (ValueError, IndexError, TypeError):
        return False


def _jpeg(size: int) -> bytes:
    from io import BytesIO

    from PIL import Image

    out = BytesIO()
    Image.new("RGB", (max(size, 1), max(size, 1)), (120, 80, 200)).save(out, "JPEG")
    return out.getvalue()
