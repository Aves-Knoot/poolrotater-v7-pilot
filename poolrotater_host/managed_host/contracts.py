"""Closed, generation-bound fleet protocol; no free-form shell or file actions."""
import hashlib
import re
from ..network.contracts import canonical, fields, identifier, integer, number, require, text
from .settings import validate

ACTIONS = frozenset(('open','close','restart','configure','inspect','cancel'))
TIMINGS = frozenset(('live','next_map','when_empty','immediate'))
PHASES = frozenset(('received','validated','waiting_boundary','applying','applied','refused','uncertain','cancelled','expired'))
TERMINAL = frozenset(('applied','refused','uncertain','cancelled','expired'))
ERRORS = frozenset((None,'unsupported_capability','host_identity_mismatch','configuration_revision_changed',
    'room_generation_changed','host_capacity_reached','room_ownership_requires_review','operation_expired',
    'room_busy','room_not_found','invalid_settings','provider_outcome_uncertain','catalog_unavailable',
    'provider_unavailable','controller_unavailable','operation_cancelled','managed_operation_pending',
    'managed_application_requires_review'))

def digest(value):return hashlib.sha256(canonical(value)).hexdigest()

def alias(value):
    require(type(value) is str and re.fullmatch(r'room[1-4]',value) is not None,'invalid_managed_alias')
    return value

def request(value):
    fields(value,('protocol_version','policy_version','id','host_id','installation_id','generation',
                  'alias','actor','role','action','expected_revision','expected_room_generation',
                  'timing','patch','expires_at','cancel_operation_id'))
    require(type(value['protocol_version']) is int and value['protocol_version']==1,'unsupported_protocol')
    require(type(value['policy_version']) is int and value['policy_version']==1,'unsupported_managed_policy')
    for key in ('id','host_id','installation_id','generation'):identifier(value[key])
    alias(value['alias']);text(value['actor'],80)
    require(type(value['role']) is str and value['role']=='FLEET_ADMIN','managed_role_refused',403)
    require(type(value['action']) is str and value['action'] in ACTIONS and type(value['timing']) is str and value['timing'] in TIMINGS,'invalid_managed_action')
    integer(value['expected_revision'],0,2**53-1);number(value['expires_at'])
    require(value['expected_room_generation'] is None or type(value['expected_room_generation']) is str
            and re.fullmatch('[a-f0-9]{32}',value['expected_room_generation']) is not None,'invalid_managed_generation')
    require(type(value['patch']) is dict and len(value['patch'])<=256,'invalid_managed_patch')
    if value['action']=='configure':require(bool(value['patch']),'invalid_managed_patch')
    else:require(not value['patch'],'invalid_managed_patch')
    if value['action']=='cancel':identifier(value['cancel_operation_id'])
    else:require(value['cancel_operation_id'] is None,'invalid_managed_cancel')
    # Validate values against the saved room in the host controller; no preset
    # assumption at the hub, and never accept a configurable path/credential.
    from .settings import FIELDS
    require(not set(value['patch'])-FIELDS,'invalid_managed_patch')
    return value

def capabilities(value):
    fields(value,('protocol_version','policy_version','installation_id','generation','host_id',
                  'max_concurrent_rooms','provider_room_limit','capacity_used','rooms','controller_phase'))
    for key in ('protocol_version','policy_version'):integer(value[key],1,1)
    for key in ('installation_id','generation','host_id'):identifier(value[key])
    for key in ('max_concurrent_rooms','provider_room_limit','capacity_used'):integer(value[key],0,1024)
    require(type(value['rooms']) is list and len(value['rooms'])<=4,'invalid_managed_rooms')
    require(type(value['controller_phase']) is str and value['controller_phase'] in ('running','stopped','failed','starting','unavailable'),'invalid_managed_phase')
    seen=set()
    for room in value['rooms']:
        fields(room,('alias','desired','revision','applied_revision','phase','room_id','room_generation',
                     'preset','visibility','pending_operation','failure_code'))
        alias(room['alias']);require(room['alias'] not in seen,'invalid_managed_rooms');seen.add(room['alias'])
        require(type(room['desired']) is str and room['desired'] in ('running','stopped'),'invalid_managed_desired')
        for key in ('revision','applied_revision'):integer(room[key],0,2**53-1)
        require(type(room['phase']) is str and room['phase'] in ('unobserved','running','stopped','waiting_boundary','applying','uncertain','failed'),'invalid_managed_phase')
        if room['room_id'] is not None:integer(room['room_id'],1,2**63-1)
        require(room['room_generation'] is None or type(room['room_generation']) is str
            and re.fullmatch('[a-f0-9]{32}',room['room_generation']) is not None,'invalid_managed_generation')
        from ..config import PRESETS
        require(type(room['preset']) is str and room['preset'] in PRESETS and type(room['visibility']) is str
                and room['visibility'] in ('public','private'),'invalid_managed_rooms')
        if room['pending_operation'] is not None:identifier(room['pending_operation'])
        require(room['failure_code'] is None or type(room['failure_code']) is str and room['failure_code'] in ERRORS,'invalid_managed_error')
    return value
