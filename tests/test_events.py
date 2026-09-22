"""The /v1/events stream: framing, initial message, fan-out, keep-alive, back-pressure."""

import threading
import time

from conftest import TEST_KEEPALIVE_SECONDS, Comment

from meter_driver_emulator.events import QUEUE_MAXSIZE, EventBus
from meter_driver_emulator.server import KEEPALIVE_SECONDS, Emulator


def test_stream_headers_and_first_message(subscribe, validator):
    subscriber = subscribe(consume_initial=False)
    assert subscriber.status == 200
    assert subscriber.headers["Content-Type"] == "text/event-stream"
    assert subscriber.headers["Cache-Control"] == "no-cache"

    first = subscriber.next()
    assert first is not None
    assert first.event == "gateway_status"
    assert [line.split(":", 1)[0] for line in first.lines] == ["id", "event", "data"]
    assert set(first.data) == {"type", "data"}
    assert first.data["type"] == "gateway_status"
    validator.assert_valid("GatewayStatus", first.payload)
    assert first.payload["gateway_type"] == "emulator"
    assert first.payload["gateway_firmware_raw"] == "emulator"


def test_two_subscribers_receive_the_same_event(client, subscribe):
    first = subscribe()
    second = subscribe()
    assert first.initial.event == second.initial.event == "gateway_status"

    client.register(65276, mac=65276)
    from_first = first.expect("node_registered")
    from_second = second.expect("node_registered")
    assert from_first.id == from_second.id
    assert (
        from_first.data
        == from_second.data
        == {
            "type": "node_registered",
            "data": {"node_id": 65276, "source_type": 1},
        }
    )
    first.expect("node_firmware_version_changed")
    second.expect("node_firmware_version_changed")
    first.assert_silent(0.2)
    second.assert_silent(0.2)


def test_ids_increase_across_events(client, subscriber):
    client.register(1)
    client.register(2)
    client.delete("/v1/nodes/1")
    ids = [subscriber.expect(t).id for t in ("node_registered", "node_firmware_version_changed") * 2]
    ids.append(subscriber.expect("node_unregistered").id)
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    assert ids[0] > subscriber.initial.id


def test_keep_alive_comment_after_idle(subscriber):
    started = time.monotonic()
    item = subscriber.next(timeout=TEST_KEEPALIVE_SECONDS * 4, comments=True)
    assert isinstance(item, Comment), f"expected a keep-alive comment, got {item}"
    assert item.text == ": keep-alive"
    assert time.monotonic() - started >= TEST_KEEPALIVE_SECONDS * 0.9


def test_default_keep_alive_interval_is_fifteen_seconds():
    assert KEEPALIVE_SECONDS == 15.0
    emulator = Emulator(bind=("127.0.0.1", 0))
    try:
        assert emulator.keepalive_seconds == 15.0
    finally:
        emulator.shutdown()


def test_disconnected_subscriber_is_removed(client, subscribe, emulator):
    subscriber = subscribe()
    assert emulator.bus.subscriber_count == 1
    subscriber.close()
    # The handler notices the closed socket on its next write.
    client.register(1)
    deadline = time.monotonic() + 3
    while emulator.bus.subscriber_count and time.monotonic() < deadline:
        client.register(1)
        time.sleep(0.05)
    assert emulator.bus.subscriber_count == 0


def test_shutdown_ends_open_streams(client, subscriber):
    client.post("/v1/shutdown")
    assert subscriber.closed.wait(timeout=5)


def test_events_of_one_request_are_contiguous_under_concurrency(client, subscriber):
    """Mutation and publication are one atomic step, so pairs never interleave."""
    client.init(period=1)
    subscriber.expect_sequence("driver_configuration_applied", "gateway_status")
    node_ids = list(range(100, 130))
    threads = [threading.Thread(target=client.register, args=(node_id,)) for node_id in node_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    messages = subscriber.collect(1.6)
    registered = [m for m in messages if m.event == "node_registered"]
    assert sorted(m.payload["node_id"] for m in registered) == node_ids
    for index, message in enumerate(messages):
        if message.event == "node_registered":
            follower = messages[index + 1]
            assert follower.event == "node_firmware_version_changed"
            assert follower.payload["node_id"] == message.payload["node_id"]
    # The heartbeat cycle ran in the same window and its own events are
    # contiguous too: readings, then statistics, then status.
    cycle_start = next(i for i, m in enumerate(messages) if m.event == "electrical_meter_reading")
    cycle = [m.event for m in messages[cycle_start:]]
    statistics_at = cycle.index("heartbeat_statistics")
    assert set(cycle[:statistics_at]) == {"electrical_meter_reading"}
    assert cycle[statistics_at + 1] == "gateway_status"


def test_bus_drops_a_subscriber_that_falls_behind():
    assert QUEUE_MAXSIZE == 1000
    bus = EventBus(queue_maxsize=2)
    slow = bus.subscribe()
    fast = bus.subscribe()
    for index in range(3):
        bus.publish("node_unregistered", {"node_id": index})
        fast.get_nowait()
    assert bus.dropped == 1
    assert bus.subscriber_count == 1
    # The slow queue holds what fit, then the close sentinel.
    drained = []
    while not slow.empty():
        drained.append(slow.get_nowait())
    assert drained[-1] is None
    assert all(event is not None for event in drained[:-1])
    # A subscriber created after the bus closed ends immediately.
    bus.close()
    assert fast.get_nowait() is None
    late = bus.subscribe()
    assert late.get_nowait() is None
