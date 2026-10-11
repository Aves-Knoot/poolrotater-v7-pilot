"""Invented qualification of the supported installer, never candidate authority.

Building is confined to offline packaging. Privileged installation/binding also
requires the protected disposable KVM role; a fixture environment flag alone
cannot authorize writes on the owner's machine. No credentials are accepted.
"""
import os
from pathlib import Path
import subprocess
import uuid

from ..config import ConfigError
from ..pilot_identity import QUALIFICATION
from ..runtime_package import _build, fixture_only

SETUP = Path('/opt/poolrotater-v7-test/SETUP.json')


def require_donor(*, owner_boot_id=None):
    from ..pilot_installation import _platform
    from ..provisioning import _control, protected
    fixture_only()
    if os.geteuid() != 0:
        raise ConfigError('qualification requires disposable donor administrator')
    _platform()
    protected(SETUP.parent)
    role = _control(SETUP)
    if (type(role) is not dict or role.get('purpose') != 'disposable-v7-test'
            or role.get('production_data_present') is not False):
        raise ConfigError('qualification requires the protected disposable donor role')
    result = subprocess.run(['/usr/bin/systemd-detect-virt'], capture_output=True,
        text=True, timeout=10, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
    if result.returncode or result.stdout.strip() != 'kvm':
        raise ConfigError('qualification requires the disposable KVM donor')
    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if owner_boot_id is not None:
        try:
            if str(uuid.UUID(owner_boot_id)) != owner_boot_id or owner_boot_id == boot_id:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise ConfigError('qualification donor must differ from the owner host') from None
    return boot_id


def build(root, cache, output, *, version='v7.0.0', catalog_cache):
    fixture_only()
    return _build(root, cache, output, version=version,
                  catalog_cache=catalog_cache, execution=QUALIFICATION)


def install(instance, bundle, expected_sha256, *, owner_boot_id, presets=None):
    if owner_boot_id is None:
        raise ConfigError('qualification installation requires the owner boot identity')
    require_donor(owner_boot_id=owner_boot_id)
    from ..pilot_installation import _install
    return _install(instance, bundle, expected_sha256,
                    presets=presets, execution=QUALIFICATION)


def administrative_binding(instance):
    from ..pilot_installation import _administrative_binding
    return _administrative_binding(instance, execution=QUALIFICATION)


def initialize_catalogs(instance, trust, expected_sha256):
    from ..catalog_store import CatalogStore
    return CatalogStore._initialize_pilot(instance, trust, expected_sha256,
                                         administrative_binding, QUALIFICATION)


def open_catalogs(instance):
    from ..catalog_store import CatalogStore
    return CatalogStore._open_pilot_administrative(instance, administrative_binding,
                                                 QUALIFICATION)


def unit_text(instance):
    require_donor()
    from ..pilot_operations import _unit_text
    return _unit_text(instance, qualification=True)


def install_unit(instance):
    target, _, record = administrative_binding(instance)
    from ..pilot_operations import _install_unit
    return _install_unit(instance, target, record, unit_text(instance))
