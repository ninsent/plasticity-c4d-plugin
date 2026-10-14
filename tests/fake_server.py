"""
A scriptable fake Plasticity WebSocket server for client tests.

Runs the bundled websockets (legacy) server on 127.0.0.1 in its own thread
and event loop.  Incoming client messages are recorded; replies are
produced by ``responder(raw_bytes) -> list[bytes]`` and extra frames can be
pushed at any time with ``push()``.
"""

import asyncio
import struct
import threading

from plasticity_bridge._vendor.websockets import server as ws_server

import wire as W


def default_responder(data):
    msg_type, msg_id = struct.unpack_from("<II", data, 0)
    if msg_type == 100:                               # HANDSHAKE_1
        return [W.handshake_response(msg_id, [0, 20, 22, 23, 25, 26, 31, 100])]
    if msg_type in (20, 22):                          # LIST_ALL / LIST_VISIBLE
        obj = W.mesh_object(7, "Box", [0.0] * 9, [0, 1, 2])
        return [W.list_response("file.plasticity", [W.item(1, [obj])],
                                message_id=msg_id, msg_type=msg_type)]
    return []


class FakePlasticityServer:
    def __init__(self, responder=default_responder):
        self.responder = responder
        self.received = []
        self.connections = 0
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._stop = None
        self._clients = set()
        self.port = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="fake-plasticity-server")

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self):
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("fake server did not start")
        return self

    def __exit__(self, *exc):
        self.stop()

    def stop(self):
        if self._stop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._stop.set_result, None)
        self._thread.join(5)

    @property
    def address(self):
        return f"127.0.0.1:{self.port}"

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())
        self._loop.close()

    async def _main(self):
        self._stop = self._loop.create_future()
        async with ws_server.serve(self._handler, "127.0.0.1", 0,
                                   max_size=2 ** 32 - 1) as server:
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            await self._stop
            for ws in list(self._clients):
                await ws.close()

    async def _handler(self, ws, path=None):
        self.connections += 1
        self._clients.add(ws)
        try:
            async for data in ws:
                self.received.append(data)
                for reply in self.responder(data) or []:
                    await ws.send(reply)
        except Exception:
            pass
        finally:
            self._clients.discard(ws)

    # -- control -------------------------------------------------------------
    def push(self, data):
        """Send a frame to every connected client."""
        async def send_all():
            for ws in list(self._clients):
                await ws.send(data)
        asyncio.run_coroutine_threadsafe(send_all(), self._loop).result(5)

    def drop_clients(self):
        """Close every client connection from the server side."""
        async def close_all():
            for ws in list(self._clients):
                await ws.close()
        asyncio.run_coroutine_threadsafe(close_all(), self._loop).result(5)

    def received_types(self):
        return [struct.unpack_from("<I", d, 0)[0] for d in self.received]
