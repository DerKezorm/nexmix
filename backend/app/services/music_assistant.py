"""A Music Assistant server at the level of single messages: who it is (``/info``), its websocket ``/ws`` with the
commands and the events, and the few plain requests that need no sign-in (the sound of a preview, a picture).
``sources/music_assistant.py`` builds the catalogue and the playlists on top of it.

What the calls below rely on (Music Assistant 2.10.9, schema 65, measured on a throwaway server):

* ``GET /info`` answers without signing in: the server's id, version, schema and the oldest schema it still serves
  (``min_supported_schema_version``). Its ``base_url`` is the address the server sees itself under (a container's,
  often): never used, every address is built from the one the operator typed.
* **One websocket per server** (``Connection``). Right after it opens, the server sends its info unasked; the first
  command is ``auth`` with the token. Before that every command fails with error 20, a token run out or revoked fails
  with 23: both mean "connect again". Commands go as ``{"message_id", "command", "args"}``; the answer carries the same
  ``message_id``, a long one comes in several messages with ``"partial": true`` and is put together here. An error is
  ``{"message_id", "error_code", "details"}``; only the code counts (2: not found, 3: not valid, 999: anything else),
  the words never reach the log. Events (``{"event", "object_id", "data"}``) come to every signed-in client and are
  handed to whoever listens (``events_of``).
* The websocket breaks when Music Assistant restarts (about 13 seconds until it answers again). It is opened again at
  the next command, but not sooner than ``REOPEN`` after the last try failed: a server that is away is not asked again
  and again.
* ``/preview?token=…`` (30 seconds of AAC from the start of a track) and ``/imageproxy/<id>`` (fixed sizes) answer
  without signing in: the token of a preview is good for 60 seconds, a picture's id is 64 hexadecimal characters. Both
  are fetched by nexmix and passed on; neither the address nor a token of a preview ever reaches a browser.

The token travels inside the websocket only (the ``auth`` command), never in an address, never in the log. No redirect
is followed, certificates are checked, and an address that points at a link-local or metadata service is refused.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import itertools
import json
import logging
import queue
import re
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from .. import clock
from . import outbound, plex
from .plex import PlexError

logger = logging.getLogger("nexmix.musicassistant")

#: The tests put a stand-in Music Assistant here: ``TRANSPORT``/``ASYNC_TRANSPORT`` for the plain requests
#: (``httpx.MockTransport``), ``CHANNEL`` for the websocket (a function of the websocket's address); None: the network.
TRANSPORT: httpx.BaseTransport | None = None
ASYNC_TRANSPORT: httpx.AsyncBaseTransport | None = None
CHANNEL: Callable[[str], Channel] | None = None

#: The schema nexmix speaks (measured on 2.10.9), and the oldest schema of a server it takes.
SCHEMA = 65
MIN_SCHEMA = 28
#: A server id of Music Assistant: 32 hexadecimal characters.
SERVER_ID = re.compile(r"\A[0-9a-f]{32}\Z")
#: An id of a picture (``/imageproxy/<id>``).
PICTURE_ID = re.compile(r"\A[0-9a-f]{64}\Z")
#: What ``music/tracks/preview`` answers: a path with the token of the preview.
PREVIEW_PATH = re.compile(r"\A/preview\?token=[A-Za-z0-9_\-]{8,128}\Z")
#: The sizes ``/imageproxy`` takes (0: the original), and nexmix's three sizes of a cover mapped onto them.
PICTURE_SIZES = (80, 160, 256, 512, 1024)
COVER_SIZE = {80: 80, 200: 256, 400: 512}
#: How long one command may take, and how long the opening (the info and ``auth``).
CALL_SECONDS = 30.0
OPEN_SECONDS = 10.0
#: The waits before a websocket that failed is opened again, after tries in a row; the last one stays.
REOPEN = (1.0, 2.0, 5.0, 10.0, 15.0)
#: The largest message taken (a page of 1,000 tracks is some 1.7 MB).
MAX_MESSAGE = 2**25
#: How a wait is waited (the tests count instead).
pause: Callable[[float], None] = time.sleep

#: Codes that mean the server was not reached: whoever asked waits for it.
AWAY = frozenset({"ma_unreachable", "ma_timeout", "ma_starting"})
#: Music Assistant's error codes, as nexmix names them.
ERRORS = {2: "ma_not_found", 3: "ma_bad_request", 12: "ma_unsupported", 20: "ma_refused", 23: "ma_refused"}


class MaError(PlexError):
    """Music Assistant did not play along. ``code`` says how; never a text from Music Assistant."""


def check_address(url: str) -> str:
    """``http(s)://host[:port]`` as nexmix keeps it (no credentials, no path); a link-local or metadata address is
    refused, whoever typed it."""
    try:
        return plex.check_address(url)
    except PlexError as exc:
        if exc.code == "plex_unreachable":
            raise MaError("ma_unreachable") from exc
        raise MaError(exc.code, exc.status) from exc


def http_client(timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(transport=TRANSPORT, verify=outbound.tls(), trust_env=False, follow_redirects=False,
                        timeout=httpx.Timeout(timeout, connect=min(timeout, 8.0)))


def async_client(timeout: float = 15.0) -> httpx.AsyncClient:
    """For the sound of a preview, streamed: the same rules (no redirect, certificates checked)."""
    return httpx.AsyncClient(transport=ASYNC_TRANSPORT, verify=outbound.tls(), trust_env=False,
                             follow_redirects=False, timeout=httpx.Timeout(timeout, connect=min(timeout, 8.0)))


def token_expiry(token: str) -> datetime | None:
    """When a token of Music Assistant runs out: the ``exp`` of the JWT it is (read, not checked: the server checks
    it). None when it says nothing."""
    parts = (token or "").split(".")
    if len(parts) != 3:
        return None
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        claims = json.loads(raw)
        moment = int(claims.get("exp")) if isinstance(claims, dict) else 0
    except (ValueError, TypeError, binascii.Error, OverflowError):
        return None
    if moment <= 0:
        return None
    try:
        return datetime.fromtimestamp(moment, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def check_token(token: str) -> str:
    token = (token or "").strip()
    if not token or len(token) > 4096 or any(char.isspace() or char in '"\\' for char in token):
        raise MaError("bad_token", 422)
    return token


# --- Plain requests -----------------------------------------------------------------------------------------------


def info(url: str, timeout: float = 10.0) -> dict[str, Any]:
    """Who the server at ``url`` is (``/info``, no sign-in): its id, name, version and schema. ``not_ma`` when it is
    none, ``ma_too_old`` when its schema is older than nexmix takes."""
    try:
        with http_client(timeout) as client:
            answer = client.get(f"{url.rstrip('/')}/info", headers={"Accept": "application/json"})
    except httpx.TimeoutException as exc:
        raise MaError("ma_timeout", 504) from exc
    except httpx.HTTPError as exc:
        raise MaError("ma_unreachable") from exc
    if answer.status_code != 200 or len(answer.content) > 65536:
        raise MaError("not_ma", 422)
    try:
        found = answer.json()
    except ValueError as exc:
        raise MaError("not_ma", 422) from exc
    return _server_info(found)


def _server_info(found: Any) -> dict[str, Any]:
    if not isinstance(found, dict):
        raise MaError("not_ma", 422)
    server = str(found.get("server_id") or "").lower()
    try:
        schema = int(found.get("schema_version"))
        oldest = int(found.get("min_supported_schema_version") or 0)
    except (TypeError, ValueError) as exc:
        raise MaError("not_ma", 422) from exc
    if not SERVER_ID.fullmatch(server):
        raise MaError("not_ma", 422)
    if schema < MIN_SCHEMA or oldest > SCHEMA:
        raise MaError("ma_too_old", 422)
    return {"id": server, "name": str(found.get("name") or "").strip()[:200] or "Music Assistant",
            "version": str(found.get("server_version") or "")[:40], "schema": schema,
            "starting": str(found.get("status") or "") == "starting"}


def picture(url: str, picture_id: str, size: int, timeout: float = 20.0) -> tuple[bytes, str]:
    """A picture by its id, scaled by Music Assistant at one of its sizes, as JPEG (``fmt=webp`` is passed over
    there). Only a checked id and a fixed size reach the server."""
    if not PICTURE_ID.fullmatch(picture_id or "") or size not in PICTURE_SIZES:
        raise MaError("bad_path", 400)
    try:
        with http_client(timeout) as client:
            answer = client.get(f"{url.rstrip('/')}/imageproxy/{picture_id}", params={"size": size, "fmt": "jpeg"})
    except httpx.TimeoutException as exc:
        raise MaError("ma_timeout", 504) from exc
    except httpx.HTTPError as exc:
        raise MaError("ma_unreachable") from exc
    if answer.status_code == 404:
        raise MaError("ma_not_found", 404)
    if answer.status_code != 200:
        raise MaError("ma_error")
    kind = answer.headers.get("content-type", "").split(";")[0].strip().lower()
    if kind not in ("image/jpeg", "image/png") or not answer.content or len(answer.content) > plex.COVER_BYTES:
        raise MaError("no_cover")
    return answer.content, kind


def preview_url(url: str, path: str) -> str:
    """The address of a preview: the server's own address and the checked path it answered."""
    if not PREVIEW_PATH.fullmatch(path or ""):
        raise MaError("ma_error")
    return f"{url.rstrip('/')}{path}"


# --- The websocket -------------------------------------------------------------------------------------------------


class Channel(Protocol):
    """A websocket as ``Connection`` uses it: text out, text in (None: nothing within ``timeout``), closed."""

    def send(self, text: str) -> None: ...

    def recv(self, timeout: float) -> str | None: ...

    def close(self) -> None: ...


class Closed(Exception):
    """The websocket is closed."""


def socket_address(url: str) -> tuple[str, str, int, bool]:
    """The websocket's address for the server's checked address: ``(ws url, host, port, tls)``."""
    parts = urlsplit(url)
    secure = parts.scheme == "https"
    host = parts.hostname or ""
    port = parts.port or (443 if secure else 80)
    return f"{'wss' if secure else 'ws'}://{parts.netloc}/ws", host, port, secure


class _Socket:
    """A real websocket (``websockets``): the connection is made here, to the checked address, so that no redirect is
    followed and no proxy of the environment is used."""

    def __init__(self, url: str) -> None:
        from websockets.sync.client import connect

        address, host, port, secure = socket_address(url)
        self._raw = socket.create_connection((host, port), timeout=OPEN_SECONDS)
        try:
            self._socket = connect(address, sock=self._raw, ssl=outbound.tls() if secure else None,
                                   server_hostname=host if secure else None, proxy=None, open_timeout=OPEN_SECONDS,
                                   compression=None, ping_interval=None, max_size=MAX_MESSAGE,
                                   user_agent_header="nexmix", legacy=True)
        except BaseException:
            self._raw.close()
            raise

    def send(self, text: str) -> None:
        from websockets.exceptions import WebSocketException

        try:
            self._socket.send(text)
        except (OSError, WebSocketException) as exc:
            raise Closed from exc

    def recv(self, timeout: float) -> str | None:
        from websockets.exceptions import WebSocketException

        try:
            found = self._socket.recv(timeout=timeout)
        except TimeoutError:
            return None
        except (OSError, WebSocketException) as exc:
            raise Closed from exc
        return found if isinstance(found, str) else found.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._socket.close()
        except Exception:  # noqa: BLE001 - it is going anyway
            logger.debug("Music Assistant websocket closed while in use")
        finally:
            self._raw.close()


def open_channel(url: str) -> Channel:
    """The websocket to the server at ``url`` (a checked address)."""
    address = socket_address(url)[0]
    if CHANNEL is not None:
        return CHANNEL(address)
    return _Socket(url)


@dataclass
class _Waiting:
    done: threading.Event = field(default_factory=threading.Event)
    parts: list[Any] = field(default_factory=list)
    result: Any = None
    error: str | None = None


def _named(code: Any) -> str:
    try:
        return ERRORS.get(int(code), "ma_error")
    except (TypeError, ValueError):
        return "ma_error"


class Connection:
    """The websocket to one server, signed in, shared by everybody who asks that server (the sync of the playlists,
    the catalogue, a preview). One thread reads it: answers go to whoever waits for them, events into ``events``."""

    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self._token = token
        self._channel: Channel | None = None
        self._send_lock = threading.Lock()
        self._waiting: dict[str, _Waiting] = {}
        self._waiting_lock = threading.Lock()
        self._ids = itertools.count(1)
        self._reader: threading.Thread | None = None
        self.alive = False
        self.server: dict[str, Any] = {}
        #: Events as they came, for whoever listens (``ma_events``); the oldest go when nobody does.
        self.events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=2000)

    def open(self) -> None:
        """Opens the websocket, reads the server's greeting and signs in. ``ma_refused`` for a token the server does
        not take (any more)."""
        channel = open_channel(self.url)
        try:
            greeting = self._first(channel)
            self.server = _server_info(greeting)
            channel.send(json.dumps({"message_id": "auth", "command": "auth", "args": {"token": self._token}}))
            deadline = clock.monotonic() + OPEN_SECONDS
            while True:
                left = deadline - clock.monotonic()
                if left <= 0:
                    raise MaError("ma_timeout", 504)
                raw = channel.recv(left)
                if raw is None:
                    continue
                message = _parse(raw)
                if message is None or message.get("message_id") != "auth":
                    continue
                if "error_code" in message:
                    raise MaError(_named(message.get("error_code")))
                result = message.get("result")
                if not isinstance(result, dict) or result.get("authenticated") is not True:
                    raise MaError("ma_refused")
                break
        except Closed as exc:
            channel.close()
            raise MaError("ma_unreachable") from exc
        except BaseException:
            channel.close()
            raise
        self._channel = channel
        self.alive = True
        self._reader = threading.Thread(target=self._read, name="music-assistant", daemon=True)
        self._reader.start()

    @staticmethod
    def _first(channel: Channel) -> Any:
        deadline = clock.monotonic() + OPEN_SECONDS
        while True:
            left = deadline - clock.monotonic()
            if left <= 0:
                raise MaError("ma_timeout", 504)
            raw = channel.recv(left)
            if raw is None:
                continue
            message = _parse(raw)
            if message is not None and "server_id" in message:
                return message

    def _read(self) -> None:
        channel = self._channel
        assert channel is not None
        try:
            while self.alive:
                raw = channel.recv(1.0)
                if raw is None:
                    continue
                message = _parse(raw)
                if message is not None:
                    self._take(message)
        except Closed:
            logger.debug("Music Assistant websocket ended")
        except Exception as exc:  # noqa: BLE001 - the reader ends, the next command opens it again
            logger.warning("Music Assistant websocket failed error=%s", type(exc).__name__)
        finally:
            self._end()

    def _take(self, message: dict[str, Any]) -> None:
        ident = message.get("message_id")
        if ident is None:
            if "event" in message:
                self._event(message)
            return
        with self._waiting_lock:
            waiting = self._waiting.get(str(ident))
        if waiting is None:
            return
        if "error_code" in message:
            waiting.error = _named(message.get("error_code"))
            waiting.done.set()
            return
        result = message.get("result")
        if message.get("partial") is True:
            if isinstance(result, list):
                waiting.parts.extend(result)
            return
        if waiting.parts:
            waiting.result = [*waiting.parts, *(result if isinstance(result, list) else [])]
        else:
            waiting.result = result
        waiting.done.set()

    def _event(self, message: dict[str, Any]) -> None:
        try:
            self.events.put_nowait(message)
        except queue.Full:
            # Nobody listens: the oldest goes (the lists are looked at anyway).
            try:
                self.events.get_nowait()
                self.events.put_nowait(message)
            except (queue.Empty, queue.Full):
                pass

    def _end(self) -> None:
        self.alive = False
        with self._waiting_lock:
            waiting = list(self._waiting.values())
        for one in waiting:
            if not one.done.is_set():
                one.error = "ma_unreachable"
                one.done.set()
        channel, self._channel = self._channel, None
        if channel is not None:
            channel.close()
        self._event({"event": "_closed", "object_id": None, "data": None})

    def close(self) -> None:
        self.alive = False
        channel = self._channel
        if channel is not None:
            channel.close()

    def call(self, command: str, args: dict[str, Any] | None = None, timeout: float = CALL_SECONDS) -> Any:
        """One command and its answer (the parts of a long one put together). ``ma_unreachable`` when the websocket
        breaks meanwhile, ``ma_timeout`` when no answer comes in time."""
        if not self.alive or self._channel is None:
            raise MaError("ma_unreachable")
        ident = str(next(self._ids))
        waiting = _Waiting()
        with self._waiting_lock:
            self._waiting[ident] = waiting
        try:
            try:
                with self._send_lock:
                    channel = self._channel
                    if channel is None:
                        raise MaError("ma_unreachable")
                    channel.send(json.dumps({"message_id": ident, "command": command, "args": args or {}}))
            except Closed as exc:
                raise MaError("ma_unreachable") from exc
            if not waiting.done.wait(timeout):
                raise MaError("ma_timeout", 504)
            if waiting.error is not None:
                raise MaError(waiting.error, 404 if waiting.error == "ma_not_found" else 502)
            return waiting.result
        finally:
            with self._waiting_lock:
                self._waiting.pop(ident, None)


def _parse(raw: str) -> dict[str, Any] | None:
    try:
        found = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return found if isinstance(found, dict) else None


# --- One connection per server ---------------------------------------------------------------------------------------


@dataclass
class _Kept:
    connection: Connection | None
    #: What the connection was opened for (address and token, hashed): another one opens anew.
    mark: str
    failures: int = 0
    failed_at: float = 0.0


_kept: dict[int, _Kept] = {}
_kept_lock = threading.Lock()
#: One lock per server around opening, so that two asking at once open one websocket.
_opening: dict[int, threading.Lock] = {}


def _mark(url: str, token: str) -> str:
    return hashlib.sha256(f"{url}\0{token}".encode()).hexdigest()


def connection(server_id: int, url: str, token: str) -> Connection:
    """The open websocket of a server, opened when there is none (or it broke). After a failed opening the next try
    waits for its turn (``REOPEN``): until then ``ma_unreachable`` at once."""
    mark = _mark(url, token)
    with _kept_lock:
        lock = _opening.setdefault(server_id, threading.Lock())
    with lock:
        with _kept_lock:
            kept = _kept.get(server_id)
            if kept is not None and kept.mark != mark:
                if kept.connection is not None:
                    kept.connection.close()
                kept = None
            if kept is not None and kept.connection is not None and kept.connection.alive:
                return kept.connection
            if kept is not None and kept.failures:
                wait = REOPEN[min(kept.failures, len(REOPEN)) - 1]
                if clock.monotonic() - kept.failed_at < wait:
                    raise MaError("ma_unreachable")
        made = Connection(url, token)
        try:
            made.open()
        except MaError as exc:
            with _kept_lock:
                before = _kept.get(server_id)
                failures = before.failures + 1 if before is not None and before.mark == mark else 1
                _kept[server_id] = _Kept(None, mark, failures, clock.monotonic())
            logger.debug("Music Assistant websocket not opened error=%s", exc.code)
            raise
        except (OSError, TimeoutError) as exc:
            with _kept_lock:
                before = _kept.get(server_id)
                failures = before.failures + 1 if before is not None and before.mark == mark else 1
                _kept[server_id] = _Kept(None, mark, failures, clock.monotonic())
            raise MaError("ma_unreachable") from exc
        except Exception as exc:
            with _kept_lock:
                _kept[server_id] = _Kept(None, mark, 1, clock.monotonic())
            logger.debug("Music Assistant websocket not opened error=%s", type(exc).__name__)
            raise MaError("ma_unreachable") from exc
        with _kept_lock:
            _kept[server_id] = _Kept(made, mark)
        return made


def current(server_id: int) -> Connection | None:
    """The websocket of a server while it is open (for whoever listens to its events)."""
    with _kept_lock:
        kept = _kept.get(server_id)
    return kept.connection if kept is not None and kept.connection is not None and kept.connection.alive else None


def drop(server_id: int) -> None:
    """The server's websocket closes (let go of, another token, another address)."""
    with _kept_lock:
        kept = _kept.pop(server_id, None)
    if kept is not None and kept.connection is not None:
        kept.connection.close()


def forget() -> None:
    """Every websocket closes (the tests, the end of the server)."""
    with _kept_lock:
        found = list(_kept.values())
        _kept.clear()
    for kept in found:
        if kept.connection is not None:
            kept.connection.close()
