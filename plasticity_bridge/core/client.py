"""
WebSocket client for connecting to Plasticity server.

Runs an asyncio event loop in a background thread. All parsed messages
are dispatched to the main thread via the ThreadingBridge.
Architecture matches the Blender addon: only the listen loop calls recv().

Every public call is non-blocking.  Requests are encoded on the calling
thread, handed to the event loop and sent there; a failure to send is
reported back through the bridge as an ``OPERATION_FAILED`` event instead of
blocking Cinema 4D's main thread while waiting for the socket.  Each
connection lives in its own ``_Session`` so a connection that is still
shutting down can never clobber the state of a newer one.
"""

import asyncio
import itertools
import threading
import weakref
from typing import List, Optional

try:
    # Bundled copy only, never the global ``websockets`` (shared interpreter).
    from .._vendor.websockets import client as ws_client
    from .._vendor.websockets.exceptions import ConnectionClosed
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    print("[Plasticity] Warning: websockets library not found")

from .protocol import (
    MessageType, MessageParser, LIST_TYPES, peek_message_type,
    encode_list_all, encode_list_visible, encode_subscribe_all,
    encode_subscribe_some, encode_unsubscribe, encode_refacet_some,
    encode_handshake, encode_put_some,
)
from .threading_bridge import (
    ThreadingBridge, BridgeEvent, EventType, StatusReporter,
)

MAX_SIZE = 2 ** 32 - 1

#: Seconds websockets waits for the peer during the closing handshake (its
#: default of 10 s kept a cancelled connection attempt alive that long).
CLOSE_TIMEOUT = 1.0

#: Upper bound for a single send to complete (large PUT_SOME uploads on a
#: slow link included) before it is reported as failed.
SEND_TIMEOUT = 120.0

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

# Request/response types: a response that cannot be parsed must still
# release whoever is waiting for it (e.g. the dialog's busy state).
_RESPONSE_TYPES = frozenset(LIST_TYPES | {MessageType.REFACET_SOME_1,
                                          MessageType.PUT_SOME_1})


class _Session:
    """State of one connection attempt (one thread, one event loop)."""

    _counter = itertools.count(1)

    def __init__(self, server):
        self.server = server
        self.number = next(self._counter)
        self.loop = asyncio.new_event_loop()
        self.thread = None
        self.websocket = None
        self.main_task = None
        self.running = True
        self._ids = itertools.count(1)
        self._id_lock = threading.Lock()

    def next_message_id(self):
        with self._id_lock:
            return next(self._ids)

    def request_close(self):
        """Runs on the session loop: close the socket or cancel connecting."""
        self.running = False
        if self.websocket is not None:
            asyncio.ensure_future(self._close_ws())
        elif self.main_task is not None:
            self.main_task.cancel()

    async def _close_ws(self):
        try:
            await asyncio.wait_for(self.websocket.close(), timeout=2.0)
        except Exception:
            pass
        if self.main_task is not None and not self.main_task.done():
            self.main_task.cancel()


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
        self.subscribed = False
        self.filename = None

        self._session: Optional[_Session] = None
        self._lock = threading.Lock()
        self._parser = MessageParser()
        self._closing_threads = []

        # Handshake / capability negotiation (filled by the handler on the
        # main thread when HANDSHAKE_RESPONSE is dispatched).
        self.supported_messages = set()
        # Callback invoked (main thread) after a list response is processed.
        self.on_list_complete = None

    @property
    def connected(self) -> bool:
        return self.bridge.connected

    @property
    def connecting(self) -> bool:
        """True while a connection attempt is in progress or open."""
        with self._lock:
            s = self._session
        return s is not None and s.thread is not None and s.thread.is_alive()

    @property
    def websocket(self):
        s = self._session
        return s.websocket if s else None

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
        with self._lock:
            if self._session is not None:
                self.status.warning("Already connected" if self.connected
                                    else "Already connecting")
                return
            if server:
                self.server = server
            session = _Session(self.server)
            self._session = session
            session.thread = threading.Thread(
                target=self._run_session, args=(session,), daemon=True,
                name=f"PlasticityBridge-ws-{session.number}")

        self.status.info(f"Connecting to {self.server}...")
        session.thread.start()

    def disconnect(self):
        """Close the connection without blocking the calling thread.

        The bridge state flips to disconnected immediately and a
        ``DISCONNECTED`` event is queued; the socket and event loop are
        torn down on the background thread.
        """
        with self._lock:
            session = self._session
            self._session = None
        if session is None:
            return

        self.status.info("Disconnecting...")
        session.running = False
        try:
            session.loop.call_soon_threadsafe(session.request_close)
        except RuntimeError:
            pass  # loop already closed: the thread is finishing on its own
        if session.thread is not None:
            self._closing_threads.append(session.thread)

        with self._lock:
            # Under the lock so a connect() racing with this call can't have
            # its fresh state reset, and DISCONNECTED is queued before any
            # event of a new session.
            was_connected = self.bridge.connected
            self._cleanup_state()
            self.bridge.push_event(
                BridgeEvent(event_type=EventType.DISCONNECTED))
        self.status.info("Disconnected" if was_connected
                         else "Connection cancelled")

    def shutdown(self, timeout: float = 2.0):
        """Disconnect and wait (bounded) for background threads to exit.

        Only meant for plugin shutdown (``C4DPL_ENDACTIVITY``); the threads
        are daemons, so an unresponsive socket can never block C4D exit.
        """
        self.disconnect()
        self.wait_closed(timeout)

    def wait_closed(self, timeout: float = 2.0) -> bool:
        """Join threads of closed sessions; True if all of them exited."""
        import time
        deadline = time.monotonic() + timeout
        alive = []
        for t in self._closing_threads:
            t.join(max(0.0, deadline - time.monotonic()))
            if t.is_alive():
                alive.append(t)
        self._closing_threads = alive
        return not alive

    def _cleanup_state(self):
        self.bridge.connected = False
        self.bridge.filename = None
        self.filename = None
        self.subscribed = False
        self.supported_messages = set()
        self.on_list_complete = None

    def _push(self, session, event):
        """Queue an event unless the session has been superseded.

        Check and push happen under the client lock, and ``disconnect()``
        detaches the session under the same lock before queueing
        DISCONNECTED — so nothing from an old session can land after it.
        """
        with self._lock:
            if self._session is session:
                self.bridge.push_event(event)
                return True
        return False

    def _run_session(self, session: _Session):
        loop = session.loop
        asyncio.set_event_loop(loop)
        try:
            session.main_task = loop.create_task(
                self._connect_and_listen(session))
            loop.run_until_complete(session.main_task)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[Plasticity] Event loop error: {e}")
            self._push(session, BridgeEvent(
                event_type=EventType.CONNECTION_ERROR, error_message=str(e)))
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
            with self._lock:
                if self._session is session:
                    # The server went away (or connecting failed) on its
                    # own.  Cleanup and the event happen under the lock so a
                    # concurrent connect() is not clobbered.
                    self._session = None
                    self._cleanup_state()
                    self.bridge.push_event(
                        BridgeEvent(event_type=EventType.DISCONNECTED))

    async def _connect_and_listen(self, session: _Session):
        uri = f"ws://{session.server}"
        try:
            async with ws_client.connect(uri, max_size=MAX_SIZE,
                                         close_timeout=CLOSE_TIMEOUT) as ws:
                if not session.running:
                    return
                session.websocket = weakref.proxy(ws)
                with self._lock:
                    # Atomic with disconnect(): never mark a session that
                    # was already detached as connected.
                    if self._session is not session:
                        return
                    self.bridge.connected = True
                    self.bridge.push_event(
                        BridgeEvent(event_type=EventType.CONNECTED))
                self.status.info(f"Connected to {session.server}")

                # Send the handshake immediately after connecting.
                await ws.send(encode_handshake(session.next_message_id()))

                while session.running:
                    try:
                        message = await ws.recv()
                    except ConnectionClosed:
                        if session.running:
                            print("[Plasticity] Connection closed by server")
                            self.status.info("Disconnected from server")
                        break
                    self._handle_message(session, message)

        except asyncio.CancelledError:
            raise
        except ConnectionClosed:
            self.status.info("Disconnected from server")
        except OSError as e:
            if session.running:
                self.status.error(f"Connection failed: {e}")
                self._push(session, BridgeEvent(
                    event_type=EventType.CONNECTION_ERROR,
                    error_message=str(e)))
        except Exception as e:
            if session.running:
                self.status.error(f"Connection error: {e}")
                self._push(session, BridgeEvent(
                    event_type=EventType.CONNECTION_ERROR,
                    error_message=str(e)))
        finally:
            session.websocket = None

    # =========================================================================
    # Message handling (background thread)
    # =========================================================================

    def _handle_message(self, session, data):
        if isinstance(data, str):
            print("[Plasticity] Ignoring unexpected text frame")
            return
        try:
            parsed = self._parser.parse_message(data)
        except Exception as e:
            self._report_parse_failure(session, data, e)
            return
        if parsed:
            self._dispatch_parsed(session, parsed)

    def _report_parse_failure(self, session, data, exc):
        raw_type = peek_message_type(data)
        try:
            msg_type = MessageType(raw_type)
        except (TypeError, ValueError):
            msg_type = None
        label = msg_type.name if msg_type is not None else raw_type
        print(f"[Plasticity] Could not parse {label} message "
              f"({len(data)} bytes): {exc}")
        if msg_type in _RESPONSE_TYPES:
            self.status.error(f"Unreadable {label} response from Plasticity")
            self._push(session, BridgeEvent(
                event_type=EventType.OPERATION_FAILED,
                data={'operation': msg_type},
                error_message=f"Unreadable {label} response: {exc}"))
        elif msg_type == MessageType.TRANSACTION_1:
            self.status.error("A Live Link update could not be read — "
                              "click Refresh to resync")

    def _dispatch_parsed(self, session, parsed: dict):
        """Route a parsed message to the bridge as a typed event."""
        msg_type = parsed.get('type')
        event_type = _TYPE_TO_EVENT.get(msg_type)
        if event_type is None:
            return

        with self._lock:
            if self._session is not session:
                return                       # superseded: drop silently
            if msg_type in _FILENAME_TYPES and not parsed.get('error'):
                fn = parsed.get('filename', '')
                self.filename = fn
                self.bridge.filename = fn
            elif msg_type == MessageType.HANDSHAKE_1:
                # Record capabilities right away so a Refresh queued before
                # the HANDSHAKE_RESPONSE event is dispatched already sees them.
                self.supported_messages = set(
                    parsed.get('supported_messages', ()))
            self.bridge.push_event(BridgeEvent(event_type=event_type,
                                               data=parsed))

    # =========================================================================
    # Sending (non-blocking)
    # =========================================================================

    def _fail(self, operation, message):
        self.status.error(message)
        self.bridge.push_event(BridgeEvent(
            event_type=EventType.OPERATION_FAILED,
            data={'operation': operation}, error_message=message))

    def _submit(self, operation, encode, *args) -> Optional[int]:
        """Encode on the calling thread and send on the session loop.

        Returns the message ID, or None if nothing was sent (the failure is
        reported through the bridge either way).
        """
        with self._lock:
            session = self._session
        if session is None or not self.connected or session.websocket is None:
            self._fail(operation, "Cannot send — not connected")
            return None
        message_id = session.next_message_id()
        try:
            payload = encode(message_id, *args)
        except Exception as e:
            self._fail(operation, f"Could not encode {operation.name}: {e}")
            return None

        async def send():
            ws = session.websocket
            if ws is None:
                raise ConnectionError("connection closed")
            await asyncio.wait_for(ws.send(payload), timeout=SEND_TIMEOUT)

        try:
            future = asyncio.run_coroutine_threadsafe(send(), session.loop)
        except RuntimeError as e:
            self._fail(operation, f"Send failed: {e}")
            return None

        def done(f):
            # Runs on the session loop's thread: only queue an event.
            if f.cancelled():
                exc = ConnectionError("send cancelled")
            else:
                exc = f.exception()
            if exc is not None:
                message = f"Send failed: {exc or type(exc).__name__}"
                if self._push(session, BridgeEvent(
                        event_type=EventType.OPERATION_FAILED,
                        data={'operation': operation}, error_message=message)):
                    self.status.error(message)

        future.add_done_callback(done)
        return message_id

    # =========================================================================
    # Public API
    # =========================================================================

    def list_all(self, on_complete=None):
        if not self.connected:
            return None
        if on_complete is not None:
            self.on_list_complete = on_complete
        self.status.info("Refreshing all objects...")
        return self._submit(MessageType.LIST_ALL_1, encode_list_all)

    def list_visible(self, on_complete=None):
        if not self.connected:
            return None
        if on_complete is not None:
            self.on_list_complete = on_complete
        self.status.info("Refreshing visible objects...")
        return self._submit(MessageType.LIST_VISIBLE_1, encode_list_visible)

    def subscribe_all(self):
        if not self.connected:
            return None
        self.status.info("Subscribing to live updates...")
        mid = self._submit(MessageType.SUBSCRIBE_ALL_1, encode_subscribe_all)
        self.subscribed = mid is not None
        return mid

    def unsubscribe(self):
        if not self.connected:
            return None
        self.status.info("Unsubscribing...")
        mid = self._submit(MessageType.UNSUBSCRIBE_ALL_1, encode_unsubscribe)
        self.subscribed = False
        return mid

    def subscribe_some(self, filename: str, plasticity_ids: List[int]):
        if not self.connected or not plasticity_ids:
            return None
        return self._submit(MessageType.SUBSCRIBE_SOME_1,
                            encode_subscribe_some, filename,
                            list(plasticity_ids))

    def refacet_some(self, filename, plasticity_ids, **kwargs):
        if not self.connected or not plasticity_ids:
            return None
        self.status.info(f"Refaceting {len(plasticity_ids)} objects...")
        return self._submit(
            MessageType.REFACET_SOME_1,
            lambda mid: encode_refacet_some(mid, filename,
                                            list(plasticity_ids), **kwargs))

    def put_some(self, filename: str, groups: list, items: list):
        """Upload outbox meshes to Plasticity."""
        if not self.connected:
            return None
        self.status.info(
            f"Uploading {len(groups)} groups and {len(items)} items to Plasticity...")
        return self._submit(MessageType.PUT_SOME_1, encode_put_some,
                            filename, groups, items)
