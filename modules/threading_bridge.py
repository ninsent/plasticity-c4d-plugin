"""
Threading bridge for communication between background WebSocket thread and C4D main thread.

Cinema 4D's API is not thread-safe, so all C4D operations must happen on the main thread.
This module provides a thread-safe queue for passing parsed messages from the WebSocket
client (running in a background thread) to the scene handler (called from main thread).
"""

import threading
from queue import Queue, Empty
from typing import Any, Callable, Optional, Dict, List
from dataclasses import dataclass
from enum import Enum, auto


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

    def __init__(self, max_queue_size: int = 1000):
        self._queue = Queue(maxsize=max_queue_size)
        self._lock = threading.Lock()
        self._connected = False
        self._filename = None
        self._status_message = "Disconnected"
        self._callbacks: Dict[EventType, List[Callable]] = {}

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

    def push_event(self, event: BridgeEvent) -> bool:
        try:
            self._queue.put_nowait(event)
            return True
        except Exception:
            return False

    def register_callback(self, event_type: EventType, callback: Callable):
        if event_type not in self._callbacks:
            self._callbacks[event_type] = []
        self._callbacks[event_type].append(callback)

    def dispatch_event(self, event: BridgeEvent):
        if event.event_type in self._callbacks:
            for callback in self._callbacks[event.event_type]:
                try:
                    callback(event)
                except Exception as e:
                    print(f"[Bridge] Callback error for {event.event_type}: {e}")
                    import traceback
                    traceback.print_exc()

    def process_pending_events(self) -> int:
        """Poll and dispatch ALL pending events. Called from main thread."""
        count = 0
        while True:
            try:
                event = self._queue.get_nowait()
                self.dispatch_event(event)
                count += 1
            except Empty:
                break
        return count

    def clear_queue(self):
        while True:
            try:
                self._queue.get_nowait()
            except Empty:
                break


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
