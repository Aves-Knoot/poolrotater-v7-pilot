"""Invented authenticated referee/API payloads behind real loopback TLS.

Authorization, room evidence and typed API calls cross real loopback TLS. Referee
completions/events cross the TLS/WebSocket fixture using this model. All identities
and data are invented, and no upstream URL or credential input is supported.
"""
from collections import deque
import copy
import os
import re
from urllib.parse import parse_qs, urlsplit

from ..oauth import AuthError, SCOPES
from .runtime import RuntimeApi, RuntimeHub
from .provider_https import TLSProvider


class InventedRoomService(RuntimeHub):
    def __init__(self, provider):
        if os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1' or type(provider) is not TLSProvider:
            raise AuthError('qualified_room_fixture_required')
        super().__init__()
        self.provider = provider
        self.connected = False
        self.api = RuntimeApi(self)
        self.faults = {}
        self.authenticated_calls = []
        provider.api_handler = self.api_request
        from .referee_service import RefereeFixture
        self.wire = RefereeFixture(self)
        provider.referee_handler = self.wire

    def api_request(self, method, path, body, bearer, headers):
        """Invented API payloads behind the actual TLS/parser/deadline path."""
        parts = urlsplit(path)
        target = parts.path.removeprefix('/api/v2')
        params = parse_qs(parts.query)
        operation = None
        args = []
        kwargs = {}
        match = re.fullmatch(r'/chat/channels/([0-9]+)/messages', target)
        if match and method in ('GET', 'POST'):
            operation = 'chat_messages' if method == 'GET' else 'chat_send'
            args = [int(match[1])] + ([body['message']] if method == 'POST' else [])
        match = re.fullmatch(r'/chat/channels/([0-9]+)/users/([0-9]+)', target)
        if match and method == 'PUT':
            self.provider.calls.append({'method':method, 'path':path, 'form':body,
                                        'bearer':bearer, 'headers':dict(headers)})
            return 403, {}
        match = re.fullmatch(r'/chat/channels/([0-9]+)', target)
        if match and method == 'GET': operation, args = 'chat_join', [int(match[1]), 99]
        if method == 'POST' and target == '/chat/ack': operation = 'chat_ack'
        match = re.fullmatch(r'/(beatmaps|beatmapsets)/([0-9]+)', target)
        if match and method == 'GET': operation, args = match[1][:-1], [int(match[2])]
        match = re.fullmatch(r'/users/([0-9]+)/(osu|mania|taiko|fruits|scores/best)', target)
        if match and method == 'GET':
            operation, args = ('user_top_scores' if match[2] == 'scores/best' else 'user'), [int(match[1])]
            kwargs = {'mode': params.get('mode', [match[2]])[0]}
        match = re.fullmatch(r'/rooms/([0-9]+)/playlist/([0-9]+)/scores', target)
        if match and method == 'GET': operation, args = 'room_item_scores', [int(match[1]), int(match[2])]
        if operation is None: return None
        self.provider.calls.append({'method':method, 'path':path, 'form':body,
                                    'bearer':bearer, 'headers':dict(headers)})
        if self.provider.access.get(bearer) != (99, 123, SCOPES, False): return 401, {}
        value = self.api_authenticated(bearer, operation, args, kwargs)
        if operation == 'chat_ack': value = {'silences':[]}
        if operation == 'room_item_scores': value = {'scores':[]}
        if operation == 'chat_join':
            value = {'channel': {**value, 'current_user_attributes': {'can_message': True}}, 'users': []}
        return 200, value

    def fault(self, operation, when='after'):
        if when not in ('before', 'after', 'bad-owner', 'no-referee', 'generation'):
            raise ValueError('invalid invented room fault')
        self.faults.setdefault(operation, deque()).append(when)

    def authenticate(self, bearer):
        if self.provider.access.get(bearer) != (99, 123, SCOPES, False):
            raise AuthError('invented_room_identity_refused')

    def connect_authenticated(self, bearer):
        self.authenticate(bearer)
        self.connected = True
        self.authenticated_calls.append('connect')

    def reconnect_authenticated(self, bearer):
        self.authenticate(bearer)
        self.connected = False
        if self.shared: self.shared.route('HubDisconnected', {})
        self._connect_generation += 1
        self.connected = True
        self.authenticated_calls.append('reconnect')
        if self.shared: self.shared.route('HubReconnected', {})

    def _call(self, bearer, operation, function):
        from bot.hub import HubDisconnected, HubInvocationUncertain
        self.authenticate(bearer)
        if not self.connected: raise HubDisconnected('invented_referee_disconnected')
        fault = self.faults[operation].popleft() if self.faults.get(operation) else None
        self.authenticated_calls.append(operation)
        if fault == 'before': raise HubInvocationUncertain('invented_reply_lost')
        value = function()
        if fault == 'after': raise HubInvocationUncertain('invented_reply_lost')
        if fault == 'generation': raise HubInvocationUncertain('invented_generation_lost')
        if fault == 'bad-owner': value['user_id'] = 100
        if fault == 'no-referee': value['referees'] = []
        return value

    def make_authenticated(self, bearer, *args, **kwargs):
        def make():
            value = super(InventedRoomService, self).make_room(*args, **kwargs)
            value['user_id'] = self.rooms[value['room_id']]['user_id'] = 99
            self._evidence(value['room_id'], active=True)
            return value
        return self._call(bearer, 'MakeRoom', make)

    def invoke_authenticated(self, bearer, method, args, **kwargs):
        def invoke():
            value = super(InventedRoomService, self).invoke(method, args, **kwargs)
            self._evidence(int(args[0]), active=method != 'CloseRoom')
            return value
        return self._call(bearer, method, invoke)

    def _evidence(self, room_id, *, active):
        detail = {'id': room_id, 'user_id': 99, 'type': 'head_to_head',
                  'active': active, 'ends_at': None if active else '2000-01-01T00:00:00Z'}
        self.provider.details[str(room_id)] = (200, detail)
        self.provider.rooms = [copy.deepcopy(row) for status, row in self.provider.details.values()
                               if status == 200 and row['active']]

    def api_authenticated(self, bearer, name, args, kwargs):
        self.authenticate(bearer)
        self.authenticated_calls.append('api.' + name)
        if name == 'usage_report': return 'invented provider fixture'
        if name == 'room_item_scores': return []
        return getattr(self.api, name)(*args, **kwargs)
