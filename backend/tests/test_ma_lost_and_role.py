"""Music Assistant: a taking out whose answer was lost (the websocket broke right after the command went out) is kept
until no such task can run any more, found among the playlist's tasks; and a token's user who loses the admin's right
while the websocket stays open is seen at the first refusal, not only when the websocket opens anew."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.services import music_assistant
from app.services import sync as server_sync
from app.services.sources import music_assistant as connector

from .fakema import FILES, FakeMA, _Error
from .ma_world import both_equal, connect, events, list_key, make, numbers, sync
from .plex_world import mirror

REMOVE = "music/playlists/remove_playlist_tracks"


def _setup(client: TestClient, fake: FakeMA, count: int = 6) -> tuple[int, int, list[int]]:
    server = connect(client, fake)
    chosen = sorted(one.id for one in fake.tracks.values() if one.mappings[0][0] == FILES)[:count]
    made = make(client, server, "Verloren", numbers(server, chosen))
    sync()
    return server, int(made["id"]), chosen


def _entries(client: TestClient, playlist: int) -> list[dict[str, Any]]:
    return client.get(f"/api/v1/playlists/{playlist}").json()["items"]


def _removes(fake: FakeMA) -> int:
    return sum(1 for command, _args in fake.calls if command == REMOVE)


def _rounds(server: int, fake: FakeMA, count: int = 3) -> None:
    for _round in range(count):
        with SessionLocal() as db:
            server_sync.request_all(db, server)
        events(server, fake)
        sync()


def _lost_first(client: TestClient, fake: FakeMA, playlist: int) -> None:
    """nexmix takes out the first entry; the command reaches Music Assistant, its task waits in the queue, and the
    websocket breaks before the answer comes back."""
    fake.task_later = 10**6
    fake.break_after.add(REMOVE)
    items = _entries(client, playlist)
    assert client.post(f"/api/v1/playlists/{playlist}/items/remove",
                       json={"entries": [items[0]["id"]]}).status_code == 200
    sync(rounds=1)
    fake.task_later = 0


def test_a_taking_out_whose_answer_was_lost_is_kept_and_cancelled_before_it_takes_the_wrong_entry(
        client: TestClient, operator: object, fake_ma: FakeMA) -> None:
    """The answer to nexmix's taking out of the first entry is lost; its task still waits in Music Assistant. Meanwhile
    somebody takes the first entry out in Music Assistant's own interface, so place 1 now names the second. nexmix
    keeps the lost task (the playlist waits), finds it among the playlist's tasks and cancels it: the second entry stays
    on both sides."""
    server, playlist, chosen = _setup(client, fake_ma)
    key = list_key(playlist)
    _lost_first(client, fake_ma, playlist)
    row = mirror(playlist)
    kept = [one["id"] for one in (row.writing or {}).get("tasks") or []]
    assert len(kept) == 1 and kept[0].startswith(connector.LOST + str(key) + ":"), row.writing
    assert row.error == "ma_task_open"
    removes = _removes(fake_ma)
    fake_ma.ui_remove(key, chosen[0])
    _rounds(server, fake_ma)
    # Late, the queue runs: the task was cancelled, it takes nothing.
    fake_ma.run_tasks()
    _rounds(server, fake_ma)
    there = both_equal(fake_ma, server, playlist)
    assert there == chosen[1:], there
    # No second taking out by place while the lost one could still run.
    assert _removes(fake_ma) == removes
    row = mirror(playlist)
    assert row.state == "synced"
    assert not (isinstance(row.writing, dict) and (row.writing.get("tasks") or row.writing.get("restore")))


def test_a_lost_taking_out_that_runs_late_takes_nothing_that_was_not_meant(
        client: TestClient, operator: object, fake_ma: FakeMA, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same, but Music Assistant refuses to cancel (the task runs already): the playlist waits, and when the task
    has run late and taken the second entry by its place, that one goes back. Only the first is gone."""
    server, playlist, chosen = _setup(client, fake_ma)
    key = list_key(playlist)

    def refused(_args: dict[str, Any]) -> object:
        raise _Error(20)

    _lost_first(client, fake_ma, playlist)
    monkeypatch.setattr(fake_ma, "_c_tasks__cancel", refused)
    fake_ma.ui_remove(key, chosen[0])
    _rounds(server, fake_ma, 2)
    assert mirror(playlist).error == "ma_task_open"
    fake_ma.run_tasks()
    assert chosen[1] not in fake_ma.entries(key)
    _rounds(server, fake_ma)
    there = both_equal(fake_ma, server, playlist)
    assert chosen[0] not in there and set(chosen[1:]) <= set(there), there


def test_a_lost_taking_out_that_music_assistant_never_made_holds_nothing_up(
        client: TestClient, operator: object, fake_ma: FakeMA) -> None:
    """Music Assistant started anew (its tasks with it): no task of the playlist is there, the playlist goes on and the
    first entry is taken out again."""
    server, playlist, chosen = _setup(client, fake_ma)
    _lost_first(client, fake_ma, playlist)
    fake_ma.tasks.clear()
    _rounds(server, fake_ma)
    assert both_equal(fake_ma, server, playlist) == chosen[1:]
    assert mirror(playlist).state == "synced"


def test_the_role_is_read_again_when_music_assistant_refuses_a_task_command(
        client: TestClient, operator: object, fake_ma: FakeMA) -> None:
    """The token's user loses the admin's right while nexmix's websocket stays open. Music Assistant refuses the next
    ``tasks/run``: nexmix reads the role again at once, the card says so, and nothing more goes by place, without the
    websocket being opened anew."""
    server, playlist, chosen = _setup(client, fake_ma)
    assert music_assistant.may_manage(server) is True
    opened = len(fake_ma.channels)
    fake_ma.set_role(fake_ma.token, "user", open_too=True)
    items = _entries(client, playlist)
    client.post(f"/api/v1/playlists/{playlist}/items/remove", json={"entries": [items[0]["id"]]})
    sync(rounds=2)
    assert music_assistant.may_manage(server) is False
    assert client.get("/api/v1/music-assistant").json()["servers"][0]["not_admin"] is True
    removes = _removes(fake_ma)
    items = _entries(client, playlist)
    client.post(f"/api/v1/playlists/{playlist}/items/remove", json={"entries": [items[-1]["id"]]})
    sync(rounds=2)
    assert _removes(fake_ma) == removes
    assert mirror(playlist).error == "ma_not_admin"
    assert chosen[-1] in fake_ma.entries(list_key(playlist))
    # The websocket of the sync was never opened anew: only the short one that read the role came on top.
    assert len([one for one in fake_ma.channels[opened:] if not one.closed]) == 0


def test_a_refusal_with_the_right_still_there_changes_nothing(client: TestClient, operator: object,
                                                              fake_ma: FakeMA, monkeypatch: pytest.MonkeyPatch) -> None:
    """``tasks/run`` refused for another reason while the token's user is still an admin: the role read again stays
    admin, and the taking out goes through."""
    server, playlist, chosen = _setup(client, fake_ma)
    fake_ma.fail_once["tasks/run"] = 20
    items = _entries(client, playlist)
    client.post(f"/api/v1/playlists/{playlist}/items/remove", json={"entries": [items[2]["id"]]})
    sync()
    assert music_assistant.may_manage(server) is True
    assert both_equal(fake_ma, server, playlist) == [one for place, one in enumerate(chosen) if place != 2]
