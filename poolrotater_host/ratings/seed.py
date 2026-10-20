"""Repeat-safe stopped historical capture from the immutable migration backup.

Never open a production ledger or upload a full SQLite database. Only eligible
human opinions become closed immutable events; shadow/automatic rows stay local.
"""
import hashlib
import json
import sqlite3
import uuid
from urllib.parse import quote

from ..layout import contained, load
from ..network.contracts import require,canonical,fingerprint
from .migration import migration_record,_ledger_connection,_private,LEDGER,BACKUP
from .contracts import event
from bot.rating_outbox import validate, public_key


def seed_ledger(root):
    marker,_=load(root);record=migration_record(root)
    require(record is not None and record['phase']=='complete' and record['backup'] is not None,
            'historical_seed_backup_required')
    backup=_private(contained(root,BACKUP))
    with backup.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
    require(digest==record['backup']['sha256'] and backup.stat().st_size==record['backup']['bytes'],
            'historical_seed_backup_mismatch')
    # Read only the captured historical snapshot. Current capture, policy and
    # seed rows are reconciled atomically under a stopped writer proof upstream.
    source=sqlite3.connect('file:'+quote(str(backup))+'?mode=ro&immutable=1',uri=True)
    conn=_ledger_connection(contained(root,LEDGER))
    counts={'seeded':0,'already_seeded':0,'post_boundary':0,'excluded':0}
    namespace=uuid.UUID(marker['installation_id'])
    try:
        require(source.execute('PRAGMA quick_check').fetchall()==[('ok',)],'historical_seed_backup_mismatch')
        conn.execute('BEGIN EXCLUSIVE');meta=validate(conn)
        require(tuple(meta[1:3])==(marker['installation_id'],marker['generation']),'foreign_rating_stream')
        for bid,uid,rating,created,origin,keys,ignored,cutoff in source.execute(
            'SELECT r.beatmap_id,r.user_id,r.rating,r.created_at,r.map_origin,r.key_count,p.is_ignored,p.accept_after '
            'FROM map_ratings r LEFT JOIN rating_ignored_users p ON p.user_id=r.user_id ORDER BY r.beatmap_id,r.user_id'):
            if (created is None or created>record['capture_started'] or ignored
                    or (ignored is not None and (cutoff is None or created<cutoff))):
                counts['excluded']+=1;continue
            policy=conn.execute('SELECT is_ignored,accept_after FROM rating_ignored_users WHERE user_id=?',(uid,)).fetchone()
            if policy is not None and (policy[0] or policy[1] is None or created<policy[1]):
                counts['excluded']+=1;continue
            public_bid, ruleset_id, variant = public_key(bid)
            seed_id=str(uuid.uuid5(namespace,marker['generation']+':'+digest+':'+str(bid)+':'+str(uid)))
            prior=conn.execute('SELECT payload FROM rating_outbox WHERE event_id=?',(seed_id,)).fetchone()
            if prior is not None:
                item=json.loads(prior[0]);event(item)
                require(item['kind']=='historical_seed' and item['rating']==rating
                        and item['beatmap_id']==public_bid and item['user_id']==uid,'historical_seed_identity_mismatch')
                counts['already_seeded']+=1;continue
            # Any genuine post-boundary command wins, even if pending/conflicted.
            if conn.execute('SELECT 1 FROM rating_outbox WHERE beatmap_id=? AND user_id=? LIMIT 1',(bid,uid)).fetchone():
                counts['post_boundary']+=1;continue
            sequence=validate(conn)[3]+1
            item={'format':2 if ruleset_id==2 else 1,'event_id':seed_id,**{k:marker[k] for k in ('installation_id','generation')},
                  'sequence':sequence,'kind':'historical_seed','beatmap_id':public_bid,'user_id':uid,'rating':rating,
                  'base_revision':None,'predecessor':None,'observed_at':created,'map_origin':origin,'key_count':keys,
                  'context':{'ruleset_id':ruleset_id,'room_id':None,'playlist_item_id':None,'required_mods':[]}}
            event(item)
            conn.execute('INSERT INTO rating_outbox(event_id,sequence,beatmap_id,user_id,payload) VALUES(?,?,?,?,?)',
                         (seed_id,sequence,bid,uid,canonical(item).decode()))
            conn.execute('UPDATE rating_sync_meta SET sequence=? WHERE singleton=1',(sequence,));counts['seeded']+=1
        conn.commit()
    except BaseException:conn.rollback();raise
    finally:conn.close();source.close()
    return {'ok':True,**counts,'historical_seed':True,'uploaded':False,'provider_dispatch':False}
