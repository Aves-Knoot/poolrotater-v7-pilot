"""Enrolled host booking exchange; independent of the public browser listener."""
import json
import time
from ..diagnostic_logs.common import private_database,transaction
from ..network.contracts import fields,identifier,integer,number,require,verifier,canonical,fingerprint
from .contracts import PATH,BINDING
from .repository import Repository as Ledger

class Repository:
    def __init__(self,store):
        self.store=store;self.path=private_database(store.root,'bookings-hosts.sqlite3')
        with transaction(self.path)as db:
            db.execute('CREATE TABLE IF NOT EXISTS reports(host TEXT PRIMARY KEY,sequence INTEGER NOT NULL,pin TEXT NOT NULL,payload TEXT NOT NULL,received REAL NOT NULL,response TEXT NOT NULL)')
        from .eligibility import Eligibility
        self.ledger=Ledger(store.root,clock=store.clock,eligibility=Eligibility(store,clock=store.clock))

    def accept(self,token,value):
        fields(value,('protocol_version',*BINDING,'sequence','sent_at','capabilities','observations','receipts'))
        integer(value['protocol_version'],1,1);integer(value['sequence'],0,2**53-1);number(value['sent_at'])
        for key in BINDING:identifier(value[key])
        caps=value['capabilities'];fields(caps,('version','enabled_aliases','supported_presets','active_booking_ids'))
        integer(caps['version'],1,1)
        require(type(caps['enabled_aliases'])is list and len(caps['enabled_aliases'])<=4 and len(set(caps['enabled_aliases']))==len(caps['enabled_aliases'])and all(a in('room1','room2','room3','room4')for a in caps['enabled_aliases']),'booking_invalid_capabilities')
        from ..config import PRESETS
        require(type(caps['supported_presets'])is list and len(caps['supported_presets'])<=len(PRESETS)and all(p in PRESETS for p in caps['supported_presets']),'booking_invalid_capabilities')
        require(type(caps['active_booking_ids'])is list and len(caps['active_booking_ids'])<=4,'booking_invalid_capabilities')
        for item in caps['active_booking_ids']:identifier(item)
        require(type(value['observations'])is list and len(value['observations'])<=8 and type(value['receipts'])is list and len(value['receipts'])<=32,'booking_invalid_observations')
        now=self.store.clock()
        # Finish authentication before touching the independent booking ledger;
        # never hold the global authority transaction during pool/booking work.
        with self.store.transaction()as db:
            host,_=self.store._authenticate(db,verifier(token),'heartbeat')
            require(all(value[k]==host[k if k!='host_id'else'id']for k in BINDING),'host_identity_mismatch',403)
        pin=fingerprint(value)
        with transaction(self.path)as db:
            old=db.execute('SELECT * FROM reports WHERE host=?',(value['host_id'],)).fetchone()
            if old:
                require(value['sequence']>=old['sequence'],'booking_stale_exchange',409)
                if value['sequence']==old['sequence']:
                    require(old['pin']==pin,'booking_exchange_conflict',409);return json.loads(old['response'])
        require(abs(now-value['sent_at'])<=300,'booking_exchange_expired',409)
        seen=set()
        for item in value['observations']:
            fields(item,('booking_id','observation'));identifier(item['booking_id'])
            require(item['booking_id']not in seen,'booking_invalid_observations');seen.add(item['booking_id'])
            require(all(item['observation'].get(k)==value[k]for k in('installation_id','generation')),'host_identity_mismatch',403)
            self.ledger.observe(value['host_id'],item['booking_id'],item['observation'])
        self.ledger.observe_receipts(value['host_id'],value['receipts'])
        leases=self.ledger.leases(value['host_id'],value['installation_id'],value['generation'])
        commands=self.ledger.pending(value['host_id'])
        response={'ok':True,'sequence':value['sequence'],'leases':leases,'commands':[]}
        for command in commands:
            candidate={**response,'commands':[*response['commands'],command]}
            if len(canonical(candidate))>65536:break
            response=candidate
        require(len(canonical(response))<=65536,'booking_exchange_too_large',503)
        with transaction(self.path)as db:
            old=db.execute('SELECT sequence,pin,response FROM reports WHERE host=?',(value['host_id'],)).fetchone()
            if old and old['sequence']>=value['sequence']:
                require(old['sequence']==value['sequence']and old['pin']==pin,'booking_exchange_conflict',409)
                return json.loads(old['response'])
            db.execute('INSERT OR REPLACE INTO reports VALUES(?,?,?,?,?,?)',(value['host_id'],value['sequence'],pin,canonical(value).decode(),now,canonical(response).decode()))
        return response
