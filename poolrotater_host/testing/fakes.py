"""Deterministic in-process referee and loopback-only central service test double.

This is a fault-injection harness, not the v7 production sync protocol.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading


class FakeReferee:
    def __init__(self):
        self.rooms = {}
        self.next_id = 1
        self.fail_next = None

    def create(self, alias):
        if self.fail_next:
            reason, self.fail_next = self.fail_next, None
            raise ConnectionError(reason)
        if len(self.rooms) >= 4:
            raise ValueError('fixture account room limit')
        room = self.next_id; self.next_id += 1
        self.rooms[room] = {'alias': alias, 'state': 'idle', 'rounds': 0}
        return room

    def play(self, room_id, map_id):
        room = self.rooms[room_id]
        if room['state'] != 'idle': raise ValueError('room not idle')
        room['state'] = 'playing'
        room['map_id'] = map_id
        room['rounds'] += 1
        room['state'] = 'idle'

    def close(self, room_id):
        del self.rooms[room_id]


class FakeNetwork:
    def __init__(self):
        self.tokens = {'fixture-host-a': 'a', 'fixture-host-b': 'b'}
        self.status = {}
        self.fail_next = 0
        self.lock = threading.Lock()

    @contextmanager
    def serve(self):
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def reply(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)

            def do_GET(self):
                if self.path != '/v1/capabilities': return self.reply(404, {})
                self.reply(200, {'protocol': 1, 'test_double': True, 'rating_ingestion': False})

            def do_POST(self):
                if self.path != '/v1/hosts/self/heartbeat': return self.reply(404, {})
                host = state.tokens.get(self.headers.get('Authorization', '').removeprefix('Bearer '))
                if host is None: return self.reply(401, {})
                try:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 65536: return self.reply(413, {})
                    value = json.loads(self.rfile.read(size))
                    if not isinstance(value, dict) or set(value) != {'sequence', 'rooms'}: raise ValueError()
                    if type(value['sequence']) is not int or value['sequence'] < 0 or not isinstance(value['rooms'], list): raise ValueError()
                except (ValueError, UnicodeError): return self.reply(400, {})
                with state.lock:
                    if state.fail_next:
                        state.fail_next -= 1
                        return self.reply(503, {'retryable': True})
                    previous = state.status.get(host)
                    if previous is None or value['sequence'] > previous['sequence']:
                        state.status[host] = value
                    self.reply(200, {'host': host, 'sequence': state.status[host]['sequence']})

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try: yield f'http://127.0.0.1:{server.server_port}'
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
