"""Listening in: about thirty seconds of a track, from a good third of it, converted by Plex and passed through nexmix.

**The way to Plex.** Only a track of a live catalogue, by its number; the track's server builds the request itself
(its connector, ``sources.PlexSource.sound_start``: Plex's converter, ``universal/start.mp3`` from 35 % of the track at
192 kbit/s) with the token in a header. Nothing the browser sends reaches Plex but the server's key of that track. The
answer is passed on as it comes and cut at ``MAX_BYTES`` (about forty seconds) or ``MAX_SECONDS``; then, and whenever
the listener goes away early (the browser stops, another preview of the same listener begins), Plex is told to end the
session (``universal/stop``).

**Who may.** ``may_listen`` says whether a request may hear a track and who listens (``Listener``): from the start an
account signed in (a session or a device). It is a name in this module, looked up at each request, so that another way
in (a guest of an invitation) can stand in its place and fall back to it. The operator's switch (``preview_allowed``)
holds for everybody, after that.

**Limits**, per listener: one preview at a time (a new one ends the one before, the way a browser moves from one track
to the next), ``PER_HOUR`` an hour (``429`` with ``Retry-After``), and on the whole server ``SLOTS`` at once (Plex
converts each one). Kept in memory: a restart forgets them, which is fine for a brake.

**When Plex says no.** Every refusal of Plex (another status than 200, no connection, no answer in time, a 200 without
sound) is one WARNING line: the track, Plex's status, the kind, how long it took and at most 200 characters of Plex's
own words, cleaned (no markup, no address with its parameters, never the token or a header), or the type of the error.
A refusal that may pass by itself (5xx, 400, a 200 without sound) is tried once more after ``RETRY_SECONDS`` with a new
session, unless the listener left or began another preview meanwhile; the second try is the same preview for the hour.
A 401, 403 or 404, no connection or no answer in time are said at once.

**Parts of a preview (Safari).** A browser that asks for the sound from the start (no ``Range``, or ``bytes=0-``) gets
it as it comes, without a length: it plays after the first piece. Safari asks for the first two bytes first and wants
the whole length in the answer; a browser that seeks asks for a part further in. Such a request gets the preview kept
whole on the server (up to ``MAX_BYTES``): nexmix reads it from Plex to the end, then answers ``206`` with
``Content-Range`` and ``Content-Length``. Every preview that ended whole is kept for ``KEEP_SECONDS`` per listener (at
most ``KEPT`` on the server), so the next parts come from memory: they count as the same preview, Plex is not asked
again, and nobody but the same listener reaches them (the checks above run at each request first).
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import threading
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session
from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from .. import clock
from ..errors import error
from ..models import CatalogTrack
from . import plex, plex_link, settings_service
from .sources import PlexSource

logger = logging.getLogger("nexmix.preview")

#: Where the preview starts, as a share of the track.
START_SHARE = 0.35
#: What the browser plays of it.
SECONDS = 30
BITRATE = 192
#: About forty seconds at 192 kbit/s: a little more than is played, never a whole track.
MAX_BYTES = 1_000_000
#: The longest a preview may take from Plex, whatever comes.
MAX_SECONDS = 90.0
PER_HOUR = 120
HOUR = 3600.0
#: Previews at once on the whole server.
SLOTS = 8
#: How long a whole preview is kept for the parts a browser asks for next, and how many are kept on the server (the
#: oldest goes first): at most ``KEPT`` × ``MAX_BYTES`` of memory.
KEEP_SECONDS = 180.0
KEPT = 16
#: The wait before the one more try after a refusal that may pass by itself.
RETRY_SECONDS = 0.5
#: How the wait is waited (the tests count it instead).
pause: Callable[[float], Awaitable[None]] = anyio.sleep
#: Of Plex's own words in a refusal, at most this many characters go to the log (read: at most ``_READ`` bytes).
DETAIL_CHARS = 200
_READ = 4096


@dataclass(frozen=True)
class Listener:
    """Who listens: ``key`` names the listener for the limits (``account:<id>``, ``guest:<id>``); ``server``, when set,
    the only server whose tracks it may hear (a guest: its playlist's)."""

    key: str
    kind: str = "account"
    server: int | None = None


def account_listener(request: Request, _db: Session, _track_id: int | None) -> Listener:
    """A signed-in account (a session or a device token): 401 otherwise, as at every route."""
    from ..deps import require_account

    account = require_account(request)
    return Listener(f"account:{account.id}")


#: May this request hear this track, and who listens? ``track_id`` None: may it listen in at all (the status). Raises
#: the answer otherwise (401, 403, 404). Replaced where another way in is added; the replacement falls back to
#: ``account_listener`` for a request that is none of its own.
may_listen: Callable[[Request, Session, int | None], Listener] = account_listener


# --- Limits ----------------------------------------------------------------------------------------------------------


@dataclass
class Running:
    key: str
    #: Set when another preview of the same listener takes its place: this one ends.
    replaced: asyncio.Event = field(default_factory=asyncio.Event)
    #: The loop the preview runs on (the event is set from there).
    loop: asyncio.AbstractEventLoop | None = None

    def end(self) -> None:
        if self.loop is None:
            return
        try:
            self.loop.call_soon_threadsafe(self.replaced.set)
        except RuntimeError:
            # Its loop is closed: it ended with it.
            pass


@dataclass
class Kept:
    """A preview that ended whole, for the parts the same listener asks for next."""

    key: str
    track: int
    data: bytes
    made: float


_lock = threading.Lock()
_hours: dict[str, deque[float]] = {}
_running: dict[str, Running] = {}
#: Per listener its last whole preview (one: a new preview of the listener takes its place).
_kept: dict[str, Kept] = {}


def forget() -> None:
    """For the tests."""
    with _lock:
        _hours.clear()
        _running.clear()
        _kept.clear()


def _keep(key: str, track: int, data: bytes) -> None:
    if not data:
        return
    with _lock:
        _kept.pop(key, None)
        while len(_kept) >= KEPT:
            oldest = min(_kept.values(), key=lambda one: one.made)
            del _kept[oldest.key]
        _kept[key] = Kept(key, track, data, clock.monotonic())


def kept(key: str, track: int) -> bytes | None:
    """The listener's whole preview of this track, while it is kept."""
    moment = clock.monotonic()
    with _lock:
        for stale in [one.key for one in _kept.values() if moment - one.made >= KEEP_SECONDS]:
            del _kept[stale]
        found = _kept.get(key)
        return found.data if found is not None and found.track == track else None


def kept_now() -> int:
    with _lock:
        return len(_kept)


# --- Ranges ----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Part:
    """What a ``Range`` header asks for: from ``first`` to ``last`` (inclusive; None: to the end), or the last
    ``suffix`` bytes."""

    first: int = 0
    last: int | None = None
    suffix: int | None = None

    def bounds(self, total: int) -> tuple[int, int] | None:
        """The first and last byte within ``total``; None when nothing of it lies there (416)."""
        if self.suffix is not None:
            if self.suffix <= 0 or total == 0:
                return None
            return max(0, total - self.suffix), total - 1
        if self.first >= total:
            return None
        last = total - 1 if self.last is None else min(self.last, total - 1)
        return self.first, last


_RANGE_MAX = 64


def asked_part(header: str | None) -> Part | None:
    """The one part a ``Range`` header asks for; None for the whole (no header, ``bytes=0-``, or a header this server
    does not serve in parts: several ranges, another unit, nonsense). A malformed header is ignored, as RFC 9110
    says."""
    if not header or len(header) > _RANGE_MAX:
        return None
    unit, _, spec = header.strip().partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None
    first, dash, last = spec.strip().partition("-")
    if not dash or not (first.isdigit() or last.isdigit()) or (first and not first.isdigit()) or (
            last and not last.isdigit()):
        return None
    if not first:
        return Part(suffix=int(last))
    start, end = int(first), int(last) if last else None
    if end is not None and end < start:
        return None
    if start == 0 and end is None:
        return None
    return Part(start, end)


def part_response(data: bytes, part: Part | None) -> Response:
    """A kept preview, whole (200) or the part asked for (206), always with its length; 416 for a part beyond it."""
    headers = {"Cache-Control": "no-store", "Accept-Ranges": "bytes"}
    total = len(data)
    if part is None:
        return Response(data, status_code=200, headers=headers, media_type="audio/mpeg")
    bounds = part.bounds(total)
    if bounds is None:
        return Response(b"", status_code=416, headers={**headers, "Content-Range": f"bytes */{total}"})
    first, last = bounds
    return Response(data[first:last + 1], status_code=206, media_type="audio/mpeg",
                    headers={**headers, "Content-Range": f"bytes {first}-{last}/{total}"})


def _window(key: str, moment: float) -> deque[float]:
    seen = _hours.setdefault(key, deque())
    while seen and moment - seen[0] >= HOUR:
        seen.popleft()
    return seen


def left(key: str) -> tuple[int, int | None]:
    """How many previews are left this hour, and in how many seconds the next one is (None: now)."""
    moment = clock.monotonic()
    with _lock:
        seen = _window(key, moment)
        count = len(seen)
        wait = max(1, int(HOUR - (moment - seen[0])) + 1) if count >= PER_HOUR else None
    return max(0, PER_HOUR - count), wait


def _take_hour(key: str) -> None:
    moment = clock.monotonic()
    with _lock:
        seen = _window(key, moment)
        if len(seen) >= PER_HOUR:
            wait = max(1, int(HOUR - (moment - seen[0])) + 1)
            exc = error("preview_too_often", "Too many previews this hour.", 429, retry_after=wait)
            exc.headers = {"Retry-After": str(wait)}
            raise exc
        seen.append(moment)


def _claim(key: str) -> Running:
    """The listener's place: one before it ends; a full server says so (503)."""
    with _lock:
        before = _running.pop(key, None)
        if before is not None:
            before.end()
        if len(_running) >= SLOTS:
            exc = error("preview_busy", "Too many previews at once. Try again in a moment.", 503)
            exc.headers = {"Retry-After": "5"}
            raise exc
        running = Running(key, loop=asyncio.get_running_loop())
        _running[key] = running
        return running


def _release(running: Running) -> None:
    with _lock:
        if _running.get(running.key) is running:
            del _running[running.key]


def running_now() -> int:
    with _lock:
        return len(_running)


# --- The way to Plex -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """Everything a preview needs, read from the database before the first byte."""

    listener: Listener
    url: str
    token: str
    client_id: str
    track: int
    offset: int
    #: The track's key on its server.
    key: str = ""


def allowed(db: Session) -> bool:
    return bool(settings_service.get(db, "preview_allowed"))


def status(request: Request) -> dict[str, Any]:
    """Whether the asking listener may listen in now, and how many previews are left this hour."""
    from ..db import SessionLocal

    with SessionLocal() as db:
        listener = may_listen(request, db, None)
        on = allowed(db)
    remaining, wait = left(listener.key)
    return {"allowed": on, "seconds": SECONDS, "per_hour": PER_HOUR, "left": remaining, "retry_after": wait}


def prepare(request: Request, track_id: int) -> Plan:
    """The checks, in the order that tells nobody more than they may know: who (401), the switch (403), the track in
    the catalogue (404)."""
    from ..db import SessionLocal
    from . import catalog, media

    with SessionLocal() as db:
        listener = may_listen(request, db, track_id)
        if not allowed(db):
            raise error("preview_off", "Listening in is switched off.", 403)
        track = None
        for server, generation in catalog.live(db).items():
            if listener.server is None or server == listener.server:
                track = db.get(CatalogTrack, (server, generation, track_id)) or track
        row = media.get(db, track.server_id) if track is not None else None
        keys = media.keys(db, row.id, media.TRACK, [track_id]) if row is not None else {}
        if track is None or row is None or track_id not in keys:
            raise error("not_found", "Not found.", 404)
        try:
            token = plex_link.token_of(row)
        except plex_link.Unreadable as exc:
            raise error("token_unreadable", "Connect Plex again.", 409) from exc
        client_id = plex_link.client_id(db)
        url = row.url
        seconds = max(0, int((track.duration or 0) / 1000))
    return Plan(listener, url, token, client_id, track_id, int(seconds * START_SHARE), keys[track_id])


def _status_error(status: int) -> HTTPException:
    if status in (401, 403):
        return error("plex_refused", "Plex did not play along.", 502)
    if status == 404:
        return error("not_found", "Not found.", 404)
    if status == 503:
        return error("plex_busy", "Plex is busy. Try again in a moment.", 503)
    return error("plex_error", "Plex did not play along.", 502)


def _status_kind(status: int) -> str:
    if status in (401, 403):
        return "plex_refused"
    if status == 404:
        return "plex_not_found"
    if status == 503:
        return "plex_busy"
    return "plex_error"


def _passes_by_itself(status: int) -> bool:
    """A refusal worth one more try: the converter confused or busy for a moment. Never a token or a track Plex does
    not take (401, 403, 404): asking again changes nothing there."""
    return status == 400 or status >= 500


_MARKUP = re.compile(r"<[^>]*>")
#: An address, or anything with parameters after it: Plex may repeat the request it refused, with the token in it.
_ADDRESS = re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://\S*|\S*\?\S*")
_PLEX_FIELD = re.compile(r"(?i)x-plex-[a-z-]+\s*[:=]\s*\S*")
_UNSEEN = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def clean_detail(text: str, token: str) -> str:
    """Plex's own words for the log: no markup, no address with its parameters, no ``X-Plex-…`` header or parameter,
    never the token, one line, at most ``DETAIL_CHARS`` characters."""
    text = _MARKUP.sub(" ", text)
    if token:
        text = text.replace(token, "…")
    text = _ADDRESS.sub("…", text)
    text = _PLEX_FIELD.sub("…", text)
    text = _UNSEEN.sub(" ", text)
    text = " ".join(text.split())
    return text[:DETAIL_CHARS]


@dataclass(frozen=True)
class Refusal:
    """Plex did not give the sound: how (``kind``), Plex's status (None: no answer), its words or the type of the
    error (``detail``), whether one more try may help, and the answer the listener gets."""

    kind: str
    status: int | None
    detail: str
    retry: bool
    answer: HTTPException


def _log_refusal(plan: Plan, refusal: Refusal, attempt: int | str, began: float) -> None:
    """``attempt`` 1 or 2 before the sound, ``-`` once it was flowing."""
    took = max(0, int((clock.monotonic() - began) * 1000))
    logger.warning("Plex refused a preview track=%s attempt=%s status=%s kind=%s ms=%s detail=%r", plan.track, attempt,
                   refusal.status if refusal.status is not None else "-", refusal.kind, took, refusal.detail)


async def _stop(client: httpx.AsyncClient, plan: Plan, session: str) -> None:
    try:
        url, headers, params = PlexSource.sound_stop(plan.url, plan.token, plan.client_id, session)
        await client.get(url, params=params, headers=headers, timeout=5.0)
    except httpx.HTTPError:
        logger.info("Plex did not take the end of a preview")


async def _close(client: httpx.AsyncClient, plan: Plan, session: str, upstream: httpx.Response | None = None) -> None:
    with anyio.CancelScope(shield=True):
        if upstream is not None:
            await upstream.aclose()
        await _stop(client, plan, session)
        await client.aclose()


async def _words(upstream: httpx.Response) -> bytes:
    """The beginning of what Plex said with a refusal, for the log; nothing when it does not come at once."""
    read = bytearray()
    try:
        with anyio.move_on_after(2.0):
            async for chunk in upstream.aiter_bytes():
                read += chunk
                if len(read) >= _READ:
                    break
    except httpx.HTTPError:
        pass
    return bytes(read[:_READ])


async def _ask(client: httpx.AsyncClient, plan: Plan, session: str
               ) -> tuple[httpx.Response, AsyncIterator[bytes], bytes] | Refusal:
    """One request to Plex's converter: the answer, its stream and its first piece of sound, or how Plex refused. A
    refusal has closed everything and told Plex the session is over."""
    upstream: httpx.Response | None = None
    try:
        url, headers, params = PlexSource.sound_start(plan.url, plan.token, plan.client_id, plan.key, plan.offset,
                                                      session, BITRATE)
        request = client.build_request("GET", url, headers=headers, params=params)
        upstream = await client.send(request, stream=True)
        if upstream.status_code != 200:
            said = (await _words(upstream)).decode("utf-8", "replace")
            refusal = Refusal(_status_kind(upstream.status_code), upstream.status_code, clean_detail(said, plan.token),
                              _passes_by_itself(upstream.status_code), _status_error(upstream.status_code))
            await _close(client, plan, session, upstream)
            return refusal
        # The first piece of sound before the listener hears a 200: an answer without sound is a refusal too.
        stream = upstream.aiter_bytes()
        first = b""
        async for chunk in stream:
            if chunk:
                first = chunk
                break
        if not first:
            await _close(client, plan, session, upstream)
            return Refusal("plex_empty", 200, "", True, error("plex_error", "Plex did not play along.", 502))
        return upstream, stream, first
    except BaseException as exc:
        await _close(client, plan, session, upstream)
        if isinstance(exc, httpx.TimeoutException):
            return Refusal("plex_timeout", None, type(exc).__name__, False,
                           error("plex_timeout", "Plex did not answer in time.", 504))
        if isinstance(exc, httpx.HTTPError):
            return Refusal("plex_unreachable", None, type(exc).__name__, False,
                           error("plex_unreachable", "Plex could not be reached.", 502))
        raise


async def start(plan: Plan, range_header: str | None = None,
                gone: Callable[[], Awaitable[bool]] | None = None) -> Response:
    """A request with a ``Range`` header is answered from the listener's kept preview of this track when there is one
    (a browser's next request of the same preview). Else nexmix asks Plex for the sound and answers with it as it
    comes, or, for a part, whole and then the part (``PreviewResponse``). A request without the header (a program)
    is always a new preview. ``gone`` says whether the listener has left: then a refusal is not tried again."""
    part = asked_part(range_header)
    data = kept(plan.listener.key, plan.track) if range_header else None
    if data is not None:
        return part_response(data, part)
    _take_hour(plan.listener.key)
    try:
        running = _claim(plan.listener.key)
    except HTTPException:
        # A full server is not the listener's doing: the hour keeps the preview that did not happen.
        with _lock:
            seen = _hours.get(plan.listener.key)
            if seen:
                seen.pop()
        raise
    try:
        attempt = 0
        while True:
            attempt += 1
            session = secrets.token_hex(12)
            client = plex.async_client(15.0)
            began = clock.monotonic()
            asked = await _ask(client, plan, session)
            if not isinstance(asked, Refusal):
                upstream, stream, first = asked
                return PreviewResponse(plan, session, client, upstream, running, part, stream, first)
            _log_refusal(plan, asked, attempt, began)
            if attempt > 1 or not asked.retry or running.replaced.is_set() or (gone is not None and await gone()):
                raise asked.answer
            await pause(RETRY_SECONDS)
            if running.replaced.is_set() or (gone is not None and await gone()):
                raise asked.answer
    except BaseException:
        _release(running)
        raise


#: How a preview that is kept may have ended: everything Plex sent, the cut at ``MAX_BYTES``, the deadline.
_WHOLE = ("done", "full", "late")


class PreviewResponse(Response):
    """The sound as Plex sends it, until ``MAX_BYTES`` or ``MAX_SECONDS``; the listener leaving, or another preview of
    theirs, ends it early. Plex hears of the end in every case.

    ``part`` None: passed on as it comes, without a length. A part: read whole first, then answered with its length
    (``part_response``). Either way a preview that ended whole is kept for the listener's next requests."""

    media_type = "audio/mpeg"

    def __init__(self, plan: Plan, session: str, client: httpx.AsyncClient, upstream: httpx.Response,
                 running: Running, part: Part | None = None, stream: AsyncIterator[bytes] | None = None,
                 first: bytes = b"") -> None:
        super().__init__(status_code=200, headers={"Cache-Control": "no-store", "Accept-Ranges": "none"},
                         media_type=self.media_type)
        # The length is not known before the end: no Content-Length (an empty body would have said 0).
        self.raw_headers = [(name, value) for name, value in self.raw_headers if name != b"content-length"]
        self.plan = plan
        self.session = session
        self.client = client
        self.upstream = upstream
        self.running = running
        self.part = part
        #: The answer's stream, of which ``first`` was read already (``_ask``).
        self.stream = stream if stream is not None else upstream.aiter_bytes()
        self.first = first
        self.started = clock.monotonic()
        #: What was read from Plex (passed on as it came, or kept for the part), and why it ended (for the log and the
        #: tests).
        self.sent = 0
        self.ended = ""

    async def _sound(self) -> AsyncIterator[bytes]:
        if self.first:
            yield self.first
        async for chunk in self.stream:
            yield chunk

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        gone = False
        whole = bytearray()
        streaming = self.part is None
        try:
            if streaming:
                await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
            async with anyio.create_task_group() as group:

                async def listener_left() -> None:
                    nonlocal gone
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            break
                    gone = True
                    self.ended = self.ended or "left"
                    group.cancel_scope.cancel()

                async def replaced() -> None:
                    await self.running.replaced.wait()
                    self.ended = self.ended or "replaced"
                    group.cancel_scope.cancel()

                group.start_soon(listener_left)
                group.start_soon(replaced)
                # Nothing is raised out of the group (it would come wrapped): a failure ends the stream and says how.
                try:
                    with anyio.move_on_after(MAX_SECONDS) as deadline:
                        async for chunk in self._sound():
                            piece = chunk[: MAX_BYTES - self.sent]
                            if piece:
                                whole += piece
                                if streaming:
                                    await send({"type": "http.response.body", "body": piece, "more_body": True})
                                self.sent += len(piece)
                            if self.sent >= MAX_BYTES:
                                self.ended = self.ended or "full"
                                break
                    if deadline.cancelled_caught:
                        self.ended = self.ended or "late"
                except OSError:
                    # The listener's connection broke while a piece went out.
                    gone = True
                    self.ended = self.ended or "left"
                except httpx.HTTPError as exc:
                    self.ended = self.ended or "plex"
                    _log_refusal(self.plan, Refusal("plex_broke", 200, type(exc).__name__, False,
                                                    _status_error(502)), "-", self.started)
                self.ended = self.ended or "done"
                group.cancel_scope.cancel()
            if self.ended in _WHOLE:
                _keep(self.plan.listener.key, self.plan.track, bytes(whole))
            if not gone and streaming:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            elif not gone:
                if whole:
                    answer = part_response(bytes(whole), self.part)
                else:
                    empty = Refusal("plex_empty", 200, "", False, _status_error(502))
                    _log_refusal(self.plan, empty, "-", self.started)
                    answer = Response(b"", status_code=502, headers={"Cache-Control": "no-store"})
                await answer(scope, receive, send)
        except OSError:
            self.ended = self.ended or "left"
        finally:
            with anyio.CancelScope(shield=True):
                await self.upstream.aclose()
                await _stop(self.client, self.plan, self.session)
                await self.client.aclose()
            _release(self.running)
            logger.debug("Preview ended how=%s bytes=%s", self.ended, self.sent)
