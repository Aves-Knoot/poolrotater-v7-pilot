"""Bounded JSON RPC over an inherited socketpair. Requests are never retried.

The reader always runs independently of request handlers, so a callback can
make a reverse request without deadlocking the connection. A timeout is an
unknown outcome, including when the remote handler later returns successfully.
"""
from __future__ import annotations

import itertools
import json
import queue
import select
import socket
import struct
import threading
import time

MAX_FRAME = 4 * 1024 * 1024


class PeerClosed(RuntimeError):
    pass


class RemoteError(RuntimeError):
    def __init__(self, kind, message):
        self.kind = kind
        super().__init__(message)


class UncertainResult(RuntimeError):
    pass


class Peer:
    def __init__(self, sock, handler=None, on_event=None, name="rpc"):
        self.sock, self.handler, self.on_event = sock, handler, on_event
        self.closed = threading.Event()
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending = {}
        self._ids = itertools.count(1)
        self._requests = queue.Queue(maxsize=64)
        self._threads = [
            threading.Thread(target=self._read, name=name + "-read", daemon=True),
            threading.Thread(target=self._serve, name=name + "-serve", daemon=True),
        ]

    def start(self):
        for thread in self._threads:
            thread.start()
        return self

    def send(self, message):
        raw = json.dumps(message, separators=(",", ":"), allow_nan=False).encode()
        if len(raw) > MAX_FRAME:
            raise ValueError("RPC frame exceeds limit")
        if not self._send_lock.acquire(timeout=5):
            self.close()
            raise PeerClosed("RPC writer backlog exceeded deadline")
        try:
            if self.closed.is_set():
                raise PeerClosed("worker connection closed")
            try:
                data = memoryview(struct.pack("!I", len(raw)) + raw)
                deadline = time.monotonic() + 5
                while data:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([], [self.sock], [], remaining)[1]:
                        raise OSError("RPC peer stopped reading")
                    try:
                        sent = self.sock.send(data, socket.MSG_DONTWAIT)
                    except BlockingIOError:
                        continue
                    if not sent:
                        raise OSError("RPC socket closed")
                    data = data[sent:]
            except (OSError, ValueError) as exc:
                self.close()
                raise PeerClosed("worker connection failed") from exc
        finally:
            self._send_lock.release()

    def call(self, method, *args, timeout=120, **kwargs):
        with self._pending_lock:
            if len(self._pending) >= 64:
                raise RuntimeError("too many pending RPC requests")
            ident = next(self._ids)
            inbox = queue.Queue(maxsize=1)
            self._pending[ident] = inbox
        try:
            self.send({"type": "call", "id": ident, "method": method,
                       "args": args, "kwargs": kwargs})
            try:
                result = inbox.get(timeout=timeout)
            except queue.Empty as exc:
                raise UncertainResult(f"{method} timed out; outcome unknown") from exc
            if isinstance(result, Exception):
                raise result
            if "error" in result:
                raise RemoteError(result["error"]["kind"], result["error"]["message"])
            return result.get("value")
        finally:
            with self._pending_lock:
                self._pending.pop(ident, None)

    def emit(self, name, value):
        self.send({"type": "event", "name": name, "value": value})

    def _receive(self, size):
        chunks = bytearray()
        while len(chunks) < size:
            part = self.sock.recv(size - len(chunks))
            if not part:
                raise PeerClosed("peer exited")
            chunks.extend(part)
        return chunks

    def _read(self):
        try:
            while not self.closed.is_set():
                size, = struct.unpack("!I", self._receive(4))
                if not 0 < size <= MAX_FRAME:
                    raise ValueError("invalid frame length")
                msg = json.loads(self._receive(size))
                if not isinstance(msg, dict):
                    raise ValueError("invalid RPC envelope")
                if msg.get("type") == "reply":
                    with self._pending_lock:
                        target = self._pending.get(msg.get("id"))
                        if target is not None:
                            target.put_nowait(msg)
                elif msg.get("type") == "call":
                    self._requests.put_nowait(msg)
                elif msg.get("type") == "event" and self.on_event:
                    self.on_event(msg["name"], msg.get("value"))
                else:
                    raise ValueError("invalid RPC message type")
        except (OSError, ValueError, KeyError, TypeError, queue.Full, PeerClosed):
            pass
        finally:
            self.close()

    def _serve(self):
        while not self.closed.is_set():
            try:
                msg = self._requests.get(timeout=.2)
            except queue.Empty:
                continue
            response = {"type": "reply", "id": msg.get("id")}
            try:
                if self.handler is None:
                    raise ValueError("requests are not accepted")
                response["value"] = self.handler(msg["method"], *msg.get("args", []),
                                                 **msg.get("kwargs", {}))
            except Exception as exc:
                response["error"] = {"kind": type(exc).__name__, "message": str(exc)[:1000]}
            try:
                self.send(response)
            except (PeerClosed, ValueError, TypeError):
                self.close()

    def close(self):
        self.closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        with self._pending_lock:
            for target in self._pending.values():
                try:
                    target.put_nowait(PeerClosed("RPC disconnected; outcome unknown"))
                except queue.Full:
                    pass
