"""Music Assistant changed in its own interface while nexmix writes by place: never a track lost that nobody took out.
A step broken off half way, a new name broken off half way, events lost."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock
from app.db import SessionLocal
from app.models import Playlist
from app.services import music_assistant
from app.services import sync as server_sync
from app.services.music_assistant import MaError
from app.services.sources import music_assistant as connector
from app.services.sources.music_assistant import Raw, _wrongly_taken

from .fakema import FakeMA
from .ma_world import both_equal, connect, events, list_key, make, numbers, sync
from .plex_world import mirror, playlist_row


def _setup(client: TestClient, fake: FakeMA, count: int = 9) -> tuple[int, int, list[int]]:
    server = connect(client, fake)
    chosen = sorted(one.id for one in fake.tracks.values() if one.mappings[0][0].startswith("filesystem_local--a"))
    made = make(client, server, "Rennen", numbers(server, chosen[:count]))
    sync()
    return server, int(made["id"]), chosen[:count]


def _entries(client: TestClient, playlist: int) -> list[dict[str, object]]:
    return client.get(f"/api/v1/playlists/{playlist}").json()["items"]


def _remove(client: TestClient, playlist: int, places: list[int]) -> None:
    items = _entries(client, playlist)
    answer = client.post(f"/api/v1/playlists/{playlist}/items/remove",
                         json={"entries": [items[place]["id"] for place in places]})
    assert answer.status_code == 200


def test_taken_out_in_music_assistant_while_nexmix_takes_out_by_place(client: TestClient, operator: object,
                                                                      fake_ma: FakeMA) -> None:
    server, playlist, chosen = _setup(client, fake_ma)
    key = list_key(playlist)
    # nexmix takes out the fifth; right before Music Assistant does it, somebody takes out the second there: the
    # place 5 then names the sixth.
    fake_ma.before_task = lambda: fake_ma.ui_remove(key, chosen[1])
    _remove(client, playlist, [4])
    sync()
    events(server, fake_ma)
    sync()
    wanted = [one for place, one in enumerate(chosen) if place not in (1, 4)]
    assert both_equal(fake_ma, server, playlist) == wanted
    assert chosen[5] in fake_ma.entries(key)


def test_taken_out_in_music_assistant_after_nexmix_took_out(client: TestClient, operator: object,
                                                            fake_ma: FakeMA, monkeypatch: pytest.MonkeyPatch) -> None:
    server, playlist, chosen = _setup(client, fake_ma)
    key = list_key(playlist)
    original = connector.MusicAssistantSource._take_out

    def then_somebody(self: connector.MusicAssistantSource, list_key_: str, places: list[int], length: int,
                      watch: dict[str, tuple[str, str, int]] | None = None) -> str:
        status = original(self, list_key_, places, length, watch)
        fake_ma.ui_remove(key, chosen[7])
        return status

    monkeypatch.setattr(connector.MusicAssistantSource, "_take_out", then_somebody)
    _remove(client, playlist, [2])
    sync()
    monkeypatch.setattr(connector.MusicAssistantSource, "_take_out", original)
    events(server, fake_ma)
    sync()
    # Nothing nexmix took was wrong: the other change is taken as it is.
    assert both_equal(fake_ma, server, playlist) == [one for place, one in enumerate(chosen) if place not in (2, 7)]


def test_sorted_in_nexmix_while_music_assistant_takes_one_out(client: TestClient, operator: object,
                                                              fake_ma: FakeMA) -> None:
    server, playlist, chosen = _setup(client, fake_ma)
    key = list_key(playlist)
    items = _entries(client, playlist)
    fake_ma.before_task = lambda: fake_ma.ui_remove(key, chosen[8])
    moved = client.post(f"/api/v1/playlists/{playlist}/items/move",
                        json={"entry": items[6]["id"], "after": items[1]["id"]})
    assert moved.status_code == 200
    sync()
    events(server, fake_ma)
    sync()
    there = both_equal(fake_ma, server, playlist)
    # Every track nobody took out is there; the one taken out in Music Assistant may come back (adding wins).
    assert set(chosen[:8]) <= set(there)
    assert there.index(chosen[6]) == there.index(chosen[1]) + 1


def test_a_step_broken_off_after_taking_out_is_put_right(client: TestClient, operator: object,
                                                         fake_ma: FakeMA, monkeypatch: pytest.MonkeyPatch) -> None:
    server, playlist, chosen = _setup(client, fake_ma)
    key = list_key(playlist)
    original = connector.MusicAssistantSource.raw
    reads = {"count": 0, "armed": False}

    def broken(self: connector.MusicAssistantSource, list_key_: str) -> list[Raw]:
        if reads["armed"]:
            reads["count"] += 1
            # The third reading: the one after taking out (the sync's own, the one before taking out, then this).
            if reads["count"] == 3:
                raise MaError("ma_unreachable")
        return original(self, list_key_)

    monkeypatch.setattr(connector.MusicAssistantSource, "raw", broken)
    # The wrong one goes (somebody took one out in between), then the websocket breaks before nexmix sees it.
    fake_ma.before_task = lambda: fake_ma.ui_remove(key, chosen[0])
    _remove(client, playlist, [3])
    reads["armed"] = True
    sync(rounds=2)
    reads["armed"] = False
    row = mirror(playlist)
    assert isinstance(row.writing, dict) and row.writing.get("restore")
    with SessionLocal() as db:
        server_sync.request_all(db, server)
    events(server, fake_ma)
    sync()
    there = both_equal(fake_ma, server, playlist)
    # The fifth, taken by mistake, is back; nothing nobody took out is lost.
    assert set(chosen) - {chosen[3], chosen[0]} <= set(there)
    assert chosen[3] not in there
    assert not (isinstance(mirror(playlist).writing, dict) and mirror(playlist).writing.get("restore"))


def test_a_new_name_broken_off_half_way_leaves_one_playlist(client: TestClient, operator: object, fake_ma: FakeMA,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    server, playlist, chosen = _setup(client, fake_ma)
    before = list_key(playlist)
    original = connector.MusicAssistantSource.copy
    calls = {"count": 0}

    def broken(self: connector.MusicAssistantSource, key: str, into: str) -> list[Raw]:
        calls["count"] += 1
        if calls["count"] == 1:
            self._add_uris(into, [one.uri for one in self.raw(key)][:3])
            raise MaError("ma_unreachable")
        return original(self, key, into)

    monkeypatch.setattr(connector.MusicAssistantSource, "copy", broken)
    assert client.put(f"/api/v1/playlists/{playlist}", json={"name": "Neu"}).status_code == 200
    _remove(client, playlist, [0])
    sync(rounds=2)
    # Half a copy there: never taken in as a playlist of its own, never read as a deletion.
    assert len([one for one in fake_ma.playlists.values() if one.editable]) == 2
    sync(look=True)
    with SessionLocal() as db:
        assert len(list(db.scalars(select(Playlist.id)))) == 1
    with SessionLocal() as db:
        server_sync.request_all(db, server)
    sync()
    sync(look=True)
    mine = [one for one in fake_ma.playlists.values() if one.editable]
    assert [one.name for one in mine] == ["Neu"] and before not in fake_ma.playlists
    assert both_equal(fake_ma, server, playlist) == chosen[1:]
    assert playlist_row(playlist).deleted_at is None


def test_a_new_name_while_entries_change_on_both_sides(client: TestClient, operator: object,
                                                       fake_ma: FakeMA) -> None:
    server, playlist, chosen = _setup(client, fake_ma)
    key = list_key(playlist)
    extra = fake_ma.track_by_name("Small Hours").id
    fake_ma.ui_add(key, extra)
    assert client.put(f"/api/v1/playlists/{playlist}", json={"name": "Umbenannt"}).status_code == 200
    _remove(client, playlist, [2])
    sync()
    events(server, fake_ma)
    sync(look=True)
    there = both_equal(fake_ma, server, playlist)
    assert extra in there and chosen[2] not in there
    assert fake_ma.playlists[list_key(playlist)].name == "Umbenannt"
    assert len([one for one in fake_ma.playlists.values() if one.editable]) == 1


def test_events_lost_are_made_up_for(client: TestClient, operator: object, fake_ma: FakeMA) -> None:
    server, playlist, chosen = _setup(client, fake_ma)
    key = list_key(playlist)
    fake_ma.ui_remove(key, chosen[0])
    # The event is lost (the queue taken away), but the websocket broke meanwhile: everything is looked at again.
    connection = music_assistant.current(server)
    assert connection is not None
    from .ma_world import settle

    settle(fake_ma)
    while not connection.events.empty():
        connection.events.get_nowait()
    from app.services import ma_events

    assert ma_events.handle(server, {"event": "_closed"}) == "again"
    sync()
    assert both_equal(fake_ma, server, playlist) == chosen[1:]
    # And without any event, the playlist is read again within two minutes.
    fake_ma.ui_remove(key, chosen[1])
    settle(fake_ma)
    while not connection.events.empty():
        connection.events.get_nowait()
    later = clock.now() + timedelta(seconds=200)
    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(clock, "now", lambda: later)
        server_sync.look_now(server)
        sync()
    assert both_equal(fake_ma, server, playlist) == chosen[2:]


def test_the_list_of_music_assistant_is_looked_at_every_minute(client: TestClient, operator: object,
                                                              fake_ma: FakeMA) -> None:
    server, _playlist, _chosen = _setup(client, fake_ma)
    from app.services import jobs

    with SessionLocal() as db:
        moment = clock.now()
        server_sync.tick(db, moment)
        jobs.run_pending(lane="playlists")
        assert server_sync.tick(db, moment + timedelta(seconds=30)) == 0
        assert server_sync.tick(db, moment + timedelta(seconds=61)) >= 1
    assert server


# --- What may have been taken by mistake (pure) -----------------------------------------------------------------------


def _raw(names: list[str]) -> list[Raw]:
    return [Raw(place, name, "p", name, name) for place, name in enumerate(names, 1)]


def test_wrongly_taken_somebody_took_one_before() -> None:
    before = _raw(list("abcdefgh"))
    # Place 5 (e) meant; b went before, so f went.
    after = _raw(list("acdeg h".replace(" ", "")))
    assert [one.uri for one in _wrongly_taken(before, after, [5])] == ["f"]


def test_wrongly_taken_somebody_took_one_after() -> None:
    before = _raw(list("abcdefgh"))
    after = _raw(list("abcdfgh".replace("h", "")))
    # e meant and gone, h gone by somebody else afterwards: nothing to put back.
    assert _wrongly_taken(before, after, [5]) == []


def test_wrongly_taken_unexplained_puts_back_everything_not_meant() -> None:
    before = _raw(list("abcdef"))
    after = _raw(list("fbd"))
    assert sorted(one.uri for one in _wrongly_taken(before, after, [3])) == ["a", "e"]


@pytest.mark.parametrize("seed", range(40))
def test_wrongly_taken_never_misses_a_track_this_step_took(seed: int) -> None:
    import random

    rng = random.Random(seed)
    names = [f"t{one}" for one in range(rng.randint(3, 12))]
    before = _raw(names)
    places = sorted(rng.sample(range(1, len(names) + 1), rng.randint(1, min(3, len(names)))))
    meant = {names[place - 1] for place in places}
    current = list(names)
    earlier = rng.sample(current, rng.randint(0, 2))
    for one in earlier:
        current.remove(one)
    ours = set()
    if max(places) <= len(current):
        ours = {current[place - 1] for place in places}
        current = [one for one in current if one not in ours]
    later = rng.sample(current, rng.randint(0, min(2, len(current))))
    for one in later:
        current.remove(one)
    put_back = {one.uri for one in _wrongly_taken(before, _raw(current), places)}
    # Whatever this step took that it did not mean is put back, whatever else happened.
    assert ours - meant <= put_back
    assert not put_back & meant


def test_a_task_that_waits_behind_others_is_cancelled_and_nothing_runs_later(client: TestClient, operator: object,
                                                                              fake_ma: FakeMA) -> None:
    server, playlist, chosen = _setup(client, fake_ma)
    key = list_key(playlist)
    # Music Assistant's tasks hang behind lookups of metadata: nexmix's taking out never runs.
    fake_ma.task_later = 10**6
    from app import clock as app_clock

    moment = [0.0]

    def ticking() -> float:
        moment[0] += 5.0
        return moment[0]

    patch = pytest.MonkeyPatch()
    patch.setattr(app_clock, "monotonic", ticking)
    try:
        _remove(client, playlist, [2])
        sync(rounds=2)
    finally:
        patch.undo()
    assert mirror(playlist).state == "waiting"
    # It asked to run next, then cancelled it: run later, it does nothing.
    assert fake_ma.pushed
    fake_ma.task_later = 0
    fake_ma.run_tasks()
    assert fake_ma.entries(key) == chosen
    with SessionLocal() as db:
        server_sync.request_all(db, server)
    sync()
    assert both_equal(fake_ma, server, playlist) == [one for place, one in enumerate(chosen) if place != 2]
    # Put in again by the library's track, never by a provider's path.
    for command, args in fake_ma.calls:
        if command == "music/playlists/add_playlist_tracks":
            assert all(str(uri).startswith("library://track/") for uri in args["uris"])


def test_added_in_music_assistant_to_the_old_playlist_while_it_is_copied_for_a_new_name(
        client: TestClient, operator: object, fake_ma: FakeMA, monkeypatch: pytest.MonkeyPatch) -> None:
    server, playlist, chosen = _setup(client, fake_ma)
    old = list_key(playlist)
    extra = fake_ma.track_by_name("Small Hours").id
    original = connector.MusicAssistantSource.copy

    def meanwhile(self: connector.MusicAssistantSource, key: str, into: str) -> list[Raw]:
        copied = original(self, key, into)
        # Somebody adds to the old one in Music Assistant right after it was copied.
        fake_ma.ui_add(old, extra)
        return copied

    monkeypatch.setattr(connector.MusicAssistantSource, "copy", meanwhile)
    assert client.put(f"/api/v1/playlists/{playlist}", json={"name": "Umgezogen"}).status_code == 200
    sync()
    events(server, fake_ma)
    sync()
    assert old not in fake_ma.playlists
    there = both_equal(fake_ma, server, playlist)
    assert extra in there and set(chosen) <= set(there)


def test_sorted_in_music_assistant_while_nexmix_sorts_by_place(client: TestClient, operator: object,
                                                               fake_ma: FakeMA) -> None:
    server, playlist, chosen = _setup(client, fake_ma)
    key = list_key(playlist)
    items = _entries(client, playlist)
    # Right before nexmix's taking out runs, somebody turns the whole list round there: the places name others.
    fake_ma.before_task = lambda: fake_ma.ui_order(key, list(reversed(fake_ma.entries(key))))
    moved = client.post(f"/api/v1/playlists/{playlist}/items/move",
                        json={"entry": items[7]["id"], "after": items[2]["id"]})
    assert moved.status_code == 200
    sync(rounds=1)
    # The same step put back what it took by mistake (not only the next sync).
    assert set(chosen) <= set(fake_ma.entries(key))
    sync()
    events(server, fake_ma)
    sync()
    # Nothing lost on either side, whatever order won.
    assert sorted(both_equal(fake_ma, server, playlist)) == sorted(chosen)


def test_what_could_not_be_put_back_stays_kept_until_it_is(client: TestClient, operator: object,
                                                           fake_ma: FakeMA) -> None:
    server, playlist, chosen = _setup(client, fake_ma)
    key = list_key(playlist)
    items = _entries(client, playlist)

    def meanwhile() -> None:
        fake_ma.ui_order(key, list(reversed(fake_ma.entries(key))))
        # And Music Assistant takes nothing of what nexmix puts in for a while.
        fake_ma.drop_adds = 3

    fake_ma.before_task = meanwhile
    client.post(f"/api/v1/playlists/{playlist}/items/move", json={"entry": items[7]["id"], "after": items[2]["id"]})
    sync(rounds=1)
    row = mirror(playlist)
    assert isinstance(row.writing, dict) and row.writing.get("restore"), "kept until it is back"
    fake_ma.drop_adds = 0
    with SessionLocal() as db:
        server_sync.request_all(db, server)
    sync()
    events(server, fake_ma)
    sync()
    assert sorted(both_equal(fake_ma, server, playlist)) == sorted(chosen)
