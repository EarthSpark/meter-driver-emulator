"""Event envelope and fan-out to /v1/events subscribers."""

import json
import queue
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    """One SSE message: a monotonic id, the spec event name, and its payload."""

    id: int
    type: str
    data: dict

    def envelope(self):
        """The JSON body carried on the data: line, `{type, data}` per the spec's Event schema."""
        return {"type": self.type, "data": self.data}

    def to_sse(self):
        """Render as one SSE message: id:, event: and data: lines ended by a blank line."""
        payload = json.dumps(self.envelope(), separators=(",", ":"))
        return f"id: {self.id}\nevent: {self.type}\ndata: {payload}\n\n"


class EventBus:
    """Delivers every published event to every subscriber's queue.

    Each subscriber owns a `queue.Queue`; `publish` copies the event into all
    of them under one lock, so ids are strictly increasing and every
    subscriber sees the same order. `close` puts `None` in each queue so a
    blocked reader wakes and ends its stream.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._next_id = 1
        self._subscribers = []
        self._published = 0
        self._closed = False

    @property
    def published(self):
        """Number of events published so far."""
        return self._published

    @property
    def subscriber_count(self):
        """Number of open subscriber queues."""
        with self._lock:
            return len(self._subscribers)

    def _allocate(self, event_type, data):
        event = Event(self._next_id, event_type, data)
        self._next_id += 1
        return event

    def subscribe(self, initial=None):
        """Create a subscriber queue. `initial` is `(type, data)` placed first in that queue only."""
        subscriber = queue.Queue()
        with self._lock:
            if initial is not None:
                subscriber.put(self._allocate(*initial))
            if self._closed:
                subscriber.put(None)
            else:
                self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber):
        """Stop delivering to a subscriber queue."""
        with self._lock:
            try:
                self._subscribers.remove(subscriber)
            except ValueError:
                pass

    def publish(self, event_type, data):
        """Deliver one event to every subscriber; returns the Event."""
        with self._lock:
            event = self._allocate(event_type, data)
            self._published += 1
            for subscriber in self._subscribers:
                subscriber.put(event)
        return event

    def close(self):
        """Wake every subscriber with a `None` sentinel and refuse new deliveries."""
        with self._lock:
            self._closed = True
            for subscriber in self._subscribers:
                subscriber.put(None)
            self._subscribers.clear()
