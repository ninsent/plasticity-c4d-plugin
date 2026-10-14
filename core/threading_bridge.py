"""
Threading bridge for communication between background WebSocket thread and C4D main thread.

Cinema 4D's API is not thread-safe, so all C4D operations must happen on the main thread.
This module provides a thread-safe queue for passing parsed messages from the WebSocket
client (running in a background thread) to the scene handler (called from main thread).

The queue is unbounded: dropping a Live Link transaction would silently
desync the scene, so events are never discarded.  A growing backlog is
reported loudly instead.  The main thread drains it with a time budget per
timer tick so a burst of large updates neither freezes the UI for long nor
falls permanently behind.
"""

import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional


class EventType(Enum):
    CONNECTED = auto()
    DISCONNECTED = auto()
    CONNECTION_ERROR = auto()
    LIST_RESPONSE = auto()
    TRANSACTION = auto()
    REFACET_RESPONSE = auto()
    NEW_VERSION = auto()
    NEW_FILE = auto()
    STATUS_UPDATE = auto()
    HANDSHAKE_RESPONSE = auto()
    PUT_SOME_RESPONSE = auto()
    # A request could not be sent, or its response could not be parsed.
    # ``data`` carries {'operation': <MessageType or name>}.
    OPERATION_FAILED = auto()


@dataclass
class BridgeEvent:
    event_type: EventType
    data: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None


class ThreadingBridge:
    """
    Thread-safe bridge for passing events between background and main threads.

    Usage:
        1. Background thread calls push_event() to queue events
        2. Main thread calls process_pending_events() from GeDialog.Timer()
        3. Registered callbacks are dispatched on the main thread
    """

    #: Backlog size at which a warning is printed (and again at every
    #: doubling), so a stalled main thread is visible in the console.
    BACKLOG_WARNING = 200

    def __init__(self, max_queue_size: int = 0):
        # ``max_queue_size`` is accepted for backwards compatibility and
        # ignored: the queue is unbounded by design (see module docstring).
        self._queue = deque()
        self._queue_lock = threading.Lock()
        self._lock = threading.Lock()
        self._connected = False
        self._filename = None
        self._status_message = "Disconnected"
        self._callbacks: Dict[EventType, List[Callable]] = {}
        self._next_warning = self.BACKLOG_WARNING
        self._dispatching = False

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @connected.setter
    def connected(self, value: bool):
        with self._lock:
            self._connected = value

    @property
    def filename(self) -> Optional[str]:
        with self._lock:
            return self._filename

    @filename.setter
    def filename(self, value: Optional[str]):
        with self._lock:
            self._filename = value

    @property
    def status_message(self) -> str:
        with self._lock:
            return self._status_message

    @status_message.setter
    def status_message(self, value: str):
        with self._lock:
            self._status_message = value

    def pending_count(self) -> int:
        with self._queue_lock:
            return len(self._queue)

    def push_event(self, event: BridgeEvent) -> bool:
        """Queue an event (any thread).  Never drops; always returns True."""
        with self._queue_lock:
            self._queue.append(event)
            size = len(self._queue)
            warn = size >= self._next_warning
            if warn:
                self._next_warning *= 2
        if warn:
            print(f"[Bridge] WARNING: {size} events waiting for the main "
                  f"thread — Cinema 4D is not keeping up with Plasticity "
                  f"(is the Plasticity Bridge dialog open?)")
        return True

    def register_callback(self, event_type: EventType, callback: Callable):
        if event_type not in self._callbacks:
            self._callbacks[event_type] = []
        self._callbacks[event_type].append(callback)

    def dispatch_event(self, event: BridgeEvent):
        for callback in list(self._callbacks.get(event.event_type, ())):
            try:
                callback(event)
            except Exception as e:
                print(f"[Bridge] Callback error for {event.event_type}: {e}")
                import traceback
                traceback.print_exc()

    def _pop(self):
        with self._queue_lock:
            if not self._queue:
                return None
            event = self._queue.popleft()
            if len(self._queue) < self.BACKLOG_WARNING:
                self._next_warning = self.BACKLOG_WARNING
            return event

    def process_pending_events(self, max_events: Optional[int] = None,
                               time_budget: Optional[float] = 0.05) -> int:
        """Dispatch queued events on the calling (main) thread.

        Processes events in FIFO order until the queue is empty, or until
        ``max_events`` events were handled, or until ``time_budget`` seconds
        have elapsed (at least one event is always processed).  Re-entrant
        calls — e.g. a Timer firing while a callback shows a modal dialog —
        return immediately so events are never dispatched out of order.
        """
        if self._dispatching:
            return 0
        self._dispatching = True
        try:
            count = 0
            start = time.monotonic()
            while max_events is None or count < max_events:
                if (count and time_budget is not None
                        and time.monotonic() - start >= time_budget):
                    break
                event = self._pop()
                if event is None:
                    break
                self.dispatch_event(event)
                count += 1
            return count
        finally:
            self._dispatching = False

    def clear_queue(self):
        with self._queue_lock:
            self._queue.clear()
            self._next_warning = self.BACKLOG_WARNING


class StatusReporter:
    """Helper for reporting status updates from the WebSocket thread."""

    def __init__(self, bridge: ThreadingBridge):
        self._bridge = bridge

    def info(self, message: str):
        self._bridge.status_message = message

    def warning(self, message: str):
        self._bridge.status_message = f"Warning: {message}"

    def error(self, message: str):
        self._bridge.status_message = f"Error: {message}"
