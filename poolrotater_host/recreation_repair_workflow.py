"""Pinned local stopped repair; use only from the verified release kit.

No uncertain creation is cleared here. Existing native recovery may clear known
ended identities; missing identities require discovery and external evidence.
"""
from .config import ConfigError
from .layout import file_lease
from .recreation_repair_helpers import Helpers
from .recreation_repair_receipt import Receipt


def require(value,code):
    if not value:raise ConfigError(code)


def run(instance,bundle,candidate,supported_predecessors,ops,*,start=False):
    from . import pilot_installation as installation,pilot_upgrade as upgrade,pilot_operations as pilot
    from .runtime_lifecycle import require_no_pending
    from .provisioning import _control
    from .rescue.pin_refresh import refresh_pin,diagnose_pin
    from .recreation_repair_backup import snapshot as backup
    require(type(start)is bool,'repair_start_choice_required')
    target,marker,record=installation.administrative_binding(instance)
    require(instance!='owner' and record['execution']=='pilot-live','repair_community_instance_required')
    require(record['bundle_sha256']in {*supported_predecessors,candidate},'repair_predecessor_refused')
    ops.record=record
    helpers=Helpers(instance,ops)
    with file_lease(target['prefix']/'recreation-repair.lock'):
        service=ops.state(target['unit'])
        receipt=Receipt(target['prefix'],instance,record,candidate,helpers)
        if service['MainPID']or service['ActiveState']not in ('inactive','failed'):
            require(record['bundle_sha256']==candidate,'repair_requires_stopped_bot')
            prior=receipt.load()
            restored=None
            if prior is not None:
                require(prior['startup_attempted']and prior['phase']in ('startup_attempted','complete'),
                        'repair_receipt_requires_review')
                with file_lease(target['prefix']/'deployment-admission.lock'), \
                     file_lease(target['prefix']/'network-admin.lock'):
                    require(installation.administrative_binding(instance)[2]==record,'repair_registration_changed')
                    require_no_pending(target['prefix'],marker['installation_id'])
                    # Positive current startup observations, never a second start.
                    pilot._wait_ready(target,record)
                    if prior['phase']=='startup_attempted':prior=receipt.advance(prior,'complete')
                    restored=helpers.restore(prior['helpers'])
                    ops.save('helpers-restored-after-interruption',restored)
            return {'ok':True,'phase':'already_running','service_restarted':False,'start_attempts':0,
                    'helpers':restored}
        require(service['UnitFileState']=='disabled','repair_requires_disabled_bot')
        # Never retire a live or partially committed native operation by editing it.
        require_no_pending(target['prefix'],marker['installation_id'])
        job=_control(target['prefix']/'controller-update.json',optional=True,maximum=2*1024**2)
        require(job is None or job.get('phase')in ('complete','cancelled'),
                'repair_review_native_update_before_pausing')
        snapshot=helpers.snapshot()
        intent=receipt.begin(snapshot);snapshot=intent['helpers']
        require(not intent['startup_attempted'],'repair_start_already_attempted_retain_state')
        require(intent['phase']not in ('complete','requires_review'),'repair_receipt_requires_review')
        ops.save('repair-intent',intent)
        helpers.pause(snapshot)
        if intent['phase']=='prepared':intent=receipt.advance(intent,'helpers_paused')
        # Hold throughout upgrade/review/start. Unlike public entry calls, the
        # trusted internal stopped helper and lifecycle accept this admission.
        with file_lease(target['prefix']/'deployment-admission.lock'), \
             file_lease(target['prefix']/'network-admin.lock'):
            try:
                current=installation.administrative_binding(instance)[2]
                require(current==record,'repair_registration_changed')
                installation.require_no_service_processes(record['service_uid'])
                backup_binding={**intent['binding'],'before_sha256':intent['before_sha256']}
                if record['bundle_sha256']==candidate and intent['before_sha256']!=candidate:
                    require((target['prefix']/'recreation-repair-backups'/intent['operation_id']).is_dir(),
                            'repair_original_backup_missing')
                ops.operation='stopped_backup'
                saved=backup(target['root'],target['prefix'],record['service_uid'],
                             intent['operation_id'],backup_binding)
                ops.save('stopped-backup',saved)
                if record['bundle_sha256']!=candidate:
                    require(intent['phase']=='helpers_paused','repair_receipt_requires_review')
                    ops.operation='upgrade'
                    result=upgrade.upgrade(instance,bundle,candidate,from_sha256=record['bundle_sha256'])
                    ops.save('upgrade',result)
                    require(result.get('operator_state_preserved')is True and result.get('service_started')is False
                            and result.get('status')in ('upgraded','already_upgraded'),'repair_upgrade_requires_review')
                target,marker,current=installation.administrative_binding(instance)
                require(current['bundle_sha256']==candidate and all(current[k]==record[k]for k in
                        ('installation_id','generation','service_uid','host_root','prefix','execution')),
                        'repair_registered_successor_required')
                ops.record=current
                # The stopped installer may have armed new reconciliation timers.
                helpers.pause(snapshot)
                installation.require_no_service_processes(current['service_uid'])
                if intent['phase']=='helpers_paused':intent=receipt.advance(intent,'upgraded')
                if intent['phase']in ('upgraded','requires_upstream'):
                    intent=receipt.advance(intent,'ownership_review')
                def ownership(action,**kw):
                    return pilot._ownership_stopped(instance,action,target,current,
                        journal_sha256=kw.get('journal_sha256'),alias=None,room_id=None,
                        confirm_lost_creation=False,transaction_id=None,decision=None)
                # Pin custody is independent of room ownership. Collect this even
                # when an unknown creation will keep startup refused below.
                ops.operation='recovery_pin'
                pin=refresh_pin(instance,current);ops.save('recovery-pin',pin)
                if pin.get('reason')=='recovery_pin_not_ancestor':
                    ops.save('recovery-pin-lineage',diagnose_pin(instance,current))
                ops.operation='ownership_review'
                review=ownership('review');ops.save('ownership-review',review)
                require(review.get('journal_unchanged')is True and not review.get('untracked_active_room_ids')
                        and not review.get('untracked_unconfirmed_room_ids')
                        and type(review.get('rooms'))is list,
                        'repair_ownership_requires_review')
                # A never-started installation legitimately has no journal rows.
                # This is not absence-based recovery: any stored unknown creation
                # is still returned by native review and takes the guarded branch.
                # Startup performs its normal fresh provider/account admission.
                rooms=review['rooms']
                if any(row.get('result')=='unknown_creation'for row in rooms):
                    ops.operation='ownership_inventory'
                    inventory=ownership('inventory');ops.save('owned-history',inventory)
                    require(inventory.get('history_complete')is True and inventory.get('history_stable')is True
                            and inventory.get('absence_recovery_qualified')is False,'repair_inventory_requires_review')
                    if intent['phase']=='ownership_review':intent=receipt.advance(intent,'requires_upstream')
                    outcome={'ok':False,'phase':'requires_upstream_evidence','start_attempts':0,'recovery_pin':pin,
                             'next_action':'retain_state; identify_actual_replacement_or_request_upstream_creation_outcome'}
                else:
                    require(all(row.get('result')in ('local_clean','confirmed_ended')for row in rooms),
                            'repair_ownership_requires_review')
                    if any(row['result']=='confirmed_ended'for row in rooms):
                        ops.operation='ownership_clear'
                        clear=ownership('clear',journal_sha256=review['journal_sha256']);ops.save('ownership-clear',clear)
                        require(clear.get('phase')=='committed'and clear.get('recovery_applied')is True,
                                'repair_ownership_requires_review')
                        review=ownership('review');ops.save('ownership-review-after',review)
                    require(review.get('journal_unchanged')is True and
                            all(row.get('result')=='local_clean'for row in review.get('rooms',[]))
                            and not review.get('untracked_active_room_ids')and not review.get('untracked_unconfirmed_room_ids'),
                            'repair_ownership_requires_review')
                    if intent['phase']=='ownership_review':intent=receipt.advance(intent,'ready')
                    outcome={'ok':True,'phase':'ready_for_start','start_attempts':0,'recovery_pin':pin}
                    if start:
                        intent=receipt.advance(intent,'startup_attempted',journal_sha256=review['journal_sha256'])
                        # Marker is fsynced before the only admitted start call.
                        ops.operation='operator_start'
                        result=pilot.lifecycle(instance,'start',preserve_managed_intent=True,admission_held=True)
                        ops.save('start',result)
                        require(result.get('ok')is True,'repair_start_requires_review')
                        intent=receipt.advance(intent,'complete')
                        outcome.update(phase='started',start_attempts=1)
            except BaseException:
                # The installer may have armed reconciliation immediately before
                # failing. Keep admission until any rearmed helpers are paused.
                failed_operation=getattr(ops,'operation','repair')
                try:helpers.pause(snapshot)
                finally:ops.operation=failed_operation
                raise
        restored=helpers.restore(snapshot,allow_reconciliation=outcome['phase']in ('started','requires_upstream_evidence'))
        ops.save('helpers-restored',restored)
        return {**outcome,'operator_state_preserved':True,'helpers':restored}
