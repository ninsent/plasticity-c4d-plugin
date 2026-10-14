"""Dialog busy-state handling: every operation that sets _busy has an exit."""

import c4d
import pytest

from core.protocol import MessageType
from core.threading_bridge import BridgeEvent, EventType, ThreadingBridge
from dialogs.main_dialog import IDS, PlasticityDialog


class FakeClient:
    def __init__(self, bridge, send_ok=True):
        self.bridge = bridge
        self.connecting = False
        self.send_ok = send_ok
        self.calls = []
        self.on_list_complete = None
        self.supported_messages = set()

    @property
    def connected(self):
        return self.bridge.connected

    def supports(self, mt):
        return mt.value in self.supported_messages

    def _send(self, name, *a, **k):
        self.calls.append(name)
        return 1 if self.send_ok else None

    def list_all(self, on_complete=None):
        if on_complete is not None:
            self.on_list_complete = on_complete
        return self._send("list_all")

    def list_visible(self, on_complete=None):
        return self._send("list_visible")

    def refacet_some(self, **kw):
        return self._send("refacet_some")

    def connect(self, server):
        self.calls.append(("connect", server))

    def disconnect(self):
        self.calls.append("disconnect")


class FakeHandler:
    _items = {}

    def get_selected_plasticity_ids(self, doc):
        return [("f", 1)]


@pytest.fixture
def dlg(doc):
    bridge = ThreadingBridge()
    bridge.connected = True
    client = FakeClient(bridge)
    d = PlasticityDialog(client, FakeHandler(), bridge)
    d.InitValues()
    return d


def _tick(d):
    d.Timer(None)


@pytest.mark.parametrize("event_type", [
    EventType.LIST_RESPONSE, EventType.REFACET_RESPONSE,
    EventType.PUT_SOME_RESPONSE, EventType.OPERATION_FAILED,
    EventType.DISCONNECTED, EventType.CONNECTION_ERROR,
])
def test_busy_cleared_by_every_terminal_event(dlg, event_type):
    dlg.Command(IDS.BTN_REFRESH, None)
    assert dlg._busy
    dlg.bridge.push_event(BridgeEvent(event_type, data={"error": 500},
                                      error_message="x"))
    _tick(dlg)
    assert not dlg._busy


def test_refresh_send_failure_clears_busy_immediately(dlg):
    dlg.client.send_ok = False
    dlg.Command(IDS.BTN_REFRESH, None)
    assert not dlg._busy


def test_busy_times_out(dlg, monkeypatch):
    dlg.Command(IDS.BTN_REFRESH, None)
    assert dlg._busy
    dlg._busy_since -= dlg.BUSY_TIMEOUT + 1
    _tick(dlg)
    assert not dlg._busy
    assert "No response" in dlg.bridge.status_message


def test_refresh_ignored_while_busy(dlg):
    dlg.Command(IDS.BTN_REFRESH, None)
    dlg.Command(IDS.BTN_REFRESH, None)
    assert dlg.client.calls.count("list_all") == 1


def test_refacet_sets_and_clears_busy(dlg):
    dlg.Command(IDS.BTN_REFACET, None)
    assert dlg._busy and "refacet_some" in dlg.client.calls
    dlg.bridge.push_event(BridgeEvent(EventType.REFACET_RESPONSE, data={}))
    _tick(dlg)
    assert not dlg._busy


def test_auto_refresh_on_new_version_only_with_live_link(dlg):
    dlg.bridge.push_event(BridgeEvent(EventType.NEW_VERSION, data={"filename": "f"}))
    _tick(dlg)
    assert "list_all" not in dlg.client.calls
    dlg.SetBool(IDS.CHK_LIVELINK, True)
    dlg.bridge.push_event(BridgeEvent(EventType.NEW_FILE, data={"filename": "g"}))
    _tick(dlg)
    assert dlg.client.calls.count("list_all") == 1 and dlg._busy


def test_connect_ignored_while_connecting(dlg):
    dlg.bridge.connected = False
    dlg.client.connecting = True
    dlg.Command(IDS.BTN_CONNECT, None)
    assert not any(isinstance(c, tuple) for c in dlg.client.calls)


def test_ui_state_is_only_reapplied_on_change(dlg):
    _tick(dlg)
    dlg._enabled.clear()
    _tick(dlg)
    assert dlg._enabled == {}                      # nothing re-applied
    dlg.bridge.status_message = "changed"
    _tick(dlg)
    assert dlg._enabled                            # re-applied once
    assert dlg.GetString(IDS.LBL_STATUS) == "Status: changed"


def test_destroy_window_disconnects_and_stops_timer(dlg):
    dlg.Command(IDS.BTN_REFRESH, None)
    dlg.DestroyWindow()
    assert dlg._timer == 0 and "disconnect" in dlg.client.calls
    assert not dlg._busy


def test_refresh_callback_uploads_outbox_when_supported(dlg):
    uploads = []
    dlg.client.supported_messages = {MessageType.PUT_SOME_1.value}
    dlg.client.put_some = lambda fn, g, i: uploads.append((fn, g, i)) or 1
    dlg.handler.get_outbox_data = lambda fn, ov: {"groups": [], "items": [{"x": 1}]}
    dlg.Command(IDS.BTN_REFRESH, None)
    dlg.client.on_list_complete("file")
    assert uploads and uploads[0][0] == c4d.documents.GetActiveDocument().GetDocumentName()
