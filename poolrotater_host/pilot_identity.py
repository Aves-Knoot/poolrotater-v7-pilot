"""Explicit installation identities; qualification is never live authority.

These are artifact and protected registration contracts, not environment flags.
The candidate remains fenced; only the separate protected live artifact dispatches.
"""
from .config import ConfigError

LEGACY = 'offline-fixture'
CANDIDATE = 'pilot-candidate'
QUALIFICATION = 'pilot-qualification'
LIVE = 'pilot-live'
LIVE_PROVIDER = 'provider-live-pilot'
INSTALL_EXECUTIONS = (LEGACY, CANDIDATE, QUALIFICATION, LIVE)
PILOT_PROVIDER = 'provider-pilot'


def installation_execution(value):
    if type(value) is not str or value not in INSTALL_EXECUTIONS:
        raise ConfigError('invalid installation execution identity')
    return value


def provider_execution(runtime):
    """Legacy mocks/fixtures stay disabled; only a checked candidate gets pilot."""
    record = runtime.get('registration', {})
    execution = installation_execution(record.get('execution', LEGACY))
    if execution == QUALIFICATION:
        raise ConfigError('qualification authority requires its invented entry')
    if execution == LIVE: return LIVE_PROVIDER
    return PILOT_PROVIDER if execution == CANDIDATE else 'provider-disabled'


def require_entry(execution, *, prepared):
    execution = installation_execution(execution)
    if execution in (CANDIDATE, LIVE) and not prepared:
        raise ConfigError('pilot candidate cannot execute a fixture entry')
    if execution == QUALIFICATION and prepared:
        raise ConfigError('qualification cannot execute a fixed provider entry')
