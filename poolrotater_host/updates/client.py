"""Durable verified release selection and download, without lifecycle authority."""
import hashlib
import os
from pathlib import Path
import shutil
import time
from types import SimpleNamespace

from ..catalog_delivery import HttpsOrigin
from ..catalog_trust import _private_directory, bounded_file, dependencies, no_links
from ..layout import atomic_json, file_lease, read_json
from ..runtime_package import inspect as inspect_runtime
from .contracts import (canonical, decode, digest, fingerprint, identifier, integer,
                        require, root as validate_root, timestamp, MAX_METADATA, release)
from .trust import verify


def read(path, *, optional=False):
    if optional and not os.path.lexists(path): return None
    with bounded_file(path, MAX_METADATA) as stream:
        info = os.fstat(stream.fileno())
        require(info.st_uid == os.geteuid() and not info.st_mode & 0o077, 'update_state_custody_refused')
        return decode(stream.read())


class Client:
    def __init__(self, directory, bootstrap, *, bootstrap_sha256, origin, binding, clock=time.time):
        require(type(bootstrap) is bytes and hashlib.sha256(bootstrap).hexdigest() == digest(bootstrap_sha256),
                'update_bootstrap_digest_mismatch')
        self.bootstrap = validate_root(decode(bootstrap)); self.origin = origin
        require(type(binding) is dict and set(binding) == {'installation_id','generation','host_id'},
                'update_installation_binding_required')
        for value in binding.values(): identifier(value)
        self.binding = binding; self.clock = clock
        self.root = _private_directory(directory, create=True)
        require(no_links(origin.cache).is_relative_to(self.root), 'update_transport_cache_must_be_separate_namespace')
        for folder in ('downloads', 'history'): _private_directory(self.root/folder, create=True)
        self.path = self.root/'client.json'
        expected = {'format': 1, 'binding': binding, 'bootstrap_sha256': fingerprint(self.bootstrap),
                    'origin': origin.url}
        with file_lease(self.root/'client.lock'):
            previous = read(self.path, optional=True)
            if previous is None:
                atomic_json(self.path, {**expected, 'trust': None, 'index': None, 'channel_heads': {},
                    'phase': 'idle', 'available': None, 'download': None, 'clock_high_water': 0,
                    'installed_release_sequence': 0})
            else:
                require(all(previous.get(k) == v for k,v in expected.items()), 'update_client_binding_changed')

    def state(self):
        value = read(self.path)
        require(value.get('binding') == self.binding and value.get('bootstrap_sha256') == fingerprint(self.bootstrap)
                and value.get('origin') == self.origin.url, 'update_client_binding_changed')
        return value

    def _clock(self, state):
        now = timestamp(self.clock())
        require(now >= state['clock_high_water'], 'update_clock_rollback')
        state['clock_high_water'] = now
        # Observe the clock even if a network exchange or signature fails later.
        atomic_json(self.path, state)
        return now

    def _fetcher(self):
        _, _, interface, errors = dependencies()
        return self.origin.fetcher(interface, errors)

    def check(self, channel, installed, *, baselines=()):
        from .contracts import CHANNELS
        require(channel in CHANNELS, 'update_channel_refused'); digest(installed)
        require(type(baselines) in (tuple,list) and len(baselines) <= 8, 'update_baseline_refused')
        for baseline in baselines: digest(baseline)
        with file_lease(self.root/'client.lock'):
            state = self.state(); now = self._clock(state)
            raw = b''.join(self._fetcher()._fetch(self.origin.url+'metadata/releases.json'))
            payload, trust = verify(raw, self.bootstrap, now=now, previous=state['trust'])
            # A newer signed index must not rewrite a known artifact descriptor.
            previous = {r['artifact']['sha256']: r for r in (state['index'] or {}).get('releases', [])}
            for item in payload['releases']:
                sha = item['artifact']['sha256']
                require(sha not in previous or previous[sha] == item, 'update_immutable_descriptor_changed')
            releases = {r['artifact']['sha256']: r for r in payload['releases']}
            heads = dict(state['channel_heads'])
            for name, sha in payload['channels'].items():
                require(releases[sha]['sequence'] >= heads.get(name, 0), 'update_channel_downgrade')
                heads[name] = releases[sha]['sequence']
            # Persist rollback high waters even if the current installation has
            # no supported path to this channel's head.
            state.update(index=payload, trust=trust, channel_heads=heads, available=None, phase='checked')
            atomic_json(self.root/'verified-envelope.json', decode(raw))
            atomic_json(self.path, state)
            target = payload['channels'].get(channel)
            if target is None or target == installed: return self.summary()
            current_sequence=max(state['installed_release_sequence'],releases.get(installed,{}).get('sequence',0))
            if releases[target]['sequence'] <= current_sequence:
                # Switching from pilot to stable may leave this host ahead of
                # stable. Wait for stable to catch up; never reinstall old code.
                return self.summary()
            # Exact-baseline graph allows an older host to install intermediate
            # controller releases; never skip a required bootstrap/compatibility hop.
            def path_to(sha, visiting):
                if sha in (installed, *baselines): return []
                if sha in visiting or sha not in releases: return None
                item = releases[sha]
                # A signed direct predecessor is sufficient; descriptor hash
                # ordering must not force an unnecessary intermediate update.
                if any(before in (installed, *baselines) for before in item['from_sha256']):
                    return [item]
                for before in item['from_sha256']:
                    chain = path_to(before, visiting | {sha})
                    if chain is not None and (not chain or chain[-1]['sequence'] < item['sequence']):
                        return chain+[item]
                return None
            chain = path_to(target, set())
            require(chain, 'update_no_compatible_baseline_path')
            require(chain[0]['sequence']>current_sequence,'update_intermediate_release_downgrade')
            state.update(available=chain[0], phase='available')
            atomic_json(self.path, state)
            return self.summary()

    def download(self,item=None):
        with file_lease(self.root/'client.lock'):
            state = self.state(); now = self._clock(state)
            require(state['index'] is not None and now < state['index']['expires_at']
                    and now < self.bootstrap['expires_at'], 'update_metadata_expired_or_future')
            item=self._selected(state,item)
            sha = item['artifact']['sha256']; maximum = item['artifact']['bytes']
            require(shutil.disk_usage(self.root).free >= 5*1024**3 + maximum*3,
                    'update_disk_reserve_required')
            fetcher = self._fetcher()
            info = SimpleNamespace(path='runtime.zip', length=maximum, hashes={'sha256': sha})
            fetcher.register(info)
            destination = self.root/'downloads'/(sha+'.zip')
            state.update(phase='downloading', download={'sha256': sha, 'bytes': 0, 'total': maximum})
            atomic_json(self.path, state)
            temporary = destination.with_suffix('.writing')
            no_links(temporary); no_links(destination)
            # Transport's own .part files retain bounded resumable bytes; the
            # output here is disposable and never executable before verification.
            try:
                count = 0; checksum = hashlib.sha256(); checkpoint = 0
                with temporary.open('wb') as stream:
                    temporary.chmod(0o600)
                    for chunk in fetcher._fetch(self.origin.url+'targets/'+sha+'.runtime.zip'):
                        count += len(chunk); require(count <= maximum, 'update_artifact_size_refused')
                        checksum.update(chunk); stream.write(chunk)
                        if count-checkpoint >= 1024**2:
                            checkpoint = count; state['download']['bytes'] = count
                            atomic_json(self.path, state)
                    stream.flush(); os.fsync(stream.fileno())
                require(count == maximum and checksum.hexdigest() == sha, 'update_artifact_digest_mismatch')
                spec = inspect_runtime(temporary, sha, execution=item['execution'])
                require(fingerprint(spec) == item['manifest_sha256'] and spec['version'] == item['version'],
                        'update_artifact_manifest_mismatch')
                os.replace(temporary, destination)
                state.update(phase='downloaded', download={'sha256': sha,'bytes':count,'total':maximum})
                atomic_json(self.path, state)
                return self.summary()
            finally:
                temporary.unlink(missing_ok=True)

    def artifact(self,item=None):
        state = self.state(); now = self._clock(state)
        item=self._selected(state,item)
        require(now < state['index']['expires_at'] and now < self.bootstrap['expires_at'],
                'update_metadata_expired_or_future')
        path = self.root/'downloads'/(item['artifact']['sha256']+'.zip')
        spec = inspect_runtime(path, item['artifact']['sha256'], execution=item['execution'])
        require(fingerprint(spec) == item['manifest_sha256'], 'update_artifact_manifest_mismatch')
        return path, item

    def _selected(self,state,item):
        item=state['available']if item is None else item
        require(item is not None,'update_no_available_release');release(item)
        require(state['index']is not None and any(r==item for r in state['index']['releases']),
                'update_release_no_longer_approved')
        return item

    def summary(self):
        state = self.state(); item = state['available']
        return {'ok': True, 'phase': state['phase'],
                'metadata_sequence': (state['trust'] or {}).get('sequence', 0),
                'available_version': item['version'] if item else None,
                'available_kind': item['kind'] if item else None,
                'download': state['download'], 'live_rooms_affected': False}

    def installed(self,item):
        with file_lease(self.root/'client.lock'):
            state=self.state()
            require(item['sequence']>=state['installed_release_sequence'],'update_installed_release_downgrade')
            state.update(installed_release_sequence=item['sequence'],available=None,phase='checked')
            atomic_json(self.path,state)
