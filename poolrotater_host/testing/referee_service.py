"""Invented fixed-path TLS/JSON SignalR service; never binds a public interface."""
import base64
import hashlib
import os
import select
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit

from ..oauth import AuthError
from ..referee_contract import NEGOTIATE
from ..referee_protocol import Frames, Records, encoded, frame
from ..referee_wire import GUID


class RefereeFixture:
    def __init__(self, backend):
        if os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1': raise AuthError('offline_fixture_required')
        self.backend = backend
        self.provider = backend.provider
        self._lock = threading.RLock()
        self._write = threading.Lock()
        self._tokens = {}
        self.socket = None
        self.connections = 0
        self.commands = []
        self.upgrades = []
        self.handshake_delay = 0
        self.bad_accept = False
        self.handshake_reply = {}
        self.completion_error = None
        self.creation_error_after_commit = None
        self.completion_delay = 0
        self.stopped = threading.Event()
        backend.shared = self

    def reply(self, handler, code, value):
        raw = encoded(value)
        handler.send_response(code);handler.send_header('Content-Type','application/json')
        handler.send_header('Content-Length',str(len(raw)));handler.end_headers()
        handler.wfile.write(raw)

    def handle(self, handler):
        parts = urlsplit(handler.path)
        if parts.path not in ('/referee','/referee/negotiate'):return False
        bearer = handler.headers.get('Authorization','').removeprefix('Bearer ')
        try:self.backend.authenticate(bearer)
        except AuthError:self.reply(handler,401,{});return True
        if handler.command == 'POST' and handler.path == NEGOTIATE:
            with self._lock:
                self._tokens = {key:value for key,value in self._tokens.items() if value[1]>time.monotonic()}
                if len(self._tokens)>=32:self.reply(handler,429,{});return True
                token = 'invented-connection-'+os.urandom(16).hex()
                self._tokens[token] = (bearer,time.monotonic()+20)
            self.reply(handler,200,{'negotiateVersion':1,'connectionId':'invented-public-id',
                'connectionToken':token,'availableTransports':[{'transport':'WebSockets','transferFormats':['Text']}]})
            return True
        params = parse_qs(parts.query)
        with self._lock: ticket = self._tokens.pop(params.get('id',[''])[0],None)
        if (handler.command != 'GET' or parts.path != '/referee' or set(params) != {'id'}
                or len(params['id']) != 1 or ticket is None or ticket[0] != bearer or ticket[1]<time.monotonic()
                or handler.headers.get('Upgrade','').lower()!='websocket'
                or handler.headers.get('Connection','').lower()!='upgrade'
                or handler.headers.get('Sec-WebSocket-Version')!='13'):
            self.reply(handler,400,{});return True
        key = handler.headers.get('Sec-WebSocket-Key','')
        try:
            if len(base64.b64decode(key,validate=True)) != 16:raise ValueError
        except Exception:self.reply(handler,400,{});return True
        accept = base64.b64encode(hashlib.sha1((key+GUID).encode('ascii')).digest()).decode('ascii')
        handler.send_response(101);handler.send_header('Upgrade','websocket');handler.send_header('Connection','Upgrade')
        handler.send_header('Sec-WebSocket-Accept','invalid' if self.bad_accept else accept);handler.end_headers()
        self.upgrades.append({'path':handler.path,'bearer':bearer})
        sock = handler.connection
        try:
            frames, records = Frames(sock,expect_masked=True),Records()
            opcode, raw = frames.receive()
            values = records.feed(raw) if opcode==1 else []
            if values != [{'protocol':'json','version':1}]:raise AuthError('invented_handshake_refused')
            if self.stopped.wait(self.handshake_delay):return True
            with self._lock:
                self.socket = sock
                if self.connections:self.backend.reconnect_authenticated(bearer)
                else:self.backend.connect_authenticated(bearer)
                self.connections += 1
            self.send(self.handshake_reply)
            while not self.stopped.is_set():
                frames.check();records.check()
                if not select.select([sock],[],[],.1)[0] and not sock.pending():continue
                opcode, raw = frames.receive()
                if opcode is None or opcode==10:continue
                if opcode==8:break
                if opcode==9:
                    with self._write:sock.sendall(frame(raw,opcode=10))
                    continue
                for value in records.feed(raw):
                    if value.get('type')==6:self.send({'type':6});continue
                    self.commands.append(value)
                    method,args,identity=value['target'],value['arguments'],value['invocationId']
                    if self.completion_error is not None:
                        self.send({'type':3,'invocationId':identity,'error':self.completion_error});continue
                    if method=='MakeRoom':
                        body=args[0]
                        result=self.backend.make_authenticated(bearer,body['name'],body['beatmap_id'],body['ruleset_id'])
                        if self.creation_error_after_commit is not None:
                            self.send({'type':3,'invocationId':identity,'error':self.creation_error_after_commit})
                            continue
                    else:result=self.backend.invoke_authenticated(bearer,method,args)
                    # Official RoomJoinedResponse has no top-level creator.
                    # Keep explicit corrupt-owner faults for refusal coverage.
                    if method in ('MakeRoom','JoinRoom') and type(result) is dict and result.get('user_id')==99:
                        result={k:v for k,v in result.items() if k!='user_id'}
                    if self.stopped.wait(self.completion_delay):return True
                    self.send({'type':3,'invocationId':identity,'result':result})
        except Exception:
            # Lost completion means transport loss, never a fabricated error
            # completion for an operation that may already have mutated state.
            pass
        finally:
            with self._lock:
                if self.socket is sock:self.socket=None;self.backend.connected=False
            try:sock.shutdown(socket.SHUT_RDWR)
            except OSError:pass
        return True

    def send(self, value):
        with self._write:
            sock=self.socket
            if sock is None:raise AuthError('invented_referee_disconnected')
            sock.settimeout(5);sock.sendall(frame(encoded(value)+b'\x1e'))

    def route(self,name,payload):
        if name in ('HubDisconnected','HubReconnected'):return
        self.send({'type':1,'target':name,'arguments':[payload]})

    def drop(self):
        with self._lock:
            sock=self.socket
            if sock is not None:
                try:sock.shutdown(socket.SHUT_RDWR)
                except OSError:pass

    def stop(self):
        self.stopped.set();self.drop()
