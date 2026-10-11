"""Bind distinct OAuth authorities to a protected, currently registered runtime.

Credentials are either injected or read from an explicitly configured private
offline profile. No provider, browser, SSH or service is started.
Every authentication transaction rechecks registration/code/environment under
the same owner lease used by controller startup and stopped release switching.
"""
import os
from pathlib import Path

from .oauth import AuthError, OfflineOAuth
from .operator_client import (configure_operator_client, load_operator_client,
                              operator_client_fingerprint, same_client,
                              _configure_provider_client, _load_provider_client,
                              provider_client_fingerprint, PROFILE_PATH)
from .provisioning import load_provisioned


def _runtime_guard(root, release, *, prepared=False):
    if type(prepared) is not bool: raise AuthError('invalid_prepared_runtime_choice')
    if os.geteuid() == 0 or (not prepared and (
            os.environ.get('POOLROTATER_OFFLINE_SANDBOX') != '1'
            or os.environ.get('POOLROTATER_PROVISIONING_SANDBOX') != '1'
            )):
        raise AuthError('installed_offline_fixture_required')
    root, release = Path(root).absolute(), Path(release).absolute()

    def guard():
        try:
            if prepared: return load_provisioned(root, release=release, prepared=True)
            else: return load_provisioned(root, release=release)
        except Exception: raise AuthError('installed_runtime_binding_requires_review') from None

    # Refuse foreign/candidate/mutated runtime before reading private authority.
    guard()
    return guard


def installed_authentication(root, release, credentials, transport):
    guard = _runtime_guard(root, release)
    return OfflineOAuth(root, credentials, transport, local_guard=guard)


def configure_installed_client(root, release, profile):
    guard = _runtime_guard(root, release)
    return configure_operator_client(root, profile, local_guard=guard)


def configure_installed_provider_client(root, release, profile):
    guard = _runtime_guard(root, release, prepared=True)
    from .pilot_identity import provider_execution
    return _configure_provider_client(root, profile, local_guard=guard,
                                      execution=provider_execution(guard()))


def installed_provider_state(root, release, *, initialize=False, configuration=None):
    """Protected local setup/status, without CA discovery or upstream permission."""
    guard = _runtime_guard(root, release, prepared=True)
    return _provider_authentication(root, guard,
                                    _fixed_transport(guard()).local_state(configuration), initialize=initialize)


def installed_provider_authentication(root, release, trust, configuration=None, *, initialize=False):
    """Bind an explicit fixed provider; every actual authorization stays disabled."""
    guard = _runtime_guard(root, release, prepared=True)
    return _provider_authentication(root, guard,
                                    _fixed_transport(guard())(trust, configuration), initialize=initialize)


def _fixed_transport(runtime):
    from .pilot_identity import provider_execution, PILOT_PROVIDER
    from .provider_preflight import FixedProviderTransport, PilotProviderTransport, LivePilotTransport
    execution = provider_execution(runtime)
    if execution == 'provider-live-pilot': return LivePilotTransport
    return PilotProviderTransport if execution == PILOT_PROVIDER else FixedProviderTransport


def _provider_authentication(root, runtime_guard, transport, *, initialize=False):
    """One complete prepared namespace; reconstruction never initializes a ledger."""
    from .provider_authorization import ProviderOAuth
    from .provider_pacing import ProviderRequestGate
    from .provider_preflight import FixedProviderTransport, PilotProviderTransport, LivePilotTransport
    from .provider_transport import DeadlineFixtureTransport

    if type(initialize) is not bool: raise AuthError('invalid_provider_initialization_choice')
    if type(transport) in (FixedProviderTransport, PilotProviderTransport, LivePilotTransport):
        execution = transport.execution
        from .pilot_identity import provider_execution
        if provider_execution(runtime_guard()) != execution:
            raise AuthError('provider_installation_execution_mismatch')
        interval = transport.configuration.minimum_interval_seconds
    elif type(transport) is DeadlineFixtureTransport:
        execution = 'provider-fixture'
        interval = transport.gate.interval
    else:
        raise AuthError('qualified_provider_transport_required')
    profile = _load_provider_client(root, execution=execution)

    def room_guard():
        if not same_client(profile, _load_provider_client(root, execution=execution)):
            raise AuthError('operator_client_changed')
        # Namespace separation is not migration: legacy credentials, pending
        # attempts, pacing and room ownership must be resolved explicitly.
        legacy = (PROFILE_PATH, 'secrets/osu-token.json', 'secrets/oauth-pending.json',
                  'secrets/oauth-candidate.json', 'state/oauth-account.json',
                  'state/provider-pacing.json', 'state/provider-pacing-anchor.json')
        if any(os.path.lexists(Path(root) / name) for name in legacy):
            raise AuthError('unbound_authority_requires_migration')
        from .provider_room_state import binding, require_namespace
        require_namespace(root, binding(root, {
            'execution': execution, 'client_id': profile.credentials.client_id,
            'client_profile_sha256': provider_client_fingerprint(root, profile, execution=execution),
        }, profile.expected_user_id))

    def guard():
        runtime_guard()
        room_guard()

    guard()
    fingerprint = provider_client_fingerprint(root, profile, execution=execution)
    auth = ProviderOAuth(root, profile.credentials, transport, local_guard=guard,
                         expected_user_id=profile.expected_user_id, redirect_uri=profile.redirect_uri,
                         client_profile_sha256=fingerprint)
    # Full immutable runtime/environment verification remains at construction,
    # every OAuth transaction/owner entry and each controller worker spawn.
    # During the lifetime owner lease, root-owned code cannot be switched by a
    # cooperating administrator. Room calls still reread mutable profile/journal
    # provenance without hashing the entire installed environment for every chat.
    auth._room_guard = room_guard
    with auth._transaction():
        existing = False
        for path, kind in ((auth.token_path, 'token'), (auth.pending_path, 'pending'),
                           (auth.candidate_path, 'token'), (auth.account_path, 'account')):
            existing = auth._read(path, kind, optional=True) is not None or existing
        paths = (Path(root) / ProviderRequestGate.state_path, Path(root) / ProviderRequestGate.anchor_path)
        present = tuple(os.path.lexists(path) for path in paths)
        if not all(present) and (not initialize or existing):
            raise AuthError('provider_pacing_state_missing')
        gate = ProviderRequestGate(root, profile.credentials.client_id, execution=execution,
                                   client_profile_sha256=fingerprint, minimum_interval=interval)
        gate.require_authority(root, profile.credentials.client_id, user_id=auth._account())
        transport.gate = gate
    return auth


def installed_provider_preflight(root, release, declaration, configuration, trust):
    """Read-only protected-runtime/profile consistency; does not create authority.

    Snapshot again after trust validation to detect profile/runtime changes during
    preparation. This diagnostic is not a lease, startup permit, or live enrollment.
    Existing authorization, pacing and room-recovery fences remain untouched.
    """
    from .provider_preflight import operator_preflight
    guard = _runtime_guard(root, release)
    profile = load_operator_client(root)
    result = operator_preflight(profile, declaration, configuration, trust)
    guard()
    if not same_client(profile, load_operator_client(root)):
        raise AuthError('operator_client_changed')
    return {**result, 'installed_runtime_checked': True, 'private_profile_checked': True,
            'authority_state_checked': False, 'authority_state_modified': False}


def installed_operator_authentication(root, release, transport):
    """Use a private profile, pinning it for every transaction and owner entry."""
    return _operator_authentication(root, _runtime_guard(root, release), transport)


def _operator_authentication(root, runtime_guard, transport):
    profile = load_operator_client(root)

    def guard():
        runtime_guard()
        if not same_client(profile, load_operator_client(root)):
            raise AuthError('operator_client_changed')

    return OfflineOAuth(root, profile.credentials, transport, local_guard=guard,
                        expected_user_id=profile.expected_user_id, redirect_uri=profile.redirect_uri,
                        client_profile_sha256=operator_client_fingerprint(root, profile))


def installed_https_authentication(root, release, origin, trust, *, timeout=2,
                                   minimum_interval=.05):
    """Stopped offline factory: protected profile, loopback TLS, durable pacing.

    Validation and authority reads precede any pacing write. The OAuth transaction
    repeats runtime/profile checks under owner/account and OAuth leases before
    initializing or reopening the shared ledger. Reconstruction never resets it.
    No network request, callback listener or worker starts here; the returned
    authority retains the existing explicit authorization/recovery/start fences.
    """
    from .config import ConfigError
    from .provider_pacing import PersistentRequestGate
    from .provider_transport import DeadlineFixtureTransport, RequestGate

    runtime_guard = _runtime_guard(root, release)
    if type(timeout) not in (int, float) or not 0 < timeout <= 5:
        raise AuthError('invalid_provider_timeout')
    if type(minimum_interval) not in (int, float) or not .05 <= minimum_interval <= 60:
        raise AuthError('invalid_provider_pacing')
    # An explicit, transient gate validates the transport without touching global
    # origin gates or persistent files. Replace it only inside the owner leases.
    transport = DeadlineFixtureTransport(origin, trust, timeout=timeout,
                                         gate=RequestGate(minimum_interval=minimum_interval))
    auth = _operator_authentication(root, runtime_guard, transport)
    try:
        with auth._transaction():
            gate = PersistentRequestGate(auth.root, auth.credentials.client_id,
                                         minimum_interval=minimum_interval)
            gate.require_authority(auth.root, auth.credentials.client_id, user_id=auth._account())
            transport.gate = gate
    except AuthError:
        raise
    except ConfigError:
        raise AuthError('installed_provider_state_or_owner_requires_review') from None
    return auth
