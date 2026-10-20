#!/usr/bin/env python3
"""Build a fixed, manually trusted network pilot from qualified prepared exports.

No production database/account/config discovery. New pilot TUF signers stay in
memory and are discarded; this immutable 30-day publication is not fleet key
custody or an updater. TLS private material goes only to an explicit external
root-private directory, never the kit/source/development evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pilot.pilot import canonical, digest, no_links, regular, PilotError
from pilot.catalog_pilot import verify
from poolrotater_host.catalog_export import inspect_bundle, CORE_MODES as MODES
from scripts.prepare_pilot_release import scan, wheel_metadata, zip_tree


def publish_catalogs(prepared, destination):
    from securesystemslib.signer import CryptoSigner
    from tuf.api.metadata import Metadata, Root, Snapshot, Targets, Timestamp, MetaFile, TargetFile
    now = datetime.now(timezone.utc); expires = now + timedelta(days=30)
    destination.mkdir(mode=0o700)
    (destination / 'metadata').mkdir(); (destination / 'targets').mkdir()
    signers = {r: [CryptoSigner.generate_ed25519() for _ in range(2 if r == 'root' else 1)]
               for r in ('root', 'targets', 'snapshot', 'timestamp')}
    root = Metadata(Root(version=1, expires=now + timedelta(days=365), consistent_snapshot=True))
    for role, keys in signers.items():
        for key in keys: root.signed.add_key(key.public_key, role)
        root.signed.roles[role].threshold = len(keys)
    for index, key in enumerate(signers['root']): root.sign(key, append=index > 0)
    root_bytes = root.to_bytes()
    (destination / 'metadata/1.root.json').write_bytes(root_bytes)
    targets = Metadata(Targets(version=1, expires=expires)); ids = {}; hashes = {}
    for mode in MODES:
        folder = prepared / mode; manifest = inspect_bundle(folder)
        if manifest['mode'] != mode: raise PilotError('prepared_catalog_mode_mismatch')
        ids[mode] = manifest['content_id']
        hashes[mode] = {p.name: digest(p) for p in folder.iterdir()}
        (destination / 'targets' / mode).mkdir()
        # Readers cross-check the expanded DB identities against signed target
        # metadata as well as the compressed archive. Sign every export member.
        for name in sorted(hashes[mode]):
            data = regular(folder / name).read_bytes()
            info = TargetFile.from_data(mode + '/' + name, data, ['sha256'])
            targets.signed.targets[mode + '/' + name] = info
            (destination / 'targets' / mode / (info.hashes['sha256'] + '.' + name)).write_bytes(data)
    targets.sign(signers['targets'][0]); raw = targets.to_bytes()
    (destination / 'metadata/1.targets.json').write_bytes(raw)
    snapshot = Metadata(Snapshot(version=1, expires=expires))
    snapshot.signed.meta = {'targets.json': MetaFile.from_data(1, raw, ['sha256'])}
    snapshot.sign(signers['snapshot'][0]); raw = snapshot.to_bytes()
    (destination / 'metadata/1.snapshot.json').write_bytes(raw)
    timestamp = Metadata(Timestamp(version=1, expires=expires))
    timestamp.signed.snapshot_meta = MetaFile.from_data(1, raw, ['sha256'])
    timestamp.sign(signers['timestamp'][0])
    (destination / 'metadata/timestamp.json').write_bytes(timestamp.to_bytes())
    return root_bytes, ids, hashes, expires.isoformat()


def certificate(ip, private, kit):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    now = datetime.now(timezone.utc); ca_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'PoolRotater fixed catalog pilot CA')])
    ca = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                      content_commitment=False, key_encipherment=False, data_encipherment=False,
                                      key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    server_key = ec.generate_private_key(ec.SECP256R1())
    server = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(ip))]))
              .issuer_name(subject).public_key(server_key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=90))
              .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
              .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip)]), critical=False)
              .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
              .sign(ca_key, hashes.SHA256()))
    (kit / 'ca.crt').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    # CA private key is discarded. The server cannot mint trusted certificates.
    (private / 'server.crt').write_bytes(server.public_bytes(serialization.Encoding.PEM))
    key_bytes = server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption())
    with (private / 'server.key').open('xb') as handle:
        os.fchmod(handle.fileno(), 0o600); handle.write(key_bytes)


def prepare(*, prepared, wheel, output, private_directory, origin):
    if os.getpriority(os.PRIO_PROCESS, 0) < 15: os.nice(15 - os.getpriority(os.PRIO_PROCESS, 0))
    os.umask(0o077)
    prepared, wheel, output, private = map(no_links, (prepared, wheel, output, private_directory))
    if (private.is_relative_to(ROOT) or not private.is_relative_to(Path('/root'))
            or output.exists() or private.exists()):
        raise PilotError('fresh_external_private_directory_and_output_required')
    if shutil.disk_usage(ROOT).free < 5 * 1024**3: raise PilotError('five_gib_disk_reserve_required')
    from urllib.parse import urlsplit
    parsed = urlsplit(origin); ip = ipaddress.ip_address(parsed.hostname)
    if parsed.scheme != 'https' or parsed.port is None: raise PilotError('explicit_https_ip_required')
    package, version, _ = wheel_metadata(regular(wheel).read_bytes())
    if package != 'poolrotater-host' or version != '7.0.0.dev27': raise PilotError('requires_dev27_wheel')
    output.mkdir(mode=0o700, parents=True); private.mkdir(mode=0o700)
    kit = output / 'kit'; kit.mkdir(mode=0o700)
    bootstrap, ids, hashes, expires = publish_catalogs(prepared, output / 'repository')
    (kit / 'bootstrap.json').write_bytes(bootstrap)
    certificate(ip, private, kit)
    for source, target in (('pilot/catalog_pilot.py', 'catalog_pilot.py'), ('pilot/pilot.py', 'pilot.py'),
                           ('pilot/CATALOG_DOWNLOAD_README.md', 'README.md')):
        data = regular(ROOT / source).read_bytes(); scan(data); (kit / target).write_bytes(data)
    old = ROOT / 'validation/pilot-github-publication/source'
    for name in ('SOURCE_POLICY.md', 'THIRD_PARTY_NOTICES.md', 'notice-inventory.json'):
        shutil.copyfile(old / name, kit / name)
    shutil.copytree(old / 'notices', kit / 'notices')
    (kit / 'wheelhouse').mkdir(); wheels = []
    for path in [wheel, *sorted((ROOT / '.catalog-wheelhouse').glob('*.whl'))]:
        raw = path.read_bytes(); package, version, _ = wheel_metadata(raw)
        shutil.copyfile(path, kit / 'wheelhouse' / path.name)
        wheels.append({'name': package, 'version': version, 'file': path.name, 'sha256': digest(path)})
    wheels.sort(key=lambda w: w['name'])
    (kit / 'requirements-hashed.lock').write_text(''.join(
        f"{w['name']}=={w['version']} --hash=sha256:{w['sha256']}\n" for w in wheels))
    manifest = {'format': 1, 'kind': 'https-catalog-download-pilot', 'version': '7.0.0.dev27',
                'live_ready': False, 'target': {'os': 'Ubuntu 24.04', 'architecture': 'x86_64', 'python': '3.12'},
                'connection': {'origin': origin, 'bootstrap_sha256': digest(kit / 'bootstrap.json'),
                               'ca_sha256': digest(kit / 'ca.crt'), 'content_ids': ids, 'metadata_expires': expires},
                'wheels': wheels, 'files': {p.relative_to(kit).as_posix(): {'bytes': p.stat().st_size, 'sha256': digest(p)}
                                          for p in sorted(kit.rglob('*')) if p.is_file()}}
    (kit / 'release.json').write_bytes(canonical(manifest)); manifest_hash = digest(kit / 'release.json')
    verify(kit, manifest_hash)
    zip_tree(kit, output / 'catalog-download-pilot.zip')
    (output / 'SHA256SUMS.txt').write_text(digest(output / 'catalog-download-pilot.zip')+'  catalog-download-pilot.zip\n')
    report = {'format': 1, 'ok': True, 'version': '7.0.0.dev27', 'manifest_sha256': manifest_hash,
              'archive_sha256': digest(output / 'catalog-download-pilot.zip'),
              'archive_bytes': (output / 'catalog-download-pilot.zip').stat().st_size,
              'connection': manifest['connection'], 'source_catalog_hashes': hashes,
              'repository_files': {p.relative_to(output / 'repository').as_posix(): {'bytes': p.stat().st_size, 'sha256': digest(p)}
                                   for p in sorted((output / 'repository').rglob('*')) if p.is_file()},
              'private_signers_exported': False, 'fixed_snapshot_only': True, 'live_ready': False}
    for path in kit.rglob('*'):
        if path.is_file(): scan(path.read_bytes())
    (output / 'report.json').write_bytes(canonical(report))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('prepared', 'wheel', 'output', 'private-directory'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--origin', required=True)
    args = vars(parser.parse_args())
    report = prepare(**args)
    print(json.dumps({k: report[k] for k in ('ok', 'version', 'manifest_sha256', 'archive_sha256', 'archive_bytes')}))
