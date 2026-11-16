"""The stand-in Music Assistant over a socket, for the browser tests: one port of 127.0.0.1, the websocket ``/ws`` and
the plain requests on it, as Music Assistant has them.

    python -m tests.fakema.serve --port 28624

nexmix is pointed at it by its address (``http://127.0.0.1:<port>``), as an operator would type it. ``GET /fake/token``
gives a made-up long-lived token (made at the start, never written down; ``?role=user`` a new one of a user without the
admin's role); ``GET /fake/race-next?playlist=&take=&add=`` has somebody replace a track of a playlist in Music
Assistant's own interface right before the next task runs; ``GET /fake/list?name=`` lists a playlist's
titles; ``GET /fake/ui-add?playlist=&track=`` adds a track to a playlist (by their names) as Music Assistant's own
interface would, with its event (the websocket server reads no body, so everything here is a ``GET``). Everything else
goes to ``FakeMA``.
"""

from __future__ import annotations

import argparse
import json
import queue
import threading
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from websockets.datastructures import Headers
from websockets.http11 import Request, Response
from websockets.sync.server import ServerConnection, serve

from . import FakeMA


def _answer(status: int, body: bytes, kind: str, extra: dict[str, str] | None = None) -> Response:
    # The websocket server closes the connection after a plain answer: said so, a client never sends the next
    # request on it (it would hang up).
    headers = Headers({"Content-Type": kind, "Content-Length": str(len(body)), "Connection": "close",
                       **(extra or {})})
    return Response(status, "OK" if status < 400 else "Error", headers, body)


def make_server(fake: FakeMA, port: int):  # type: ignore[no-untyped-def]
    def http(_connection: ServerConnection, request: Request) -> Response | None:
        parts = urlsplit(request.path)
        if parts.path == "/ws":
            return None
        params = {key: values[0] for key, values in parse_qs(parts.query).items()}
        if parts.path == "/health":
            return _answer(200, b"ok", "text/plain")
        if parts.path == "/fake/token":
            # ``role=user``: a new token of a user without the admin's role.
            token = fake.make_token(role="user") if params.get("role") == "user" else fake.token
            return _answer(200, json.dumps({"token": token}).encode(), "application/json")
        if parts.path == "/fake/race-next":
            # Right before the next task runs, somebody replaces a track of the playlist in Music Assistant's own
            # interface: ``take`` out, ``add`` in (by their names).
            wanted = {key: unquote(params.get(key, "")) for key in ("playlist", "take", "add")}
            with fake.lock:
                found = next((one for one in fake.playlists.values() if one.name == wanted["playlist"]), None)
                taken = next((one for one in fake.tracks.values() if fake.title(one) == wanted["take"]), None)
                added = next((one for one in fake.tracks.values() if fake.title(one) == wanted["add"]), None)
                ready = found is not None and taken is not None and added is not None
                if ready:
                    def meanwhile() -> None:
                        fake.ui_remove(found.id, taken.id)
                        fake.ui_add(found.id, added.id)

                    fake.before_task = meanwhile
            return _answer(200 if ready else 404, json.dumps({"set": ready}).encode(), "application/json")
        if parts.path == "/fake/list":
            name = unquote(params.get("name", ""))
            with fake.lock:
                found = next((one for one in fake.playlists.values() if one.name == name), None)
                titles = fake.titles(found) if found is not None else []
            return _answer(200 if found else 404, json.dumps({"tracks": titles}).encode(), "application/json")
        if parts.path == "/fake/ui-add":
            wanted = {"playlist": unquote(params.get("playlist", "")), "track": unquote(params.get("track", ""))}
            with fake.lock:
                found = next((one for one in fake.playlists.values() if one.name == wanted.get("playlist")), None)
                track = next((one for one in fake.tracks.values() if fake.title(one) == wanted.get("track")), None)
                if found is not None and track is not None:
                    fake.ui_add(found.id, track.id)
            done = found is not None and track is not None
            return _answer(200 if done else 404, json.dumps({"added": done}).encode(), "application/json")
        answer = fake.handle(httpx.Request("GET", f"http://127.0.0.1:{port}{request.path}"))
        extra = {name: value for name, value in answer.headers.items() if name in ("cache-control",)}
        return _answer(answer.status_code, answer.content, answer.headers.get("content-type", "text/plain"), extra)

    def socket(connection: ServerConnection) -> None:
        from app.services.music_assistant import Closed

        try:
            channel = fake.channel("ws")
        except Closed:
            connection.close()
            return
        stopped = threading.Event()

        def pump() -> None:
            while not stopped.is_set():
                try:
                    item = channel.inbox.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is None:
                    connection.close()
                    return
                connection.send(item)

        threading.Thread(target=pump, daemon=True).start()
        try:
            for message in connection:
                fake.receive(channel, message if isinstance(message, str) else message.decode())
        finally:
            stopped.set()
            channel.closed = True

    return serve(socket, "127.0.0.1", port, process_request=http, max_size=2**25)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=28624)
    arguments = parser.parse_args()
    with make_server(FakeMA(), arguments.port) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
