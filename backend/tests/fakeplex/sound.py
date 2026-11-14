"""The stand-in's sound and sonic analysis: Plex's converter (``universal/start.mp3`` and ``universal/stop``) and
``/library/metadata/<id>/nearest``.

The sound is silence in real MPEG frames (a browser plays it); without ``X-Plex-Platform`` the converter says 400, as a
real server did on 13.11.2026. ``nearest`` answers in the shape Plex's own clients
read (a ``MediaContainer`` of tracks, each with a ``distance``); for a track that is not analysed it gives what a real
server gave on 13.11.2026: an empty container with ``size`` 0.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from . import FakePlex

#: One MPEG-1 layer III frame of silence, 128 kbit/s, 44.1 kHz: a header and nothing else.
FRAME = bytes([0xFF, 0xFB, 0x90, 0x64]) + bytes(413)
START = "/music/:/transcode/universal/start.mp3"
STOP = "/music/:/transcode/universal/stop"


def silence(size: int) -> bytes:
    frames = -(-size // len(FRAME))
    return (FRAME * frames)[:size]


def handles(path: str) -> bool:
    return path in (START, STOP) or (path.startswith("/library/metadata/") and path.endswith("/nearest"))


def answer(fake: FakePlex, path: str, params: dict[str, str], headers: httpx.Headers) -> httpx.Response:
    if path == STOP:
        fake.stops.append(params.get("session", ""))
        return httpx.Response(200, content=b"", headers={"content-type": "text/plain"})
    if path == START:
        fake.starts.append(dict(params))
        # A real server picks the converter's profile by the platform and refuses without one (measured: 400).
        if not headers.get("x-plex-platform"):
            return httpx.Response(400, text="<html>400</html>", headers={"content-type": "text/html"})
        key = params.get("path", "").rsplit("/", 1)[-1]
        if not key.isdigit() or int(key) not in fake.items:
            return httpx.Response(404, text="<html>404</html>", headers={"content-type": "text/html"})
        if fake.sound_queue:
            planned = fake.sound_queue.pop(0)
            if isinstance(planned, Exception):
                raise planned
            status, body = planned
            kind = "audio/mpeg" if status == 200 else "text/html"
            return httpx.Response(status, content=body, headers={"content-type": kind})
        if fake.sound_status != 200:
            return httpx.Response(fake.sound_status, text="<html>no</html>", headers={"content-type": "text/html"})
        content: Any = fake.sound_stream() if fake.sound_stream is not None else silence(fake.sound_bytes)
        return httpx.Response(200, content=content, headers={"content-type": "audio/mpeg"})
    key = int(path.split("/")[3])
    item = fake.items.get(key)
    if item is None:
        return httpx.Response(404, text="<html>404</html>", headers={"content-type": "text/html"})
    limit = int(params.get("limit", "50"))
    farthest = float(params.get("maxDistance", "1"))
    found: list[dict[str, Any]] = []
    if item.analysed:
        near = fake.near.get(key)
        if near is None:
            near = [(other.key, round(abs(other.key - key) / 100, 4)) for other in fake.items.values()
                    if other.kind == item.kind and other.analysed and other.key != key]
        for other, distance in sorted(near, key=lambda pair: (pair[1], pair[0])):
            if distance <= farthest and other in fake.items:
                found.append({**fake.entry(fake.items[other]), "distance": distance})
    found = found[:limit]
    container: dict[str, Any] = {"identifier": "com.plexapp.plugins.library", "mediaTagPrefix": "/system/bundle/media/flags/",
                                 "mediaTagVersion": 1790000000, "size": len(found)}
    if found:
        container["Metadata"] = found
    return httpx.Response(200, json={"MediaContainer": container})
