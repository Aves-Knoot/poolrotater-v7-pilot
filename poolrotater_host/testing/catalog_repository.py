"""Disposable local TUF repository with real Ed25519 signatures, offline only.

Private signers exist only in this fixture object's memory, never repository files.
This module is not a production publisher or public-root provisioning workflow.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os
import tempfile

from poolrotater_host.catalog_export import inspect_bundle
from poolrotater_host.catalog_trust import BootstrapTrust, dependencies
from poolrotater_host.config import ConfigError
from poolrotater_host.runtime_package import fixture_only


class FixtureRepository:
    def __init__(self, folder, bundles=()):
        fixture_only(); dependencies()
        from securesystemslib.signer import CryptoSigner
        from tuf.api.metadata import Metadata, Root, Snapshot, Targets, Timestamp
        self.folder = Path(folder)
        self.folder.mkdir(mode=0o700)
        (self.folder / 'metadata').mkdir()
        (self.folder / 'targets').mkdir()
        self.signers = {role: [CryptoSigner.generate_ed25519() for _ in range(2 if role=='root' else 1)]
                        for role in ('root', 'timestamp', 'snapshot', 'targets')}
        expires = datetime.now(timezone.utc) + timedelta(days=7)
        self.root = Metadata(Root(version=1, expires=expires, consistent_snapshot=True))
        for role, signers in self.signers.items():
            for signer in signers:
                self.root.signed.add_key(signer.public_key, role)
            self.root.signed.roles[role].threshold = len(signers)
        self.timestamp = Metadata(Timestamp(version=1, expires=expires))
        self.snapshot = Metadata(Snapshot(version=1, expires=expires))
        self.targets = Metadata(Targets(version=1, expires=expires))
        self.publish_root()
        self.trust = BootstrapTrust(self.root.to_bytes(), fixture=True)
        for bundle in bundles:
            self.add_bundle(bundle)
        self.publish()

    def publish_root(self):
        for index, signer in enumerate(self.signers['root']):
            self.root.sign(signer, append=index>0)
        self.root.to_file(str(self.folder/'metadata'/f'{self.root.signed.version}.root.json'))

    def add_bundle(self, bundle):
        bundle = Path(bundle)
        manifest = inspect_bundle(bundle)
        mode = manifest['mode']
        for path in sorted(bundle.iterdir()):
            self.add_target(mode + '/' + path.name, path.read_bytes())

    def add_target(self, name, data):
        from tuf.api.metadata import TargetFile
        from poolrotater_host.runtime_package import safe_name
        safe_name(name)
        if name.startswith(('metadata/', '.')):
            raise ConfigError('invalid fixture target path')
        info = TargetFile.from_data(name, data, ['sha256'])
        self.targets.signed.targets[name] = info
        parent = self.folder/'targets'/Path(name).parent
        parent.mkdir(parents=True, exist_ok=True)
        (parent/(info.hashes['sha256'] + '.' + Path(name).name)).write_bytes(data)
        return info

    def publish(self, *, bump=False, sign_targets=True):
        from tuf.api.metadata import MetaFile
        if bump:
            for metadata in (self.targets, self.snapshot, self.timestamp):
                metadata.signed.version += 1
        if sign_targets:
            self.targets.sign(self.signers['targets'][0])
        raw = self.targets.to_bytes()
        self._atomic(self.folder/'metadata'/f'{self.targets.signed.version}.targets.json', raw)
        self.snapshot.signed.meta = {'targets.json': MetaFile.from_data(self.targets.signed.version, raw, ['sha256'])}
        self.snapshot.sign(self.signers['snapshot'][0])
        raw = self.snapshot.to_bytes()
        self._atomic(self.folder/'metadata'/f'{self.snapshot.signed.version}.snapshot.json', raw)
        self.timestamp.signed.snapshot_meta = MetaFile.from_data(self.snapshot.signed.version, raw, ['sha256'])
        self.timestamp.sign(self.signers['timestamp'][0])
        self._atomic(self.folder/'metadata/timestamp.json', self.timestamp.to_bytes())

    @staticmethod
    def _atomic(path, data):
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
            temporary = Path(f.name)
            try:
                f.write(data); f.flush(); os.fsync(f.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True); raise
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)

    def publish_complete(self, *, bump=False):
        """Advertise only after every referenced immutable target verifies."""
        from poolrotater_host.catalog_trust import bounded_file
        for name, info in self.targets.signed.targets.items():
            with bounded_file(self.target_path(name), 512 * 1024**2) as f:
                info.verify_length_and_hashes(f)
        self.publish(bump=bump)

    def target_path(self, name):
        info = self.targets.signed.targets[name]
        return self.folder/'targets'/Path(name).parent/(info.hashes['sha256'] + '.' + Path(name).name)

    def rotate_root(self):
        from securesystemslib.signer import CryptoSigner
        previous = self.signers['root']
        self.signers['root'] = [CryptoSigner.generate_ed25519() for _ in previous]
        self.root.signed.version += 1
        for signer in previous:
            self.root.signed.revoke_key(signer.public_key.keyid, 'root')
        for signer in self.signers['root']:
            self.root.signed.add_key(signer.public_key, 'root')
        # Both old and new thresholds must authenticate this transition.
        for index, signer in enumerate(previous + self.signers['root']):
            self.root.sign(signer, append=index>0)
        self.root.to_file(str(self.folder/'metadata'/f'{self.root.signed.version}.root.json'))
