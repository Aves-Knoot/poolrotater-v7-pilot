"""Exact signed controller approval; never relax occupied worker compatibility."""
import time
from pathlib import Path

from ..config import ConfigError
from ..provisioning import _control, protected
from .contracts import canonical, fingerprint, require, version, digest
from .trust import verify


def compatible(before, after):
    # Shared controller code may change only through the exact signed artifact.
    # Stateful migrations and new dependency/interpreter contracts are deliberately
    # refused; those require an independently supported migration, not auto-apply.
    for key in ('protocol','storage_epoch','storage','requirements','python_hash','environment_hash'):
        require(key in before and before[key] == after.get(key), 'update_controller_contract_refused')
    require(before.get('migrations') == after.get('migrations') == [], 'update_migration_refused')
    require(version(after['version']) >= version(before['version']), 'update_controller_downgrade')


def require_approved(before, after, parent, candidate, *, recovering=False):
    compatible(before, after)
    directory = Path(parent['prefix'])/'updates'
    config = _control(protected(directory/'configuration.json'), private=True)
    bootstrap = _control(protected(directory/'bootstrap.json'), private=True)
    approval = _control(protected(directory/'approvals'/(candidate['bundle_sha256']+'.json')),
                        private=True, maximum=512*1024)
    require(fingerprint(bootstrap) == config['bootstrap_sha256'], 'update_approval_bootstrap_changed')
    require(approval['installation_id'] == parent['installation_id']
            and approval['generation'] == parent['generation']
            and approval['before_sha256'] == parent['bundle_sha256']
            and approval['after_sha256'] == candidate['bundle_sha256'], 'update_approval_binding_refused')
    baseline=digest(approval.get('baseline_sha256',parent['bundle_sha256']))
    if baseline!=parent['bundle_sha256']:
        # A signed transition may follow a completed worker roll. That worker
        # has identical controller/storage contracts to this registered parent;
        # prove its exact root approval instead of weakening parent admission.
        from ..worker_releases import receipt_path,validate_candidate
        approved=_control(protected(receipt_path(parent,baseline)))
        runtime=validate_candidate(parent,approved)
        if not recovering:
            host=_control(Path(parent['host_root'])/'config/host.json',parent['service_uid'],private=True)
            rows=_control(Path(parent['host_root'])/'state/rolling/ownership.json',parent['service_uid'],private=True)['rooms']
            require(bool(rows)and set(rows)=={r['alias']for r in host['rooms']}and all(
                r.get('release')==str(runtime['release'])and r.get('ownership_unknown')is False
                for r in rows.values()),'update_approval_worker_baseline_changed')
    # Explicit interrupted-transaction recovery may use the durable acceptance
    # time. New queue/commit admission always needs presently unexpired metadata.
    now = approval['accepted_at'] if recovering else time.time()
    payload, _ = verify(canonical(approval['envelope']), bootstrap, now=now)
    item = next((r for r in payload['releases'] if r['artifact']['sha256'] == candidate['bundle_sha256']), None)
    require(item is not None and item['kind'] == 'controller'
            and baseline in item['from_sha256']
            and item['execution'] == candidate['execution'] == parent['execution']
            and item['version'] == after['version'], 'update_approval_artifact_refused')


def require_upgrade(before, after, parent, candidate, *, recovering=False):
    from ..pilot_upgrade import require_repair_compatible
    try:
        require_repair_compatible(before, after)
    except ConfigError:
        require_approved(before, after, parent, candidate, recovering=recovering)
