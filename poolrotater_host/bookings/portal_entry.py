"""Explicit launcher for the private-booking loopback portal."""
import argparse
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import urlsplit

from ..network.contracts import NetworkError, canonical, decode, require
from .oauth import OAuthClient
from .server import PortalServer
from .web import Portal
from ..network.hub_layout import check as check_hub_root

DEFAULT_CONFIG = {
    'format': 1,
    'enabled': False,
    'origin': 'https://poolrotater.com',
    'bind': '127.0.0.1',
    'port': 9446,
}
CONFIG_KEYS = frozenset(DEFAULT_CONFIG)
OAUTH_KEYS = frozenset(('client_id', 'client_secret'))
PREVIEW_KEYS = frozenset(('preview_catalog_root',))
MAX_CONFIG_BYTES = 65536


def _origin(value):
    require(type(value) is str, 'booking_site_config_invalid')
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or '').casefold()
        port = parsed.port
    except ValueError:
        raise NetworkError('booking_site_config_invalid') from None
    require(parsed.scheme == 'https' and hostname and port is None and not parsed.username and not parsed.password
            and parsed.path in ('', '/') and not parsed.query and not parsed.fragment,
            'booking_site_config_invalid')
    require(len(hostname) <= 253 and all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                         for label in hostname.split('.')),
            'booking_site_config_invalid')
    return f'https://{hostname}'


def validate_config(value):
    require(type(value) is dict and CONFIG_KEYS <= set(value) and not set(value) - CONFIG_KEYS - OAUTH_KEYS - PREVIEW_KEYS,
            'booking_site_config_invalid')
    require(type(value['format']) is int and value['format'] == 1, 'booking_site_config_invalid')
    require(type(value['enabled']) is bool, 'booking_site_config_invalid')
    origin = _origin(value['origin'])
    require(value['bind'] == '127.0.0.1', 'booking_portal_loopback_required')
    require(type(value['port']) is int and 1024 <= value['port'] <= 65535, 'booking_site_config_invalid')
    has_client_id = 'client_id' in value
    has_client_secret = 'client_secret' in value
    require(has_client_id == has_client_secret, 'booking_site_config_invalid')
    if value['enabled']:
        require(has_client_id, 'booking_oauth_configuration_required')
    if has_client_id:
        require(type(value['client_id']) is str and value['client_id'].isascii()
                and value['client_id'].isdigit() and 0 < len(value['client_id']) <= 20,
                'booking_oauth_configuration_required')
    if 'preview_catalog_root' in value:
        directory=value['preview_catalog_root']
        require(type(directory)is str and len(directory)<=1024 and Path(directory).is_absolute()
                and not any(part in ('.','..')for part in directory.split('/'))
                and not any(ord(ch)<32 for ch in directory),'booking_preview_configuration_refused')
        require(type(value['client_secret']) is str and 8 <= len(value['client_secret']) <= 256
                and not any(ord(ch) <= 32 for ch in value['client_secret']),
                'booking_oauth_configuration_required')
    return {**value, 'origin': origin}


def _read_config(root):
    root, _ = check_hub_root(root)
    path = root / 'bookings-site.json'
    for parent in (path, *path.parents):
        require(not parent.is_symlink(), 'booking_site_config_unsafe')
    if not os.path.lexists(path):
        return root, dict(DEFAULT_CONFIG)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and info.st_nlink == 1
            and not stat.S_IMODE(info.st_mode) & 0o077 and info.st_size <= MAX_CONFIG_BYTES,
            'booking_site_config_unsafe')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
    finally:
        os.close(fd)
    require(len(raw) <= MAX_CONFIG_BYTES, 'booking_site_config_invalid')
    value = decode(raw, maximum=MAX_CONFIG_BYTES)
    return root, validate_config(value)


def public_config_summary(config):
    """Status safe for stdout; never include OAuth values."""
    return {'ok': True, 'enabled': config['enabled'], 'origin': config['origin'],
            'bind': config['bind'], 'port': config['port'],
            'oauth_configured': 'client_id' in config}


def _serve(root, config):
    require(config['enabled'], 'booking_portal_disabled', 503)
    callback = config['origin'] + '/private-lobbies/oauth/callback'
    oauth = OAuthClient(config['client_id'], config['client_secret'], callback)
    from .hub import Repository as BookingHubRepository
    from ..network.store import Store
    store = Store(root)
    repository = BookingHubRepository(store).ledger
    preview=None
    if config.get('preview_catalog_root'):
        from .preview import ExportPreviewProvider
        preview=ExportPreviewProvider(config['preview_catalog_root'])
    portal = Portal(root, repository, origin=config['origin'], oauth=oauth,
                    enabled=True, preview=preview, clock=store.clock)
    server = PortalServer((config['bind'], config['port']), portal)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Private booking portal; disabled by default')
    parser.add_argument('--root', required=True, type=Path, help='existing private hub root')
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--check', action='store_true', help='validate local portal config without networking')
    action.add_argument('--serve', action='store_true', help='serve the explicitly enabled loopback portal')
    args = parser.parse_args(argv)
    try:
        root, config = _read_config(args.root)
        if args.check:
            result = public_config_summary(config)
        else:
            _serve(root, config)
            result = {'ok': True, 'portal_stopped': True}
    except (NetworkError, OSError, ValueError, TypeError, KeyError) as exc:
        result = {'ok': False, 'error': exc.code if isinstance(exc, NetworkError) else 'booking_portal_refused'}
    sys.stdout.buffer.write(canonical(result) + b'\n')
    sys.stdout.buffer.flush()
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
