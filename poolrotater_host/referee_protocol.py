"""Bounded JSON SignalR and RFC6455 text framing; no sockets or destinations here."""
import json
import math
import os
import re
import struct
import time

from .layout import unique_fields
from .oauth import AuthError

MAX_MESSAGE = 1024**2
MAX_COMMAND = 32768


def encoded(value, maximum=MAX_MESSAGE):
    try: raw = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    except Exception: raise AuthError('referee_message_refused') from None
    if len(raw) > maximum: raise AuthError('referee_message_too_large')
    return raw


def decoded(raw):
    if type(raw) is not bytes or len(raw) > MAX_MESSAGE: raise AuthError('referee_message_too_large')
    def finite(raw):
        value = float(raw)
        if not math.isfinite(value): raise ValueError
        return value
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_fields,
                           parse_float=finite, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(value) is not dict: raise ValueError
        return value
    except Exception: raise AuthError('referee_message_refused') from None


class Records:
    def __init__(self): self.buffer = b''; self.deadline = None

    def feed(self, raw):
        self.check()
        if len(self.buffer) + len(raw) > MAX_MESSAGE: raise AuthError('referee_message_too_large')
        self.buffer += raw
        parts = self.buffer.split(b'\x1e'); self.buffer = parts.pop()
        if len(parts) > 256: raise AuthError('referee_record_flood')
        if self.buffer and (self.deadline is None or parts): self.deadline = time.monotonic() + 5
        if not self.buffer: self.deadline = None
        return [decoded(part) for part in parts]

    def check(self):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise AuthError('referee_record_deadline')


def frame(raw, *, opcode=1, masked=False, final=True):
    if type(raw) is not bytes or len(raw) > MAX_MESSAGE: raise AuthError('referee_message_too_large')
    if opcode not in (0, 1, 8, 9, 10) or opcode >= 8 and (not final or len(raw) > 125):
        raise AuthError('referee_frame_refused')
    length = len(raw)
    head = bytes([(128 if final else 0) | opcode])
    mark = 128 if masked else 0
    if length < 126: head += bytes([mark | length])
    elif length < 65536: head += bytes([mark | 126]) + struct.pack('!H', length)
    else: head += bytes([mark | 127]) + struct.pack('!Q', length)
    if masked:
        mask = os.urandom(4)
        return head + mask + bytes(value ^ mask[i % 4] for i, value in enumerate(raw))
    return head + raw


class Frames:
    def __init__(self, sock, *, expect_masked, read=None):
        self.sock, self.expect_masked = sock, expect_masked
        self.read = sock.recv if read is None else read
        self.parts = b''; self.fragmented = False; self.fragment_deadline = None

    def _exact(self, size, deadline):
        raw = bytearray()
        while len(raw) < size:
            left = deadline - time.monotonic()
            if left <= 0: raise AuthError('referee_frame_deadline')
            self.sock.settimeout(left)
            part = self.read(size - len(raw))
            if not part: raise AuthError('referee_socket_closed')
            if time.monotonic() >= deadline: raise AuthError('referee_frame_deadline')
            raw.extend(part)
        return bytes(raw)

    def check(self):
        if self.fragment_deadline is not None and time.monotonic() >= self.fragment_deadline:
            raise AuthError('referee_frame_deadline')

    def receive(self, *, deadline=None):
        self.check()
        deadline = time.monotonic() + 5 if deadline is None else deadline
        if self.fragment_deadline is not None: deadline = min(deadline, self.fragment_deadline)
        a, b = self._exact(2, deadline)
        final, opcode, masked, size = bool(a & 128), a & 15, bool(b & 128), b & 127
        if a & 112 or masked != self.expect_masked or opcode not in (0, 1, 8, 9, 10):
            raise AuthError('referee_frame_refused')
        if size == 126:
            size = struct.unpack('!H', self._exact(2, deadline))[0]
            if size < 126: raise AuthError('referee_frame_refused')
        elif size == 127:
            size = struct.unpack('!Q', self._exact(8, deadline))[0]
            if size < 65536: raise AuthError('referee_frame_refused')
        if size > MAX_MESSAGE: raise AuthError('referee_message_too_large')
        if opcode >= 8 and (not final or size > 125): raise AuthError('referee_frame_refused')
        mask = self._exact(4, deadline) if masked else None
        raw = self._exact(size, deadline)
        if mask: raw = bytes(value ^ mask[i % 4] for i, value in enumerate(raw))
        if opcode >= 8:
            if opcode == 8:
                if len(raw) == 1: raise AuthError('referee_frame_refused')
                if raw:
                    code = struct.unpack('!H', raw[:2])[0]
                    if not (1000 <= code <= 1014 and code not in (1004, 1005, 1006) or 3000 <= code <= 4999):
                        raise AuthError('referee_frame_refused')
                    try: raw[2:].decode('utf-8')
                    except UnicodeError: raise AuthError('referee_frame_refused') from None
            return opcode, raw
        if opcode == 0 and not self.fragmented or opcode == 1 and self.fragmented:
            raise AuthError('referee_frame_refused')
        if len(self.parts) + len(raw) > MAX_MESSAGE: raise AuthError('referee_message_too_large')
        self.parts += raw
        if not final:
            self.fragmented = True
            if self.fragment_deadline is None: self.fragment_deadline = time.monotonic() + 5
            return None, None
        raw, self.parts = self.parts, b''
        self.fragmented = False; self.fragment_deadline = None
        try: raw.decode('utf-8')
        except UnicodeError: raise AuthError('referee_frame_refused') from None
        return 1, raw


def message(value):
    """Only JSON invocation events, completion, ping and close are supported."""
    kind = value.get('type')
    if type(kind) is not int or kind not in (1, 3, 6, 7): raise AuthError('referee_message_refused')
    if kind == 1:
        if (type(value.get('target')) is not str or not re.fullmatch(r'[A-Za-z][A-Za-z0-9]{0,99}', value['target'])
                or type(value.get('arguments')) is not list or len(value['arguments']) > 64
                or 'invocationId' in value or 'streamIds' in value):
            raise AuthError('referee_message_refused')
    if kind == 3:
        if (type(value.get('invocationId')) is not str or not re.fullmatch(r'[1-9][0-9]{0,18}', value['invocationId'])
                or 'result' in value and 'error' in value
                or 'error' in value and (type(value['error']) is not str or len(value['error']) > 8192)):
            raise AuthError('referee_message_refused')
    if kind == 7 and ('allowReconnect' in value and type(value['allowReconnect']) is not bool
                      or 'error' in value and type(value['error']) is not str):
        raise AuthError('referee_message_refused')
    return kind
