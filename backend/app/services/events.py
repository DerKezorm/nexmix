"""Live events, sent to the browsers that have a playlist open (Server-Sent Events, ``routers/playlists.py``).

One queue per open stream, subscribed to topics (``playlist:<id>``). Publishing never blocks and never waits: a stream
whose queue is full misses events, and the browser notices by the version of the playlist (each event carries it) and
loads the playlist anew. An event says what changed; it is never the only way to the truth.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from typing import Any

QUEUE_SIZE = 200


@dataclass(eq=False)
class Subscriber:
    topics: frozenset[str]
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[str] = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_SIZE))


class Hub:
    def __init__(self) -> None:
        self._subscribers: set[Subscriber] = set()
        self._lock = threading.Lock()

    def subscribe(self, topics: set[str]) -> Subscriber:
        """In the stream's own event loop: the queue belongs to it."""
        subscriber = Subscriber(topics=frozenset(topics), loop=asyncio.get_running_loop())
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def topics(self) -> set[str]:
        """The topics somebody listens to right now (the playlists open in a browser)."""
        with self._lock:
            return {topic for subscriber in self._subscribers for topic in subscriber.topics}

    @property
    def connections(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def publish(self, topic: str, event: str, payload: Any = None) -> None:
        """From any thread. ``asyncio.Queue`` is not safe across threads: a synchronous route runs in a worker
        thread, so the message is handed to each stream's loop (else it would wait until something else woke it)."""
        message = f"event: {event}\ndata: {json.dumps(payload if payload is not None else {}, default=str)}\n\n"
        with self._lock:
            chosen = [subscriber for subscriber in self._subscribers if topic in subscriber.topics]
        for subscriber in chosen:
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is subscriber.loop:
                _put(subscriber, message)
            elif not subscriber.loop.is_closed():
                subscriber.loop.call_soon_threadsafe(_put, subscriber, message)


def _put(subscriber: Subscriber, message: str) -> None:
    try:
        subscriber.queue.put_nowait(message)
    except asyncio.QueueFull:
        pass


hub = Hub()


def playlist_topic(playlist_id: int) -> str:
    return f"playlist:{int(playlist_id)}"
