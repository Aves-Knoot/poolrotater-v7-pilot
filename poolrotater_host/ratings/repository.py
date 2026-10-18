"""Private canonical feedback in the dedicated hub SQLite, never a bot ledger.

Schema installation and contribution grants are explicit local administration.
Constructors/HTTP handlers validate existing schema; they never migrate it.
"""
import json

from ..network.contracts import (require, identifier, verifier, fingerprint,
                                 canonical, NetworkError, SCOPES, fields, base)
from .contracts import event, batch, query
from .conflict import decide

TABLES = {'ratings_policy','ratings_events','ratings_current','ratings_sources'}
SCHEMA = (
    'CREATE TABLE ratings_policy(host_id TEXT PRIMARY KEY REFERENCES hosts(id), '
    "mode TEXT NOT NULL CHECK(mode IN ('observe','contribute','disabled')), last_sequence INTEGER NOT NULL DEFAULT 0)",
    'CREATE TABLE ratings_events(event_id TEXT PRIMARY KEY,host_id TEXT NOT NULL REFERENCES hosts(id), '
    'installation_id TEXT,generation TEXT,sequence INTEGER,beatmap_id INTEGER,user_id INTEGER, '
    'payload_hash TEXT NOT NULL,payload TEXT NOT NULL,phase TEXT NOT NULL,reason TEXT NOT NULL, '
    'revision INTEGER NOT NULL,watermark INTEGER NOT NULL,received REAL NOT NULL, '
    'UNIQUE(installation_id,generation,sequence))',
    'CREATE INDEX ratings_event_host_time ON ratings_events(host_id,received)',
    'CREATE INDEX ratings_event_user_time ON ratings_events(user_id,received)',
    'CREATE TABLE ratings_current(beatmap_id INTEGER NOT NULL,user_id INTEGER NOT NULL,rating INTEGER, '
    'revision INTEGER NOT NULL,event_id TEXT,host_id TEXT,suppressed INTEGER NOT NULL DEFAULT 0, '
    'PRIMARY KEY(beatmap_id,user_id))',
    'CREATE TABLE ratings_sources(host_id TEXT NOT NULL,beatmap_id INTEGER NOT NULL,user_id INTEGER NOT NULL, '
    'rating INTEGER NOT NULL,event_id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL, '
    'PRIMARY KEY(host_id,beatmap_id,user_id))',
)


def prepare(store):
    """Internal primitive; local administrator holds the hub-server lease."""
    with store.transaction() as db:
        previous = db.execute("SELECT value FROM meta WHERE key='ratings_format'").fetchone()
        if previous is not None:
            require(previous[0] == '1', 'ratings_schema_refused')
            _validate(db)
            from .policy import Policy
            Policy(store).initialize(db);return False
        for statement in SCHEMA: db.execute(statement)
        db.execute("INSERT INTO meta VALUES('ratings_format','1')")
        db.execute("INSERT INTO meta VALUES('ratings_watermark','0')")
        from .policy import Policy
        Policy(store).initialize(db)
        store._audit(db, 'ratings_schema_prepared')
    return True


def _validate(db):
    row = db.execute("SELECT value FROM meta WHERE key='ratings_format'").fetchone()
    require(row is not None and row[0] == '1', 'ratings_migration_required', 503)
    for statement in ('SELECT host_id,mode,last_sequence FROM ratings_policy LIMIT 0',
            'SELECT event_id,host_id,installation_id,generation,sequence,beatmap_id,user_id,payload_hash,payload,phase,reason,revision,watermark,received FROM ratings_events LIMIT 0',
            'SELECT beatmap_id,user_id,rating,revision,event_id,host_id,suppressed FROM ratings_current LIMIT 0',
            'SELECT host_id,beatmap_id,user_id,rating,event_id,revision,payload FROM ratings_sources LIMIT 0'):
        db.execute(statement)
    row = db.execute("SELECT value FROM meta WHERE key='ratings_watermark'").fetchone()
    require(row is not None and row[0].isdigit() and 0 <= int(row[0]) < 2**53, 'ratings_schema_refused')


class Repository:
    def __init__(self, store):
        self.store = store
        from .policy import Policy
        self.policy=Policy(store)
        from .hub_migration import require_settled
        require_settled(store)
        with store.transaction() as db:_validate(db);self.policy.check(db)

    def grant(self, host_id, *, mode='observe'):
        """Explicit scopes/contribution policy; revocation never erases history."""
        identifier(host_id); require(mode in ('observe','contribute','disabled'), 'invalid_contribution_policy')
        with self.store.transaction() as db:
            self.policy.check(db)
            host = db.execute('SELECT * FROM hosts WHERE id=?', (host_id,)).fetchone()
            require(host is not None and not host['revoked'], 'host_revoked', 403)
            db.execute('INSERT INTO ratings_policy(host_id,mode) VALUES(?,?) ON CONFLICT(host_id) DO UPDATE SET mode=excluded.mode', (host_id,mode))
            for credential in db.execute('SELECT verifier,scopes FROM credentials WHERE host_id=? AND retired=0', (host_id,)).fetchall():
                scopes = set(json.loads(credential['scopes']))
                require(scopes <= set(SCOPES)|{'ratings-submit','ratings-query','ratings-seed','ratings-read'}, 'scope_refused', 403)
                scopes -= {'ratings-submit','ratings-query','ratings-seed','ratings-read'}
                if mode != 'disabled': scopes.update(('ratings-submit','ratings-read'))
                if mode == 'contribute': scopes.add('ratings-query')
                db.execute('UPDATE credentials SET scopes=? WHERE verifier=?', (json.dumps(sorted(scopes)),credential['verifier']))
            self.store._audit(db, 'ratings_policy_'+mode, host_id)
        return {'ok': True, 'contribution_policy': mode, 'historical_contributions_erased': False}

    def grant_seed(self,host_id,*,allow):
        identifier(host_id);require(type(allow) is bool,'invalid_seed_grant')
        with self.store.transaction() as db:
            host=db.execute('SELECT revoked FROM hosts WHERE id=?',(host_id,)).fetchone()
            policy=db.execute('SELECT mode FROM ratings_policy WHERE host_id=?',(host_id,)).fetchone()
            require(host is not None and not host[0] and policy is not None and policy[0]=='contribute',
                    'ratings_policy_refused',403)
            for row in db.execute('SELECT verifier,scopes FROM credentials WHERE host_id=? AND retired=0',(host_id,)).fetchall():
                scopes=set(json.loads(row['scopes']));scopes.discard('ratings-seed')
                if allow:scopes.add('ratings-seed')
                db.execute('UPDATE credentials SET scopes=? WHERE verifier=?',(canonical(sorted(scopes)).decode(),row['verifier']))
            self.store._audit(db,'ratings_seed_granted' if allow else 'ratings_seed_removed',host_id)
        return {'ok':True,'seed_allowed':allow}

    def _authorized(self, db, token, scope):
        self.policy.check(db)
        host, _ = self.store._authenticate(db, verifier(token), scope)
        policy = db.execute('SELECT * FROM ratings_policy WHERE host_id=?', (host['id'],)).fetchone()
        require(policy is not None and policy['mode'] != 'disabled', 'ratings_policy_refused', 403)
        return host, policy

    def _lineage(self,db,host_id):
        row=db.execute('SELECT value FROM meta WHERE key=?',('ratings_lineage_'+host_id,)).fetchone()
        ancestors=json.loads(row[0]) if row else []
        require(type(ancestors) is list and len(ancestors)<=100 and len(set(ancestors))==len(ancestors)
                and host_id not in ancestors,'ratings_lineage_requires_review',409)
        for item in ancestors:identifier(item)
        return [host_id,*ancestors]

    def link_replacement(self,host_id,previous_host_id):
        """Root-local review only, never implicit in enrollment or a remote request."""
        identifier(host_id);identifier(previous_host_id)
        require(host_id!=previous_host_id,'ratings_lineage_refused',409)
        with self.store.transaction() as db:
            new=db.execute('SELECT * FROM hosts WHERE id=?',(host_id,)).fetchone()
            old=db.execute('SELECT * FROM hosts WHERE id=?',(previous_host_id,)).fetchone()
            require(new is not None and old is not None and not new['revoked'] and old['revoked']
                    and all(new[k]==old[k] for k in ('operator_id','account_id','installation_id','generation')),
                    'ratings_lineage_refused',409)
            ancestors=self._lineage(db,previous_host_id)
            require(len(ancestors)<=100 and host_id not in ancestors,'ratings_lineage_refused',409)
            prior=db.execute('SELECT value FROM meta WHERE key=?',('ratings_lineage_'+host_id,)).fetchone()
            require(prior is None or json.loads(prior[0])==ancestors,'ratings_lineage_refused',409)
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',('ratings_lineage_'+host_id,canonical(ancestors).decode()))
            self.store._audit(db,'ratings_replacement_linked',host_id)
        return {'ok':True,'replacement_linked':True,'old_events_rewritten':False}

    def stream_state(self,token,value):
        fields(value,('protocol_version','previous_host_id'));base(value);identifier(value['previous_host_id'])
        with self.store.transaction() as db:
            host,policy=self._authorized(db,token,'ratings-query')
            require(policy['mode']=='contribute' and value['previous_host_id'] in self._lineage(db,host['id']),
                    'ratings_lineage_refused',409)
            high=db.execute('SELECT MAX(p.last_sequence) FROM ratings_policy p JOIN hosts h ON h.id=p.host_id '
                            'WHERE h.installation_id=? AND h.generation=?',
                            (host['installation_id'],host['generation'])).fetchone()[0] or 0
            epoch=db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]
            self.store._audit(db,'ratings_stream_query',host['id'])
        return {'ok':True,'protocol_version':1,'host_id':host['id'],'server_epoch':epoch,
                'installation_id':host['installation_id'],'generation':host['generation'],'last_sequence':high}

    def _watermark(self, db):
        current = int(db.execute("SELECT value FROM meta WHERE key='ratings_watermark'").fetchone()[0])
        require(current < 2**53-1, 'ratings_revision_exhausted', 503)
        current += 1
        db.execute("UPDATE meta SET value=? WHERE key='ratings_watermark'", (str(current),))
        return current

    def ingest(self, token, value):
        batch(value)
        with self.store.transaction() as db:
            host, policy = self._authorized(db, token, 'ratings-submit')
            credential=db.execute('SELECT scopes FROM credentials WHERE verifier=?',(verifier(token),)).fetchone()
            rules,_=self.policy.check(db)
            policy={**dict(policy),'seed_allowed':'ratings-seed' in json.loads(credential[0]),'rules':rules}
            outcomes = []
            for index, item in enumerate(value['events']):
                result = self._event(db, host, policy, item)
                outcomes.append({'index': index, **result})
            watermark = int(db.execute("SELECT value FROM meta WHERE key='ratings_watermark'").fetchone()[0])
            self.store._audit(db, 'ratings_batch', host['id'], value['batch_id'])
        return {'ok': True, 'protocol_version': 1, 'outcomes': outcomes, 'processing_watermark': watermark,
                'aggregate_inclusion_confirmed': False}

    def _event(self, db, host, policy, item):
        try:
            # Even malformed events need stable identities to persist an outcome.
            require(type(item) is dict, 'invalid_rating_event'); identifier(item.get('event_id'))
        except NetworkError:
            return {'event_id': None, 'phase': 'invalid', 'reason': 'invalid_event_identity', 'revision': 0, 'watermark': 0}
        event_id = item['event_id']; digest = fingerprint(item)
        previous = db.execute('SELECT * FROM ratings_events WHERE event_id=?', (event_id,)).fetchone()
        if previous is not None:
            if previous['host_id'] not in self._lineage(db,host['id']) or previous['payload_hash'] != digest:
                self.store._audit(db, 'rating_event_id_conflict', host['id'])
                return {'event_id': event_id, 'phase': 'permanently_rejected','reason':'event_id_conflict','revision':0,'watermark':0}
            return {'event_id':event_id,'phase':'already_accepted' if previous['phase']=='accepted' else previous['phase'],
                    'reason':previous['reason'],'revision':previous['revision'],'watermark':previous['watermark']}
        valid = True
        try: event(item)
        except NetworkError: valid = False
        if not valid:
            # Never retain accidental arbitrary private fields in malformed input.
            return self._record(db, host, item, digest, 'invalid', 'invalid_event', 0, valid=False)
        if any(item[k] != host[k] for k in ('installation_id','generation')):
            return self._record(db, host, item, digest, 'permanently_rejected', 'host_identity_mismatch', 0, valid=False)
        collision = db.execute('SELECT event_id FROM ratings_events WHERE installation_id=? AND generation=? AND sequence=?',
                               (item['installation_id'],item['generation'],item['sequence'])).fetchone()
        if collision:
            return self._record(db, host, item, digest, 'permanently_rejected', 'sequence_reused', 0, valid=False)
        # A missing old sequence after restore is not proof of new player intent.
        last_sequence=db.execute('SELECT MAX(p.last_sequence) FROM ratings_policy p JOIN hosts h ON h.id=p.host_id '
                                 'WHERE h.installation_id=? AND h.generation=?',
                                 (host['installation_id'],host['generation'])).fetchone()[0] or 0
        if item['sequence'] <= last_sequence:
            return self._record(db, host, item, digest, 'permanently_rejected', 'restored_sequence', 0)
        db.execute('UPDATE ratings_policy SET last_sequence=? WHERE host_id=?', (item['sequence'],host['id']))
        state = db.execute('SELECT * FROM ratings_current WHERE beatmap_id=? AND user_id=?',
                           (item['beatmap_id'],item['user_id'])).fetchone()
        state=self.policy.effective_state(policy['rules'],state,bid=item['beatmap_id'],uid=item['user_id'],host=host['id'])
        revision = state['revision'] if state else 0
        exclusion=self.policy.excluded(policy['rules'],bid=item['beatmap_id'],uid=item['user_id'],host=host['id'])
        if exclusion is not None:
            return self._record(db,host,item,digest,'permanently_rejected','policy_excluded',revision,
                                valid=exclusion['effect']!='delete')
        if policy['mode'] == 'observe':
            return self._record(db, host, item, digest, 'quarantined','contribution_observe_only',revision)
        if item['kind']=='historical_seed' and not policy['seed_allowed']:
            return self._record(db,host,item,digest,'quarantined','historical_seed_not_authorized',revision)
        now = self.store.clock()
        player = db.execute('SELECT COUNT(*) FROM ratings_events WHERE user_id=? AND received>=?', (item['user_id'],now-86400)).fetchone()[0]
        # Host transport throughput is CPU-bounded, not a voting quarantine.
        # Per-player abuse limits and signed contribution policy still apply.
        if player >= 200:
            return self._record(db, host, item, digest, 'quarantined','player_quota',revision)
        predecessor = None
        if item['predecessor'] is not None:
            previous = db.execute('SELECT * FROM ratings_events WHERE event_id=?', (item['predecessor'],)).fetchone()
            if previous and previous['host_id'] in self._lineage(db,host['id']) and previous['phase'] == 'accepted':
                predecessor = {**json.loads(previous['payload']), 'phase': previous['phase'], 'revision': previous['revision']}
        phase, reason = decide(dict(state) if state else None, item, predecessor)
        return self._record(db, host, item, digest, phase, reason, revision)

    def _record(self, db, host, item, digest, phase, reason, revision, *, valid=True):
        watermark = self._watermark(db)
        payload = canonical(item).decode() if valid else '{}'
        if phase == 'accepted':
            revision = watermark
            db.execute('INSERT INTO ratings_current(beatmap_id,user_id,rating,revision,event_id,host_id) VALUES(?,?,?,?,?,?) '
                       'ON CONFLICT(beatmap_id,user_id) DO UPDATE SET rating=excluded.rating,revision=excluded.revision,event_id=excluded.event_id,host_id=excluded.host_id',
                       (item['beatmap_id'],item['user_id'],item['rating'],revision,item['event_id'],host['id']))
            db.execute('INSERT INTO ratings_sources VALUES(?,?,?,?,?,?,?) ON CONFLICT(host_id,beatmap_id,user_id) DO UPDATE SET '
                       'rating=excluded.rating,event_id=excluded.event_id,revision=excluded.revision,payload=excluded.payload',
                       (host['id'],item['beatmap_id'],item['user_id'],item['rating'],item['event_id'],revision,payload))
        db.execute('INSERT INTO ratings_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (item['event_id'],host['id'],item['installation_id'] if valid else None,item['generation'] if valid else None,
                    item['sequence'] if valid else None,item['beatmap_id'] if valid else None,item['user_id'] if valid else None,
                    digest,payload,phase,reason,revision,watermark,self.store.clock()))
        return {'event_id':item['event_id'],'phase':phase,'reason':reason,'revision':revision,'watermark':watermark}

    def state_query(self, token, value):
        query(value)
        with self.store.transaction() as db:
            host, policy = self._authorized(db, token, 'ratings-query')
            require(policy['mode'] == 'contribute', 'ratings_policy_refused', 403)
            results = []
            rules,_=self.policy.check(db)
            for event_id in value['event_ids']:
                handled = db.execute('SELECT beatmap_id,user_id,host_id FROM ratings_events WHERE event_id=?', (event_id,)).fetchone()
                if handled is None or handled['host_id'] not in self._lineage(db,host['id']) or handled['beatmap_id'] is None:
                    results.append({'event_id':event_id,'known':False}); continue
                state = db.execute('SELECT rating,revision,suppressed FROM ratings_current WHERE beatmap_id=? AND user_id=?', (handled['beatmap_id'],handled['user_id'])).fetchone()
                state=self.policy.effective_state(rules,state,bid=handled['beatmap_id'],uid=handled['user_id'],host=host['id'])
                results.append({'event_id':event_id,'known':True,'revision':state['revision'] if state else 0,
                                'rating':state['rating'] if state else None,'suppressed':bool(state['suppressed']) if state else False})
            self.store._audit(db, 'ratings_state_query', host['id'])
        return {'ok':True,'protocol_version':1,'states':results}

    def diagnostics(self):
        with self.store.transaction() as db:
            counts = dict(db.execute('SELECT phase,COUNT(*) FROM ratings_events GROUP BY phase'))
            sources = db.execute('SELECT COUNT(*) FROM ratings_sources').fetchone()[0]
            watermark = int(db.execute("SELECT value FROM meta WHERE key='ratings_watermark'").fetchone()[0])
        return {'ok':True,'events':counts,'source_candidates':sources,'processing_watermark':watermark,'provider_access':False}
