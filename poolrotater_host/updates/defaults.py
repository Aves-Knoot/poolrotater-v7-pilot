"""Pinned public updater bootstrap; missing policy defaults on, explicit choices stay.

No keys or credentials are embedded. Installing code alone never starts a bot.
Only a local network-start action initializes a missing enrolled updater.
"""
from pathlib import Path
import os

# Public release authority and TLS pins shipped with the audited installer.
PUBLIC_CONFIGURATION = {'automatic_download': True,
 'automatic_install': True,
 'bootstrap': {'expires_at': 1823636783,
               'format': 1,
               'keys': {'07f4ba7f06f84b978ab712cbb1472ec5a31a37078b0ef36d21f226872a074c73': '490018f003b9a575daf33bc5724e9fd2e7e4ef224c8ef84275094024a55697b5',
                        'f94132780c7fba9d9ede16680d58154edf71f236347f3fd7220612465771d237': '43d090d936587afcd98f85f329c7c010af9b29ea8a2f3a56bc934f3be1bffcb3'},
               'purpose': 'poolrotater-release-root',
               'repository_id': '5a411e20-f05f-4e64-9b84-bf90afc69485',
               'threshold': 2},
 'bootstrap_sha256': 'd88125b6a8406d607307ee9403989b30b02331f2335b9a2bad8c49ff64dff027',
 'ca_pem': '-----BEGIN CERTIFICATE-----\n'
           'MIIBxjCCAWugAwIBAgIUCJvxd8z1t/nCt9TYAuuIk0k0SacwCgYIKoZIzj0EAwIw\n'
           'MDEuMCwGA1UEAwwlUG9vbFJvdGF0ZXIgZXhlY3V0YWJsZS1yZWxlYXNlIFRMUyBD\n'
           'QTAeFw0yNjEwMTUyMTQ2MjNaFw0zNjEwMTIyMTQ2MjNaMDAxLjAsBgNVBAMMJVBv\n'
           'b2xSb3RhdGVyIGV4ZWN1dGFibGUtcmVsZWFzZSBUTFMgQ0EwWTATBgcqhkjOPQIB\n'
           'BggqhkjOPQMBBwNCAATyiOXCPSGxHpyybhyuYmkEpfHo5T5jCsJi4qmGXjSzE/a0\n'
           'MBDkMt4Qu3UWZxbWMjyEn5qK6LUq9GHb53pv2d3to2MwYTAdBgNVHQ4EFgQU2uV4\n'
           'f7nIl9rukcfDObfuAr3vqbYwHwYDVR0jBBgwFoAU2uV4f7nIl9rukcfDObfuAr3v\n'
           'qbYwDwYDVR0TAQH/BAUwAwEB/zAOBgNVHQ8BAf8EBAMCAQYwCgYIKoZIzj0EAwID\n'
           'SQAwRgIhAJkgZfMWlFS+tT56aGIas3mNW0whTDlGO100SWVIAK0dAiEA9t7qhCzk\n'
           'NivlG4Qus44Q5hoNHdmpp9Zo+u74jHeHnJI=\n'
           '-----END CERTIFICATE-----\n',
 'ca_sha256': '58ec13427d16a40aaac091c6d64aa531a295e67a939a6cb77fa977e8b673c35e',
 'channel': 'pilot',
 'check_seconds': 1800,
 'controller_updates': True,
 'origin': 'https://91.107.237.182:9446/',
 'paused': False,
 'report_to_hub': True}


def initialize(instance):
    from ..pilot_installation import administrative_binding
    from .operations import configure_instance, unit
    from .contracts import require
    target,_,_=administrative_binding(instance)
    directory=target['prefix']/'updates'
    if os.path.lexists(directory/'configuration.json'):
        return {'configured':True,'existing_choices_preserved':True,'timer_started':False}
    # Retain any partial initializer state for explicit recovery, never silently
    # replace a cache, existing trust or an interrupted administrator operation.
    require(not os.path.lexists(directory),'update_partial_bootstrap_requires_review')
    result=configure_instance(instance,PUBLIC_CONFIGURATION)
    unit(instance,'unit')
    unit(instance,'start')
    return {**result,'timer_started':True,'existing_choices_preserved':True}
