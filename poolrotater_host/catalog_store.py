"""Offline signed catalog store: immutable versions and atomic complete-set refs.

The administrative prototype writes only disposable fixture mounts. Ordinary
owned stores require explicit fixture trust and cannot be used by a provisioned
host. A VerifiedCatalog dataclass is never accepted as installation authority.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid

from .catalog_export import MODES, inspect_bundle, sha256
from .catalog_trust import BootstrapTrust, bounded_file, no_links, verified_local_catalog
from .config import ConfigError, PRESETS, contained, fields
from .layout import atomic_json, file_lease, load, read_json
from .runtime_package import fixture_only, json_bytes

POINTER = 'state/catalog-installation.json'
RESERVE = 5 * 1024**3
MAX_VERSIONS = 128
CATALOG_KEYS = ('loved_db', 'loved_mode', 'catch_catalog', 'farm_db', 'standard_dt_db', 'standard_catalog_binding', 'mania_db',
                'mania_farm_db', 'mania_farm_enabled', 'mania_convert_db',
                'mania_convert_validation_db', '_host_catalog_set', '_host_catalog_reference')


def mode_for(room):
    spec = PRESETS[room['preset']]
    return 'loved' if room['preset'] == 'standard-loved' else 'standard' if spec['ruleset'] == 0 else 'catch' if spec['ruleset'] == 2 else 'mania' + str(spec['keys'])


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise ConfigError('invalid catalog content/reference identity')
    return value


def _sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)


def _identity(marker, root, uid, store, fixture, *, execution='offline-fixture'):
    extra = {} if execution == 'offline-fixture' else {'execution': execution}
    return {**marker, 'host_root': str(root), 'service_uid': uid,
            'store': str(store), 'fixture': fixture, **extra}


class CatalogStore:
    """Use initialize/open factories; every read checks ownership and binding."""
    def __init__(self, root, store, binding):
        self.root, self.path, self.binding = Path(root), Path(store), binding
        self.fixture = binding['fixture']

    @classmethod
    def initialize_fixture(cls, root, trust):
        fixture_only()
        if not isinstance(trust, BootstrapTrust) or trust.fixture is not True:
            raise ConfigError('owned catalog stores require explicit offline fixture trust')
        root = Path(root).absolute(); marker, _ = load(root)
        if os.path.lexists(root / 'state/runtime-installation.json'):
            raise ConfigError('provisioned hosts require protected catalog ownership')
        return cls._initialize(root, root / 'catalogs/.managed', marker, os.geteuid(), trust, True)

    @classmethod
    def initialize_protected(cls, root, prefix, trust, *, uid=1000):
        _administrator(root, prefix)
        from .provisioning import _host_identity, protected
        marker = _host_identity(Path(root), uid)
        if not isinstance(trust, BootstrapTrust) or trust.fixture is not True:
            raise ConfigError('this administrative prototype requires explicit offline fixture trust')
        from .runtime_lifecycle import _administrative_binding
        _, bound_prefix, _, _, _ = _administrative_binding(root, uid)
        if Path(prefix).absolute() != bound_prefix: raise ConfigError('catalog prefix must match installed runtime')
        prefix = protected(prefix)
        parent = prefix / 'catalog-stores'
        parent.mkdir(mode=0o755, exist_ok=True); protected(parent)
        return cls._initialize(Path(root).absolute(), parent / marker['installation_id'], marker, uid, trust, False)

    @classmethod
    def initialize_pilot(cls, instance, trust, expected_sha256):
        from .pilot_installation import administrative_binding
        from .pilot_identity import CANDIDATE
        return cls._initialize_pilot(instance, trust, expected_sha256,
                                     administrative_binding, administrative_binding(instance)[2]['execution'])

    @classmethod
    def _initialize_pilot(cls, instance, trust, expected_sha256, binding_loader, execution):
        from .provisioning import protected
        target, marker, record = binding_loader(instance)
        if (type(trust) is not BootstrapTrust or trust.fixture is not False
                or _hash(expected_sha256) != trust.identity):
            raise ConfigError('pilot catalogs require independently confirmed nonfixture bootstrap trust')
        prefix = target['prefix']; root = target['root']
        parent = prefix / 'catalog-stores'
        parent.mkdir(mode=0o755, exist_ok=True); protected(parent)
        return cls._initialize(root, parent / marker['installation_id'], marker,
                               record['service_uid'], trust, False, execution=execution)

    @classmethod
    def open_pilot_administrative(cls, instance):
        from .pilot_installation import administrative_binding
        from .pilot_identity import CANDIDATE
        return cls._open_pilot_administrative(instance, administrative_binding, administrative_binding(instance)[2]['execution'])

    @classmethod
    def _open_pilot_administrative(cls, instance, binding_loader, execution):
        from .provisioning import _control
        target, marker, record = binding_loader(instance)
        root = target['root']; store = target['prefix'] / 'catalog-stores' / marker['installation_id']
        binding = _identity(marker, root, record['service_uid'], store, False, execution=execution)
        if _control(root / POINTER, record['service_uid'], private=True) != binding:
            raise ConfigError('pilot catalog pointer belongs to another installation')
        result = cls(root, store, binding); result._check()
        return result

    @classmethod
    def _initialize(cls, root, store, marker, uid, trust, fixture, *, execution='offline-fixture'):
        binding = _identity(marker, root, uid, store, fixture, execution=execution)
        pointer = root / POINTER
        if os.path.lexists(pointer):
            # Do not adopt a preexisting directory or overwrite a different root.
            from .provisioning import _control
            actual = read_json(pointer) if fixture else _control(pointer, uid, private=True)
            if actual != binding: raise ConfigError('catalog pointer already belongs to another binding')
            existing = cls(root, store, binding); existing._check()
            if existing.trust.identity != trust.identity: raise ConfigError('catalog bootstrap trust cannot be replaced')
            return existing
        if os.path.lexists(store): raise ConfigError('refusing unregistered catalog store adoption')
        no_links(store.parent)
        store.mkdir(mode=0o700 if fixture else 0o755)
        result = cls(root, store, binding)
        for name in ('versions', 'receipts', '.staging', 'cache'):
            (store / name).mkdir(mode=0o700 if fixture or name in ('.staging', 'cache') else 0o755)
        result._write(store / 'binding.json', binding)
        (store / 'bootstrap.json').write_bytes(trust.data)
        (store / 'bootstrap.json').chmod(0o400 if fixture else 0o444)
        _sync(store / 'bootstrap.json')
        result._write(store / 'current.json', result._reference({}))
        _sync(store)
        if fixture: atomic_json(pointer, binding)
        else:
            from .provisioning import _write
            _write(pointer, binding, owner=uid, private=True)
        return result

    @classmethod
    def open(cls, root):
        root = Path(root).absolute(); marker, _ = load(root)
        pointer = contained(root, POINTER)
        binding = read_json(pointer)
        execution = binding.get('execution', 'offline-fixture') if isinstance(binding, dict) else None
        from .pilot_identity import installation_execution
        installation_execution(execution)
        extra = () if execution == 'offline-fixture' else ('execution',)
        fields(binding, ('format', 'installation_id', 'generation', 'host_root', 'service_uid', 'store', 'fixture', *extra), 'catalog binding')
        if type(binding['fixture']) is not bool or type(binding['service_uid']) is not int:
            raise ConfigError('invalid catalog ownership binding')
        store = Path(binding['store'])
        if binding != _identity(marker, root, os.geteuid(), store, binding['fixture'], execution=execution):
            raise ConfigError('catalog installation/operator binding changed')
        if binding['fixture']:
            if execution != 'offline-fixture': raise ConfigError('pilot catalogs require protected ownership')
            fixture_only()
            if store != root / 'catalogs/.managed' or os.path.lexists(root / 'state/runtime-installation.json'):
                raise ConfigError('owned fixture cannot supply a provisioned host')
        else:
            # Namespace authority only: bootstrap/controller/spawn retain the full
            # immutable runtime guard. Do not rehash the whole interpreter on
            # every configuration stamp or worker preference refresh.
            from .provisioning import protected, _control, _pointer
            from .runtime_lifecycle import REGISTRATION_KEYS, require_no_pending
            pointer = read_json(root / 'state/runtime-installation.json')
            if not isinstance(pointer, dict) or not isinstance(pointer.get('prefix'), str):
                raise ConfigError('protected runtime pointer required')
            prefix = protected(Path(pointer['prefix']))
            if pointer != _pointer(marker, prefix): raise ConfigError('catalog/runtime identity changed')
            registration_path = prefix / 'installations' / (marker['installation_id'] + '.json')
            protected(registration_path); registration = _control(registration_path)
            expected = {**pointer, 'service_uid': os.geteuid(), 'host_root': str(root),
                        'execution': execution, 'live_ready': False}
            if (not isinstance(registration, dict) or set(registration) != REGISTRATION_KEYS
                    or any(type(registration.get(k)) is not type(v) or registration.get(k) != v
                           for k, v in expected.items())):
                raise ConfigError('catalog/runtime registration changed')
            require_no_pending(prefix, marker['installation_id'])
            if store != prefix / 'catalog-stores' / marker['installation_id']:
                raise ConfigError('catalog store does not belong to protected installation')
        result = cls(root, store, binding); result._check()
        return result

    @classmethod
    def open_administrative(cls, root, prefix, *, uid=1000):
        _administrator(root, prefix)
        from .runtime_lifecycle import _administrative_binding
        from .provisioning import _control
        root, installed, marker, _, _ = _administrative_binding(root, uid)
        if installed != Path(prefix).absolute(): raise ConfigError('catalog/runtime prefix mismatch')
        store = installed / 'catalog-stores' / marker['installation_id']
        binding = _identity(marker, root, uid, store, False)
        if _control(root / POINTER, uid, private=True) != binding:
            raise ConfigError('catalog administrative pointer changed')
        result = cls(root, store, binding); result._check()
        return result

    def _check_path(self, path, *, directory=False):
        path = no_links(path); st = path.stat()
        expected_uid = self.binding['service_uid'] if self.fixture else 0
        if (st.st_uid != expected_uid or st.st_mode & (0o077 if self.fixture else 0o022)
                or (not directory and (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1))
                or (directory and not stat.S_ISDIR(st.st_mode))):
            raise ConfigError('catalog store ownership/type/permissions changed')
        if not self.fixture:
            from .provisioning import protected
            protected(path)
        return path

    def _read(self, name, *, optional=False):
        path = self.path / name
        if optional and not os.path.lexists(path): return None
        self._check_path(path)
        with bounded_file(path, 65536) as f:
            from .layout import unique_fields
            try: return json.load(f, object_pairs_hook=unique_fields,
                                  parse_constant=lambda _: (_ for _ in ()).throw(ConfigError('nonfinite catalog state')))
            except (ValueError, UnicodeError) as exc: raise ConfigError('invalid catalog state JSON') from exc

    def _write(self, path, data):
        # Set final readability before atomic replacement, including crash windows.
        atomic_json(path, data, mode=0o600 if self.fixture else 0o644)
        _sync(path); _sync(path.parent)

    def _check(self):
        from .catalog_rekey import pending_path
        if os.path.lexists(pending_path(self)):
            raise ConfigError('catalog trust transition requires explicit recovery')
        for name in ('', 'versions', 'receipts', '.staging', 'cache'):
            # Service users cannot inspect private administrator workspaces.
            if not self.fixture and os.geteuid() != 0 and name in ('.staging', 'cache'): continue
            self._check_path(self.path / name, directory=True)
        if self._read('binding.json') != self.binding:
            raise ConfigError('protected catalog registration changed')
        self._check_path(self.path / 'bootstrap.json')

    @property
    def trust(self):
        with bounded_file(self.path / 'bootstrap.json', 65536) as f:
            return BootstrapTrust(f.read(), fixture=self.binding.get('execution', 'offline-fixture') == 'offline-fixture')

    def _reference(self, modes, *, serial=0):
        body = {'format': 1, 'serial': serial, 'modes': modes, 'bootstrap_sha256': self.trust.identity}
        import hashlib
        return {**body, 'set_id': hashlib.sha256(json_bytes(body)).hexdigest()}

    def _validate_reference(self, ref):
        fields(ref, ('format', 'serial', 'modes', 'bootstrap_sha256', 'set_id'), 'catalog reference')
        if (type(ref['format']) is not int or ref['format'] != 1 or type(ref['serial']) is not int
                or not 0 <= ref['serial'] <= 2**53 or not isinstance(ref['modes'], dict)
                or not set(ref['modes']) <= set(MODES)
                or any(_hash(v) != v for v in ref['modes'].values())
                or ref != self._reference(ref['modes'], serial=ref['serial'])):
            raise ConfigError('invalid complete catalog reference')
        return ref

    def version(self, mode, identity):
        if mode not in MODES: raise ConfigError('unsupported catalog mode')
        _hash(identity)
        path = self.path / 'versions' / identity
        self._check_path(path, directory=True)
        if path.stat().st_mode & 0o222: raise ConfigError('catalog version directory is not sealed')
        # Bound inventory before inspecting contents (also rejects symlink/extra adoption).
        children = list(path.iterdir())
        if len(children) > 5: raise ConfigError('catalog version inventory exceeded')
        for p in children:
            self._check_path(p)
            if p.stat().st_mode & 0o222: raise ConfigError('catalog version file is not sealed')
        manifest = inspect_bundle(path)
        receipt = self._read('receipts/' + identity + '.json')
        expected = {'format': 1, 'mode': mode, 'content_id': identity,
                    'manifest_sha256': sha256(path / 'manifest.json'), 'bootstrap_sha256': self.trust.identity}
        if receipt != expected or manifest['content_id'] != identity or manifest['mode'] != mode:
            raise ConfigError('catalog version lacks matching protected verification receipt')
        return path, manifest

    def current(self, *, allow_pending=False):
        self._check()
        if not allow_pending and os.path.lexists(self.path / 'pending.json'):
            raise ConfigError('catalog transaction requires explicit recovery')
        return self._validate_reference(self._read('current.json'))

    def status(self):
        result, _ = self._inspection()
        return result

    def _inspection(self):
        ref = self.current(allow_pending=True)
        pending = self._read('pending.json', optional=True)
        if pending is not None: self._journal(pending)
        manifests = {mode: self.version(mode, identity)[1] for mode, identity in ref['modes'].items()}
        return ({'reference': ref, 'recovery_required': pending is not None,
                 'fixture': self.fixture, 'live_ready': False}, manifests)

    def inspect(self):
        # Full inspection is expensive on complete catalogs. Reuse the exact
        # manifests verified in this call instead of scanning every DB twice.
        # There is no cross-call cache or reduced verification boundary.
        result, result_manifests = self._inspection()
        result['manifests'] = result_manifests
        return result

    def _writer(self):
        self._check()
        if not self.fixture:
            if self.binding.get('execution') in ('pilot-candidate', 'pilot-live'):
                # Reconstruct root-owned runtime and catalog authority before
                # each administrative transaction. Never widen fixture mounts.
                current = type(self).open_pilot_administrative(self.path.parents[1].name)
                if current.root != self.root or current.path != self.path or current.binding != self.binding:
                    raise ConfigError('pilot catalog writer belongs to another installation')
            elif self.binding.get('execution') == 'pilot-qualification':
                from .testing.pilot_qualification import open_catalogs
                current = open_catalogs(self.path.parents[1].name)
                if current.root != self.root or current.path != self.path or current.binding != self.binding:
                    raise ConfigError('qualification catalog writer belongs to another installation')
            else:
                _administrator(self.root, self.path.parents[1])
        return file_lease(self.path / '.writer.lock')

    def stage(self, repository, modes):
        """Authenticate and publish all requested immutable versions; refs unchanged."""
        modes = list(modes)
        if not modes or len(modes) > len(MODES) or len(modes) != len(set(modes)) or any(m not in MODES for m in modes):
            raise ConfigError('stage requires distinct supported modes')
        with self._writer():
            before = self.current()
            if {'catch','loved'} & set(modes):
                self._accept_exclusions_locked(repository,include_catch='catch' in modes,include_loved='loved' in modes)
            if len(list((self.path / 'versions').iterdir())) + len(modes) > MAX_VERSIONS:
                raise ConfigError('catalog version budget exceeded; reference-aware cleanup required')
            selected = dict(before['modes'])
            snapshot_identity = None
            # Each verified context owns its private bytes until sealed publication.
            for mode in modes:
                with verified_local_catalog(repository, self.path / 'cache', self.trust, mode,
                                            workspace=self.path / '.staging') as kit:
                    snapshot = sha256(self.path / 'cache/metadata/snapshot.json')
                    if snapshot_identity is not None and snapshot != snapshot_identity:
                        raise ConfigError('catalog repository changed during complete-set staging; retry')
                    snapshot_identity = snapshot
                    identity = kit.manifest['content_id']; destination = self.path / 'versions' / identity
                    required = sum(p.stat().st_size for p in kit.folder.iterdir())
                    free = os.statvfs(self.path).f_bavail * os.statvfs(self.path).f_frsize
                    if free < RESERVE + required: raise ConfigError('catalog staging would consume disk reserve')
                    if os.path.lexists(destination): self.version(mode, identity)
                    else:
                        with tempfile.TemporaryDirectory(prefix='publish-', dir=self.path / '.staging') as tmp:
                            folder = Path(tmp) / 'bundle'; folder.mkdir(mode=0o700)
                            for p in kit.folder.iterdir():
                                shutil.copyfile(p, folder / p.name); _sync(folder / p.name)
                                (folder / p.name).chmod(0o400 if self.fixture else 0o444)
                            if inspect_bundle(folder) != kit.manifest: raise ConfigError('verified catalog changed during publication')
                            receipt = {'format': 1, 'mode': mode, 'content_id': identity,
                                       'manifest_sha256': sha256(folder / 'manifest.json'), 'bootstrap_sha256': kit.bootstrap_sha256}
                            # Moving a directory between parents needs owner write
                            # permission for its '..' entry. Seal immediately after
                            # rename; no reference/receipt authorizes it until sealed.
                            _sync(folder); os.rename(folder, destination)
                            destination.chmod(0o500 if self.fixture else 0o555)
                            _sync(destination); _sync(destination.parent)
                            self._write(self.path / 'receipts' / (identity + '.json'), receipt)
                            self.version(mode, identity)
                    selected[mode] = identity
            if selected == before['modes']:
                # A refresh that authenticated identical content is not a new set.
                (self.path / 'candidate.json').unlink(missing_ok=True)
                return before
            proposal = {'format': 1, 'before': before,
                        'after': self._reference(selected, serial=before['serial'] + 1)}
            self._write(self.path / 'candidate.json', proposal)
            return proposal['after']

    def _journal(self, value):
        fields(value, ('format', 'before', 'after'), 'catalog transaction')
        if type(value['format']) is not int or value['format'] != 1: raise ConfigError('invalid catalog transaction')
        self._validate_reference(value['before']); self._validate_reference(value['after'])
        if value['after']['serial'] != value['before']['serial'] + 1:
            raise ConfigError('catalog transaction serial mismatch')
        return value

    def commit(self):
        """Commit desired refs only. Existing workers never follow this reference."""
        with self._writer():
            before = self.current(); candidate = self._journal(self._read('candidate.json'))
            if before != candidate['before']: raise ConfigError('catalog candidate is stale; stage again')
            self._complete(candidate['after'])
            self._write(self.path / 'pending.json', candidate)
            self._write(self.path / 'last-good.json', before)
            self._write(self.path / 'current.json', candidate['after'])
            (self.path / 'pending.json').unlink(); _sync(self.path)
            return candidate['after']

    def _complete(self, ref):
        self._validate_reference(ref)
        from .provisioning import _control
        config = read_json(self.root / 'config/host.json') if self.fixture else _control(self.root / 'config/host.json', self.binding['service_uid'], private=True)
        from .config import validate
        config = validate(config)
        if not {mode_for(r) for r in config['rooms']} <= set(ref['modes']):
            raise ConfigError('complete selected preset catalog set required before activation')
        for mode, identity in ref['modes'].items(): self.version(mode, identity)

    def recover(self):
        """Finish a committed atomic ref or abort an uncommitted one, never guess."""
        with self._writer():
            journal = self._journal(self._read('pending.json'))
            current = self.current(allow_pending=True)
            if current not in (journal['before'], journal['after']):
                raise ConfigError('catalog refs diverged from pending transaction')
            if current == journal['after']:
                self._complete(current)
                self._write(self.path / 'last-good.json', journal['before'])
            else:
                for mode, identity in current['modes'].items(): self.version(mode, identity)
            (self.path / 'pending.json').unlink(); _sync(self.path)
            return {'status': 'committed' if current == journal['after'] else 'aborted', 'reference': current}

    def exclusions(self, *, mode=None):
        from .catalog_exclusions import initial_state, validate_state
        self._check()
        name='exclusions-'+mode+'.json' if mode in ('catch','loved') else 'exclusions.json'
        state = self._read(name, optional=True)
        return validate_state(state if state is not None else initial_state(self.trust.identity), self.trust.identity)

    def _accept_exclusions_locked(self, repository, *, include_catch=False, include_loved=False):
        from .catalog_exclusions import apply_feed, validate_feed
        from .catalog_trust import verified_target
        from .layout import unique_fields
        from .catalog_export import CORE_MODES
        import hashlib
        def target(name):
            raw=verified_target(repository,self.path/'cache',self.trust,name)
            try:feed=json.loads(raw,object_pairs_hook=unique_fields)
            except (ValueError,UnicodeError) as exc:raise ConfigError('invalid signed exclusion JSON') from exc
            validate_feed(feed)
            return raw,feed
        raw,feed=target('exclusions.json')
        if any(not set(e['modes'])<=set(CORE_MODES) for e in feed['events']):
            raise ConfigError('legacy exclusion target contains an incompatible mode')
        result=apply_feed(self.exclusions(),feed,hashlib.sha256(raw).hexdigest())
        if include_catch:
            extended_raw,extended=target('exclusions-v2.json')
            if extended['revision']!=feed['revision']:
                raise ConfigError('catalog exclusion contracts changed during refresh; retry')
            from .catalog_publication import _legacy_feed
            if _legacy_feed(extended)!=feed:
                raise ConfigError('catalog exclusion contract projections disagree')
            events=[]
            for e in extended['events']:
                if 'catch' in e['modes']:
                    events.append({**e,'modes':['catch'],'memberships':
                                   {'catch':e['memberships']['catch']} if e['memberships'] else {}})
            scoped={**extended,'events':events}
            catch=apply_feed(self.exclusions(mode='catch'),scoped,hashlib.sha256(extended_raw).hexdigest())
            # Catch state never enters the legacy file. An older runtime can
            # reactivate its previous reference without deleting newer data.
            self._write(self.path/'exclusions-catch.json',catch)
        if include_loved:
            loved_raw,loved_feed=target('exclusions-loved.json')
            if loved_feed['revision']!=feed['revision'] or any(e['modes']!=['loved'] for e in loved_feed['events']):
                raise ConfigError('Loved exclusion target scope/revision mismatch')
            loved=apply_feed(self.exclusions(mode='loved'),loved_feed,hashlib.sha256(loved_raw).hexdigest())
            self._write(self.path/'exclusions-loved.json',loved)
        self._write(self.path/'exclusions.json',result)
        return result

    def accept_exclusions(self, repository):
        with self._writer():
            return self._accept_exclusions_locked(repository,include_catch='catch' in self.current()['modes'],include_loved='loved' in self.current()['modes'])

    def rollback(self, reference=None):
        """Explicit local verified rollback; remote metadata and exclusions unchanged."""
        with self._writer():
            before = self.current()
            target = self._validate_reference(reference if reference is not None else self._read('last-good.json'))
            self._complete(target)
            after = self._reference(target['modes'], serial=before['serial']+1)
            transaction = {'format': 1, 'before': before, 'after': after}
            self._write(self.path/'pending.json', transaction)
            self._write(self.path/'last-good.json', before)
            self._write(self.path/'current.json', after)
            (self.path/'pending.json').unlink(); _sync(self.path)
            return after

    @contextmanager
    def worker_pin(self, reference, *, modes=None):
        """Keep durable exact version refs for a reader process's lifetime."""
        self._validate_reference(reference)
        selected = list(reference['modes']) if modes is None else list(modes)
        if not selected or len(selected)!=len(set(selected)) or not set(selected)<=set(reference['modes']):
            raise ConfigError('reader pin requires supported modes in the bound complete set')
        directory = no_links(self.root/'state/catalog-pins')
        directory.mkdir(mode=0o700, exist_ok=True)
        st = directory.stat()
        if st.st_uid != self.binding['service_uid'] or st.st_mode & 0o077: raise ConfigError('unsafe catalog pin directory')
        path = directory/(uuid.uuid4().hex+'.json')
        # Fixture pins serialize with cleanup. Protected service pins are covered
        # by the owner lease: administrative removal is stopped-only.
        from contextlib import nullcontext
        writer = self._writer() if self.fixture else nullcontext()
        with writer:
            for mode in selected: self.version(mode, reference['modes'][mode])
            atomic_json(path, {'format': 1, 'installation_id': self.binding['installation_id'], 'reference': reference})
            lease = file_lease(path); lease.__enter__()
        try: yield reference
        finally:
            lease.__exit__(None, None, None); path.unlink(missing_ok=True)

    def cleanup(self, *, dry_run=True):
        """Delete only attributable unreferenced versions, under stopped ownership.

        Root-owned provisioning refuses mutation while a controller owns the host.
        Fixture reader pins are preserved too; dry-run never changes data.
        """
        if type(dry_run) is not bool: raise ConfigError('cleanup dry-run must be boolean')
        with self.stopped(), self._writer():
            current = self.current(allow_pending=True)
            preserved = set(current['modes'].values())
            refs = {'current': current}
            previous = self._read('last-good.json', optional=True)
            if previous is not None:
                previous = self._validate_reference(previous); refs['last_good'] = previous
                preserved.update(previous['modes'].values())
            for name in ('pending.json', 'candidate.json'):
                journal = self._read(name, optional=True)
                if journal is not None:
                    self._journal(journal)
                    for side in ('before','after'):
                        refs[name+'/'+side] = journal[side]; preserved.update(journal[side]['modes'].values())
            pins = self.root/'state/catalog-pins'
            if os.path.lexists(pins):
                no_links(pins)
                if pins.stat().st_uid != self.binding['service_uid'] or pins.stat().st_mode & 0o077:
                    raise ConfigError('unsafe catalog reader pins')
                children = list(pins.iterdir())
                if len(children)>128: raise ConfigError('catalog reader pin budget exceeded')
                for path in children:
                    if not re.fullmatch('[a-f0-9]{32}\\.json', path.name): raise ConfigError('unattributable reader pin')
                    with bounded_file(path,65536) as f:
                        from .layout import unique_fields
                        pin = json.load(f, object_pairs_hook=unique_fields)
                    fields(pin, ('format','installation_id','reference'), 'catalog reader pin')
                    if pin['format'] != 1 or pin['installation_id'] != self.binding['installation_id']:
                        raise ConfigError('foreign reader pin')
                    self._validate_reference(pin['reference'])
                    # Preserve stale pins conservatively, too. The owning process
                    # removes its own pin on clean exit; no PID reuse inference.
                    preserved.update(pin['reference']['modes'].values()); refs['pin/'+path.name] = pin['reference']
            removable, retained = [], []
            interrupted = self._read('cleanup-pending.json', optional=True)
            if interrupted is not None:
                fields(interrupted, ('format','content_id','files'), 'cleanup journal')
                if interrupted['format'] != 1 or not isinstance(interrupted['files'],dict) or not 1 <= len(interrupted['files']) <= 4:
                    raise ConfigError('invalid cleanup journal')
                _hash(interrupted['content_id'])
                if interrupted['content_id'] in preserved: raise ConfigError('interrupted cleanup now references a version; refusing deletion')
                for name, digest in interrupted['files'].items():
                    if name not in ('manifest.json','catalog.db','dt.db','farm.db','converts.db','bundle.tar.gz'): raise ConfigError('unsafe cleanup journal member')
                    _hash(digest)
            for path in sorted((self.path/'versions').iterdir()):
                if path.name in preserved:
                    retained.append({'content_id': path.name, 'reason': 'referenced'}); continue
                if not re.fullmatch('[a-f0-9]{64}', path.name):
                    retained.append({'content_id': path.name, 'reason': 'unattributable'}); continue
                self._check_path(path, directory=True)
                children = list(path.iterdir())
                if len(children)>4: raise ConfigError('unattributable managed version inventory')
                for child in children: self._check_path(child)
                if interrupted is not None and interrupted['content_id'] == path.name:
                    for child in children:
                        if child.name not in interrupted['files'] or sha256(child) != interrupted['files'][child.name]:
                            raise ConfigError('interrupted cleanup member changed')
                else:
                    manifest = inspect_bundle(path)
                    if manifest['content_id'] != path.name: raise ConfigError('managed cleanup content binding changed')
                receipt = self.path/'receipts'/(path.name+'.json')
                if receipt.exists(): self._check_path(receipt)
                removable.append({'content_id': path.name, 'bytes': sum(c.stat().st_size for c in children)})
            result = {'dry_run': dry_run, 'remove': removable, 'retained': retained,
                      'referenced_sets': refs, 'reclaimed_bytes': 0, 'scope': 'owned managed versions only; stopped controller'}
            if not dry_run:
                # Validate the whole candidate inventory before the first deletion.
                # Resume the recorded version before allowing another deletion
                # to replace its journal. Keep the original member hashes even
                # when an interruption has already emptied its directory.
                if interrupted is not None:
                    removable.sort(key=lambda item: item['content_id'] != interrupted['content_id'])
                for item in removable:
                    identity = item['content_id']; path = self.path/'versions'/identity
                    if interrupted is None or identity != interrupted['content_id']:
                        self._write(self.path/'cleanup-pending.json', {'format':1, 'content_id':identity,
                                    'files':{child.name:sha256(child) for child in path.iterdir()}})
                    path.chmod(0o700 if self.fixture else 0o755)
                    for child in path.iterdir(): child.unlink()
                    path.rmdir(); _sync(path.parent)
                    (self.path/'receipts'/(identity+'.json')).unlink(missing_ok=True)
                    _sync(self.path/'receipts'); result['reclaimed_bytes'] += item['bytes']
                    (self.path/'cleanup-pending.json').unlink(); _sync(self.path)
                if interrupted is not None and not (self.path/'versions'/interrupted['content_id']).exists():
                    (self.path/'receipts'/(interrupted['content_id']+'.json')).unlink(missing_ok=True)
                    (self.path/'cleanup-pending.json').unlink(missing_ok=True); _sync(self.path/'receipts'); _sync(self.path)
            return result

    @contextmanager
    def stopped(self):
        """Acquire the existing owner inode and validate known room ownership."""
        from .runtime_lifecycle import _stopped
        if os.path.lexists(self.root / 'state/rolling/ownership.json'):
            with _stopped(self.root, self.binding['service_uid']): yield
            return
        # Fresh hosts have no ownership history; unexplained provider state forbids adoption.
        if any((self.root / 'state').glob('*room*')) or any((self.root / 'state').glob('*recovery*')):
            raise ConfigError('fresh import requires settled room ownership')
        path = self.root / 'state/owner.lock'
        fd = os.open(path, os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            st = os.fstat(fd)
            if st.st_uid != self.binding['service_uid']:
                if not self.fixture and st.st_uid == 0 and st.st_size == 0 and st.st_nlink == 1:
                    os.fchown(fd, self.binding['service_uid'], self.binding['service_uid']); st = os.fstat(fd)
                else: raise ConfigError('foreign owner lock')
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_mode & 0o077:
                raise ConfigError('unsafe owner lock')
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise ConfigError('stopped import requires service to be stopped') from None
            yield
        finally: os.close(fd)

    def import_local(self, repository, modes):
        with self.stopped():
            before = self.current()
            staged = self.stage(repository, modes)
            # An authenticated unchanged set has no candidate transaction.
            # Do not attempt commit or rewrite recovery/activation history.
            if staged == before:
                return before
            return self.commit()


def _administrator(root, prefix):
    fixture_only()
    if (os.environ.get('POOLROTATER_PROVISIONING_SANDBOX') != '1' or os.geteuid() != 0
            or not Path(root).absolute().is_relative_to('/state')
            or not Path(prefix).absolute().is_relative_to('/runtime') or Path(prefix).absolute() == Path('/runtime')):
        raise ConfigError('catalog administrative writes require disposable provisioning mounts')


def resolved_catalogs(root, *, room_alias=None, reference=None):
    """Verify a complete reference, inspecting only an explicitly scoped room.

    Host preflight still inspects every bundle. A guarded worker retains its
    pinned reference and may inspect only databases declared for its own mode.
    """
    root = Path(root)
    if reference is not None and room_alias is None:
        raise ConfigError('pinned catalog resolution requires an explicit room')
    if not os.path.lexists(contained(root, POINTER)):
        if reference is not None: raise ConfigError('pinned catalog store is missing')
        return None
    store = CatalogStore.open(root); current = store.current()
    ref = store._validate_reference(reference) if reference is not None else current
    _, config = load(root)
    if not {mode_for(r) for r in config['rooms']} <= set(ref['modes']):
        raise ConfigError('managed catalog set is incomplete; stopped import required')
    modes = set(ref['modes'])
    if room_alias is not None:
        selected = [room for room in config['rooms'] if room['alias'] == room_alias]
        if len(selected) != 1: raise ConfigError('unknown configured room')
        modes = {mode_for(selected[0])}
    bundles = {mode: store.version(mode, ref['modes'][mode]) for mode in sorted(modes)}
    return {'set_id': ref['set_id'], 'reference': ref, 'bundles': bundles, 'store': store.path}


def settings_for(room, resolved):
    folder, manifest = resolved['bundles'][mode_for(room)]
    companions = manifest['capabilities']['companions']
    files = {f['name']: f['sha256'] for f in manifest['files']}
    return {**({'loved_db':str(folder/'catalog.db'),'loved_mode':True} if manifest['mode']=='loved' else {}),
            **({'catch_catalog': str(folder/'catalog.db')} if manifest['ruleset']==2 else {}),
            'farm_db': str(folder / 'catalog.db') if manifest['ruleset'] == 0 else '',
            'mania_db': str(folder / 'catalog.db') if manifest['ruleset'] == 3 else '',
            'standard_dt_db': str(folder / 'dt.db') if 'dt' in companions else '',
            'standard_catalog_binding': {'source_sha256': files['catalog.db'], 'dt_sha256': files['dt.db']} if 'dt' in companions else None,
            'mania_farm_db': str(folder / 'farm.db') if 'farm' in companions else '',
            'mania_farm_enabled': 'farm' in companions,
            'mania_convert_db': str(folder / 'converts.db') if 'converts' in companions else '',
            'mania_convert_validation_db': str(folder / 'converts.db') if 'converts' in companions else None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--prefix', type=Path, help='protected administrative fixture prefix')
    parser.add_argument('--uid', type=int, default=1000)
    parser.add_argument('action', choices=('initialize', 'status', 'inspect', 'stage', 'commit', 'import', 'recover', 'rollback', 'cleanup', 'exclusions', 'refresh'))
    parser.add_argument('--repository', type=Path)
    parser.add_argument('--mode', action='append', choices=MODES)
    parser.add_argument('--trust-root', type=Path, help='explicit out-of-band trusted fixture bootstrap root')
    parser.add_argument('--origin', help='explicit loopback fixture origin')
    parser.add_argument('--delivery-cache', type=Path)
    parser.add_argument('--ca-file', type=Path)
    parser.add_argument('--apply', action='store_true', help='apply managed cleanup; default is dry-run')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--exclusions-only', action='store_true')
    args = parser.parse_args(argv)
    try:
        fixture_only()
        if args.action == 'initialize':
            if args.trust_root is None: raise ConfigError('explicit trusted fixture root file required')
            with bounded_file(args.trust_root,65536) as f: trust=BootstrapTrust(f.read(),fixture=True)
            store=(CatalogStore.initialize_protected(args.root,args.prefix,trust,uid=args.uid)
                   if args.prefix is not None else CatalogStore.initialize_fixture(args.root,trust))
            print(json.dumps(store.status(),sort_keys=True));return 0
        store = (CatalogStore.open_administrative(args.root, args.prefix, uid=args.uid)
                                if args.prefix is not None else CatalogStore.open(args.root))
        if args.action == 'refresh':
            from .catalog_delivery import FixtureOrigin, RefreshClient
            if not args.origin or args.delivery_cache is None: raise ConfigError('explicit fixture origin and delivery cache required')
            client = RefreshClient(store, FixtureOrigin(args.origin, args.delivery_cache, ca_file=args.ca_file))
            result = (client.refresh_exclusions if args.exclusions_only else client.refresh)(force=args.force)
        elif args.action == 'exclusions':
            if args.repository is None: result = store.exclusions()
            else: result = store.accept_exclusions(args.repository)
        elif args.action == 'rollback': result = store.rollback()
        elif args.action == 'cleanup': result = store.cleanup(dry_run=not args.apply)
        elif args.action in ('stage', 'import'):
            if args.repository is None or not args.mode: raise ConfigError('repository and selected modes required')
            result = store.stage(args.repository, args.mode) if args.action == 'stage' else store.import_local(args.repository, args.mode)
        elif args.action == 'commit': result = store.commit()
        elif args.action == 'recover': result = store.recover()
        elif args.action == 'inspect': result = store.inspect()
        else: result = store.status()
        print(json.dumps(result, sort_keys=True))
    except (ConfigError, OSError) as exc:
        parser.exit(2, (str(exc) if isinstance(exc, ConfigError) else type(exc).__name__) + '\n')
    return 0


if __name__ == '__main__': raise SystemExit(main())
