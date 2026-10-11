"""Run the operator workflow with invented credentials and loopback HTTP only."""
import argparse
import http.client
import json
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from poolrotater_host.authorization import OfflineAuthorizationSession, authorization_diagnostic, recover_authorization
from poolrotater_host.layout import setup
from poolrotater_host.oauth import AuthError, ClientCredentials, OfflineOAuth
from poolrotater_host.testing.oauth import OAuthProvider


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--mode', choices=('local', 'ssh'), default='local')
    parser.add_argument('--scenario', choices=('success', 'cancel', 'timeout', 'recover', 'lost-reply'), default='success')
    parser.add_argument('--redirect-uri', default='http://127.0.0.1:51111/oauth/callback')
    parser.add_argument('--reauthorize', action='store_true')
    parser.add_argument('--replace-pending', action='store_true')
    args = parser.parse_args(argv)
    # Never read credentials from the environment, disk or command arguments.
    try:
        with OAuthProvider() as provider:
            setup(args.root)
            auth = OfflineOAuth(args.root, ClientCredentials(123, 'invented-client-secret'), provider.transport)
            with OfflineAuthorizationSession(auth, args.redirect_uri, mode=args.mode,
                    reauthorize=args.reauthorize, replace_pending=args.replace_pending) as session:
                instructions = session.instructions()
                if args.scenario == 'cancel':
                    session.cancel()
                elif args.scenario == 'timeout':
                    try: session.wait(.02)
                    except AuthError: pass
                else:
                    if args.scenario == 'recover': provider.fault('/api/v2/me', 503, {})
                    if args.scenario == 'lost-reply': provider.fault('/oauth/token', 'disconnect', after=True)
                    state = parse_qs(urlsplit(session.authorization_url).query)['state'][0]
                    parts = urlsplit(args.redirect_uri)
                    connection = http.client.HTTPConnection('127.0.0.1', parts.port, timeout=5)
                    try:
                        connection.request('GET', parts.path + '?' + urlencode({'code': provider.code(), 'state': state}))
                        response = connection.getresponse(); response.read()
                    finally:
                        connection.close()
                    try: session.wait(1)
                    except AuthError:
                        if args.scenario not in {'recover', 'lost-reply'}: raise
                    if args.scenario == 'recover': recover_authorization(auth)
            result = authorization_diagnostic(auth)
            result.update(scenario=args.scenario, instructions=instructions,
                          synthetic_code_exchanges=sum(c['method'] == 'POST' for c in provider.calls),
                          actual_room_testing=False)
            print(json.dumps(result, indent=2))
            return 0
    except Exception:
        print(json.dumps({'ok': False, 'execution': 'offline-fixture', 'live_ready': False,
                          'next_action': 'review_private_state_or_callback_setup'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
