"""A Plex that is slow or down for covers must never hold up the rest of nexmix.

Every cover not kept on disk asks Plex. Before, it did so in a thread of the server's shared pool and waited there for
one of the few places for covers: a page full of covers filled the pool, and every other route (the playlists,
signing in) waited behind them. Now a cover waits for its place on the event loop, a short while only, and asks Plex
in threads of its own. A Plex that fails is left alone for a while, with one line in the log for the outage."""

from __future__ import annotations

import logging
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import clock
from app.models import Account
from app.services import covers, plex

from .fakeplex import FakePlex
from .plex_world import connect

PLEX_SECONDS = 3.0


def _photo_calls(fake: FakePlex) -> int:
    return sum(1 for _method, _host, path, _params in list(fake.calls) if path == "/photo/:/transcode")


def _albums(client: TestClient) -> list[int]:
    found = [entry["id"] for entry in client.get("/api/v1/catalog/search?kind=albums&q=").json().get("albums", [])]
    assert found, "the made-up library has albums"
    return found


def test_covers_from_a_slow_plex_do_not_hold_up_the_rest(client: TestClient, operator: Account,
                                                         fake_plex: FakePlex,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    connect(client, fake_plex)
    albums = _albums(client)
    inner = fake_plex.handle

    def slow(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/photo/"):
            time.sleep(PLEX_SECONDS)
        return inner(request)

    monkeypatch.setattr(plex, "TRANSPORT", httpx.MockTransport(slow))
    answers: list[int] = []

    def ask_cover(album: int, size: int) -> None:
        # The same client, so the same server loop and its one pool of threads, as for a real server.
        answers.append(client.get(f"/api/v1/catalog/cover/{album}?s={size}").status_code)

    workers = [threading.Thread(target=ask_cover, args=(albums[number % len(albums)], (80, 200, 400)[number % 3]))
               for number in range(60)]
    for one in workers:
        one.start()
    time.sleep(1.0)
    started = time.perf_counter()
    answer = client.get("/api/v1/playlists")
    took = time.perf_counter() - started
    for one in workers:
        one.join()
    assert answer.status_code == 200
    assert took < 1.0, f"the list of playlists waited {took:.1f} s behind the covers"
    # Those that found a place got their cover; the others were told to come back, none waited for Plex in a queue.
    assert set(answers) <= {200, 503}
    assert answers.count(200) <= covers.PLACES * 2
    assert _photo_calls(fake_plex) <= covers.PLACES * 2


def test_a_cover_waits_for_a_place_only_briefly(client: TestClient, operator: Account, fake_plex: FakePlex,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """All places taken: the next cover is answered 503 with Retry-After within about ``WAIT_SECONDS``."""
    connect(client, fake_plex)
    albums = _albums(client)
    for _ in range(covers.PLACES):
        assert covers._places.acquire(blocking=False)
    try:
        started = time.perf_counter()
        answer = client.get(f"/api/v1/catalog/cover/{albums[0]}?s=80")
        took = time.perf_counter() - started
    finally:
        for _ in range(covers.PLACES):
            covers._places.release()
    assert answer.status_code == 503
    assert answer.json()["detail"]["code"] == "plex_busy"
    assert int(answer.headers["retry-after"]) >= 1
    assert took < covers.WAIT_SECONDS + 1.0
    assert _photo_calls(fake_plex) == 0
    # A place free again: the cover comes.
    assert client.get(f"/api/v1/catalog/cover/{albums[0]}?s=80").status_code == 200


def test_plex_down_one_request_and_one_line_for_many_covers(client: TestClient, operator: Account,
                                                            fake_plex: FakePlex, caplog: pytest.LogCaptureFixture,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    connect(client, fake_plex)
    albums = _albums(client)
    moment = [1000.0]
    monkeypatch.setattr(clock, "monotonic", lambda: moment[0])
    fake_plex.calls.clear()
    fake_plex.fail_after = 0
    caplog.set_level(logging.DEBUG, logger="nexmix")
    answers = [client.get(f"/api/v1/catalog/cover/{albums[number % len(albums)]}?s={(80, 200, 400)[number % 3]}")
               for number in range(50)]
    assert {answer.status_code for answer in answers} == {503}
    assert all(int(answer.headers["retry-after"]) >= 1 for answer in answers)
    assert {answer.json()["detail"]["code"] for answer in answers} == {"plex_busy"}
    assert _photo_calls(fake_plex) == 1, "a Plex in trouble is asked once, not for every cover"
    loud = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(loud) == 1, [record.getMessage() for record in loud]
    assert "covers" in loud[0].getMessage()

    # The quiet time over and Plex back: the next cover asks again, gets it, and says so once.
    fake_plex.fail_after = None
    moment[0] += covers.QUIET_SECONDS + 1
    caplog.clear()
    assert client.get(f"/api/v1/catalog/cover/{albums[0]}?s=80").status_code == 200
    assert _photo_calls(fake_plex) == 2
    assert [record.getMessage() for record in caplog.records if record.name == "nexmix.covers"] == [
        "Plex delivers covers again"]
    assert client.get(f"/api/v1/catalog/cover/{albums[1]}?s=80").status_code == 200


def test_a_guest_cover_keeps_to_the_same_quiet(client: TestClient, operator: Account, fake_plex: FakePlex,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    from .plex_world import make
    from .test_guests_and_plex import invite, phone

    connect(client, fake_plex)
    albums = _albums(client)
    playlist = make(client, "Garden", [3001])
    code = invite(client, playlist["id"])
    guest = phone(code)
    # Well first: the guest's cover and the cover of the page of the link come through the same way.
    assert guest.get(f"/api/v1/guest/cover/{albums[0]}?s=80").status_code == 200
    door = guest.get(f"/api/v1/join/{code}").json()["playlist"]["covers"]
    assert door and guest.get(f"/api/v1/join/{code}/cover/{door[0]}?s=200").status_code == 200
    moment = [5000.0]
    monkeypatch.setattr(clock, "monotonic", lambda: moment[0])
    covers.plex_failed("plex_timeout")
    fake_plex.calls.clear()
    answer = guest.get(f"/api/v1/guest/cover/{albums[1]}?s=80")
    assert answer.status_code == 503 and answer.json()["detail"]["code"] == "plex_timeout"
    assert int(answer.headers["retry-after"]) >= 1
    assert guest.get(f"/api/v1/join/{code}/cover/{door[0]}?s=400").status_code == 503
    assert _photo_calls(fake_plex) == 0
    moment[0] += covers.QUIET_SECONDS + 1
    assert guest.get(f"/api/v1/guest/cover/{albums[1]}?s=80").status_code == 200
