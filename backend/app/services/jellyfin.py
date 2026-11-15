"""A Jellyfin server at the level of single requests: who it is, signing in by Quick Connect, its users and music
libraries, the items of a library in pages, the covers. ``sources/jellyfin.py`` builds the catalogue on top of it.

What the calls below rely on (Jellyfin 12.4, measured on a throwaway server and, only reading, on a library of some
52,800 tracks):

* Only ``Authorization: MediaBrowser Client="…", Device="…", DeviceId="…", Version="…", Token="…"`` signs a request
  in; ``X-Emby-Token``, ``X-Emby-Authorization`` and ``?api_key=`` are refused with 401. Quick Connect and the sign-in
  need the four fields even without a token.
* **One device id holds one token**: signing in again with the same ``DeviceId`` makes the token before invalid (401).
  nexmix gives itself one fixed device id per installation and server (``jellyfin_link.device_id``).
* Errors are ``text/plain`` "Error processing request."; the status says what happened.
* While Jellyfin starts it answers **503** to everything for a few seconds to about a minute (``WAITS``); before that a
  connection is refused or broken off. A long piece of work (a run of the catalogue) waits and asks again.
* Lists come in pages (``StartIndex``, ``Limit``); a page of 5,000 tracks took up to 9.4 seconds, whatever fields are
  asked for: the time is the server's. ``Limit=0`` without ``EnableTotalRecordCount=false`` gives the total alone.
* ``MinDateLastSaved`` filters what was saved since a moment (UTC, on Jellyfin's clock: the ``Date`` of its answers);
  ``MinDateCreated`` and ``MinDateAdded`` are ignored.
* Audio and pictures are served **without signing in**: whoever knows an id and reaches the server can hear and see.
  Neither the address nor an id ever goes to a browser; nexmix passes sound and covers through.

The token goes in a header, never in an address. No redirect is followed, certificates are checked, and an address
that points at a link-local or metadata service is refused (``check_address``), whoever typed it. Every id that goes
into an address is 32 hexadecimal characters (``ID``), checked here.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Self

import httpx

from .. import __version__
from . import outbound, plex
from .plex import PlexError

#: The tests put a stand-in Jellyfin here (``httpx.MockTransport``); None: the network.
TRANSPORT: httpx.BaseTransport | None = None
#: The same for the streamed sound of a preview.
ASYNC_TRANSPORT: httpx.AsyncBaseTransport | None = None

CLIENT = "nexmix"
#: An id of Jellyfin: an item, a user, a library, the server itself.
ID = re.compile(r"\A[0-9a-f]{32}\Z")
#: Entries per page of a list.
PAGE = 5000
#: The longest one page may take (measured: up to 9.4 seconds for 5,000 tracks).
PAGE_SECONDS = 60.0
#: The waits, in seconds, while Jellyfin starts (503) or cannot be reached, before a long piece of work gives up.
WAITS = (2, 5, 10, 30, 30)
#: How a wait is waited (the tests count instead).
pause: Callable[[float], None] = time.sleep
COVER_SIZES = plex.COVER_SIZES
COVER_BYTES = plex.COVER_BYTES
ANSWER_BYTES = plex.ANSWER_BYTES
#: Fields of a track nexmix keeps; the path of the file is never asked for.
TRACK_FIELDS = "ProviderIds,Genres,DateCreated,ProductionYear,ParentId"
ALBUM_FIELDS = "ProviderIds,Genres,DateCreated,ProductionYear"
AUDIO, ALBUM = "Audio", "MusicAlbum"
#: What a server that is starting, or not there, is called; a long piece of work waits for it.
STARTING = "jellyfin_starting"
AWAY = frozenset({STARTING, "jellyfin_unreachable", "jellyfin_timeout"})


class JellyfinError(PlexError):
    """Jellyfin did not play along. ``code`` says how; never a text from Jellyfin."""


def check_address(url: str) -> str:
    """``http(s)://host[:port]`` as nexmix keeps it (no credentials, no path); a link-local or metadata address is
    refused, whoever typed it."""
    try:
        return plex.check_address(url)
    except PlexError as exc:
        if exc.code == "plex_unreachable":
            raise JellyfinError("jellyfin_unreachable") from exc
        raise JellyfinError(exc.code, exc.status) from exc


def checked_id(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not ID.fullmatch(text):
        raise JellyfinError("bad_path", 400)
    return text


def http_client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        transport=TRANSPORT, verify=outbound.tls(), trust_env=False, follow_redirects=False,
        timeout=httpx.Timeout(timeout, connect=min(timeout, 8.0)),
    )


def async_client(timeout: float = 15.0) -> httpx.AsyncClient:
    """For the sound of a preview, streamed: the same rules (no redirect, certificates checked)."""
    return httpx.AsyncClient(
        transport=ASYNC_TRANSPORT, verify=outbound.tls(), trust_env=False, follow_redirects=False,
        timeout=httpx.Timeout(timeout, connect=min(timeout, 8.0)),
    )


def _quote(value: str) -> str:
    return re.sub(r'["\\\r\n]', "", value)[:120]


def authorization(device_id: str, token: str = "") -> str:
    """The one header Jellyfin 12.4 takes; without a token for Quick Connect."""
    parts = [f'Client="{CLIENT}"', 'Device="nexmix"', f'DeviceId="{_quote(device_id)}"',
             f'Version="{_quote(__version__)}"']
    if token:
        parts.append(f'Token="{_quote(token)}"')
    return "MediaBrowser " + ", ".join(parts)


def headers(device_id: str, token: str = "") -> dict[str, str]:
    return {"Authorization": authorization(device_id, token), "Accept": "application/json"}


def _http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def stamp(moment: datetime) -> str:
    """A moment as Jellyfin's filters take it (UTC)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


@dataclass(frozen=True)
class Library:
    key: str
    title: str


@dataclass(frozen=True)
class User:
    id: str
    name: str
    admin: bool


class JellyfinServer:
    """One server, one token (or none, for what needs none). Open it with ``with``. ``patient``: a server that starts or
    is away is waited for (``WAITS``), as a run of the catalogue does; else it is said at once."""

    def __init__(self, url: str, token: str, device_id: str, *, timeout: float = 30.0,
                 stop: Callable[[], bool] | None = None, patient: bool = False) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self.device_id = device_id
        self._client = http_client(timeout)
        self._stop = stop
        self.patient = patient
        #: Jellyfin's clock at its last answer, and at its first one (its ``Date``).
        self.clock: datetime | None = None
        self.began: datetime | None = None
        #: How many times a start or an outage was waited for (for the log and the tests).
        self.waited = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _once(self, method: str, path: str, params: dict[str, Any] | None, body: Any,
              timeout: float | None) -> httpx.Response:
        if self._stop is not None and self._stop():
            raise JellyfinError("stopped", 503)
        try:
            answer = self._client.request(
                method, f"{self.url}{path}", params=params, json=body, headers=headers(self.device_id, self._token),
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT)
        except httpx.TimeoutException as exc:
            raise JellyfinError("jellyfin_timeout", 504) from exc
        except httpx.HTTPError as exc:
            raise JellyfinError("jellyfin_unreachable") from exc
        moment = _http_date(answer.headers.get("date"))
        if moment is not None:
            self.clock = moment
            if self.began is None:
                self.began = moment
        status = answer.status_code
        if status == 401:
            raise JellyfinError("jellyfin_refused")
        if status == 403:
            # Signed in, but this user may not do it (another user's playlist, a library not shared with him).
            raise JellyfinError("jellyfin_forbidden")
        if status == 404:
            raise JellyfinError("jellyfin_not_found", 404)
        if status == 503:
            raise JellyfinError(STARTING, 503)
        if status == 400:
            raise JellyfinError("jellyfin_bad_request", 400)
        if status >= 300:
            raise JellyfinError("jellyfin_error")
        if len(answer.content) > ANSWER_BYTES:
            raise JellyfinError("jellyfin_error")
        return answer

    def send(self, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None,
             timeout: float | None = None) -> httpx.Response:
        tries = 0
        while True:
            try:
                return self._once(method, path, params, body, timeout)
            except JellyfinError as exc:
                if not self.patient or exc.code not in (STARTING, "jellyfin_unreachable") or tries >= len(WAITS):
                    raise
                wait = WAITS[tries]
                tries += 1
                self.waited += 1
                # A stop while waiting ends the wait at the next look.
                pause(wait)

    def get(self, path: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        answer = self.send("GET", path, params, timeout=timeout)
        try:
            return answer.json()
        except ValueError as exc:
            raise JellyfinError("jellyfin_error") from exc

    def post(self, path: str, params: dict[str, Any] | None = None, body: Any = None,
             timeout: float | None = None) -> Any:
        answer = self.send("POST", path, params, body, timeout)
        if not answer.content:
            return None
        try:
            return answer.json()
        except ValueError as exc:
            raise JellyfinError("jellyfin_error") from exc

    # --- Who it is ----------------------------------------------------------------------------------------------

    def public_info(self) -> dict[str, str]:
        """Name, id and version, asked without signing in (``/System/Info/Public``): the address is a Jellyfin."""
        found = self.get("/System/Info/Public", timeout=10.0)
        product = str(found.get("ProductName") or "Jellyfin Server") if isinstance(found, dict) else ""
        if "jellyfin" not in product.lower():
            raise JellyfinError("not_jellyfin", 422)
        try:
            server = checked_id(found.get("Id"))
        except JellyfinError as exc:
            raise JellyfinError("not_jellyfin", 422) from exc
        return {"id": server, "name": str(found.get("ServerName") or "").strip()[:200] or "Jellyfin",
                "version": str(found.get("Version") or "")[:40]}

    def me(self) -> User | None:
        """The user the token belongs to; None for an API key (it has none: Jellyfin answers 400)."""
        try:
            found = self.get("/Users/Me", timeout=10.0)
        except JellyfinError as exc:
            if exc.code == "jellyfin_bad_request":
                return None
            raise
        return _user(found)

    def users(self) -> list[User]:
        found = self.get("/Users", timeout=15.0)
        users = [_user(one) for one in found if isinstance(one, dict)] if isinstance(found, list) else []
        return [one for one in users if one is not None]

    def libraries(self, user: str | None, admin: bool) -> list[Library]:
        """The music libraries: every one for an administrator or an API key, the ones the user sees otherwise."""
        found: list[Library] = []
        if admin:
            entries = self.get("/Library/VirtualFolders", timeout=15.0)
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict) and str(entry.get("CollectionType") or "").lower() == "music":
                    key = str(entry.get("ItemId") or "").lower()
                    if ID.fullmatch(key):
                        found.append(Library(key, str(entry.get("Name") or "").strip()[:200]))
            return found
        views = self.get("/UserViews", {"userId": checked_id(user)}, timeout=15.0)
        for entry in (views or {}).get("Items") or [] if isinstance(views, dict) else []:
            if isinstance(entry, dict) and str(entry.get("CollectionType") or "").lower() == "music":
                key = str(entry.get("Id") or "").lower()
                if ID.fullmatch(key):
                    found.append(Library(key, str(entry.get("Name") or "").strip()[:200]))
        return found

    # --- Quick Connect ------------------------------------------------------------------------------------------

    def quick_begin(self) -> dict[str, str]:
        """A code to confirm in Jellyfin, and the secret that redeems it (never logged, kept in memory only)."""
        found = self.post("/QuickConnect/Initiate", timeout=10.0)
        if not isinstance(found, dict):
            raise JellyfinError("jellyfin_error")
        secret, code = str(found.get("Secret") or ""), str(found.get("Code") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{16,128}", secret) or not re.fullmatch(r"\d{4,10}", code):
            raise JellyfinError("jellyfin_error")
        return {"secret": secret, "code": code}

    def quick_done(self, secret: str) -> bool:
        """Whether the code was confirmed in Jellyfin. ``quick_expired`` when Jellyfin has it no longer."""
        try:
            found = self.get("/QuickConnect/Connect", {"secret": secret}, timeout=10.0)
        except JellyfinError as exc:
            if exc.code == "jellyfin_not_found":
                raise JellyfinError("quick_expired", 404) from exc
            raise
        return bool(isinstance(found, dict) and found.get("Authenticated") is True)

    def quick_redeem(self, secret: str) -> str:
        """The token of the user who confirmed; redeemed once, the secret is forgotten by the caller."""
        try:
            found = self.post("/Users/AuthenticateWithQuickConnect", body={"Secret": secret}, timeout=15.0)
        except JellyfinError as exc:
            if exc.code == "jellyfin_not_found":
                raise JellyfinError("quick_expired", 404) from exc
            raise
        token = str(found.get("AccessToken") or "") if isinstance(found, dict) else ""
        if not re.fullmatch(r"[0-9a-fA-F]{16,128}", token):
            raise JellyfinError("jellyfin_error")
        return token

    # --- The library --------------------------------------------------------------------------------------------

    def _list_params(self, library: str, kind: str, since: datetime | None, user: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {"ParentId": checked_id(library), "Recursive": "true", "IncludeItemTypes": kind}
        if since is not None:
            params["MinDateLastSaved"] = stamp(since)
        if user:
            params["userId"] = checked_id(user)
        return params

    def count(self, library: str, kind: str, since: datetime | None = None) -> int:
        """How many items of a kind (since a moment): ``Limit=0`` gives the total alone."""
        found = self.get("/Items", {**self._list_params(library, kind, since, None), "Limit": 0,
                                    "EnableImages": "false", "EnableUserData": "false"}, timeout=PAGE_SECONDS)
        try:
            return max(0, int((found or {}).get("TotalRecordCount") or 0))
        except (TypeError, ValueError, AttributeError):
            return 0

    def pages(self, library: str, kind: str, fields: str, *, since: datetime | None = None, user: str | None = None,
              images: bool = False, page: int | None = None) -> Iterator[list[dict[str, Any]]]:
        """The items of a kind, a page at a time, in a stable order. ``user``: with that user's play counts."""
        page = page or PAGE
        start = 0
        while True:
            params = {**self._list_params(library, kind, since, user), "Fields": fields, "StartIndex": start,
                      "Limit": page, "SortBy": "SortName,Id", "SortOrder": "Ascending",
                      "EnableImages": "true" if images else "false",
                      "EnableUserData": "true" if user else "false", "EnableTotalRecordCount": "false"}
            if images:
                params["ImageTypeLimit"] = 1
                params["EnableImageTypes"] = "Primary"
            found = self.get("/Items", params, timeout=PAGE_SECONDS)
            entries = [one for one in (found or {}).get("Items") or [] if isinstance(one, dict)] \
                if isinstance(found, dict) else []
            yield entries
            start += page
            if len(entries) < page:
                return

    def cover(self, album: str, size: int) -> tuple[bytes, str]:
        """An album's picture scaled by Jellyfin, always as JPEG (without ``format`` a browser's ``Accept`` gets
        WebP)."""
        if size not in COVER_SIZES:
            raise JellyfinError("bad_path", 400)
        answer = self.send("GET", f"/Items/{checked_id(album)}/Images/Primary",
                           {"fillWidth": size, "fillHeight": size, "quality": 80, "format": "Jpg"}, timeout=20.0)
        kind = answer.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind not in ("image/jpeg", "image/png") or not answer.content or len(answer.content) > COVER_BYTES:
            raise JellyfinError("no_cover")
        return answer.content, kind


def _user(entry: Any) -> User | None:
    if not isinstance(entry, dict):
        return None
    ident = str(entry.get("Id") or "").lower()
    if not ID.fullmatch(ident):
        return None
    policy = entry.get("Policy") if isinstance(entry.get("Policy"), dict) else {}
    return User(ident, str(entry.get("Name") or "").strip()[:200], bool(policy.get("IsAdministrator")))


def sound_request(url: str, token: str, device_id: str, track: str, offset: int, session: str,
                  bitrate: int) -> tuple[str, dict[str, str], dict[str, Any]]:
    """The request for a preview: ``stream.mp3`` always converts (``universal`` plays a fitting file from its start and
    passes over the start time), from ``offset`` seconds at ``bitrate``. Only a checked track id, never a path."""
    return (f"{url.rstrip('/')}/Audio/{checked_id(track)}/stream.mp3", headers(device_id, token),
            {"audioBitRate": int(bitrate) * 1000, "startTimeTicks": max(0, int(offset)) * 10_000_000,
             "static": "false", "deviceId": device_id, "playSessionId": session})
