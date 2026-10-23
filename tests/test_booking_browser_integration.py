"""Loopback HTTP integration for OAuth sessions, CSRF, and private invites."""
import http.client
import json
import sqlite3
import threading
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from poolrotater_host.bookings.repository import Repository
from poolrotater_host.bookings.server import PortalServer
from poolrotater_host.bookings.web import COOKIE, OAUTH_COOKIE, Portal


HOST = str(uuid.UUID(int=501))
INSTALLATION = str(uuid.UUID(int=502))
GENERATION = str(uuid.UUID(int=503))
OWNER = 707

class Clock:
    value = 1000.0

    def __call__(self):
        return self.value


class MockOAuth:
    def authorization_url(self, state):
        return 'https://osu.ppy.sh/oauth/authorize?state=' + state

    def exchange(self, code):
        assert code == 'invented-oauth-code'
        return {'id': OWNER, 'username': 'Invented Browser User'}


def _eligibility(host_id, alias):
    return {'host_id': host_id, 'installation_id': INSTALLATION,
            'generation': GENERATION, 'fresh': True, 'supported': True,
            'effective_limit': 4, 'capacity_used': 0,
            'observed_booking_ids': [], 'supported_presets': ['standard-auto']}


def _request(port, method, path, *, headers=None, body=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    raw = None if body is None else json.dumps(body, separators=(',', ':')).encode()
    request_headers = dict(headers or {})
    if raw is not None:
        request_headers.setdefault('Content-Type', 'application/json')
    connection.request(method, path, body=raw, headers=request_headers)
    response = connection.getresponse()
    response_headers = {}
    for key, value in response.getheaders():
        if key.casefold() == 'set-cookie':
            response_headers.setdefault('Set-Cookie', []).append(value)
        else:
            response_headers[key] = value
    result = (response.status, response_headers, response.read())
    connection.close()
    return result


def _start_portal(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    clock = Clock()
    ledger = Repository(root, clock=clock, eligibility=_eligibility)
    ledger.configure_slot('west', HOST, 'room1', 'West Pool', ['standard-auto'],
                          {'sr_min': 1, 'sr_max': 5}, enabled=True)
    portal = Portal(root, ledger, origin='https://poolrotater.com',
                    oauth=MockOAuth(), enabled=True, clock=clock)
    server = PortalServer(('127.0.0.1', 0), portal, fixture=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    return root, ledger, server, thread, clock


def _login(port):
    status, headers, _ = _request(port, 'GET', '/private-lobbies/oauth/start')
    assert status == 302
    authorization = urlsplit(headers['Location'])
    state = parse_qs(authorization.query)['state'][0]
    oauth_cookie = headers['Set-Cookie'][0].split(';', 1)[0]
    assert all(flag in headers['Set-Cookie'][0] for flag in
               ('Secure', 'HttpOnly', 'SameSite=Lax'))
    assert oauth_cookie.startswith(OAUTH_COOKIE + '=')

    status, headers, _ = _request(port, 'GET',
                                  '/private-lobbies/oauth/callback?state=' + state +
                                  '&code=invented-oauth-code',
                                  headers={'Cookie': oauth_cookie})
    assert status == 303
    cookie = next(value.split(';', 1)[0] for value in headers['Set-Cookie']
                  if value.startswith(COOKIE + '='))
    assert all(flag in next(value for value in headers['Set-Cookie']
                            if value.startswith(COOKIE + '='))
               for flag in ('Secure', 'HttpOnly', 'SameSite=Lax'))
    assert cookie.startswith(COOKIE + '=')
    return cookie


def test_loopback_http_oauth_csrf_public_slots_and_revocable_invite(tmp_path):
    root, ledger, server, thread, clock = _start_portal(tmp_path)
    try:
        port = server.server_port
        status, _, body = _request(port, 'GET', '/private-lobbies/api/me',
                                   headers={'Cookie': '__Host-poolrotater-wiki=unrelated'})
        assert status == 200 and json.loads(body)['authenticated'] is False

        cookie = _login(port)
        status, headers, body = _request(port, 'GET', '/private-lobbies/api/me',
                                         headers={'Cookie': cookie})
        me = json.loads(body)
        assert status == 200 and me['authenticated'] is True
        assert me['user']['id'] == OWNER
        csrf = me['csrf_token']

        public = _request(port, 'GET', '/private-lobbies/api/slots')[2]
        for private_field in (b'password', b'room_id', b'players', b'join_url', b'host_id', str(OWNER).encode()):
            assert private_field not in public

        request_id = str(uuid.uuid4())
        reserve_body = {'request_id': request_id, 'slot_id': 'west', 'preset': 'standard-auto',
                        'settings': {}, 'designated_friend_id': None}
        common_headers = {'Cookie': cookie, 'Content-Type': 'application/json'}
        status, _, _ = _request(port, 'POST', '/private-lobbies/api/reserve',
                                headers=common_headers, body=reserve_body)
        assert status == 403
        mutation_headers = {**common_headers, 'Origin': 'https://poolrotater.com',
                            'X-CSRF-Token': csrf}
        status, _, body = _request(port, 'POST', '/private-lobbies/api/reserve',
                                   headers=mutation_headers, body=reserve_body)
        assert status == 201
        booking = json.loads(body)['booking']
        assert booking['role'] == 'owner'
        lease, = ledger.leases(HOST, INSTALLATION, GENERATION)

        observed = {'sequence': 0, 'phase': 'active', 'room_id': 8899,
                    'incarnation': 'native-incarnation',
                    'roster': [{'user_id': OWNER, 'username': 'Invented Browser User'}],
                    'roster_known': True, 'updated_at': clock(), 'ready': True,
                    'closed': False, 'effective_settings': booking['settings'],
                    'cleanup_complete': False, 'ownership_known': True,
                    'operation_receipts': [], 'installation_id': INSTALLATION,
                    'generation': GENERATION,
                        'session_generation': lease['session_generation'], 'alias': 'room1'}
        ledger.observe(HOST, booking['id'], observed)

        status, _, invite_body = _request(port, 'POST', '/private-lobbies/api/invite',
                                          headers=mutation_headers,
                                          body={'booking_id': booking['id']})
        assert status == 200
        invite_url = json.loads(invite_body)['invite_url']
        invite = parse_qs(urlsplit(invite_url).query)['invite'][0]
        clock.value += 10
        status, _, next_invite_body = _request(port, 'POST', '/private-lobbies/api/invite',
                                               headers=mutation_headers,
                                               body={'booking_id': booking['id']})
        assert status == 200
        next_invite = parse_qs(urlsplit(json.loads(next_invite_body)['invite_url']).query)['invite'][0]
        revoked_status, _, _ = _request(port, 'GET', '/private-lobbies/join?invite=' + invite)
        assert revoked_status == 403
        status, _, join_body = _request(port, 'GET', '/private-lobbies/join?invite=' + next_invite)
        joined = json.loads(join_body)['joining']
        assert status == 200
        assert joined == {'booking_id': booking['id'], 'phase': 'active',
                          'join_url': 'https://osu.ppy.sh/multiplayer/rooms/8899',
                          'password': booking['password']}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_invite_html_escapes_password_uses_fixed_join_origin_and_ends_cleanly(tmp_path):
    root, ledger, server, thread, clock = _start_portal(tmp_path)
    try:
        port = server.server_port
        cookie = _login(port)
        me = json.loads(_request(port, 'GET', '/private-lobbies/api/me',
                                 headers={'Cookie': cookie})[2])
        mutation_headers = {'Cookie': cookie, 'Content-Type': 'application/json',
                            'Origin': 'https://poolrotater.com',
                            'X-CSRF-Token': me['csrf_token']}
        reserve_body = {'request_id': str(uuid.uuid4()), 'slot_id': 'west',
                        'preset': 'standard-auto', 'settings': {},
                        'designated_friend_id': None}
        status, _, body = _request(port, 'POST', '/private-lobbies/api/reserve',
                                   headers=mutation_headers, body=reserve_body)
        assert status == 201
        booking = json.loads(body)['booking']
        lease, = ledger.leases(HOST, INSTALLATION, GENERATION)
        observation = {'sequence': 0, 'phase': 'active', 'room_id': 8899,
                       'incarnation': 'native-incarnation',
                       'roster': [{'user_id': OWNER, 'username': 'Invented Browser User'}],
                       'roster_known': True, 'updated_at': clock(), 'ready': True,
                       'closed': False, 'effective_settings': booking['settings'],
                       'cleanup_complete': False, 'ownership_known': True,
                       'operation_receipts': [], 'installation_id': INSTALLATION,
                       'generation': GENERATION,
                       'session_generation': lease['session_generation'], 'alias': 'room1'}
        ledger.observe(HOST, booking['id'], observation)

        injected_password = '<script>&"private</script>'
        with sqlite3.connect(ledger.path) as db:
            db.execute('UPDATE bookings SET password=? WHERE id=?',
                       (injected_password, booking['id']))
        status, _, invite_body = _request(port, 'POST', '/private-lobbies/api/invite',
                                          headers=mutation_headers,
                                          body={'booking_id': booking['id']})
        assert status == 200
        invite = parse_qs(urlsplit(json.loads(invite_body)['invite_url']).query)['invite'][0]
        status, headers, html = _request(port, 'GET', '/private-lobbies/join?invite=' + invite,
                                         headers={'Accept': 'text/html'})
        page = html.decode()
        assert status == 200
        assert 'text/html' in headers['Content-Type']
        assert 'href="https://osu.ppy.sh/multiplayer/rooms/8899"' in page
        assert '&lt;script&gt;&amp;&quot;private&lt;/script&gt;' in page
        assert injected_password not in page
        assert 'http://evil.example' not in page

        # Issuing a fresh invitation revokes the previous token. The revoked
        # response must contain no room credential or join destination.
        clock.value += 10
        status, _, next_body = _request(port, 'POST', '/private-lobbies/api/invite',
                                        headers=mutation_headers,
                                        body={'booking_id': booking['id']})
        assert status == 200
        next_invite = parse_qs(urlsplit(json.loads(next_body)['invite_url']).query)['invite'][0]
        status, _, revoked_body = _request(port, 'GET', '/private-lobbies/join?invite=' + invite,
                                           headers={'Accept': 'text/html'})
        assert status == 403
        assert injected_password.encode() not in revoked_body
        assert b'osu.ppy.sh/multiplayer/rooms/' not in revoked_body

        ended = {**observation, 'sequence': 1, 'phase': 'ended', 'room_id': None,
                 'incarnation': None, 'roster': [], 'roster_known': True,
                 'updated_at': clock(), 'ready': False, 'closed': True,
                 'cleanup_complete': True, 'ownership_known': True}
        ledger.observe(HOST, booking['id'], ended)
        status, _, ended_html = _request(port, 'GET',
            '/private-lobbies/join?invite=' + next_invite, headers={'Accept': 'text/html'})
        # Confirmed cleanup revokes and removes joining grants, including the
        # last invitation. An old URL no longer exposes even its session state.
        assert status == 403
        assert b'osu.ppy.sh/multiplayer/rooms/' not in ended_html
        assert injected_password.encode() not in ended_html
        status, _, ended_json = _request(port, 'GET',
            '/private-lobbies/join?invite=' + next_invite)
        assert status == 403
        assert json.loads(ended_json)['error'] == 'booking_invite_expired'
        assert ledger.join_info(booking['id']) == {
            'booking_id': booking['id'], 'phase': 'ended',
            'join_url': None, 'password': None}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_browser_configure_carries_latest_host_settings_revision(tmp_path):
    _, ledger, server, thread, clock = _start_portal(tmp_path)
    try:
        port = server.server_port
        cookie = _login(port)
        me = json.loads(_request(port, 'GET', '/private-lobbies/api/me',
                                 headers={'Cookie': cookie})[2])
        headers = {'Cookie': cookie, 'Content-Type': 'application/json',
                   'Origin': 'https://poolrotater.com', 'X-CSRF-Token': me['csrf_token']}
        status, _, body = _request(port, 'POST', '/private-lobbies/api/reserve',
                                   headers=headers, body={'request_id': str(uuid.uuid4()),
                                       'slot_id': 'west', 'preset': 'standard-auto',
                                       'settings': {}, 'designated_friend_id': None})
        assert status == 201
        booking = json.loads(body)['booking']
        lease, = ledger.leases(HOST, INSTALLATION, GENERATION)
        observed = {'sequence': 0, 'phase': 'active', 'room_id': 8899,
                    'incarnation': 'native-incarnation', 'roster': [],
                    'roster_known': True, 'updated_at': clock(), 'ready': True,
                    'closed': False, 'effective_settings': booking['settings'],
                    'cleanup_complete': False, 'ownership_known': True,
                    'operation_receipts': [], 'installation_id': INSTALLATION,
                    'generation': GENERATION,
                    'session_generation': lease['session_generation'], 'alias': 'room1',
                    'settings_revision': 7}
        ledger.observe(HOST, booking['id'], observed)
        status, _, session_body = _request(port, 'GET',
            '/private-lobbies/api/session?id=' + booking['id'], headers={'Cookie': cookie})
        assert status == 200
        current = json.loads(session_body)['booking']
        assert current['settings_revision'] == 7
        status, _, operation_body = _request(port, 'POST', '/private-lobbies/api/action',
            headers=headers, body={'request_id': str(uuid.uuid4()), 'booking_id': booking['id'],
                'expected_revision': current['revision'], 'action': 'configure',
                'payload': {'sr_min': 2}})
        assert status == 202
        operation = json.loads(operation_body)['operation']
        command, = ledger.pending(HOST)
        assert command['operation_id'] == operation['id']
        assert command['expected_settings_revision'] == 7
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
