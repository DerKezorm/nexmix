"""The three-way merge with random changes on both sides and syncs at random times, ids as the sync gives them
(nexmix's entry ids, Plex's ``playlistItemID``, the base with both): nothing is lost that no side took out, what was
taken out stays out unless the other side put it in again, nothing twice, nothing from nowhere, the order of the side
that sorted alone stays, Plex is sorted by the moves, and a second sync changes nothing. ``NEXMIX_MERGE_RUNS`` sets how
many sequences (2,000 by default)."""

from __future__ import annotations

import os
import random
from collections import Counter

from app.services import merge

POOL = list(range(1, 31))


class World:
    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.next_local = 1
        self.next_item = 10_001
        start = rng.sample(POOL, rng.randint(0, 12))
        self.local = [(t, self.lid()) for t in start]   # (track, local id)
        self.remote = [(self.iid(), t) for t in start]  # (item id, track)
        self.base = [merge.Kept(t, lid, iid) for (t, lid), (iid, _t) in zip(self.local, self.remote, strict=True)]
        self.name_base = "A"
        self.name_local = "A"
        self.name_remote = "A"
        self.moved = {"local": False, "remote": False}
        self.ops = {"local": [], "remote": []}
        self.stats_sole = 0

    def lid(self) -> int:
        self.next_local += 1
        return self.next_local

    def iid(self) -> int:
        self.next_item += 1
        return self.next_item

    # --- operations
    def op(self, side: str) -> None:
        rng = self.rng
        kind = rng.choice(("add", "add", "remove", "move", "again", "rename"))
        tracks = [t for t, _ in self.local] if side == "local" else [t for _, t in self.remote]
        if kind == "add":
            new = [t for t in POOL if t not in tracks]
            if not new:
                return
            t = rng.choice(new)
            if side == "local":
                self.local.insert(rng.randint(0, len(self.local)), (t, self.lid()))
            else:
                # Plex: at the end (Plexamp may move it afterwards)
                self.remote.append((self.iid(), t))
        elif kind == "remove" and tracks:
            t = rng.choice(tracks)
            if side == "local":
                self.local = [e for e in self.local if e[0] != t]
            else:
                self.remote = [e for e in self.remote if e[1] != t]
        elif kind == "move" and len(tracks) > 1:
            lst = self.local if side == "local" else self.remote
            e = lst.pop(rng.randrange(len(lst)))
            lst.insert(rng.randint(0, len(lst)), e)
            self.moved[side] = True
        elif kind == "again" and tracks:
            t = rng.choice(tracks)
            if side == "local":
                self.local = [e for e in self.local if e[0] != t] + [(t, self.lid())]
            else:
                self.remote = [e for e in self.remote if e[1] != t] + [(self.iid(), t)]
        elif kind == "rename":
            n = rng.choice("BCDEFG")
            if side == "local":
                self.name_local = n
            else:
                self.name_remote = n
        else:
            return
        self.ops[side].append(kind)

    # --- one sync, as plex_sync does it
    def sync(self) -> None:
        before_local = list(self.local)
        before_remote = list(self.remote)
        base_ids = {k.track: k for k in self.base}
        m = merge.merge(self.base, self.local, [(t, i) for i, t in self.remote])
        order = m.order
        ctx = (self.base, before_local, before_remote, m)
        assert len(order) == len(set(order)), ("twice", ctx)
        result = set(order)
        local_ids = dict(before_local)
        remote_ids = {t: i for i, t in before_remote}
        # Nothing lost that no side took out.
        for t, lid in before_local:
            other_removed = t in base_ids and t not in remote_ids and base_ids[t].local == lid
            if not other_removed:
                assert t in result, ("lost (nexmix)", t, ctx)
        for iid, t in before_remote:
            other_removed = t in base_ids and t not in local_ids and base_ids[t].remote == iid
            if not other_removed:
                assert t in result, ("lost (Plex)", t, ctx)
        # Taken out stays out, unless the other side put it in again.
        for t, k in base_ids.items():
            if t not in local_ids and remote_ids.get(t) == k.remote:
                assert t not in result, ("back after taken out in nexmix", t, ctx)
            if t not in remote_ids and local_ids.get(t) == k.local:
                assert t not in result, ("back after taken out in Plex", t, ctx)
        # Nothing from nowhere.
        assert result <= set(local_ids) | set(remote_ids), ("from nowhere", ctx)
        # Sole sorter keeps its order: a side sorted when the entries it kept unchanged from the base (same id)
        # stand in another order than in the base.
        base_order = [k.track for k in self.base]

        def unchanged(side: str) -> list[int]:
            if side == "local":
                return [t for t, lid in before_local if t in base_ids and base_ids[t].local == lid]
            return [t for i, t in before_remote if t in base_ids and base_ids[t].remote == i]

        def sorted_(side: str) -> bool:
            same = unchanged(side)
            return [t for t in base_order if t in set(same)] != same

        eff = {"local": sorted_("local"), "remote": sorted_("remote")}
        for side in ("local", "remote"):
            other = "remote" if side == "local" else "local"
            if eff[side] and not eff[other]:
                kept = [t for t in unchanged(side) if t in result]
                assert [t for t in order if t in set(kept)] == kept, ("order of the one that sorted", side, ctx)
                self.stats_sole += 1
        # Neither sorted: nexmix's relative order of what it kept stays.
        if not eff["local"] and not eff["remote"]:
            kept = [t for t, _ in before_local if t in result]
            assert [t for t in order if t in set(kept)] == kept, ("order without sorting", ctx)
        # Apply to nexmix.
        new_local = []
        for t in order:
            new_local.append((t, local_ids[t] if t in local_ids else self.lid()))
        self.local = new_local
        # Apply to Plex: out, in at the end, moves on the state just read.
        self.remote = [(i, t) for i, t in self.remote if t in result]
        present = {t for _, t in self.remote}
        for t in order:
            if t not in present:
                self.remote.append((self.iid(), t))
        steps = merge.moves(self.remote, order)
        self.remote = merge.apply_moves(self.remote, steps)
        assert [t for _, t in self.remote] == order, ("Plex not sorted", ctx)
        # Name.
        name = merge.name(self.name_base, self.name_local, self.name_remote)
        if self.name_local != self.name_base and self.name_remote == self.name_base:
            assert name == self.name_local
        if self.name_remote != self.name_base and self.name_local == self.name_base:
            assert name == self.name_remote, ("name from Plex lost", self.name_remote, name)
        self.name_base = self.name_local = self.name_remote = name
        lmap = dict(self.local)
        self.base = [merge.Kept(t, lmap[t], i) for i, t in self.remote]
        self.moved = {"local": False, "remote": False}


def run(sequences: int, seed: int) -> Counter:
    stats: Counter = Counter()
    for n in range(sequences):
        rng = random.Random(seed * 1_000_003 + n)
        w = World(rng)
        for _step in range(rng.randint(5, 60)):
            r = rng.random()
            if r < 0.4:
                w.op("local")
            elif r < 0.8:
                w.op("remote")
            else:
                w.sync()
                stats["syncs"] += 1
        w.sync()
        # A second sync with no change changes nothing.
        before = (list(w.local), list(w.remote))
        w.sync()
        assert (w.local, w.remote) == before
        assert [t for t, _ in w.local] == [t for _, t in w.remote]
        stats["sequences"] += 1
        stats["sole_sorter_checks"] += w.stats_sole
    return stats



def test_random_sequences_on_both_sides() -> None:
    found = run(int(os.environ.get("NEXMIX_MERGE_RUNS", "2000")), 1)
    assert found["sequences"] and found["syncs"] and found["sole_sorter_checks"]
