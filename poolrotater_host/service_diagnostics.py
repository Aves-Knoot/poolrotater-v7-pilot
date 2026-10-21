"""Closed diagnostic vocabulary; never forward private exception text."""
import re
FAILURE_CODES = frozenset(('managed_host_enrollment_required','managed_policy_migration_busy',
    'managed_policy_migration_requires_review','managed_root_policy_required',
    'invalid_provider_budget_loader', 'provider_budget_requires_review',
    'provider_budget_changed', 'owner_provider_budget_required', 'owner_provider_budget_mismatch',
    'owner_provider_budget_requires_completed_adoption', 'referee_snapshot_requires_review',
    'referee_room_owner_requires_review', 'referee_generation_changed',
    'referee_creation_record_requires_review', 'ownership_room_evidence_unavailable',
    'ownership_listing_unavailable', 'ownership_listing_refused',
    'provider_existing_rooms_require_review', 'referee_unauthorized',
    'referee_rate_limited', 'referee_unavailable', 'service_failed',
    'ownership_recovery_unresolved', 'ownership_creation_candidate_required',
    'ownership_creation_candidate_refused', 'ownership_recovery_expected_digest_required',
    'ownership_recovery_explicit_choices_required', 'ownership_journal_changed',
    'ownership_room_evidence_refused', 'ownership_listing_unavailable',
    'ownership_recovery_pending', 'ownership_recovery_write_uncertain',
    'ownership_recovery_binding_changed', 'ownership_recovery_transaction_required',
    'ownership_local_state_requires_review', 'ownership_journal_refused', 'ownership_journal_missing',
    'provider_room_binding_requires_review', 'installed_runtime_binding_requires_review',
    'operator_client_changed', 'unbound_authority_requires_migration',
    'provider_pacing_state_missing', 'ownership_recovery_record_changed',
    'ownership_recovery_action_required', 'ownership_recovery_no_known_rooms',
    'ownership_recovery_record_too_large', 'ownership_review_authority_refused',
    'ownership_review_provider_refused', 'ownership_recovery_refused',
    'process_identity_platform_required', 'process_target_refused',
    'provider_child_cleanup_uncertain', 'referee_child_cleanup_uncertain',
    'identity_temporarily_unavailable', 'provider_identity_mismatch_or_restricted',
    'expected_account_mismatch', 'verified_account_binding_missing', 'verified_owner_required',
    'required_scopes_missing', 'token_client_mismatch', 'token_expired',
    'provider_pacing_binding_mismatch', 'provider_pacing_state_refused',
    'provider_pacing_clock_refused', 'provider_pacing_initialization_uncertain',
    'provider_gate_process_changed', 'provider_pacing_deferred', 'provider_rate_limited',
    'provider_deadline_exceeded', 'provider_transport_uncertain', 'transport_reply_uncertain',
    'unsafe_or_invalid_local_state', 'local_binding_mismatch',
    'operator_client_binding_requires_review', 'operator_client_missing',
    'unsafe_operator_client_state', 'unfinished_authorization_requires_resolution'))


# Reviewed literal local/provider authority codes; never accept arbitrary text.
AUTHORITY_FAILURE_CODES = frozenset((
    'invalid_provider_budget_loader',
    'provider_budget_requires_review',
    'provider_budget_changed',
    'owner_provider_budget_required',
    'owner_provider_budget_mismatch',
    'owner_provider_budget_requires_completed_adoption',
    'access_token_rejected',
    'account_in_use',
    'account_permission_denied',
    'authorization_attempt_changed',
    'authorization_attempt_mismatch',
    'authorization_denied',
    'authorization_exchange_failed',
    'authorization_expired',
    'authorization_recovery_unavailable',
    'authorization_reply_lost_reauthorize',
    'callback_already_consumed',
    'client_redirect_mismatch',
    'credential_recovery_required',
    'credentials_missing',
    'expected_account_mismatch',
    'explicit_attempt_replacement_required',
    'explicit_reauthorization_required',
    'fixed_provider_required',
    'identity_temporarily_unavailable',
    'installation_changed',
    'installed_offline_fixture_required',
    'installed_provider_state_or_owner_requires_review',
    'installed_runtime_binding_requires_review',
    'invalid_authority_type',
    'invalid_authorization_state',
    'invalid_callback',
    'invalid_client_input',
    'invalid_client_profile_binding',
    'invalid_credentials',
    'invalid_expected_account',
    'invalid_identity',
    'invalid_local_guard',
    'invalid_loopback_redirect',
    'invalid_operator_declaration',
    'invalid_prepared_request',
    'invalid_prepared_runtime_choice',
    'invalid_provider_configuration',
    'invalid_provider_execution',
    'invalid_provider_initialization_choice',
    'invalid_provider_pacing',
    'invalid_provider_reply',
    'invalid_provider_timeout',
    'invalid_reauthorization_choice',
    'invalid_replacement_choice',
    'invalid_secret_format',
    'invalid_time',
    'invalid_token_phase',
    'invalid_token_response',
    'local_binding_mismatch',
    'local_persistence_failed',
    'offline_fixture_required',
    'offline_verified_tls_required',
    'operator_client_binding_requires_review',
    'operator_client_change_requires_migration',
    'operator_client_changed',
    'operator_client_input_too_large',
    'operator_client_layout_requires_review',
    'operator_client_missing',
    'operator_client_owner_mismatch',
    'operator_client_persistence_failed',
    'operator_registered_redirect_mismatch',
    'operator_requested_scopes_mismatch',
    'persisted_token_authority_mismatch',
    'provider_client_binding_required',
    'provider_content_type_refused',
    'provider_deadline_exceeded',
    'provider_endpoint_refused',
    'provider_form_refused',
    'provider_framing_refused',
    'provider_gate_process_changed',
    'provider_grant_refused',
    'provider_identity_mismatch_or_restricted',
    'provider_installation_execution_mismatch',
    'provider_pacing_account_mismatch',
    'provider_pacing_binding_mismatch',
    'provider_pacing_busy',
    'provider_pacing_clock_changed',
    'provider_pacing_clock_refused',
    'provider_pacing_deferred',
    'provider_pacing_initialization_uncertain',
    'provider_pacing_not_admitted',
    'provider_pacing_outcome_refused',
    'provider_pacing_recovery_refused',
    'provider_pacing_recovery_required',
    'provider_pacing_recovery_unavailable',
    'provider_pacing_state_changed',
    'provider_pacing_state_missing',
    'provider_pacing_state_refused',
    'provider_pacing_write_uncertain',
    'provider_redirect_refused',
    'provider_reply_deadline',
    'provider_reply_too_large',
    'provider_reply_truncated',
    'provider_request_too_large',
    'provider_room_binding_requires_review',
    'provider_transport_uncertain',
    'provider_trust_refused',
    'qualified_live_gate_required',
    'qualified_provider_transport_required',
    'required_scopes_missing',
    'revocation_unconfirmed',
    'token_client_mismatch',
    'token_expired',
    'transport_reply_uncertain',
    'unbound_authority_requires_migration',
    'unfinished_authorization_requires_resolution',
    'unsafe_operator_client_state',
    'unsafe_or_invalid_local_state',
    'unsupported_token_claims',
    'verified_account_binding_missing',
    'verified_owner_required',
))
FAILURE_CODES = FAILURE_CODES | AUTHORITY_FAILURE_CODES
FAILURE_CODES = FAILURE_CODES | frozenset((
    'provider_api_unavailable', 'provider_api_unauthorized', 'provider_api_rate_limited',
    'provider_api_mutation_uncertain', 'provider_api_reply_refused',
    'provider_api_user_mismatch', 'provider_api_endpoint_refused',
    'provider_chat_access_unavailable', 'provider_chat_cannot_message',
    'provider_worker_ownership_recovery_required', 'provider_worker_failed',
    'provider_worker_exited', 'provider_worker_connection_closed',
    'provider_cleanup_incomplete', 'provider_child_setup_refused',
    'maintenance_request_refused','maintenance_operation_pending','maintenance_operation_changed',
    'maintenance_requires_running_controller','maintenance_room_topology_changed',
))


def failure_code(error):
    return failure_details(error)['failure_code']


def failure_details(error):
    """Recover only reviewed codes/numeric status, including exact RPC strings."""
    seen=set();pending=[error]
    while pending and len(seen)<32:
        item=pending.pop()
        if id(item) in seen:continue
        seen.add(id(item))
        from .oauth import AuthError
        # AuthError.args carries a prose prefix; its stable code is separate.
        # Reading only args hid stopped-review errors as service_failed.
        if isinstance(item, AuthError) and type(item.code) is str and item.code in FAILURE_CODES:
            result = {'failure_code': item.code}
            status = getattr(item, 'http_status', None)
            if type(status) is int and 100 <= status <= 599: result['failure_http_status'] = status
            return result
        if (type(getattr(item,'args',None)) is tuple and len(item.args)==1
                and type(item.args[0]) is str and item.args[0] in FAILURE_CODES):
            return {'failure_code': item.args[0]}
        if type(getattr(item, 'args', None)) is tuple and len(item.args) == 1 and type(item.args[0]) is str:
            match = re.fullmatch(r'(?:offline )?OAuth: ([a-z_]+)(?: \[HTTP ([1-5][0-9]{2})\])?', item.args[0])
            if match and match[1] in FAILURE_CODES:
                result = {'failure_code': match[1]}
                if match[2]: result['failure_http_status'] = int(match[2])
                return result
        pending.extend(e for e in (item.__cause__,item.__context__) if isinstance(e,BaseException))
    return {'failure_code': 'service_failed'}
