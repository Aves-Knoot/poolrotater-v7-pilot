"""Closed recovery receipt shared by startup, sync and operator status."""
import os
import re

from ..layout import contained,read_json
from ..network.contracts import require,fields,identifier,integer,fingerprint


def record(root,marker):
    path=contained(root,'state/ratings-epoch-review.json')
    if not os.path.lexists(path):return None
    value=read_json(path)
    fields(value,('format','installation_id','generation','phase','old','new_epoch','host_id','expected_profile_sha256','ledger_backup'))
    require(type(value['format']) is int and value['format']==1
            and all(value[k]==marker[k] for k in ('installation_id','generation'))
            and value['phase'] in ('prepared','complete'),'ratings_epoch_review_requires_review')
    identifier(value['new_epoch']);identifier(value['host_id'])
    old=value['old']
    fields(old,('format','installation_id','generation','share_ratings','network_profile_sha256','host_id','server_epoch'))
    require(type(old['format']) is int and old['format']==1 and type(old['share_ratings']) is bool
            and all(old[k]==marker[k] for k in ('installation_id','generation')),
            'ratings_epoch_review_requires_review')
    identifier(old['host_id']);identifier(old['server_epoch'])
    for digest in (old['network_profile_sha256'],value['expected_profile_sha256']):
        require(type(digest) is str and re.fullmatch('[a-f0-9]{64}',digest),'ratings_epoch_review_requires_review')
    require(fingerprint(old)==value['expected_profile_sha256'] and old['server_epoch']!=value['new_epoch'],
            'ratings_epoch_review_requires_review')
    if value['ledger_backup'] is not None:
        backup=value['ledger_backup'];fields(backup,('sha256','bytes'))
        require(type(backup['sha256']) is str and re.fullmatch('[a-f0-9]{64}',backup['sha256']),
                'ratings_epoch_review_requires_review');integer(backup['bytes'],1,256*1024**2)
    require(value['phase']!='complete' or value['ledger_backup'] is not None,'ratings_epoch_review_requires_review')
    return value
