"""Stopped acquisition of missing full signed modes, preserving existing IDs.

The service UID never collects maps or performs analysis. Root authenticates
the same published bundles through existing TLS/TUF delivery, publishes sealed
versions, then switches the desired reference only while workers are stopped.
"""
import hashlib
import http.client
import os
from pathlib import Path
import ssl
import stat
import time

from ..config import ConfigError
from ..catalog_export import MODES


STANDARD_REFRESH = '.staging/managed-standard-refresh.json'
_STAGE_ATTEMPTS = 3
_STAGE_RETRY_BACKOFF = (0.2, 0.4)


def _require(value):
    if not value:raise ConfigError('managed_catalog_preparation_requires_review')


def _transient_stage_failure(exc):
    """Accept only the known resumable delivery interruption envelope."""
    if type(exc) is not ConfigError or exc.args != ('catalog trust/validation failed: DownloadError',):
        return False
    from ..catalog_trust import dependencies
    try:
        _, _, _, errors = dependencies()
        download_error = errors.DownloadError
    except Exception:
        return False
    cause = exc.__cause__
    if type(cause) is not download_error:
        return False
    # This is the one deliberately raised response-truncation marker in the
    # authenticated fixture transport. No other DownloadError text is trusted.
    if cause.__cause__ is None and cause.__context__ is None:
        return cause.args == ('truncated fixture response',)
    transient = (TimeoutError, ConnectionResetError, ConnectionAbortedError,
                 BrokenPipeError, http.client.IncompleteRead,
                 http.client.RemoteDisconnected)
    seen = set()
    retryable = False
    chain = cause.__cause__
    while chain is not None and id(chain) not in seen and len(seen) < 16:
        seen.add(id(chain))
        if isinstance(chain, ssl.SSLCertVerificationError):
            return False
        if type(chain) in transient:
            retryable = True
        chain = chain.__cause__ or chain.__context__
    return retryable and chain is None


def _stage_binding(store):
    """Snapshot the refs and ownership inputs that a retry must preserve."""
    return (str(store.root), str(store.path), bool(store.fixture), dict(store.binding),
            store.trust.identity, store.trust.fixture,
            store.current(allow_pending=True),
            store._read('candidate.json', optional=True),
            store._read('pending.json', optional=True))


def _metadata_snapshot(store):
    """Custody-check and hash TUF metadata before resuming a partial target."""
    from ..catalog_trust import _cache_check
    cache = store.path / 'cache'
    _cache_check(cache)
    metadata = cache / 'metadata'
    result = []
    if not metadata.exists():
        return ()
    for path in sorted(metadata.rglob('*')):
        st = path.lstat()
        rel = path.relative_to(cache).as_posix()
        common = (rel, st.st_uid, stat.S_IMODE(st.st_mode), st.st_nlink,
                  st.st_size, st.st_mtime_ns)
        if stat.S_ISLNK(st.st_mode):
            result.append(common + ('link', os.readlink(path)))
        elif stat.S_ISDIR(st.st_mode):
            result.append(common + ('dir',))
        elif stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
            digest = hashlib.sha256()
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                opened = os.fstat(fd)
                if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                        or (opened.st_dev, opened.st_ino, opened.st_uid,
                            opened.st_size, opened.st_mtime_ns) !=
                           (st.st_dev, st.st_ino, st.st_uid, st.st_size, st.st_mtime_ns)):
                    raise ConfigError('catalog metadata custody changed')
                with os.fdopen(fd, 'rb', closefd=False) as stream:
                    for block in iter(lambda: stream.read(65536), b''):
                        digest.update(block)
                after = os.fstat(fd)
                if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
                    raise ConfigError('catalog metadata changed during retry check')
            finally:
                os.close(fd)
            result.append(common + ('file', digest.hexdigest()))
        else:
            raise ConfigError('catalog metadata custody changed')
    return tuple(result)


def _stage_with_transient_retry(store, repository, modes, *, sleep=time.sleep):
    """Resume an authenticated partial delivery without changing its authority."""
    initial = _stage_binding(store)
    metadata = None
    for attempt in range(_STAGE_ATTEMPTS):
        if attempt:
            if _stage_binding(store) != initial:
                raise ConfigError('managed_catalog_retry_state_changed')
            if _metadata_snapshot(store) != metadata:
                raise ConfigError('managed_catalog_retry_metadata_changed')
        try:
            return store.stage(repository, modes)
        except ConfigError as exc:
            if not _transient_stage_failure(exc) or attempt + 1 == _STAGE_ATTEMPTS:
                raise
            if _stage_binding(store) != initial:
                raise ConfigError('managed_catalog_retry_state_changed') from None
            # A metadata refresh may be part of the first interrupted attempt.
            # Pin its authenticated result, then require it to stay byte-for-byte
            # stable across every subsequent retry boundary.
            current_metadata = _metadata_snapshot(store)
            if metadata is not None and current_metadata != metadata:
                raise ConfigError('managed_catalog_retry_metadata_changed') from None
            metadata = current_metadata
            sleep(_STAGE_RETRY_BACKOFF[attempt])
            if _stage_binding(store) != initial:
                raise ConfigError('managed_catalog_retry_state_changed') from None
            if _metadata_snapshot(store) != metadata:
                raise ConfigError('managed_catalog_retry_metadata_changed') from None


def prepare_store(store,repository,*,owner_lease_held=False):
    """Internal seam; the installed caller proves its stopped owner lease."""
    _require(owner_lease_held is True)
    if store.fixture:
        from ..runtime_package import fixture_only
        fixture_only()
    else:_require(os.geteuid()==0)
    record=store.path/'.staging/managed-prewarm.json'
    def read():
        # Catalog journals are protected root-owned control records (0644),
        # unlike private host settings. CatalogStore._read applies the correct
        # ownership, mode, no-link and bounded-JSON checks for this namespace.
        try:return store._read('.staging/managed-prewarm.json',optional=True)
        except ConfigError:raise ConfigError('managed_catalog_prewarm_requires_review') from None
    value=read();binding={k:store.binding[k] for k in ('installation_id','generation','host_root','service_uid')}
    if value is not None:
        _require(type(value) is dict and set(value)=={'format','binding','bootstrap_sha256','before','after','phase'}
                 and type(value['format']) is int and value['format']==1 and value['binding']==binding
                 and value['bootstrap_sha256']==store.trust.identity and value['phase'] in ('staging','ready','complete'))
        store._validate_reference(value['before'])
        if value['after'] is not None:store._validate_reference(value['after'])
    pending=store._read('pending.json',optional=True)
    if pending is not None:
        _require(value is not None and value['after'] is not None
                 and pending=={'format':1,'before':value['before'],'after':value['after']})
        store.recover()
    before=store.current()
    if set(before['modes'])==set(MODES):
        if value is not None and value['phase']!='complete':
            _require(before==value['after'])
            value['phase']='complete';store._write(record,value)
        return {'ok':True,'reference':before,'all_modes_prepared':True,'existing_modes_replaced':False}
    if value is None or value['phase']=='complete':
        old_candidate=store._read('candidate.json',optional=True)
        if old_candidate is not None:
            store._journal(old_candidate)
            _require(old_candidate['after']==before)
        value={'format':1,'binding':binding,'bootstrap_sha256':store.trust.identity,
               'before':before,'after':None,'phase':'staging'}
        store._write(record,value)
    _require(before==value['before'])
    candidate=store._read('candidate.json',optional=True)
    if candidate is not None and candidate.get('after')==before:
        # CatalogStore retains the committed candidate as historical evidence.
        # Only that exact terminal record may be replaced by a fresh stage.
        candidate=None
    if value['phase']=='staging':
        if candidate is None:
            _stage_with_transient_retry(store,repository,sorted(set(MODES)-set(before['modes'])))
            candidate=store._read('candidate.json')
        store._journal(candidate)
        after=candidate['after']
        _require(candidate['before']==before and set(after['modes'])==set(MODES)
                 and all(after['modes'][m]==identity for m,identity in before['modes'].items()))
        store._complete(after)
        value.update(phase='ready',after=after);store._write(record,value)
    _require(candidate=={'format':1,'before':value['before'],'after':value['after']})
    after=store.commit()
    _require(after==value['after'])
    value['phase']='complete';store._write(record,value)
    return {'ok':True,'reference':after,'all_modes_prepared':True,'existing_modes_replaced':False}


def _same_except_standard(before, after):
    return (set(before['modes']) == set(after['modes'])
            and all(before['modes'][mode] == after['modes'][mode]
                    for mode in before['modes'] if mode != 'standard')
            and before['modes'].get('standard') != after['modes'].get('standard')
            and after['serial'] == before['serial'] + 1)


def _refresh_standard_store_locked(store, repository, *, owner_lease_held=False):
    """Refresh only the authenticated Standard target while workers are stopped.

    The journal is separate from managed-prewarm because that receipt only
    describes filling absent modes. It binds resumable stage/commit recovery to
    this exact installation and preserves all non-Standard mode identities.
    """
    _require(owner_lease_held is True)
    if store.fixture:
        from ..runtime_package import fixture_only
        fixture_only()
    else:
        _require(os.geteuid() == 0)
    record_path = store.path / STANDARD_REFRESH
    binding = {k: store.binding[k] for k in
               ('installation_id', 'generation', 'host_root', 'service_uid')}

    def write(value):
        store._write(record_path, value)

    def read():
        value = store._read(STANDARD_REFRESH, optional=True)
        if value is None:
            return None
        _require(type(value) is dict and set(value) ==
                 {'format', 'binding', 'bootstrap_sha256', 'before', 'after', 'phase'}
                 and type(value['format']) is int and value['format'] == 1
                 and value['binding'] == binding
                 and value['bootstrap_sha256'] == store.trust.identity
                 and value['phase'] in ('staging', 'ready', 'complete'))
        store._validate_reference(value['before'])
        if value['after'] is not None:
            store._validate_reference(value['after'])
        return value

    def candidate():
        value = store._read('candidate.json', optional=True)
        return None if value is None else store._journal(value)

    def validate_candidate(proposal, before):
        _require(proposal is not None and proposal['before'] == before
                 and _same_except_standard(before, proposal['after']))
        # Authenticate the complete activated set from the store's immutable
        # receipts. _complete also checks that the current host config's modes
        # are covered without changing that config.
        store._complete(proposal['after'])
        return proposal['after']

    def terminal_candidate(proposal, current):
        if proposal is None:
            return False
        store._complete(proposal['after'])
        _require(proposal['after'] == current
                 and proposal['before']['serial'] + 1 == current['serial'])
        return True

    value = read()
    current = store.current(allow_pending=True)
    pending = store._read('pending.json', optional=True)
    proposal = candidate()

    if value is not None and value['phase'] != 'complete':
        _require(current in (value['before'], value['after']))
        if value['phase'] == 'ready':
            _require(value['after'] is not None
                     and _same_except_standard(value['before'], value['after']))
            expected = {'format': 1, 'before': value['before'], 'after': value['after']}
            if pending is not None:
                _require(pending == expected)
                store.recover()
                current = store.current(allow_pending=True)
                pending = None
            if current == value['after']:
                store._complete(current)
                value['phase'] = 'complete';write(value)
                return {'ok': True, 'reference': current,
                        'standard_refreshed': current != value['before']}
            _require(current == value['before'] and pending is None and proposal == expected)
            committed = store.commit()
            _require(committed == value['after'])
            value['phase'] = 'complete';write(value)
            return {'ok': True, 'reference': committed, 'standard_refreshed': True}

        # A crash during CatalogStore.commit can leave its pending transaction
        # before the journal advances to ready only if our stage call had not
        # returned; we never start commit before publishing ready.
        _require(value['phase'] == 'staging' and value['after'] is None
                 and pending is None and current == value['before'])
        if proposal is not None and proposal['after'] == current:
            # The previous completed activation may have left its terminal
            # proposal behind when this new journal was written.
            terminal_candidate(proposal, current)
            (store.path / 'candidate.json').unlink()
            proposal = None
        if proposal is None:
            staged = _stage_with_transient_retry(store, repository, ['standard'])
            if staged == current:
                # CatalogStore removes candidate.json on identical content.
                value.update(after=current, phase='complete');write(value)
                return {'ok': True, 'reference': current,
                        'standard_refreshed': False}
            proposal = candidate()
        after = validate_candidate(proposal, value['before'])
        value.update(after=after, phase='ready');write(value)
        committed = store.commit()
        _require(committed == after)
        value['phase'] = 'complete';write(value)
        return {'ok': True, 'reference': committed, 'standard_refreshed': True}

    # A completed journal is historical evidence; a subsequent valid catalog
    # activation may have moved the reference since then. Start a fresh bound
    # refresh from the current complete set.
    _require(pending is None)
    _require('standard' in current['modes'] and set(current['modes']) == set(MODES))

    if proposal is not None:
        terminal_candidate(proposal, current)
        (store.path / 'candidate.json').unlink()
    value = {'format': 1, 'binding': binding,
             'bootstrap_sha256': store.trust.identity,
             'before': current, 'after': None, 'phase': 'staging'}
    write(value)
    staged = _stage_with_transient_retry(store, repository, ['standard'])
    if staged == current:
        value.update(after=current, phase='complete');write(value)
        return {'ok': True, 'reference': current, 'standard_refreshed': False}
    proposal = candidate()
    after = validate_candidate(proposal, current)
    value.update(after=after, phase='ready');write(value)
    committed = store.commit()
    _require(committed == after)
    value['phase'] = 'complete';write(value)
    return {'ok': True, 'reference': committed, 'standard_refreshed': True}


def refresh_standard_store(store, repository, *, owner_lease_held=False):
    """Serialize the refresh receipt with the stopped-owner preparation path."""
    from ..layout import file_lease
    _require(owner_lease_held is True)
    with file_lease(store.path / '.staging/managed-standard-refresh.lock'):
        return _refresh_standard_store_locked(store, repository,
                                              owner_lease_held=owner_lease_held)


def prepare_store_and_refresh_standard(store, repository, *, owner_lease_held=False):
    """Recover this helper's in-flight commit before legacy prewarm inspects it."""
    _require(owner_lease_held is True)
    receipt=store._read(STANDARD_REFRESH,optional=True)
    recovered_refresh=False
    if receipt is not None:
        _require(type(receipt) is dict and receipt.get('phase') in
                 ('staging','ready','complete'))
        if receipt['phase']!='complete':
            recovered=refresh_standard_store(store,repository,
                                             owner_lease_held=owner_lease_held)
            recovered_refresh=recovered['standard_refreshed']
    prepared=prepare_store(store,repository,owner_lease_held=owner_lease_held)
    refreshed=refresh_standard_store(store,repository,
                                     owner_lease_held=owner_lease_held)
    return {**prepared,'reference':refreshed['reference'],
            'standard_refreshed':recovered_refresh or refreshed['standard_refreshed'],
            'existing_modes_replaced':recovered_refresh or refreshed['standard_refreshed']}


def prepare(target,current):
    """Fixed protected source; never accepts paths/URLs/credentials from the hub."""
    from ..pilot_installation import _admin,_stopped
    from ..catalog_store import CatalogStore
    from ..catalog_delivery import HttpsOrigin
    from ..provisioning import protected
    from ..catalog_publication import _write as write_code
    from .catalog_source import ORIGIN,CA_PEM,CA_SHA256,BOOTSTRAP_SHA256
    _admin()
    _require(Path(current['host_root'])==target['root'] and Path(current['prefix'])==target['prefix'])
    with _stopped(target['root'],current['service_uid']):
        store=CatalogStore.open_pilot_administrative(target['root'].name)
        _require(store.trust.identity==BOOTSTRAP_SHA256)
        path=protected(target['prefix'])/'managed-catalog-ca.pem'
        if os.path.lexists(path):
            protected(path)
            _require(path.is_file() and path.stat().st_nlink==1 and path.stat().st_size<=65536
                     and hashlib.sha256(path.read_bytes()).hexdigest()==CA_SHA256)
        else:write_code(path,CA_PEM.encode('ascii'),mode=0o444,replace=False)
        repository=HttpsOrigin(ORIGIN,target['prefix']/'managed-catalog-delivery',
                               ca_file=path,ca_sha256=CA_SHA256,timeout=30,deadline=60)
        return prepare_store_and_refresh_standard(store,repository,
                                                  owner_lease_held=True)
