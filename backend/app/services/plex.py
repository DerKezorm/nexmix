"""The Plex Media Server: the music library, read in pages, and the covers through Plex's own scaler.

What the calls below rely on (Plex Media Server 1.43, a library of some 52,000 tracks):

* Lists come in pages of up to 5,000 (``X-Plex-Container-Start``/``-Size``, sent as parameters and as headers); a page
  of tracks takes about two seconds. ``X-Plex-Container-Size=0`` answers with ``totalSize`` alone: the cheap count.
* ``updatedAt>>=<seconds>`` and ``addedAt>>=<seconds>`` filter every type; the times are Plex's own clock, whole
  seconds, and some entries have no ``updatedAt`` at all. The ``Date`` of each answer tells that clock
  (``PlexServer.clock``, ``PlexServer.began``).
* ``/library/metadata/<k1,k2,...>`` answers for many entries at once with their moods, styles and genres.
* Covers come from ``/photo/:/transcode`` with exactly ``width``, ``height``, ``minSize``, ``upscale`` and ``url``.
  Anything more is never sent: one more parameter (an output format) once made a real server restart.

The token goes in a header, never in an address: an address may end up in a log, a header does not. No redirect is
followed, certificates are checked, and an address that points at a link-local or metadata service is refused
(``check_address``), whoever typed it.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

from . import outbound

logger = logging.getLogger("nexmix.plex")

ARTIST, ALBUM, TRACK = 8, 9, 10
PRODUCT = "nexmix"
PAGE = 5000
#: Entries per request of ``/library/metadata/<k1,k2,...>``.
BULK = 100
#: Only these covers are ever fetched; the path is fetched with the server's token. ``\Z``, not ``$``: a line break at
#: the end is no part of a cover's path (``$`` lets one through).
THUMB = re.compile(r"\A/library/metadata/\d{1,12}/thumb/\d{1,14}\Z")
KEY = re.compile(r"\A\d{1,12}\Z")
COVER_SIZES = (80, 200, 400)
#: The most a cover from Plex may weigh.
COVER_BYTES = 2_000_000
#: The most any one answer of Plex may weigh (a page of 5,000 tracks is some 8 MB).
ANSWER_BYTES = 64_000_000
#: The fields a list leaves out when only the keys are wanted.
LIGHT = {
    "excludeFields": "summary,guid,parentGuid,grandparentGuid,art,grandparentArt,thumb,parentThumb,grandparentThumb,"
    "key,parentKey,grandparentKey",
    "excludeElements": "Media,Genre,Image,Guid,Mood,Style,Country,Collection,Similar,Location",
}

#: The tests put a stand-in Plex here (``httpx.MockTransport``); None: the network.
TRANSPORT: httpx.BaseTransport | None = None
#: The same for the streamed sound of a preview (``async_client``).
ASYNC_TRANSPORT: httpx.AsyncBaseTransport | None = None
#: Plex's converter: a piece of a track as MP3 from a given second, and the end of such a session.
SOUND_START = "/music/:/transcode/universal/start.mp3"
SOUND_STOP = "/music/:/transcode/universal/stop"
#: How a host name is turned into addresses for ``check_address``; the tests answer it themselves.
resolver: Callable[[str, int], list[str]] = outbound.resolve


class PlexError(Exception):
    """Plex did not play along. ``code`` says how, for the person and for the log; never a text from Plex."""

    def __init__(self, code: str, status: int = 502) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def check_address(url: str) -> str:
    """The address of a Plex server as nexmix keeps it: ``http(s)://host[:port]``, no credentials, no path. A host
    that resolves to a link-local or metadata address is refused (``address_refused``), whoever typed it."""
    text = (url or "").strip().rstrip("/")
    try:
        parts = urlsplit(text)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise PlexError("bad_url", 422) from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PlexError("bad_url", 422)
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise PlexError("bad_url", 422)
    try:
        addresses = resolver(parts.hostname, port)
    except outbound.Unreachable as exc:
        raise PlexError("plex_unreachable") from exc
    for address in addresses:
        try:
            ip = outbound.ip_of(address)
        except ValueError as exc:
            raise PlexError("address_refused", 422) from exc
        if outbound.never(ip):
            raise PlexError("address_refused", 422)
    return f"{parts.scheme}://{parts.netloc}"


def http_client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        transport=TRANSPORT, verify=outbound.tls(), trust_env=False, follow_redirects=False,
        timeout=httpx.Timeout(timeout, connect=min(timeout, 8.0)),
    )


def async_client(timeout: float = 15.0) -> httpx.AsyncClient:
    """For the sound of a preview, streamed: the same rules as ``http_client`` (no redirect, certificates checked)."""
    return httpx.AsyncClient(
        transport=ASYNC_TRANSPORT, verify=outbound.tls(), trust_env=False, follow_redirects=False,
        timeout=httpx.Timeout(timeout, connect=min(timeout, 8.0)),
    )


def sound_headers(token: str, client_id: str) -> dict[str, str]:
    """The converter picks its profile by the platform: without one Plex answers 400 (measured)."""
    return {"X-Plex-Token": token, "X-Plex-Client-Identifier": client_id, "X-Plex-Product": PRODUCT,
            "X-Plex-Platform": "Chrome"}


def sound_params(key: int, offset: int, session: str, bitrate: int) -> dict[str, Any]:
    """A track converted to MP3 from ``offset`` seconds: only a track of the catalogue (``key``), never a path from
    outside, and only these parameters."""
    if not KEY.fullmatch(str(key)):
        raise PlexError("bad_path", 400)
    return {
        "path": f"/library/metadata/{key}", "mediaIndex": 0, "partIndex": 0, "protocol": "http",
        "directPlay": 0, "directStream": 0, "musicBitrate": bitrate, "maxAudioBitrate": bitrate,
        "offset": max(0, int(offset)), "session": session, "X-Plex-Session-Identifier": session,
        "location": "lan", "hasMDE": 1,
    }


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _http_date(value: str | None) -> int | None:
    """An HTTP date (``Date`` of an answer) in seconds; None when there is none or it says nothing sensible."""
    if not value:
        return None
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    seconds = int(moment.timestamp())
    return seconds if 0 < seconds < 10**11 else None


@dataclass
class Section:
    key: str
    title: str


class PlexServer:
    """One server, one token. Open it with ``with`` (one connection pool for a whole run)."""

    def __init__(self, url: str, token: str, client_id: str, *, timeout: float = 60.0,
                 stop: Callable[[], bool] | None = None) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self.client_id = client_id
        self._client = http_client(timeout)
        self._stop = stop
        #: The time on the server's own clock, from the ``Date`` of its last answer (seconds); None until one said it.
        self.clock: int | None = None
        #: The same of its first answer: when the work with this server began, on its clock.
        self.began: int | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "X-Plex-Token": self._token,
            "X-Plex-Client-Identifier": self.client_id,
            "X-Plex-Product": PRODUCT,
        }
        headers.update(extra or {})
        return headers

    def _send(self, method: str, path: str, params: dict[str, Any] | None = None,
              headers: dict[str, str] | None = None, timeout: float | None = None) -> httpx.Response:
        if self._stop is not None and self._stop():
            raise PlexError("stopped", 503)
        try:
            answer = self._client.request(method, f"{self.url}{path}", params=params, headers=self._headers(headers),
                                          timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT)
        except httpx.TimeoutException as exc:
            raise PlexError("plex_timeout", 504) from exc
        except httpx.HTTPError as exc:
            raise PlexError("plex_unreachable") from exc
        stamp = _http_date(answer.headers.get("date"))
        if stamp is not None:
            self.clock = stamp
            if self.began is None:
                self.began = stamp
        status = answer.status_code
        if status in (401, 403):
            raise PlexError("plex_refused")
        if status == 404:
            raise PlexError("plex_not_found", 404)
        if status == 503:
            raise PlexError("plex_busy", 503)
        if status >= 300:
            # A redirect is not followed (it could lead anywhere), any other failure is Plex's.
            raise PlexError("plex_error")
        if len(answer.content) > ANSWER_BYTES:
            raise PlexError("plex_error")
        return answer

    def get(self, path: str, params: dict[str, Any] | None = None, *, start: int | None = None,
            size: int | None = None, timeout: float | None = None) -> dict[str, Any]:
        query: dict[str, Any] = dict(params or {})
        headers: dict[str, str] = {}
        if start is not None and size is not None:
            query["X-Plex-Container-Start"] = start
            query["X-Plex-Container-Size"] = size
            headers["X-Plex-Container-Start"] = str(start)
            headers["X-Plex-Container-Size"] = str(size)
        answer = self._send("GET", path, query, headers, timeout)
        try:
            payload = answer.json()
        except ValueError as exc:
            raise PlexError("plex_error") from exc
        container = payload.get("MediaContainer") if isinstance(payload, dict) else None
        return container if isinstance(container, dict) else {}

    # --- What the server is -------------------------------------------------------------------------------------

    def identity(self) -> dict[str, str]:
        """Name, machine id and version; the root asks for the token, so a wrong one is found here."""
        root = self.get("/", timeout=10.0)
        return {
            "machine_id": str(root.get("machineIdentifier") or ""),
            "name": str(root.get("friendlyName") or "").strip()[:200],
            "version": str(root.get("version") or ""),
        }

    def music_sections(self) -> list[Section]:
        container = self.get("/library/sections", timeout=15.0)
        found = []
        for entry in container.get("Directory") or []:
            if isinstance(entry, dict) and entry.get("type") == "artist" and KEY.fullmatch(str(entry.get("key") or "")):
                found.append(Section(str(entry["key"]), str(entry.get("title") or "").strip()[:200]))
        return found

    def music_analysis_enabled(self, section: str) -> bool | None:
        """The library's switch for sonic analysis, as its preferences say; None when Plex does not say."""
        try:
            container = self.get(f"/library/sections/{section}/prefs", timeout=10.0)
        except PlexError as exc:
            if exc.code in ("plex_refused", "plex_unreachable", "plex_timeout"):
                raise
            return None
        for setting in container.get("Setting") or []:
            if isinstance(setting, dict) and setting.get("id") == "musicAnalysis":
                value = setting.get("value")
                if isinstance(value, bool):
                    return value
                if str(value).lower() in ("1", "true"):
                    return True
                if str(value).lower() in ("0", "false"):
                    return False
        return None

    # --- The library --------------------------------------------------------------------------------------------

    def _all(self, section: str) -> str:
        if not KEY.fullmatch(section):
            raise PlexError("library_gone", 409)
        return f"/library/sections/{section}/all"

    def count(self, section: str, kind: int, filters: dict[str, Any] | None = None) -> int:
        container = self.get(self._all(section), {"type": kind, **(filters or {})}, start=0, size=0)
        total = _int(container.get("totalSize"))
        return total if total is not None else _int(container.get("size")) or 0

    def pages(self, section: str, kind: int, filters: dict[str, Any] | None = None,
              *, page: int = PAGE) -> Iterator[tuple[list[dict[str, Any]], int]]:
        """The entries of a type, a page at a time, with the total Plex reports."""
        start = 0
        while True:
            container = self.get(self._all(section), {"type": kind, **(filters or {})}, start=start, size=page)
            entries = [entry for entry in container.get("Metadata") or [] if isinstance(entry, dict)]
            total = _int(container.get("totalSize"))
            yield entries, total if total is not None else start + len(entries)
            start += page
            if not entries or (total is not None and start >= total) or (total is None and len(entries) < page):
                return

    def keys(self, section: str, kind: int, filters: dict[str, Any] | None = None) -> set[int]:
        """Only the keys of a type: a light list."""
        found: set[int] = set()
        for entries, _total in self.pages(section, kind, {**LIGHT, **(filters or {})}):
            found.update(key for key in (_int(entry.get("ratingKey")) for entry in entries) if key is not None)
        return found

    def tag_values(self, section: str, kind: str, of_type: int) -> list[tuple[str, str]]:
        """The values of a filter (``mood``, ``genre``, ``style``) for a type: ``(key, name)``."""
        container = self.get(f"/library/sections/{section}/{kind}", {"type": of_type}, timeout=20.0)
        found = []
        for entry in container.get("Directory") or []:
            if isinstance(entry, dict) and entry.get("key") is not None and entry.get("title"):
                found.append((str(entry["key"]), str(entry["title"]).strip()[:200]))
        return found

    def bulk(self, keys: Iterable[int], *, guids: bool = False) -> list[dict[str, Any]]:
        """Many entries at once, with their tags (``guids``: and the ids of the agents, MusicBrainz among them)."""
        chosen = [str(int(key)) for key in keys]
        if not chosen:
            return []
        container = self.get(f"/library/metadata/{','.join(chosen)}", {"includeGuids": "1"} if guids else None,
                             timeout=60.0)
        return [entry for entry in container.get("Metadata") or [] if isinstance(entry, dict)]

    def nearest(self, key: int, limit: int, max_distance: float) -> list[tuple[int, float]]:
        """The tracks that sound closest to a track, by Plex's sonic analysis: ``(ratingKey, distance)``, closest
        first. A track that is not analysed has none (Plex answers an empty list). The form of a filled answer is taken
        from Plex's own clients (``distance`` on each entry); a track without one counts as far."""
        if not KEY.fullmatch(str(key)):
            raise PlexError("bad_path", 400)
        container = self.get(f"/library/metadata/{key}/nearest",
                             {"limit": int(limit), "maxDistance": f"{float(max_distance):.2f}"}, timeout=10.0)
        found: list[tuple[int, float]] = []
        for entry in container.get("Metadata") or []:
            if not isinstance(entry, dict) or entry.get("type", "track") != "track":
                continue
            other = _int(entry.get("ratingKey"))
            try:
                distance = float(entry.get("distance"))
            except (TypeError, ValueError):
                distance = float(max_distance)
            if other is not None and other != key and math.isfinite(distance):
                found.append((other, max(0.0, distance)))
        found.sort(key=lambda pair: (pair[1], pair[0]))
        return found

    # --- Covers -------------------------------------------------------------------------------------------------

    def cover(self, thumb: str, size: int) -> tuple[bytes, str]:
        """A cover scaled by Plex: exactly the five parameters, nothing else."""
        if not THUMB.fullmatch(thumb) or size not in COVER_SIZES:
            raise PlexError("bad_path", 400)
        params = {"width": size, "height": size, "minSize": 1, "upscale": 1, "url": thumb}
        answer = self._send("GET", "/photo/:/transcode", params, {"Accept": "image/*"}, 20.0)
        kind = answer.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind not in ("image/jpeg", "image/png"):
            raise PlexError("no_cover")
        if not answer.content or len(answer.content) > COVER_BYTES:
            raise PlexError("no_cover")
        return answer.content, kind


def tags_of(entry: dict[str, Any], name: str) -> list[str]:
    """The names of an entry's ``Mood``, ``Genre`` or ``Style`` tags."""
    found = []
    for tag in entry.get(name) or []:
        if isinstance(tag, dict) and tag.get("tag"):
            found.append(str(tag["tag"]).strip()[:200])
    return found

