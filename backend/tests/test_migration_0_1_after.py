"""After the step from 0.1.0: what changed in Plex meanwhile is read and nothing is written back, the jobs 0.1.0 left
waiting or running go on as jobs of server 1, and numbers given from many threads at once for two servers never meet.
The database is the one of ``test_migration_0_1`` (made with the code of 0.1.0, every name made up)."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from sqlalchemy import select

from app import db as database
from app.config import get_settings
from app.db import SessionLocal
from app.models import MediaServer, ServerItem
from app.services import jobs, media
from app.services import sync as server_sync

from .conftest import new_client
from .fakeplex import FakePlex
from .plex_world import sync
from .test_migration_0_1 import _old_database, _operator, _place, _stand_in


def _migrated(fake: FakePlex, tmp_path: Path, change: object = None) -> None:
    _stand_in(fake)
    old = tmp_path / "nexmix-0.1.0.db"
    _old_database(old, fake.account_token)
    if callable(change):
        with closing(sqlite3.connect(old)) as connection:
            change(connection)
            connection.commit()
    _place(old)
    database.init_db()
    server_sync.forget()


def _quiet(fake: FakePlex, rounds: int = 4) -> None:
    for _round in range(rounds):
        with SessionLocal() as db:
            server_sync.request_all(db)
        fake.tick(30)
        sync(fake, look_at_plex=True)


def _key(fake: FakePlex, title: str) -> int:
    return next(key for key, one in fake.lists.lists.items() if one.title == title)


def test_changes_made_in_plex_meanwhile_are_read_and_nothing_is_written(fake_plex: FakePlex, tmp_path: Path) -> None:
    _migrated(fake_plex, tmp_path)
    # In Plex after the update, before the first sync: added, taken out, moved, renamed, a new one.
    fake_plex.lists.add(_key(fake_plex, "Sommerabend"), [8002])
    fake_plex.lists.remove_track(_key(fake_plex, "Aus Plexamp"), 8026)
    fake_plex.lists.move_track(_key(fake_plex, "Regentage"), 8001, None)
    fake_plex.lists.rename(_key(fake_plex, "Unterwegs im Zug"), "Im Zug")
    fake_plex.lists.make("Neu in Plex", [8004, 8005])
    fake_plex.tick()
    _quiet(fake_plex)
    assert fake_plex.lists.writes == []
    with new_client(_operator()) as client:
        cards = {card["name"]: card["id"] for card in client.get("/api/v1/playlists").json()["playlists"]}

        def tracks(name: str) -> list[int]:
            items = client.get(f"/api/v1/playlists/{cards[name]}").json()["items"]
            return [entry["track"] for entry in items if not entry["plex_hint"]]

        assert {"Im Zug", "Neu in Plex"} <= set(cards) and "Unterwegs im Zug" not in cards
        assert 8002 in tracks("Sommerabend")
        assert 8026 not in tracks("Aus Plexamp")
        assert tracks("Regentage") == fake_plex.lists.tracks(_key(fake_plex, "Regentage")) == [8001, 8007]
        assert tracks("Neu in Plex") == [8004, 8005]


def test_jobs_left_waiting_or_running_go_on_for_server_1(fake_plex: FakePlex, tmp_path: Path) -> None:
    left = (("catalog_part", 0, "queued"), ("catalog_full", 0, "running"), ("plex_round", 1, "running"),
            ("plex_round", 4, "queued"), ("plex_poll", 0, "queued"))

    def waiting(connection: sqlite3.Connection) -> None:
        for kind, ref, state in left:
            connection.execute("INSERT INTO jobs (kind, ref, payload, state, attempts, created_at) VALUES "
                               "(?, ?, NULL, ?, 0, '2026-11-15 10:00:00.000000')", (kind, ref, state))

    _migrated(fake_plex, tmp_path, waiting)
    with closing(sqlite3.connect(get_settings().database_path)) as connection:
        open_jobs = connection.execute("SELECT kind, ref, state FROM jobs WHERE state IN ('queued', 'running') "
                                       "ORDER BY id").fetchall()
    assert open_jobs == [("catalog_part", 1, "queued"), ("catalog_full", 1, "running"), ("server_round", 1, "running"),
                         ("server_round", 4, "queued"), ("server_poll", 1, "queued")]
    with SessionLocal() as db:
        for lane in ("catalog", "playlists"):
            jobs.recover(db, lane)
    jobs.run_pending(lane="catalog")
    _quiet(fake_plex, 3)
    assert fake_plex.lists.writes == []
    with closing(sqlite3.connect(get_settings().database_path)) as connection:
        assert connection.execute("SELECT count(*) FROM jobs WHERE state IN ('queued', 'running')").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM jobs WHERE state = 'failed'").fetchone()[0] == 0


def test_numbers_given_at_once_for_two_servers_never_meet() -> None:
    with SessionLocal() as db:
        for ident in (1, 2):
            db.add(MediaServer(id=ident, kind="plex", active=True, machine_id=f"m{ident}", marks={}))
        db.commit()
    results: list[tuple[int, dict[str, int]]] = []
    errors: list[BaseException] = []
    keys = [str(key) for key in range(5000, 5400)]
    barrier = threading.Barrier(8)

    def worker(server: int) -> None:
        try:
            barrier.wait()
            with SessionLocal() as db:
                got = media.numbers(db, server, media.TRACK, keys)
                db.commit()
            results.append((server, got))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(1 + number % 2,)) for number in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    by_server: dict[int, dict[str, int]] = {}
    for server, got in results:
        assert by_server.setdefault(server, got) == got, "the same key, the same number, whoever asked"
    assert not set(by_server[1].values()) & set(by_server[2].values())
    with SessionLocal() as db:
        ids = [row.id for row in db.scalars(select(ServerItem))]
    assert len(ids) == len(set(ids)) == 800
