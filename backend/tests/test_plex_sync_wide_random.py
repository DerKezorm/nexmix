"""Random runs over the whole sync with Plex, wider than ``test_plex_sync_random``: several playlists at once, the same
track added, taken out and added again on one side, names changed on both sides, playlists deleted in nexmix (trash,
bring back, delete for good) and in Plex (also while Plex cannot be reached), playlists made in Plex, a guest adding and
taking out, faults at random requests (a broken connection, 503, a timeout, a stop and a start again, and a write that
Plex did but whose answer was lost), outages over several steps.

Every track of a playlist belongs to one side for the whole run (nexmix with its guest, or Plex): what that side did
last decides whether it is there at the end. Changes Plex had not yet handed over when Plex deleted the playlist are
lost with it (Plex deleted the playlist); changes of nexmix never are. At the end, after a quiet sync: both sides alike,
nothing twice, no copy in Plex without its playlist in nexmix, no copy left of a playlist in the trash, the name of the
only side that renamed. Then the order: sorted only in nexmix, then only in Plex, it holds on both sides.

``NEXMIX_WIDE_RUNS`` sets how many runs (10 by default, the release check runs 200), ``NEXMIX_WIDE_SEED`` where the
seeds start; ``NEXMIX_WIDE_LOG`` names a file for the steps of the last run."""

from __future__ import annotations

import os
import random
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.main import app
from app.models import Account, Playlist, PlaylistItem, PlexPlaylist
from app.services import jobs, plex_sync

from .conftest import TAB
from .fakeplex import FakePlex
from .plex_world import connect, local_tracks, sync

POOL = list(range(3001, 3033))
RUNS = int(os.environ.get("NEXMIX_WIDE_RUNS", "10"))
FIRST = int(os.environ.get("NEXMIX_WIDE_SEED", "0"))


class Lost:
    """A write Plex carries out, whose answer never arrives (the connection breaks after Plex did it)."""

    def __init__(self, fake: FakePlex) -> None:
        self.after: int | None = None
        self.count = 0
        original = fake.lists.handle

        def handle(request: httpx.Request, path: str, params: dict[str, str]) -> httpx.Response | None:
            answer = original(request, path, params)
            if answer is not None and request.method != "GET" and self.after is not None:
                self.count += 1
                if self.count > self.after:
                    self.after = None
                    raise httpx.RemoteProtocolError("Server disconnected without sending a response.",
                                                    request=request)
            return answer

        fake.lists.handle = handle  # type: ignore[method-assign]

    def arm(self, after: int) -> None:
        self.after, self.count = after, 0


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


def _mirror(playlist_id: int) -> PlexPlaylist | None:
    with SessionLocal() as db:
        row = db.scalars(select(PlexPlaylist).where(PlexPlaylist.playlist_id == playlist_id)).first()
        if row is not None:
            db.expunge(row)
    return row


def _key(playlist_id: int) -> int | None:
    row = _mirror(playlist_id)
    return row.rating_key if row is not None else None


def _row(playlist_id: int) -> Playlist | None:
    with SessionLocal() as db:
        row = db.get(Playlist, playlist_id)
        if row is not None:
            db.expunge(row)
    return row


def _entry(playlist_id: int, track: int) -> int:
    with SessionLocal() as db:
        found = db.scalar(select(PlaylistItem.id).where(PlaylistItem.playlist_id == playlist_id,
                                                        PlaylistItem.track_id == track,
                                                        PlaylistItem.removed_at.is_(None)))
    assert found is not None, (playlist_id, track)
    return int(found)


class Run:
    def __init__(self, client: TestClient, world: FakePlex, seed: int) -> None:
        self.client = client
        self.world = world
        self.rng = random.Random(104_729 * seed + 31)
        self.seed = seed
        self.lost = Lost(world)
        self.log: list[str] = []
        #: nexmix id -> {"own": {track: "nx"|"px"}, "exp": {track: bool|None}, "nx_name": str|None,
        #: "px_name": str|None, "plex_gone": bool}
        self.lists: dict[int, dict[str, Any]] = {}
        #: Plex keys of playlists made in Plex by the run, with their tracks; None once deleted there.
        self.made: dict[int, list[int] | None] = {}
        self.guest: TestClient | None = None
        self.guest_list: int | None = None
        self.outage = 0
        #: Playlists deleted for good, with when they were made: SQLite gives a later playlist the same id again.
        self.purged: dict[int, datetime] = {}

    # --- helpers

    def _state(self, pid: int) -> str:
        if pid in self.purged:
            return "purged"
        row = _row(pid)
        return "gone" if row is None else ("trash" if row.deleted_at is not None else "alive")

    def alive(self) -> list[int]:
        return [pid for pid in self.lists if self._state(pid) == "alive"]

    def trashed(self) -> list[int]:
        return [pid for pid in self.lists if self._state(pid) == "trash"]

    def plex_side(self, pid: int) -> int | None:
        key = _key(pid)
        return key if key is not None and key in self.world.lists.lists else None

    def new_playlist(self, name: str) -> None:
        start = self.rng.sample(POOL, self.rng.randint(0, 8))
        made = self.client.post("/api/v1/playlists", json={"name": name})
        assert made.status_code == 201, made.text
        pid = int(made.json()["id"])
        if start:
            answer = self.client.post(f"/api/v1/playlists/{pid}/items", json={"tracks": start})
            assert answer.status_code == 200, answer.text
        own = {t: self.rng.choice(("nx", "px")) for t in POOL}
        self.lists[pid] = {"own": own, "exp": {t: (t in start) for t in POOL}, "nx_name": None, "px_name": None,
                           "plex_gone": False, "names_unsure": False}
        self.log.append(f"make {pid} {start}")

    # --- the steps

    def nx_add(self, pid: int) -> None:
        data = self.lists[pid]
        here = set(local_tracks(pid))
        new = [t for t in POOL if data["own"][t] == "nx" and t not in here]
        if not new:
            return
        chosen = self.rng.sample(new, min(len(new), self.rng.randint(1, 3)))
        answer = self.client.post(f"/api/v1/playlists/{pid}/items", json={"tracks": chosen})
        assert answer.status_code == 200, answer.text
        for t in chosen:
            data["exp"][t] = True
        self.log.append(f"nx add {pid} {chosen}")

    def nx_remove(self, pid: int) -> None:
        data = self.lists[pid]
        old = [t for t in local_tracks(pid) if data["own"][t] == "nx"]
        if not old:
            return
        t = self.rng.choice(old)
        answer = self.client.post(f"/api/v1/playlists/{pid}/items/remove", json={"entries": [_entry(pid, t)]})
        assert answer.status_code == 200, answer.text
        data["exp"][t] = False
        self.log.append(f"nx remove {pid} {t}")

    def nx_move(self, pid: int) -> None:
        here = local_tracks(pid)
        if len(here) < 2:
            return
        t = self.rng.choice(here)
        after = self.rng.choice([None, *[x for x in here if x != t]])
        answer = self.client.post(f"/api/v1/playlists/{pid}/items/move",
                                  json={"entry": _entry(pid, t), "after": _entry(pid, after) if after else None})
        assert answer.status_code == 200, answer.text
        self.log.append(f"nx move {pid} {t} after {after}")

    def nx_rename(self, pid: int) -> None:
        name = f"N{self.seed}-{pid}-{self.rng.randint(1, 10**6)}"
        answer = self.client.put(f"/api/v1/playlists/{pid}", json={"name": name})
        assert answer.status_code == 200, answer.text
        self.lists[pid]["nx_name"] = name
        self.log.append(f"nx rename {pid} {name}")

    def nx_trash(self, pid: int) -> None:
        answer = self.client.delete(f"/api/v1/playlists/{pid}")
        assert answer.status_code == 200, answer.text
        data = self.lists[pid]
        # What Plex took out and nexmix did not hear of yet is not looked for in a playlist going away.
        for t in POOL:
            if data["own"][t] == "px" and data["exp"][t] is False:
                data["exp"][t] = None
        # A name given in Plex and not yet heard of goes with the copy there, as its removals do.
        if data["px_name"]:
            data["px_name"], data["names_unsure"] = None, True
        self.log.append(f"nx trash {pid}")

    def nx_restore(self, pid: int) -> None:
        answer = self.client.post(f"/api/v1/playlists/{pid}/restore")
        assert answer.status_code == 200, answer.text
        self.log.append(f"nx restore {pid}")

    def nx_purge(self, pid: int) -> None:
        made = _row(pid)
        assert made is not None
        answer = self.client.delete(f"/api/v1/playlists/{pid}", params={"purge": "true"})
        assert answer.status_code == 200, answer.text
        self.purged[pid] = made.created_at
        self.log.append(f"nx purge {pid}")

    def guest_join(self, pid: int) -> None:
        assert self.client.post(f"/api/v1/playlists/{pid}/invite").status_code == 200
        answer = self.client.put(f"/api/v1/playlists/{pid}/invite", json={"start": True, "max": 100})
        assert answer.status_code == 200, answer.text
        code = answer.json()["invite"]["code"]
        phone = TestClient(app, base_url="http://testserver", headers=TAB, client=(f"phone-{self.seed}", 50000))
        assert phone.post(f"/api/v1/join/{code}", json={"name": "Lena"}).status_code == 200
        self.guest, self.guest_list = phone, pid
        self.log.append(f"guest joins {pid}")

    def guest_step(self) -> None:
        assert self.guest is not None and self.guest_list is not None
        pid = self.guest_list
        data = self.lists[pid]
        view = self.guest.get("/api/v1/guest")
        if view.status_code != 200:
            return
        items = view.json()["playlist"]["items"]
        if self.rng.random() < 0.6:
            here = {item["track"] for item in items}
            new = [t for t in POOL if data["own"][t] == "nx" and t not in here]
            if not new:
                return
            chosen = self.rng.sample(new, min(len(new), self.rng.randint(1, 2)))
            answer = self.guest.post("/api/v1/guest/items", json={"tracks": chosen})
            if answer.status_code == 200:
                skipped = set(answer.json().get("skipped") or [])
                for t in chosen:
                    if t not in skipped:
                        data["exp"][t] = True
                self.log.append(f"guest add {pid} {chosen}")
        else:
            mine = [item for item in items if item["mine"]]
            if not mine:
                return
            item = self.rng.choice(mine)
            answer = self.guest.post("/api/v1/guest/items/remove", json={"entries": [item["id"]]})
            if answer.status_code == 200:
                data["exp"][item["track"]] = False
                self.log.append(f"guest remove {pid} {item['track']}")

    def px_add(self, pid: int, key: int) -> None:
        data = self.lists[pid]
        there = self.world.lists.tracks(key)
        new = [t for t in POOL if data["own"][t] == "px" and t not in there]
        if not new:
            return
        chosen = self.rng.sample(new, min(len(new), self.rng.randint(1, 2)))
        self.world.lists.add(key, chosen)
        self.world.tick()
        for t in chosen:
            data["exp"][t] = True
        self.log.append(f"px add {pid} {chosen}")

    def px_remove(self, pid: int, key: int) -> None:
        data = self.lists[pid]
        old = [t for t in self.world.lists.tracks(key) if data["own"][t] == "px"]
        if not old:
            return
        t = self.rng.choice(old)
        self.world.lists.remove_track(key, t)
        self.world.tick()
        data["exp"][t] = False
        self.log.append(f"px remove {pid} {t}")

    def px_move(self, pid: int, key: int) -> None:
        there = self.world.lists.tracks(key)
        if len(there) < 2:
            return
        t = self.rng.choice(there)
        after = self.rng.choice([None, *[x for x in there if x != t]])
        self.world.lists.move_track(key, t, after)
        self.log.append(f"px move {pid} {t} after {after}")

    def px_rename(self, pid: int, key: int) -> None:
        name = f"P{self.seed}-{pid}-{self.rng.randint(1, 10**6)}"
        self.world.lists.rename(key, name)
        self.world.tick()
        self.lists[pid]["px_name"] = name
        self.log.append(f"px rename {pid} {name}")

    def px_delete(self, pid: int, key: int) -> None:
        self.world.lists.delete(key)
        self.world.tick()
        data = self.lists[pid]
        data["plex_gone"] = True
        for t in POOL:
            if data["own"][t] == "px":
                data["exp"][t] = None
        self.log.append(f"px delete {pid}")

    def px_make(self) -> None:
        tracks = self.rng.sample(POOL, self.rng.randint(1, 5))
        made = self.world.lists.make(f"Plex {self.seed}-{len(self.made)}", tracks)
        self.world.tick()
        self.made[made.key] = tracks
        self.log.append(f"px make {made.key} {tracks}")

    def fault(self) -> None:
        kind = self.rng.choice(("drop", "503", "timeout", "stop", "lost", "lost"))
        at = self.world.library_requests + self.rng.randint(0, 9)
        if kind == "stop":
            def hook(count: int, _path: str) -> None:
                if count > at:
                    jobs.stopping.set()
                    self.world.hook = None
            self.world.hook = hook
            sync(self.world, rounds=3, look_at_plex=self.rng.random() < 0.5)
            self.world.hook = None
            _restart()
        elif kind == "lost":
            self.lost.arm(self.rng.randint(0, 4))
            sync(self.world, rounds=3, look_at_plex=self.rng.random() < 0.5)
            self.lost.after = None
        else:
            self.world.fail_after = at
            self.world.fail_mode = kind
            sync(self.world, rounds=3, look_at_plex=self.rng.random() < 0.5)
            self.world.fail_after = None
        self.log.append(f"fault {kind}")

    def step(self) -> None:
        if self.outage:
            self.outage -= 1
            if not self.outage:
                self.world.fail_after = None
                self.log.append("outage over")
        roll = self.rng.random()
        alive = self.alive()
        pid = self.rng.choice(alive) if alive else None
        key = self.plex_side(pid) if pid is not None else None
        if roll < 0.13 and pid is not None:
            self.nx_add(pid)
        elif roll < 0.22 and pid is not None:
            self.nx_remove(pid)
        elif roll < 0.28 and pid is not None:
            self.nx_move(pid)
        elif roll < 0.31 and pid is not None:
            self.nx_rename(pid)
        elif roll < 0.42 and pid is not None and key is not None:
            self.px_add(pid, key)
        elif roll < 0.50 and pid is not None and key is not None:
            self.px_remove(pid, key)
        elif roll < 0.55 and pid is not None and key is not None:
            self.px_move(pid, key)
        elif roll < 0.58 and pid is not None and key is not None:
            self.px_rename(pid, key)
        elif roll < 0.60 and pid is not None and key is not None:
            self.px_delete(pid, key)
        elif roll < 0.62 and pid is not None:
            self.nx_trash(pid)
        elif roll < 0.645:
            trashed = self.trashed()
            if trashed:
                self.nx_restore(self.rng.choice(trashed))
        elif roll < 0.655:
            trashed = self.trashed()
            if trashed:
                self.nx_purge(self.rng.choice(trashed))
        elif roll < 0.67:
            self.px_make()
        elif roll < 0.73 and self.guest is not None:
            self.guest_step()
        elif roll < 0.75 and not self.outage:
            # Plex away for a few steps, whatever happens meanwhile.
            self.outage = self.rng.randint(2, 6)
            self.world.fail_after = self.world.library_requests
            self.world.fail_mode = self.rng.choice(("503", "drop", "timeout"))
            self.log.append(f"outage {self.world.fail_mode} {self.outage}")
        elif roll < 0.90:
            self.world.tick(self.rng.randint(0, 3))
            sync(self.world, look_at_plex=self.rng.random() < 0.7)
            self.log.append("sync")
        elif not self.outage:
            self.fault()

    def heal(self) -> None:
        self.world.fail_after = None
        self.world.hook = None
        self.lost.after = None
        self.outage = 0
        jobs.stopping.clear()
        for _round in range(5):
            with SessionLocal() as db:
                plex_sync.request_all(db)
            self.world.tick(20)
            sync(self.world, look_at_plex=True)

    # --- the end

    def dump(self) -> None:
        """``NEXMIX_WIDE_LOG``: the steps of the run written to that file, to follow a red seed."""
        target = os.environ.get("NEXMIX_WIDE_LOG")
        if target:
            with open(target, "w", encoding="utf-8") as out:
                out.write("\n".join(self.log))

    def check(self) -> None:
        self.dump()
        info = (self.seed, self.log)
        keys_alive: dict[int, int] = {}
        for pid, data in self.lists.items():
            row = _row(pid)
            if pid in self.purged:
                # Gone, or the id given to a playlist made later (one taken in from Plex).
                assert row is None or row.created_at != self.purged[pid], ("purged but there", pid, info)
                continue
            assert row is not None, ("gone without purge", pid, info)
            here = local_tracks(pid)
            assert len(set(here)) == len(here), ("twice here", pid, here, info)
            for t, want in data["exp"].items():
                if want is True:
                    assert t in here, ("lost", pid, t, data["own"][t], here, info)
                elif want is False:
                    assert t not in here, ("came back", pid, t, data["own"][t], here, info)
            if row.deleted_at is not None:
                key = _key(pid)
                assert key is None or key not in self.world.lists.lists, ("copy in Plex of a trashed one", pid, info)
                continue
            key = _key(pid)
            assert key is not None and key in self.world.lists.lists, ("no copy in Plex", pid, key, info)
            there = self.world.lists.tracks(key)
            assert here == there, ("sides differ", pid, here, there, info)
            title = self.world.lists.lists[key].title
            assert row.name == title, ("names differ", pid, row.name, title, info)
            if data["nx_name"] and not data["px_name"] and not data["names_unsure"]:
                assert row.name == data["nx_name"], ("nexmix's name lost", pid, row.name, info)
            if data["px_name"] and not data["nx_name"] and not data["plex_gone"] and not data["names_unsure"]:
                assert row.name == data["px_name"], ("Plex's name lost", pid, row.name, info)
            keys_alive[key] = pid
        # Playlists made in Plex came over, each once.
        with SessionLocal() as db:
            mirrored = {key: pid for key, pid in db.execute(
                select(PlexPlaylist.rating_key, PlexPlaylist.playlist_id).where(PlexPlaylist.rating_key.is_not(None)))}
            alive_ids = set(db.scalars(select(Playlist.id).where(Playlist.deleted_at.is_(None))))
        for key in self.made:
            if key in self.world.lists.lists:
                assert key in mirrored, ("made in Plex, not here", key, info)
                pid = mirrored[key]
                assert pid in alive_ids, ("made in Plex, trashed here", key, info)
                assert local_tracks(pid) == self.world.lists.tracks(key), ("Plex's one differs", key, info)
        # Nothing in Plex without its playlist here, nothing twice.
        for key in self.world.lists.lists:
            assert key in mirrored, ("orphan in Plex", key, self.world.lists.lists[key].title, info)
            assert mirrored[key] in alive_ids, ("in Plex, trashed here", key, info)
        assert len(set(mirrored.values())) == len(mirrored), ("two copies of one", info)

    def order_check(self) -> None:
        self.dump()
        info = (self.seed, self.log)
        for pid in self.alive():
            here = local_tracks(pid)
            if len(here) < 3:
                continue
            # Sorted in nexmix only (with a fault on the way).
            target = list(here)
            self.rng.shuffle(target)
            for index, t in enumerate(target):
                after = _entry(pid, target[index - 1]) if index else None
                answer = self.client.post(f"/api/v1/playlists/{pid}/items/move",
                                          json={"entry": _entry(pid, t), "after": after})
                assert answer.status_code == 200, answer.text
                if self.rng.random() < 0.15:
                    self.fault()
            self.heal()
            key = _key(pid)
            assert key is not None
            assert local_tracks(pid) == target, ("nexmix's order not kept here", pid, target, info)
            assert self.world.lists.tracks(key) == target, ("nexmix's order not in Plex", pid, target, info)
            # Sorted in Plex only.
            target = list(target)
            self.rng.shuffle(target)
            for index, t in enumerate(target):
                self.world.lists.move_track(key, t, target[index - 1] if index else None)
            assert self.world.lists.tracks(key) == target
            self.heal()
            assert self.world.lists.tracks(key) == target, ("Plex's order changed in Plex", pid, info)
            assert local_tracks(pid) == target, ("Plex's order not here", pid, target, local_tracks(pid), info)


@pytest.mark.parametrize("seed", range(FIRST, FIRST + RUNS))
def test_wide_random_sync(client: TestClient, world: FakePlex, seed: int) -> None:
    run = Run(client, world, seed)
    for number in range(run.rng.randint(1, 3)):
        run.new_playlist(f"Liste {seed}-{number}")
    if run.rng.random() < 0.5:
        run.guest_join(next(iter(run.lists)))
    sync(world)
    for _step in range(run.rng.randint(15, 40)):
        run.step()
    run.heal()
    run.check()
    run.order_check()
    run.check()
