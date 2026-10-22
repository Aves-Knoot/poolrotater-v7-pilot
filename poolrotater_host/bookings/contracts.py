"""Closed private booking protocol. Browser roles never become fleet roles."""
import re
from ..network.contracts import (NetworkError,canonical,decode,fields,fingerprint,
                                identifier,integer,number,require,text)

PATH='/v1/hosts/self/bookings'
PUBLIC_STATES=('available','preparing','reserved','active','closing','unavailable')
PHASES=('preparing','reserved','active','closing','unavailable','ended')
ROLES=('owner','controller','player')
ACTIONS=('configure','start','abort','skip','end','grant','revoke')
APPLICATIONS=('live','next_map','recreate')
SAFE_OPERATION_ERRORS=frozenset(('host_operation_failed','booking_room_not_private','booking_room_uncertain',
    'booking_room_in_use','booking_policy_refused','booking_catalog_unavailable','booking_capacity_refused',
    'booking_role_changed','booking_forbidden','booking_closing','stale_booking_command','configure_cooldown',
    'configure_pending','empty_settings','invalid_action_payload','invalid_controller','invalid_booking_settings',
    'booking_observation_state_invalid','host_generation_mismatch','booking_closed','booking_cleanup_complete',
    'booking_not_closed','booking_effect_not_found','booking_not_found','operation_not_found','booking_lease_conflict',
    'revoked','role_revoked','booking_ending','booking_closed','owner_required','roster_or_role_changed',
    'provider_failure','operation_failed','invalid_booking_command','invalid_booking_lease',
    'invalid_booking_lifecycle','invalid_booking_binding','invalid_booking_owner','invalid_designated_friend',
    'invalid_booking_observation','invalid_booking_sequence','booking_alias_in_use','invalid_operation_phase',
    'invalid_operation_application','invalid_operation_transition','operation_receipt_conflict',
    'booking_invalid_setting','booking_invalid_settings','booking_settings_refused','booking_preset_unsupported',
    'booking_invalid_filters','booking_filter_unsupported','player_not_in_roster','invalid_roster','invalid_observation'))
SAFE_OPERATION_ERRORS|=frozenset(('booking_command_queue_full','settings_revision_conflict','worker_action_outcome_unknown'))
FAILURES=frozenset(('native_operation_requires_review','worker_action_outcome_unknown','booking_preparation_failed'))
BINDING=('host_id','installation_id','generation')
SESSION_BINDING=(*BINDING,'booking_id','session_generation','alias')

def binding(value):
    require(type(value)is dict,'booking_invalid_binding')
    for key in BINDING:identifier(value.get(key))
    return value

def session_binding(value):
    binding(value);identifier(value.get('booking_id'))
    require(type(value.get('session_generation'))is str and re.fullmatch('[a-f0-9]{32}',value['session_generation'])is not None,'booking_invalid_generation')
    require(value.get('alias')in('room1','room2','room3','room4'),'booking_invalid_slot')
    return value

def player(value):
    fields(value,('user_id','username'));integer(value['user_id'],1,2**63-1);text(value['username'],80)
    return value

def map_info(value):
    if value is None:return None
    fields(value,('beatmap_id','label','stars','hit_length'))
    integer(value['beatmap_id'],1,2**63-1);text(value['label'],240)
    number(value['stars'],0,100);integer(value['hit_length'],0,86400)
    return dict(value)

def queue_info(value):
    require(type(value)is list and len(value)<=5,'booking_invalid_queue')
    return [map_info(item)for item in value]

def observation(value):
    required={'sequence','phase','room_id','incarnation','roster','roster_known','updated_at',
              'ready','closed','effective_settings','cleanup_complete','ownership_known',
              'operation_receipts','installation_id','generation','session_generation','alias'}
    optional={'terminal_reason','reason','claim_deadline','settings_revision','failure_code','current_map','queue','queue_known'}
    require(type(value)is dict and required<=set(value) and not set(value)-required-optional
            and not({'terminal_reason','reason'}<=set(value)),'booking_invalid_observation')
    integer(value['sequence'],0)
    require(value['phase']in('preparing','awaiting_claim','reserved','active','closing','ended','failed','unavailable'),'booking_invalid_phase')
    if value['room_id']is not None:integer(value['room_id'],1,2**63-1)
    require(value['incarnation']is None or type(value['incarnation'])is str and 0<len(value['incarnation'])<=128,'booking_invalid_observation')
    require(type(value['roster_known'])is bool and type(value['ready'])is bool and type(value['closed'])is bool
            and type(value['cleanup_complete'])is bool and type(value['ownership_known'])is bool,'booking_invalid_observation')
    identifier(value['installation_id']);identifier(value['generation'])
    require(type(value['session_generation'])is str and re.fullmatch('[a-f0-9]{32}',value['session_generation'])is not None,'booking_invalid_generation')
    require(value['alias']in('room1','room2','room3','room4'),'booking_invalid_slot')
    require(type(value['roster'])is list and len(value['roster'])<=256,'booking_invalid_roster')
    seen=set()
    for p in value['roster']:
        player(p);require(p['user_id']not in seen,'booking_invalid_roster');seen.add(p['user_id'])
    require(type(value['effective_settings'])is dict and len(canonical(value['effective_settings']))<=16384,'booking_invalid_settings')
    require(type(value['operation_receipts'])is list and len(value['operation_receipts'])<=32,'booking_invalid_receipts')
    for receipt in value['operation_receipts']:
        require(type(receipt)is dict and set(receipt)<={'operation_id','phase','error','application'}
                and {'operation_id','phase'}<=set(receipt),'booking_invalid_receipts')
        identifier(receipt['operation_id'])
        require(receipt['phase']in('applied','failed','cancelled'),'booking_invalid_receipts')
        require(receipt.get('error')is None or receipt.get('error')in SAFE_OPERATION_ERRORS,'booking_invalid_receipts')
        require(receipt.get('application')is None or receipt.get('application')in APPLICATIONS,'booking_invalid_receipts')
    reason=value.get('terminal_reason',value.get('reason'))
    require(reason is None or reason in ('claim_timeout','inactive_timeout','host_closed'),'booking_invalid_observation')
    number(value['updated_at'])
    require(value.get('claim_deadline')is None or number(value['claim_deadline']),'booking_invalid_claim_deadline')
    if 'settings_revision'in value:integer(value['settings_revision'],0,2**53-1)
    require(value.get('failure_code')is None or value['failure_code']in FAILURES,'booking_invalid_failure')
    if 'current_map'in value:map_info(value['current_map'])
    if 'queue'in value:queue_info(value['queue'])
    if 'queue_known'in value:require(type(value['queue_known'])is bool,'booking_invalid_queue')
    require(len(canonical(value))<=65536,'booking_invalid_observation');return value
