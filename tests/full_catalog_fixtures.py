"""Resolve immutable full exports and checked historical runtimes for isolated tests."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from functools import lru_cache
import hashlib
from pathlib import Path
from poolrotater_host.catalog_export import inspect_bundle


def workspace():
    for parent in Path(__file__).resolve().parents:
        if (parent/'scripts/test_isolation.py').is_file() and (parent/'validation/loved726/inputs').is_dir():
            return parent
    raise FileNotFoundError('isolated full catalog fixture workspace is unavailable')


@lru_cache(maxsize=5)
def full_export(mode):
    if mode not in ('standard','mania4','mania7','catch','loved'):
        raise ValueError('unsupported test export')
    folder=workspace()/'validation/private-booking/fixtures/catalogs'/mode
    manifest=inspect_bundle(folder)
    if manifest['mode']!=mode:
        raise AssertionError('full export mode does not match the fixture')
    for item in manifest['files']:
        path=folder/item['name']
        if path.is_symlink() or path.stat().st_mode & 0o222:
            raise AssertionError('full test export must remain immutable')
    return folder


def loved_source():
    path=workspace()/'validation/loved726/inputs/loved-source.db'
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError('full Loved research fixture is unavailable')
    return path


def historical_runtime(version):
    if version=='v7.1.20':
        path=workspace()/'validation/private-booking/fixtures/baseline-v7.1.20.zip'
        expected='b88e3c2a80ca94a68ee4a3412632c26d7e039d1d6c6e198f2becc397980da595'
    elif version=='v7.1.11':
        path=workspace()/'validation/private-booking/fixtures/v7.1.11-runtime.zip'
        expected='9750a3ee044db4aa852c4d3dfecb48838281ad237cad2e96d112983ea4ad8149'
    elif version=='v7.2.5':
        path=workspace()/'validation/private-booking/fixtures/roster-recovery725-runtime.zip'
        expected='ba5cf2fc6a1ca7b997ee03af35c0bdc8163a41fcc83032f2b2b6b2df5948ce9d'
    else:
        raise ValueError('unknown historical test runtime')
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
        raise AssertionError('historical runtime does not match retained manifest hash')
    return path
