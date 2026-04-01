"""
WebSocket client for connecting to Plasticity server.

Runs asyncio event loop in a background thread. All parsed messages
are dispatched to the main thread via the ThreadingBridge.
Architecture matches the Blender addon: only the listen loop calls recv().
"""

import asyncio
import threading
import struct
import weakref
from typing import Optional, List
from concurrent.futures import Future

try:
    import websockets.client as ws_client
    from websockets.exceptions import ConnectionClosed
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    print("[Plasticity] Warning: websockets library not found")

from modules.protocol import (
    MessageType, MessageParser, FacetShapeType,
    encode_list_all, encode_list_visible, encode_subscribe_all,
    encode_subscribe_some, encode_unsubscribe, encode_refacet_some,
    encode_handshake, encode_put_some,
)
from modules.threading_bridge import (
    ThreadingBridge, BridgeEvent, EventType, StatusReporter,
)

MAX_SIZE = 2 ** 32 - 1

# Mapping from protocol MessageType to bridge EventType for dispatch.
_TYPE_TO_EVENT = {
    MessageType.LIST_ALL_1:     EventType.LIST_RESPONSE,
    MessageType.LIST_SOME_1:    EventType.LIST_RESPONSE,
    MessageType.LIST_VISIBLE_1: EventType.LIST_RESPONSE,
    MessageType.TRANSACTION_1:  EventType.TRANSACTION,
    MessageType.REFACET_SOME_1: EventType.REFACET_RESPONSE,
    MessageType.NEW_VERSION_1:  EventType.NEW_VERSION,
    MessageType.NEW_FILE_1:     EventType.NEW_FILE,
    MessageType.HANDSHAKE_1:    EventType.HANDSHAKE_RESPONSE,
    MessageType.PUT_SOME_1:     EventType.PUT_SOME_RESPONSE,
}

# Message types that carry a filename to track on the client/bridge.
_FILENAME_TYPES = frozenset({
    MessageType.LIST_ALL_1, MessageType.LIST_SOME_1,
    MessageType.LIST_VISIBLE_1, MessageType.TRANSACTION_1,
    MessageType.NEW_VERSION_1, MessageType.NEW_FILE_1,
})


class PlasticityClient:
    """
    WebSocket client for Plasticity server.

    Runs asyncio event loop in a background thread and communicates
    with the main thread via ThreadingBridge.
    """

    def __init__(self, handler, bridge: ThreadingBridge):
        self.handler = handler
        self.bridge = bridge
        self.status = StatusReporter(bridge)

        self.server = "localhost:8980"
        self.websocket = None
        self.message_id = 0
        self.subscribed = False
        self.filename = None

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._parser = MessageParser()

        # v2.1.0: handshake / capability negotiation
        self.supported_messages = set()
        # v2.1.0: callback invoked after list response is processed
        self.on_list_complete = None

    @property
    def connected(self) -> bool:
        return self.bridge.connected

    def supports(self, message_type: MessageType) -> bool:
        """Check whether the server supports a given message type."""
        return message_type.value in self.supported_messages

    # =========================================================================
    # Connection management
    # =========================================================================

    def connect(self, server: Optional[str] = None):
        if not WEBSOCKETS_AVAILABLE:
            self.status.error("websockets library not available")
            return
        if self.connected:
            self.status.warning("Already connected")
            return

        if server:
            self.server = server

        self.status.info(f"Connecting to {self.server}...")
        self._loop = asyncio.new_event_loop()
        self._running = True

        self._thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self._thread.start()

    def disconnect(self):
        if not self._running and not self.connected:
            return

        self.status.info("Disconnecting...")
        self._running = False

        # Ask the websocket to close so recv() raises ConnectionClosed
        if self._loop and self.websocket:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._disconnect_async(), self._loop
                )
                try:
                    future.result(timeout=2.0)
                except Exception:
                    pass
            except Exception as e:
                print(f"[Plasticity] Disconnect error: {e}")

        # Wait for the background thread to finish and push DISCONNECTED
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)

        # Safety net: if the thread didn't clean up, do it from here
        if self.bridge.connected:
            self._cleanup_state()
            self.bridge.push_event(BridgeEvent(event_type=EventType.DISCONNECTED))
            self.status.info("Disconnected")

    def _cleanup_state(self):
        self.bridge.connected = False
        self.websocket = None
        self.filename = None
        self.subscribed = False
        self.supported_messages = set()
        self.on_list_complete = None

    def _run_event_loop(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect_and_listen())
        except Exception as e:
            print(f"[Plasticity] Event loop error: {e}")
            self.bridge.push_event(BridgeEvent(
                event_type=EventType.CONNECTION_ERROR, error_message=str(e)
            ))
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self._loop = None
            self._cleanup_state()
            # Let the main thread handle on_disconnect via the bridge callback
            self.bridge.push_event(BridgeEvent(event_type=EventType.DISCONNECTED))

    async def _connect_and_listen(self):
        uri = f"ws://{self.server}"
        try:
            async with ws_client.connect(uri, max_size=MAX_SIZE) as ws:
                self.websocket = weakref.proxy(ws)
                self.bridge.connected = True
                self.message_id = 0
                self.bridge.push_event(BridgeEvent(event_type=EventType.CONNECTED))
                self.status.info(f"Connected to {self.server}")

                # v2.1.0: send handshake immediately after connecting
                await self._send_handshake()

                while self._running:
                    try:
                        message = await ws.recv()
                        await self._handle_message(message)
                    except ConnectionClosed:
                        print("[Plasticity] Connection closed by server")
                        break
                    except Exception as e:
                        print(f"[Plasticity] Listen error: {e}")
                        continue

        except ConnectionClosed:
            self.status.info("Disconnected from server")
        except OSError as e:
            self.status.error(f"Connection failed: {e}")
            self.bridge.push_event(BridgeEvent(
                event_type=EventType.CONNECTION_ERROR,
                error_message=str(e)
            ))
        except Exception as e:
            self.status.error(f"Connection error: {e}")
            self.bridge.push_event(BridgeEvent(
                event_type=EventType.CONNECTION_ERROR,
                error_message=str(e)
            ))

    async def _disconnect_async(self):
        if self.websocket:
            try:
                await self.websocket.close()
            except Exception:
                pass
        self._running = False

    # =========================================================================
    # Message handling
    # =========================================================================

    async def _handle_message(self, data: bytes):
        try:
            parsed = self._parser.parse_message(data)
            if parsed:
                self._dispatch_parsed(parsed)
        except Exception as e:
            print(f"[Plasticity] Parse error: {e}")
            import traceback
            traceback.print_exc()

    def _dispatch_parsed(self, parsed: dict):
        """Route a parsed message to the bridge as a typed event."""
        msg_type = parsed.get('type')
        event_type = _TYPE_TO_EVENT.get(msg_type)
        if event_type is None:
            return

        if msg_type in _FILENAME_TYPES:
            fn = parsed.get('filename', '')
            self.filename = fn
            self.bridge.filename = fn

        self.bridge.push_event(BridgeEvent(event_type=event_type, data=parsed))

    # =========================================================================
    # Async send helpers
    # =========================================================================

    def _run_async(self, coro) -> Optional[Future]:
        if not self._loop or not self.connected:
            return None
        try:
            return asyncio.run_coroutine_threadsafe(coro, self._loop)
        except Exception:
            return None

    def _send_and_wait(self, coro, timeout=5.0):
        future = self._run_async(coro)
        if future:
            try:
                future.result(timeout=timeout)
            except Exception as e:
                print(f"[Plasticity] Send failed: {e}")
                self.status.error(f"Send failed: {e}")
                self.bridge.push_event(BridgeEvent(
                    event_type=EventType.STATUS_UPDATE,
                    error_message=str(e),
                ))
        else:
            self.status.error("Cannot send — not connected")
            self.bridge.push_event(BridgeEvent(
                event_type=EventType.STATUS_UPDATE,
                error_message="Cannot send — not connected",
            ))

    # =========================================================================
    # Public API
    # =========================================================================

    def list_all(self, on_complete=None):
        if not self.connected:
            return
        self.on_list_complete = on_complete
        self.status.info("Refreshing all objects...")
        self._send_and_wait(self._send(encode_list_all))

    def list_visible(self, on_complete=None):
        if not self.connected:
            return
        self.on_list_complete = on_complete
        self.status.info("Refreshing visible objects...")
        self._send_and_wait(self._send(encode_list_visible))

    def subscribe_all(self):
        if not self.connected:
            return
        self.status.info("Subscribing to live updates...")
        self._send_and_wait(self._send(encode_subscribe_all))
        self.subscribed = True

    def unsubscribe(self):
        if not self.connected:
            return
        self.status.info("Unsubscribing...")
        self._send_and_wait(self._send(encode_unsubscribe))
        self.subscribed = False

    def subscribe_some(self, filename: str, plasticity_ids: List[int]):
        if not self.connected or not plasticity_ids:
            return
        self._send_and_wait(self._send_subscribe_some(filename, plasticity_ids))

    def refacet_some(self, filename, plasticity_ids, **kwargs):
        if not self.connected or not plasticity_ids:
            return
        self.status.info(f"Refaceting {len(plasticity_ids)} objects...")
        self._send_and_wait(self._send_refacet(filename, plasticity_ids, **kwargs))

    def put_some(self, filename: str, groups: list, items: list):
        """Upload outbox meshes to Plasticity (v2.1.0)."""
        if not self.connected:
            return
        self.status.info(
            f"Uploading {len(groups)} groups and {len(items)} items to Plasticity...")
        self._send_and_wait(
            self._send_put_some(filename, groups, items), timeout=30.0)

    # =========================================================================
    # Async send implementations
    # =========================================================================

    async def _send(self, encode_fn):
        self.message_id += 1
        msg = encode_fn(self.message_id)
        await self.websocket.send(msg)

    async def _send_handshake(self):
        self.message_id += 1
        msg = encode_handshake(self.message_id)
        await self.websocket.send(msg)

    async def _send_subscribe_some(self, filename, ids):
        self.message_id += 1
        msg = encode_subscribe_some(self.message_id, filename, ids)
        await self.websocket.send(msg)

    async def _send_refacet(self, filename, ids, **kwargs):
        self.message_id += 1
        msg = encode_refacet_some(self.message_id, filename, ids, **kwargs)
        await self.websocket.send(msg)

    async def _send_put_some(self, filename, groups, items):
        self.message_id += 1
        msg = encode_put_some(self.message_id, filename, groups, items)
        await self.websocket.send(msg)
