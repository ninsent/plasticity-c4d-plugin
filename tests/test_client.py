"""
PlasticityClient against a real local WebSocket server (bundled websockets).
Covers connect/handshake/list/subscribe/disconnect, server-side closes,
errors, non-blocking sends and thread hygiene.
"""

import socket
import struct
import time

import pytest

from plasticity_bridge.core.client import PlasticityClient
from plasticity_bridge.core.protocol import MessageType
from plasticity_bridge.core.threading_bridge import EventType, ThreadingBridge
from fake_server import FakePlasticityServer, default_responder
import wire as W

pytestmark = pytest.mark.usefixtures("no_thread_leaks")


class Recorder:
    def __init__(self, bridge):
        self.bridge = bridge
        self.events = []
        for et in EventType:
            bridge.register_callback(et, self.events.append)

    def types(self):
        return [e.event_type for e in self.events]

    def pump(self, until, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.bridge.process_pending_events(time_budget=None)
            if until():
                return True
            time.sleep(0.005)
        self.bridge.process_pending_events(time_budget=None)
        return until()

    def has(self, et):
        return et in self.types()

    def last(self, et):
        return [e for e in self.events if e.event_type == et][-1]


def make_client():
    bridge = ThreadingBridge()
    rec = Recorder(bridge)
    return PlasticityClient(handler=None, bridge=bridge), bridge, rec


def _shutdown(client):
    client.shutdown(timeout=5.0)


def test_connect_handshake_list_subscribe_disconnect():
    client, bridge, rec = make_client()
    with FakePlasticityServer() as srv:
        client.connect(srv.address)
        assert rec.pump(lambda: rec.has(EventType.HANDSHAKE_RESPONSE))
        assert bridge.connected and client.connected
        assert client.supports(MessageType.PUT_SOME_1)
        assert srv.received_types()[0] == 100            # handshake first

        called = []
        mid = client.list_all(on_complete=called.append)
        assert mid == 2                                  # handshake used id 1
        assert rec.pump(lambda: rec.has(EventType.LIST_RESPONSE))
        data = rec.last(EventType.LIST_RESPONSE).data
        assert data["filename"] == "file.plasticity" and data["message_id"] == 2
        assert bridge.filename == "file.plasticity"
        assert client.on_list_complete == called.append

        assert client.subscribe_all() == 3 and client.subscribed
        assert rec.pump(lambda: 23 in srv.received_types())
        client.unsubscribe()
        assert rec.pump(lambda: 25 in srv.received_types())
        assert not client.subscribed

        client.disconnect()
        assert not bridge.connected
        assert rec.pump(lambda: rec.has(EventType.DISCONNECTED))
        assert client.wait_closed(5.0)
    _shutdown(client)


def test_disconnect_is_non_blocking():
    client, bridge, rec = make_client()
    with FakePlasticityServer() as srv:
        client.connect(srv.address)
        assert rec.pump(lambda: bridge.connected)
        t0 = time.monotonic()
        client.disconnect()
        assert time.monotonic() - t0 < 0.1
        assert client.wait_closed(5.0)
    _shutdown(client)


def test_calls_do_not_block_on_a_stalled_server():
    """The server never reads after the handshake: sends must return
    immediately regardless (the 1.1.x client waited up to 30 s)."""
    stalled = []

    def responder(data):
        if struct.unpack_from("<I", data)[0] == 100:
            return default_responder(data)
        stalled.append(1)
        time.sleep(0.5)                   # blocks the server loop
        return []

    client, bridge, rec = make_client()
    with FakePlasticityServer(responder) as srv:
        client.connect(srv.address)
        assert rec.pump(lambda: rec.has(EventType.HANDSHAKE_RESPONSE))
        big = [{"c4d_id": "a", "name": "a", "parent_c4d_collection_id": "",
                "options": 1, "positions": [0.0] * 300000,
                "indices": list(range(99999)) + [0, 1, 2][:0] or list(range(99999)),
                "sizes": [3] * 33333}]
        t0 = time.monotonic()
        for _ in range(3):
            client.list_all()
        client.put_some("x.c4d", [], big)
        assert time.monotonic() - t0 < 0.5
        client.disconnect()
        assert client.wait_closed(5.0)
    _shutdown(client)


def test_send_when_not_connected_reports_operation_failed():
    client, bridge, rec = make_client()
    bridge.connected = True                        # stale flag, no session
    assert client.list_all() is None
    rec.pump(lambda: rec.has(EventType.OPERATION_FAILED))
    ev = rec.last(EventType.OPERATION_FAILED)
    assert ev.data["operation"] == MessageType.LIST_ALL_1
    assert "not connected" in ev.error_message


def test_encode_error_is_reported_not_raised():
    client, bridge, rec = make_client()
    with FakePlasticityServer() as srv:
        client.connect(srv.address)
        assert rec.pump(lambda: rec.has(EventType.HANDSHAKE_RESPONSE))
        bad = [{"c4d_id": "a", "name": "a", "parent_c4d_collection_id": "",
                "options": 1, "positions": [0.0, 1.0], "indices": [],
                "sizes": []}]
        assert client.put_some("x", [], bad) is None
        assert rec.pump(lambda: rec.has(EventType.OPERATION_FAILED))
        assert rec.last(EventType.OPERATION_FAILED).data["operation"] == MessageType.PUT_SOME_1
        client.disconnect()
    _shutdown(client)


def test_server_close_emits_disconnected_and_cleans_up():
    client, bridge, rec = make_client()
    with FakePlasticityServer() as srv:
        client.connect(srv.address)
        assert rec.pump(lambda: rec.has(EventType.HANDSHAKE_RESPONSE))
        client.subscribed = True
        srv.drop_clients()
        assert rec.pump(lambda: rec.has(EventType.DISCONNECTED))
        assert not bridge.connected and not client.subscribed
        assert client.supported_messages == set()
        assert rec.pump(lambda: not client.connecting)
    _shutdown(client)


def test_connection_refused_reports_error():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                      # nothing listens here
    client, bridge, rec = make_client()
    client.connect(f"127.0.0.1:{port}")
    assert rec.pump(lambda: rec.has(EventType.CONNECTION_ERROR), timeout=10)
    assert rec.pump(lambda: rec.has(EventType.DISCONNECTED))
    assert not bridge.connected
    assert "Connection failed" in bridge.status_message or \
        "Connection error" in bridge.status_message
    _shutdown(client)


def test_double_connect_is_refused():
    client, bridge, rec = make_client()
    with FakePlasticityServer() as srv:
        client.connect(srv.address)
        client.connect(srv.address)                # still connecting
        assert rec.pump(lambda: rec.has(EventType.HANDSHAKE_RESPONSE))
        time.sleep(0.1)
        assert srv.connections == 1
        assert rec.types().count(EventType.CONNECTED) == 1
        client.disconnect()
    _shutdown(client)


def test_reconnect_after_disconnect_is_not_clobbered():
    """The old session finishing must not reset the new one's state."""
    client, bridge, rec = make_client()
    with FakePlasticityServer() as srv:
        client.connect(srv.address)
        assert rec.pump(lambda: bridge.connected)
        client.disconnect()
        client.connect(srv.address)                # immediately
        assert rec.pump(lambda: bridge.connected and srv.connections == 2)
        assert client.wait_closed(5.0)             # old thread gone
        time.sleep(0.1)
        bridge.process_pending_events(time_budget=None)
        assert bridge.connected
        assert client.list_all() is not None
        assert rec.pump(lambda: rec.has(EventType.LIST_RESPONSE))
        client.disconnect()
    _shutdown(client)


def test_disconnect_while_connecting():
    client, bridge, rec = make_client()
    # A listening socket that never completes the WebSocket handshake.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        client.connect(f"127.0.0.1:{s.getsockname()[1]}")
        time.sleep(0.1)
        assert client.connecting and not bridge.connected
        client.disconnect()
        assert client.wait_closed(5.0)
        assert not client.connecting
        assert rec.pump(lambda: rec.has(EventType.DISCONNECTED))
        assert not rec.has(EventType.CONNECTION_ERROR)
    finally:
        s.close()
    _shutdown(client)


def test_unreadable_list_response_emits_operation_failed():
    def responder(data):
        t, mid = struct.unpack_from("<II", data)
        if t == 100:
            return default_responder(data)
        if t == 20:
            full = W.list_response("f", [W.item(1, [W.mesh_object(1, "x", [0.0] * 9, [0, 1, 2])])],
                                   message_id=mid)
            return [full[:-7]]                     # truncated
        return []
    client, bridge, rec = make_client()
    with FakePlasticityServer(responder) as srv:
        client.connect(srv.address)
        assert rec.pump(lambda: rec.has(EventType.HANDSHAKE_RESPONSE))
        client.list_all()
        assert rec.pump(lambda: rec.has(EventType.OPERATION_FAILED))
        ev = rec.last(EventType.OPERATION_FAILED)
        assert ev.data["operation"] == MessageType.LIST_ALL_1
        assert not rec.has(EventType.LIST_RESPONSE)
        assert bridge.connected                    # connection survives
        client.disconnect()
    _shutdown(client)


def test_list_error_code_is_delivered_as_list_response_with_error():
    def responder(data):
        t, mid = struct.unpack_from("<II", data)
        if t == 100:
            return default_responder(data)
        return [W.list_response("", [], message_id=mid, code=500)]
    client, bridge, rec = make_client()
    with FakePlasticityServer(responder) as srv:
        client.connect(srv.address)
        assert rec.pump(lambda: rec.has(EventType.HANDSHAKE_RESPONSE))
        client.list_all()
        assert rec.pump(lambda: rec.has(EventType.LIST_RESPONSE))
        assert rec.last(EventType.LIST_RESPONSE).data["error"] == 500
        assert bridge.filename is None             # not overwritten with ""
        client.disconnect()
    _shutdown(client)


def test_pushed_transactions_and_notifications_arrive_in_order():
    client, bridge, rec = make_client()
    with FakePlasticityServer() as srv:
        client.connect(srv.address)
        assert rec.pump(lambda: rec.has(EventType.HANDSHAKE_RESPONSE))
        for i in range(20):
            srv.push(W.transaction("f", [W.delete_item([i])], version=i))
        srv.push(W.new_version("f", 99))
        srv.push(W.new_file("g"))
        srv.push(b"\x09\x09\x09\x09junk")          # unknown type: ignored
        assert rec.pump(lambda: rec.has(EventType.NEW_FILE))
        txs = [e.data["version"] for e in rec.events if e.event_type == EventType.TRANSACTION]
        assert txs == list(range(20))
        assert bridge.filename == "g"
        client.disconnect()
    _shutdown(client)


def test_list_without_callback_keeps_pending_callback():
    client, bridge, rec = make_client()
    with FakePlasticityServer() as srv:
        client.connect(srv.address)
        assert rec.pump(lambda: rec.has(EventType.HANDSHAKE_RESPONSE))
        cb = object()
        client.list_all(on_complete=cb)
        client.list_visible()                      # auto-refresh: no callback
        assert client.on_list_complete is cb
        client.disconnect()
        assert client.on_list_complete is None
    _shutdown(client)


def test_shutdown_joins_threads():
    client, bridge, rec = make_client()
    with FakePlasticityServer() as srv:
        client.connect(srv.address)
        assert rec.pump(lambda: bridge.connected)
        client.shutdown(timeout=5.0)
        assert not client.connecting
        assert client.wait_closed(0)


def test_receive_error_ends_session_instead_of_spinning(monkeypatch):
    """F26: a non-close exception from recv() used to be retried forever."""
    import plasticity_bridge._vendor.websockets.legacy.protocol as proto
    calls = []

    async def broken_recv(self):
        calls.append(1)
        raise RuntimeError("boom")
    client, bridge, rec = make_client()
    with FakePlasticityServer() as srv:
        client.connect(srv.address)
        assert rec.pump(lambda: rec.has(EventType.HANDSHAKE_RESPONSE))
        monkeypatch.setattr(proto.WebSocketCommonProtocol, "recv", broken_recv)
        srv.push(W.new_file("x"))      # wake the pending recv
        assert rec.pump(lambda: rec.has(EventType.DISCONNECTED))
        assert rec.has(EventType.CONNECTION_ERROR)
        assert len(calls) <= 2
    _shutdown(client)


def test_disconnect_racing_with_connect_never_leaves_stale_connected(monkeypatch):
    """Disconnect lands right after the socket opened but before the session
    is marked connected: the bridge must end up disconnected, with no
    CONNECTED event after DISCONNECTED."""
    import plasticity_bridge.core.client as C
    client, bridge, rec = make_client()
    real_proxy = C.weakref.proxy

    def proxy_then_disconnect(obj):
        p = real_proxy(obj)
        client.disconnect()          # the race window
        return p
    monkeypatch.setattr(C.weakref, "proxy", proxy_then_disconnect)
    with FakePlasticityServer() as srv:
        client.connect(srv.address)
        assert client.wait_closed(5.0) or rec.pump(lambda: not client.connecting)
        rec.pump(lambda: rec.has(EventType.DISCONNECTED))
        time.sleep(0.1)
        bridge.process_pending_events(time_budget=None)
        assert not bridge.connected
        types = rec.types()
        if EventType.CONNECTED in types:
            assert types.index(EventType.CONNECTED) < types.index(EventType.DISCONNECTED)
    _shutdown(client)
