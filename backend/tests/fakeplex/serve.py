"""The stand-in Plex over a socket, for the browser tests and the pictures of the settings: plex.tv, Plex's sign-in
page and the media server on one port of 127.0.0.1.

    python -m tests.fakeplex.serve --port 28622

nexmix is pointed at it with ``NEXMIX_PLEX_TV_URL=http://127.0.0.1:<port>`` and
``NEXMIX_PLEX_AUTH_URL=http://127.0.0.1:<port>/auth``. The sign-in page agrees at once (the browser test does what
the operator would do at Plex). ``POST /fake/slow`` makes every page of the library answer slowly, so that a picture
can be taken while the catalogue is being fetched; ``POST /fake/fast`` undoes it. ``POST /fake/reset`` takes the
agreement back, for the next sign-in. The converter sends its sound at a listening pace; ``GET /fake/sound`` says
which previews began and which sessions ended, ``POST /fake/analyse`` marks every track analysed.

The playlists of the account, as Plexamp would change them: ``GET /fake/playlists`` lists them (key, title, smart,
tracks), ``POST /fake/playlists/add?key=&track=``, ``/remove?key=&track=``, ``/rename?key=&title=``,
``/delete?key=``, ``/move?key=&track=&after=``, ``/make?title=&tracks=1,2``, ``/smart?title=&tracks=1,2`` change them,
``POST /fake/playlists/reset`` deletes them all. ``POST /fake/down?mode=503|401|drop`` makes every further request to
the media server fail that way, ``POST /fake/up`` ends it.
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx

from . import FakePlex, sound

PAGE = """<!doctype html><meta charset="utf-8"><title>Plex</title>
<body style="font:16px sans-serif;padding:40px">Signed in at the stand-in Plex. This window closes.
<script>fetch('/fake/approve', {method: 'POST'}).then(() => setTimeout(() => window.close(), 300))</script>"""


def make_handler(fake: FakePlex, state: dict[str, float]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def _answer(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/health":
                return self._send(200, b"ok", "text/plain")
            if path == "/auth":
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            if path == "/fake/approve":
                fake.approve()
                return self._send(204, b"", "text/plain")
            if path == "/fake/reset":
                fake.approved = False
                return self._send(204, b"", "text/plain")
            if path.startswith("/fake/playlists"):
                return self._playlists(path)
            if path in ("/fake/down", "/fake/up"):
                mode = parse_qs(urlsplit(self.path).query).get("mode", ["503"])[0]
                fake.fail_after = fake.library_requests if path == "/fake/down" else None
                fake.fail_mode = mode if mode in ("503", "401", "drop", "timeout") else "503"
                return self._send(204, b"", "text/plain")
            if path in ("/fake/slow", "/fake/fast"):
                state["delay"] = 1.2 if path == "/fake/slow" else 0.0
                return self._send(204, b"", "text/plain")
            if path == "/fake/sound":
                # What the converter was asked: the previews begun, and the sessions ended.
                body = json.dumps({"starts": fake.starts, "stops": fake.stops}).encode()
                return self._send(200, body, "application/json")
            if path == "/fake/analyse":
                # Every track counts as analysed from now on: ``nearest`` answers.
                for item in fake.items.values():
                    item.analysed = True
                return self._send(204, b"", "text/plain")
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            request = httpx.Request(self.command, f"http://{self.headers.get('host', '127.0.0.1')}{self.path}",
                                    headers=dict(self.headers.items()), content=body)
            if state.get("delay") and path.startswith("/library/sections/") and path.endswith("/all"):
                time.sleep(state["delay"])
            try:
                answer = fake.handle(request)
            except httpx.HTTPError:
                self.close_connection = True
                return None
            if path == sound.START and answer.status_code == 200:
                return self._pace(answer.content)
            return self._send(answer.status_code, answer.content, answer.headers.get("content-type", "text/plain"))

        def _pace(self, body: bytes) -> None:
            """The sound as a converter sends it: a head start, then a piece a moment, until the reader goes."""
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.end_headers()
            self.close_connection = True
            try:
                for start in range(0, len(body), 32768):
                    self.wfile.write(body[start:start + 32768])
                    self.wfile.flush()
                    if start >= 131072:
                        time.sleep(0.2)
            except OSError:
                return

        def _playlists(self, path: str) -> None:
            query = {key: values[0] for key, values in parse_qs(urlsplit(self.path).query).items()}
            store = fake.lists
            if path == "/fake/playlists":
                found = [{"key": one.key, "title": one.title, "smart": one.smart, "tracks": one.tracks()}
                         for one in store.lists.values()]
                return self._send(200, json.dumps(found).encode(), "application/json")
            action = path.rsplit("/", 1)[-1]
            key = int(query.get("key", "0") or 0)
            if action == "reset":
                for one in list(store.lists):
                    store.delete(one)
            elif action in ("make", "smart"):
                tracks = [int(track) for track in query.get("tracks", "").split(",") if track]
                made = store.make(query.get("title", "Plexamp"), tracks, smart=action == "smart")
                key = made.key
            elif key not in store.lists:
                return self._send(404, b"no such playlist", "text/plain")
            elif action == "add":
                store.add(key, [int(query["track"])])
            elif action == "remove":
                store.remove_track(key, int(query["track"]))
            elif action == "move":
                after = query.get("after")
                store.move_track(key, int(query["track"]), int(after) if after else None)
            elif action == "rename":
                store.rename(key, query.get("title", ""))
            elif action == "delete":
                store.delete(key)
            else:
                return self._send(404, b"no such action", "text/plain")
            fake.tick()
            store.settle()
            return self._send(200, json.dumps({"key": key}).encode(), "application/json")

        def _send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _answer
        do_POST = _answer
        do_PUT = _answer
        do_DELETE = _answer

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=28622)
    arguments = parser.parse_args()
    fake = FakePlex()
    own = f"http://127.0.0.1:{arguments.port}"
    fake.connections = [{"uri": own, "local": True, "relay": False}]
    # The shared server answers nowhere: port 1 of this machine refuses at once, and no name is looked up.
    fake.shared_connections = [{"uri": "https://127.0.0.1:1", "local": False, "relay": False}]
    server = ThreadingHTTPServer(("127.0.0.1", arguments.port), make_handler(fake, {"delay": 0.0}))
    server.serve_forever()


if __name__ == "__main__":
    main()
