"""Existing OsuApi typed methods on the provider's owned HTTPS transport.

No httpx client or legacy limiter is constructed. Importing this module stays
stdlib-only for the preview wheel; typed methods are loaded in the full runtime.
"""
import math
import logging
import re
import threading

from .oauth import AuthError
from .provider_api_contract import ID, prepare_api


class ProviderKeepaliveUnavailable(AuthError):
    """Only the idempotent chat ACK may tolerate known dispatched uncertainty.

    The owned transport has already recorded uncertainty and its persistent
    cooldown. This class does not grant an immediate retry or recover a ledger.
    """


class ProviderApiClient:
    def __init__(self, auth):
        from .provider_rooms import _authority
        _authority(auth)
        self.auth = auth
        self.cancel_pending = threading.Event()
        self._profile_cache_lock = threading.Lock()
        self._profile_cache_seconds = 21600.
        self._profile_cache = {}
        self._context = threading.local()

    def __getattr__(self, name):
        # Reuse the fixed typed endpoints/cache methods, never the legacy
        # constructor, transport, retry loop, liveness probe or limiter.
        from bot.api import OsuApi
        if name in ('_profile_cache_get', '_profile_cache_put'):
            return getattr(OsuApi, name).__get__(self, type(self))
        raise AttributeError(name)

    def call(self, name, *args, **kwargs):
        from bot.api import OsuApi
        from rolling.adapters import Api
        from .provider_rooms import _bearer
        if name not in Api.METHODS or name in ('room', 'room_probe'):
            raise AuthError('provider_api_method_refused')
        check = kwargs.pop('_authority_check', None)
        if check is not None and not callable(check): raise AuthError('room_api_authority_changed')
        # Hold the lifetime mutex through pacing/I/O: owner exit cannot retire
        # verified authority while a bounded exchange is still in progress.
        with self.auth._mutex:
            _bearer(self.auth)  # Cache hits and local settings require authority too.
            previous = getattr(self._context, 'check', None)
            self._context.check = check
            try:
                if name == 'usage_report': return self.usage_report()
                if name == 'configure_rate_limit': return self.configure_rate_limit(*args, **kwargs)
                if name == 'chat_join': return self.chat_join(*args, **kwargs)
                if name == 'configure_profile_cache':
                    return self.configure_profile_cache(*args, **kwargs)
                if check is not None and not check(): raise AuthError('room_api_authority_changed')
                return getattr(OsuApi, name).__get__(self, type(self))(*args, **kwargs)
            finally: self._context.check = previous

    def _boundary(self, *, mutation):
        if not self.auth._verified or self.auth._active is None:
            raise AuthError('verified_owner_required')
        guard = getattr(self.auth, '_room_guard', self.auth._local_guard)
        if guard is not None: guard()
        if mutation and self.cancel_pending.is_set(): raise AuthError('room_api_cancelled')
        check = getattr(self._context, 'check', None)
        if check is not None and not check(): raise AuthError('room_api_authority_changed')

    def _req(self, method, path, **kwargs):
        from .provider_rooms import _bearer
        before = kwargs.pop('_before_request', None)
        if set(kwargs) - {'params', 'json'}: raise AuthError('provider_api_arguments_refused')
        params, body = kwargs.get('params'), kwargs.get('json')
        full = '/api/v2' + path
        # Validate before a refresh could make a request on behalf of bad input.
        prepare_api(method, full, bearer='request-validation', params=params, json_body=body)
        mutation = method != 'GET'
        keepalive = method == 'POST' and full == '/api/v2/chat/ack'
        if before is not None:
            if not callable(before): raise AuthError('provider_api_arguments_refused')
            try: before()
            except Exception: raise AuthError('provider_api_preparation_failed') from None
        for attempt in range(2 if not mutation else 1):
            self._boundary(mutation=mutation)
            token = _bearer(self.auth)
            with self.auth.transport.api_operation(method, full, bearer=token,
                    params=params, json_body=body) as permit:
                # No _bearer here: refresh would recursively acquire the same
                # durable pacing lease. Recheck mutable authority after waiting.
                self._boundary(mutation=mutation)
                try:
                    code, value = permit.api_request(method, full, bearer=token,
                                                    params=params, json_body=body)
                except AuthError as error:
                    if (keepalive and permit.used and error.code in
                            ('provider_deadline_exceeded', 'provider_transport_uncertain')):
                        raise ProviderKeepaliveUnavailable(error.code, http_status=error.http_status) from None
                    if mutation and permit.used:
                        raise AuthError('provider_api_mutation_uncertain') from None
                    raise
                if mutation and 500 <= code < 600:
                    self.auth.transport.gate.uncertain()
                    if keepalive:
                        raise ProviderKeepaliveUnavailable('provider_chat_keepalive_unavailable', http_status=code)
                    raise AuthError('provider_api_mutation_uncertain')
                if code in (200, 201, 204):
                    sequence = bool(re.fullmatch(r'/api/v2/users/' + ID + r'/scores/best', full)
                                    or method == 'GET' and bool(re.fullmatch(r'/api/v2/chat/channels/' + ID + r'/messages', full)))
                    invalid = (code == 201 and not mutation or
                               code != 204 and not self._valid_reply(full, method, value, sequence, params) or
                               code == 204 and full != '/api/v2/chat/ack')
                    if invalid:
                        if mutation:
                            self.auth.transport.gate.uncertain()
                            raise AuthError('provider_api_mutation_uncertain')
                        raise AuthError('provider_api_reply_refused')
            if code == 401:
                if not mutation and attempt == 0:
                    self.auth.invalidate_access_token(token)
                    continue
                raise AuthError('provider_api_unauthorized', http_status=code)
            if code == 429: raise AuthError('provider_api_rate_limited', http_status=code)
            if code == 404 and not mutation: return None
            if code not in (200, 201, 204):
                operation = 'chat_channel_access' if re.fullmatch(r'/api/v2/chat/channels/' + ID, full) else 'typed_api'
                logging.getLogger(__name__).warning('provider %s %s refused: HTTP %d', method, operation, code)
                raise AuthError('provider_api_unavailable', http_status=code)
            if code == 204: return {}
            return value

    @staticmethod
    def _valid_reply(path, method, value, sequence, params):
        if type(value) is not (list if sequence else dict): return False
        if sequence: return len(value) <= params['limit']
        channel_match = re.fullmatch(r'/api/v2/chat/channels/(' + ID + r')', path)
        if channel_match:
            channel = value.get('channel')
            if type(channel) is not dict or type(channel.get('channel_id')) is not int or channel['channel_id'] != int(channel_match[1]):
                return False
            attributes = channel.get('current_user_attributes')
            return type(attributes) is dict and type(attributes.get('can_message')) is bool
        match = re.fullmatch(r'/api/v2/(beatmaps|beatmapsets)/(' + ID + r')', path)
        if not match: match = re.fullmatch(r'/api/v2/users/(' + ID + r')/(osu|taiko|fruits|mania)', path)
        if match:
            expected = int(match[2] if match[1] in ('beatmaps', 'beatmapsets') else match[1])
            return type(value.get('id')) is int and value['id'] == expected
        match = re.fullmatch(r'/api/v2/chat/channels/(' + ID + r')/messages', path)
        if match:
            channel = int(match[1])
            message_id = value.get('message_id')
            return (type(message_id) is int and 0 < message_id < 10**19
                    and ('channel_id' not in value or type(value['channel_id']) is int and value['channel_id'] == channel))
        if re.fullmatch(r'/api/v2/rooms/' + ID + r'/playlist/' + ID + r'/scores', path):
            return type(value.get('scores')) is list and len(value['scores']) <= 1000
        return True

    def chat_join(self, channel_id, user_id):
        """Check referee chat membership established by MakeRoom/JoinRoom.

        Lazer multiplayer membership is not joined through the generic public
        channel PUT endpoint. GET returns a channel wrapper and checks membership
        upstream; never bypass a refusal or replay a room mutation to repair it.
        """
        if type(user_id) is not int or user_id != self.auth._active:
            raise AuthError('provider_api_user_mismatch')
        if type(channel_id) is not int or not 0 < channel_id < 10**19:
            raise AuthError('provider_api_endpoint_refused')
        result = self._req('GET', f'/chat/channels/{channel_id}')
        if result is None:
            # Preserve OsuApi's native missing-channel contract. Startup still
            # requires membership; bounded transport recovery retries a False
            # join as HubDisconnected without replaying mutations. Other HTTP
            # denials retain their typed failure and status.
            return None
        channel = result['channel']
        if channel['current_user_attributes']['can_message'] is not True:
            raise AuthError('provider_chat_cannot_message')
        return channel

    def configure_profile_cache(self, seconds):
        from bot.api import OsuApi
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 2592000:
            raise AuthError('provider_api_cache_refused')
        return OsuApi.configure_profile_cache(self, seconds)

    def configure_rate_limit(self, per_minute, burst_capacity):
        # The provider gate is deliberately stricter than the legacy burst
        # limiter. Hotloading can slow pacing, never shorten saved reservations.
        if (type(per_minute) is not int or not 1 <= per_minute <= 60
                or type(burst_capacity) is not int or not 1 <= burst_capacity <= 90):
            raise AuthError('invalid_provider_pacing')
        self.auth.transport.gate.interval = max(self.auth.transport.gate.interval, 60. / min(50, per_minute))

    def usage_report(self):
        from .provider_usage import snapshot
        pacing = self.auth._require_gate().snapshot()
        value = pacing['counters']
        observed = snapshot(self.auth.root, authoritative=value)
        lines = ['**osu! API usage · rolling averages**']
        if observed['available']:
            for row in observed['windows']:
                if row['label'] == '4h':
                    continue
                lines.append(f"{row['label']}: {row['count']} req · {row['rpm']:.1f}/min")
            budget = pacing['budget']
            sustained = 60. / budget['minimum_interval_seconds']
            lines.append(f"Limiter: {sustained:g}/min sustained · burst {budget['burst_capacity']} (shared)")
            queued = observed['queue']['observed_waiting']
            wait = observed['queue']['head_ready_seconds']
            hour = next(row for row in observed['windows'] if row['label'] == '1h')
            lines.append(f"Queue: {queued} waiting request{'s' if queued != 1 else ''} · head ready in {wait:.1f}s · 429s in 1h: {hour['rate_limited']}")
            lines.append('Scope: observed v7 HTTP requests, including OAuth and referee handshakes; queue excludes requests waiting for the shared lease.')
            if observed['telemetry_gap']:
                lines.append('Coverage: incomplete telemetry; counts may be lower than actual usage.')
            elif not hour['coverage_complete']:
                lines.append('Coverage: partial 1h history since v7 observation began; earlier v6 history is retained separately.')
        else:
            lines.append('Rolling request history is unavailable; lifetime counters remain available.')
        lines.append(('Provider lifetime requests: attempted={attempted}, dispatched={dispatched}, '
                      'replies={replies}, rate_limited={rate_limited}, uncertain={uncertain}, '
                      'deferred={deferred}').format(**value))
        return '\n'.join(lines)
