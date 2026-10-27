"""Root coordination for stopped ownership work; never an owner/account lease.

Lock order matches managed reconciliation and controller updates. The network
admin lease also excludes an explicit local agent start while a stopped helper
runs. No timers, preferences, room intent or journals are changed here.
"""
from contextlib import contextmanager

from .config import ConfigError
from .layout import file_lease
from .pilot_installation import administrative_binding


@contextmanager
def admission(instance):
    initial=administrative_binding(instance)
    target,_,record=initial
    with file_lease(target['prefix']/'deployment-admission.lock'), \
            file_lease(target['prefix']/'network-admin.lock'):
        # Never use a predecessor observed before another admitted upgrade.
        if administrative_binding(instance)!=initial:
            raise ConfigError('stopped recovery registration changed before admission')
        yield target,record
