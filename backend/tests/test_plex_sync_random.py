"""Random runs of the whole sync against the stand-in Plex: changes on both sides, syncs at random times, faults and stops
at random requests. Every track is added or taken out at most once (by one side, or by both the same way), so the end
is known: untouched and added ones are there, taken-out ones are not, both sides alike, nothing twice.

``NEXMIX_SYNC_RUNS`` sets how many runs (60 by default; the release check runs 2,000), ``NEXMIX_SYNC_SEED`` where the
seeds start."""

from __future__ import annotations

import os
import random
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import Account
from app.services import jobs, plex_sync

from .fakeplex import FakePlex
from .plex_world import connect, entry_of, local_tracks, mirror, plex_key, sync
from .test_plex_sync import synced

POOL = list(range(3001, 3033))
RUNS = int(os.environ.get("NEXMIX_SYNC_RUNS", "60"))
FIRST = int(os.environ.get("NEXMIX_SYNC_SEED", "0"))


@pytest.fixture
def world(client: TestClient, operator: Account, fake_plex: FakePlex) -> Iterator[FakePlex]:
    connect(client, fake_plex)
    plex_sync.forget()
    yield fake_plex


def _restart() -> None:
    jobs.stopping.clear()
    with SessionLocal() as db:
        jobs.recover(db, plex_sync.LANE)
        plex_sync.settle(db)


def _fault_sync(world: FakePlex, rng: random.Random) -> str:
    kind = rng.choice(("drop", "503", "timeout", "stop"))
    at = world.library_requests + rng.randint(0, 9)
    if kind == "stop":
        def hook(count: int, _path: str) -> None:
            if count > at:
                jobs.stopping.set()
                world.hook = None
        world.hook = hook
        sync(world, rounds=3)
        world.hook = None
        _restart()
    else:
        world.fail_after = at
        world.fail_mode = kind
        sync(world, rounds=3)
        world.fail_after = None
    return kind


def _has_key(playlist_id: int) -> bool:
    return mirror(playlist_id).rating_key is not None


@pytest.mark.parametrize("seed", range(FIRST, FIRST + RUNS))
def test_random_changes_on_both_sides_with_faults(client: TestClient, world: FakePlex, seed: int) -> None:
    rng = random.Random(7919 * seed + 13)
    start = rng.sample(POOL, rng.randint(0, 10))
    playlist_id, key = synced(client, world, f"Zufall {seed}", start)
    touched: dict[int, str] = {}
    log: list[str] = []
    for _step in range(rng.randint(6, 18)):
        roll = rng.random()
        here = local_tracks(playlist_id)
        there = world.lists.tracks(key) if key in world.lists.lists else []
        if roll < 0.18:
            new = [t for t in POOL if t not in here and touched.get(t, "add") == "add"]
            if new:
                chosen = rng.sample(new, min(len(new), rng.randint(1, 3)))
                for t in chosen:
                    touched[t] = "add"
                answer = client.post(f"/api/v1/playlists/{playlist_id}/items", json={"tracks": chosen})
                assert answer.status_code == 200, answer.text
                log.append(f"nx add {chosen}")
        elif roll < 0.30:
            old = [t for t in here if touched.get(t, "remove") == "remove"]
            if old:
                t = rng.choice(old)
                touched[t] = "remove"
                answer = client.post(f"/api/v1/playlists/{playlist_id}/items/remove",
                                     json={"entries": [entry_of(client, playlist_id, t)]})
                assert answer.status_code == 200, answer.text
                log.append(f"nx remove {t}")
        elif roll < 0.40 and len(here) > 1:
            t = rng.choice(here)
            after = rng.choice([None, *[x for x in here if x != t]])
            answer = client.post(f"/api/v1/playlists/{playlist_id}/items/move",
                                 json={"entry": entry_of(client, playlist_id, t),
                                       "after": entry_of(client, playlist_id, after) if after else None})
            assert answer.status_code == 200, answer.text
            log.append(f"nx move {t} after {after}")
        elif roll < 0.52:
            new = [t for t in POOL if t not in there and touched.get(t, "add") == "add"]
            if new and key in world.lists.lists:
                chosen = rng.sample(new, min(len(new), rng.randint(1, 2)))
                for t in chosen:
                    touched[t] = "add"
                world.lists.add(key, chosen)
                world.tick()
                log.append(f"px add {chosen}")
        elif roll < 0.62:
            old = [t for t in there if touched.get(t, "remove") == "remove"]
            if old and key in world.lists.lists:
                t = rng.choice(old)
                touched[t] = "remove"
                world.lists.remove_track(key, t)
                world.tick()
                log.append(f"px remove {t}")
        elif roll < 0.70 and len(there) > 1 and key in world.lists.lists:
            t = rng.choice(there)
            after = rng.choice([None, *[x for x in there if x != t]])
            world.lists.move_track(key, t, after)
            log.append(f"px move {t} after {after}")
        elif roll < 0.82:
            log.append("sync")
            world.tick(rng.randint(0, 3))
            sync(world, look_at_plex=rng.random() < 0.7)
        else:
            log.append("fault " + _fault_sync(world, rng))
        key = plex_key(playlist_id) if _has_key(playlist_id) else key
    # Healed: everything due, looked at until quiet.
    world.fail_after = None
    world.hook = None
    jobs.stopping.clear()
    for _round in range(4):
        with SessionLocal() as db:
            plex_sync.request_all(db)
        world.tick(20)
        sync(world, look_at_plex=True)
    key = plex_key(playlist_id)
    here = local_tracks(playlist_id)
    there = world.lists.tracks(key)
    info = (seed, start, log, here, there)
    assert here == there, ("sides differ", info)
    assert len(set(here)) == len(here), ("twice", info)
    for t in start:
        if t not in touched:
            assert t in here, ("lost (untouched)", t, info)
    for t, what in touched.items():
        if what == "add":
            assert t in here, ("lost (added)", t, info)
        else:
            assert t not in here, ("came back (taken out)", t, info)
    assert mirror(playlist_id).writing is None
