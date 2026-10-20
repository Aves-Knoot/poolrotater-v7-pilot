"""Explicit stopped adoption of an independently pinned replacement catalog root.

Old pilot signers cannot be recovered. A replacement therefore authenticates a
fresh complete set before replacing anything. The original store is retained
for recovery. No account credentials, local votes or host settings are changed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import uuid
from contextlib import nullcontext

from .catalog_export import MODES, CORE_MODES, complete_modes, inspect_bundle, sha256
from .catalog_store import CatalogStore, MAX_VERSIONS, RESERVE, _hash, _sync
from .catalog_trust import BootstrapTrust, verified_local_catalog, verified_target
from .config import ConfigError, fields
from .layout import unique_fields
from .runtime_package import json_bytes


def pending_path(store):
    return store.path.parent / ('.trust-transition-' + store.binding['installation_id'] + '.json')


def _paths(store, transaction):
    if type(transaction) is not str or len(transaction) != 32 or any(c not in '0123456789abcdef' for c in transaction):
        raise ConfigError('catalog trust transaction invalid')
    return (store.path.parent / ('.trust-candidate-' + transaction),
            store.path.parent / ('.trust-retired-' + transaction))


def _inventory(store, path):
    """Hash bounded attributable data; the sole TUF cache link is explicit."""
    store._check_path(path, directory=True)
    result = {}
    for index, entry in enumerate(sorted(path.rglob('*'))):
        if index > MAX_VERSIONS * 8 + 512:
            raise ConfigError('catalog trust inventory budget exceeded')
        relative = entry.relative_to(path).as_posix()
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(entry)
            import re
            if relative != 'cache/metadata/root.json' or not re.fullmatch(r'root_history/[1-9][0-9]{0,9}\.root\.json', target):
                raise ConfigError('catalog trust inventory link refused')
            result[relative] = {'link': target}
        elif stat.S_ISDIR(info.st_mode):
            if relative.startswith('cache/'):
                if info.st_uid != (store.binding['service_uid'] if store.fixture else 0) or info.st_mode & 0o022:
                    raise ConfigError('catalog trust cache ownership changed')
            else: store._check_path(entry, directory=True)
        else:
            if relative.startswith('cache/'):
                if (not stat.S_ISREG(info.st_mode)
                        or info.st_uid != (store.binding['service_uid'] if store.fixture else 0)
                        or info.st_mode & 0o022 or info.st_nlink != 1):
                    raise ConfigError('catalog trust cache ownership changed')
            else: store._check_path(entry)
            if info.st_size > 512 * 1024**2:
                raise ConfigError('catalog trust inventory file exceeded')
            result[relative] = {'sha256': sha256(entry), 'bytes': info.st_size}
    return result


def _policy(store, repository, stage):
    """New trust must explicitly carry every existing exclusion scope."""
    include_catch='catch' in stage.current()['modes']
    include_loved='loved' in stage.current()['modes']
    stage._accept_exclusions_locked(repository,include_catch=include_catch,include_loved=include_loved)
    for mode in [None]+(['catch'] if include_catch else [])+(['loved'] if include_loved else []):
        previous=store.exclusions(mode=mode)
        state=stage.exclusions(mode=mode)
        active={(m,e['kind'],e['id']) for e in state['events'] for m in e['modes']}
        for old in previous['events']:
            for scope in old['modes']:
                if (scope,old['kind'],old['id']) not in active:
                    raise ConfigError('replacement catalog trust drops existing exclusion')


def _stage(store, repository, trust, transaction, modes):
    stage_path, _ = _paths(store, transaction)
    if os.path.lexists(stage_path):
        raise ConfigError('catalog trust stage already exists')
    stage_path.mkdir(mode=0o700 if store.fixture else 0o755)
    stage = CatalogStore(store.root, stage_path, store.binding)
    for name in ('versions', 'receipts', '.staging', 'cache'):
        (stage_path / name).mkdir(mode=0o700 if store.fixture or name in ('.staging', 'cache') else 0o755)
    stage._write(stage_path / 'binding.json', store.binding)
    (stage_path / 'bootstrap.json').write_bytes(trust.data)
    (stage_path / 'bootstrap.json').chmod(0o400 if store.fixture else 0o444)
    _sync(stage_path / 'bootstrap.json')
    selected = {}; snapshot = None
    for mode in modes:
        with verified_local_catalog(repository, stage_path / 'cache', trust, mode,
                                    workspace=stage_path / '.staging') as kit:
            observed = sha256(stage_path / 'cache/metadata/snapshot.json')
            if snapshot is not None and snapshot != observed:
                raise ConfigError('replacement catalog repository changed during staging')
            snapshot = observed
            identity = kit.manifest['content_id']; destination = stage_path / 'versions' / identity
            destination.mkdir(mode=0o700)
            for source in kit.folder.iterdir():
                shutil.copyfile(source, destination / source.name)
                (destination / source.name).chmod(0o400 if store.fixture else 0o444)
                _sync(destination / source.name)
            if inspect_bundle(destination) != kit.manifest:
                raise ConfigError('replacement catalog bytes changed')
            destination.chmod(0o500 if store.fixture else 0o555)
            _sync(destination)
            stage._write(stage_path / 'receipts' / (identity + '.json'),
                {'format': 1, 'mode': mode, 'content_id': identity,
                 'manifest_sha256': sha256(destination / 'manifest.json'), 'bootstrap_sha256': trust.identity})
            stage.version(mode, identity); selected[mode] = identity
    reference = stage._reference(selected, serial=store.current()['serial'] + 1)
    stage._complete(reference)
    stage._write(stage_path / 'current.json', reference)
    _policy(store, repository, stage)
    if sha256(stage_path / 'cache/metadata/snapshot.json') != snapshot:
        raise ConfigError('replacement exclusions changed catalog repository snapshot')
    _sync(stage_path)
    return stage, reference


def replace(store, repository, trust, *, old_sha256, new_sha256, confirm_replacement=False, failpoint=None, owner_lease_held=False):
    """Authenticate complete full catalogs first, then journal and swap stopped."""
    if type(confirm_replacement) is not bool or not confirm_replacement:
        raise ConfigError('explicit catalog trust replacement confirmation required')
    if type(owner_lease_held) is not bool:
        raise ConfigError('catalog trust stopped lease proof invalid')
    if (type(trust) is not BootstrapTrust or trust.fixture != store.fixture
            or _hash(new_sha256) != trust.identity or _hash(old_sha256) != store.trust.identity
            or old_sha256 == new_sha256):
        raise ConfigError('independently pinned old and replacement trust identities required')
    with (nullcontext() if owner_lease_held else store.stopped()), store._writer():
        before = store.current()
        if any(os.path.lexists(store.path / name) for name in ('pending.json', 'cleanup-pending.json')):
            raise ConfigError('catalog transaction must settle before trust replacement')
        candidate = store._read('candidate.json', optional=True)
        if candidate is not None and store._journal(candidate)['after'] != before:
            raise ConfigError('uncommitted catalog candidate must settle before trust replacement')
        modes = sorted(set(CORE_MODES) | set(before['modes']) |
                       ({'catch'} if (store.path/'exclusions-catch.json').exists() else set()) |
                       ({'loved'} if (store.path/'exclusions-loved.json').exists() else set()))
        if os.statvfs(store.path).f_bavail * os.statvfs(store.path).f_frsize < RESERVE + 1024**3:
            raise ConfigError('catalog trust replacement disk reserve required')
        old_inventory = _inventory(store, store.path)
        transaction = uuid.uuid4().hex
        stage_path, _ = _paths(store, transaction)
        try:
            stage, after = _stage(store, repository, trust, transaction, modes)
            if _inventory(store, store.path) != old_inventory:
                raise ConfigError('old catalog store changed during trust preparation')
            journal = {'format': 1, 'transaction_id': transaction, 'binding': store.binding,
                       'before_sha256': old_sha256, 'after_sha256': new_sha256,
                       'before_reference': before, 'after_reference': after,
                       'before_inventory': old_inventory, 'after_inventory': _inventory(stage, stage_path)}
            store._write(pending_path(store), journal)
        except BaseException:
            if not os.path.lexists(pending_path(store)) and os.path.lexists(stage_path):
                _discard_stage(stage_path)
            raise
        if failpoint is not None: failpoint('prepared')
        return _finish(store, journal, failpoint=failpoint)


def _discard_stage(path):
    # Only freshly-created owned stage, never a caller-selected tree.
    for entry in path.rglob('*'):
        if entry.is_dir() and not entry.is_symlink(): entry.chmod(0o700)
    path.chmod(0o700)
    shutil.rmtree(path)


def _journal(store):
    path = pending_path(store)
    store._check_path(path)
    from .catalog_trust import bounded_file
    try:
        with bounded_file(path, 2 * 1024**2) as handle:
            value = json.load(handle, object_pairs_hook=unique_fields,
                              parse_constant=lambda _: (_ for _ in ()).throw(ConfigError('nonfinite catalog trust journal')))
    except (ValueError, UnicodeError) as exc:
        raise ConfigError('invalid catalog trust journal JSON') from exc
    fields(value, ('format', 'transaction_id', 'binding', 'before_sha256', 'after_sha256',
                  'before_reference', 'after_reference', 'before_inventory', 'after_inventory'), 'catalog trust journal')
    if type(value['format']) is not int or value['format'] != 1 or value['binding'] != store.binding:
        raise ConfigError('catalog trust journal binding changed')
    _paths(store, value['transaction_id'])
    _hash(value['before_sha256']); _hash(value['after_sha256'])
    if value['before_sha256'] == value['after_sha256']:
        raise ConfigError('catalog trust journal unchanged roots')
    for kind in ('before_inventory', 'after_inventory'):
        if not isinstance(value[kind], dict) or len(value[kind]) > MAX_VERSIONS * 8 + 512:
            raise ConfigError('catalog trust journal inventory invalid')
    _references(value)
    return value


def _references(journal):
    for side in ('before', 'after'):
        ref = journal[side + '_reference']; root = journal[side + '_sha256']
        fields(ref, ('format', 'serial', 'modes', 'bootstrap_sha256', 'set_id'), 'catalog trust reference')
        if (type(ref['format']) is not int or ref['format'] != 1
                or type(ref['serial']) is not int or not 0 <= ref['serial'] <= 2**53
                or not isinstance(ref['modes'], dict) or not set(ref['modes']) <= set(MODES)
                or any(_hash(identity) != identity for identity in ref['modes'].values())
                or ref['bootstrap_sha256'] != root):
            raise ConfigError('catalog trust reference fields changed')
        body = {key: ref[key] for key in ('format', 'serial', 'modes', 'bootstrap_sha256')}
        if ref['set_id'] != hashlib.sha256(json_bytes(body)).hexdigest():
            raise ConfigError('catalog trust reference hash changed')
        entry = journal[side + '_inventory'].get('bootstrap.json')
        if not isinstance(entry, dict) or entry.get('sha256') != root:
            raise ConfigError('catalog trust inventory root pin changed')
    if (journal['after_reference']['serial'] != journal['before_reference']['serial'] + 1
            or not complete_modes(journal['after_reference']['modes'])
            or not set(journal['before_reference']['modes']) <= set(journal['after_reference']['modes'])):
        raise ConfigError('catalog trust replacement complete-set sequence changed')


def _validate_copy(store, path, journal, side):
    if _inventory(store, path) != journal[side + '_inventory']:
        raise ConfigError('catalog trust ' + side + ' store diverged')
    candidate = CatalogStore(store.root, path, store.binding)
    if (candidate._read('binding.json') != store.binding
            or candidate.trust.identity != journal[side + '_sha256']
            or candidate._validate_reference(candidate._read('current.json')) != journal[side + '_reference']):
        raise ConfigError('catalog trust ' + side + ' authority diverged')
    reference = journal[side + '_reference']
    if side == 'after': candidate._complete(reference)
    else:
        for mode, identity in reference['modes'].items(): candidate.version(mode, identity)
    from .catalog_exclusions import initial_state, validate_state
    policy = candidate._read('exclusions.json', optional=True)
    validate_state(policy if policy is not None else initial_state(candidate.trust.identity), candidate.trust.identity)


def _finish(store, journal, *, failpoint=None):
    _references(journal)
    stage_path, retired = _paths(store, journal['transaction_id'])
    current_exists = os.path.lexists(store.path)
    retired_exists = os.path.lexists(retired)
    # Qualify both complete authorities before moving the last usable old path.
    # Interrupted pre-publication data cannot strand a currently intact store.
    _validate_copy(store, retired if retired_exists else store.path, journal, 'before')
    _validate_copy(store, stage_path if os.path.lexists(stage_path) else store.path, journal, 'after')
    if not retired_exists:
        if not current_exists or _inventory(store, store.path) != journal['before_inventory']:
            raise ConfigError('catalog trust old store diverged')
        os.rename(store.path, retired); _sync(store.path.parent)
        if failpoint is not None: failpoint('retired')
        current_exists = False
    elif _inventory(store, retired) != journal['before_inventory']:
        raise ConfigError('catalog trust retained store diverged')
    if not current_exists:
        if _inventory(store, stage_path) != journal['after_inventory']:
            raise ConfigError('catalog trust staged store diverged')
        os.rename(stage_path, store.path); _sync(store.path.parent)
        if failpoint is not None: failpoint('installed')
    elif os.path.lexists(stage_path):
        raise ConfigError('catalog trust has ambiguous installed and staged stores')
    if _inventory(store, store.path) != journal['after_inventory']:
        raise ConfigError('catalog trust installed store diverged')
    replacement = CatalogStore(store.root, store.path, store.binding)
    reference = replacement._validate_reference(replacement._read('current.json'))
    if reference != journal['after_reference'] or replacement.trust.identity != journal['after_sha256']:
        raise ConfigError('catalog trust new reference diverged')
    replacement._complete(reference)
    receipt = {key: journal[key] for key in ('format', 'transaction_id', 'before_sha256', 'after_sha256',
                                           'before_reference', 'after_reference')}
    replacement._write(store.path.parent / ('.trust-receipt-' + journal['transaction_id'] + '.json'), receipt)
    pending_path(store).unlink(); _sync(store.path.parent)
    return {'ok': True, 'phase': 'committed', 'transaction_id': journal['transaction_id'],
            'catalog_set': reference['set_id'], 'full_modes': sorted(reference['modes']),
            'old_store_retained': True, 'operator_state_preserved': True, 'live_ready': False}


def resume(store, *, transaction_id, owner_lease_held=False):
    """The caller reconstructs fixed protected binding without opening the store."""
    if type(owner_lease_held) is not bool:
        raise ConfigError('catalog trust stopped lease proof invalid')
    with nullcontext() if owner_lease_held else store.stopped():
        # The normal store is fenced while pending; serialize on a stable sibling
        # inode rather than the directory being moved between two names.
        from .layout import file_lease
        with file_lease(store.path.parent / ('.trust-recovery-' + store.binding['installation_id'] + '.lock')):
            journal = _journal(store)
            if journal['transaction_id'] != transaction_id:
                raise ConfigError('catalog trust recovery transaction mismatch')
            return _finish(store, journal)
