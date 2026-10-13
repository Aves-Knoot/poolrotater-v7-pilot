"""Reuse the working notification socket under the installed provider authority.

The socket is optional and never starts at construction. HTTP and referee room
ownership remain authoritative; this supplies the same chat acceleration/fallback
as the owner bot without accepting an arbitrary bearer destination.
"""
import math
import threading

from .oauth import AuthError
from .provider_contract import require_live_provider

ENDPOINT = 'wss://notify.ppy.sh'


def available():
    return True


def create(authentication, configs):
    from .provider_rooms import _authority
    _authority(authentication)
    enabled = [cfg for cfg in configs if cfg.get('chat_websocket_enabled') is True]
    if not enabled:
        return None
    endpoints = {cfg.get('chat_websocket_endpoint') or ENDPOINT for cfg in enabled}
    if endpoints != {ENDPOINT}:
        raise AuthError('provider_chat_endpoint_refused')
    values = [cfg.get('chat_websocket_reconnect_max_seconds', 30.) for cfg in enabled]
    if any(type(value) not in (int, float) or not math.isfinite(value)
           or not 1 <= value <= 300 for value in values) or len(set(values)) != 1:
        raise AuthError('provider_chat_settings_refused')
    require_live_provider(authentication.execution)
    if not authentication._verified:
        raise AuthError('verified_owner_required')
    tokens = threading.local()

    def bearer():
        require_live_provider(authentication.execution)
        if not authentication._verified:
            raise AuthError('verified_owner_required')
        tokens.value = authentication.access_token()
        return tokens.value

    def rejected():
        value = getattr(tokens, 'value', None)
        if value is not None:
            authentication.invalidate_access_token(value)
            del tokens.value

    from bot.chat_websocket import ChatWebSocket
    return ChatWebSocket(bearer, endpoint=ENDPOINT, enabled=True,
                         reconnect_max_seconds=values[0], invalidate_token=rejected)
