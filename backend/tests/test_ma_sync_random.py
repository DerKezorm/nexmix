"""Random runs against the stand-in Music Assistant: changes in nexmix and in Music Assistant's own interface, some of
them right while nexmix writes by place, restarts in between. After each run both sides hold the same, and every track
nobody took out is still there (``NEXMIX_MA_RUNS`` runs, 25 by default)."""

from __future__ import annotations

import os
import random

import pytest
from fastapi.testclient import TestClient

from app.services import music_assistant

from .fakema import FILES, FakeMA
from .ma_world import both_equal, connect, events, keys, list_key, make, numbers, sync
from .plex_world import local_tracks, playlist_row

RUNS = int(os.environ.get("NEXMIX_MA_RUNS", "25"))


@pytest.mark.parametrize("seed", range(RUNS))
def test_random_changes_on_both_sides_lose_nothing(seed: int, client: TestClient, operator: object,
                                                   fake_ma: FakeMA) -> None:
    rng = random.Random(seed)
    server = connect(client, fake_ma)
    pool = sorted(one.id for one in fake_ma.tracks.values() if one.mappings[0][0] == FILES)
    start = rng.sample(pool, rng.randint(3, 10))
    made = make(client, server, f"Zufall {seed}", numbers(server, start))
    playlist = int(made["id"])
    sync()
    key = list_key(playlist)
    removed: set[int] = set()
    added: set[int] = set(start)

    def nexmix_add() -> None:
        here = set(keys(server, local_tracks(playlist)))
        free = [one for one in pool if one not in here]
        if free:
            chosen = rng.sample(free, min(len(free), rng.randint(1, 3)))
            added.update(chosen)
            client.post(f"/api/v1/playlists/{playlist}/items", json={"tracks": numbers(server, chosen)})

    def nexmix_remove() -> None:
        items = client.get(f"/api/v1/playlists/{playlist}").json()["items"]
        if items:
            going = rng.sample(items, rng.randint(1, min(2, len(items))))
            removed.update(keys(server, [int(one["track"]) for one in going]))
            client.post(f"/api/v1/playlists/{playlist}/items/remove", json={"entries": [one["id"] for one in going]})

    def nexmix_move() -> None:
        items = client.get(f"/api/v1/playlists/{playlist}").json()["items"]
        if len(items) > 1:
            entry, after = rng.sample(items, 2)
            client.post(f"/api/v1/playlists/{playlist}/items/move",
                        json={"entry": entry["id"], "after": after["id"] if rng.random() < 0.8 else None})

    def nexmix_rename() -> None:
        client.put(f"/api/v1/playlists/{playlist}", json={"name": f"Zufall {seed} {rng.randint(1, 99)}"})

    def ui_add() -> None:
        current = list_key(playlist)
        if current not in fake_ma.playlists:
            return
        free = [one for one in pool if one not in fake_ma.entries(current)]
        if free:
            chosen = rng.choice(free)
            added.add(chosen)
            fake_ma.ui_add(current, chosen)

    def ui_remove() -> None:
        current = list_key(playlist)
        if current not in fake_ma.playlists:
            return
        there = fake_ma.entries(current)
        if there:
            chosen = rng.choice(there)
            removed.add(chosen)
            fake_ma.ui_remove(current, chosen)

    def ui_order() -> None:
        current = list_key(playlist)
        if current not in fake_ma.playlists:
            return
        there = fake_ma.entries(current)
        rng.shuffle(there)
        fake_ma.ui_order(current, there)

    def meanwhile() -> None:
        # Somebody changes it in Music Assistant right before nexmix's next task runs there.
        fake_ma.before_task = rng.choice([ui_add, ui_remove, ui_order])

    def restart() -> None:
        fake_ma.restart()
        sync(rounds=2)
        fake_ma.up()
        music_assistant.forget()

    steps = [nexmix_add, nexmix_add, nexmix_remove, nexmix_remove, nexmix_move, nexmix_move, ui_add, ui_remove,
             ui_order, meanwhile, meanwhile, nexmix_rename, restart]
    for _burst in range(rng.randint(3, 6)):
        for _step in range(rng.randint(1, 4)):
            rng.choice(steps)()
        if rng.random() < 0.5:
            events(server, fake_ma)
        sync(rounds=rng.randint(1, 4))
    fake_ma.before_task = None
    fake_ma.up()
    for _round in range(4):
        events(server, fake_ma)
        from app.db import SessionLocal
        from app.services import sync as server_sync

        with SessionLocal() as db:
            server_sync.request_all(db, server)
        sync(rounds=20)
    key = list_key(playlist)
    there = both_equal(fake_ma, server, playlist)
    # Nothing nobody took out is lost.
    assert (added - removed) <= set(there), sorted((added - removed) - set(there))
    # One playlist there, of nexmix's name.
    mine = [one for one in fake_ma.playlists.values() if one.editable]
    assert len(mine) == 1 and mine[0].id == key
    from app.services.sources.music_assistant import MusicAssistantSource

    assert MusicAssistantSource.same_title(playlist_row(playlist).name, mine[0].name)
