"""Local administrator publication. The HTTP service has no signing key access."""
import hashlib
import os
from pathlib import Path
import time
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from ..catalog_publication import private, _public_root, _write, _copy_target
from ..catalog_trust import bounded_file, no_links
from ..layout import atomic_json, file_lease, read_json
from ..runtime_package import inspect as inspect_runtime
from .contracts import (CHANNELS, canonical, decode, digest, fingerprint, index,
                        integer, require, root, timestamp, version, MAX_METADATA)


def classify(before, after):
    """Publication preflight only; installed native validation remains mandatory."""
    require(before['execution'] == after['execution'], 'update_execution_changed')
    require(version(after['version']) >= version(before['version']), 'update_version_downgrade')
    for key in ('target', 'interpreter_sha256', 'wheels', 'environment_id'):
        require(before[key] == after[key], 'update_environment_requires_manual_migration')
    from rolling.release import SHARED, STORES
    for name in STORES:
        require(before['files'].get('payload/'+name) == after['files'].get('payload/'+name),
                'update_storage_requires_manual_migration')
    changes = {name for name in before['files'].keys() | after['files'].keys()
               if before['files'].get(name) != after['files'].get(name)}
    require(changes, 'update_unchanged_artifact')
    worker = all(name.startswith('payload/bot/') and name.removeprefix('payload/') not in SHARED
                 for name in changes)
    if worker:
        require(before['version'] != after['version'], 'update_worker_requires_new_version')
    return 'worker' if worker else 'controller'


def initialize(custody, served, *, now=None):
    """Create a NEW independent authority; never read catalog or OAuth keys."""
    now = int(time.time() if now is None else timestamp(now))
    custody, served = no_links(custody), no_links(served)
    require(custody != served and not custody.is_relative_to(served)
            and not served.is_relative_to(custody), 'update_key_custody_overlaps_serving')
    private(custody.parent, directory=True)
    require(not os.path.lexists(custody) and not os.path.lexists(served), 'update_fresh_authority_required')
    custody.mkdir(mode=0o700)
    # Retain incomplete custody after an interruption rather than silently minting
    # a different trust root at a previously supplied location.
    (custody/'keys').mkdir(mode=0o700)
    public = {}
    for number in range(2):
        key = Ed25519PrivateKey.generate()
        raw = key.public_key().public_bytes_raw()
        keyid = hashlib.sha256(raw).hexdigest()
        public[keyid] = raw.hex()
        _write(custody/'keys'/f'{keyid}.pem', key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()), mode=0o600)
    bootstrap = root({'format': 1, 'purpose': 'poolrotater-release-root',
                      'repository_id': str(uuid.uuid4()), 'expires_at': now+365*86400,
                      'threshold': 2, 'keys': public})
    _write(custody/'bootstrap.json', canonical(bootstrap), mode=0o600)
    served.mkdir(mode=0o755);served.chmod(0o755)
    for folder in ('metadata', 'targets'):
        (served/folder).mkdir(mode=0o755);(served/folder).chmod(0o755)
    _write(served/'metadata/release-root.json', canonical(bootstrap))
    atomic_json(custody/'state.json', {'format': 1, 'purpose': 'poolrotater-release-signers',
        'served': str(served), 'bootstrap_sha256': fingerprint(bootstrap),
        'sequence': 0, 'release_sequence': 0, 'channels': {}, 'releases': [],
        'clock_high_water': now, 'pending': None})
    return {'ok': True, 'bootstrap_sha256': fingerprint(bootstrap),
            'repository_id': bootstrap['repository_id'], 'published': False}


class Publisher:
    def __init__(self, custody, *, clock=time.time):
        self.custody = private(custody, directory=True)
        self.clock = clock

    def _load(self):
        private(self.custody/'state.json', limit=MAX_METADATA)
        self.state = read_json(self.custody/'state.json', maximum=MAX_METADATA)
        require(self.state.get('purpose') == 'poolrotater-release-signers', 'update_authority_refused')
        private(self.custody/'bootstrap.json')
        self.bootstrap = root(read_json(self.custody/'bootstrap.json'))
        require(fingerprint(self.bootstrap) == self.state['bootstrap_sha256'], 'update_signer_root_changed')
        self.served = _public_root(self.state['served'])
        now = int(timestamp(self.clock()))
        require(self.state['clock_high_water'] <= now < self.bootstrap['expires_at'],
                'update_signer_clock_or_expiry_refused')
        self.state['clock_high_water'] = now
        atomic_json(self.custody/'state.json', self.state)
        return now

    def _commit(self, now, *, channels, releases, release_sequence):
        payload = index({'format': 1, 'purpose': 'poolrotater-release-index',
            'repository_id': self.bootstrap['repository_id'], 'sequence': self.state['sequence']+1,
            'issued_at': now, 'expires_at': min(now+7*86400, self.bootstrap['expires_at']),
            'channels': channels, 'releases': releases})
        signatures = []
        for keyid in self.bootstrap['keys']:
            path = private(self.custody/'keys'/f'{keyid}.pem')
            with bounded_file(path, 16384) as stream:
                key = serialization.load_pem_private_key(stream.read(), password=None)
            require(isinstance(key, Ed25519PrivateKey) and
                    key.public_key().public_bytes_raw().hex() == self.bootstrap['keys'][keyid],
                    'update_signer_identity_changed')
            signatures.append({'keyid': keyid, 'signature': key.sign(canonical(payload)).hex()})
        envelope = {'signed': payload, 'signatures': signatures}
        require(len(canonical(envelope)) <= MAX_METADATA, 'update_metadata_size_refused')
        self.state['pending'] = {'envelope': envelope, 'release_sequence': release_sequence}
        atomic_json(self.custody/'state.json', self.state)
        return self._finish()

    def _finish(self):
        pending = self.state['pending']; require(pending is not None, 'update_publication_not_pending')
        from .trust import verify
        payload, _ = verify(canonical(pending['envelope']), self.bootstrap, now=self.clock())
        require(payload['sequence'] == self.state['sequence']+1, 'update_pending_sequence_refused')
        for item in payload['releases']:
            path = self.served/'targets'/(item['artifact']['sha256']+'.runtime.zip')
            with bounded_file(path, item['artifact']['bytes']) as stream:
                require(os.fstat(stream.fileno()).st_size == item['artifact']['bytes']
                        and hashlib.file_digest(stream, 'sha256').hexdigest() == item['artifact']['sha256'],
                        'update_published_target_changed')
        raw = canonical(pending['envelope'])
        _write(self.served/'metadata'/f"{payload['sequence']}.releases.json", raw)
        _write(self.served/'metadata/releases.json', raw, replace=True)
        self.state.update(sequence=payload['sequence'], release_sequence=pending['release_sequence'],
                          releases=payload['releases'], channels=payload['channels'], pending=None)
        atomic_json(self.custody/'state.json', self.state)
        return {'ok': True, 'sequence': payload['sequence'], 'channels': payload['channels'],
                'expires_at': payload['expires_at'], 'private_keys_served': False}

    def status(self):
        """Read-only local authority report, never private key bytes/paths."""
        private(self.custody/'state.json',limit=MAX_METADATA)
        value=read_json(self.custody/'state.json',maximum=MAX_METADATA)
        require(value.get('purpose')=='poolrotater-release-signers','update_authority_refused')
        return {'ok':True,'metadata_sequence':value['sequence'],'release_sequence':value['release_sequence'],
                'channels':value['channels'],'publication_pending':value['pending']is not None,
                'approved_releases':len(value['releases']),'signing_keys_exposed':False}

    def publish(self, bundle, sha256, *, before_bundle, before_sha256, channel='pilot', execution='pilot-live', additional_baselines=()):
        require(channel in CHANNELS, 'update_channel_refused'); digest(sha256); digest(before_sha256)
        with file_lease(self.custody/'publish.lock'):
            now = self._load(); require(self.state['pending'] is None, 'update_publication_requires_resume')
            before = inspect_runtime(before_bundle, before_sha256, execution=execution)
            after = inspect_runtime(bundle, sha256, execution=execution)
            kind = classify(before, after)
            # One immutable artifact may serve hosts on different reviewed
            # predecessors. Admit every exact archive BEFORE signing/copying,
            # in one metadata transaction; no later descriptor rewrite needed.
            require(type(additional_baselines) in (tuple, list) and len(additional_baselines) <= 15,
                    'update_baseline_refused')
            baselines = [before_sha256]
            for baseline in additional_baselines:
                require(type(baseline) in (tuple, list) and len(baseline) == 2, 'update_baseline_refused')
                path, pin = baseline
                digest(pin)
                require(pin not in baselines, 'update_baseline_refused')
                previous = inspect_runtime(path, pin, execution=execution)
                require(classify(previous, after) == kind, 'update_baseline_kind_changed')
                baselines.append(pin)
            require(all(r['artifact']['sha256'] != sha256 for r in self.state['releases']),
                    'update_artifact_already_published_use_promotion')
            item = {'sequence': self.state['release_sequence']+1, 'version': after['version'],
                'kind': kind, 'artifact': {'sha256': sha256, 'bytes': Path(bundle).stat().st_size},
                'from_sha256': sorted(baselines), 'manifest_sha256': fingerprint(after),
                'execution': execution, 'rollback_safe': kind == 'worker'}
            _copy_target(self.served/'targets'/(sha256+'.runtime.zip'), bundle,
                         item['artifact'], mode=0o644)
            return self._commit(now, channels={**self.state['channels'], channel: sha256},
                                releases=self.state['releases']+[item], release_sequence=item['sequence'])

    def promote(self, sha256, *, channel):
        require(channel in CHANNELS, 'update_channel_refused'); digest(sha256)
        with file_lease(self.custody/'publish.lock'):
            now = self._load(); require(self.state['pending'] is None, 'update_publication_requires_resume')
            item = next((r for r in self.state['releases'] if r['artifact']['sha256'] == sha256), None)
            require(item is not None, 'update_release_absent')
            previous = next((r for r in self.state['releases'] if r['artifact']['sha256'] ==
                             self.state['channels'].get(channel)), None)
            require(previous is None or item['sequence'] >= previous['sequence'], 'update_channel_downgrade')
            return self._commit(now, channels={**self.state['channels'], channel: sha256},
                                releases=self.state['releases'], release_sequence=self.state['release_sequence'])

    def renew(self):
        with file_lease(self.custody/'publish.lock'):
            now = self._load(); require(self.state['pending'] is None, 'update_publication_requires_resume')
            return self._commit(now, channels=self.state['channels'], releases=self.state['releases'],
                                release_sequence=self.state['release_sequence'])

    def resume(self):
        with file_lease(self.custody/'publish.lock'):
            self._load()
            return self._finish()
