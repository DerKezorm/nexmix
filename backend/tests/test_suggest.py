"""Suggestions and filling by mood: the rules on a made-up catalogue (rank, the kinds, at most two per artist, one song
once in the version shown, never what is in the playlist or hidden, the reasons, the two voices), the same answers in
the browser, and the routes with their rights.

``tests/fixtures/suggest/cases.json`` holds what the rules give for a list of cases on the compact catalogue of the
made-up library (``tests/fixtures/search/catalog.json``); ``frontend/src/lib/suggest.test.ts`` holds the browser to the
same file. When the rules change on purpose, both sides change, and the file is made anew:

    NEXMIX_RECORD_SUGGEST=1 python -m pytest tests/test_suggest.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.models import Account
from app.services import jobs, search, suggest

from . import made_up_catalog
from .conftest import make_account, new_client
from .fakeplex import FakePlex

FIXTURES = Path(__file__).parent / "fixtures"
CATALOG = FIXTURES / "search" / "catalog.json"
CASES = FIXTURES / "suggest" / "cases.json"

SUGGEST_CASES: list[dict[str, Any]] = [
    {"seed": [301]},
    {"seed": [301], "mode": "known"},
    {"seed": [301], "mode": "new"},
    {"seed": [311, 312, 313]},
    {"seed": [311, 312, 313], "mode": "new"},
    {"seed": [317, 319], "exclude": [317, 319]},
    {"seed": [326], "hidden": [328]},
    {"seed": [326]},
    {"seed": [301, 302, 303, 304], "together": {"311": 3, "317": 1}},
    {"seed": [322], "sound": {"326": 0.1, "301": 0.5, "999": 0.01}},
    {"seed": []},
    {"seed": [305]},
    {"seed": [314, 315, 316], "mode": "new", "limit": 3},
    {"seed": [324, 317, 312, 302, 326], "mode": "known"},
]

BUILD_CASES: list[dict[str, Any]] = [
    {"moods": ["Energetic"], "minutes": 60, "hits": 55, "seed": 1},
    {"moods": ["Energetic", "Mellow"], "decades": [1980], "seed": 7},
    {"moods": ["Summery", "Fun", "Cheerful"], "hits": 0, "seed": 3},
    {"moods": ["Summery", "Fun", "Cheerful"], "hits": 100, "seed": 3},
    {"moods": ["Summery"], "hits": 100, "seed": 42, "exclude": [317]},
    {"moods": ["No Such Mood"], "seed": 5},
    {"moods": ["Romantic", "Warm", "Brooding"], "minutes": 5, "seed": 9},
    {"moods": ["Romantic", "Warm", "Brooding", "Atmospheric", "Melancholy"], "minutes": 240, "seed": 2},
    {"moods": ["Romantic", "Warm", "Brooding", "Atmospheric", "Melancholy"], "minutes": 5, "seed": 2},
]


def index() -> search.Index:
    return search.prepare(json.loads(CATALOG.read_text(encoding="utf-8")))


def run_suggest(ix: search.Index, case: dict[str, Any]) -> list[dict[str, Any]]:
    return suggest.suggest(
        ix, suggest.seed_positions(ix, case["seed"]), mode=case.get("mode", "fit"), exclude=case.get("exclude", []),
        hidden=case.get("hidden", []), together={int(k): v for k, v in case.get("together", {}).items()},
        sound={int(k): v for k, v in case.get("sound", {}).items()}, limit=case.get("limit", suggest.LIMIT))


def run_build(ix: search.Index, case: dict[str, Any]) -> list[dict[str, Any]]:
    return suggest.build(ix, case["moods"], decades=case.get("decades", []), minutes=case.get("minutes", 60),
                         hits=case.get("hits", 55), exclude=case.get("exclude", []), hidden=case.get("hidden", []),
                         seed=case["seed"])


def artist_place(ix: search.Index, track: int) -> int:
    return suggest.artist_of(ix, ix.position[track])


# --- The rules -------------------------------------------------------------------------------------------------------


def test_one_song_once_in_the_version_shown_and_never_the_seed() -> None:
    ix = index()
    found = run_suggest(ix, {"seed": [301]})
    assert found, "something fits"
    assert all(track["id"] == track["song"] for track in found)
    assert len({track["song"] for track in found}) == len(found)
    assert 301 not in [track["song"] for track in found], "the seed's own song is never suggested"
    # A seed given as another version (305 is a version of the song 301) leaves that song out too.
    assert 301 not in [track["song"] for track in run_suggest(ix, {"seed": [305], "limit": 50})]


def test_at_most_two_per_artist_and_one_for_discoveries() -> None:
    ix = index()
    for mode, cap in (("fit", 2), ("known", 2), ("new", 1)):
        for seed in ([301], [311, 312, 313], [326], [324, 317, 312, 302, 326]):
            found = run_suggest(ix, {"seed": seed, "mode": mode, "limit": 50})
            per: dict[int, int] = {}
            for track in found:
                per[artist_place(ix, track["id"])] = per.get(artist_place(ix, track["id"]), 0) + 1
            assert max(per.values(), default=0) <= cap, (mode, seed, per)


def test_what_is_in_the_playlist_or_hidden_is_never_suggested() -> None:
    ix = index()
    plain = [track["song"] for track in run_suggest(ix, {"seed": [326], "limit": 50})]
    assert 328 in plain
    hidden = [track["song"] for track in run_suggest(ix, {"seed": [326], "hidden": [328], "limit": 50})]
    assert 328 not in hidden and set(hidden) <= set(plain)
    left = [track["song"] for track in run_suggest(ix, {"seed": [326], "exclude": [328, plain[0]], "limit": 50})]
    assert 328 not in left and plain[0] not in left


def test_known_favours_the_popular_and_discoveries_what_was_never_played() -> None:
    ix = index()
    known = run_suggest(ix, {"seed": [311, 312, 313], "mode": "known", "limit": 50})
    new = run_suggest(ix, {"seed": [311, 312, 313], "mode": "new", "limit": 50})
    pop = {track["song"]: ix.song_pop[track["song"]] for track in known + new}
    assert pop[known[0]["song"]] >= pop[new[0]["song"]]
    assert ix.song_plays[new[0]["song"]] == 0


def test_the_reason_names_shared_moods_a_style_and_the_decade() -> None:
    ix = index()
    found = {track["song"]: track for track in run_suggest(ix, {"seed": [311], "limit": 50})}
    # Ärger im Café: on the album of the seed (Romantic, Bittersweet: 0.7 each, the tie by name), by the same artist
    # (Warm: 0.4 only), 1979. The seed's own Nostalgic it does not have.
    reason = found[312]["reason"]
    assert reason["moods"] == ["Bittersweet", "Romantic"]
    assert reason["style"] == "Chanson" and reason["decade"] == 1970
    assert reason["together"] == 0 and reason["sound"] is False


def test_the_two_voices_lift_a_song_and_say_so() -> None:
    ix = index()
    plain = run_suggest(ix, {"seed": [322], "limit": 50})
    lifted = run_suggest(ix, {"seed": [322], "sound": {"326": 0.05}, "limit": 50})
    assert 326 not in [track["song"] for track in plain], "nothing else speaks for it"
    assert lifted[0]["song"] == 326 and lifted[0]["reason"]["sound"] is True
    together = run_suggest(ix, {"seed": [301, 302, 303, 304], "together": {"311": 3}, "limit": 50})
    entry = next(track for track in together if track["song"] == 311)
    assert entry["reason"]["together"] == 3


def test_a_seed_of_nothing_suggests_nothing() -> None:
    assert run_suggest(index(), {"seed": []}) == []
    assert run_suggest(index(), {"seed": [999999]}) == []


def test_the_chance_is_the_same_for_the_same_seed() -> None:
    one, again, other = suggest.mulberry32(1), suggest.mulberry32(1), suggest.mulberry32(2)
    first = [one() for _ in range(50)]
    assert first == [again() for _ in range(50)]
    assert all(0 <= number < 1 for number in first) and len(set(first)) == 50
    assert first != [other() for _ in range(50)]
    # Spread over the whole range, not stuck in a corner.
    assert min(first) < 0.2 and max(first) > 0.8


def test_filling_by_mood_holds_its_rules() -> None:
    ix = index()
    for case in BUILD_CASES:
        found = run_build(ix, case)
        assert run_build(ix, case) == found, "the same seed, the same mix"
        songs = [track["song"] for track in found]
        assert len(songs) == len(set(songs)) and all(track["id"] == track["song"] for track in found)
        assert not set(case.get("exclude", [])) & set(songs)
        per: dict[int, int] = {}
        for track in found:
            assert 90 <= track["duration"] <= 600
            place = artist_place(ix, track["id"])
            per[place] = per.get(place, 0) + 1
            if case.get("decades"):
                assert ix.decade[ix.position[track["id"]]] in case["decades"]
        assert max(per.values(), default=0) <= 2
        # Never the same artist twice in a row where another one could stand between.
        places = [artist_place(ix, track["id"]) for track in found]
        for at in range(1, len(places)):
            if places[at] == places[at - 1]:
                assert all(place == places[at] for place in places[at:]), (case, places)
    assert run_build(ix, {"moods": ["No Such Mood"], "seed": 1}) == []
    short = run_build(ix, {"moods": ["Romantic", "Warm", "Brooding"], "minutes": 5, "seed": 9})
    assert sum(track["duration"] for track in short[:-1]) < 300 <= sum(track["duration"] for track in short)
    # The length stops it while there would be more.
    moods = ["Romantic", "Warm", "Brooding", "Atmospheric", "Melancholy"]
    long, brief = run_build(ix, {"moods": moods, "minutes": 240, "seed": 2}), run_build(ix, {"moods": moods, "minutes": 5, "seed": 2})
    assert len(brief) < len(long)
    assert sum(track["duration"] for track in brief[:-1]) < 300 <= sum(track["duration"] for track in brief)


def test_the_shared_cases_hold_in_both_languages() -> None:
    ix = index()
    numbers = suggest.mulberry32(7)
    answers = {
        "random": [round(numbers(), 12) for _ in range(5)],
        "suggest": [{"case": case, "expect": run_suggest(ix, case)} for case in SUGGEST_CASES],
        "build": [{"case": case, "expect": run_build(ix, case)} for case in BUILD_CASES],
    }
    if os.environ.get("NEXMIX_RECORD_SUGGEST") == "1":
        CASES.parent.mkdir(parents=True, exist_ok=True)
        CASES.write_text(json.dumps(answers, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    assert json.loads(CASES.read_text(encoding="utf-8")) == json.loads(json.dumps(answers)), (
        "The suggestions changed: record the fixtures anew (see the top).")


# --- The routes ------------------------------------------------------------------------------------------------------


@pytest.fixture
def catalog_in() -> None:
    made_up_catalog.install()


def test_the_server_suggests_what_the_rules_say_and_leaves_the_hidden_out(catalog_in: None) -> None:
    ix = index()
    mia = make_account("mia")
    with new_client(mia) as browser:
        answer = browser.get("/api/v1/suggestions", params={"seed": [301], "mode": "fit"})
        assert answer.status_code == 200
        assert answer.json()["tracks"] == run_suggest(ix, {"seed": [301]})
        assert answer.json()["seed"] == [301]
        first = answer.json()["tracks"][0]["song"]
        # Not again: by another version, every version of the song stays out.
        version = next(track for track in ix.ids if ix.song[ix.position[track]] == first)
        assert browser.post("/api/v1/suggestions/hidden", json={"track": version}).json() == {"songs": [first]}
        again = browser.get("/api/v1/suggestions", params={"seed": [301]}).json()["tracks"]
        assert first not in [track["song"] for track in again]
        assert again == run_suggest(ix, {"seed": [301], "hidden": [first]})
        assert browser.get("/api/v1/suggestions/hidden").json() == {"songs": [first]}
        # Twice is once.
        assert browser.post("/api/v1/suggestions/hidden", json={"track": first}).json() == {"songs": [first]}
        with new_client(make_account("noah")) as other:
            assert first in [track["song"] for track in
                             other.get("/api/v1/suggestions", params={"seed": [301]}).json()["tracks"]]
            assert other.get("/api/v1/suggestions/hidden").json() == {"songs": []}
        # The undo.
        assert browser.delete(f"/api/v1/suggestions/hidden/{first}").json() == {"songs": []}
        assert browser.post("/api/v1/suggestions/hidden", json={"track": 999999}).status_code == 404


def test_with_a_playlist_its_songs_stay_out_and_its_last_five_are_the_seed(catalog_in: None) -> None:
    ix = index()
    mia = make_account("mia")
    with new_client(mia) as browser:
        playlist = browser.post("/api/v1/playlists", json={"name": "Abend"}).json()["id"]
        tracks = [301, 302, 311, 312, 313, 317]
        browser.post(f"/api/v1/playlists/{playlist}/items", json={"tracks": tracks})
        answer = browser.get("/api/v1/suggestions", params={"playlist": playlist}).json()
        assert answer["seed"] == tracks[-5:]
        assert answer["tracks"] == run_suggest(ix, {"seed": tracks[-5:], "exclude": [301, 302, 311, 312, 313, 317]})
        with new_client(make_account("noah")) as other:
            assert other.get("/api/v1/suggestions", params={"playlist": playlist}).status_code == 404
            assert other.get("/api/v1/suggestions/mood", params={"moods": ["Energetic"],
                                                                  "playlist": playlist}).status_code == 404


def test_filling_by_mood_on_the_server(catalog_in: None) -> None:
    ix = index()
    with new_client(make_account("mia")) as browser:
        answer = browser.get("/api/v1/suggestions/mood",
                             params={"moods": ["Summery", "Fun", "Cheerful"], "hits": 0, "seed": 3})
        assert answer.status_code == 200
        expect = run_build(ix, {"moods": ["Summery", "Fun", "Cheerful"], "hits": 0, "seed": 3})
        assert answer.json() == {"tracks": expect, "duration": sum(track["duration"] for track in expect)}
        assert browser.get("/api/v1/suggestions/mood").status_code == 422, "at least one mood"


def test_often_together_counts_only_playlists_the_asker_may_see(catalog_in: None, client: TestClient,
                                                                 operator: Account) -> None:
    mia, noah = make_account("mia"), make_account("noah")
    with new_client(mia) as first, new_client(noah) as second:
        mine = first.post("/api/v1/playlists", json={"name": "Mias"}).json()["id"]
        first.post(f"/api/v1/playlists/{mine}/items", json={"tracks": [301, 311]})
        secret = second.post("/api/v1/playlists", json={"name": "Noahs"}).json()["id"]
        second.post(f"/api/v1/playlists/{secret}/items", json={"tracks": [301, 317, 319]})
        shared = second.post("/api/v1/playlists", json={"name": "Geteilt"}).json()["id"]
        second.post(f"/api/v1/playlists/{shared}/items", json={"tracks": [305, 311, 324]})

        def together(browser: TestClient) -> dict[str, Any]:
            answer = browser.get("/api/v1/suggestions/together", params={"songs": [301]})
            assert answer.status_code == 200
            return answer.json()

        # Mia sees only her own: Noah's playlists add nothing, not even to the number.
        assert together(first) == {"songs": [{"song": 311, "count": 1}], "playlists": 1}
        second.put(f"/api/v1/playlists/{shared}/shares/{mia.id}", json={"access": "read"})
        assert together(first) == {"songs": [{"song": 311, "count": 2}, {"song": 324, "count": 1}], "playlists": 2}
        # The operator sees every playlist.
        assert together(client)["playlists"] == 3
        # A playlist in the trash counts no more.
        second.delete(f"/api/v1/playlists/{shared}")
        assert together(first) == {"songs": [{"song": 311, "count": 1}], "playlists": 1}
    with new_client() as stranger:
        assert stranger.get("/api/v1/suggestions/together", params={"songs": [301]}).status_code == 401


@pytest.fixture
def heard(client: TestClient, operator: Account, fake_plex: FakePlex) -> FakePlex:
    """The stand-in Plex with its sonic analysis switched on and five tracks analysed, its catalogue fetched."""
    fake_plex.analysis = True
    # 3009 is the best-of version of 3001: the same song, which never sounds "alike" to itself.
    for key in (3001, 3003, 3009, 3013, 3020, 3026):
        fake_plex.items[key].analysed = True
    answer = client.post("/api/v1/plex/connect", json={"url": "http://127.0.0.1:32400", "token": fake_plex.account_token})
    assert answer.status_code == 200, answer.text
    assert jobs.run_pending(lane="catalog") == 1
    return fake_plex


def nearest_calls(fake: FakePlex) -> list[str]:
    return [path for _method, path, _params in fake.library_calls() if path.endswith("/nearest")]


def test_sounds_alike_asks_plex_while_the_analysis_runs_and_keeps_the_answer(client: TestClient,
                                                                             heard: FakePlex) -> None:
    assert client.get("/api/v1/catalog/status").json()["analysis"]["state"] in ("running", "on")
    ix = search.prepare(client.get("/api/v1/catalog").json())
    answer = client.get("/api/v1/suggestions/sonic", params={"track": [3001]})
    assert answer.status_code == 200, answer.text
    found = answer.json()
    assert found["state"] in ("running", "on")
    # The stand-in's distances: a hundredth per step of the id; each as the song it belongs to, closest first.
    expect = [(ix.song[ix.position[key]], round(abs(key - 3001) / 100, 4)) for key in (3003, 3013, 3020, 3026)]
    assert [(one["song"], one["distance"]) for one in found["songs"]] == [pair for pair in expect if pair[1] <= 0.6]
    # Plex named 3009 as close; it is a version of the seed's own song and stays out.
    assert ix.song[ix.position[3009]] == ix.song[ix.position[3001]]
    assert ix.song[ix.position[3001]] not in [one["song"] for one in found["songs"]]
    assert nearest_calls(heard) == ["/library/metadata/3001/nearest"]
    [(_method, _path, params)] = [call for call in heard.library_calls() if call[1].endswith("/nearest")]
    assert params == {"limit": "30", "maxDistance": "0.60"}
    # Asked again: from what was kept, Plex is not asked.
    assert client.get("/api/v1/suggestions/sonic", params={"track": [3001]}).json() == found
    assert len(nearest_calls(heard)) == 1
    # A track not analysed: Plex answers nothing, and so does nexmix.
    assert client.get("/api/v1/suggestions/sonic", params={"track": [3002]}).json()["songs"] == []
    assert client.get("/api/v1/suggestions/sonic", params={"track": [999999]}).status_code == 404
    # The suggestions on the server take the voice in.
    lifted = client.get("/api/v1/suggestions", params={"seed": [3001], "limit": 50}).json()["tracks"]
    assert any(track["reason"]["sound"] for track in lifted)


def test_asking_plex_about_the_sound_has_a_brake(client: TestClient, heard: FakePlex,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import brakes

    monkeypatch.setitem(brakes.LIMITS, "sonic", 2)
    assert client.get("/api/v1/suggestions/sonic", params={"track": [3001]}).status_code == 200
    assert client.get("/api/v1/suggestions/sonic", params={"track": [3003]}).status_code == 200
    refused = client.get("/api/v1/suggestions/sonic", params={"track": [3013]})
    assert refused.status_code == 429 and refused.json()["detail"]["code"] == "slow_down"
    assert "retry-after" in refused.headers
    # What is kept costs Plex nothing and counts nothing.
    assert client.get("/api/v1/suggestions/sonic", params={"track": [3001]}).status_code == 200
    assert len(nearest_calls(heard)) == 2


def test_sounds_alike_is_empty_while_the_analysis_is_off(client: TestClient, operator: Account,
                                                          fake_plex: FakePlex) -> None:
    fake_plex.analysis = False
    answer = client.post("/api/v1/plex/connect", json={"url": "http://127.0.0.1:32400", "token": fake_plex.account_token})
    assert answer.status_code == 200
    assert jobs.run_pending(lane="catalog") == 1
    assert client.get("/api/v1/suggestions/sonic", params={"track": [3001]}).json() == {"state": "off", "songs": []}
    assert nearest_calls(fake_plex) == []
    with new_client() as stranger:
        assert stranger.get("/api/v1/suggestions/sonic", params={"track": [3001]}).status_code == 401


def test_a_key_reads_suggestions_but_changes_nothing(catalog_in: None, client: TestClient,
                                                     operator: Account) -> None:
    client.put("/api/v1/settings", json={"api_keys_allowed": True})
    key = client.post("/api/v1/api-keys", json={"name": "script", "scope": "read"})
    assert key.status_code == 201, key.text
    token = key.json()["secret"]
    with TestClient(client.app, base_url="http://testserver") as program:
        headers = {"Authorization": f"Bearer {token}"}
        assert program.get("/api/v1/suggestions", params={"seed": [301]}, headers=headers).status_code == 200
        assert program.get("/api/v1/suggestions/hidden", headers=headers).status_code == 200
        refused = program.post("/api/v1/suggestions/hidden", json={"track": 301}, headers=headers)
        assert refused.status_code in (401, 403)
