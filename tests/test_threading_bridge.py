"""ThreadingBridge: ordering, no drops, callback isolation, concurrency."""

import threading
import time

from core.threading_bridge import BridgeEvent, EventType, ThreadingBridge


def _ev(i, t=EventType.TRANSACTION):
    return BridgeEvent(event_type=t, data={"i": i})


def test_fifo_order_and_all_events_dispatched():
    b = ThreadingBridge()
    seen = []
    b.register_callback(EventType.TRANSACTION, lambda e: seen.append(e.data["i"]))
    for i in range(50):
        b.push_event(_ev(i))
    while b.process_pending_events():
        pass
    assert seen == list(range(50))


def test_no_events_are_dropped_beyond_old_limit(capsys):
    b = ThreadingBridge(max_queue_size=1000)       # legacy arg is ignored
    for i in range(5000):
        assert b.push_event(_ev(i)) is True
    assert b.pending_count() == 5000
    out = capsys.readouterr().out
    assert "events waiting for the main thread" in out
    # warnings are throttled: 200, 400, 800, 1600, 3200 → 5 lines
    assert out.count("WARNING") == 5
    seen = []
    b.register_callback(EventType.TRANSACTION, lambda e: seen.append(e.data["i"]))
    while b.process_pending_events(time_budget=None):
        pass
    assert seen == list(range(5000))


def test_time_budget_processes_at_least_one_and_stops():
    b = ThreadingBridge()
    calls = []

    def slow(e):
        calls.append(e.data["i"])
        time.sleep(0.03)
    b.register_callback(EventType.TRANSACTION, slow)
    for i in range(10):
        b.push_event(_ev(i))
    n = b.process_pending_events(time_budget=0.05)
    assert 1 <= n <= 3
    assert calls == list(range(n))
    n2 = b.process_pending_events(time_budget=0.0)    # budget exhausted → 1
    assert n2 == 1


def test_max_events_cap():
    b = ThreadingBridge()
    for i in range(10):
        b.push_event(_ev(i))
    assert b.process_pending_events(max_events=3) == 3
    assert b.pending_count() == 7


def test_callback_exception_does_not_stop_dispatch(capsys):
    b = ThreadingBridge()
    seen = []

    def boom(e):
        raise RuntimeError("bad callback")
    b.register_callback(EventType.TRANSACTION, boom)
    b.register_callback(EventType.TRANSACTION, lambda e: seen.append(e.data["i"]))
    for i in range(3):
        b.push_event(_ev(i))
    b.process_pending_events()
    assert seen == [0, 1, 2]
    assert "bad callback" in capsys.readouterr().out


def test_reentrant_processing_is_ignored():
    """A Timer firing while a callback runs a modal dialog must not
    dispatch later events first."""
    b = ThreadingBridge()
    order = []

    def first(e):
        order.append(("start", e.data["i"]))
        b.process_pending_events()          # re-entrant call
        order.append(("end", e.data["i"]))
    b.register_callback(EventType.TRANSACTION, first)
    b.push_event(_ev(0))
    b.push_event(_ev(1))
    b.process_pending_events()
    assert order == [("start", 0), ("end", 0), ("start", 1), ("end", 1)]


def test_concurrent_producers_keep_per_producer_order():
    b = ThreadingBridge()
    n_threads, per = 8, 2000

    def produce(t):
        for i in range(per):
            b.push_event(BridgeEvent(EventType.TRANSACTION, data={"t": t, "i": i}))
    threads = [threading.Thread(target=produce, args=(t,)) for t in range(n_threads)]
    seen = []
    b.register_callback(EventType.TRANSACTION, lambda e: seen.append((e.data["t"], e.data["i"])))
    for th in threads:
        th.start()
    # consume while producing
    while any(th.is_alive() for th in threads) or b.pending_count():
        b.process_pending_events(time_budget=None)
    for th in threads:
        th.join()
    assert len(seen) == n_threads * per
    for t in range(n_threads):
        assert [i for tt, i in seen if tt == t] == list(range(per))


def test_unregistered_event_types_are_consumed():
    b = ThreadingBridge()
    b.push_event(_ev(0, EventType.NEW_FILE))
    assert b.process_pending_events() == 1
    assert b.pending_count() == 0


def test_clear_queue_and_state_properties():
    b = ThreadingBridge()
    for i in range(5):
        b.push_event(_ev(i))
    b.clear_queue()
    assert b.pending_count() == 0
    b.connected = True
    b.filename = "x"
    b.status_message = "s"
    assert (b.connected, b.filename, b.status_message) == (True, "x", "s")
