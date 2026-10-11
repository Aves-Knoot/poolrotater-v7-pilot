"""Owned fixed-target SignalR socket worker and bounded private IPC.

All DNS/TLS/header/frame I/O lives in the killable child. No library network
client, proxy discovery, redirect, destination argument or raw protocol logging.
"""
from email.message import Message
import base64
import hashlib
import http.client
import multiprocessing
import os
import select
import signal
import socket
import struct
import threading
import time
from urllib.parse import quote

from .oauth import AuthError, _secret
from .provider_contract import exchange_fixture, require_live_provider
from .provider_transport import TrustSnapshot, retry_after
from .referee_contract import REFEREE_HOST, exchange_referee_upstream, negotiation, prepare_referee
from .referee_protocol import Frames, Records, MAX_COMMAND, encoded, decoded, frame, message

MAX_PACKET = 2 * 1024**2
GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


def boundary(port):
    if port is None:
        require_live_provider()
        return
    if (os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1'
            or type(port) is not int or not 0 < port <= 65535):
        raise AuthError('offline_verified_tls_required')


def _read(fd, count, deadline, stop=None):
    raw = bytearray()
    while len(raw) < count:
        if stop is not None and stop.is_set(): raise AuthError('referee_stopped')
        left = deadline - time.monotonic()
        if left <= 0: raise AuthError('referee_deadline_exceeded')
        if not select.select([fd], [], [], min(left, .05))[0]: continue
        part = os.read(fd, min(16384, count - len(raw)))
        if not part: raise AuthError('referee_pipe_closed')
        raw.extend(part)
    return bytes(raw)


def receive_packet(fd, deadline, stop=None):
    size = struct.unpack('!i', _read(fd, 4, deadline, stop))[0]
    if not 0 < size <= MAX_PACKET: raise AuthError('referee_packet_refused')
    # Packet envelopes can hold a full bounded protocol message.
    import json
    from .layout import unique_fields
    import math
    def finite(raw):
        value = float(raw)
        if not math.isfinite(value): raise ValueError
        return value
    try: value = json.loads(_read(fd, size, deadline, stop), object_pairs_hook=unique_fields,
                            parse_float=finite, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except AuthError: raise
    except Exception: raise AuthError('referee_packet_refused') from None
    if type(value) is not dict: raise AuthError('referee_packet_refused')
    return value


def send_packet(fd, value, deadline):
    raw = encoded(value, MAX_PACKET)
    raw = struct.pack('!i', len(raw)) + raw
    os.set_blocking(fd, False)
    try:
        offset = 0
        while offset < len(raw):
            left = deadline - time.monotonic()
            if left <= 0 or not select.select([], [fd], [], left)[1]:
                raise AuthError('referee_deadline_exceeded')
            try: offset += os.write(fd, raw[offset:offset+16384])
            except BlockingIOError: continue
    finally: os.set_blocking(fd, True)


def _upgrade(port, trust, bearer, token):
    boundary(port)
    host, target_port = (REFEREE_HOST, 443) if port is None else ('127.0.0.1', port)
    sock = None
    try:
        sock = socket.create_connection((host, target_port), timeout=5)
        sock = trust.context().wrap_socket(sock, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        target = '/referee?id=' + quote(token, safe='')
        authority = host if port is None else host + ':' + str(port)
        request = (f'GET {target} HTTP/1.1\r\nHost: {authority}\r\n'
                   'Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n'
                   f'Sec-WebSocket-Key: {key}\r\nAuthorization: Bearer {_secret(bearer)}\r\n\r\n')
        sock.sendall(request.encode('ascii'))
        deadline = time.monotonic() + 5
        raw = bytearray()
        while not raw.endswith(b'\r\n\r\n'):
            left = deadline - time.monotonic()
            if left <= 0: raise AuthError('referee_upgrade_deadline')
            sock.settimeout(left)
            part = sock.recv(1)
            if not part: raise AuthError('referee_upgrade_refused')
            raw.extend(part)
            if len(raw) > 16384: raise AuthError('referee_upgrade_refused')
        lines = bytes(raw).decode('ascii').split('\r\n')
        import re
        status = re.fullmatch(r'HTTP/1\.[01] ([1-5][0-9]{2})(?: [^\r\n]*)?', lines[0])
        if not status: raise AuthError('referee_upgrade_refused')
        code = int(status[1]); headers = Message()
        for line in lines[1:-2]:
            name, separator, value = line.partition(':')
            if not separator or not re.fullmatch(r'[A-Za-z0-9-]+', name) or line[0].isspace():
                raise AuthError('referee_upgrade_refused')
            headers[name] = value.strip()
        if code != 101:
            sock.close(); sock = None
            return None, code, retry_after(headers) if code == 429 else 0
        expected = base64.b64encode(hashlib.sha1((key+GUID).encode('ascii')).digest()).decode('ascii')
        if (headers.get_all('Sec-WebSocket-Accept') != [expected]
                or len(headers.get_all('Upgrade', [])) != 1 or headers['Upgrade'].lower() != 'websocket'
                or len(headers.get_all('Connection', [])) != 1
                or 'upgrade' not in [part.strip().lower() for part in headers['Connection'].split(',')]
                or headers.get_all('Sec-WebSocket-Extensions') or headers.get_all('Sec-WebSocket-Protocol')
                or headers.get_all('Transfer-Encoding') or headers.get_all('Content-Length')):
            raise AuthError('referee_upgrade_refused')
        return sock, 101, 0
    except BaseException:
        if sock is not None: sock.close()
        raise


def _worker(pipe, port, trust, bearer, parent_pid):
    sock = None
    try:
        with open(os.devnull, 'wb') as null:
            os.dup2(null.fileno(), 1); os.dup2(null.fileno(), 2)
        import ctypes
        if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            raise AuthError('referee_child_setup_refused')
        if os.getppid() != parent_pid: raise AuthError('referee_parent_changed')
        boundary(port)
        if type(trust) is not TrustSnapshot: raise AuthError('provider_trust_refused')
        request = prepare_referee(bearer)
        host, target_port = (REFEREE_HOST, 443) if port is None else ('127.0.0.1', port)
        connection = http.client.HTTPSConnection(host, target_port, context=trust.context(), timeout=5)
        cooldown = 0
        def status(code, headers):
            nonlocal cooldown
            if code == 429: cooldown = retry_after(headers)
        exchange = exchange_referee_upstream if port is None else exchange_fixture
        code, value = exchange(connection, request, timeout=5, on_status=status)
        send_packet(pipe.fileno(), {'kind':'negotiated','status':code,'data':value,'cooldown':cooldown}, time.monotonic()+5)
        if code != 200: return
        token = negotiation(value)
        command = receive_packet(pipe.fileno(), time.monotonic()+10)
        if command != {'kind':'upgrade'}: raise AuthError('referee_packet_refused')
        sock, code, cooldown = _upgrade(port, trust, bearer, token)
        send_packet(pipe.fileno(), {'kind':'upgraded','status':code,'cooldown':cooldown}, time.monotonic()+5)
        if code != 101: return
        command = receive_packet(pipe.fileno(), time.monotonic()+10)
        if command != {'kind':'handshake'}: raise AuthError('referee_packet_refused')
        sock.settimeout(5)
        sock.sendall(frame(b'{"protocol":"json","version":1}\x1e', masked=True))
        frames, records = Frames(sock, expect_masked=False), Records()
        deadline = time.monotonic()+5
        handshake = False
        last_received = last_ping = time.monotonic()
        while True:
            frames.check(); records.check()
            now = time.monotonic()
            if not handshake and now >= deadline: raise AuthError('referee_handshake_deadline')
            if now-last_received > 60: raise AuthError('referee_receive_deadline')
            if handshake and now-last_ping >= 15:
                sock.settimeout(5);sock.sendall(frame(b'{"type":6}\x1e',masked=True));last_ping=now
            ready = select.select([sock, pipe.fileno()], [], [], .1)[0]
            if pipe.fileno() in ready:
                command = receive_packet(pipe.fileno(), time.monotonic()+5)
                if not handshake or set(command) != {'kind','value'} or command['kind'] != 'invoke':
                    raise AuthError('referee_packet_refused')
                value = command['value']
                from rolling.broker import HUB_METHODS
                import re
                if (type(value) is not dict or type(value.get('type')) is not int or value['type'] != 1
                        or value.get('target') not in HUB_METHODS | {'MakeRoom'}
                        or type(value.get('arguments')) is not list
                        or len(value['arguments']) > 64
                        or type(value.get('invocationId')) is not str
                        or not re.fullmatch(r'[1-9][0-9]{0,18}', value['invocationId'])
                        or set(value) != {'type','target','arguments','invocationId'}):
                    raise AuthError('referee_packet_refused')
                sock.settimeout(5);sock.sendall(frame(encoded(value, MAX_COMMAND)+b'\x1e',masked=True))
            if sock not in ready and not sock.pending(): continue
            opcode, raw = frames.receive(deadline=deadline if not handshake else None)
            last_received = time.monotonic()
            if opcode is None or opcode == 10: continue
            if opcode == 9:
                sock.settimeout(5);sock.sendall(frame(raw,opcode=10,masked=True));continue
            if opcode == 8:
                sock.settimeout(5);sock.sendall(frame(raw,opcode=8,masked=True))
                raise AuthError('referee_socket_closed')
            for value in records.feed(raw):
                if not handshake:
                    if value: raise AuthError('referee_handshake_refused')
                    handshake = True
                    send_packet(pipe.fileno(), {'kind':'ready'}, time.monotonic()+5)
                    continue
                kind = message(value)
                if kind == 3 and 'error' in value: value['error'] = 'referee_command_rejected'
                if kind == 7 and 'error' in value: value['error'] = 'referee_closed'
                send_packet(pipe.fileno(), {'kind':'message','value':value}, time.monotonic()+5)
                if kind == 7: return
    except AuthError as error:
        try: send_packet(pipe.fileno(), {'kind':'error','code':error.code}, time.monotonic()+.5)
        except Exception: pass
    except Exception:
        try: send_packet(pipe.fileno(), {'kind':'error','code':'referee_transport_uncertain'}, time.monotonic()+.5)
        except Exception: pass
    finally:
        if sock is not None: sock.close()
        pipe.close()


class ChildSession:
    def __init__(self, port, trust, bearer):
        boundary(port)
        self._lock = threading.Lock(); self._write = threading.Lock(); self.closed = False
        self._pidfd = None
        context = multiprocessing.get_context('spawn')
        self.pipe, child = context.Pipe(duplex=True)
        self.process = context.Process(target=_worker, args=(child,port,trust,bearer,os.getpid()),
                                       name='referee-wire',daemon=True)
        try:
            mask = signal.pthread_sigmask(signal.SIG_BLOCK,(signal.SIGINT,signal.SIGTERM))
            try:
                self.process.start()
                self._pidfd = os.pidfd_open(self.process.pid)
            finally: signal.pthread_sigmask(signal.SIG_SETMASK,mask)
        except BaseException:
            if self.process.pid is not None:
                if self.process.exitcode is None: self.process.kill()
                self.process.join(timeout=1)
            self.pipe.close();self.process.close();raise
        finally: child.close()

    def receive(self, deadline, stop=None):
        value = receive_packet(self.pipe.fileno(),deadline,stop)
        if value.get('kind') == 'error': raise AuthError(value.get('code','referee_transport_uncertain'))
        return value

    def send(self, value, deadline):
        if not self._write.acquire(timeout=max(0,deadline-time.monotonic())):
            raise AuthError('referee_deadline_exceeded')
        try:
            if self.closed: raise AuthError('referee_stopped')
            send_packet(self.pipe.fileno(),value,deadline)
        finally: self._write.release()

    def close(self):
        with self._lock:
            if self.closed:return
            mask = signal.pthread_sigmask(signal.SIG_BLOCK,(signal.SIGINT,signal.SIGTERM))
            try:
                if self.process.pid is not None:
                    try: signal.pidfd_send_signal(self._pidfd,signal.SIGKILL)
                    except ProcessLookupError: pass
                    self.process.join(timeout=1)
                    if self.process.exitcode is None: raise AuthError('referee_child_cleanup_uncertain')
                self.pipe.close();self.process.close()
                if self._pidfd is not None:os.close(self._pidfd);self._pidfd=None
                self.closed=True
            finally: signal.pthread_sigmask(signal.SIG_SETMASK,mask)
