"""Random runs of a playlist with its source on the stand-in Plex and a mirror on the stand-in Jellyfin: changes in
nexmix, in Plex and in Jellyfin at random times, syncs in between, Jellyfin away, starting (503) or failing a write
(before or after doing it), and nexmix stopped in the middle and started again. Every track is added or taken out at
most once (by one side, or by several the same way), so the end is known: untouched and added ones are there, taken-out
ones are not, nothing twice anywhere, and all three alike, but the tracks Jellyfin does not have, which stay in nexmix
and Plex and are marked as not on Jellyfin.

``NEXMIX_MIRROR_RUNS`` sets how many runs (40 by default), ``NEXMIX_MIRROR_SEED`` where the seeds start."""

from __future__ import annotations

import os
import random

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import Account
from app.services import jobs
from app.services import sync as server_sync

from .fakejellyfin import FakeJellyfin
from .fakeplex import FakePlex
from .fakeplex.library import TRACK
from .mirror_world import connect_both, copy_row, detail, mirror_key, plex_numbers
from .plex_world import entry_of, sync

RUNS = int(os.environ.get("NEXMIX_MIRROR_RUNS", "40"))
FIRST = int(os.environ.get("NEXMIX_MIRROR_SEED", "0"))
LEFT_OUT = frozenset({3021, 3030})


def _restart() -> None:
    jobs.stopping.clear()
    with SessionLocal() as db:
        jobs.recover(db, server_sync.LANE)
        server_sync.settle(db)


def _fault(fake: FakeJellyfin, fake_plex: FakePlex, rng: random.Random) -> str:
    kind = rng.choice(("before", "after", "503", "stop", "down"))
    if kind == "stop":
        at = len(fake.calls) + rng.randint(1, 8)

        def hook(count: int, _path: str) -> None:
            if count >= at:
                jobs.stopping.set()
                fake.hook = None

        fake.hook = hook
        sync(fake_plex, rounds=3)
        fake.hook = None
        _restart()
    elif kind == "503":
        fake.starting = rng.randint(1, 4)
        sync(fake_plex, rounds=2)
        fake.starting = 0
    elif kind == "down":
        fake.down.add("jellyfin.example.com")
        sync(fake_plex, rounds=2)
        fake.down.clear()
    else:
        fake.fail_writes, fake.fail_after = rng.randint(1, 2), kind == "after"
        sync(fake_plex, rounds=3)
        fake.fail_writes = 0
    return kind


@pytest.mark.parametrize("seed", range(FIRST, FIRST + RUNS))
def test_random_changes_on_the_source_the_mirror_and_in_nexmix(
        client: TestClient, operator: Account, fake_plex: FakePlex, fake_jellyfin: FakeJellyfin, seed: int) -> None:
    fake = fake_jellyfin
    _plex, jelly, twins = connect_both(client, fake_plex, fake, LEFT_OUT)
    server_sync.forget()
    rng = random.Random(6007 * seed + 41)
    pool = sorted(key for key, item in fake_plex.items.items() if item.kind == TRACK)
    start = rng.sample(pool, rng.randint(0, 7))
    made = client.post("/api/v1/playlists", json={"name": f"Zufall {seed}"}).json()
    playlist_id = int(made["id"])
    if start:
        assert client.post(f"/api/v1/playlists/{playlist_id}/items", json={"tracks": plex_numbers(start)}).status_code \
            == 200
    sync(fake_plex)
    assert client.post(f"/api/v1/playlists/{playlist_id}/mirrors", json={"server": jelly}).status_code == 201
    sync(fake_plex)
    touched: dict[int, str] = {}
    log: list[str] = []
    back = {ident: key for key, ident in twins.items()}

    def here() -> list[int]:
        from .mirror_world import plex_tracks_here

        return plex_tracks_here(playlist_id)

    def on_plex() -> list[int]:
        return fake_plex.lists.tracks(int(mirror_key(playlist_id, 1)))

    def on_jelly() -> list[int]:
        key = copy_row(playlist_id, jelly).ext_key
        return [back[one] for one in dict.fromkeys(fake.tracks_of(key))] if key and key in fake.lists else []

    for _step in range(rng.randint(8, 20)):
        roll = rng.random()
        mine = here()
        if roll < 0.12:
            new = [t for t in pool if t not in mine and touched.get(t, "add") == "add"]
            if new:
                chosen = rng.sample(new, min(len(new), rng.randint(1, 3)))
                touched.update(dict.fromkeys(chosen, "add"))
                assert client.post(f"/api/v1/playlists/{playlist_id}/items",
                                   json={"tracks": plex_numbers(chosen)}).status_code == 200
                log.append(f"nx add {chosen}")
        elif roll < 0.20:
            old = [t for t in mine if touched.get(t, "remove") == "remove"]
            if old:
                t = rng.choice(old)
                touched[t] = "remove"
                assert client.post(f"/api/v1/playlists/{playlist_id}/items/remove", json={
                    "entries": [entry_of(client, playlist_id, plex_numbers([t])[0])]}).status_code == 200
                log.append(f"nx remove {t}")
        elif roll < 0.26 and len(mine) > 1:
            t = rng.choice(mine)
            after = rng.choice([None, *[x for x in mine if x != t]])
            assert client.post(f"/api/v1/playlists/{playlist_id}/items/move", json={
                "entry": entry_of(client, playlist_id, plex_numbers([t])[0]),
                "after": entry_of(client, playlist_id, plex_numbers([after])[0]) if after else None}).status_code \
                == 200
            log.append(f"nx move {t} after {after}")
        elif roll < 0.34:
            there = on_plex()
            new = [t for t in pool if t not in there and touched.get(t, "add") == "add"]
            if new:
                chosen = rng.sample(new, min(len(new), rng.randint(1, 2)))
                touched.update(dict.fromkeys(chosen, "add"))
                fake_plex.lists.add(int(mirror_key(playlist_id, 1)), chosen)
                log.append(f"plex add {chosen}")
        elif roll < 0.40:
            old = [t for t in on_plex() if touched.get(t, "remove") == "remove"]
            if old:
                t = rng.choice(old)
                touched[t] = "remove"
                fake_plex.lists.remove_track(int(mirror_key(playlist_id, 1)), t)
                log.append(f"plex remove {t}")
        elif roll < 0.44 and len(on_plex()) > 1:
            there = on_plex()
            t = rng.choice(there)
            after = rng.choice([None, *[x for x in there if x != t]])
            fake_plex.lists.move_track(int(mirror_key(playlist_id, 1)), t, after)
            log.append(f"plex move {t} after {after}")
        elif roll < 0.54:
            there = on_jelly()
            key = copy_row(playlist_id, jelly).ext_key
            new = [t for t in pool if t in twins and t not in there and touched.get(t, "add") == "add"]
            if new and key in fake.lists:
                chosen = rng.sample(new, min(len(new), rng.randint(1, 2)))
                touched.update(dict.fromkeys(chosen, "add"))
                fake.list_add(key, [twins[t] for t in chosen])
                log.append(f"jf add {chosen}")
        elif roll < 0.61:
            key = copy_row(playlist_id, jelly).ext_key
            old = [t for t in on_jelly() if touched.get(t, "remove") == "remove"]
            if old and key in fake.lists:
                t = rng.choice(old)
                touched[t] = "remove"
                fake.list_remove(key, [twins[t]])
                log.append(f"jf remove {t}")
        elif roll < 0.67:
            key = copy_row(playlist_id, jelly).ext_key
            there = on_jelly()
            if len(there) > 1 and key in fake.lists:
                t = rng.choice(there)
                fake.list_move(key, twins[t], rng.randrange(len(fake.tracks_of(key))))
                log.append(f"jf move {t}")
        elif roll < 0.88:
            log.append("sync")
            sync(fake_plex, look_at_plex=rng.random() < 0.7)
        else:
            log.append("fault " + _fault(fake, fake_plex, rng))
    fake.fail_writes, fake.starting, fake.hook = 0, 0, None
    fake.down.clear()
    jobs.stopping.clear()
    for _round in range(5):
        with SessionLocal() as db:
            server_sync.request_all(db)
        sync(fake_plex, look_at_plex=True)
    mine, plex_side, jelly_side = here(), on_plex(), on_jelly()
    info = (seed, start, log, mine, plex_side, jelly_side)
    assert mine == plex_side, ("nexmix and Plex differ", info)
    assert jelly_side == [t for t in mine if t not in LEFT_OUT], ("Jellyfin differs", info)
    assert len(set(mine)) == len(mine), ("twice", info)
    raw = fake.tracks_of(mirror_key(playlist_id, jelly))
    assert len(raw) == len(set(raw)), ("twice in Jellyfin", info)
    for t in start:
        if t not in touched:
            assert t in mine, ("lost (untouched)", t, info)
    for t, what in touched.items():
        if what == "add":
            assert t in mine, ("lost (added)", t, info)
        else:
            assert t not in mine, ("came back (taken out)", t, info)
    # The tracks Jellyfin lacks are marked as not there, exactly those.
    view = detail(client, playlist_id)
    absent = sorted(item["track"] for item in view["items"] if item["absent"] == [jelly])
    assert absent == sorted(plex_numbers([t for t in mine if t in LEFT_OUT])), ("marks", info)
    assert view["mirrors"][0]["state"] == "synced", ("state", info)
    assert copy_row(playlist_id, jelly).writing is None and copy_row(playlist_id, 1).writing is None
