"""Listening in against the stand-in Plex: the request to Plex's converter (35 % in, 192 kbit/s, nothing from the
browser but the track), the cut at 1 MB, Plex told to stop when it ends, when the listener leaves and when a new preview
of the same listener begins; the limits (one at a time, 120 an hour, the server's slots); who may (a sign-in, the
operator's switch, a track of the catalogue) and the place where another way in hangs itself."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import clock
from app.db import SessionLocal
from app.main import app
from app.models import Account
from app.security import SESSION_COOKIE, start_session
from app.services import jobs, preview

from .conftest import make_account, new_client
from .fakeplex import FakePlex
from .fakeplex.sound import FRAME

TRACK = 3001  # 200 seconds long in the stand-in library


@pytest.fixture
def synced(client: TestClient, operator: Account, fake_plex: FakePlex) -> FakePlex:
    answer = client.post("/api/v1/plex/connect", json={"url": "http://127.0.0.1:32400", "token": fake_plex.account_token})
    assert answer.status_code == 200, answer.text
    assert jobs.run_pending(lane="catalog") == 1
    return fake_plex


def test_a_preview_asks_plex_for_a_third_of_the_way_in_and_ends_the_session(client: TestClient,
                                                                           synced: FakePlex) -> None:
    synced.sound_bytes = 200_000
    answer = client.get(f"/api/v1/preview/{TRACK}")
    assert answer.status_code == 200
    assert answer.headers["content-type"] == "audio/mpeg"
    assert answer.headers["cache-control"] == "no-store"
    assert answer.content[:4] == FRAME[:4] and len(answer.content) == 200_000
    [asked] = synced.starts
    assert asked["path"] == f"/library/metadata/{TRACK}"
    assert asked["offset"] == "70", "35 % of 200 seconds"
    assert asked["musicBitrate"] == asked["maxAudioBitrate"] == "192"
    assert set(asked) == {"path", "mediaIndex", "partIndex", "protocol", "directPlay", "directStream", "musicBitrate",
                          "maxAudioBitrate", "offset", "session", "X-Plex-Session-Identifier", "location", "hasMDE"}
    # The token goes in a header, never in the address; and Plex hears that the session is over.
    assert synced.stops == [asked["session"]]
    assert synced.account_token not in str(answer.headers)
    assert preview.running_now() == 0


def test_a_preview_stops_at_one_megabyte(client: TestClient, synced: FakePlex) -> None:
    synced.sound_bytes = 3_000_000
    answer = client.get(f"/api/v1/preview/{TRACK}")
    assert answer.status_code == 200
    assert len(answer.content) == preview.MAX_BYTES
    assert synced.stops == [synced.starts[0]["session"]]


def test_without_a_sign_in_401_and_a_track_not_in_the_catalogue_404(synced: FakePlex) -> None:
    with new_client() as stranger:
        assert stranger.get(f"/api/v1/preview/{TRACK}").status_code == 401
        assert stranger.get("/api/v1/preview").status_code == 401
    with new_client(make_account("mia")) as member:
        assert member.get("/api/v1/preview/999999").status_code == 404
        # An album or an artist is no track: its id is not a way to Plex either.
        assert member.get("/api/v1/preview/2001").status_code == 404
        assert member.get("/api/v1/preview/1001").status_code == 404
        assert member.get("/api/v1/preview/0").status_code == 422
        assert member.get("/api/v1/preview/abc").status_code == 422
    assert synced.starts == []


def test_a_member_may_listen_and_a_device_too(client: TestClient, synced: FakePlex) -> None:
    synced.sound_bytes = 10_000
    member = make_account("mia")
    with new_client(member) as browser:
        assert browser.get(f"/api/v1/preview/{TRACK}").status_code == 200
        token = browser.post("/api/v1/devices", json={"name": "phone"}).json()["token"]
    with TestClient(app, base_url="http://testserver") as device:
        answer = device.get(f"/api/v1/preview/{TRACK}", headers={"Authorization": f"Bearer {token}"})
        assert answer.status_code == 200 and len(answer.content) == 10_000
        wrong = device.get(f"/api/v1/preview/{TRACK}", headers={"Authorization": "Bearer nmd_" + "w" * 43})
        assert wrong.status_code == 401


def test_the_operators_switch_turns_listening_off_for_everybody(client: TestClient, synced: FakePlex) -> None:
    assert client.get("/api/v1/settings").json()["preview_allowed"] is True
    assert client.put("/api/v1/settings", json={"preview_allowed": False}).json()["preview_allowed"] is False
    with new_client(make_account("mia")) as member:
        answer = member.get(f"/api/v1/preview/{TRACK}")
        assert answer.status_code == 403 and answer.json()["detail"]["code"] == "preview_off"
        assert member.get("/api/v1/preview").json()["allowed"] is False
        # A member may not turn it on again.
        assert member.put("/api/v1/settings", json={"preview_allowed": True}).status_code == 403
    with new_client() as stranger:
        assert stranger.get(f"/api/v1/preview/{TRACK}").status_code == 401, "who asks comes first"
    assert synced.starts == []
    client.put("/api/v1/settings", json={"preview_allowed": True})
    assert client.get(f"/api/v1/preview/{TRACK}").status_code == 200


def test_120_an_hour_then_429_with_the_wait(client: TestClient, synced: FakePlex,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    synced.sound_bytes = 1_000
    moment = [1000.0]
    monkeypatch.setattr(clock, "monotonic", lambda: moment[0])
    for _ in range(preview.PER_HOUR):
        assert client.get(f"/api/v1/preview/{TRACK}").status_code == 200
        moment[0] += 1
    assert client.get("/api/v1/preview").json()["left"] == 0
    refused = client.get(f"/api/v1/preview/{TRACK}")
    assert refused.status_code == 429
    assert refused.json()["detail"]["code"] == "preview_too_often"
    # The first of the hour drops out 3600 s after it began: 120 seconds have passed.
    assert refused.headers["retry-after"] == str(3600 - 120 + 1)
    assert refused.json()["detail"]["retry_after"] == 3600 - 120 + 1
    assert len(synced.starts) == preview.PER_HOUR
    # Somebody else is not held up by it.
    with new_client(make_account("mia")) as member:
        assert member.get(f"/api/v1/preview/{TRACK}").status_code == 200
    moment[0] = 1000.0 + 3600
    assert client.get(f"/api/v1/preview/{TRACK}").status_code == 200


def test_the_status_says_what_is_left(client: TestClient, synced: FakePlex) -> None:
    synced.sound_bytes = 1_000
    before = client.get("/api/v1/preview").json()
    assert before == {"allowed": True, "seconds": 30, "per_hour": 120, "left": 120, "retry_after": None}
    client.get(f"/api/v1/preview/{TRACK}")
    assert client.get("/api/v1/preview").json()["left"] == 119


def test_plex_refusing_the_sound_is_said_and_the_session_ended(client: TestClient, synced: FakePlex,
                                                               waits: list[float]) -> None:
    synced.sound_status = 503
    answer = client.get(f"/api/v1/preview/{TRACK}")
    assert answer.status_code == 503 and answer.json()["detail"]["code"] == "plex_busy"
    # Busy is asked once more; both sessions are ended at Plex.
    assert len(synced.starts) == 2 and synced.stops == [one["session"] for one in synced.starts]
    assert preview.running_now() == 0
    synced.sound_status = 401
    answer = client.get(f"/api/v1/preview/{TRACK}")
    assert answer.status_code == 502 and answer.json()["detail"]["code"] == "plex_refused"
    assert "token" not in answer.text.lower()


# --- When Plex says no: one line in the log, and one more try where it may help -------------------------------------


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The wait before the second try, counted instead of waited."""
    seen: list[float] = []

    async def counted(seconds: float) -> None:
        seen.append(seconds)

    monkeypatch.setattr(preview, "pause", counted)
    return seen


def refusals(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records
            if record.name == "nexmix.preview" and record.levelno == logging.WARNING]


@pytest.mark.parametrize("bad", [(500, b"<html>Internal Server Error</html>"), (400, b"<html>400</html>"),
                                 (503, b"busy"), (200, b"")])
def test_a_refusal_that_may_pass_is_tried_once_more_and_the_sound_comes(
    client: TestClient, synced: FakePlex, waits: list[float], caplog: pytest.LogCaptureFixture,
    bad: tuple[int, bytes],
) -> None:
    from .fakeplex.sound import silence

    synced.sound_queue = [bad, (200, silence(20_000))]
    with caplog.at_level(logging.INFO, logger="nexmix.preview"):
        answer = client.get(f"/api/v1/preview/{TRACK}")
    assert answer.status_code == 200 and len(answer.content) == 20_000 and answer.content[:4] == FRAME[:4]
    first, second = synced.starts
    # A new session for the second try; Plex hears the end of both; half a second between them.
    assert first["session"] != second["session"] and synced.stops == [first["session"], second["session"]]
    assert waits == [preview.RETRY_SECONDS] and preview.RETRY_SECONDS == 0.5
    [line] = refusals(caplog)
    message = line.getMessage()
    kind = "plex_empty" if bad[0] == 200 else "plex_error" if bad[0] in (400, 500) else "plex_busy"
    assert f"track={TRACK}" in message and f"status={bad[0]}" in message and f"kind={kind}" in message
    assert "attempt=1" in message and re.search(r" ms=\d+ ", message)
    # One preview for the hour, not two.
    assert client.get("/api/v1/preview").json()["left"] == preview.PER_HOUR - 1
    assert preview.running_now() == 0


@pytest.mark.parametrize("ranged", [False, True])
def test_two_refusals_answer_502_with_two_lines(client: TestClient, synced: FakePlex, waits: list[float],
                                                caplog: pytest.LogCaptureFixture, ranged: bool) -> None:
    synced.sound_queue = [(500, b"<html><body><h1>Transcoder gave up</h1></body></html>"), (200, b"")]
    with caplog.at_level(logging.INFO, logger="nexmix.preview"):
        answer = client.get(f"/api/v1/preview/{TRACK}", headers={"Range": "bytes=0-1"} if ranged else {})
    assert answer.status_code == 502 and answer.json()["detail"]["code"] == "plex_error"
    assert len(synced.starts) == 2 and len(waits) == 1
    first, second = (line.getMessage() for line in refusals(caplog))
    assert "attempt=1" in first and "status=500" in first and "kind=plex_error" in first
    assert "detail='Transcoder gave up'" in first
    assert "attempt=2" in second and "status=200" in second and "kind=plex_empty" in second
    assert client.get("/api/v1/preview").json()["left"] == preview.PER_HOUR - 1
    assert preview.running_now() == 0 and preview.kept_now() == 0


@pytest.mark.parametrize(("status", "code", "kind"), [(401, "plex_refused", "plex_refused"),
                                                       (403, "plex_refused", "plex_refused"),
                                                       (404, "not_found", "plex_not_found")])
def test_a_refused_token_or_a_missing_track_is_not_asked_again(
    client: TestClient, synced: FakePlex, waits: list[float], caplog: pytest.LogCaptureFixture,
    status: int, code: str, kind: str,
) -> None:
    synced.sound_queue = [(status, b"<html>no</html>"), (200, FRAME * 10)]
    with caplog.at_level(logging.INFO, logger="nexmix.preview"):
        answer = client.get(f"/api/v1/preview/{TRACK}")
    assert answer.json()["detail"]["code"] == code
    assert len(synced.starts) == 1 and waits == []
    [line] = refusals(caplog)
    assert f"status={status}" in line.getMessage() and f"kind={kind}" in line.getMessage()


@pytest.mark.parametrize(("error", "status", "code", "kind"), [
    (httpx.ConnectError("refused"), 502, "plex_unreachable", "plex_unreachable"),
    (httpx.ReadTimeout("slow"), 504, "plex_timeout", "plex_timeout"),
])
def test_no_connection_or_no_answer_is_said_with_the_type_and_not_asked_again(
    client: TestClient, synced: FakePlex, waits: list[float], caplog: pytest.LogCaptureFixture,
    error: Exception, status: int, code: str, kind: str,
) -> None:
    synced.sound_queue = [error, (200, FRAME * 10)]
    with caplog.at_level(logging.INFO, logger="nexmix.preview"):
        answer = client.get(f"/api/v1/preview/{TRACK}")
    assert answer.status_code == status and answer.json()["detail"]["code"] == code
    assert len(synced.starts) == 1 and waits == []
    [line] = refusals(caplog)
    message = line.getMessage()
    assert "status=- " in message and f"kind={kind}" in message and f"detail='{type(error).__name__}'" in message
    assert synced.stops == [synced.starts[0]["session"]] and preview.running_now() == 0


def test_the_token_headers_and_the_address_never_reach_the_log(client: TestClient, synced: FakePlex,
                                                               waits: list[float],
                                                               caplog: pytest.LogCaptureFixture) -> None:
    """Plex may repeat the request it refused, token and all: the line keeps Plex's words, never the token, a header
    or the address with its parameters."""
    token = synced.account_token
    said = (f"<html><body><h1>400 Bad Request</h1><p>Cannot start {plex_sound_start()}?path=%2Flibrary%2Fmetadata%2F"
            f"{TRACK}&X-Plex-Token={token}&session=abc</p><p>X-Plex-Token: {token}</p>"
            f"<p>Client X-Plex-Client-Identifier={token[:6]}x</p><p>http://127.0.0.1:32400/x?X-Plex-Token={token}</p>"
            f"<p>bare {token} end</p>{'z' * 400}</body></html>").encode()
    synced.sound_queue = [(400, said), (400, said)]
    with caplog.at_level(logging.DEBUG):
        answer = client.get(f"/api/v1/preview/{TRACK}")
    assert answer.status_code == 502
    assert token not in caplog.text and token not in answer.text
    lines = [line.getMessage() for line in refusals(caplog)]
    assert len(lines) == 2
    for message in lines:
        assert "X-Plex" not in message and "?" not in message and "&" not in message and "32400" not in message
        detail = re.search(r"detail='(.*)'$", message)
        assert detail is not None and len(detail.group(1)) <= preview.DETAIL_CHARS
        assert detail.group(1).startswith("400 Bad Request Cannot start")
        assert "bare … end" in detail.group(1)
    assert synced.stops == [one["session"] for one in synced.starts]


def plex_sound_start() -> str:
    return "/music/:/transcode/universal/start.mp3"


def test_the_detail_is_cleaned() -> None:
    secret = secrets.token_urlsafe(16)
    assert preview.clean_detail(f"<b>no</b>\r\nmore\x00 {secret}", secret) == "no more …"
    assert preview.clean_detail("see https://plex.example.com:32400/a?b=c now", "") == "see … now"
    assert preview.clean_detail("x-plex-product=nexmix rest", "") == "… rest"
    assert len(preview.clean_detail("y" * 1000, "")) == preview.DETAIL_CHARS


async def test_a_listener_who_left_is_not_asked_for_again(synced: FakePlex, operator: Account,
                                                          waits: list[float]) -> None:
    synced.sound_queue = [(500, b"no"), (200, FRAME * 10)]
    plan = preview.Plan(preview.Listener(f"account:{operator.id}"), "http://127.0.0.1:32400",
                        synced.account_token, "client-x", TRACK, 70, str(TRACK))

    async def left() -> bool:
        return True

    with pytest.raises(HTTPException) as refused:
        await preview.start(plan, None, left)
    assert refused.value.status_code == 502
    assert len(synced.starts) == 1 and waits == [] and preview.running_now() == 0


async def test_another_preview_meanwhile_ends_the_second_try(synced: FakePlex, operator: Account,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    synced.sound_queue = [(500, b"no"), (200, FRAME * 10)]
    key = f"account:{operator.id}"
    plan = preview.Plan(preview.Listener(key), "http://127.0.0.1:32400", synced.account_token, "client-x", TRACK, 70,
                        str(TRACK))

    async def replaced_while_waiting(_seconds: float) -> None:
        with preview._lock:
            preview._running[key].end()
        await asyncio.sleep(0)

    monkeypatch.setattr(preview, "pause", replaced_while_waiting)
    with pytest.raises(HTTPException):
        await preview.start(plan)
    assert len(synced.starts) == 1 and preview.running_now() == 0


def test_a_full_server_says_busy_and_keeps_the_hour(client: TestClient, synced: FakePlex,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preview, "SLOTS", 0)
    answer = client.get(f"/api/v1/preview/{TRACK}")
    assert answer.status_code == 503 and answer.json()["detail"]["code"] == "preview_busy"
    assert client.get("/api/v1/preview").json()["left"] == preview.PER_HOUR
    assert synced.starts == []


# --- The listener leaves, or begins another one ----------------------------------------------------------------------


def _session_of(account: Account) -> str:
    with SessionLocal() as db:
        row = db.get(Account, account.id)
        assert row is not None
        return start_session(db, row, "127.0.0.1", "tests")


async def _endless() -> AsyncIterator[bytes]:
    """About 200 kB a second: a megabyte takes five seconds, longer than any of these tests waits."""
    for _ in range(100_000):
        yield FRAME * 10
        await asyncio.sleep(0.02)


class Ear:
    """A browser at the level of the server's interface: it can go away after the first piece of sound (or never)."""

    def __init__(self, cookie: str, leave_after: int | None) -> None:
        self.cookie = cookie
        self.leave_after = leave_after
        self.left = asyncio.Event()
        self.heard = 0
        self.pieces = 0
        self.status = 0
        self.first = asyncio.Event()
        self.complete = False
        self.headers: dict[bytes, bytes] = {}
        #: A ``Range`` header to send, if any.
        self.range: str | None = None

    async def listen(self, track: int) -> None:
        path = f"/api/v1/preview/{track}"
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
            "root_path": "", "client": ("127.0.0.1", 50000), "server": ("testserver", 80),
            "headers": [(b"host", b"testserver"), (b"cookie", f"{SESSION_COOKIE}={self.cookie}".encode())]
            + ([(b"range", self.range.encode())] if self.range else []),
        }
        asked = False

        async def receive() -> dict[str, object]:
            nonlocal asked
            if not asked:
                asked = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await self.left.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            if message["type"] == "http.response.start":
                self.status = int(message["status"])  # type: ignore[call-overload]
                self.headers = {bytes(name).lower(): bytes(value) for name, value in message["headers"]}  # type: ignore[attr-defined]
            elif message["type"] == "http.response.body":
                body = message.get("body") or b""
                self.heard += len(body)  # type: ignore[arg-type]
                if body:
                    self.pieces += 1
                    self.first.set()
                if not message.get("more_body"):
                    self.complete = True
                if self.leave_after is not None and self.pieces >= self.leave_after:
                    self.left.set()

        await app(scope, receive, send)


@pytest.fixture
def endless(synced: FakePlex) -> Iterator[FakePlex]:
    synced.sound_stream = _endless
    yield synced


async def test_the_listener_leaving_ends_the_preview_and_tells_plex(endless: FakePlex, operator: Account) -> None:
    ear = Ear(_session_of(operator), leave_after=3)
    await asyncio.wait_for(ear.listen(TRACK), 10)
    assert ear.status == 200 and ear.pieces >= 3
    # The length is not known in advance: a server would refuse a body longer than a length said.
    assert b"content-length" not in ear.headers and ear.headers[b"content-type"] == b"audio/mpeg"
    assert ear.heard < preview.MAX_BYTES and not ear.complete
    assert endless.stops == [endless.starts[0]["session"]]
    assert preview.running_now() == 0


async def test_a_new_preview_of_the_same_listener_ends_the_one_before(endless: FakePlex, operator: Account) -> None:
    first = Ear(_session_of(operator), leave_after=None)
    playing = asyncio.create_task(first.listen(TRACK))
    await asyncio.wait_for(first.first.wait(), 10)
    assert preview.running_now() == 1
    second = Ear(_session_of(operator), leave_after=2)
    await asyncio.wait_for(second.listen(3003), 10)
    # The first one ended by itself, long before its megabyte, with its stream closed properly, and Plex heard of both.
    await asyncio.wait_for(playing, 2)
    assert first.complete and first.heard < preview.MAX_BYTES
    assert sorted(endless.stops) == sorted(one["session"] for one in endless.starts)
    assert len(endless.starts) == 2 and preview.running_now() == 0


async def test_two_listeners_play_side_by_side(endless: FakePlex, operator: Account) -> None:
    other = make_account("mia")
    first = Ear(_session_of(operator), leave_after=None)
    playing = asyncio.create_task(first.listen(TRACK))
    await asyncio.wait_for(first.first.wait(), 10)
    second = Ear(_session_of(other), leave_after=2)
    await asyncio.wait_for(second.listen(TRACK), 10)
    assert preview.running_now() == 1 and not playing.done()
    first.left.set()
    await asyncio.wait_for(playing, 10)
    assert len(endless.stops) == 2


# --- Another way in --------------------------------------------------------------------------------------------------


def test_another_way_in_hangs_itself_in_and_falls_back(client: TestClient, synced: FakePlex,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """What a guest of an invitation will do: a function of its own in ``may_listen`` that knows its requests and leaves
    every other one to the account's."""
    synced.sound_bytes = 1_000
    fallback = preview.may_listen

    def guests_too(request, db, track_id):  # type: ignore[no-untyped-def]
        if request.headers.get("x-test-guest") == "lena":
            if track_id == 3003:
                from app.errors import error

                raise error("not_found", "Not found.", 404)
            return preview.Listener("guest:7", "guest")
        return fallback(request, db, track_id)

    monkeypatch.setattr(preview, "may_listen", guests_too)
    with new_client() as guest:
        assert guest.get(f"/api/v1/preview/{TRACK}", headers={"X-Test-Guest": "lena"}).status_code == 200
        assert guest.get("/api/v1/preview/3003", headers={"X-Test-Guest": "lena"}).status_code == 404
        assert guest.get(f"/api/v1/preview/{TRACK}").status_code == 401
        assert guest.get("/api/v1/preview", headers={"X-Test-Guest": "lena"}).json()["left"] == 119
    assert client.get(f"/api/v1/preview/{TRACK}").status_code == 200
    assert client.get("/api/v1/preview").json()["left"] == 119, "the guest's hour is the guest's"


# --- Parts of a preview: what Safari asks for ------------------------------------------------------------------------


def test_safari_asks_for_two_bytes_then_the_rest_and_plex_is_asked_once(client: TestClient, synced: FakePlex) -> None:
    synced.sound_bytes = 300_000
    probe = client.get(f"/api/v1/preview/{TRACK}", headers={"Range": "bytes=0-1"})
    assert probe.status_code == 206
    assert probe.headers["content-range"] == "bytes 0-1/300000"
    assert probe.headers["content-length"] == "2" and probe.content == FRAME[:2]
    assert probe.headers["accept-ranges"] == "bytes" and probe.headers["cache-control"] == "no-store"
    assert probe.headers["content-type"] == "audio/mpeg"
    rest = client.get(f"/api/v1/preview/{TRACK}", headers={"Range": "bytes=0-299999"})
    assert rest.status_code == 206 and len(rest.content) == 300_000
    assert rest.headers["content-range"] == "bytes 0-299999/300000"
    middle = client.get(f"/api/v1/preview/{TRACK}", headers={"Range": "bytes=1000-1999"})
    assert middle.status_code == 206 and middle.content == rest.content[1000:2000]
    tail = client.get(f"/api/v1/preview/{TRACK}", headers={"Range": "bytes=-500"})
    assert tail.status_code == 206 and tail.headers["content-range"] == "bytes 299500-299999/300000"
    from_start = client.get(f"/api/v1/preview/{TRACK}", headers={"Range": "bytes=0-"})
    assert from_start.status_code == 200 and from_start.headers["content-length"] == "300000"
    # One preview: Plex converted once and heard of its end once, and the hour counts one.
    assert len(synced.starts) == 1 and synced.stops == [synced.starts[0]["session"]]
    assert client.get("/api/v1/preview").json()["left"] == preview.PER_HOUR - 1
    assert preview.running_now() == 0


def test_a_part_beyond_the_end_is_416_with_the_length(client: TestClient, synced: FakePlex) -> None:
    synced.sound_bytes = 10_000
    assert client.get(f"/api/v1/preview/{TRACK}", headers={"Range": "bytes=0-1"}).status_code == 206
    beyond = client.get(f"/api/v1/preview/{TRACK}", headers={"Range": "bytes=10000-20000"})
    assert beyond.status_code == 416 and beyond.headers["content-range"] == "bytes */10000"
    assert len(synced.starts) == 1


def test_a_preview_heard_as_it_came_answers_the_next_parts_from_memory(client: TestClient, synced: FakePlex) -> None:
    synced.sound_bytes = 50_000
    first = client.get(f"/api/v1/preview/{TRACK}", headers={"Range": "bytes=0-"})
    assert first.status_code == 200 and "content-length" not in first.headers and len(first.content) == 50_000
    seek = client.get(f"/api/v1/preview/{TRACK}", headers={"Range": "bytes=40000-"})
    assert seek.status_code == 206 and seek.content == first.content[40000:]
    assert len(synced.starts) == 1


def test_a_program_without_a_range_always_gets_a_new_preview(client: TestClient, synced: FakePlex) -> None:
    synced.sound_bytes = 5_000
    assert client.get(f"/api/v1/preview/{TRACK}").status_code == 200
    assert client.get(f"/api/v1/preview/{TRACK}").status_code == 200
    assert len(synced.starts) == 2


def test_the_kept_preview_is_the_listeners_own_and_of_this_track_only(client: TestClient, synced: FakePlex) -> None:
    synced.sound_bytes = 20_000
    assert client.get(f"/api/v1/preview/{TRACK}", headers={"Range": "bytes=0-1"}).status_code == 206
    with new_client(make_account("mia")) as other:
        assert other.get(f"/api/v1/preview/{TRACK}", headers={"Range": "bytes=0-1"}).status_code == 206
    assert len(synced.starts) == 2, "another listener is a preview of its own"
    assert client.get("/api/v1/preview/3003", headers={"Range": "bytes=0-1"}).status_code == 206
    assert len(synced.starts) == 3, "another track too"
    # The new track took the place of the first one: the first is asked from Plex again.
    assert client.get(f"/api/v1/preview/{TRACK}", headers={"Range": "bytes=2-3"}).status_code == 206
    assert len(synced.starts) == 4


def test_a_kept_preview_goes_after_its_time_and_never_more_than_the_limit(client: TestClient, synced: FakePlex,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    synced.sound_bytes = 2_000
    moment = [500.0]
    monkeypatch.setattr(clock, "monotonic", lambda: moment[0])
    assert client.get(f"/api/v1/preview/{TRACK}", headers={"Range": "bytes=0-1"}).status_code == 206
    moment[0] += preview.KEEP_SECONDS - 1
    assert client.get(f"/api/v1/preview/{TRACK}", headers={"Range": "bytes=0-1"}).status_code == 206
    assert len(synced.starts) == 1
    moment[0] += 2
    assert client.get(f"/api/v1/preview/{TRACK}", headers={"Range": "bytes=0-1"}).status_code == 206
    assert len(synced.starts) == 2
    monkeypatch.setattr(preview, "KEPT", 3)
    for number in range(5):
        moment[0] += 1
        preview._keep(f"account:{900 + number}", TRACK, b"x" * 10)
    assert preview.kept_now() == 3
    assert preview.kept("account:904", TRACK) == b"x" * 10 and preview.kept("account:900", TRACK) is None


def test_the_rights_are_checked_before_a_kept_preview_is_handed_out(client: TestClient, synced: FakePlex) -> None:
    synced.sound_bytes = 2_000
    assert client.get(f"/api/v1/preview/{TRACK}", headers={"Range": "bytes=0-1"}).status_code == 206
    client.put("/api/v1/settings", json={"preview_allowed": False})
    off = client.get(f"/api/v1/preview/{TRACK}", headers={"Range": "bytes=0-1"})
    assert off.status_code == 403 and off.json()["detail"]["code"] == "preview_off"
    with new_client() as stranger:
        assert stranger.get(f"/api/v1/preview/{TRACK}", headers={"Range": "bytes=0-1"}).status_code == 401


@pytest.mark.parametrize(("header", "expected"), [
    (None, None), ("", None), ("bytes=0-", None), ("bytes=0-1", preview.Part(0, 1)), ("bytes=5-", preview.Part(5)),
    ("bytes=-20", preview.Part(suffix=20)), ("BYTES = 3-4", preview.Part(3, 4)), ("bytes=0-1,5-6", None),
    ("items=0-1", None), ("bytes=4-2", None), ("bytes=a-b", None), ("bytes=-", None), ("bytes=1", None),
    ("bytes=" + "9" * 80 + "-", None),
])
def test_the_range_header_is_read_strictly(header: str | None, expected: preview.Part | None) -> None:
    assert preview.asked_part(header) == expected


async def test_a_listener_leaving_while_the_whole_is_read_keeps_nothing(endless: FakePlex, operator: Account) -> None:
    ear = Ear(_session_of(operator), leave_after=None)
    ear.range = "bytes=0-1"
    listening = asyncio.create_task(ear.listen(TRACK))
    await asyncio.sleep(0.3)
    ear.left.set()
    await asyncio.wait_for(listening, 10)
    assert ear.status == 0, "nothing was answered before the whole was there"
    assert endless.stops == [endless.starts[0]["session"]]
    assert preview.kept_now() == 0 and preview.running_now() == 0
