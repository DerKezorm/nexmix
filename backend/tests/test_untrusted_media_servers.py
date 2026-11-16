"""What a media server sends is not trusted, its size neither, and where its name points is checked at every
connection; what a page of another site can make a signed-in browser do.

* An answer of Music Assistant in parts is held to ``MAX_MESSAGE`` in all; past it the websocket closes.
* Answers of Plex, Jellyfin and Music Assistant (lists, covers, ``/info``, pictures) are read up to their limit and no
  further, a declared length past it is not read at all; a line of Plex's event stream without end ends the stream.
* A server's name is looked up and checked again at every request and every websocket: a name that points at a
  link-local or metadata address later is never reached (DNS rebinding).
* Guards: a JSON body sent as text/plain, the tab header on every writing route of the media servers, a guest of Music
  Assistant and the twin on Plex, no secret of a media server in any GET answer, event or log line."""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app.services import jellyfin, music_assistant, plex

MIB = 1024 * 1024


# --- Music Assistant: an answer in parts has no end --------------------------------------------------------------


class _Flooding:
    """A websocket of a Music Assistant that answers every command with parts, without end (a server taken over, or
    broken): each part is a list of small entries, well under ``MAX_MESSAGE``."""

    def __init__(self, parts: int, per_part: int) -> None:
        self.inbox: queue.Queue[str] = queue.Queue()
        self.parts = parts
        self.per_part = per_part
        server = "ab" * 16
        self.inbox.put(json.dumps({"server_id": server, "server_version": "2.10.9", "schema_version": 65,
                                   "min_supported_schema_version": 28, "name": "x"}))
        self.sent_parts = 0

    def send(self, text: str) -> None:
        message = json.loads(text)
        if message["command"] == "auth":
            self.inbox.put(json.dumps({"message_id": "auth",
                                       "result": {"authenticated": True, "user": {"role": "admin"}}}))
            return
        ident = message["message_id"]
        part = json.dumps({"message_id": ident, "partial": True, "result": ["x" * 64] * self.per_part})

        def flood() -> None:
            for _ in range(self.parts):
                self.inbox.put(part)
                self.sent_parts += 1
            self.inbox.put(json.dumps({"message_id": ident, "result": []}))

        threading.Thread(target=flood, daemon=True).start()

    def recv(self, timeout: float) -> str | None:
        try:
            return self.inbox.get(timeout=min(timeout, 0.2))
        except queue.Empty:
            return None

    def close(self) -> None:
        pass


def test_an_answer_of_music_assistant_in_parts_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """One command, and a server that keeps sending parts (here some 77 MB, each part well under ``MAX_MESSAGE``):
    the parts count together, past ``MAX_MESSAGE`` the websocket closes and the command fails."""
    flood = _Flooding(parts=1100, per_part=1000)  # ~ 1100 x 70 kB = some 77 MB, every part < 2 MB
    monkeypatch.setattr(music_assistant, "CHANNEL", lambda _address: flood)
    connection = music_assistant.Connection("http://ma.example.com:8095", "t" * 20)
    connection.open()
    try:
        try:
            found = connection.call("music/tracks/library_items", {}, timeout=60)
        except music_assistant.MaError:
            return  # refused: what is wanted
        assert len(json.dumps(found)) <= music_assistant.MAX_MESSAGE, (
            f"an answer of {len(json.dumps(found)) // MIB} MiB was put together and taken")
    finally:
        connection.close()


# --- Answers read whole before their size is looked at -----------------------------------------------------------


class _Counted:
    def __init__(self, chunks: int, size: int = MIB) -> None:
        self.chunks = chunks
        self.size = size
        self.read = 0

    def __iter__(self) -> Iterator[bytes]:
        for _ in range(self.chunks):
            self.read += 1
            yield b"\xff" * self.size


def test_a_picture_of_music_assistant_is_read_no_further_than_its_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cover is asked for by every reader and guest: an answer without end is read no further than ``COVER_BYTES``
    (2 MB)."""
    body = _Counted(chunks=40)
    monkeypatch.setattr(music_assistant, "TRANSPORT", httpx.MockTransport(
        lambda _request: httpx.Response(200, headers={"content-type": "image/jpeg"}, content=iter(body))))
    with pytest.raises(music_assistant.MaError):
        music_assistant.picture("http://ma.example.com:8095", "a" * 64, 256)
    assert body.read <= 4, f"{body.read} MiB read for a picture of at most 2 MB"


def test_an_answer_of_jellyfin_is_read_no_further_than_its_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cover of Jellyfin without end is read no further than ``COVER_BYTES`` (2 MB); every other answer stops at
    ``ANSWER_BYTES`` the same way."""
    body = _Counted(chunks=100)
    monkeypatch.setattr(jellyfin, "TRANSPORT", httpx.MockTransport(
        lambda _request: httpx.Response(200, headers={"content-type": "image/jpeg"}, content=iter(body))))
    with jellyfin.JellyfinServer("http://jf.example.com:8096", "t" * 32, "dev") as server, \
            pytest.raises(jellyfin.JellyfinError):
        server.cover("b" * 32, 200)
    assert body.read <= 4, f"{body.read} MiB read for a cover of at most 2 MB"


# --- A guest of a playlist on Music Assistant, and the twin on Plex ---------------------------------------------------


def test_a_guest_of_music_assistant_hears_the_twin_but_reaches_nothing_of_plex_by_number(
        client: TestClient, operator: object, fake_ma: object, fake_plex: object) -> None:
    """``preview._twin`` swaps a track of Music Assistant for the same track on Plex, on the server. The guest names
    only tracks of its playlist's server: a Plex number (the twin's own, or any other) and a Plex cover stay 404."""
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import CatalogAlbum, CatalogTrack
    from app.services import media
    from tests.ma_world import connect as connect_ma
    from tests.ma_world import make, number_of
    from tests.plex_world import connect as connect_plex
    from tests.test_guests_and_plex import phone

    connect_plex(client, fake_plex)  # type: ignore[arg-type]
    server = connect_ma(client, fake_ma)  # type: ignore[arg-type]
    track = number_of(server, fake_ma.track_by_name("Fever Glass").id)  # type: ignore[attr-defined]
    playlist = make(client, server, "Party", [track])
    with SessionLocal() as db:
        plex_id = media.primary(db).id  # type: ignore[union-attr]
        generation = media.generation_of(db, plex_id)
        plex_tracks = list(db.scalars(select(CatalogTrack.id).where(CatalogTrack.server_id == plex_id,
                                                                     CatalogTrack.generation == generation)))
        plex_album = db.scalar(select(CatalogAlbum.id).where(CatalogAlbum.server_id == plex_id,
                                                             CatalogAlbum.generation == generation))
    assert plex_tracks and plex_album
    assert client.post(f"/api/v1/playlists/{playlist['id']}/invite").status_code == 200
    code = client.put(f"/api/v1/playlists/{playlist['id']}/invite",
                      json={"preview": True, "start": True}).json()["invite"]["code"]
    guest = phone(code)
    heard = guest.get(f"/api/v1/preview/{track}")
    assert heard.status_code == 200 and heard.headers["content-type"] == "audio/mpeg"  # the twin on Plex
    for other in plex_tracks[:5]:
        assert guest.get(f"/api/v1/preview/{other}").status_code == 404
    assert guest.get(f"/api/v1/guest/cover/{plex_album}").status_code == 404
    catalogue = guest.get("/api/v1/guest/catalog").json()
    assert not ({row[0] for row in catalogue["tracks"]} & set(plex_tracks))
    added = guest.post("/api/v1/guest/items", json={"tracks": plex_tracks[:1]})
    assert added.status_code in (200, 403, 409) and plex_tracks[0] not in {
        entry["track"] for entry in guest.get("/api/v1/guest").json()["playlist"]["items"]}


# --- Another site: a JSON body as a form sends it ------------------------------------------------------------------


def test_a_json_body_sent_as_text_plain_is_not_taken(client: TestClient, operator: object) -> None:
    """A form of another site can send text/plain only (no preflight): FastAPI must not read it as JSON (422)."""
    made = client.post("/api/v1/playlists", content=json.dumps({"name": "Von woanders"}),
                       headers={"content-type": "text/plain"})
    assert made.status_code == 422
    moved = client.post("/api/v1/playlists/1/move", content=json.dumps({"server": 2, "old": "delete"}),
                        headers={"content-type": "text/plain"})
    assert moved.status_code in (404, 422)


def test_every_new_writing_route_refuses_a_browser_without_the_tab_header(client: TestClient, operator: object,
                                                                            fake_jellyfin: object) -> None:
    """A page of another site (or of a sibling under the same domain, SameSite=Lax lets the cookie through) can make
    the browser send a form, never a header of its own: ``GuardMiddleware`` refuses every change without
    ``X-Nexmix-Client`` (400). Walked over every writing route of the media servers, mirrors and moves, with the
    operator's cookie."""
    from tests.jellyfin_world import connect
    from tests.test_routes import api_routes, concrete

    connect(client, fake_jellyfin)  # type: ignore[arg-type]
    new = ("/api/v1/jellyfin", "/api/v1/music-assistant", "/api/v1/servers", "/mirrors", "/move")
    routes = [(method, path) for method, path in api_routes()
              if method in ("POST", "PUT", "PATCH", "DELETE") and any(part in path for part in new)]
    assert len(routes) >= 25
    with TestClient(client.app, base_url="http://testserver", cookies=dict(client.cookies)) as page:
        odd = []
        for method, path in routes:
            answer = page.request(method, concrete(path), content=b'{"server": 2}',
                                  headers={"content-type": "text/plain", "Origin": "https://other.example.com"})
            if answer.status_code != 400 or answer.json()["detail"]["code"] != "client_required":
                odd.append(f"{method} {path} -> {answer.status_code}")
    assert odd == []


# --- Three servers at once: no secret, address or id of a server in any answer of a GET route --------------------------


def test_no_get_route_tells_a_token_an_address_or_an_id_of_a_media_server(
        client: TestClient, operator: object, fake_plex: object, fake_jellyfin: object, fake_ma: object,
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Every GET route of the interface, with the real numbers of servers, playlists, entries, tracks and albums, as the
    operator (who sees most): no token, no address, no server id, no id of a track, album or playlist of Jellyfin or
    Music Assistant, no picture id. The same for what the hub publishes and for the log."""
    import re

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import CatalogAlbum, MediaServer, PlaylistItem, ServerItem, ServerPlaylist
    from app.services import events, jellyfin_link, logs, ma_link, plex_link
    from tests.jellyfin_world import connect as connect_jf
    from tests.jellyfin_world import make as make_jf
    from tests.jellyfin_world import sync as sync_jf
    from tests.ma_world import connect as connect_ma
    from tests.ma_world import make as make_ma
    from tests.plex_world import connect as connect_plex
    from tests.test_routes import api_routes

    told: list[str] = []
    original = events.hub.publish

    def keep(topic: str, kind: str, payload: object) -> None:
        told.append(repr(payload))
        original(topic, kind, payload)

    monkeypatch.setattr(events.hub, "publish", keep)
    connect_plex(client, fake_plex)  # type: ignore[arg-type]
    jf = connect_jf(client, fake_jellyfin)  # type: ignore[arg-type]
    ma = connect_ma(client, fake_ma)  # type: ignore[arg-type]
    with SessionLocal() as db:
        def some(server: int) -> list[int]:
            return [int(one) for one in db.scalars(select(ServerItem.id).where(
                ServerItem.server_id == server, ServerItem.kind == "track").limit(3))]
        jf_tracks, ma_tracks = some(jf), some(ma)
    a = make_jf(client, jf, "Jelly", jf_tracks)
    b = make_ma(client, ma, "Assi", ma_tracks)
    sync_jf()
    mirrored = client.post(f"/api/v1/playlists/{a['id']}/mirrors", json={"server": ma})
    assert mirrored.status_code in (201, 409), mirrored.text
    sync_jf()

    with SessionLocal() as db:
        secrets_: set[str] = set()
        servers = list(db.scalars(select(MediaServer)))
        for row in servers:
            secrets_ |= {row.url, row.machine_id or "", row.url.split("//")[-1]}
            try:
                token = (jellyfin_link.token_of(row) if row.kind == "jellyfin" else
                         ma_link.token_of(row) if row.kind == "music_assistant" else plex_link.token_of(row))
                secrets_.add(token)
            except Exception:  # noqa: BLE001, S110 - a row without a token
                pass
        for server in (jf, ma):
            secrets_ |= {key for key in db.scalars(select(ServerItem.ext_key).where(ServerItem.server_id == server))
                         if len(key) >= 16}
            secrets_ |= {key for key in db.scalars(select(CatalogAlbum.thumb).where(CatalogAlbum.server_id == server))
                         if key and len(key) >= 16}
        secrets_ |= {key for key in db.scalars(select(ServerPlaylist.ext_key)) if key and len(key) >= 16}
        entries = [int(one) for one in db.scalars(select(PlaylistItem.id).limit(3))]
        albums = [int(one) for one in db.scalars(select(CatalogAlbum.id).limit(3))]
        server_ids = [row.id for row in servers]
    secrets_ = {one for one in secrets_ if one and len(one) >= 8}
    assert len(secrets_) > 10

    answers: dict[str, str] = {}
    for method, path in api_routes():
        if method != "GET" or path.endswith("/events") or "/preview/" in path or "/cover/" in path \
                or path.startswith(("/api/v1/guest", "/api/v1/join", "/api/v1/oidc", "/api/v1/auth/oidc")):
            continue
        values = {"server_id": server_ids, "playlist_id": [a["id"], b["id"]], "entry_id": entries,
                  "album_id": albums, "track_id": jf_tracks + ma_tracks}
        names = re.findall(r"\{([^}:]+)", path)
        fills: list[dict[str, object]] = [{}]
        for name in names:
            fills = [{**one, name: value} for one in fills for value in values.get(name, [1])]
        for fill in fills:
            concrete_path = path
            for name, value in fill.items():
                concrete_path = re.sub(r"\{" + name + r"(?::[^}]*)?\}", str(value), concrete_path)
            for query in ("", *(f"?server={one}" for one in server_ids)):
                answer = client.get(concrete_path + query)
                if answer.headers.get("content-type", "").startswith(("application/json", "text/")):
                    answers[concrete_path + query] = answer.text
    log = logs.log_file().read_text(encoding="utf-8")
    leaks = [(where, secret[:6] + "…") for secret in secrets_ for where, text in
             [*answers.items(), ("log", log), ("events", "\n".join(told))] if secret in text]
    assert len(answers) > 100
    assert leaks == []


# --- More answers read no further than their limit ------------------------------------------------------------------


def test_a_cover_of_plex_is_read_no_further_than_its_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _Counted(chunks=100)
    monkeypatch.setattr(plex, "TRANSPORT", httpx.MockTransport(
        lambda _request: httpx.Response(200, headers={"content-type": "image/jpeg"}, content=iter(body))))
    with plex.PlexServer("http://plex.example.com:32400", "t" * 20, "client") as server, \
            pytest.raises(plex.PlexError) as refused:
        server.cover("/library/metadata/12/thumb/34", 200)
    assert refused.value.code == "no_cover"
    assert body.read <= 4, f"{body.read} MiB read for a cover of at most 2 MB"


def test_a_list_of_plex_too_long_is_refused_and_one_that_fits_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same limit for every answer of Plex (``ANSWER_BYTES``): past it ``plex_error``, below it the answer as it
    was, also packed with gzip (unpacked in bounded steps)."""
    import gzip

    body = _Counted(chunks=80)
    monkeypatch.setattr(plex, "TRANSPORT", httpx.MockTransport(
        lambda _request: httpx.Response(200, headers={"content-type": "application/json"}, content=iter(body))))
    with plex.PlexServer("http://plex.example.com:32400", "t" * 20, "client") as server, \
            pytest.raises(plex.PlexError) as refused:
        server.get("/library/sections")
    assert refused.value.code == "plex_error"
    assert body.read <= plex.ANSWER_BYTES // MIB + 2

    packed = gzip.compress(json.dumps({"MediaContainer": {"size": 1, "Directory": []}}).encode())
    monkeypatch.setattr(plex, "TRANSPORT", httpx.MockTransport(lambda _request: httpx.Response(
        200, headers={"content-type": "application/json", "content-encoding": "gzip"}, content=packed)))
    with plex.PlexServer("http://plex.example.com:32400", "t" * 20, "client") as server:
        assert server.get("/library/sections") == {"size": 1, "Directory": []}


def test_an_answer_that_says_it_is_too_long_is_not_read_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _Counted(chunks=10)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/jpeg", "content-length": str(10 * MIB)},
                              content=iter(body))

    monkeypatch.setattr(jellyfin, "TRANSPORT", httpx.MockTransport(handler))
    with jellyfin.JellyfinServer("http://jf.example.com:8096", "t" * 32, "dev") as server, \
            pytest.raises(jellyfin.JellyfinError) as refused:
        server.cover("b" * 32, 200)
    assert refused.value.code == "no_cover"
    assert body.read == 0


def test_the_info_of_music_assistant_is_read_no_further_than_its_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _Counted(chunks=50)
    monkeypatch.setattr(music_assistant, "TRANSPORT", httpx.MockTransport(
        lambda _request: httpx.Response(200, headers={"content-type": "application/json"}, content=iter(body))))
    with pytest.raises(music_assistant.MaError) as refused:
        music_assistant.info("http://ma.example.com:8095")
    assert refused.value.code == "not_ma"
    assert body.read <= 1


def test_a_line_of_the_event_stream_of_plex_without_end_ends_the_stream() -> None:
    from app.services import plex_events

    sent = {"chunks": 0}

    def endless() -> Iterator[bytes]:
        # 8 MiB of one line, then the stream ends: a limit that does not hold fails here instead of filling the memory.
        yield b"event: ping\ndata: {}\n\n"
        for _ in range(128):
            sent["chunks"] += 1
            yield b"x" * 65536

    lines = plex_events.lines_of(endless(), limit=MIB)
    assert next(lines) == "event: ping"
    assert next(lines) == "data: {}"
    assert next(lines) == ""
    with pytest.raises(plex.PlexError):
        next(lines)
    assert sent["chunks"] <= MIB // 65536 + 1
    assert list(plex_events.lines_of([b"event: time", b"line\r\ndata: 1\n", b"rest"])) == [
        "event: timeline\r", "data: 1", "rest"]


# --- A name checked at every connection --------------------------------------------------------------------------


class _Names:
    """A resolver whose answer the test changes: the name first points somewhere allowed, later at a metadata
    service."""

    def __init__(self, *addresses: str) -> None:
        self.addresses = list(addresses)
        self.asked = 0

    def __call__(self, _host: str, _port: int) -> list[str]:
        self.asked += 1
        return list(self.addresses)


class _Dialled:
    """Every connection the client would open, recorded instead of opened."""

    def __init__(self) -> None:
        self.to: list[tuple[str, int]] = []

    def tcp(self, host: str, port: int) -> object:
        import httpcore

        self.to.append((host, port))
        raise httpcore.ConnectError("not here")

    def plain(self, address: tuple[str, int], *_args: object, **_kwargs: object) -> object:
        self.to.append(address)
        raise OSError("not here")


@pytest.fixture
def dialled(monkeypatch: pytest.MonkeyPatch) -> _Dialled:
    import socket

    import httpcore

    found = _Dialled()

    def sync(_backend: object, host: str, port: int, *_args: object, **_kwargs: object) -> object:
        return found.tcp(host, port)

    async def later(_backend: object, host: str, port: int, *_args: object, **_kwargs: object) -> object:
        return found.tcp(host, port)

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", sync)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", later)
    monkeypatch.setattr(socket, "create_connection", found.plain)
    for module in (plex, jellyfin, music_assistant):
        monkeypatch.setattr(module, "TRANSPORT", None)
        monkeypatch.setattr(module, "ASYNC_TRANSPORT", None)
    monkeypatch.setattr(music_assistant, "CHANNEL", None)
    return found


def _every_way(url: str) -> list[str]:
    """Each way nexmix reaches a media server, tried once: the codes the plain requests failed with."""
    import asyncio

    from app.services import jellyfin_events

    failed: list[str] = []
    with plex.PlexServer(url, "t" * 20, "client") as server, pytest.raises(plex.PlexError) as one:
        server.identity()
    failed.append(one.value.code)
    with jellyfin.JellyfinServer(url, "t" * 32, "dev") as other, pytest.raises(jellyfin.JellyfinError) as two:
        other.public_info()
    failed.append(two.value.code)
    with pytest.raises(music_assistant.MaError) as three:
        music_assistant.info(url)
    failed.append(three.value.code)

    async def sound() -> None:
        async with plex.async_client() as client:
            await client.get(f"{url}/music/:/transcode/universal/start.mp3")

    with pytest.raises(httpx.ConnectError):
        asyncio.run(sound())
    with pytest.raises(OSError):
        music_assistant._Socket(url)
    with pytest.raises(OSError), jellyfin_events.open_socket(url, "t" * 32, "dev"):
        pass
    return failed


def test_a_name_that_points_somewhere_refused_later_is_never_reached(monkeypatch: pytest.MonkeyPatch,
                                                                     dialled: _Dialled) -> None:
    """Checked when the operator sets it, the name points at an address of the own network; later its owner turns it
    to a metadata service. Every request and both websockets look it up again and connect to nothing; the name that
    still points where it did is reached at exactly the address that was checked."""
    names = _Names("192.0.2.10")
    monkeypatch.setattr(plex, "resolver", names)
    url = jellyfin.check_address("http://media.example.com:8096")
    assert music_assistant.check_address(url) == url

    names.addresses = ["169.254.169.254"]
    asked = names.asked
    assert _every_way(url) == ["plex_unreachable", "jellyfin_unreachable", "ma_unreachable"]
    assert dialled.to == []
    assert names.asked - asked == 6, "the name was not looked up again at every connection"

    names.addresses = ["192.0.2.77", "fd00:ec2::254"]  # one of them a metadata service: none is reached
    _every_way(url)
    assert dialled.to == []

    names.addresses = ["192.0.2.77"]
    _every_way(url)
    assert dialled.to and set(dialled.to) == {("192.0.2.77", 8096)}


def test_every_address_of_a_name_is_tried_in_turn(monkeypatch: pytest.MonkeyPatch, dialled: _Dialled) -> None:
    monkeypatch.setattr(plex, "resolver", _Names("192.0.2.77", "192.0.2.78"))
    with plex.PlexServer("http://plex.example.com:32400", "t" * 20, "client") as server, \
            pytest.raises(plex.PlexError):
        server.identity()
    assert dialled.to == [("192.0.2.77", 32400), ("192.0.2.78", 32400)]
