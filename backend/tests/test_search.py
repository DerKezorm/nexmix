"""The search: the compact catalogue for the browser (ETag, 304, packed, only for those signed in, one state of the
catalogue at a time), and the server's search by the same rules as the browser's.

``tests/fixtures/search/`` holds the compact catalogue of the made-up library (``made_up_catalog.py``) and a list of
queries with what they must find. The server is held to it here, the browser's search
(``frontend/src/lib/catalogSearch.test.ts``) to the same file: both ways find the same in the same order. When the rules
change on purpose, both sides change, and the file is made anew:

    NEXMIX_RECORD_SEARCH=1 python -m pytest tests/test_search.py
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Account, CatalogTrack, MediaServer
from app.services import catalog_view, search

from . import made_up_catalog
from .conftest import new_client, person

FIXTURES = Path(__file__).parent / "fixtures" / "search"

#: The queries both ways must agree on: words, short words, words in another order, accents, filters, albums, artists.
CASES: list[dict[str, Any]] = [
    {"q": ""},
    {"q": "northern lights"},
    {"q": "lights northern"},
    {"q": "NORTHERN"},
    {"q": "live"},
    {"q": "northern live"},
    {"q": "glasshouse"},
    {"q": "night drive"},
    {"q": "drive"},
    {"q": "strasse"},
    {"q": "Straße der Träume"},
    {"q": "traume"},
    {"q": "cafe"},
    {"q": "Café"},
    {"q": "zoe"},
    {"q": "orlo"},
    {"q": "a"},
    {"q": "ol"},
    {"q": "lluvia"},
    {"q": "senorita"},
    {"q": "intro"},
    {"q": "velvet harbour"},
    {"q": "glow"},
    {"q": "", "genre": "Electronic"},
    {"q": "nothing like this"},
    {"q": "", "mood": "g:calm"},
    {"q": "", "mood": "g:party"},
    {"q": "", "mood": "Romantic"},
    {"q": "", "mood": "Energetic"},
    {"q": "", "mood": "No Such Mood"},
    {"q": "", "mood": "g:nothing"},
    {"q": "", "decade": 1980},
    {"q": "", "decade": 1990},
    {"q": "", "decade": 2020},
    {"q": "", "genre": "Rock"},
    {"q": "", "genre": "Chanson"},
    {"q": "", "genre": "No Such Genre"},
    {"q": "lights", "decade": 1980},
    {"q": "", "mood": "g:feeling", "decade": 1970, "genre": "Chanson"},
    {"q": "dr", "mood": "Energetic"},
    {"q": "", "kind": "albums"},
    {"q": "lights", "kind": "albums"},
    {"q": "z", "kind": "albums"},
    {"q": "harbour", "kind": "albums"},
    {"q": "", "kind": "artists"},
    {"q": "the", "kind": "artists"},
    {"q": "br", "kind": "artists"},
    {"q": "olberg", "kind": "artists"},
]


def answer_of(found: dict[str, Any], kind: str) -> dict[str, Any]:
    """What a case is held to: the count and the items in order (the whole of each)."""
    return {"total": found["total"], "items": found[kind]}


def server_search(client: TestClient, case: dict[str, Any]) -> dict[str, Any]:
    kind = case.get("kind", "tracks")
    params = {key: value for key, value in case.items() if key != "kind"}
    answer = client.get("/api/v1/catalog/search", params={**params, "kind": kind})
    assert answer.status_code == 200, answer.text
    return answer_of(answer.json(), kind)


def local_search(ix: search.Index, case: dict[str, Any]) -> dict[str, Any]:
    kind = case.get("kind", "tracks")
    if kind == "albums":
        return answer_of(search.search_albums(ix, case["q"]), kind)
    if kind == "artists":
        return answer_of(search.search_artists(ix, case["q"]), kind)
    filters = search.Filters(mood=case.get("mood", ""), decade=case.get("decade", 0), genre=case.get("genre", ""))
    return answer_of(search.search_tracks(ix, case["q"], filters), kind)


@pytest.fixture
def catalog_in() -> None:
    made_up_catalog.install()


@pytest.fixture
def reader(catalog_in: None) -> Any:
    with person("mira") as browser:
        yield browser


# --- The compact catalogue -------------------------------------------------------------------------------------------


def test_the_catalogue_is_there_only_for_those_signed_in(catalog_in: None) -> None:
    with new_client() as stranger:
        assert stranger.get("/api/v1/catalog").status_code == 401
        assert stranger.get("/api/v1/catalog/search", params={"q": "lights"}).status_code == 401
        assert stranger.get("/api/v1/catalog/albums/201").status_code == 401


def test_before_the_first_run_there_is_no_catalogue(client: TestClient, operator: Account) -> None:
    answer = client.get("/api/v1/catalog")
    assert answer.status_code == 409 and answer.json()["detail"]["code"] == "no_catalog"
    assert client.get("/api/v1/catalog/search").status_code == 409


def test_a_member_gets_it_packed_with_a_name_and_then_304(reader: TestClient) -> None:
    answer = reader.get("/api/v1/catalog", headers={"Accept-Encoding": "gzip"})
    assert answer.status_code == 200
    assert answer.headers["content-encoding"] == "gzip"
    assert answer.headers["cache-control"] == "private, no-cache"
    tag = answer.headers["etag"]
    assert tag.startswith('"c1-')
    data = answer.json()
    assert len(data["tracks"]) == len(made_up_catalog.TRACKS)
    again = reader.get("/api/v1/catalog", headers={"If-None-Match": tag})
    assert again.status_code == 304 and again.content == b""
    plain = reader.get("/api/v1/catalog", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers and plain.json() == data
    assert reader.get("/api/v1/catalog", headers={"If-None-Match": '"c1-other"'}).status_code == 200


def test_the_packed_form_is_the_plain_one_packed(catalog_in: None) -> None:
    view = catalog_view.current()
    assert view is not None
    assert gzip.decompress(view.packed) == view.body
    assert len(view.packed) < len(view.body)


def test_the_compact_form_names_song_shown_version_and_original_year(reader: TestClient) -> None:
    data = reader.get("/api/v1/catalog").json()
    tracks = {row[0]: row for row in data["tracks"]}
    albums = data["albums"]
    # Northern Lights by The Velvet Harbour: five versions, the one of 1984 shown (not the karaoke one with more
    # listeners, not the best-of), and 1984 the original year of all.
    harbour = [301, 305, 308, 310]
    assert {tracks[track][10] for track in harbour} == {301}
    assert all(tracks[track][11] == 1984 for track in harbour)
    # Dax Orlo's Northern Lights is another song.
    assert tracks[320][10] == 320
    # Places, not names: the album of a track, the artist of an album.
    album = albums[tracks[311][2]]
    assert album[1] == "Straße der Träume" and data["artists"][album[2]][1] == "Zoë Brandt"
    assert tracks[313][3] == "Zoë Brandt & Dax Orlo"
    # Moods by place, sorted names; cover only where Plex has one.
    assert data["moods"] == sorted(data["moods"])
    assert [data["moods"][number] for number in tracks[317][7]] == ["Fun", "Summery"]
    assert {row[0]: row[4] for row in albums}[209] == 0


def test_a_partial_run_that_changed_nothing_keeps_the_name_and_a_change_gives_a_new_one(reader: TestClient) -> None:
    first = reader.get("/api/v1/catalog").headers["etag"]
    with SessionLocal() as db:
        row = db.scalars(select(MediaServer)).one()
        row.synced_at = row.synced_at.replace(year=row.synced_at.year + 1)
        db.commit()
    assert reader.get("/api/v1/catalog", headers={"If-None-Match": first}).status_code == 304
    with SessionLocal() as db:
        db.get(CatalogTrack, (1, 1, 302)).title = "Glasshouse (Remastered)"
        row = db.scalars(select(MediaServer)).one()
        row.synced_at = row.synced_at.replace(year=row.synced_at.year + 1)
        db.commit()
    changed = reader.get("/api/v1/catalog", headers={"If-None-Match": first})
    assert changed.status_code == 200 and changed.headers["etag"] != first


def test_the_catalogue_is_read_from_one_state_even_when_a_run_switches_meanwhile(catalog_in: None,
                                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """The generation is read first; a run that deletes the rows right after must not leave the build half empty."""
    original = catalog_view.compact

    def meanwhile(db: Any, generation: int, server_id: int | None = None) -> dict[str, Any]:
        with SessionLocal() as other:
            other.execute(delete(CatalogTrack))
            other.commit()
        return original(db, generation, server_id)

    monkeypatch.setattr(catalog_view, "compact", meanwhile)
    view = catalog_view.current()
    assert view is not None and view.tracks == len(made_up_catalog.TRACKS)


def test_where_the_interface_searches_is_the_servers_choice(reader: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert reader.get("/api/v1/catalog/status").json()["search"] == "browser"
    monkeypatch.setattr(get_settings(), "server_search_from", len(made_up_catalog.TRACKS) - 1)
    assert reader.get("/api/v1/catalog/status").json()["search"] == "server"


def test_an_api_key_reads_the_catalogue_and_searches(client: TestClient, operator: Account, catalog_in: None) -> None:
    assert client.put("/api/v1/settings", json={"api_keys_allowed": True}).status_code == 200
    secret = client.post("/api/v1/api-keys", json={"name": "script"}).json()["secret"]
    with new_client() as program:
        program.headers.pop("X-Nexmix-Client", None)
        bearer = {"Authorization": f"Bearer {secret}"}
        assert program.get("/api/v1/catalog", headers=bearer).status_code == 200
        found = program.get("/api/v1/catalog/search", params={"q": "lluvia"}, headers=bearer).json()
        # "Lluvia de Verano" begins with the word; the song of the compilation is the album's version.
        assert [track["id"] for track in found["tracks"]] == [318, 317, 319]


# --- The rules -------------------------------------------------------------------------------------------------------


def test_folding_takes_case_accents_and_signs_away() -> None:
    assert search.fold("Ärger im Café — Straße!") == "arger im cafe strasse"
    assert search.fold("  Señorita\tLLUVIA ") == "senorita lluvia"
    assert search.fold("ﬁne Zoë") == "fine zoe"
    assert search.fold(None) == ""


def test_one_song_once_with_the_version_shown_and_how_many_there_are(reader: TestClient) -> None:
    found = reader.get("/api/v1/catalog/search", params={"q": "northern lights"}).json()
    ids = [track["id"] for track in found["tracks"]]
    # The Velvet Harbour's song once (its 1984 version), Dax Orlo's apart; never the karaoke one first. Then the
    # tracks of the album called so.
    assert ids == [301, 320, 322, 321]
    assert found["tracks"][0]["versions"] == 4 and found["tracks"][0]["song"] == 301
    assert found["total"] == 4


def test_the_version_shown_wins_over_a_lower_id_with_the_same_score(reader: TestClient) -> None:
    found = reader.get("/api/v1/catalog/search", params={"q": "glow"}).json()
    assert [track["id"] for track in found["tracks"]] == [331]
    assert found["tracks"][0]["versions"] == 2


def test_a_word_that_only_a_side_version_has_finds_that_version(reader: TestClient) -> None:
    found = reader.get("/api/v1/catalog/search", params={"q": "northern live"}).json()
    assert [track["id"] for track in found["tracks"]] == [308]


def test_words_in_any_order_and_without_accents(reader: TestClient) -> None:
    def ids(query: str) -> list[int]:
        return [track["id"] for track in reader.get("/api/v1/catalog/search", params={"q": query}).json()["tracks"]]

    assert ids("traume strasse") == ids("Straße der Träume") == ids("STRASSE TRÄUME")
    assert ids("traume strasse")[0] == 311
    assert 312 in ids("arger cafe") and 319 in ids("cafe")


def test_the_exact_title_comes_first(reader: TestClient) -> None:
    found = reader.get("/api/v1/catalog/search", params={"q": "drive"}).json()
    # "Drive" is the title; "Night Drive" has more listeners but only holds the word.
    assert found["tracks"][0]["id"] == 307


def test_filters_by_effective_mood_original_decade_and_genre(reader: TestClient) -> None:
    def ids(**params: Any) -> set[int]:
        found = reader.get("/api/v1/catalog/search", params={"q": "", "limit": 200, **params}).json()
        return {track["id"] for track in found["tracks"]}

    # A mood of the artist reaches every track of it (weight 0.4), the track's own the strongest.
    assert 326 in ids(mood="Mellow") and 302 in ids(mood="Mellow")
    # The group stands for its moods.
    assert ids(mood="g:calm") >= {326, 302}
    # Northern Lights on the best-of of 2005 counts in the eighties: its original year is 1984 (but one song once).
    assert 301 in ids(decade=1980) and 305 not in ids(decade=2000)
    assert ids(genre="Ambient") == {326, 327, 328, 329}
    assert ids(mood="No Such Mood") == set() and ids(genre="Nope") == set()


def test_album_and_artist(reader: TestClient) -> None:
    album = reader.get("/api/v1/catalog/albums/205").json()
    assert album["album"]["title"] == "Straße der Träume" and album["album"]["artist"] == "Zoë Brandt"
    assert [track["id"] for track in album["tracks"]] == [311, 312, 313]
    artist = reader.get("/api/v1/catalog/artists/101").json()
    assert artist["artist"]["name"] == "The Velvet Harbour"
    # One song once among the best known; albums newest first.
    assert [track["id"] for track in artist["tracks"]][:2] == [301, 302]
    assert [one["id"] for one in artist["albums"]] == [204, 202, 203, 201]
    assert reader.get("/api/v1/catalog/albums/999").status_code == 404
    assert reader.get("/api/v1/catalog/artists/999").status_code == 404


def test_the_filters_offered(reader: TestClient) -> None:
    facets = reader.get("/api/v1/catalog/facets").json()
    assert {"key": "calm", "moods": search.MOOD_GROUPS[0][1]} == facets["groups"][0]
    assert facets["decades"] == [1970, 1980, 1990, 2000, 2010, 2020]
    assert "Chanson" in facets["genres"]


def test_the_server_answers_in_time(reader: TestClient) -> None:
    """Measured seven times: the quickest says what the search costs (other work on the machine only ever adds to a
    run, never takes away), the middle one that it is not slow as a rule. A search that got slower for real is slower
    every time; a busy machine slows a few runs, not all."""
    runs = [reader.get("/api/v1/catalog/search", params={"q": "northern"}).json() for _ in range(7)]
    took = sorted(found["took_ms"] for found in runs)
    assert took[0] < 50, took
    assert took[3] < 250, took
    assert runs[0]["tracks_in_catalog"] == len(made_up_catalog.TRACKS)


# --- Both ways the same ----------------------------------------------------------------------------------------------


def test_the_shared_list_of_queries_holds_on_the_server_and_in_the_prepared_catalogue(reader: TestClient) -> None:
    catalog = reader.get("/api/v1/catalog").json()
    ix = search.prepare(catalog)
    answers = [{"case": case, "expect": server_search(reader, case)} for case in CASES]
    for answer in answers:
        assert local_search(ix, answer["case"]) == answer["expect"], answer["case"]
    if os.environ.get("NEXMIX_RECORD_SEARCH") == "1":
        FIXTURES.mkdir(parents=True, exist_ok=True)
        (FIXTURES / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=1) + "\n",
                                               encoding="utf-8", newline="\n")
        (FIXTURES / "cases.json").write_text(json.dumps(answers, ensure_ascii=False, indent=1) + "\n",
                                             encoding="utf-8", newline="\n")
    recorded_catalog = json.loads((FIXTURES / "catalog.json").read_text(encoding="utf-8"))
    recorded = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))
    assert recorded_catalog == catalog, "The compact catalogue changed: record the fixtures anew (see the top)."
    assert recorded == answers, "The search finds something else now: record the fixtures anew (see the top)."


def test_the_search_index_finds_parts_of_words_and_the_short_ones_are_looked_at_by_hand(reader: TestClient) -> None:
    view = catalog_view.current()
    assert view is not None
    with SessionLocal() as db:
        assert catalog_view.candidates(db, view, "a ol") is None
        found = catalog_view.candidates(db, view, "lassho")
    assert found is not None and {view.index.ids[p] for p in found} == {302, 306, 328}


# --- After a real run ------------------------------------------------------------------------------------------------


def test_a_run_of_the_catalogue_makes_the_search_index_and_disconnecting_empties_it(client: TestClient, operator: Account,
                                                                                   fake_plex: Any) -> None:
    from sqlalchemy import text

    from app.services import jobs

    from .test_catalog_sync import connect

    connect(client, fake_plex)
    assert jobs.run_pending(lane="catalog") == 1
    with SessionLocal() as db:
        indexed = db.execute(text("SELECT count(*) FROM track_search")).scalar()
    assert indexed == client.get("/api/v1/catalog/status").json()["counts"]["tracks"] > 0
    found = client.get("/api/v1/catalog/search", params={"q": "glasshou"}).json()
    assert found["total"] >= 1 and all("Glasshouse" in track["title"] for track in found["tracks"])
    assert client.delete("/api/v1/plex").status_code in (200, 204)
    with SessionLocal() as db:
        assert db.execute(text("SELECT count(*) FROM track_search")).scalar() == 0
