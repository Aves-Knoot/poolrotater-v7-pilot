"""Persistent account-wide soft bans, serialized across room workers."""
import json
import logging
import time
import math
import re
import os
import stat
from pathlib import Path
from rolling.release import atomic_json

log = logging.getLogger('moderation')


def read_rolling_status(path):
    """Read native UID-owned status (0600/0640), never private credentials.

    RollingSupervisor publishes 0640 files atomically. Refuse unsafe metadata,
    mutation/replacement during the read, duplicate fields and nonfinite JSON.
    The caller supplies the fixed contained host status path.
    """
    path = Path(path).absolute()
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError('rolling status symlink refused')
    def identity(info):
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                info.st_uid, info.st_gid, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    def check(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) not in (0o600, 0o640)
                or info.st_size > 2*1024*1024):
            raise ValueError('unsafe rolling status metadata')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno()); check(before)
        raw = handle.read(2*1024*1024+1)
        after = os.fstat(handle.fileno()); check(after)
        current = path.lstat(); check(current)
        if (len(raw) != before.st_size or identity(before) != identity(after)
                or identity(before) != identity(current)):
            raise ValueError('rolling status changed during read')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value: raise ValueError('duplicate rolling status field')
            value[key] = item
        return value
    def invalid_number(_): raise ValueError('nonfinite rolling status number')
    def finite_float(raw):
        value = float(raw)
        if not math.isfinite(value): invalid_number(raw)
        return value
    value = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_number,
                       parse_float=finite_float)
    if not isinstance(value, dict): raise ValueError('invalid rolling status object')
    return value


def parse_ban_duration(value):
    match = re.fullmatch(r'([1-9][0-9]*)([mhd])', value.lower())
    if not match:
        raise ValueError('Use a duration such as 30m, 24h, or 7d.')
    seconds = int(match[1]) * {'m':60, 'h':3600, 'd':86400}[match[2]]
    if seconds > 365 * 86400:
        raise ValueError('Temporary bans must be between 1 minute and 365 days.')
    return seconds


class BanStore:
    def __init__(self, path):
        self.path = Path(path)
        self.bans = {}
        self.signature = None
        self.next_check = 0.0
        self.loaded = False
        self.last_error = None

    def read(self):
        if not self.path.exists():
            if self.loaded and self.bans:
                raise ValueError('ban file disappeared; restore it rather than clearing bans')
            return {'schema':1,'bans':[],'audit':[]}
        with self.path.open('rb') as stream:
            raw = stream.read(8*1024*1024+1)
        if len(raw)>8*1024*1024:
            raise ValueError('ban file exceeds 8 MiB')
        data=json.loads(raw)
        if (not isinstance(data,dict) or data.get('schema')!=1
                or not isinstance(data.get('bans'),list) or not isinstance(data.get('audit'),list)):
            raise ValueError('invalid ban file format')
        seen=set()
        for row in data['bans']:
            if (not isinstance(row,dict) or type(row.get('user_id')) is not int
                    or row['user_id']<=0 or row['user_id'] in seen):
                raise ValueError('invalid/duplicate banned user ID')
            seen.add(row['user_id'])
            expiry=row.get('expires_at')
            if expiry is not None and (type(expiry) not in (int,float)
                    or not math.isfinite(expiry) or expiry<=0):
                raise ValueError('invalid ban expiry')
        return data

    def _active(self, data):
        now=time.time()
        return {row['user_id']:row for row in data['bans']
                if row.get('expires_at') is None or row['expires_at']>now}

    def refresh(self, force=False):
        now=time.monotonic()
        if not force and now<self.next_check:
            self.bans=self._active({'bans':list(self.bans.values())})
            return
        self.next_check=now+2
        try:
            stat=self.path.stat() if self.path.exists() else None
            signature=(stat.st_ino,stat.st_mtime_ns,stat.st_size) if stat else None
            if self.loaded and signature==self.signature:
                self.bans=self._active({'bans':list(self.bans.values())})
                return
            data=self.read()
            self.bans=self._active(data)
            self.loaded=True;self.signature=signature;self.last_error=None
        except (OSError,ValueError,TypeError) as error:
            self.bans=self._active({'bans':list(self.bans.values())})
            if str(error)!=self.last_error:
                log.error('Cannot refresh bans at %s; retaining last valid list: %s',self.path,error)
                self.last_error=str(error)

    def change(self, action, uid, name, actor, actor_name, room, reason='', duration=None):
        if action not in ('ban','unban') or type(uid) is not int or uid<=0:
            raise ValueError('invalid moderation operation')
        if duration is not None and (action!='ban' or type(duration) is not int
                or not 60<=duration<=365*86400):
            raise ValueError('invalid temporary ban duration')
        import fcntl
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with self.path.with_suffix('.lock').open('a') as lock:
            deadline=time.monotonic()+3
            while True:
                try:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>deadline:raise TimeoutError('ban writer busy')
                    time.sleep(.02)
            data=self.read()
            bans={row['user_id']:row for row in data['bans']}
            previous=bans.get(uid)
            active=bool(previous and (previous.get('expires_at') is None or previous['expires_at']>time.time()))
            # A normal .ban promotes an active temporary ban to permanent.
            # A timed request never shortens/replaces an existing active ban.
            changed=(not active or (duration is None and previous.get('expires_at') is not None)) if action=='ban' else (uid in bans)
            if not changed:
                self.bans=self._active(data);return False
            event={'action':action,'user_id':uid,'username':name,'actor_id':actor,
                   'actor_name':actor_name,'room':room,'reason':reason,'at':time.time()}
            if action=='ban' and duration is not None:
                event['expires_at']=event['at']+duration
            if action=='ban':bans[uid]=event
            else:bans.pop(uid,None)
            data['bans']=list(bans.values());data['audit'].append(event)
            encoded=json.dumps(data).encode()
            if len(encoded)>8*1024*1024:raise ValueError('ban file full; archive audit before adding more')
            atomic_json(self.path,data)
            self.bans=self._active(data);self.loaded=True;self.signature=None;self.next_check=0
            log.warning('MODERATION %s user=%s actor=%s room=%s reason=%r',action,uid,actor,room,reason)
            return True
