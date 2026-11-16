"""Music Assistant changed by somebody else right while nexmix writes there by place (a track replaced: one taken out,
another added), tasks that run late after cancelling them was refused, taking up a playlist after a lost answer, a new
name of a playlist with an entry outside the library, a token that is no admin's."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import SERVER_MA, MediaServer, Playlist, PlaylistItem
from app.services import music_assistant
from app.services import sync as server_sync
from app.services.music_assistant import MaError
from app.services.sources import music_assistant as connector

from .fakema import FILES, FakeMA, _Error
from .ma_world import both_equal, connect, events, list_key, make, numbers, sync
from .plex_world import local_tracks, mirror


def _setup(client: TestClient, fake: FakeMA, count: int = 9) -> tuple[int, int, list[int]]:
    server = connect(client, fake)
    chosen = sorted(one.id for one in fake.tracks.values() if one.mappings[0][0] == FILES)
    made = make(client, server, "Rennen", numbers(server, chosen[:count]))
    sync()
    return server, int(made["id"]), chosen[:count]


def _entries(client: TestClient, playlist: int) -> list[dict[str, Any]]:
    return client.get(f"/api/v1/playlists/{playlist}").json()["items"]


def _extra(fake: FakeMA, chosen: list[int]) -> int:
    return next(one.id for one in sorted(fake.tracks.values(), key=lambda one: one.id)
                if one.mappings[0][0] == FILES and one.id not in chosen)


def _settled(server: int, fake: FakeMA) -> None:
    for _round in range(3):
        sync()
        events(server, fake)
    sync()


def _concurrent(client: TestClient, playlist: int) -> dict[str, Any] | None:
    return client.get(f"/api/v1/playlists/{playlist}").json()["plex"].get("concurrent")


def _removes(fake: FakeMA) -> int:
    return sum(1 for command, _args in fake.calls if command == "music/playlists/remove_playlist_tracks")


def _writes(fake: FakeMA) -> int:
    return sum(1 for command, _args in fake.calls
               if command in ("music/playlists/remove_playlist_tracks", "music/playlists/add_playlist_tracks",
                              "music/playlists/create_playlist", "music/library/remove_item"))


def _refuse_cancel(fake: FakeMA, monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(_args: dict[str, Any]) -> object:
        raise _Error(20)

    monkeypatch.setattr(fake, "_c_tasks__cancel", refused)


def _ticking() -> pytest.MonkeyPatch:
    from app import clock as app_clock

    moment = [0.0]

    def ticking() -> float:
        moment[0] += 5.0
        return moment[0]

    patch = pytest.MonkeyPatch()
    patch.setattr(app_clock, "monotonic", ticking)
    return patch


def _left_open(client: TestClient, fake: FakeMA, playlist: int, place: int) -> None:
    """nexmix takes out the entry at ``place``; Music Assistant's queue hangs, and the task is left open."""
    fake.task_later = 10**6
    patch = _ticking()
    try:
        items = _entries(client, playlist)
        client.post(f"/api/v1/playlists/{playlist}/items/remove", json={"entries": [items[place]["id"]]})
        sync(rounds=1)
    finally:
        patch.undo()
    fake.task_later = 0


# --- A track replaced in Music Assistant while nexmix writes by place -------------------------------------------------


def test_a_track_added_in_ma_while_nexmix_takes_out_the_last_entry_is_never_lost_unseen(
        client: TestClient, operator: object, fake_ma: FakeMA) -> None:
    """Somebody replaces a track in Music Assistant (takes one out, adds another) right before nexmix's taking out of
    the last entry runs: the place then names the track just added, which nexmix never saw. Which track that was
    cannot be told (Music Assistant names no entry), but the playlist says so."""
    server, playlist, chosen = _setup(client, fake_ma)
    key = list_key(playlist)
    extra = _extra(fake_ma, chosen)

    def meanwhile() -> None:
        fake_ma.ui_remove(key, chosen[0])
        fake_ma.ui_add(key, extra)

    fake_ma.before_task = meanwhile
    items = _entries(client, playlist)
    assert client.post(f"/api/v1/playlists/{playlist}/items/remove",
                       json={"entries": [items[8]["id"]]}).status_code == 200
    _settled(server, fake_ma)
    there = both_equal(fake_ma, server, playlist)
    note = _concurrent(client, playlist)
    # Never lost without a word: where the track added there is gone, the playlist says so.
    assert extra in there or (note is not None and note["count"] >= 1), (there, note)
    assert note is not None and note["count"] == 1
    # Nothing nexmix knew of is lost: only what nexmix took out is gone (and what was taken out there came back).
    assert set(chosen[:8]) <= set(there) and chosen[8] not in there
    # Seen: the note goes.
    assert client.delete(f"/api/v1/playlists/{playlist}/concurrent").status_code == 200
    assert _concurrent(client, playlist) is None


def test_a_track_added_in_ma_while_nexmix_sorts_is_never_lost_unseen(client: TestClient, operator: object,
                                                                    fake_ma: FakeMA) -> None:
    """The same while nexmix sorts (everything from the first entry out of place goes out by place)."""
    server, playlist, chosen = _setup(client, fake_ma)
    key = list_key(playlist)
    extra = _extra(fake_ma, chosen)

    def meanwhile() -> None:
        fake_ma.ui_remove(key, chosen[8])
        fake_ma.ui_add(key, extra)

    fake_ma.before_task = meanwhile
    items = _entries(client, playlist)
    assert client.post(f"/api/v1/playlists/{playlist}/items/move",
                       json={"entry": items[6]["id"], "after": items[1]["id"]}).status_code == 200
    _settled(server, fake_ma)
    there = both_equal(fake_ma, server, playlist)
    note = _concurrent(client, playlist)
    assert extra in there or (note is not None and note["count"] >= 1), (there, note)
    assert set(chosen[:8]) <= set(there)


def test_nexmix_alone_writing_by_place_leaves_no_note(client: TestClient, operator: object, fake_ma: FakeMA) -> None:
    """nexmix's own tasks (taking out, putting back in a new order) are never taken for somebody else's."""
    server, playlist, chosen = _setup(client, fake_ma)
    items = _entries(client, playlist)
    client.post(f"/api/v1/playlists/{playlist}/items/move", json={"entry": items[7]["id"], "after": items[0]["id"]})
    client.post(f"/api/v1/playlists/{playlist}/items/remove", json={"entries": [items[3]["id"]]})
    _settled(server, fake_ma)
    there = both_equal(fake_ma, server, playlist)
    assert there.index(chosen[7]) == 1 and chosen[3] not in there
    assert _concurrent(client, playlist) is None


def test_an_order_changed_in_ma_while_nexmix_takes_out_by_place_loses_nothing(client: TestClient, operator: object,
                                                                              fake_ma: FakeMA) -> None:
    """Right before nexmix's taking out of the second entry runs, somebody puts it first in Music Assistant (taken out
    and added again) and takes the third out: place 2 then names the first. Taken out by somebody else before, and the
    third by this step, would explain the result too; with somebody else's adding meanwhile no such explanation is
    trusted, and the first comes back."""
    server, playlist, chosen = _setup(client, fake_ma, count=3)
    key = list_key(playlist)

    def meanwhile() -> None:
        fake_ma.ui_order(key, [chosen[1], chosen[0], chosen[2]])
        fake_ma.ui_remove(key, chosen[2])

    fake_ma.before_task = meanwhile
    items = _entries(client, playlist)
    client.post(f"/api/v1/playlists/{playlist}/items/remove", json={"entries": [items[1]["id"]]})
    _settled(server, fake_ma)
    there = both_equal(fake_ma, server, playlist)
    assert chosen[0] in there and chosen[1] not in there


def test_a_change_in_ma_after_the_sync_read_waits_before_anything_goes_by_place(
        client: TestClient, operator: object, fake_ma: FakeMA, monkeypatch: pytest.MonkeyPatch) -> None:
    """Somebody replaces a track in Music Assistant after the sync read the playlist, before it takes out by place: the
    reading right before the step differs from the one merged, so nothing goes by place; the sync merges again a moment
    later, and nothing is lost."""
    server, playlist, chosen = _setup(client, fake_ma)
    key = list_key(playlist)
    extra = _extra(fake_ma, chosen)
    original = connector.MusicAssistantSource.remove_many
    seen: dict[str, int] = {}

    def changed_first(self: connector.MusicAssistantSource, *args: Any, **kwargs: Any) -> None:
        if not seen:
            fake_ma.ui_remove(key, chosen[0])
            fake_ma.ui_add(key, extra)
            seen["removes"] = _removes(fake_ma)
            try:
                original(self, *args, **kwargs)
            finally:
                seen["after"] = _removes(fake_ma)
            return
        original(self, *args, **kwargs)

    monkeypatch.setattr(connector.MusicAssistantSource, "remove_many", changed_first)
    items = _entries(client, playlist)
    client.post(f"/api/v1/playlists/{playlist}/items/remove", json={"entries": [items[8]["id"]]})
    _settled(server, fake_ma)
    assert seen["after"] == seen["removes"], "nothing went by place from a reading that was not merged"
    there = both_equal(fake_ma, server, playlist)
    assert extra in there and chosen[8] not in there
    assert _concurrent(client, playlist) is None


def test_a_change_in_ma_under_way_waits_before_anything_goes_by_place(client: TestClient, operator: object,
                                                                      fake_ma: FakeMA) -> None:
    """Somebody else's task at the playlist has not run yet (it waits in Music Assistant's queue): nexmix takes nothing
    out by place until it ran, then merges with what it did."""
    server, playlist, chosen = _setup(client, fake_ma)
    key = list_key(playlist)
    extra = _extra(fake_ma, chosen)
    fake_ma.task_later = 10**6
    fake_ma._c_music__playlists__add_playlist_tracks({"db_playlist_id": key, "uris": [f"library://track/{extra}"]})
    fake_ma.task_later = 0
    removes = _removes(fake_ma)
    items = _entries(client, playlist)
    client.post(f"/api/v1/playlists/{playlist}/items/remove", json={"entries": [items[3]["id"]]})
    sync(rounds=3)
    assert _removes(fake_ma) == removes
    fake_ma.run_tasks()
    with SessionLocal() as db:
        server_sync.request_all(db, server)
    _settled(server, fake_ma)
    there = both_equal(fake_ma, server, playlist)
    assert extra in there and chosen[3] not in there
    assert _concurrent(client, playlist) is None


# --- A task that could not be cancelled -------------------------------------------------------------------------------


def test_a_task_that_could_not_be_cancelled_and_runs_late_takes_nothing_nobody_took(
        client: TestClient, operator: object, fake_ma: FakeMA, monkeypatch: pytest.MonkeyPatch) -> None:
    """Music Assistant's queue hangs and ``tasks/cancel`` is refused (a task running already). nexmix gives up waiting
    on its taking out of the third entry, but keeps the task: nothing is written to the playlist and nothing taken from
    it until Music Assistant says it ended. The person puts the track back in nexmix meanwhile; the queue runs again,
    the old task takes out place 3 of whatever the playlist is by then."""
    server, playlist, chosen = _setup(client, fake_ma)
    _refuse_cancel(fake_ma, monkeypatch)
    _left_open(client, fake_ma, playlist, 2)
    row = mirror(playlist)
    assert row.state == "waiting" and row.error == "ma_task_open"
    assert isinstance(row.writing, dict) and row.writing.get("tasks")
    again = client.post(f"/api/v1/playlists/{playlist}/items", json={"tracks": numbers(server, [chosen[2]])})
    assert again.status_code == 200
    writes = _writes(fake_ma)
    with SessionLocal() as db:
        server_sync.request_all(db, server)
    sync()
    # Still open: nothing written meanwhile.
    assert mirror(playlist).error == "ma_task_open"
    assert _writes(fake_ma) == writes
    fake_ma.run_tasks()
    for _round in range(2):
        with SessionLocal() as db:
            server_sync.request_all(db, server)
        events(server, fake_ma)
        sync()
    there = both_equal(fake_ma, server, playlist)
    assert not [chosen.index(one) for one in chosen if one not in there]
    row = mirror(playlist)
    assert not (isinstance(row.writing, dict) and (row.writing.get("tasks") or row.writing.get("restore")))


def test_what_a_task_run_late_took_is_put_back_never_taken_as_taken_out_there(
        client: TestClient, operator: object, fake_ma: FakeMA, monkeypatch: pytest.MonkeyPatch) -> None:
    """The task left open runs late after somebody took the first entry out in Music Assistant: its place 3 then names
    the fourth. That one comes back, the third goes as meant (the first may come back too: adding wins)."""
    server, playlist, chosen = _setup(client, fake_ma)
    key = list_key(playlist)
    _refuse_cancel(fake_ma, monkeypatch)
    _left_open(client, fake_ma, playlist, 2)
    fake_ma.ui_remove(key, chosen[0])
    with SessionLocal() as db:
        server_sync.request_all(db, server)
    events(server, fake_ma)
    sync()
    fake_ma.run_tasks()
    assert chosen[3] not in fake_ma.entries(key)
    for _round in range(2):
        with SessionLocal() as db:
            server_sync.request_all(db, server)
        events(server, fake_ma)
        sync()
    there = both_equal(fake_ma, server, playlist)
    assert chosen[3] in there and chosen[2] not in there
    assert set(chosen) - {chosen[0], chosen[2]} <= set(there)


def test_a_track_added_there_while_a_task_is_open_is_put_back_when_the_task_takes_it(
        client: TestClient, operator: object, fake_ma: FakeMA, monkeypatch: pytest.MonkeyPatch) -> None:
    """While nexmix's taking out of the last entry is left open, somebody replaces a track in Music Assistant (the
    first out, another in at the end): the late task then takes the new one by its place. nexmix saw it while it waited
    (it keeps what the playlist holds), so it goes back; only the meant one is gone."""
    server, playlist, chosen = _setup(client, fake_ma)
    key = list_key(playlist)
    extra = _extra(fake_ma, chosen)
    _refuse_cancel(fake_ma, monkeypatch)
    _left_open(client, fake_ma, playlist, 8)
    fake_ma.ui_remove(key, chosen[0])
    fake_ma.ui_add(key, extra)
    with SessionLocal() as db:
        server_sync.request_all(db, server)
    events(server, fake_ma)
    sync()
    assert mirror(playlist).error == "ma_task_open"
    fake_ma.run_tasks()
    assert extra not in fake_ma.entries(key)
    _settled(server, fake_ma)
    there = both_equal(fake_ma, server, playlist)
    assert extra in there and chosen[8] not in there
    assert set(chosen[1:8]) <= set(there)


def test_a_task_music_assistant_forgot_is_ended(client: TestClient, operator: object, fake_ma: FakeMA,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """Music Assistant started anew (its tasks with it): the task left open never runs, the playlist goes on."""
    server, playlist, chosen = _setup(client, fake_ma)
    _refuse_cancel(fake_ma, monkeypatch)
    _left_open(client, fake_ma, playlist, 5)
    assert mirror(playlist).error == "ma_task_open"
    fake_ma.tasks.clear()
    with SessionLocal() as db:
        server_sync.request_all(db, server)
    _settled(server, fake_ma)
    assert both_equal(fake_ma, server, playlist) == [one for place, one in enumerate(chosen) if place != 5]
    assert mirror(playlist).state == "synced"


def test_a_websocket_broken_while_a_task_waits_keeps_the_task(client: TestClient, operator: object,
                                                              fake_ma: FakeMA) -> None:
    """The websocket breaks while nexmix waits for its task: the task may still run, so it is kept until Music
    Assistant says it ended."""
    server, playlist, chosen = _setup(client, fake_ma)
    fake_ma.task_later = 10**6
    fake_ma.break_after.add("tasks/run")
    items = _entries(client, playlist)
    client.post(f"/api/v1/playlists/{playlist}/items/remove", json={"entries": [items[1]["id"]]})
    sync(rounds=1)
    row = mirror(playlist)
    assert isinstance(row.writing, dict) and row.writing.get("tasks"), row.writing
    fake_ma.task_later = 0
    fake_ma.run_tasks()
    music_assistant.forget()
    with SessionLocal() as db:
        server_sync.request_all(db, server)
    _settled(server, fake_ma)
    assert both_equal(fake_ma, server, playlist) == [one for place, one in enumerate(chosen) if place != 1]


# --- A token that is no admin's ---------------------------------------------------------------------------------------


def test_a_token_of_no_admin_is_refused_when_connecting(client: TestClient, operator: object,
                                                        fake_ma: FakeMA) -> None:
    token = fake_ma.make_token(role="user")
    answer = client.post("/api/v1/music-assistant/connect", json={"url": "http://ma.example.com:8095",
                                                                   "token": token})
    assert answer.status_code == 422
    assert "ma_not_admin" in answer.text and token not in answer.text
    with SessionLocal() as db:
        assert not list(db.scalars(select(MediaServer).where(MediaServer.kind == SERVER_MA)))


def test_a_token_that_lost_its_admin_writes_nothing_by_place(client: TestClient, operator: object,
                                                             fake_ma: FakeMA) -> None:
    """The token's user is no admin any more (seen when the websocket opens anew): nothing is taken out or sorted by
    place, the playlist says why, and the card says so."""
    server, playlist, chosen = _setup(client, fake_ma)
    fake_ma.set_role(fake_ma.token, "user")
    music_assistant.forget()
    removes = _removes(fake_ma)
    items = _entries(client, playlist)
    client.post(f"/api/v1/playlists/{playlist}/items/remove", json={"entries": [items[0]["id"]]})
    sync(rounds=2)
    assert _removes(fake_ma) == removes
    assert mirror(playlist).error == "ma_not_admin"
    assert client.get("/api/v1/music-assistant").json()["servers"][0]["not_admin"] is True
    assert chosen[0] in fake_ma.entries(list_key(playlist))
    # An admin again: everything goes through.
    fake_ma.set_role(fake_ma.token, "admin")
    music_assistant.forget()
    with SessionLocal() as db:
        server_sync.request_all(db, server)
    _settled(server, fake_ma)
    assert both_equal(fake_ma, server, playlist) == chosen[1:]
    assert client.get("/api/v1/music-assistant").json()["servers"][0]["not_admin"] is False


# --- Taking up a playlist after a lost answer -------------------------------------------------------------------------


def test_after_a_lost_answer_a_users_own_playlist_of_the_same_name_is_not_taken(
        client: TestClient, operator: object, fake_ma: FakeMA, monkeypatch: pytest.MonkeyPatch) -> None:
    """nexmix asks Music Assistant to make "Sommerfest"; the answer is lost and nothing was made. Meanwhile somebody
    makes a playlist "Sommerfest" with tracks of their own in Music Assistant. nexmix must not take it for its own."""
    server = connect(client, fake_ma)
    chosen = sorted(one.id for one in fake_ma.tracks.values() if one.mappings[0][0] == FILES)
    original = connector.MusicAssistantSource.create
    theirs_box: list[int] = []

    def lost(self: connector.MusicAssistantSource, title: str) -> object:
        if not theirs_box:
            theirs_box.append(fake_ma.ui_create("Sommerfest", chosen[5:9]))
            raise MaError("ma_unreachable")
        return original(self, title)

    monkeypatch.setattr(connector.MusicAssistantSource, "create", lost)
    made = make(client, server, "Sommerfest", numbers(server, chosen[:3]))
    playlist = int(made["id"])
    sync(rounds=2)
    theirs = theirs_box[0]
    with SessionLocal() as db:
        server_sync.request_all(db, server)
    sync()
    events(server, fake_ma)
    sync(look=True)
    assert mirror(playlist).ext_key != str(theirs), ("the user's own playlist was taken", fake_ma.titles(theirs))
    assert not set(numbers(server, chosen[5:9])) & set(local_tracks(playlist))
    assert fake_ma.entries(theirs) == chosen[5:9]


def test_after_a_lost_answer_the_empty_one_made_then_is_taken(client: TestClient, operator: object,
                                                              fake_ma: FakeMA) -> None:
    """The answer is lost after Music Assistant made the playlist: the empty one of exactly that name is nexmix's."""
    server = connect(client, fake_ma)
    chosen = sorted(one.id for one in fake_ma.tracks.values() if one.mappings[0][0] == FILES)
    fake_ma.break_after.add("music/playlists/create_playlist")
    made = make(client, server, "Herbstfest", numbers(server, chosen[:3]))
    playlist = int(made["id"])
    sync(rounds=2)
    music_assistant.forget()
    with SessionLocal() as db:
        server_sync.request_all(db, server)
    _settled(server, fake_ma)
    mine = [one for one in fake_ma.playlists.values() if one.editable]
    assert len(mine) == 1 and both_equal(fake_ma, server, playlist) == chosen[:3]


def test_after_a_lost_answer_two_empty_ones_of_the_name_are_neither_taken(
        client: TestClient, operator: object, fake_ma: FakeMA, monkeypatch: pytest.MonkeyPatch) -> None:
    server = connect(client, fake_ma)
    chosen = sorted(one.id for one in fake_ma.tracks.values() if one.mappings[0][0] == FILES)
    original = connector.MusicAssistantSource.create
    made_there: list[int] = []

    def lost(self: connector.MusicAssistantSource, title: str) -> object:
        if not made_there:
            made_there.append(fake_ma.ui_create("Winterfest"))
            made_there.append(fake_ma.ui_create("Winterfest"))
            raise MaError("ma_unreachable")
        return original(self, title)

    monkeypatch.setattr(connector.MusicAssistantSource, "create", lost)
    made = make(client, server, "Winterfest", numbers(server, chosen[:2]))
    playlist = int(made["id"])
    sync(rounds=2)
    with SessionLocal() as db:
        server_sync.request_all(db, server)
    sync()
    assert mirror(playlist).ext_key not in {str(one) for one in made_there}
    assert all(fake_ma.entries(one) == [] for one in made_there if one in fake_ma.playlists)


# --- A new name ---------------------------------------------------------------------------------------------------


def test_a_new_name_with_an_entry_outside_the_library_makes_no_loop(
        client: TestClient, operator: object, fake_ma: FakeMA) -> None:
    """An entry whose file is gone (outside the library) is passed over by Music Assistant when it is added again. A
    new name copies the playlist: the copy lacks that entry. The new name takes all the same, once; the entry stays
    behind with the old playlist (nexmix never showed it)."""
    server, playlist, chosen = _setup(client, fake_ma, count=4)
    key = list_key(playlist)
    fake_ma.playlists[key].entries.append((FILES, "Weg/Verschwunden (2000)/01 Fort.flac"))
    fake_ma.playlists[key].cached = list(fake_ma.playlists[key].entries)
    assert client.put(f"/api/v1/playlists/{playlist}", json={"name": "Neu benannt"}).status_code == 200
    for _try in range(5):
        with SessionLocal() as db:
            server_sync.request_all(db, server)
        sync()
    made = sum(1 for command, _args in fake_ma.calls if command == "music/playlists/create_playlist")
    deleted = sum(1 for command, _args in fake_ma.calls if command == "music/library/remove_item")
    state = mirror(playlist)
    assert made == 2 and deleted == 1, (made, deleted, state.state, state.error)
    assert state.state == "synced"
    assert fake_ma.playlists[list_key(playlist)].name == "Neu benannt"
    assert both_equal(fake_ma, server, playlist) == chosen
    with SessionLocal() as db:
        assert len(list(db.scalars(select(Playlist.id)))) == 1


def test_a_track_passed_over_in_the_copy_for_a_new_name_stays_as_missing(
        client: TestClient, operator: object, fake_ma: FakeMA, monkeypatch: pytest.MonkeyPatch) -> None:
    """Music Assistant passes over a track of its library when the playlist is copied for a new name: the new name
    takes, the track stays in nexmix with the note that Music Assistant lacks it, and nothing tries it again (no
    loop)."""
    server, playlist, chosen = _setup(client, fake_ma, count=5)
    skipped = chosen[2]
    original = fake_ma._entry_of

    def passing_over(uri: str) -> tuple[str, str] | None:
        if uri == f"library://track/{skipped}":
            return None
        return original(uri)

    monkeypatch.setattr(fake_ma, "_entry_of", passing_over)
    before = sum(1 for command, _args in fake_ma.calls if command == "music/playlists/add_playlist_tracks")
    assert client.put(f"/api/v1/playlists/{playlist}", json={"name": "Umbenannt"}).status_code == 200
    _settled(server, fake_ma)
    adds = sum(1 for command, _args in fake_ma.calls if command == "music/playlists/add_playlist_tracks")
    # The copy, once: what Music Assistant passed over is not offered again before the old one goes.
    assert adds - before == 1
    made = sum(1 for command, _args in fake_ma.calls if command == "music/playlists/create_playlist")
    for _try in range(3):
        with SessionLocal() as db:
            server_sync.request_all(db, server)
        _settled(server, fake_ma)
    assert adds == sum(1 for command, _args in fake_ma.calls if command == "music/playlists/add_playlist_tracks")
    assert made == 2
    assert fake_ma.playlists[list_key(playlist)].name == "Umbenannt"
    assert fake_ma.entries(list_key(playlist)) == [one for one in chosen if one != skipped]
    number = numbers(server, [skipped])[0]
    assert number in local_tracks(playlist)
    with SessionLocal() as db:
        hint = db.scalar(select(PlaylistItem.plex_hint).where(
            PlaylistItem.playlist_id == playlist, PlaylistItem.track_id == number, PlaylistItem.removed_at.is_(None)))
    assert hint == "missing"
    assert len([one for one in fake_ma.playlists.values() if one.editable]) == 1
    # Music Assistant takes it again; after the next run of the catalogue it goes back there, and stays in nexmix.
    monkeypatch.setattr(fake_ma, "_entry_of", original)
    with SessionLocal() as db:
        assert server_sync.after_catalogue(db, server) == 1
    _settled(server, fake_ma)
    there = both_equal(fake_ma, server, playlist)
    assert sorted(there) == sorted(chosen)


def test_a_track_passed_over_for_a_new_name_goes_back_after_the_next_catalogue_run_at_once(
        client: TestClient, operator: object, fake_ma: FakeMA, monkeypatch: pytest.MonkeyPatch) -> None:
    """The catalogue runs right after the new name took, before any other sync: the track kept as missing goes back
    to Music Assistant; it is never read as taken out there."""
    server, playlist, chosen = _setup(client, fake_ma, count=5)
    skipped = chosen[1]
    original = fake_ma._entry_of

    def passing_over(uri: str) -> tuple[str, str] | None:
        return None if uri == f"library://track/{skipped}" else original(uri)

    monkeypatch.setattr(fake_ma, "_entry_of", passing_over)
    assert client.put(f"/api/v1/playlists/{playlist}", json={"name": "Gleich wieder"}).status_code == 200
    sync()
    assert skipped not in fake_ma.entries(list_key(playlist))
    monkeypatch.setattr(fake_ma, "_entry_of", original)
    with SessionLocal() as db:
        assert server_sync.after_catalogue(db, server) == 1
    sync()
    _settled(server, fake_ma)
    assert sorted(both_equal(fake_ma, server, playlist)) == sorted(chosen)
