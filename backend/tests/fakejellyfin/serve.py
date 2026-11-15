"""The stand-in Jellyfin over a socket, for the browser tests: one port of 127.0.0.1.

    python -m tests.fakejellyfin.serve --port 28623

nexmix is pointed at it by its address (``http://127.0.0.1:<port>``), as an operator would type it. ``POST
/fake/confirm`` confirms the newest Quick Connect code as the made-up administrator, as the operator would in
Jellyfin's profile; ``GET /fake/sound`` says which previews began; ``POST /fake/list-add`` ``{"playlist", "track"}`` adds
a track to a playlist (by their names) as a Jellyfin app would, ``GET /fake/list?name=`` lists a playlist's tracks.
Everything else goes to ``FakeJellyfin.handle``.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from . import FakeJellyfin


def make_handler(fake: FakeJellyfin) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def _answer(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/health":
                return self._send(200, b"ok", "text/plain")
            if path == "/fake/confirm":
                with fake.lock:
                    code = fake.latest_code()
                    done = bool(code) and fake.confirm(code or "")
                return self._send(200 if done else 404, json.dumps({"confirmed": done}).encode(), "application/json")
            if path == "/fake/sound":
                return self._send(200, json.dumps({"starts": fake.starts}).encode(), "application/json")
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            if path == "/fake/list-add":
                # Somebody adds a track to a playlist in Jellyfin (by their names), as from a Jellyfin app.
                wanted = json.loads(body or b"{}")
                with fake.lock:
                    found = next((one for one in fake.lists.values() if one.name == wanted.get("playlist")), None)
                    track = next((ident for ident, one in fake.tracks.items() if one.name == wanted.get("track")), None)
                    if found is not None and track is not None:
                        fake.list_add(found.id, [track])
                done = found is not None and track is not None
                return self._send(200 if done else 404, json.dumps({"added": done}).encode(), "application/json")
            if path == "/fake/list":
                name = self.path.split("name=", 1)[1] if "name=" in self.path else ""
                from urllib.parse import unquote

                with fake.lock:
                    found = next((one for one in fake.lists.values() if one.name == unquote(name)), None)
                    names = [fake.tracks[ident].name for ident in found.entries if ident in fake.tracks] if found else []
                return self._send(200 if found else 404, json.dumps({"tracks": names}).encode(), "application/json")
            request = httpx.Request(self.command, f"http://{self.headers.get('host', '127.0.0.1')}{self.path}",
                                    headers=dict(self.headers.items()), content=body)
            try:
                answer = fake.handle(request)
            except httpx.HTTPError:
                self.close_connection = True
                return None
            extra = {name: value for name, value in answer.headers.items()
                     if name in ("cache-control", "accept-ranges")}
            return self._send(answer.status_code, answer.content, answer.headers.get("content-type", "text/plain"),
                              extra)

        def _send(self, status: int, body: bytes, kind: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        do_GET = _answer
        do_POST = _answer
        do_PUT = _answer
        do_DELETE = _answer

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=28623)
    arguments = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", arguments.port), make_handler(FakeJellyfin()))
    server.serve_forever()


if __name__ == "__main__":
    main()
