"""Small, fixed API-v2 request surface used by the existing room client.

This is separate from OAuth preparation: accepting a chat POST cannot turn into
accepting an arbitrary grant, room mutation, URL, caller header or query string.
"""
from dataclasses import dataclass
import json
import re
from urllib.parse import parse_qsl, urlencode

from .layout import unique_fields
from .oauth import AuthError, _secret
from .provider_contract import API_VERSION, PreparedRequest

ID = r'[1-9][0-9]{0,18}'
MODES = ('osu', 'taiko', 'fruits', 'mania')


@dataclass(frozen=True, repr=False)
class PreparedApiRequest(PreparedRequest):
    # Inherit the credential/body-hiding representation.
    pass


def prepare_api(method, path, *, bearer, params=None, json_body=None):
    if type(method) is not str or type(path) is not str:
        raise AuthError('provider_api_endpoint_refused')
    query = {} if params is None else params
    if type(query) is not dict: raise AuthError('provider_api_query_refused')
    message = re.fullmatch(r'/api/v2/chat/channels/(' + ID + r')/messages', path)
    user = re.fullmatch(r'/api/v2/users/(' + ID + r')/(osu|taiko|fruits|mania)', path)
    best = re.fullmatch(r'/api/v2/users/(' + ID + r')/scores/best', path)
    simple = re.fullmatch(r'/api/v2/(beatmaps|beatmapsets)/' + ID, path)
    scores = re.fullmatch(r'/api/v2/rooms/' + ID + r'/playlist/' + ID + r'/scores', path)
    channel = re.fullmatch(r'/api/v2/chat/channels/' + ID, path)
    allowed = (method == 'GET' and (channel or message or user or best or simple or scores)
               or method == 'POST' and (message or path == '/api/v2/chat/ack'))
    if not allowed: raise AuthError('provider_api_endpoint_refused')
    if method == 'GET' and message:
        if set(query) not in ({'limit'}, {'limit', 'since'}):
            raise AuthError('provider_api_query_refused')
        if type(query['limit']) is not int or not 1 <= query['limit'] <= 50:
            raise AuthError('provider_api_query_refused')
        if 'since' in query and (type(query['since']) is not int or not 0 < query['since'] < 10**19):
            raise AuthError('provider_api_query_refused')
    elif method == 'GET' and user:
        if query != {'key': 'id'}: raise AuthError('provider_api_query_refused')
    elif method == 'GET' and best:
        if (set(query) != {'mode', 'limit'} or query['mode'] not in MODES
                or type(query['limit']) is not int or not 1 <= query['limit'] <= 100):
            raise AuthError('provider_api_query_refused')
    elif query: raise AuthError('provider_api_query_refused')
    body = None
    if method == 'POST' and message:
        if (type(json_body) is not dict or set(json_body) != {'message', 'is_action'}
                or type(json_body['message']) is not str or not json_body['message']
                or type(json_body['is_action']) is not bool):
            raise AuthError('provider_api_body_refused')
        try:
            if len(json_body['message'].encode('utf-8')) > 4096: raise ValueError
            body = json.dumps(json_body, ensure_ascii=False, sort_keys=True,
                              separators=(',', ':'), allow_nan=False).encode('utf-8')
            if len(body) > 32768: raise ValueError
        except (ValueError, UnicodeError): raise AuthError('provider_api_body_refused') from None
    elif json_body is not None: raise AuthError('provider_api_body_refused')
    headers = {'Accept': 'application/json', 'Accept-Encoding': 'identity',
               'Connection': 'close', 'x-api-version': API_VERSION,
               'Authorization': 'Bearer ' + _secret(bearer)}
    if method == 'POST': headers['Content-Type'] = 'application/json'
    full_path = path + ('?' + urlencode(sorted(query.items())) if query else '')
    return PreparedApiRequest(method, full_path, headers, body, 1024**2)


def revalidate_api(request):
    """Reconstruct canonical bytes before I/O, including hand-built requests."""
    if type(request) is not PreparedApiRequest: raise ValueError
    path, separator, raw = request.path.partition('?')
    pairs = parse_qsl(raw, strict_parsing=True)
    params = dict(pairs)
    if len(params) != len(pairs) or (separator and not raw): raise ValueError
    for key in ('limit', 'since'):
        if key in params:
            if not re.fullmatch(r'[1-9][0-9]{0,18}', params[key]): raise ValueError
            params[key] = int(params[key])
    body = None if request.body is None else json.loads(
        request.body.decode('utf-8'), object_pairs_hook=unique_fields,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    return prepare_api(request.method, path, bearer=request.headers['Authorization'].removeprefix('Bearer '),
                       params=params, json_body=body)
