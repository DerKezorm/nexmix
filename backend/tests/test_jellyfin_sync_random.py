"""Random runs of the whole sync against the stand-in Jellyfin: changes on both sides (copies of a track made in
Jellyfin too), syncs at random times, Jellyfin away before or after a write, starting (503), and nexmix stopped in the
middle and started again. Every track is added or taken out at most once (by one side, or by both the same way), so the
end is known: untouched and added ones are there, taken-out ones are not, both sides alike, nothing twice in nexmix.

``NEXMIX_SYNC_RUNS`` sets how many runs (60 by default), ``NEXMIX_SYNC_SEED`` where the seeds start."""

from __future__ import annotations

import os
import random
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import Account
from app.services import jobs
from app.services import sync as server_sync

from .fakejellyfin import FakeJellyfin
from .jellyfin_world import connect, keys, list_key, make, numbers, sync
from .plex_world import entry_of, local_tracks, mirror

RUNS = int(os.environ.get("NEXMIX_SYNC_RUNS", "60"))
FIRST = int(os.environ.get("NEXMIX_SYNC_SEED", "0"))


@pytest.fixture
def jelly(client: TestClient, operator: Account, fake_jellyfin: FakeJellyfin) -> Iterator[tuple[FakeJellyfin, int]]:
    server = connect(client, fake_jellyfin)
    server_sync.forget()
    yield fake_jellyfin, server


def _restart() -> None:
    jobs.stopping.clear()
    with SessionLocal() as db:
        jobs.recover(db, server_sync.LANE)
        server_sync.settle(db)


def _fault(fake: FakeJellyfin, rng: random.Random) -> str:
    kind = rng.choice(("before", "after", "503", "stop"))
    if kind == "stop":
        at = len(fake.calls) + rng.randint(1, 8)

        def hook(count: int, _path: str) -> None:
            if count >= at:
                jobs.stopping.set()
                fake.hook = None

        fake.hook = hook
        sync(rounds=3)
        fake.hook = None
        _restart()
    elif kind == "503":
        fake.starting = rng.randint(1, 4)
        sync(rounds=2)
        fake.starting = 0
    else:
        fake.fail_writes, fake.fail_after = rng.randint(1, 2), kind == "after"
        sync(rounds=3)
        fake.fail_writes = 0
    return kind


@pytest.mark.parametrize("seed", range(FIRST, FIRST + RUNS))
def test_random_changes_on_both_sides_with_faults(client: TestClient, jelly: tuple[FakeJellyfin, int],
                                                  seed: int) -> None:
    fake, server = jelly
    rng = random.Random(7907 * seed + 29)
    pool = sorted(fake.tracks)
    start = rng.sample(pool, rng.randint(0, 8))
    playlist_id = int(make(client, server, f"Zufall {seed}", numbers(server, start))["id"])
    sync()
    key = list_key(playlist_id)
    touched: dict[str, str] = {}
    #: Copies of a track made in Jellyfin on purpose: how many.
    copied: dict[str, int] = {}
    log: list[str] = []
    for _step in range(rng.randint(6, 18)):
        roll = rng.random()
        here = keys(server, local_tracks(playlist_id))
        there = list(dict.fromkeys(fake.tracks_of(key))) if key in fake.lists else []
        if roll < 0.18:
            new = [t for t in pool if t not in here and touched.get(t, "add") == "add"]
            if new:
                chosen = rng.sample(new, min(len(new), rng.randint(1, 3)))
                for t in chosen:
                    touched[t] = "add"
                answer = client.post(f"/api/v1/playlists/{playlist_id}/items", json={"tracks": numbers(server, chosen)})
                assert answer.status_code == 200, answer.text
                log.append(f"nx add {chosen}")
        elif roll < 0.30:
            old = [t for t in here if touched.get(t, "remove") == "remove"]
            if old:
                t = rng.choice(old)
                touched[t] = "remove"
                answer = client.post(f"/api/v1/playlists/{playlist_id}/items/remove",
                                     json={"entries": [entry_of(client, playlist_id, numbers(server, [t])[0])]})
                assert answer.status_code == 200, answer.text
                log.append(f"nx remove {t}")
        elif roll < 0.40 and len(here) > 1:
            t = rng.choice(here)
            after = rng.choice([None, *[x for x in here if x != t]])
            answer = client.post(f"/api/v1/playlists/{playlist_id}/items/move", json={
                "entry": entry_of(client, playlist_id, numbers(server, [t])[0]),
                "after": entry_of(client, playlist_id, numbers(server, [after])[0]) if after else None})
            assert answer.status_code == 200, answer.text
            log.append(f"nx move {t} after {after}")
        elif roll < 0.50:
            new = [t for t in pool if t not in there and touched.get(t, "add") == "add"]
            if new and key in fake.lists:
                chosen = rng.sample(new, min(len(new), rng.randint(1, 2)))
                for t in chosen:
                    touched[t] = "add"
                fake.list_add(key, chosen)
                log.append(f"jf add {chosen}")
        elif roll < 0.55 and there and key in fake.lists:
            # A copy of a track already there: it counts as the one.
            t = rng.choice(there)
            fake.list_add(key, [t])
            copied[t] = copied.get(t, 0) + 1
            log.append(f"jf copy {t}")
        elif roll < 0.64:
            old = [t for t in there if touched.get(t, "remove") == "remove"]
            # Emptied here too: every track is still in the library, so that is taken as done on purpose.
            if old and key in fake.lists:
                t = rng.choice(old)
                touched[t] = "remove"
                fake.list_remove(key, [t])
                log.append(f"jf remove {t}")
        elif roll < 0.72 and len(there) > 1 and key in fake.lists:
            t = rng.choice(there)
            fake.list_move(key, t, rng.randrange(len(fake.tracks_of(key))))
            log.append(f"jf move {t}")
        elif roll < 0.86:
            log.append("sync")
            sync(look=rng.random() < 0.7)
        else:
            log.append("fault " + _fault(fake, rng))
        key = mirror(playlist_id).ext_key or key
    fake.fail_writes, fake.starting, fake.hook = 0, 0, None
    jobs.stopping.clear()
    for _round in range(4):
        with SessionLocal() as db:
            server_sync.request_all(db)
        sync(look=True)
    key = list_key(playlist_id)
    here = keys(server, local_tracks(playlist_id))
    there = list(dict.fromkeys(fake.tracks_of(key)))
    info = (seed, start, log, here, fake.tracks_of(key))
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
    # nexmix never makes a copy in Jellyfin: a track stands there more than once only by the copies made there.
    raw = fake.tracks_of(key)
    for t in set(raw):
        assert raw.count(t) <= 1 + copied.get(t, 0), ("copy in Jellyfin made by nexmix", t, info)
