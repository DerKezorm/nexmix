"""The preview of "take all over" works in the background, never in the thread of the request: begun with ``POST``,
read with ``GET`` (how many playlists are done, then the result); asked again while it runs it is not begun twice, a
different one ends the one under way. The catalogue of another server kept for finding tracks there goes when nobody
used it for a while (``sync.KEEP_IDLE``)."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import clock
from app.db import SessionLocal
from app.models import Account
from app.services import match, mirrors
from app.services import sync as server_sync

from .fakejellyfin import FakeJellyfin
from .fakeplex import FakePlex
from .mirror_world import connect_both, look, plex_numbers
from .plex_world import make, sync

PLAYLIST = [3001, 3003, 3013, 3017, 3026, 3021]


@pytest.fixture
def world(client: TestClient, operator: Account, fake_plex: FakePlex,
          fake_jellyfin: FakeJellyfin) -> tuple[FakePlex, FakeJellyfin, int]:
    _plex, jelly, _twins = connect_both(client, fake_plex, fake_jellyfin, frozenset({3021}))
    for number in range(3):
        make(client, f"Liste {number}", plex_numbers(PLAYLIST[number:number + 3]))
    sync(fake_plex)
    return fake_plex, fake_jellyfin, jelly


def test_the_preview_is_worked_out_in_a_thread_of_its_own_with_its_progress(
        client: TestClient, world: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    _plex, _fake, jelly = world
    gate = threading.Event()
    seen: list[tuple[str, int]] = []
    real = mirrors.preview

    def slow(db: Any, ids: Any, target: int, choices: Any = None, progress: Any = None, **more: Any) -> dict[str, Any]:
        def told(done: int) -> None:
            seen.append((threading.current_thread().name, done))
            assert progress is not None
            progress(done)
            if done == 1:
                gate.wait(10)

        return real(db, ids, target, choices, progress=told, **more)

    monkeypatch.setattr(mirrors, "preview", slow)
    begun = client.post(f"/api/v1/servers/{jelly}/transfer/preview", json={"mode": "mirror"})
    # The answer comes at once, while the preview waits at its second playlist.
    assert begun.status_code == 202, begun.text
    assert begun.json()["preview"]["state"] == "running" and begun.json()["preview"]["total"] == 3
    for _try in range(200):
        state = client.get(f"/api/v1/servers/{jelly}/transfer/preview").json()["preview"]
        if state["done"] == 1:
            break
        threading.Event().wait(0.01)
    assert (state["state"], state["done"], state["result"]) == ("running", 1, None)
    # Asked again the same while it runs: not begun twice.
    again = client.post(f"/api/v1/servers/{jelly}/transfer/preview", json={"mode": "mirror"})
    assert again.status_code == 202 and again.json()["preview"]["done"] == 1
    gate.set()
    found = mirrors.wait_look(jelly)
    assert found is not None and found["state"] == "done"
    assert found["result"]["playlists"] == 3 and found["result"]["entries"] == 9
    assert {name for name, _done in seen} == {f"preview-{jelly}"}, "worked out in the thread of the request"
    assert [done for _name, done in seen] == [0, 1, 2]


def test_another_preview_ends_the_one_under_way(client: TestClient, world: tuple,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    _plex, _fake, jelly = world
    gate = threading.Event()
    real = mirrors.preview
    ended: list[str] = []

    def slow(db: Any, ids: Any, target: int, choices: Any = None, progress: Any = None, **more: Any) -> dict[str, Any]:
        def told(done: int) -> None:
            if done == 1 and not gate.is_set():
                gate.wait(10)
            assert progress is not None
            try:
                progress(done)
            except Exception as exc:
                ended.append(type(exc).__name__)
                raise

        return real(db, ids, target, choices, progress=told, **more)

    monkeypatch.setattr(mirrors, "preview", slow)
    assert client.post(f"/api/v1/servers/{jelly}/transfer/preview", json={"mode": "mirror"}).status_code == 202
    with mirrors._looking_lock:
        first = mirrors._looking[jelly]
    assert client.post(f"/api/v1/servers/{jelly}/transfer/preview", json={"mode": "move"}).status_code == 202
    gate.set()
    assert first.thread is not None
    first.thread.join(10)
    assert first.state == "stopped" and ended == ["_Stopped"]
    found = mirrors.wait_look(jelly)
    assert found is not None and found["state"] == "done" and found["result"]["playlists"] == 3


def test_nothing_to_read_before_a_preview_and_a_stranger_server_is_not_found(client: TestClient,
                                                                             world: tuple) -> None:
    _plex, _fake, jelly = world
    assert client.get(f"/api/v1/servers/{jelly}/transfer/preview").json() == {"preview": None}
    assert client.get("/api/v1/servers/77/transfer/preview").status_code == 404
    assert client.post("/api/v1/servers/77/transfer/preview", json={"mode": "mirror"}).status_code == 404
    assert look(client, jelly, {"mode": "move"})["playlists"] == 3


def test_the_catalogue_of_another_server_goes_when_nobody_used_it_for_a_while(
        client: TestClient, world: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    _plex, _fake, jelly = world
    moment = [1000.0]
    monkeypatch.setattr(clock, "monotonic", lambda: moment[0])
    look(client, jelly, {"mode": "mirror"})
    assert jelly in server_sync._whole, "kept for the copy that follows the preview"
    match.album_key("Nordlicht")
    moment[0] += server_sync.KEEP_IDLE - 1
    assert server_sync.release_idle() == 0
    with SessionLocal() as db:
        # Used again: kept longer.
        server_sync._catalogue(db, jelly)
    moment[0] += server_sync.KEEP_IDLE - 1
    assert server_sync.release_idle() == 0 and jelly in server_sync._whole
    moment[0] += 2
    assert server_sync.release_idle() == 1
    assert server_sync._whole == {}
    assert match.album_key.cache_info().currsize == 0, "the names made comparable went with the last catalogue"


def test_a_look_at_a_servers_list_lets_go_of_a_catalogue_nobody_uses(
        client: TestClient, world: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_plex, _fake, jelly = world
    moment = [1000.0]
    monkeypatch.setattr(clock, "monotonic", lambda: moment[0])
    look(client, jelly, {"mode": "mirror"})
    assert jelly in server_sync._whole
    moment[0] += server_sync.KEEP_IDLE + 1
    server_sync.look_now()
    sync(fake_plex, look_at_plex=True)
    assert jelly not in server_sync._whole


def test_a_preview_over_many_playlists_looks_at_a_few_at_a_time_and_counts_them_all(
        client: TestClient, world: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    """Over many playlists the preview never holds the entries of all of them at once (with 100 playlists of 500 entries
    that was some 140 MB): it looks at ``PREVIEW_PART`` together, and its result is the same as over all at once."""
    fake_plex, _fake, jelly = world
    for number in range(3, 25):
        make(client, f"Liste {number}", plex_numbers(PLAYLIST[number % 4:number % 4 + 3]))
    sync(fake_plex)
    looked: list[int] = []
    real = mirrors.describe

    def counted(db: Any, server_id: int, tracks: Any) -> Any:
        tracks = list(tracks)
        looked.append(len(tracks))
        return real(db, server_id, tracks)

    with SessionLocal() as db:
        chosen, _smart = mirrors.candidates(db, jelly, "mirror", None)
        whole = [mirrors.preview(db, [one], jelly, {}, preselect=False) for one in chosen]
    assert len(chosen) == 25
    monkeypatch.setattr(mirrors, "describe", counted)
    with SessionLocal() as db:
        found = mirrors.preview(db, chosen, jelly, {}, preselect=False)
    # 25 playlists of three entries: three looks, none at more than ten playlists' entries.
    assert len(looked) == 3 and max(looked) <= mirrors.PREVIEW_PART * 3, looked
    for field in ("entries", "found", "unsure", "missing"):
        assert found[field] == sum(one[field] for one in whole), field
    missing = {row["track"]: row["count"] for row in found["rows"]}
    assert missing and sum(missing.values()) == sum(row["count"] for one in whole for row in one["rows"])
