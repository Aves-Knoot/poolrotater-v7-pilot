"""Secret-free public presentation for private booking slots."""
import re
from urllib.parse import urlsplit

PUBLIC_STATES = frozenset(('available', 'preparing', 'reserved', 'active', 'closing', 'unavailable'))
_DESCRIPTIONS = {
    'available': 'A private room is available to reserve.',
    'preparing': 'The private room is being prepared.',
    'reserved': 'The private room is reserved and awaiting its owner.',
    'active': 'The private room is active.',
    'closing': 'The private room is closing.',
    'unavailable': 'Private rooms are unavailable right now.',
}


def _safe_origin(value):
    if type(value) is not str:
        return None
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or '').casefold()
        labels = hostname.split('.')
        valid_host = (len(hostname) <= 253 and bool(labels) and all(
            re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in labels))
        if (parsed.scheme.casefold() != 'https' or not valid_host
                or parsed.username is not None or parsed.password is not None or parsed.port is not None
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            return None
        return f'https://{hostname}'
    except ValueError:
        return None


def _safe_label(value):
    if type(value) is not str:
        return 'Private lobby'
    value = ''.join(ch for ch in value if ord(ch) >= 32 and ord(ch) != 127)
    value = re.sub(r'[\*_~`|\[\]()<>&]', '', value).strip()
    return (value[:80] or 'Private lobby')


def public_booking_status(slot, website_origin='https://poolrotater.com'):
    """Return a bounded public website status summary without session data."""
    slot = slot if type(slot) is dict else {}
    state = slot.get('state')
    if type(state) is not str or state not in PUBLIC_STATES:
        state = 'unavailable'
    fresh = slot.get('fresh') is True
    if not fresh and state == 'available':
        state = 'unavailable'
    label = _safe_label(slot.get('label'))
    delayed = not fresh
    description = _DESCRIPTIONS[state]
    if delayed:
        description += ' Status delayed.'
        if state in ('preparing', 'reserved', 'active', 'closing'):
            description += ' The reservation remains held until the host confirms cleanup.'
    origin = _safe_origin(website_origin)
    url = f'{origin}/private-lobbies/' if origin else ''
    if state == 'available':
        button = 'Book a private lobby'
    elif state in ('preparing', 'reserved', 'active', 'closing'):
        button = 'Manage private booking'
    else:
        button = 'View private lobby status'
    fields = [('Slot', label), ('Status', state.replace('_', ' ').title())]
    if delayed:
        fields.append(('Freshness', 'Status delayed'))
    return {'title': 'Private lobbies', 'state': state, 'description': description,
            'button_label': button if url else '', 'website_url': url, 'fields': fields,
            'fresh': fresh}


def purebooking_card(slot, website_origin='https://poolrotater.com'):
    """Build a public Discord status card without exposing room/session details."""
    status = public_booking_status(slot, website_origin)
    label = _safe_label(slot.get('label') if type(slot) is dict else None)
    link = f"[{status['button_label']}]({status['website_url']})" if status['website_url'] else status['button_label']
    extra = [('Private lobby', link)] if link else []
    state = status['state']
    lifecycle = state.replace('_', ' ').title()
    if not status['fresh']:
        lifecycle += ' · Status delayed'
    return {'is_open': state == 'available', 'lifecycle': lifecycle, 'room_id': 0,
            'room_name': f'{label} · {status["description"]}', 'player_count': 0,
            'sr_min': 0.0, 'sr_max': 0.0, 'map_label': '', 'map_url': '',
            'instance': 'private-booking', 'channel_base_name': 'private-lobbies',
            'display_name': status['title'], 'image_url': '', 'extra_fields': extra}
