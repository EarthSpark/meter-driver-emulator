"""Background heartbeat: one reading per registered meter every period, then cycle statistics."""

import logging
import threading
import time

log = logging.getLogger(__name__)


class Heartbeat:
    """Runs heartbeat cycles on one thread.

    Each cycle waits `state.heartbeat_period_duration` seconds, then publishes
    per-meter readings, `heartbeat_statistics` and `gateway_status`. A period
    of 0 idles. `restart` interrupts the current wait so the first reading
    after init arrives one full period later; `stop` ends the thread.
    """

    def __init__(self, state, bus):
        self.state = state
        self.bus = bus
        self._wake = threading.Event()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)

    def start(self):
        """Start the cycle thread."""
        self._thread.start()

    def restart(self):
        """Abandon the current wait and begin a new cycle with the state's current period."""
        self._wake.set()

    def stop(self):
        """End the thread; returns once it has exited."""
        self._stop = True
        self._wake.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join()

    @property
    def running(self):
        """Whether the cycle thread is alive."""
        return self._thread.is_alive()

    def _run(self):
        while not self._stop:
            period = self.state.heartbeat_period_duration
            period_start = int(time.time())
            # wait() returns True only when restart() or stop() set the event;
            # a None timeout idles until one of them does. The timeout is
            # clamped because Event.wait raises OverflowError above TIMEOUT_MAX.
            timeout = min(period, threading.TIMEOUT_MAX) if period > 0 else None
            interrupted = self._wake.wait(timeout)
            if interrupted:
                self._wake.clear()
                continue
            # The wall clock may step backwards; a period must not.
            period_end = max(period_start, int(time.time()))
            try:
                # The state lock is held across the cycle and its publication so
                # request handlers cannot interleave their own events.
                with self.state.lock:
                    for event_type, data in self.state.heartbeat_cycle(period_start, period_end, period):
                        self.bus.publish(event_type, data)
            except Exception:
                log.exception("heartbeat cycle failed; continuing")
            else:
                log.debug("heartbeat cycle complete at %d", period_end)
