"""Pause only this installation's independent timers during an admitted switch.

Timers are not disabled, and an executing updater service is never stopped.
The durable original active states survive interrupted attempts. Native owner
leases and the service-UID process census remain authoritative throughout.
"""
from contextlib import contextmanager
import hashlib
import logging
import os

from .config import ConfigError


@contextmanager
def quiesce(instance,target,job,save):
    from .pilot_operations import UNIT_BASE,_systemctl
    from .provisioning import protected
    names={'poolrotater-v7-'+kind+'-'+instance+'.timer' for kind in ('managed','updates','recovery')}
    records=job.get('paused_helper_timers')
    if records is None:
        records=[]
        for name in sorted(names):
            path=UNIT_BASE/name
            if not os.path.lexists(path):continue
            raw=protected(path).read_bytes()
            units=[line for line in raw.decode('ascii').splitlines() if line.startswith('Unit=')]
            if units!=['Unit='+name.removesuffix('.timer')+'.service'] or b'[Timer]' not in raw:
                raise ConfigError('update_guard_requires_review')
            state=_systemctl('show',name,'--property=ActiveState','--value').decode('ascii').strip()
            if state not in ('active','inactive','failed'):raise ConfigError('update_guard_requires_review')
            records.append({'name':name,'sha256':hashlib.sha256(raw).hexdigest(),'was_active':state=='active'})
        save(paused_helper_timers=records)
    if not isinstance(records,list) or len(records)>3:raise ConfigError('update_guard_requires_review')
    seen=set()
    for row in records:
        if (type(row) is not dict or set(row)!= {'name','sha256','was_active'}
                or row['name'] not in names or row['name'] in seen or type(row['was_active']) is not bool):
            raise ConfigError('update_guard_requires_review')
        seen.add(row['name'])
        raw=protected(UNIT_BASE/row['name']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=row['sha256']:raise ConfigError('update_guard_requires_review')
    try:
        for row in records:_systemctl('stop',row['name'])
        save(helper_pause_active=True)
        yield
    finally:
        failed=False
        for row in records:
            if not row['was_active']:continue
            try:_systemctl('start',row['name'])
            except ConfigError:
                failed=True
                logging.getLogger(__name__).warning('controller helper timer restoration requires review')
        save(helper_pause_active=failed,helper_restore_required=failed)
