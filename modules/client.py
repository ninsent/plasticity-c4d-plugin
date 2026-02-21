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
)
from modules.threading_bridge import (
    ThreadingBridge, BridgeEvent, EventType, StatusReporter,
)

MAX_SIZE = 2 ** 32 - 1


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

    @property
    def connected(self) -> bool:
        return self.bridge.connected

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
                # Fix #1: on_connect is triggered by the CONNECTED event callback
                # on the main thread — not called directly here.

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
        msg_type = parsed.get('type')

        if msg_type in (MessageType.LIST_ALL_1, MessageType.LIST_SOME_1,
                        MessageType.LIST_VISIBLE_1):
            fn = parsed.get('filename', '')
            self.filename = fn
            self.bridge.filename = fn
            self.bridge.push_event(BridgeEvent(
                event_type=EventType.LIST_RESPONSE, data=parsed
            ))

        elif msg_type == MessageType.TRANSACTION_1:
            fn = parsed.get('filename', '')
            self.filename = fn
            self.bridge.filename = fn
            self.bridge.push_event(BridgeEvent(
                event_type=EventType.TRANSACTION, data=parsed
            ))

        elif msg_type == MessageType.REFACET_SOME_1:
            self.bridge.push_event(BridgeEvent(
                event_type=EventType.REFACET_RESPONSE, data=parsed
            ))

        elif msg_type == MessageType.NEW_VERSION_1:
            fn = parsed.get('filename', '')
            self.filename = fn
            self.bridge.filename = fn
            self.bridge.push_event(BridgeEvent(
                event_type=EventType.NEW_VERSION, data=parsed
            ))

        elif msg_type == MessageType.NEW_FILE_1:
            fn = parsed.get('filename', '')
            self.filename = fn
            self.bridge.filename = fn
            self.bridge.push_event(BridgeEvent(
                event_type=EventType.NEW_FILE, data=parsed
            ))

    # =========================================================================
    # Async send helper
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

    def list_all(self):
        if not self.connected:
            return
        self.status.info("Refreshing all objects...")
        self._send_and_wait(self._send(encode_list_all))

    def list_visible(self):
        if not self.connected:
            return
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

    async def _send(self, encode_fn):
        self.message_id += 1
        msg = encode_fn(self.message_id)
        await self.websocket.send(msg)

    async def _send_subscribe_some(self, filename, ids):
        self.message_id += 1
        msg = encode_subscribe_some(self.message_id, filename, ids)
        await self.websocket.send(msg)

    async def _send_refacet(self, filename, ids, **kwargs):
        self.message_id += 1
        msg = encode_refacet_some(self.message_id, filename, ids, **kwargs)
        await self.websocket.send(msg)