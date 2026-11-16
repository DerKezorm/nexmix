"""Random runs as in ``test_mirrors_random.py`` (source on the stand-in Plex, a mirror on the stand-in Jellyfin, changes
in nexmix, in Plex and in Jellyfin, faults of Jellyfin, nexmix started anew), and on top of them:

* the playlist **moved** to the other server and back, at random times (the copy before stays a mirror);
* tracks of **Jellyfin's own** (Plex has nothing like them) put into the Jellyfin copy: while Jellyfin is a mirror they
  are shown "only in Jellyfin", while it is the source they become entries;
* an entry without a track on the Jellyfin mirror **matched by hand** to one of Jellyfin's own tracks, one shown "only
  there" or one in no list yet.

A track stands for a song by a label: Plex's key, the same for its twin in Jellyfin; one of Jellyfin's own by its id;
an entry and the track chosen for it by hand share one label. Every label is added or taken out at most once, so the
end is known: what is there at the start and untouched, and what was added, is an entry or shown "only there"; what was
taken out is neither; no entry twice; no track twice in any copy; every copy holds exactly the tracks that stand for the
entries there and the ones shown "only there", in nexmix's order; no track stands for an entry and is shown "only
there" at once.

The copy that is a mirror never loses its last track there: a mirror emptied there waits for a person
(``test_mirrors_decisions.py``).

``NEXMIX_MIRROR_ROLE_RUNS`` sets how many runs (30 by default), ``NEXMIX_MIRROR_SEED`` where the seeds start."""

from __future__ import annotations

import os
import random
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Account, MirrorExtra, MirrorItem, Playlist, ServerPlaylist
from app.services import jobs, media
from app.services import playlists as playlist_service
from app.services import sync as server_sync

from .fakejellyfin import FakeJellyfin
from .fakeplex import FakePlex
from .fakeplex.library import TRACK
from .mirror_world import connect_both, copy_row, jelly_numbers, plex_numbers
from .plex_world import sync
from .test_mirrors_random import _fault

RUNS = int(os.environ.get("NEXMIX_MIRROR_ROLE_RUNS", "30"))
FIRST = int(os.environ.get("NEXMIX_MIRROR_SEED", "0"))
LEFT_OUT = frozenset({3021, 3030})
#: Jellyfin's own tracks of a title nothing else has.
OWN = ("Leise Lichter", "Sandkreise", "Funkenregen", "Möwenruf", "Teerseile", "Betonblumen", "Gleis Neun", "Abfahrt",
       "Eisblume")


@pytest.mark.parametrize("seed", range(FIRST, FIRST + RUNS))
def test_random_moves_tracks_only_there_and_choices_by_hand(
        client: TestClient, operator: Account, fake_plex: FakePlex, fake_jellyfin: FakeJellyfin, seed: int) -> None:
    fake = fake_jellyfin
    _plex, jelly, twins = connect_both(client, fake_plex, fake, LEFT_OUT)
    server_sync.forget()
    rng = random.Random(7919 * seed + 13)
    back = {ident: key for key, ident in twins.items()}
    own = {name: next(ident for ident, one in fake.tracks.items() if one.name == name) for name in OWN}
    pool = sorted(key for key, item in fake_plex.items.items() if item.kind == TRACK)
    start = rng.sample(pool, rng.randint(0, 6))
    if rng.random() < 0.6 and 3021 not in start:
        # A track Jellyfin lacks, for an entry to be matched there by hand.
        start.append(3021)
    made = client.post("/api/v1/playlists", json={"name": f"Rollen {seed}"}).json()
    playlist_id = int(made["id"])
    if start:
        assert client.post(f"/api/v1/playlists/{playlist_id}/items", json={"tracks": plex_numbers(start)}).status_code \
            == 200
    sync(fake_plex)
    assert client.post(f"/api/v1/playlists/{playlist_id}/mirrors", json={"server": jelly}).status_code == 201
    sync(fake_plex)
    alias: dict[Any, Any] = {}
    touched: dict[Any, str] = {}
    log: list[str] = []

    def canon(label: Any) -> Any:
        while label in alias:
            label = alias[label]
        return label

    def may_add(label: Any) -> bool:
        return touched.get(canon(label), "add") == "add"

    def may_remove(label: Any) -> bool:
        return touched.get(canon(label), "remove") == "remove"

    def touch(label: Any, what: str) -> None:
        touched[canon(label)] = what

    def of_jelly(ident: str) -> Any:
        return back.get(ident, ("J", ident))

    def source() -> int:
        with SessionLocal() as db:
            return int(db.scalar(select(Playlist.source_server_id).where(Playlist.id == playlist_id)) or 0)

    def key_on(server: int) -> str | None:
        return copy_row(playlist_id, server).ext_key

    def on_plex() -> list[int]:
        key = key_on(1)
        return fake_plex.lists.tracks(int(key)) if key and int(key) in fake_plex.lists.lists else []

    def on_jelly() -> list[str]:
        key = key_on(jelly)
        return list(fake.tracks_of(key)) if key and key in fake.lists else []

    def labels_of(numbers: list[int]) -> list[Any]:
        with SessionLocal() as db:
            plex_keys = media.keys(db, 1, media.TRACK, numbers)
            jelly_keys = media.keys(db, jelly, media.TRACK, numbers)
        return [int(plex_keys[one]) if one in plex_keys else of_jelly(jelly_keys[one]) for one in numbers]

    def entries() -> list[Any]:
        with SessionLocal() as db:
            items = playlist_service.live_items(db, playlist_id)
            numbers = [item.track_id for item in items]
        return [canon(label) for label in labels_of(numbers)]

    def extra_labels() -> set[Any]:
        with SessionLocal() as db:
            numbers = list(db.scalars(select(MirrorExtra.track_id).join(
                ServerPlaylist, ServerPlaylist.id == MirrorExtra.copy_id).where(
                ServerPlaylist.playlist_id == playlist_id, MirrorExtra.discard.is_(False))))
        return {canon(label) for label in labels_of([int(one) for one in numbers])}

    def never_both() -> None:
        with SessionLocal() as db:
            doubled = db.execute(select(MirrorExtra.track_id).join(
                MirrorItem, (MirrorItem.copy_id == MirrorExtra.copy_id)
                & (MirrorItem.track_id == MirrorExtra.track_id))).all()
        assert doubled == [], ("a track for an entry and only there at once", seed, log)

    for _step in range(rng.randint(10, 24)):
        roll = rng.random()
        here = entries()
        if roll < 0.10:
            # By the tracks of the playlist's source (Plex's, or their twins in Jellyfin).
            on_plex_now = source() == 1
            new = [key for key in pool if canon(key) not in here and may_add(key) and (on_plex_now or key in twins)]
            if new:
                chosen = rng.sample(new, min(len(new), rng.randint(1, 2)))
                for key in chosen:
                    touch(key, "add")
                numbers = plex_numbers(chosen) if on_plex_now else jelly_numbers(jelly, [twins[key] for key in chosen])
                answer = client.post(f"/api/v1/playlists/{playlist_id}/items", json={"tracks": numbers})
                assert answer.status_code == 200 and not answer.json().get("skipped"), (answer.text, log)
                log.append(f"nx add {chosen}")
        elif roll < 0.16:
            with SessionLocal() as db:
                items = playlist_service.live_items(db, playlist_id)
                pairs = list(zip(items, labels_of([item.track_id for item in items]), strict=True))
            old = [(item.id, label) for item, label in pairs if may_remove(label)]
            if old:
                entry, label = rng.choice(old)
                touch(label, "remove")
                assert client.post(f"/api/v1/playlists/{playlist_id}/items/remove",
                                   json={"entries": [entry]}).status_code == 200
                log.append(f"nx remove {label}")
        elif roll < 0.24:
            there = on_plex()
            new = [key for key in pool if key not in there and may_add(key)]
            if new and key_on(1):
                chosen = rng.sample(new, 1)
                touch(chosen[0], "add")
                fake_plex.lists.add(int(key_on(1) or 0), chosen)
                log.append(f"plex add {chosen}")
        elif roll < 0.29:
            old = [key for key in on_plex() if may_remove(key)]
            # Never the last one of a mirror: emptied there, it waits for a person (``test_mirrors_decisions.py``).
            if old and (source() == 1 or len(on_plex()) > 1):
                key = rng.choice(old)
                touch(key, "remove")
                fake_plex.lists.remove_track(int(key_on(1) or 0), key)
                log.append(f"plex remove {key}")
        elif roll < 0.37:
            there = on_jelly()
            twin_new = [twins[key] for key in pool if key in twins and twins[key] not in there and may_add(key)]
            own_new = [ident for ident in own.values() if ident not in there and may_add(("J", ident))]
            new = twin_new + own_new
            if new and key_on(jelly) in fake.lists:
                ident = rng.choice(new)
                touch(of_jelly(ident), "add")
                fake.list_add(key_on(jelly) or "", [ident])
                log.append(f"jf add {of_jelly(ident)}")
        elif roll < 0.42:
            old = [ident for ident in on_jelly() if may_remove(of_jelly(ident))]
            if old and key_on(jelly) in fake.lists and (source() == jelly or len(on_jelly()) > 1):
                ident = rng.choice(old)
                touch(of_jelly(ident), "remove")
                fake.list_remove(key_on(jelly) or "", [ident])
                log.append(f"jf remove {of_jelly(ident)}")
        elif roll < 0.46:
            there = on_jelly()
            if len(there) > 1 and key_on(jelly) in fake.lists:
                ident = rng.choice(there)
                fake.list_move(key_on(jelly) or "", ident, rng.randrange(len(there)))
                log.append(f"jf move {of_jelly(ident)}")
        elif roll < 0.51:
            target = jelly if source() == 1 else 1
            answer = client.post(f"/api/v1/playlists/{playlist_id}/move", json={"server": target})
            assert answer.status_code == 200, (answer.text, seed, log)
            log.append(f"move to {target}")
        elif roll < 0.62 and source() == 1 and copy_row(playlist_id, jelly).mirror:
            view = client.get(f"/api/v1/playlists/{playlist_id}").json()
            lacking = [(item["id"], item["track"]) for item in view["items"] if jelly in (item["absent"] or [])]
            shown = {int(one["id"]): one for one in (view["mirrors"][0]["extras"] if view["mirrors"] else [])}
            with SessionLocal() as db:
                row = copy_row(playlist_id, jelly)
                extra_tracks = {int(one.track_id) for one in db.scalars(select(MirrorExtra).where(
                    MirrorExtra.copy_id == row.id, MirrorExtra.discard.is_(False), MirrorExtra.id.in_(shown)))}
            everywhere = set(on_jelly())
            free = [ident for ident in own.values()
                    if ("J", ident) not in touched and canon(("J", ident)) == ("J", ident) and ident not in everywhere]
            choices = [jelly_numbers(jelly, [ident])[0] for ident in free] + sorted(extra_tracks)
            if lacking and choices:
                entry, track = rng.choice(lacking)
                number = rng.choice(choices)
                answer = client.put(f"/api/v1/playlists/{playlist_id}/mirrors/{jelly}/entries/{entry}",
                                    json={"track": number})
                assert answer.status_code in (200, 409), (answer.text, seed, log)
                if answer.status_code == 200:
                    mine, theirs = canon(labels_of([int(track)])[0]), canon(labels_of([number])[0])
                    state = "add" if "add" in (touched.get(mine), touched.get(theirs)) else touched.get(mine)
                    if mine != theirs:
                        alias[theirs] = mine
                        touched.pop(theirs, None)
                    if state is not None:
                        touched[mine] = state
                    log.append(f"choose {mine} = {theirs}")
        elif roll < 0.86:
            log.append("sync")
            sync(fake_plex, look_at_plex=rng.random() < 0.7)
        else:
            log.append("fault " + _fault(fake, fake_plex, rng))
        never_both()
    fake.fail_writes, fake.starting, fake.hook = 0, 0, None
    fake.down.clear()
    jobs.stopping.clear()
    for _round in range(6):
        with SessionLocal() as db:
            server_sync.request_all(db)
        sync(fake_plex, look_at_plex=True)
    never_both()
    here = entries()
    info = f"seed={seed} start={start} here={here} log={' | '.join(log)}"
    print("RUN", info)
    assert len(here) == len(set(here)), ("an entry twice", info)
    only = extra_labels()
    kept = set(here) | only
    for key in start:
        if canon(key) not in touched:
            assert canon(key) in kept, ("lost (untouched)", key, info)
    for label, what in touched.items():
        if what == "add":
            assert label in kept, ("lost (added)", label, info)
        else:
            assert label not in kept, ("came back (taken out)", label, info)
    # Every copy: exactly what stands for the entries there (in nexmix's order) and what is shown only there.
    with SessionLocal() as db:
        rows = {row.server_id: row for row in db.scalars(select(ServerPlaylist).where(
            ServerPlaylist.playlist_id == playlist_id))}
        items = playlist_service.live_items(db, playlist_id)
        playlist = db.get(Playlist, playlist_id)
        assert playlist is not None
        for server, raw in ((1, [str(one) for one in on_plex()]), (jelly, on_jelly())):
            assert len(raw) == len(set(raw)), ("twice in a copy", server, info)
            row = rows[server]
            if playlist.source_server_id == server:
                numbers = [item.track_id for item in items if item.plex_hint != server_sync.HINT_ELSEWHERE]
                extra_numbers: list[int] = []
            else:
                assert row.mirror, ("the copy before is no mirror", server, info)
                stands = {int(one.item_id): one.track_id for one in db.scalars(select(MirrorItem).where(
                    MirrorItem.copy_id == row.id))}
                numbers = [int(stands[item.id]) for item in items if stands.get(item.id) is not None]
                extra_numbers = [int(one) for one in db.scalars(select(MirrorExtra.track_id).where(
                    MirrorExtra.copy_id == row.id))]
            keys = media.keys(db, server, media.TRACK, numbers + extra_numbers)
            wanted = [str(keys[one]) for one in numbers]
            assert [one for one in raw if one in set(wanted)] == wanted, ("order or entries differ", server, info)
            assert set(raw) == set(wanted) | {str(keys[one]) for one in extra_numbers}, ("copy differs", server, info)
            assert row.state == "synced" and row.writing is None, ("state", server, row.state, row.error, info)
