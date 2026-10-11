"""Read-only offline prerequisite observations; never boots or installs a VM."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess

from .config import ConfigError
from .runtime_package import fixture_only


def preflight(*, image=None, expected_sha256=None):
    fixture_only()
    emulator = shutil.which('qemu-system-x86_64')
    guest = None
    if image is not None:
        image = Path(image).absolute()
        if (image.suffix not in ('.qcow2', '.img', '.iso', '.raw')
                or not isinstance(expected_sha256, str) or not re.fullmatch('[a-f0-9]{64}', expected_sha256)):
            raise ConfigError('trusted local guest image and out-of-band digest required')
        for path in (image, *image.parents):
            if path.is_symlink(): raise ConfigError('guest image paths cannot contain links')
        fd = os.open(image, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            mode = os.fstat(stream.fileno())
            if not stat.S_ISREG(mode.st_mode) or mode.st_nlink != 1 or mode.st_size > 8 * 1024**3:
                raise ConfigError('guest image must be a bounded regular file')
            actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != expected_sha256: raise ConfigError('guest image digest mismatch')
        guest = {'path': str(image), 'sha256': actual, 'bytes': mode.st_size,
                 'boot_and_no_backing_chain_verified': False}
    elif expected_sha256 is not None:
        raise ConfigError('image digest without local image')
    dotnet = Path('/usr/lib/dotnet/dotnet')
    runtimes = []
    if dotnet.is_file():
        result = subprocess.run([str(dotnet), '--list-runtimes'], cwd='/tmp',
                                env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                                     'DOTNET_CLI_TELEMETRY_OPTOUT': '1'},
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 0: runtimes = result.stdout.splitlines()
    os_release = Path('/usr/lib/os-release')
    distribution = {}
    if os_release.is_file():
        for line in os_release.read_text().splitlines():
            if line.startswith(('ID=', 'VERSION_ID=')):
                key, value = line.split('=', 1); distribution[key] = value.strip('"')
    missing = []
    if emulator is None: missing.append('offline VM emulator')
    if guest is None: missing.append('trusted local disposable Ubuntu 24.04 guest image')
    return {'format': 1, 'execution': 'offline-preflight', 'live_ready': False,
            'machine': {'distribution': distribution, 'architecture': platform.machine(),
                        'python': platform.python_version()},
            'emulator': emulator, 'hardware_acceleration_present': Path('/dev/kvm').exists(),
            'guest_image': guest, 'local_dotnet_runtimes': runtimes,
            'local_dotnet_8_present': any(line.startswith('Microsoft.NETCore.App 8.') for line in runtimes),
            'missing_inputs': missing, 'vm_qualification_completed': False,
            'systemd_start_stop_reboot_tested': False, 'downloads_performed': False,
            'host_packages_or_services_changed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', type=Path)
    parser.add_argument('--sha256')
    args = parser.parse_args()
    print(json.dumps(preflight(image=args.image, expected_sha256=args.sha256), indent=2))


if __name__ == '__main__': main()
