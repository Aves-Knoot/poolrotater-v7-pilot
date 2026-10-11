"""Fixed spectator endpoint and strict negotiation, separate from API/OAuth."""
from dataclasses import dataclass
import http.client
import ssl

from .oauth import AuthError, _secret
from .provider_contract import PreparedRequest, _exchange_checked, require_live_provider

REFEREE_HOST = 'spectator.ppy.sh'
REFEREE_ORIGIN = 'https://' + REFEREE_HOST
NEGOTIATE = '/referee/negotiate?negotiateVersion=1'


@dataclass(frozen=True, repr=False)
class PreparedRefereeRequest(PreparedRequest):
    pass


def prepare_referee(bearer):
    return PreparedRefereeRequest('POST', NEGOTIATE, {
        'Accept':'application/json', 'Accept-Encoding':'identity', 'Connection':'close',
        'Authorization':'Bearer ' + _secret(bearer)}, None, 65536)


def negotiation(value):
    if (type(value) is not dict or any(key in value for key in ('url', 'accessToken', 'error'))
            or type(value.get('negotiateVersion')) is not int or value['negotiateVersion'] != 1
            or value.get('useAck', False) is not False
            or type(value.get('availableTransports')) is not list or not 0 < len(value['availableTransports']) <= 8):
        raise AuthError('referee_negotiation_refused')
    _secret(value.get('connectionId'), 4096)
    token = _secret(value.get('connectionToken'), 4096)
    if not any(type(row) is dict and row.get('transport') == 'WebSockets'
               and type(row.get('transferFormats')) is list and 'Text' in row['transferFormats']
               for row in value['availableTransports']):
        raise AuthError('referee_websocket_required')
    return token


def exchange_referee_upstream(connection, request, *, timeout, on_status):
    require_live_provider()
    if (type(request) is not PreparedRefereeRequest or type(connection) is not http.client.HTTPSConnection
            or connection.host != REFEREE_HOST or connection.port != 443 or connection._tunnel_host is not None
            or connection._context.verify_mode != ssl.CERT_REQUIRED or not connection._context.check_hostname
            or connection._context.minimum_version < ssl.TLSVersion.TLSv1_2):
        raise AuthError('fixed_referee_required')
    return _exchange_checked(connection, request, timeout=timeout, on_status=on_status)
