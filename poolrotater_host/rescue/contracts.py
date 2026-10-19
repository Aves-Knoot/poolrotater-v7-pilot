"""Closed rescue wire values: never forward exception text or credentials."""
import re

from ..network.contracts import fields, identifier, integer, number, require
from ..update_lifecycle import STAGES, CODES
from ..service_diagnostics import FAILURE_CODES

FAILURES = CODES | FAILURE_CODES

ACTIONS = ('inspect', 'complete-update', 'start-failed-service')
RESULTS = ('complete', 'refused', 'uncertain')
ERRORS = (None, 'rescue_state_refused', 'rescue_binding_changed',
          'rescue_policy_disabled', 'rescue_request_expired', 'rescue_request_changed',
          'rescue_ownership_requires_review', 'rescue_attempt_limit',
          'rescue_update_requires_review', 'rescue_outcome_uncertain',
          'rescue_transport_unavailable')


def digest(value):
    require(type(value) is str and re.fullmatch('[a-f0-9]{64}', value), 'invalid_rescue_digest')
    return value


def observation(value):
    fields(value, ('phase', 'operation_id', 'candidate_sha256', 'job_sha256',
                   'failure_stage', 'failure_code', 'service_state', 'startup_attempted'))
    require(value['phase'] in ('absent', 'waiting_empty', 'locking', 'committing',
            'recovering', 'retrying', 'held', 'complete', 'cancelled', 'unavailable', 'service_failed'), 'invalid_rescue_phase')
    for name in ('operation_id', 'candidate_sha256', 'job_sha256'):
        if value[name] is not None:
            (identifier if name == 'operation_id' else digest)(value[name])
    require(value['failure_stage'] in (*STAGES, None), 'invalid_rescue_stage')
    require(value['failure_code'] in (*FAILURES, None), 'invalid_rescue_failure')
    require(value['service_state'] in ('active', 'activating', 'inactive', 'failed',
                                     'deactivating', 'unavailable'), 'invalid_rescue_service')
    require(type(value['startup_attempted']) is bool, 'invalid_rescue_startup')
    return value


def request(value):
    fields(value, ('id', 'host_id', 'installation_id', 'generation', 'action',
                   'operation_id', 'candidate_sha256', 'job_sha256', 'expires_at'))
    for name in ('id', 'host_id', 'installation_id', 'generation'):
        identifier(value[name])
    require(value['action'] in ACTIONS, 'invalid_rescue_action')
    for name in ('operation_id', 'candidate_sha256', 'job_sha256'):
        if value[name] is not None:
            (identifier if name == 'operation_id' else digest)(value[name])
    if value['action'] == 'complete-update':
        require(all(value[k] is not None for k in ('operation_id', 'candidate_sha256', 'job_sha256')),
                'rescue_update_identity_required')
    if value['action'] == 'start-failed-service':
        require(value['operation_id'] is None and value['candidate_sha256'] is not None
                and value['job_sha256'] is not None, 'rescue_update_identity_required')
    number(value['expires_at'])
    return value


def receipt(value):
    fields(value, ('id', 'result', 'error'))
    identifier(value['id'])
    require(value['result'] in RESULTS and value['error'] in ERRORS, 'invalid_rescue_receipt')
    return value


def exchange(value):
    fields(value, ('protocol_version', 'installation_id', 'generation', 'sequence',
                   'sent_at', 'observation', 'receipt'))
    require(type(value['protocol_version']) is int and value['protocol_version'] == 1,
            'unsupported_protocol')
    identifier(value['installation_id']); identifier(value['generation'])
    integer(value['sequence']); number(value['sent_at']); observation(value['observation'])
    if value['receipt'] is not None:
        receipt(value['receipt'])
    return value
