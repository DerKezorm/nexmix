"""The catalogue of a Jellyfin server, against the stand-in: the full run in pages, what is kept of a track (the
artists, the compilation, the MusicBrainz ids, the play counts of the chosen user), the partial run after
``MinDateLastSaved`` on Jellyfin's clock, the hand-over to a full run when too much changed or something is gone, a
server that is starting, and the entries of a playlist whose track got a new id because its file was renamed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Account, CatalogAlbum, CatalogArtist, CatalogTag, CatalogTrack, Job, MediaServer, SyncRun
from app.services import catalog, jellyfin, jobs
from app.services.sources import jellyfin as jellyfin_source

from .fakejellyfin import FakeJellyfin, make_id
from .jellyfin_world import connect, key_of, number_of


def tracks(server: int) -> dict[str, CatalogTrack]:
    """The live catalogue's tracks by Jellyfin's id."""
    with SessionLocal() as db:
        row = db.get(MediaServer, server)
        assert row is not None
        found = list(db.scalars(select(CatalogTrack).where(CatalogTrack.server_id == server,
                                                           CatalogTrack.generation == row.generation)))
        for one in found:
            db.expunge(one)
    return {key_of(server, one.id): one for one in found}


def runs(server: int) -> list[SyncRun]:
    with SessionLocal() as db:
        found = list(db.scalars(select(SyncRun).where(SyncRun.server_id == server).order_by(SyncRun.id)))
        for one in found:
            db.expunge(one)
    return found


def by_name(fake: FakeJellyfin, name: str, album: str | None = None) -> str:
    return next(ident for ident, one in fake.tracks.items()
                if one.name == name and (album is None or fake.albums[one.album].name == album))


@pytest.fixture
def jelly(client: TestClient, operator: Account, fake_jellyfin: FakeJellyfin) -> tuple[FakeJellyfin, int]:
    return fake_jellyfin, connect(client, fake_jellyfin)


def partial(server: int) -> None:
    with SessionLocal() as db:
        assert catalog.request_partial(db, server_id=server) is True
    assert jobs.run_pending(limit=1, lane="catalog") == 1


def test_the_full_run_takes_in_the_whole_library(client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    found = tracks(server)
    assert set(found) == set(fake.tracks)
    status = client.get(f"/api/v1/catalog/status?server={server}").json()
    assert status["ready"] and status["counts"]["tracks"] == len(fake.tracks)
    assert status["counts"]["albums"] == len(fake.albums)
    assert status["counts"]["moods"] == 0 and status["analysis"]["state"] == "unknown"
    with SessionLocal() as db:
        names = set(db.scalars(select(CatalogArtist.title).where(CatalogArtist.server_id == server)))
        kinds = set(db.scalars(select(CatalogTag.kind).where(CatalogTag.server_id == server)))
    assert names == {"Venrarik", "Ystasul", "Various Artists", "Orvenna", "Brakkel", "Talmira", "Quenor"}
    assert kinds == {"genre"}
    echo = found[by_name(fake, "Echo Feuer", "Wolkenfabrik")]
    assert echo.duration == 95_000 and echo.year == 1986 and echo.number == 1 and echo.disc == 1
    assert echo.mbid_track and echo.mbid_recording and echo.mbid_track != echo.mbid_recording
    assert echo.guid == ""
    assert found[by_name(fake, "Leise Lichter")].mbid_track == ""
    # A compilation: the album's artist is "Various Artists", the track's own artist the one who performs it.
    assert found[by_name(fake, "Rote Antenne")].artist == "Orvenna"
    # "feat." stays as Jellyfin writes it, and the track names its own artist.
    assert found[by_name(fake, "Nachtzug (feat. Kafin)")].artist == "Venrarik feat. Kafin"
    assert found[by_name(fake, "Kupferstrand", "Wolkenfabrik")].artist == ""
    # Songs: the remaster and the live version are versions of the original, which is the one shown.
    original = found[by_name(fake, "Kupferstrand", "Wolkenfabrik")]
    remaster = found[by_name(fake, "Kupferstrand", "Wolkenfabrik (Remastered)")]
    assert original.song_key == remaster.song_key and original.shown and not remaster.shown
    assert remaster.original_year == 1986
    assert found[by_name(fake, "Echo Feuer (Live)")].variant
    # An album without a year, and one without a picture.
    assert found[by_name(fake, "Eisblume")].year is None
    with SessionLocal() as db:
        hafen = db.scalars(select(CatalogAlbum).where(CatalogAlbum.server_id == server,
                                                      CatalogAlbum.title == "Hafenlieder")).one()
        wolken = db.scalars(select(CatalogAlbum).where(CatalogAlbum.server_id == server,
                                                       CatalogAlbum.title == "Wolkenfabrik")).one()
    assert hafen.thumb == "" and wolken.thumb


def test_the_lists_are_asked_for_in_pages_without_the_path_of_a_file(client: TestClient, operator: Account,
                                                                     fake_jellyfin: FakeJellyfin,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jellyfin, "PAGE", 5)
    server = connect(client, fake_jellyfin)
    assert set(tracks(server)) == set(fake_jellyfin.tracks)
    pages = [params for _method, path, params in fake_jellyfin.calls if path == "/Items" and params.get("Limit") != "0"]
    audio = [params for params in pages if params["IncludeItemTypes"] == "Audio"]
    assert len(audio) == len(fake_jellyfin.tracks) // 5 + 1
    assert sorted(int(params["StartIndex"]) for params in audio) == list(range(0, len(fake_jellyfin.tracks) + 1, 5))
    for params in pages:
        assert "Path" not in params["Fields"] and params["EnableTotalRecordCount"] == "false"
        assert params["Recursive"] == "true" and params["ParentId"]
    # The play counts are the chosen user's.
    assert all(params.get("userId") and params["EnableUserData"] == "true" for params in audio)


def test_the_play_counts_of_the_chosen_user_say_how_well_known_a_track_is(client: TestClient, operator: Account,
                                                                         fake_jellyfin: FakeJellyfin) -> None:
    fake_jellyfin.tracks[by_name(fake_jellyfin, "Glasfluss", "Mondgarten")].plays = 12
    server = connect(client, fake_jellyfin)
    found = tracks(server)
    glass = found[by_name(fake_jellyfin, "Glasfluss", "Mondgarten")]
    assert glass.rating_count == 12 and glass.view_count == 12
    assert found[by_name(fake_jellyfin, "Sandkreise")].rating_count == 0


def test_a_partial_run_takes_in_what_jellyfin_saved_since_the_last_one_on_its_clock(
        client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    began = fake.now
    fake.tick(900)
    fake.touch(by_name(fake, "Sandkreise"), name="Sandkreise (Single)")
    new = fake.add(make_id("album", "Ystasul", "Mondgarten"), "Polarnacht", 222)
    # Saved a little before the last run began, too late to be fetched by it: the minute looked back finds it.
    late = by_name(fake, "Möwenruf")
    fake.tracks[late].name = "Möwenruf (Demo)"
    fake.tracks[late].saved = began - timedelta(seconds=30)
    fake.calls.clear()
    partial(server)
    found = tracks(server)
    assert found[by_name(fake, "Sandkreise (Single)")].title == "Sandkreise (Single)"
    assert found[new].title == "Polarnacht" and found[new].year == 2003 and found[new].shown
    assert found[late].title == "Möwenruf (Demo)"
    last = runs(server)[-1]
    assert last.kind == "partial" and last.state == "done" and last.changed >= 3
    asked = {params.get("MinDateLastSaved") for _method, path, params in fake.calls if path == "/Items"}
    since = (began - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S")
    assert {value[:19] for value in asked if value} == {since}
    # The next run looks from where this one began, on Jellyfin's clock.
    with SessionLocal() as db:
        row = db.get(MediaServer, server)
        assert row is not None and row.marks["clock"] == int((began + timedelta(seconds=900)).timestamp())


def test_more_than_the_limit_at_once_hands_over_to_a_full_run(client: TestClient, jelly: tuple[FakeJellyfin, int],
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    fake, server = jelly
    monkeypatch.setattr(jellyfin_source, "PARTIAL_LIMIT", 2)
    fake.tick(900)
    for ident in list(fake.tracks)[:3]:
        fake.touch(ident, plays=1)
    fake.calls.clear()
    partial(server)
    pages = [params for _method, path, params in fake.calls if path == "/Items" and params.get("Limit") != "0"]
    assert pages == [], "the counts said too many: no page fetched"
    with SessionLocal() as db:
        waiting = db.scalars(select(Job).where(Job.kind == "catalog_full", Job.state == "queued")).one()
        assert waiting.payload["reason"] == "many"
    assert jobs.run_pending(lane="catalog") == 1
    assert runs(server)[-1].kind == "full" and runs(server)[-1].state == "done"


def test_a_track_gone_from_jellyfin_goes_with_a_full_run(client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    gone = by_name(fake, "Teerseile")
    fake.tick(900)
    fake.delete(gone)
    partial(server)
    # The total differed and Jellyfin has no light list of ids: the whole catalogue again.
    assert jobs.run_pending(lane="catalog") == 1
    assert gone not in tracks(server)
    assert [one.kind for one in runs(server)][-2:] == ["partial", "full"]


def test_a_server_that_is_starting_is_waited_for(client: TestClient, operator: Account,
                                                 fake_jellyfin: FakeJellyfin) -> None:
    server = connect(client, fake_jellyfin, run=False)
    fake_jellyfin.starting = 3
    assert jobs.run_pending(lane="catalog") == 1
    assert fake_jellyfin.waits == [2, 5, 10]  # type: ignore[attr-defined]
    assert runs(server)[-1].state == "done"
    assert set(tracks(server)) == set(fake_jellyfin.tracks)


def test_a_server_that_does_not_come_up_stops_the_run_without_a_fault(client: TestClient, operator: Account,
                                                                      fake_jellyfin: FakeJellyfin) -> None:
    server = connect(client, fake_jellyfin, run=False)
    fake_jellyfin.starting = 1000
    assert jobs.run_pending(lane="catalog") == 1
    assert fake_jellyfin.waits == list(jellyfin.WAITS)  # type: ignore[attr-defined]
    last = runs(server)[-1]
    assert last.state == "stopped" and last.error == "jellyfin_starting"
    with SessionLocal() as db:
        assert db.scalars(select(Job).where(Job.kind == "catalog_full", Job.state == "failed")).all() == []
    status = client.get(f"/api/v1/catalog/status?server={server}").json()
    assert status["last"]["state"] == "stopped" and status["ready"] is False
    # A quarter of an hour later the clock asks again; by then Jellyfin is up.
    fake_jellyfin.starting = 0
    with SessionLocal() as db:
        assert catalog.tick(db, datetime.now(UTC) + timedelta(minutes=16)) == "full"
    assert jobs.run_pending(lane="catalog") == 1
    assert client.get(f"/api/v1/catalog/status?server={server}").json()["ready"] is True


def test_a_server_that_goes_away_in_the_middle_fails_the_run_and_keeps_the_catalogue(
        client: TestClient, jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    fake.down.add("jellyfin.example.com")
    assert client.post(f"/api/v1/catalog/sync?server={server}").status_code == 202
    assert jobs.run_pending(lane="catalog") == 1
    last = runs(server)[-1]
    assert last.state == "failed" and last.error == "jellyfin_unreachable"
    assert set(tracks(server)) == set(fake.tracks)


# --- The id of a track changes with its file ------------------------------------------------------------------------


def _entries(client: TestClient, playlist: int) -> list[dict[str, object]]:
    return client.get(f"/api/v1/playlists/{playlist}").json()["items"]


def test_an_entry_whose_file_was_renamed_finds_its_track_again(client: TestClient,
                                                               jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    names = ["Glasfluss", "Funkenregen", "Ampelglühen", "Möwenruf"]
    chosen = [number_of(server, by_name(fake, name, None if name != "Glasfluss" else "Mondgarten")) for name in names]
    made = client.post("/api/v1/playlists", json={"name": "Umzug", "server": server}).json()
    assert client.post(f"/api/v1/playlists/{made['id']}/items", json={"tracks": chosen}).status_code == 200
    before = _entries(client, made["id"])
    # Two files renamed on the disk (one with MusicBrainz ids, one without), one deleted.
    renamed = fake.rename(by_name(fake, "Funkenregen"), "03 Funkenregen (neu)")
    fake.rename(by_name(fake, "Ampelglühen"), "Ampelglühen")
    fake.delete(by_name(fake, "Möwenruf"))
    fake.tick(900)
    partial(server)
    assert jobs.run_pending(lane="catalog") == 1, "the totals differ: a full run, then the entries are found"
    after = _entries(client, made["id"])
    assert [one["id"] for one in after] == [one["id"] for one in before], "same entries, same order"
    assert [one["title"] for one in after] == ["Glasfluss", "Funkenregen", "Ampelglühen", "Möwenruf"]
    assert after[1]["track"] == number_of(server, renamed) and after[1]["missing"] is False
    assert after[2]["missing"] is False and after[2]["track"] != before[2]["track"]
    assert after[0]["track"] == before[0]["track"]
    # Found nowhere: it stays, with what nexmix knew of it, and says it is missing from the library.
    assert after[3]["missing"] is True and after[3]["track"] == before[3]["track"]
    assert [one["added_by_name"] for one in after] == [one["added_by_name"] for one in before]


def test_a_renamed_file_is_never_taken_for_another_version_of_the_song(client: TestClient,
                                                                       jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    original = by_name(fake, "Kupferstrand", "Wolkenfabrik")
    made = client.post("/api/v1/playlists", json={"name": "Nur das Original", "server": server}).json()
    client.post(f"/api/v1/playlists/{made['id']}/items", json={"tracks": [number_of(server, original)]})
    # The original goes; only the remaster is left: another version, not the same track.
    fake.delete(original)
    fake.tick(900)
    partial(server)
    jobs.run_pending(lane="catalog")
    entry = _entries(client, made["id"])[0]
    assert entry["missing"] is True


def test_the_clock_waits_for_the_user_before_the_first_run(client: TestClient, operator: Account,
                                                           fake_jellyfin: FakeJellyfin) -> None:
    from .jellyfin_world import quick_connect

    found = quick_connect(client, fake_jellyfin)
    server = int(found["server"]["id"])
    assert found["server"]["library"] is not None and found["server"]["user"] is None
    with SessionLocal() as db:
        assert catalog.tick(db, datetime.now(UTC) + timedelta(minutes=30)) is None
    assert jobs.run_pending(lane="catalog") == 0
    handle = next(one["handle"] for one in found["users"] if one["name"] == "Erika")
    assert client.put(f"/api/v1/jellyfin/{server}/user", json={"handle": handle}).status_code == 200
    assert jobs.run_pending(lane="catalog") == 1
    assert runs(server)[-1].state == "done"


def test_a_clock_of_jellyfin_gone_back_is_followed_never_a_time_to_come(client: TestClient,
                                                                       jelly: tuple[FakeJellyfin, int]) -> None:
    fake, server = jelly
    fake.tick(-3600)
    fake.touch(by_name(fake, "Abfahrt"), name="Abfahrt (Edit)")
    partial(server)
    assert tracks(server)[by_name(fake, "Abfahrt (Edit)")].title == "Abfahrt (Edit)"
    asked = [params["MinDateLastSaved"] for _method, path, params in fake.calls
             if path == "/Items" and params.get("MinDateLastSaved")]
    assert asked[-1][:19] == (fake.now - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%S")
