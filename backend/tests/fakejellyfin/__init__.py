"""A stand-in Jellyfin 12.4 at the level of HTTP: the real client of nexmix talks to it through ``httpx.MockTransport``
(``FakeJellyfin.handle``), or over a socket when ``serve.py`` runs it for the browser tests. Its answers follow the shapes
a real server gave (see the measurement of Jellyfin for nexmix 0.2); everything in it is made up.

What it does as Jellyfin does:

* Only ``Authorization: MediaBrowser …, Token="…"`` or ``?ApiKey=`` signs in; ``X-Emby-Token`` and ``?api_key=`` are
  refused with 401. Quick Connect and the sign-in need ``Client``, ``Device``, ``DeviceId`` and ``Version``.
* **One device id holds one token**: a new sign-in with the same device id makes the token before invalid (401).
* Quick Connect: ``Initiate`` gives a six-digit code and a secret; ``Connect`` says whether it was confirmed; the code
  runs out after ten minutes (``now`` moves, 404); the secret may be redeemed more than once until then.
* An API key has no user: ``/Users/Me`` answers 400.
* Ids of tracks hang on the path of the file: ``rename`` gives the track a new id, and the old one is gone.
* ``MinDateLastSaved`` filters what was saved since; ``MinDateCreated`` is ignored. ``Limit=0`` gives the total.
* Sound (``/Audio/<id>/stream.mp3``) and pictures (``/Items/<album>/Images/Primary``) answer **without** a token.
* While it starts (``starting``: so many requests) everything answers 503.
* A ``userId`` it does not know (the user deleted): ``/Items`` answers 404, a playlist's entries 500 (measured).
* Behind a proxy whose base path changed (``moved_away``) everything answers 404.
* Playlists as ``playlists.py`` says.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from ..fakeplex import sound
from .playlists import Playlists

#: The made-up server's own id.
SERVER_ID = hashlib.md5(b"stand-in jellyfin").hexdigest()
LIBRARY_ID = hashlib.md5(b"stand-in jellyfin music").hexdigest()
OTHER_LIBRARY_ID = hashlib.md5(b"stand-in jellyfin films").hexdigest()
QUICK_SECONDS = 600
TICKS = 10_000_000
ERROR = "Error processing request."


def make_id(*parts: str) -> str:
    return hashlib.md5("\n".join(parts).encode()).hexdigest()


def _secret(length: int = 32) -> str:
    return secrets.token_hex(length)


@dataclass
class Album:
    id: str
    name: str
    artist: str
    year: int | None
    genres: list[str]
    picture: bool = True
    mbid: str = ""
    created: datetime = field(default_factory=lambda: datetime(2026, 1, 5, tzinfo=UTC))
    saved: datetime = field(default_factory=lambda: datetime(2026, 1, 5, tzinfo=UTC))


@dataclass
class Track:
    path: str
    name: str
    album: str
    artists: list[str]
    seconds: int
    year: int | None
    genres: list[str]
    number: int = 1
    disc: int = 1
    mbid_track: str = ""
    mbid_recording: str = ""
    plays: int = 0
    created: datetime = field(default_factory=lambda: datetime(2026, 1, 5, tzinfo=UTC))
    saved: datetime = field(default_factory=lambda: datetime(2026, 1, 5, tzinfo=UTC))

    @property
    def id(self) -> str:
        return make_id("audio", self.path)


@dataclass
class User:
    id: str
    name: str
    admin: bool


def _mb(*parts: str) -> str:
    raw = make_id("mb", *parts)
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def made_up_library() -> tuple[dict[str, Album], dict[str, Track]]:
    """Seven made-up artists on nine albums: an album and its remaster, a live version, a compilation of various
    artists, "feat." in the title and among the artists, an album without a picture, MusicBrainz ids on some tracks."""
    albums: dict[str, Album] = {}
    tracks: dict[str, Track] = {}

    def album(name: str, artist: str, year: int | None, genres: list[str], *, picture: bool = True) -> str:
        ident = make_id("album", artist, name)
        albums[ident] = Album(ident, name, artist, year, genres, picture)
        return ident

    def track(album_id: str, name: str, seconds: int, number: int, *, artists: list[str] | None = None,
              mb: bool = False) -> None:
        owner = albums[album_id]
        path = f"/music/{owner.artist}/{owner.name}/{number:02d} {name}.flac"
        found = Track(path, name, album_id, artists or [owner.artist], seconds, owner.year, list(owner.genres),
                      number=number, mbid_track=_mb("t", path) if mb else "", mbid_recording=_mb("r", name) if mb else "")
        tracks[found.id] = found

    wolken = album("Wolkenfabrik", "Venrarik", 1986, ["Pop"])
    track(wolken, "Echo Feuer", 95, 1, mb=True)
    track(wolken, "Kupferstrand", 214, 2, mb=True)
    track(wolken, "Leise Lichter", 187, 3)
    track(wolken, "Nachtzug (feat. Kafin)", 240, 4, artists=["Venrarik feat. Kafin"])
    remaster = album("Wolkenfabrik (Remastered)", "Venrarik", 2016, ["Pop"])
    track(remaster, "Echo Feuer", 96, 1)
    track(remaster, "Kupferstrand", 215, 2)
    live = album("Wolkenfabrik Live", "Venrarik", 1990, ["Pop"])
    track(live, "Echo Feuer (Live)", 130, 1)
    mond = album("Mondgarten", "Ystasul", 2003, ["Electronic"])
    track(mond, "Glasfluss", 301, 1, mb=True)
    track(mond, "Sandkreise", 256, 2)
    track(mond, "Funkenregen", 199, 3)
    sampler = album("Club Nächte Vol. 3", "Various Artists", 2016, ["Electronic"])
    track(sampler, "Lichtstreifen", 222, 1, artists=["Vomima"])
    track(sampler, "Glasfluss", 300, 2, artists=["Ystasul"])
    track(sampler, "Rote Antenne", 241, 3, artists=["Orvenna"])
    hafen = album("Hafenlieder", "Orvenna", 1974, ["Folk"], picture=False)
    track(hafen, "Möwenruf", 178, 1)
    track(hafen, "Teerseile", 205, 2)
    stadt = album("Neonstadt", "Brakkel", 1999, ["Rock"])
    track(stadt, "Ampelglühen", 233, 1, mb=True)
    track(stadt, "Betonblumen", 262, 2)
    track(stadt, "Intro", 14, 3)
    zug = album("Fernweh", "Talmira", 2021, ["Jazz"])
    track(zug, "Gleis Neun", 344, 1)
    track(zug, "Abfahrt", 281, 2)
    kalt = album("Frostzeit", "Quenor", None, [])
    track(kalt, "Eisblume", 199, 1)
    return albums, tracks


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


def _parse(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text[:19]).replace(tzinfo=UTC)
    except ValueError:
        return None


def _jpeg(color: tuple[int, int, int], size: int) -> bytes:
    from PIL import Image

    picture = Image.new("RGB", (size, size), color)
    out = io.BytesIO()
    picture.save(out, "JPEG", quality=70)
    return out.getvalue()


@dataclass
class FakeJellyfin(Playlists):
    """The stand-in. ``now`` is its clock (the ``Date`` of its answers); the tests move it."""

    name: str = "Probe"
    #: The server's own id (a second stand-in beside the first needs another one).
    server_id: str = SERVER_ID
    now: datetime = field(default_factory=lambda: datetime(2026, 11, 15, 12, 0, tzinfo=UTC))
    #: So many requests from now on answer 503, as while Jellyfin starts.
    starting: int = 0
    #: Hosts that cannot be reached at all.
    down: set[str] = field(default_factory=set)
    #: Quick Connect switched off in the server's settings.
    quick_enabled: bool = True
    #: Everything answers 404, as behind a proxy whose base path changed.
    moved_away: bool = False
    #: Every request seen: ``(method, path, params)``; the sound and picture requests are in there too.
    calls: list[tuple[str, str, dict[str, str]]] = field(default_factory=list)
    #: The previews that began: their parameters.
    starts: list[dict[str, str]] = field(default_factory=list)
    sound_bytes: int = 900_000
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: Called with the number of requests so far and the path, before each is answered (a test stops nexmix there).
    hook: Any = None

    def __post_init__(self) -> None:
        self.albums, self.tracks = made_up_library()
        self.users = [User(make_id("user", "Erika"), "Erika", True), User(make_id("user", "Jonas"), "Jonas", False)]
        #: Tokens by value: the user they belong to (None: an API key) and the device id they were made for.
        self.tokens: dict[str, tuple[str | None, str]] = {}
        self.api_key = _secret(16)
        self.tokens[self.api_key] = (None, "")
        #: Quick Connect under way, by secret.
        self.quick: dict[str, dict[str, Any]] = {}
        #: The tracks of a library removed for a while (``library_removed``).
        self.away_tracks: dict[str, Track] = {}
        self._init_playlists()

    # --- Changing things, as somebody would in Jellyfin or on the disk -----------------------------------------

    def tick(self, seconds: int = 1) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now

    def touch(self, track_id: str, **values: Any) -> None:
        track = self.tracks[track_id]
        for key, value in values.items():
            setattr(track, key, value)
        track.saved = self.now

    def rename(self, track_id: str, new_name: str) -> str:
        """The file renamed on the disk: Jellyfin sees a track gone and a new one, with a new id."""
        track = self.tracks.pop(track_id)
        track.path = track.path.rsplit("/", 1)[0] + f"/{new_name}.flac"
        track.saved = track.created = self.now
        self.tracks[track.id] = track
        self.drop_from_lists(track_id)
        return track.id

    def delete(self, track_id: str) -> None:
        self.tracks.pop(track_id)
        self.drop_from_lists(track_id)

    def bulk(self, count: int, *, artist: str = "Brakkel", name: str = "Lange Nacht") -> list[str]:
        """``count`` more made-up tracks on one album of their own; their ids in order."""
        ident = make_id("album", artist, name)
        self.albums[ident] = Album(ident, name, artist, 2011, ["Rock"], created=self.now, saved=self.now)
        made: list[str] = []
        for number in range(1, count + 1):
            track = Track(f"/music/{artist}/{name}/{number:05d} Stueck {number}.flac", f"Stueck {number}", ident,
                          [artist], 120 + number % 200, 2011, ["Rock"], number=number, created=self.now,
                          saved=self.now)
            self.tracks[track.id] = track
            made.append(track.id)
        return made

    def add(self, album_id: str, name: str, seconds: int = 200) -> str:
        owner = self.albums[album_id]
        number = 1 + sum(1 for one in self.tracks.values() if one.album == album_id)
        track = Track(f"/music/{owner.artist}/{owner.name}/{number:02d} {name}.flac", name, album_id, [owner.artist],
                      seconds, owner.year, list(owner.genres), number=number, created=self.now, saved=self.now)
        self.tracks[track.id] = track
        return track.id

    def remove_user(self, name: str) -> None:
        """The user deleted in Jellyfin: his tokens go with him; his playlists stay (an administrator still sees
        them)."""
        gone = next(one.id for one in self.users if one.name == name)
        self.users = [one for one in self.users if one.id != gone]
        self.tokens = {token: owner for token, owner in self.tokens.items() if owner[0] != gone}

    def library_removed(self) -> None:
        """The music library removed from Jellyfin: its tracks gone, every playlist empty (``library_back``)."""
        self.away_tracks = dict(self.tracks)
        self.tracks = {}
        for one in self.lists.values():
            if one.entries:
                one.entries = []
                one.changed()

    def library_back(self) -> None:
        """The library added again with the same folder: the same ids (they hang on the paths), playlists stay
        empty."""
        self.tracks.update(self.away_tracks)
        self.away_tracks = {}
        for one in self.tracks.values():
            one.saved = self.now

    def sign_in(self, user: str = "Erika", device: str = "elsewhere") -> str:
        """A token of a user, made as Jellyfin's own web client would."""
        ident = next(one.id for one in self.users if one.name == user)
        return self._grant(ident, device)

    def _grant(self, user: str, device: str) -> str:
        # One device id, one token: the one before goes.
        for token, (owner, made_for) in list(self.tokens.items()):
            if made_for == device and owner is not None:
                del self.tokens[token]
        token = _secret(16)
        self.tokens[token] = (user, device)
        return token

    def confirm(self, code: str, user: str = "Erika") -> bool:
        """The user confirms the code in Jellyfin (Quick Connect)."""
        ident = next(one.id for one in self.users if one.name == user)
        for entry in self.quick.values():
            if entry["code"] == code and not self._expired(entry):
                entry["authenticated"], entry["user"] = True, ident
                entry["added"] = entry["added"] + timedelta(seconds=60)
                return True
        return False

    def latest_code(self) -> str | None:
        found = [entry for entry in self.quick.values() if not self._expired(entry)]
        return found[-1]["code"] if found else None

    def _expired(self, entry: dict[str, Any]) -> bool:
        return self.now - entry["added"] >= timedelta(seconds=QUICK_SECONDS)

    # --- HTTP ---------------------------------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            return self._handle(request)

    def _answer(self, status: int, payload: Any = None, *, text: str | None = None,
                content: bytes | None = None, kind: str = "application/json; charset=utf-8",
                headers: dict[str, str] | None = None) -> httpx.Response:
        head = {"date": format_datetime(self.now, usegmt=True), "content-type": kind, **(headers or {})}
        if content is not None:
            return httpx.Response(status, content=content, headers=head)
        if text is not None:
            return httpx.Response(status, content=text.encode(), headers={**head, "content-type": "text/plain"})
        if payload is None:
            return httpx.Response(status, content=b"", headers=head)
        return httpx.Response(status, content=json.dumps(payload).encode(), headers=head)

    def _auth(self, request: httpx.Request, params: dict[str, str]) -> tuple[dict[str, str], str | None, bool]:
        """The fields of the ``Authorization`` header, the user of its token (None: an API key) and whether the token
        is a valid one."""
        header = request.headers.get("authorization", "")
        fields: dict[str, str] = {}
        if header.startswith("MediaBrowser "):
            fields = dict(re.findall(r'(\w+)="([^"]*)"', header))
        token = fields.get("Token") or params.get("ApiKey") or ""
        if token in self.tokens:
            return fields, self.tokens[token][0], True
        return fields, None, False

    def _handle(self, request: httpx.Request) -> httpx.Response:
        parts = urlsplit(str(request.url))
        if parts.hostname in self.down:
            raise httpx.ConnectError("unreachable", request=request)
        path = parts.path
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        self.calls.append((request.method, path, params))
        if self.hook is not None:
            self.hook(len(self.calls), path)
        if self.starting > 0:
            self.starting -= 1
            return self._answer(503, text="Jellyfin server is loading. Please try again shortly.")
        if self.moved_away:
            return self._answer(404, text="Not found")
        method = request.method
        if method == "GET" and path == "/System/Info/Public":
            return self._answer(200, {"LocalAddress": "http://127.0.0.1:8096", "ServerName": self.name,
                                      "Version": "12.4.0", "ProductName": "Jellyfin Server", "OperatingSystem": "",
                                      "Id": self.server_id, "StartupWizardCompleted": True})
        if path.startswith("/Audio/") and path.endswith("/stream.mp3") and method == "GET":
            return self._sound(path, params)
        if path.startswith("/Items/") and path.endswith("/Images/Primary") and method == "GET":
            return self._picture(path, params)
        fields, user, valid = self._auth(request, params)
        if path.startswith("/QuickConnect/") or path == "/Users/AuthenticateWithQuickConnect":
            return self._quick(method, path, params, request, fields)
        if not valid:
            return self._answer(401, text=ERROR)
        admin = user is None or any(one.id == user and one.admin for one in self.users)
        asked_for = params.get("userId") or params.get("UserId")
        if asked_for and not any(one.id == asked_for for one in self.users):
            # Measured on 12.4: a list as an unknown user is 404, a playlist's entries as one 500.
            if path == "/Items":
                return self._answer(404, text=ERROR)
            if method == "GET" and path.startswith("/Playlists/") and path.endswith("/Items"):
                return self._answer(500, text=ERROR)
        if method == "GET" and path == "/Users/Me":
            if user is None:
                return self._answer(400, text=ERROR)
            return self._answer(200, self._user(user))
        if method == "GET" and path == "/Users":
            return self._answer(200, [self._user(one.id) for one in self.users])
        if method == "GET" and path == "/Library/VirtualFolders":
            if not admin:
                return self._answer(403, text=ERROR)
            return self._answer(200, [
                {"Name": "Musik", "Locations": ["/music"], "CollectionType": "music", "ItemId": LIBRARY_ID},
                {"Name": "Filme", "Locations": ["/films"], "CollectionType": "movies", "ItemId": OTHER_LIBRARY_ID},
            ])
        if method == "GET" and path == "/UserViews":
            return self._answer(200, {"Items": [
                {"Name": "Musik", "Id": LIBRARY_ID, "CollectionType": "music", "Type": "CollectionFolder"},
                {"Name": "Filme", "Id": OTHER_LIBRARY_ID, "CollectionType": "movies", "Type": "CollectionFolder"},
            ], "TotalRecordCount": 2, "StartIndex": 0})
        if method == "GET" and path == "/Items" and params.get("IncludeItemTypes") == "Playlist":
            listed = self._listing(params, user)
            return self._answer(200, {"Items": listed, "TotalRecordCount": len(listed), "StartIndex": 0})
        if method == "GET" and path == "/Items" and "Ids" in params:
            found = self._by_ids(params)
            return self._answer(200, {"Items": found, "TotalRecordCount": len(found), "StartIndex": 0})
        if method == "GET" and path == "/Items":
            return self._items(params, user)
        if path.startswith("/Playlists") or (method == "DELETE" and path.startswith("/Items/")):
            return self._playlists(request, method, path, params, user)
        return self._answer(404, text=ERROR)

    def _user(self, ident: str) -> dict[str, Any]:
        one = next(user for user in self.users if user.id == ident)
        return {"Name": one.name, "ServerId": self.server_id, "Id": one.id, "HasPassword": True,
                "Policy": {"IsAdministrator": one.admin, "EnableContentDeletion": False}, "Configuration": {}}

    def _quick(self, method: str, path: str, params: dict[str, str], request: httpx.Request,
               fields: dict[str, str]) -> httpx.Response:
        if not all(fields.get(name) for name in ("Client", "Device", "DeviceId", "Version")):
            return self._answer(400, text=ERROR)
        if method == "GET" and path == "/QuickConnect/Enabled":
            return self._answer(200, self.quick_enabled)
        if method == "POST" and path == "/QuickConnect/Initiate":
            if not self.quick_enabled:
                return self._answer(401, text=ERROR)
            secret = _secret(32)
            code = f"{secrets.randbelow(10**6):06d}"
            self.quick[secret] = {"code": code, "device": fields["DeviceId"], "added": self.now,
                                  "authenticated": False, "user": None}
            return self._answer(200, self._quick_view(secret))
        if method == "GET" and path == "/QuickConnect/Connect":
            entry = self.quick.get(params.get("secret", ""))
            if entry is None or self._expired(entry):
                return self._answer(404, "Unknown secret")
            return self._answer(200, self._quick_view(params["secret"]))
        if method == "POST" and path == "/Users/AuthenticateWithQuickConnect":
            try:
                secret = json.loads(request.content or b"{}").get("Secret", "")
            except ValueError:
                return self._answer(400, text=ERROR)
            entry = self.quick.get(secret)
            if entry is None or self._expired(entry) or not entry["authenticated"]:
                return self._answer(404, text=ERROR)
            token = self._grant(entry["user"], fields["DeviceId"])
            user = self._user(entry["user"])
            return self._answer(200, {"User": user, "SessionInfo": {"UserId": entry["user"],
                                                                     "DeviceId": fields["DeviceId"]},
                                      "AccessToken": token, "ServerId": self.server_id})
        return self._answer(404, text=ERROR)

    def _quick_view(self, secret: str) -> dict[str, Any]:
        entry = self.quick[secret]
        return {"Authenticated": entry["authenticated"], "Secret": secret, "Code": entry["code"],
                "DeviceId": entry["device"], "DeviceName": "nexmix", "AppName": "nexmix", "AppVersion": "0.1.0",
                "DateAdded": _stamp(entry["added"])}

    def _items(self, params: dict[str, str], user: str | None) -> httpx.Response:
        if params.get("ParentId") not in (LIBRARY_ID, None):
            return self._answer(200, {"Items": [], "TotalRecordCount": 0, "StartIndex": 0})
        kinds = params.get("IncludeItemTypes", "")
        since = _parse(params["MinDateLastSaved"]) if params.get("MinDateLastSaved") else None
        if kinds == "MusicAlbum":
            entries = [self._album(one) for one in sorted(self.albums.values(), key=lambda one: (one.name, one.id))
                       if since is None or one.saved >= since]
        elif kinds == "Audio":
            chosen = sorted(self.tracks.values(), key=lambda one: (one.name, one.id))
            entries = [self._track(one, params) for one in chosen if since is None or one.saved >= since]
        else:
            entries = []
        total = len(entries)
        start = int(params.get("StartIndex", "0") or 0)
        limit = params.get("Limit")
        page = entries[start:] if limit is None else entries[start:start + int(limit)]
        counted = len(page) if params.get("EnableTotalRecordCount") == "false" else total
        return self._answer(200, {"Items": page, "TotalRecordCount": counted, "StartIndex": start})

    def _album(self, one: Album) -> dict[str, Any]:
        artist = {"Name": one.artist, "Id": make_id("artist", one.artist)}
        entry: dict[str, Any] = {"Name": one.name, "ServerId": self.server_id, "Id": one.id, "IsFolder": True,
                                 "Type": "MusicAlbum", "AlbumArtist": one.artist, "AlbumArtists": [artist],
                                 "Genres": list(one.genres), "ProviderIds": {}, "MediaType": "Unknown",
                                 "DateCreated": _stamp(one.created)}
        if one.year:
            entry["ProductionYear"] = one.year
            entry["PremiereDate"] = f"{one.year}-01-01T00:00:00.0000000Z"
        entry["ImageTags"] = {"Primary": make_id("picture", one.id)} if one.picture else {}
        return entry

    def _track(self, one: Track, params: dict[str, str]) -> dict[str, Any]:
        owner = self.albums[one.album]
        ids = {}
        if one.mbid_track:
            ids = {"MusicBrainzTrack": one.mbid_track, "MusicBrainzRecording": one.mbid_recording}
        entry: dict[str, Any] = {
            "Name": one.name, "ServerId": self.server_id, "Id": one.id, "DateCreated": _stamp(one.created),
            "HasLyrics": False, "Container": "flac", "Genres": list(one.genres), "RunTimeTicks": one.seconds * TICKS,
            "IndexNumber": one.number, "ParentIndexNumber": one.disc, "ProviderIds": ids, "IsFolder": False,
            "ParentId": owner.id, "Type": "Audio", "Artists": list(one.artists),
            "ArtistItems": [{"Name": name, "Id": make_id("artist", name)} for name in one.artists],
            "Album": owner.name, "AlbumId": owner.id, "AlbumArtist": owner.artist,
            "AlbumArtists": [{"Name": owner.artist, "Id": make_id("artist", owner.artist)}],
            "LocationType": "FileSystem", "MediaType": "Audio"}
        if one.year:
            entry["ProductionYear"] = one.year
        if owner.picture:
            entry["AlbumPrimaryImageTag"] = make_id("picture", owner.id)
        if params.get("EnableUserData") == "true" and params.get("userId"):
            entry["UserData"] = {"PlayCount": one.plays, "Played": one.plays > 0, "IsFavorite": False}
        return entry

    def _picture(self, path: str, params: dict[str, str]) -> httpx.Response:
        ident = path.split("/")[2]
        if not re.fullmatch(r"[0-9a-f]{32}", ident):
            return self._answer(400, text=ERROR)
        album = self.albums.get(ident)
        if album is None:
            return self._answer(400, text=ERROR) if ident not in self.tracks else self._answer(404, {"error": "none"})
        if not album.picture:
            return self._answer(404, {"Message": "No image"})
        size = int(params.get("fillWidth", "200") or 200)
        shade = int(ident[:2], 16)
        kind = "image/jpeg" if params.get("format") == "Jpg" else "image/webp"
        return self._answer(200, content=_jpeg((shade, 90, 200 - shade // 2), size), kind=kind,
                            headers={"cache-control": "public"})

    def _sound(self, path: str, params: dict[str, str]) -> httpx.Response:
        ident = path.split("/")[2]
        self.starts.append(dict(params))
        if ident not in self.tracks:
            return self._answer(400, text=ERROR)
        return self._answer(200, content=sound.silence(self.sound_bytes), kind="audio/mpeg",
                            headers={"accept-ranges": "none"})
