"""Execute an exact staged successor through its protected standalone entry."""
import subprocess

from ..runtime_lifecycle import validate_runtime
from .contracts import identifier, require


def controller(instance, target, job, *, decision=None):
    runtime = validate_runtime(job['after'], target['prefix'])
    entry = runtime['release'] / 'poolrotater_host/controller_update_entry.py'
    arguments = ['/usr/bin/python3.12', '-I', '-S', '-B', str(entry), instance]
    if decision is not None:
        require(decision == 'complete', 'update_recovery_decision_refused')
        identifier(job['id'])
        arguments += ['--operation-id', job['id'], '--decision', decision]
    result = subprocess.run(arguments, cwd='/tmp',
        env={'PATH': '/usr/sbin:/usr/bin:/bin', 'LANG': 'C.UTF-8',
             'PYTHONDONTWRITEBYTECODE': '1', 'MALLOC_ARENA_MAX': '2'},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1100)
    require(result.returncode == 0, 'update_installation_requires_review')
