"""Explicit stopped owner pacing adoption, outside service-writable state.

Root validates the private source and completed adoption before passing the
closed proof below. A permanent protected requirement pins the policy: deleting
or interrupting it cannot silently restore the default strict pacing policy.
No account/client identifiers, credentials or provider operations are recorded.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from .config import ConfigError
from .layout import file_lease
from .oauth import AuthError
from .provisioning import protected, _control, _write


def _identity(marker, fingerprint):
    try:
        if type(marker) is not dict: raise ValueError
        for key in ('installation_id', 'generation'):
            if str(uuid.UUID(marker[key])) != marker[key]: raise ValueError
        if type(fingerprint) is not str or not re.fullmatch('[a-f0-9]{64}', fingerprint): raise ValueError
    except Exception: raise ConfigError('invalid provider budget binding') from None
    return {key: marker[key] for key in ('installation_id', 'generation')} | {'client_profile_sha256': fingerprint}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _paths(prefix, identity):
    prefix = protected(Path(prefix))
    directory = prefix / 'provider-budgets'
    return directory, directory / (identity['installation_id'] + '.json'), directory / (identity['installation_id'] + '.required.json')


def _proof(proof, identity):
    keys = {'installation_id','generation','snapshot_sha256','phase','audit_ok','source_rpm','source_burst'}
    if (type(proof) is not dict or set(proof) != keys
            or any(proof[key] != identity[key] for key in ('installation_id','generation'))
            or proof['phase'] != 'committed' or proof['audit_ok'] is not True
            or type(proof['snapshot_sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', proof['snapshot_sha256'])
            or type(proof['source_rpm']) is not int or proof['source_rpm'] != 50
            or type(proof['source_burst']) is not int or proof['source_burst'] != 60):
        raise ConfigError('provider budget requires verified completed owner 50/60 adoption')
    return deepcopy(proof)


def _policy(value, identity):
    if (type(value) is not dict or set(value) != {'format',*identity,'algorithm','minimum_interval_seconds','burst_capacity','owner_proof'}
            or type(value['format']) is not int or value['format'] != 1
            or any(type(value[key]) is not type(expected) or value[key] != expected for key,expected in identity.items())
            or value['algorithm'] != 'gcra-owner'
            or type(value['minimum_interval_seconds']) is not float or value['minimum_interval_seconds'] != 1.2
            or type(value['burst_capacity']) is not int or value['burst_capacity'] != 60):
        raise ConfigError('protected provider budget invalid')
    _proof(value['owner_proof'], identity)
    return value


def _requirement(value, identity):
    if (type(value) is not dict or set(value) != {'format',*identity,'policy_sha256'}
            or type(value['format']) is not int or value['format'] != 1
            or any(type(value[key]) is not type(expected) or value[key] != expected for key,expected in identity.items())
            or type(value['policy_sha256']) is not str or not re.fullmatch('[a-f0-9]{64}', value['policy_sha256'])):
        raise ConfigError('protected provider budget requirement invalid')
    return value


def configure(prefix, marker, fingerprint, owner_proof, *, stopped_guard):
    """Idempotent root-only configure/resume of one independently reviewed proof.

    The fixed installed command supplies its existing stopped units/processes,
    owner lease and runtime guard. There is no caller-selected policy or path in
    that command. Once required, replacing/removing the policy needs review;
    repeating the same command repairs an interrupted or lost policy only.
    """
    if os.geteuid() != 0 or not callable(stopped_guard):
        raise ConfigError('provider budget preparation requires stopped administrator guard')
    stopped_guard()
    identity = _identity(marker, fingerprint); proof = _proof(owner_proof, identity)
    directory, path, required = _paths(prefix, identity)
    directory.mkdir(mode=0o755, exist_ok=True); protected(directory)
    directory.chmod(0o755)  # administrator umask must not hide the sealed policy
    policy = {'format':1,**identity,'algorithm':'gcra-owner','minimum_interval_seconds':1.2,
              'burst_capacity':60,'owner_proof':proof}
    requirement = {'format':1,**identity,'policy_sha256':_digest(policy)}
    with file_lease(directory / '.admin.lock'):
        stopped_guard()
        current_required = _control(required, optional=True, maximum=8192)
        current_policy = _control(path, optional=True, maximum=8192)
        if current_required is not None and _requirement(current_required,identity) != requirement:
            raise ConfigError('existing provider budget requires independent replacement review')
        if current_policy is not None and _policy(current_policy,identity) != policy:
            raise ConfigError('existing provider budget differs from reviewed owner proof')
        if current_required is None: _write(required, requirement)
        # A crash between these writes refuses admission until exact resume.
        stopped_guard()
        if current_policy is None: _write(path, policy)
        protected(required); protected(path)
    return {'ok':True,'algorithm':'gcra-owner','requests_per_minute':50,'burst_capacity':60,
            'policy_sha256':_digest(policy),'services_started':False,'provider_dispatch':False}


def loader(prefix, marker, fingerprint):
    """Bind a read-only loader to the already validated protected installation."""
    identity = _identity(marker, fingerprint)
    directory,path,required = _paths(prefix,identity)
    pinned = [None]

    def read():
        try:
            protected(Path(prefix))
            if not os.path.lexists(directory):
                if pinned[0] is not None: raise ValueError
                return None
            protected(directory)
            requirement = _control(required, optional=True, maximum=8192)
            policy = _control(path, optional=True, maximum=8192)
            if requirement is None and policy is None:
                if pinned[0] is not None: raise ValueError
                return None
            if requirement is None or policy is None: raise ValueError
            protected(required); protected(path)
            _requirement(requirement, identity); _policy(policy,identity)
            identity_hash = _digest(policy)
            if identity_hash != requirement['policy_sha256']: raise ValueError
            if pinned[0] is not None and pinned[0] != identity_hash: raise ValueError
            pinned[0] = identity_hash
            return deepcopy(policy)
        except Exception: raise AuthError('provider_budget_requires_review') from None
    return read


def require_owner_adoption(root, gate):
    """Read-only startup parity fence; caller retains existing owner authority.

    A fresh operator has no owner adoption receipt and keeps strict pacing.
    An adopted owner must have the protected budget from that exact completed
    source snapshot. This is checked before any room mutation, without OAuth,
    token writes, network requests or another lifetime account/owner lease.
    """
    from .owner_migration import record
    from .provider_pacing import ProviderRequestGate
    if type(gate) is not ProviderRequestGate:
        raise AuthError('owner_provider_budget_mismatch')
    adoption = record(root)
    measured = gate.snapshot()['budget']
    if adoption is None:
        return {'owner_budget_required':False,'budget':measured}
    if adoption['phase'] != 'committed':
        raise AuthError('owner_provider_budget_requires_completed_adoption')
    if gate._budget is None:
        raise AuthError('owner_provider_budget_required')
    if (gate._budget['owner_proof']['snapshot_sha256'] != adoption['snapshot_sha256']
            or measured != {'source':'protected-owner-adoption','algorithm':'gcra-owner',
                            'minimum_interval_seconds':1.2,'burst_capacity':60}):
        raise AuthError('owner_provider_budget_mismatch')
    return {'owner_budget_required':True,'budget':measured}
