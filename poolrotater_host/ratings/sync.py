"""Explicit opt-in synchronization of immutable local events; no provider I/O."""
import json
import hashlib
import os
import re
from pathlib import Path
import uuid

from ..layout import load, contained, read_json, atomic_json, file_lease
from ..network.client import Client
from ..network.contracts import require, fields, integer, identifier, canonical, fingerprint, NetworkError
from .contracts import event, MAX_BODY
from .migration import _ledger_connection, LEDGER, local_status
from bot.rating_outbox import validate

PHASES={'accepted','already_accepted','superseded','conflict','invalid','quarantined','permanently_rejected','retryable'}
# Client cadence is deliberately narrower than the unchanged 500-event wire
# contract. A nominal 30-minute scheduler sends at most 4,800 events per day.
# Manual calls, retry paths and per-player limits still require separate care.
UPLOAD_BATCH_EVENTS = 100
# Only a transient external query failure may defer reconciliation after a
# committed acknowledgment. Format, authority, epoch and policy errors retain
# their existing error/review path, even if reported with a 5xx status.
TRANSIENT_QUERY_ERRORS = frozenset(('hub_unavailable','hub_deadline_exceeded',
    'hub_transport_failed','hub_rate_limited','hub_storage_unavailable'))
REASONS={'known_revision','current_predecessor','same_current_value','canonical_revision_changed',
         'key_suppressed','invalid_event_identity','invalid_event','event_id_conflict','sequence_reused',
         'restored_sequence','host_identity_mismatch','contribution_observe_only','host_quota','player_quota',
         'historical_seed_obsolete','historical_seed_not_authorized','policy_excluded'}


class Synchronizer:
    def __init__(self, root, *, network=None):
        self.root=Path(root);root=self.root;self.marker,_=load(root)
        self.network=network or Client(root)
        self.profile_path=contained(root,'config/ratings.json')
        self.cursor_path=contained(root,'state/ratings-query.json')
        self.lock_path=contained(root,'state/ratings-sync.lock')

    def configure(self, *, share_ratings, only_if_missing=False):
        require(type(share_ratings) is bool,'explicit_rating_sharing_required')
        with file_lease(self.lock_path),file_lease(self.network.lock_path):
            if only_if_missing and os.path.lexists(self.profile_path):return self.profile()
            require(local_status(self.root)['capture_enabled'],'ratings_migration_required')
            profile=self.network.profile();credential=self.network.credential()
            require(credential is not None and self.network.pending(profile) is None,'network_enrollment_required')
            value={'format':1,'installation_id':self.marker['installation_id'],'generation':self.marker['generation'],
                   'share_ratings':share_ratings,'network_profile_sha256':fingerprint(profile),
                   'host_id':credential['host_id'],'server_epoch':credential['server_epoch']}
            if os.path.lexists(self.profile_path):
                old=self.profile()
                require(all(old[k]==value[k] for k in ('network_profile_sha256','host_id','server_epoch')),
                        'ratings_restored_binding_requires_review')
            atomic_json(self.profile_path,value)
        return {'ok':True,'share_ratings':share_ratings,'provider_dispatch':False}

    def profile(self,*,allow_review=False):
        from .epoch_review import record
        review=record(self.root,self.marker)
        if not allow_review and review is not None:
            require(review['phase']=='complete','ratings_epoch_review_recovery_required')
        value=read_json(self.profile_path)
        fields(value,('format','installation_id','generation','share_ratings','network_profile_sha256','host_id','server_epoch'))
        require(type(value['format']) is int and value['format']==1 and type(value['share_ratings']) is bool,'invalid_ratings_profile')
        require(all(value[k]==self.marker[k] for k in ('installation_id','generation')),'foreign_rating_stream')
        identifier(value['host_id']);identifier(value['server_epoch'])
        require(type(value['network_profile_sha256']) is str and re.fullmatch('[a-f0-9]{64}',value['network_profile_sha256']),'invalid_ratings_profile')
        return value

    def recover_stream(self):
        """Internal stopped administrator operation, not automatic enrollment.

        Monotonic sequence repair preserves all event IDs/payloads/outcomes. The
        root wrapper independently proves writer absence before invoking this.
        """
        with file_lease(self.lock_path),file_lease(self.network.lock_path):
            old=self.profile();profile=self.network.profile();credential=self.network.credential()
            require(credential is not None and self.network.pending(profile) is None,'network_enrollment_required')
            require(old['network_profile_sha256']==fingerprint(profile)
                    and old['server_epoch']==credential['server_epoch'],'ratings_canonical_restore_requires_review')
            self.network.check_account(profile['account_id'])
            reply=self.network.transport(profile).request('POST','/v1/ratings/stream-state',token=credential['credential'],
                    body={'protocol_version':1,'previous_host_id':old['host_id']})
            fields(reply,('ok','protocol_version','host_id','server_epoch','installation_id','generation','last_sequence'))
            require(reply['ok'] is True and type(reply['protocol_version']) is int and reply['protocol_version']==1
                    and all(reply[k]==self.marker[k] for k in ('installation_id','generation'))
                    and all(reply[k]==credential[k] for k in ('host_id','server_epoch')),'invalid_rating_stream_reply')
            high=integer(reply['last_sequence'],0)
            conn=self._conn()
            try:
                conn.execute('BEGIN EXCLUSIVE')
                local=validate(conn)[3];latest=conn.execute('SELECT MAX(sequence) FROM rating_outbox').fetchone()[0] or 0
                high=max(high,local,latest)
                conn.execute('UPDATE rating_sync_meta SET sequence=? WHERE singleton=1',(high,));conn.commit()
            except BaseException:conn.rollback();raise
            finally:conn.close()
            # A crash here repeats the monotonic transaction. Until this profile
            # publication, replacement-host sync remains fenced by old binding.
            atomic_json(self.profile_path,{**old,'host_id':credential['host_id']})
        return {'ok':True,'sequence':high,'old_events_rewritten':False,'provider_dispatch':False}

    def _conn(self):
        conn=_ledger_connection(contained(self.root,LEDGER))
        try:
            meta=validate(conn)
            require(tuple(meta[1:3])==(self.marker['installation_id'],self.marker['generation']),'foreign_rating_stream')
            return conn
        except BaseException:conn.close();raise

    def _body(self):
        body={'protocol_version':1,'batch_id':str(uuid.uuid4()),'events':[]}
        encoded_size=len(canonical(body))
        conn=self._conn()
        try:
            conn.execute('BEGIN IMMEDIATE')
            rows=conn.execute("SELECT event_id,payload FROM rating_outbox WHERE phase IN ('pending','retryable') ORDER BY sequence LIMIT ?",
                              (UPLOAD_BATCH_EVENTS,)).fetchall()
            for event_id,payload in rows:
                item=json.loads(payload);event(item)
                require(item['event_id']==event_id and all(item[k]==self.marker[k] for k in ('installation_id','generation')),'foreign_rating_stream')
                policy=conn.execute('SELECT is_ignored,accept_after FROM rating_ignored_users WHERE user_id=?',(item['user_id'],)).fetchone()
                excluded=policy is not None and (policy[0] or policy[1] is None or item['observed_at']<policy[1])
                if excluded:
                    # A previously sent/lost-reply event may already be central.
                    # This local exclusion does not claim global removal.
                    conn.execute("UPDATE rating_outbox SET phase='quarantined',acknowledgment=? WHERE event_id=?",
                        (json.dumps({'local_policy_excluded':True,'aggregate_inclusion_unknown':True}),event_id))
                    continue
                added=len(canonical(item))+(1 if body['events'] else 0)
                if encoded_size+added>MAX_BODY:break
                body['events'].append(item);encoded_size+=added
            conn.commit()
        except BaseException:conn.rollback();raise
        finally:conn.close()
        return body

    def _acknowledge(self, body, reply):
        fields(reply,('ok','protocol_version','outcomes','processing_watermark','aggregate_inclusion_confirmed'))
        require(reply['ok'] is True and type(reply['protocol_version']) is int and reply['protocol_version']==1
                and reply['aggregate_inclusion_confirmed'] is False,'invalid_rating_ack')
        integer(reply['processing_watermark'],0)
        require(type(reply['outcomes']) is list and len(reply['outcomes'])<=len(body['events']),'invalid_rating_ack')
        seen=set()
        for ack in reply['outcomes']:
            fields(ack,('index','event_id','phase','reason','revision','watermark'))
            integer(ack['index'],0,len(body['events'])-1)
            require(ack['index'] not in seen,'invalid_rating_ack');seen.add(ack['index'])
            item=body['events'][ack['index']]
            require(ack['event_id']==item['event_id'] and ack['phase'] in PHASES and ack['reason'] in REASONS,'invalid_rating_ack')
            integer(ack['revision'],0);integer(ack['watermark'],0)
            require(ack['revision']<=ack['watermark']<=reply['processing_watermark'],'invalid_rating_ack')
            if ack['phase'] in ('accepted','already_accepted'):require(ack['revision']>0,'invalid_rating_ack')
        conn=self._conn()
        try:
            conn.execute('BEGIN IMMEDIATE')
            for ack in reply['outcomes']:
                item=body['events'][ack['index']]
                row=conn.execute('SELECT payload,phase FROM rating_outbox WHERE event_id=?',(item['event_id'],)).fetchone()
                require(row is not None and fingerprint(json.loads(row[0]))==fingerprint(item)
                        and row[1] in ('pending','retryable'),'rating_outbox_changed_during_upload')
                conn.execute('UPDATE rating_outbox SET phase=?,acknowledgment=? WHERE event_id=?',
                             (ack['phase'],canonical(ack).decode(),item['event_id']))
            conn.commit()
        except BaseException:conn.rollback();raise
        finally:conn.close()

    def _query(self, transport, token):
        cursor=0
        if os.path.lexists(self.cursor_path):
            value=read_json(self.cursor_path);fields(value,('format','installation_id','generation','sequence'))
            require(type(value['format']) is int and value['format']==1
                    and all(value[k]==self.marker[k] for k in ('installation_id','generation')),'invalid_rating_query_cursor')
            cursor=integer(value['sequence'],0)
        conn=self._conn()
        try:
            rows=conn.execute("SELECT o.event_id,o.beatmap_id,o.user_id,o.sequence FROM rating_outbox o "
                "WHERE o.phase IN ('accepted','already_accepted','conflict','superseded','permanently_rejected') AND o.sequence>? "
                "AND NOT EXISTS(SELECT 1 FROM rating_outbox n WHERE n.beatmap_id=o.beatmap_id AND n.user_id=o.user_id AND n.sequence>o.sequence) "
                'ORDER BY o.sequence LIMIT 100',(cursor,)).fetchall()
        finally:conn.close()
        if not rows:
            atomic_json(self.cursor_path,{'format':1,'installation_id':self.marker['installation_id'],
                       'generation':self.marker['generation'],'sequence':0});return 0
        ids=[r[0] for r in rows]
        reply=transport.request('POST','/v1/ratings/state-query',token=token,body={'protocol_version':1,'event_ids':ids})
        fields(reply,('ok','protocol_version','states'))
        require(reply['ok'] is True and type(reply['protocol_version']) is int and reply['protocol_version']==1
                and type(reply['states']) is list and len(reply['states'])==len(rows),'invalid_rating_query_reply')
        revisions=[]
        for row,state in zip(rows,reply['states']):
            require(type(state) is dict and state.get('event_id')==row[0] and type(state.get('known')) is bool,'invalid_rating_query_reply')
            if not state['known']:fields(state,('event_id','known'));continue
            fields(state,('event_id','known','revision','rating','suppressed'));integer(state['revision'],0)
            require((state['rating'] is None or (type(state['rating']) is int and state['rating'] in (-1,0,1)))
                    and type(state['suppressed']) is bool,'invalid_rating_query_reply')
            revisions.append((row[1],row[2],state['revision']))
        conn=self._conn()
        try:
            conn.execute('BEGIN IMMEDIATE')
            for bid,uid,revision in revisions:
                prior=conn.execute('SELECT revision FROM rating_revision_cache WHERE beatmap_id=? AND user_id=?',(bid,uid)).fetchone()
                require(prior is None or revision>=prior[0],'ratings_canonical_restore_requires_review')
                conn.execute('INSERT INTO rating_revision_cache VALUES(?,?,?) ON CONFLICT(beatmap_id,user_id) DO UPDATE SET revision=excluded.revision',(bid,uid,revision))
            conn.commit()
        except BaseException:conn.rollback();raise
        finally:conn.close()
        atomic_json(self.cursor_path,{'format':1,'installation_id':self.marker['installation_id'],
                    'generation':self.marker['generation'],'sequence':rows[-1][3]})
        return len(rows)

    def ensure_defaults(self):
        """Bind a missing profile only; explicit sharing-disabled files are retained."""
        if os.path.lexists(self.profile_path):return True
        if not (self.root/'config/network.json').exists():return False
        with file_lease(self.network.lock_path):
            network=self.network.profile();credential=self.network.credential()
            if credential is None or self.network.pending(network) is not None:return False
            self.network.check_account(network['account_id'])
        self.configure(share_ratings=True,only_if_missing=True)
        return True

    def has_fresh_feedback(self, since, until):
        """Read-only wake-up hint. Historical backlog does not accelerate itself."""
        conn = self._conn()
        try:
            return conn.execute(
                "SELECT 1 FROM rating_outbox WHERE phase IN ('pending','retryable') "
                "AND json_extract(payload,'$.kind')='rating_set' "
                "AND json_extract(payload,'$.observed_at')>? "
                "AND json_extract(payload,'$.observed_at')<=? LIMIT 1", (since, until),
            ).fetchone() is not None
        finally:
            conn.close()

    def scheduled_step(self, now):
        from .delivery import DeliveryBudget
        return self.step(delivery=DeliveryBudget(self.root, self.marker, now))

    def step(self, *, delivery=None):
        if not self.ensure_defaults():return {'ok':True,'phase':'not-configured','uploaded':0}
        with file_lease(self.lock_path),file_lease(self.network.lock_path):
            settings=self.profile()
            if not settings['share_ratings']:return {'ok':True,'phase':'sharing-disabled','uploaded':0}
            profile=self.network.profile();credential=self.network.credential()
            require(credential is not None and self.network.pending(profile) is None,'network_enrollment_required')
            self.network.check_account(profile['account_id'])
            require(fingerprint(profile)==settings['network_profile_sha256']
                    and all(credential[k]==settings[k] for k in ('host_id','server_epoch')),'ratings_restored_binding_requires_review')
            transport=self.network.transport(profile);body=self._body()
            if delivery is not None:
                # The synchronization lease serializes budget reservations too.
                # Persist before dispatch; a lost response reuses the same IDs.
                body['events'] = delivery.select(body['events'])
            uploaded=0
            if body['events']:
                reply=transport.request('POST','/v1/events/batches',token=credential['credential'],body=body)
                self._acknowledge(body,reply);uploaded=len(reply['outcomes'])
            try:queried=self._query(transport,credential['credential'])
            except NetworkError as error:
                if (uploaded > 0 and error.code in TRANSIENT_QUERY_ERRORS
                        and (error.status==429 or 500<=error.status<=599)):
                    # Upload intent is already durably acknowledged. Retrying
                    # this whole step at transport backoff speed would advance
                    # new unique backlog chunks, bypassing nominal cadence.
                    return {'ok':True,'phase':'query-deferred','uploaded':uploaded,
                        'queried':0,'aggregate_inclusion_confirmed':False}
                raise
        return {'ok':True,'phase':'synchronized','uploaded':uploaded,'queried':queried,'aggregate_inclusion_confirmed':False}

    def fetch_snapshots(self):
        from .snapshots import Cache
        with file_lease(self.lock_path),file_lease(self.network.lock_path):
            settings=self.profile();profile=self.network.profile();credential=self.network.credential()
            require(credential is not None and self.network.pending(profile) is None,'network_enrollment_required')
            self.network.check_account(profile['account_id'])
            require(fingerprint(profile)==settings['network_profile_sha256']
                    and all(credential[k]==settings[k] for k in ('host_id','server_epoch')),'ratings_restored_binding_requires_review')
            return Cache(self.root).download(self.network.transport(profile),credential['credential'],
                                            server_epoch=settings['server_epoch'])

    def review_canonical_restore(self,*,expected_profile_sha256,expected_new_epoch,acknowledge_quarantined_backlog):
        """Internal stopped explicit review; no old intent crosses epochs blindly."""
        require(acknowledge_quarantined_backlog is True,'explicit_epoch_restore_review_required')
        identifier(expected_new_epoch)
        path=contained(self.root,'state/ratings-epoch-review.json')
        with file_lease(self.lock_path),file_lease(self.network.lock_path):
            old=self.profile(allow_review=True);profile=self.network.profile();credential=self.network.credential()
            require(credential is not None and self.network.pending(profile) is None,'network_enrollment_required')
            require(credential['server_epoch']==expected_new_epoch and fingerprint(profile)==old['network_profile_sha256'],
                    'ratings_restored_binding_requires_review')
            self.network.check_account(profile['account_id'])
            from .epoch_review import record as review_record
            record=review_record(self.root,self.marker)
            if record is not None and record.get('phase')=='complete' and record.get('expected_profile_sha256')==expected_profile_sha256:
                fields(record,('format','installation_id','generation','phase','old','new_epoch','host_id','expected_profile_sha256','ledger_backup'))
                expected={**record['old'],'server_epoch':expected_new_epoch,'host_id':credential['host_id']}
                require(record['new_epoch']==expected_new_epoch and record['host_id']==credential['host_id']
                        and all(record[k]==self.marker[k] for k in ('installation_id','generation'))
                        and fingerprint(old)==fingerprint(expected),'ratings_epoch_review_requires_review')
                return {'ok':True,'epoch_reviewed':True,'already_reviewed':True,'old_payloads_rewritten':False,'provider_dispatch':False}
            if record is None or record['phase']=='complete':
                require(fingerprint(old)==expected_profile_sha256 and old['server_epoch']!=expected_new_epoch,
                        'explicit_epoch_restore_review_required')
                record={'format':1,**{k:self.marker[k] for k in ('installation_id','generation')},
                        'phase':'prepared','old':old,'new_epoch':expected_new_epoch,'host_id':credential['host_id'],
                        'expected_profile_sha256':expected_profile_sha256,'ledger_backup':None}
                atomic_json(path,record)
            fields(record,('format','installation_id','generation','phase','old','new_epoch','host_id','expected_profile_sha256','ledger_backup'))
            require(type(record['format']) is int and record['format']==1 and record['phase']=='prepared'
                    and all(record[k]==self.marker[k] for k in ('installation_id','generation'))
                    and record['new_epoch']==expected_new_epoch and record['host_id']==credential['host_id']
                    and record['expected_profile_sha256']==expected_profile_sha256
                    and fingerprint(record['old'])==expected_profile_sha256,'ratings_epoch_review_requires_review')
            reply=self.network.transport(profile).request('POST','/v1/ratings/stream-state',token=credential['credential'],
                    body={'protocol_version':1,'previous_host_id':record['old']['host_id']})
            fields(reply,('ok','protocol_version','host_id','server_epoch','installation_id','generation','last_sequence'))
            require(reply['ok'] is True and type(reply['protocol_version']) is int and reply['protocol_version']==1
                    and all(reply[k]==self.marker[k] for k in ('installation_id','generation'))
                    and all(reply[k]==credential[k] for k in ('host_id','server_epoch')),'invalid_rating_stream_reply')
            high=integer(reply['last_sequence'],0)
            from .migration import snapshot_ledger,_private
            ledger_backup=contained(self.root,'backups/ratings-before-epoch-'+record['old']['server_epoch']+'.sqlite')
            if not os.path.lexists(ledger_backup):
                def remember(metadata):
                    record['ledger_backup']=metadata;atomic_json(path,record)
                snapshot_ledger(contained(self.root,LEDGER),ledger_backup,before_publish=remember)
            _private(ledger_backup)
            with ledger_backup.open('rb') as stream:backup_digest=hashlib.file_digest(stream,'sha256').hexdigest()
            require(record['ledger_backup']=={'sha256':backup_digest,'bytes':ledger_backup.stat().st_size},
                    'ratings_epoch_backup_requires_review')
            # Retain the old trusted pointer as a private recovery input before
            # clearing epoch-bound cache activation. Existing bundles stay intact.
            from .snapshots import Cache
            cache=Cache(self.root)
            archive=contained(self.root,'backups/ratings-epoch-'+record['old']['server_epoch']+'.json')
            if not os.path.lexists(archive):
                pointer=cache.pointer_value() if os.path.lexists(cache.pointer) else None
                atomic_json(archive,{'format':1,'old_profile':record['old'],'snapshot_pointer':pointer,
                                     'snapshot_policy_floor':cache.floor_value()})
            conn=self._conn()
            try:
                conn.execute('BEGIN EXCLUSIVE')
                high=max(high,validate(conn)[3],conn.execute('SELECT COALESCE(MAX(sequence),0) FROM rating_outbox').fetchone()[0])
                conn.execute('UPDATE rating_sync_meta SET sequence=? WHERE singleton=1',(high,))
                conn.execute('DELETE FROM rating_revision_cache')
                ack=canonical({'local_epoch_review':True,'old_server_epoch':record['old']['server_epoch'],
                               'aggregate_inclusion_unknown':True}).decode()
                conn.execute("UPDATE rating_outbox SET phase='quarantined',acknowledgment=? WHERE phase IN ('pending','retryable')",(ack,))
                conn.commit()
            except BaseException:conn.rollback();raise
            finally:conn.close()
            cache.pointer.unlink(missing_ok=True);cache.recovery_pointer.unlink(missing_ok=True)
            cache.policy_floor.unlink(missing_ok=True);self.cursor_path.unlink(missing_ok=True)
            reader_path=contained(self.root,'config/ratings-reader.json')
            if os.path.lexists(reader_path):
                from .reader import profile as reader_profile
                value=reader_profile(self.root)
                if value['mode']=='network':atomic_json(reader_path,{**value,'server_epoch':expected_new_epoch})
            atomic_json(self.profile_path,{**record['old'],'host_id':credential['host_id'],'server_epoch':expected_new_epoch})
            atomic_json(path,{**record,'phase':'complete'})
        return {'ok':True,'epoch_reviewed':True,'old_backlog_quarantined':True,'old_payloads_rewritten':False,
                'sequence':high,'provider_dispatch':False}
