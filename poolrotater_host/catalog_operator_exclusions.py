"""Mirror an explicitly configured owner chart policy into signed exclusions.

Only chart IDs leave the private source. Existing signed exclusions remain in
force; deleting a local row never silently reverses a published exclusion.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import stat

from .catalog_export import MODES
from .catalog_exclusions import MAX_ENTRIES,validate_feed
from .catalog_trust import no_links
from .config import ConfigError
from .layout import unique_fields

LIMIT=1024*1024


def _require(condition):
    if not condition:raise ConfigError('catalog_operator_exclusions_refused')


def read_policy(path,uid):
    _require(type(uid) is int and 0<=uid<=2**31-1)
    path=Path(path);_require(path.is_absolute());no_links(path)
    for parent in path.parents:
        info=parent.lstat()
        sticky_root=info.st_uid==0 and bool(info.st_mode&stat.S_ISVTX)
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid in (0,uid)
                 and (not info.st_mode&0o022 or sticky_root))
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(descriptor,'rb') as stream:
        before=os.fstat(stream.fileno())
        _require(stat.S_ISREG(before.st_mode) and before.st_uid==uid and before.st_nlink==1
                 and not before.st_mode&0o022 and before.st_size<=LIMIT)
        raw=stream.read(LIMIT+1);after=os.fstat(stream.fileno())
    stamp=lambda info:(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_uid,info.st_mode,info.st_nlink)
    _require(len(raw)<=LIMIT and stamp(before)==stamp(after)==stamp(path.lstat()))
    try:value=json.loads(raw,object_pairs_hook=unique_fields)
    except (ValueError,TypeError):raise ConfigError('catalog_operator_exclusions_refused') from None
    _require(type(value) is dict and set(value)=={'schema','exclusions'}
             and type(value['schema']) is int and value['schema']==1
             and type(value['exclusions']) is list and len(value['exclusions'])<=MAX_ENTRIES)
    identifiers=set()
    for row in value['exclusions']:
        _require(type(row) is dict and type(row.get('beatmap_id')) is int and 0<row['beatmap_id']<=2**53)
        identifiers.add(row['beatmap_id'])
    return {'ids':sorted(identifiers),'source_sha256':hashlib.sha256(raw).hexdigest()}


def merge_policy(current,identifiers):
    """Preserve cumulative signed policy, adding exact IDs in all native modes.

    The native operator ExclusionList is global by chart ID, irrespective of
    descriptive ruleset metadata. Match that enforcement, including DT/converts.
    """
    validate_feed(current)
    _require(type(identifiers) in (list,tuple,set,frozenset) and len(identifiers)<=MAX_ENTRIES
             and all(type(bid) is int and 0<bid<=2**53 for bid in identifiers)
             and all(row['action']=='exclude' for row in current['events']))
    result=copy.deepcopy(current);changed=False
    for bid in sorted(set(identifiers)):
        covered={mode for row in result['events'] if row['kind']=='beatmap' and row['id']==bid for mode in row['modes']}
        missing=sorted(set(MODES)-covered)
        if missing:
            result['events'].append({'kind':'beatmap','id':bid,'modes':missing,'action':'exclude',
                                     'reason':'moderation','memberships':{}})
            changed=True
    if changed:result['revision']+=1
    validate_feed(result)
    return result
