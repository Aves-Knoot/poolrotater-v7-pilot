"""Owner-controlled persistent TUF publication of prepared selection catalogs.

Private signing custody is permanently separate from the served directory. This
publisher never collects/analyzes maps, reads OAuth, or changes client trust.
The existing TUF/catalog validation contracts remain authoritative.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid

from .catalog_export import MODES, CORE_MODES, complete_modes, MAX_FILE, inspect_bundle, canonical, sha256
from .catalog_trust import BootstrapTrust, bounded_file, dependencies, no_links, ROOT_LIMIT, METADATA_LIMIT
from .catalog_exclusions import validate_feed, apply_feed, initial_state
from .config import ConfigError, fields
from .layout import atomic_json, checked_root, file_lease, read_json

ROLES=('root','targets','snapshot','timestamp')
EXPIRY_DAYS={'root':365,'targets':90,'snapshot':30,'timestamp':7}
MAX_VERSION=2**31-1


def require(condition,code):
    if not condition:raise ConfigError(code)


def private(path, *, directory=False, limit=1024*1024):
    path=no_links(path);info=path.lstat()
    require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            and info.st_uid==os.geteuid() and not stat.S_IMODE(info.st_mode)&0o077
            and (directory or (info.st_nlink==1 and info.st_size<=limit)), 'private_signer_custody_required')
    return path


def _public_root(value):
    path=checked_root(Path(value));info=path.stat()
    require(stat.S_ISDIR(info.st_mode) and not stat.S_IMODE(info.st_mode)&0o022,'protected_publication_root_required')
    return path


def _public_existing(path):
    path=no_links(path);info=path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid==os.geteuid() and info.st_nlink==1
            and not stat.S_IMODE(info.st_mode)&0o022,'unsafe_publication_file')


def _write(path,data,*,replace=False,mode=0o644):
    """Durable exclusive immutable publication, or explicit timestamp replacement."""
    path=no_links(path);temporary=None
    if os.path.lexists(path):
        _public_existing(path)
    if not replace and os.path.lexists(path):
        with bounded_file(path,max(len(data),1)) as handle:require(handle.read()==data,'immutable_publication_changed')
        return
    try:
        fd,name=tempfile.mkstemp(prefix='.catalog-publish-',dir=path.parent);temporary=Path(name)
        with os.fdopen(fd,'wb') as stream:
            os.fchmod(stream.fileno(),mode);stream.write(data);stream.flush();os.fsync(stream.fileno())
        if replace:os.replace(temporary,path)
        else:os.link(temporary,path)
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def _copy_target(destination,source,expected,*,mode):
    destination=no_links(destination)
    if os.path.lexists(destination):
        _public_existing(destination)
        with bounded_file(destination,MAX_FILE) as handle:
            require(os.fstat(handle.fileno()).st_size==expected['bytes'] and sha256(destination)==expected['sha256'],
                    'immutable_publication_changed')
        return
    temporary=None
    try:
        fd,name=tempfile.mkstemp(prefix='.catalog-publish-',dir=destination.parent);temporary=Path(name)
        digest=hashlib.sha256();count=0
        with os.fdopen(fd,'wb') as outgoing,bounded_file(source,MAX_FILE) as incoming:
            os.fchmod(outgoing.fileno(),mode)
            while data:=incoming.read(1024*1024):
                count+=len(data);require(count<=expected['bytes'],'prepared_catalog_changed')
                digest.update(data);outgoing.write(data)
            require(count==expected['bytes'] and digest.hexdigest()==expected['sha256'],'prepared_catalog_changed')
            outgoing.flush();os.fsync(outgoing.fileno())
        os.link(temporary,destination)
        fd=os.open(destination.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def _wall_now():return datetime.now(timezone.utc)


def _time(now):
    require(isinstance(now,datetime) and now.tzinfo is not None,'aware_publication_time_required')
    return now.astimezone(timezone.utc).replace(microsecond=0)


def _extended(record):
    return 'catch' in record['content_ids'] or any('catch' in e['modes'] for e in record['exclusions']['events'])


def _legacy_feed(feed):
    # Old readers reject unknown mode names, even in an unrelated scope.
    events=[]
    for event in feed['events']:
        modes=[m for m in event['modes'] if m in CORE_MODES]
        if not modes:continue
        events.append({**event,'modes':modes,'memberships':
                       {m:event['memberships'][m] for m in modes} if event['memberships'] else {}})
    return validate_feed({**feed,'events':events})


def _scoped_feed(feed, modes):
    events=[]
    for event in feed['events']:
        selected=[m for m in event['modes'] if m in modes]
        if selected:events.append({**event,'modes':selected,'memberships':
            {m:event['memberships'][m] for m in selected} if event['memberships'] else {}})
    return validate_feed({**feed,'events':events})


def _default_feed():
    return {'format':1,'revision':1,'events':[{'kind':'beatmap','id':1549496,'modes':list(CORE_MODES),
            'action':'exclude','reason':'missing_audio','memberships':{}}]}


def _cumulative(previous,incoming):
    validate_feed(incoming)
    items={}
    for feed in (previous,incoming):
        if feed is None:continue
        for event in feed['events']:
            for mode in event['modes']:
                items[(mode,event['kind'],event['id'])]={**event,'modes':[mode],
                    'memberships':{mode:event['memberships'][mode]} if event['memberships'] else {}}
    result={'format':1,'revision':incoming['revision'],'events':[items[key] for key in sorted(items)]}
    validate_feed(result)
    return result


def initialize(custody,served,*,now=None):
    """Fresh authority only. No implicit rotation/adoption of an old pilot root."""
    dependencies()
    custody=checked_root(Path(custody));served=checked_root(Path(served))
    require(custody!=served and not custody.is_relative_to(served) and not served.is_relative_to(custody),
            'signer_and_publication_roots_must_be_separate')
    require(not os.path.lexists(custody) and not os.path.lexists(served),'fresh_catalog_authority_required')
    parent=private(custody.parent,directory=True)
    require(served.parent.is_dir(),'publication_parent_required')
    now=_time(now or datetime.now(timezone.utc))
    from securesystemslib.signer import CryptoSigner
    from tuf.api.metadata import Metadata, Root
    stage=Path(tempfile.mkdtemp(prefix='.catalog-authority-',dir=parent));public_created=False
    try:
        (stage/'keys').mkdir(mode=0o700)
        root=Metadata(Root(version=1,expires=now+timedelta(days=EXPIRY_DAYS['root']),consistent_snapshot=True))
        root_signers=[]
        key_records={}
        for role in ROLES:
            signers=[CryptoSigner.generate_ed25519() for _ in range(2 if role=='root' else 1)]
            key_records[role]=[]
            for index,signer in enumerate(signers):
                name=f'{role}-{index}.pem';_write(stage/'keys'/name,signer.private_bytes,mode=0o600)
                key_records[role].append({'file':name,'keyid':signer.public_key.keyid})
                root.signed.add_key(signer.public_key,role)
            root.signed.roles[role].threshold=len(signers)
            if role=='root':root_signers=signers
        for index,signer in enumerate(root_signers):root.sign(signer,append=index>0)
        bootstrap=root.to_bytes();_write(stage/'bootstrap.json',bootstrap,mode=0o600)
        atomic_json(stage/'state.json',{'format':1,'purpose':'poolrotater-catalog-signers','repository_id':str(uuid.uuid4()),
            'served_root':str(served),'bootstrap_sha256':hashlib.sha256(bootstrap).hexdigest(),'keys':key_records,
            'version':0,'reserved_version':0,'renewal':None,'clock_high_water':now.isoformat(),'content_ids':{},'exclusions':None,'pending':None})
        served.mkdir(mode=0o755);served.chmod(0o755);public_created=True
        for directory in ('metadata','targets'):
            (served/directory).mkdir(mode=0o755);(served/directory).chmod(0o755)
        _write(served/'metadata/1.root.json',bootstrap)
        os.rename(stage,custody)
        fd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        return {'ok':True,'new_authority':True,'bootstrap_sha256':hashlib.sha256(bootstrap).hexdigest(),
                'root_threshold':2,'catalogs_published':False,'client_trust_changed':False}
    finally:
        if stage.exists():
            shutil.rmtree(stage)
            if public_created:shutil.rmtree(served)


class Publisher:
    def __init__(self,custody):
        dependencies();self.root=private(custody,directory=True)
        private(self.root/'state.json');self.state=read_json(self.root/'state.json',maximum=256*1024)
        original={'format','purpose','repository_id','served_root','bootstrap_sha256','keys','version',
                  'clock_high_water','content_ids','exclusions','pending'}
        require(set(self.state) in (original,original|{'reserved_version','renewal'}),'catalog_authority_refused')
        if set(self.state)==original:
            # Prepared foundation custody upgrades without key/trust replacement.
            self.state['reserved_version']=max(self.state['version'],self.state['pending']['version'] if self.state['pending'] else 0)
            self.state['renewal']=None
        require(type(self.state['format']) is int and self.state['format']==1
                and self.state['purpose']=='poolrotater-catalog-signers','catalog_authority_refused')
        require(str(uuid.UUID(self.state['repository_id']))==self.state['repository_id'],'catalog_authority_refused')
        require(type(self.state['version']) is int and type(self.state['reserved_version']) is int
                and 0<=self.state['version']<=self.state['reserved_version']<MAX_VERSION,'catalog_authority_refused')
        require(type(self.state['bootstrap_sha256']) is str and re.fullmatch('[a-f0-9]{64}',self.state['bootstrap_sha256']),
                'catalog_authority_refused')
        self.served=_public_root(self.state['served_root'])
        require(not self.root.is_relative_to(self.served) and not self.served.is_relative_to(self.root),
                'signer_and_publication_roots_must_be_separate')
        private(self.root/'keys',directory=True)
        private(self.root/'bootstrap.json',limit=ROOT_LIMIT)
        self.bootstrap=(self.root/'bootstrap.json').read_bytes()
        require(hashlib.sha256(self.bootstrap).hexdigest()==self.state['bootstrap_sha256'],'catalog_authority_refused')
        with bounded_file(self.served/'metadata/1.root.json',ROOT_LIMIT) as stream:
            require(stream.read()==self.bootstrap,'public_catalog_authority_changed')
        require(type(self.state['keys']) is dict and set(self.state['keys'])==set(ROLES),'catalog_authority_refused')
        require(type(self.state['content_ids']) is dict and (not self.state['content_ids'] if self.state['version']==0
                else complete_modes(self.state['content_ids']) and all(type(v) is str and re.fullmatch('[a-f0-9]{64}',v)
                     for v in self.state['content_ids'].values())),'catalog_authority_refused')
        _time(datetime.fromisoformat(self.state['clock_high_water']))
        if self.state['exclusions'] is not None:validate_feed(self.state['exclusions'])
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from securesystemslib.signer import CryptoSigner
        from tuf.api.metadata import Metadata
        trusted=Metadata.from_bytes(self.bootstrap)
        self.signers={}
        for role in ROLES:
            records=self.state['keys'][role]
            require(type(records) is list and len(records)==(2 if role=='root' else 1),'catalog_authority_refused')
            self.signers[role]=[]
            for index,record in enumerate(records):
                fields(record,('file','keyid'),'signer identity')
                require(record['file']==f'{role}-{index}.pem','catalog_authority_refused')
                path=private(self.root/'keys'/record['file'],limit=16384)
                key=load_pem_private_key(path.read_bytes(),password=None)
                require(isinstance(key,Ed25519PrivateKey),'catalog_authority_refused')
                signer=CryptoSigner(key)
                require(signer.public_key.keyid==record['keyid'] and record['keyid'] in trusted.signed.roles[role].keyids,
                        'catalog_authority_refused')
                self.signers[role].append(signer)
        trusted.verify_delegate('root',trusted)
        require(trusted.signed.consistent_snapshot and trusted.signed.roles['root'].threshold==2,'catalog_authority_refused')

    def _save(self):atomic_json(self.root/'state.json',self.state)

    def _reload(self):
        other=Publisher(self.root);self.__dict__.update(other.__dict__)

    def _verify_pending(self,record,pending):
        from tuf.api.metadata import Metadata
        version=record['version']
        root=Metadata.from_file(str(pending/f'metadata/{version+1}.root.json'))
        trusted=Metadata.from_bytes(self.bootstrap)
        trusted.verify_delegate('root',root);root.verify_delegate('root',root)
        require(root.signed.version==version+1 and root.signed.keys==trusted.signed.keys
                and root.signed.roles==trusted.signed.roles,'pending_catalog_authority_changed')
        objects={role:Metadata.from_file(str(pending/f'metadata/{version}.{role}.json'))
                 for role in ('targets','snapshot')}
        objects['timestamp']=Metadata.from_file(str(pending/'metadata/timestamp.json'))
        for role,metadata in objects.items():
            root.verify_delegate(role,metadata)
            require(metadata.signed.version==version,'pending_catalog_version_refused')
        target_raw=(pending/f'metadata/{version}.targets.json').read_bytes()
        snapshot_raw=(pending/f'metadata/{version}.snapshot.json').read_bytes()
        require(set(objects['snapshot'].signed.meta)=={'targets.json'}
                and objects['snapshot'].signed.meta['targets.json'].version==version
                and objects['timestamp'].signed.snapshot_meta.version==version,'pending_catalog_version_refused')
        for role,metadata in (('root',root),*objects.items()):
            require(metadata.signed.expires==datetime.fromisoformat(record['clock'])+timedelta(days=EXPIRY_DAYS[role]),
                    'pending_catalog_expiry_changed')
        objects['snapshot'].signed.meta['targets.json'].verify_length_and_hashes(target_raw)
        objects['timestamp'].signed.snapshot_meta.verify_length_and_hashes(snapshot_raw)
        expected={f'metadata/{version+1}.root.json',f'metadata/{version}.targets.json',
                  f'metadata/{version}.snapshot.json',f'metadata/{version}.timestamp.json','metadata/timestamp.json'}
        names={'exclusions.json'}
        if _extended(record):names.add('exclusions-v2.json')
        if 'loved' in record['content_ids'] or any('loved' in e['modes'] for e in record['exclusions']['events']):names.add('exclusions-loved.json')
        from .catalog_export import read_manifest
        for mode in record['content_ids']:
            info=objects['targets'].signed.targets[mode+'/manifest.json']
            manifest_path=pending/'targets'/mode/(info.hashes['sha256']+'.manifest.json')
            manifest=read_manifest(manifest_path)
            require(manifest['mode']==mode and manifest['content_id']==record['content_ids'][mode],
                    'pending_catalog_identity_mismatch')
            names.update(mode+'/'+name for name in ('manifest.json','bundle.tar.gz',*(row['name'] for row in manifest['files'])))
        require(set(objects['targets'].signed.targets)==names,'pending_catalog_target_scope_changed')
        feed_info=objects['targets'].signed.targets['exclusions.json']
        feed_raw=canonical(_legacy_feed(record['exclusions']))
        require(feed_info.length==len(feed_raw) and feed_info.hashes=={'sha256':hashlib.sha256(feed_raw).hexdigest()},
                'pending_catalog_exclusion_identity_changed')
        if _extended(record):
            extra=objects['targets'].signed.targets['exclusions-v2.json']
            raw=canonical(_scoped_feed(record['exclusions'],set(CORE_MODES)|{'catch'}))
            require(extra.length==len(raw) and extra.hashes=={'sha256':hashlib.sha256(raw).hexdigest()},
                    'pending_catalog_exclusion_identity_changed')
        if 'exclusions-loved.json' in names:
            extra=objects['targets'].signed.targets['exclusions-loved.json']
            raw=canonical(_scoped_feed(record['exclusions'],{'loved'}))
            require(extra.length==len(raw) and extra.hashes=={'sha256':hashlib.sha256(raw).hexdigest()},
                    'pending_catalog_exclusion_identity_changed')
        for name,info in objects['targets'].signed.targets.items():
            require(set(info.hashes)=={'sha256'},'pending_catalog_identity_mismatch')
            path='targets/'+str(Path(name).parent/(info.hashes['sha256']+'.'+Path(name).name))
            expected.add(path)
            require(record['files'][path]=={'bytes':info.length,'sha256':info.hashes['sha256']},
                    'pending_catalog_identity_mismatch')
        require(set(record['files'])==expected,'pending_catalog_target_scope_changed')
        require(record['files'][f'metadata/{version}.timestamp.json']==record['files']['metadata/timestamp.json'],
                'pending_catalog_identity_mismatch')

    def _discard_unjournaled(self):
        pending=self.root/'pending'
        if not os.path.lexists(pending):return False
        private(pending,directory=True)
        if not any(pending.iterdir()):
            pending.rmdir();return True
        receipt=read_json(pending/'.building.json')
        fields(receipt,('format','repository_id','previous_version'),'publication construction')
        require(receipt['format']==1 and receipt['repository_id']==self.state['repository_id'],
                'untracked_catalog_publication_refused')
        if receipt['previous_version']==self.state['reserved_version']:
            # No durable publication intent existed, so commit was never entered.
            shutil.rmtree(pending);return True
        require(receipt['previous_version']==self.state['reserved_version']-1,'untracked_catalog_publication_refused')
        committed=read_json(self.root/'receipts'/f"{self.state['version']}.json",maximum=256*1024)
        require(committed['content_ids']==self.state['content_ids']
                and sha256(pending/'metadata/timestamp.json')==committed['timestamp_sha256']
                and sha256(self.served/'metadata/timestamp.json')==committed['timestamp_sha256'],
                'untracked_catalog_publication_refused')
        shutil.rmtree(pending);return True

    def _cleanup_public_temps(self):
        for directory in ('metadata','targets'):
            base=no_links(self.served/directory)
            for path in base.rglob('.catalog-publish-*'):
                if re.fullmatch(r'\.catalog-publish-[A-Za-z0-9_]{8}',path.name):
                    _public_existing(path)
                    require(path.stat().st_size<=MAX_FILE,'unsafe_publication_file')
                    path.unlink()

    def _check_pending(self):
        record=self.state['pending'];require(type(record) is dict,'pending_catalog_publication_required')
        fields(record,('version','clock','content_ids','exclusions','files','timestamp_sha256'),'pending publication')
        version=record['version'];require(type(version) is int and self.state['version']<version==self.state['reserved_version'],'pending_catalog_version_refused')
        pending=private(self.root/'pending',directory=True)
        require(type(record['content_ids']) is dict and complete_modes(record['content_ids'])
                and all(type(v) is str and re.fullmatch('[a-f0-9]{64}',v) for v in record['content_ids'].values()),
                'pending_catalog_modes_refused')
        require(type(record['files']) is dict and 1<=len(record['files'])<=64,'pending_catalog_files_refused')
        require(_time(datetime.fromisoformat(record['clock']))>=datetime.fromisoformat(self.state['clock_high_water']),
                'catalog_publication_clock_rollback')
        validate_feed(record['exclusions'])
        for name,row in record['files'].items():
            fields(row,('bytes','sha256'),'pending file')
            require(type(name) is str and re.fullmatch(r'(metadata|targets)/[A-Za-z0-9_./-]+',name)
                    and all(p not in ('','.','..') for p in name.split('/')),'pending_catalog_path_refused')
            path=pending/name
            limit=ROOT_LIMIT if name.startswith('metadata/') and name.endswith('.root.json') else METADATA_LIMIT if name.startswith('metadata/') else MAX_FILE
            require(type(row['bytes']) is int and 0<row['bytes']<=limit and type(row['sha256']) is str
                    and re.fullmatch('[a-f0-9]{64}',row['sha256']),'pending_catalog_identity_mismatch')
            private(path,limit=limit)
            require(path.stat().st_size==row['bytes'] and sha256(path)==row['sha256'],'pending_catalog_identity_mismatch')
        require(record['files']['metadata/timestamp.json']['sha256']==record['timestamp_sha256'],'pending_catalog_identity_mismatch')
        self._verify_pending(record,pending)
        return record,pending

    def _commit(self):
        """Resume exactly signed pending bytes; timestamp is the visibility commit."""
        record,pending=self._check_pending();version=record['version']
        from tuf.api.metadata import Metadata
        candidate=Metadata.from_file(str(pending/'metadata/timestamp.json'))
        advertised=self.served/'metadata/timestamp.json'
        already_advertised=advertised.exists() and sha256(advertised)==record['timestamp_sha256']
        require(candidate.signed.expires>_wall_now() or already_advertised,'pending_catalog_metadata_expired')
        self._cleanup_public_temps()
        for name in sorted(record['files']):
            if name=='metadata/timestamp.json':continue
            path=self.served/name;path.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
            # Administrator umask077 must not make a new public mode unreadable
            # by the independent unprivileged HTTPS listener. Signing custody
            # and pending directories remain private. Validate before widening.
            _public_root(path.parent);path.parent.chmod(0o755)
            if name.startswith('targets/'):_copy_target(path,pending/name,record['files'][name],mode=0o644)
            else:_write(path,(pending/name).read_bytes())
        # An operator must not roll back the public timestamp behind custody.
        timestamp=self.served/'metadata/timestamp.json'
        from tuf.api.metadata import Metadata
        if not timestamp.exists() and self.state['version']:
            prior=read_json(self.root/'receipts'/f"{self.state['version']}.json",maximum=256*1024)
            retained=self.served/f"metadata/{self.state['version']}.timestamp.json"
            with bounded_file(retained,16384) as handle:retained_raw=handle.read()
            require(hashlib.sha256(retained_raw).hexdigest()==prior['timestamp_sha256'],'public_catalog_version_changed')
            _write(timestamp,retained_raw)
        if timestamp.exists():
            with bounded_file(timestamp,16384) as handle:observed=Metadata.from_bytes(handle.read())
            Metadata.from_bytes(self.bootstrap).verify_delegate('timestamp',observed)
            require(observed.signed.version in (self.state['version'],version),'public_catalog_version_changed')
            if observed.signed.version==self.state['version']:
                prior=read_json(self.root/'receipts'/f"{self.state['version']}.json",maximum=256*1024)
                require(sha256(timestamp)==prior['timestamp_sha256'],'public_catalog_version_changed')
            if observed.signed.version==version:
                require(sha256(timestamp)==record['timestamp_sha256'],'public_catalog_version_changed')
        _write(timestamp,(pending/'metadata/timestamp.json').read_bytes(),replace=True)
        receipts=self.root/'receipts';receipts.mkdir(mode=0o700,exist_ok=True)
        receipt={**record,'bootstrap_sha256':self.state['bootstrap_sha256']}
        atomic_json(receipts/f'{version}.json',receipt)
        self.state.update(version=version,clock_high_water=record['clock'],content_ids=record['content_ids'],
                          exclusions=record['exclusions'],pending=None)
        self._save();shutil.rmtree(pending)
        if self.state['renewal'] is not None:self._finish_renewal(version)
        return {'ok':True,'version':version,'content_ids':self.state['content_ids'],
                'bootstrap_sha256':self.state['bootstrap_sha256'],'metadata_expires':Metadata.from_file(str(timestamp)).signed.expires.isoformat(),
                'resumed':True,'client_trust_changed':False}

    def _old_public_checkpoint(self):
        """Positive exact committed observation, never absence after commitment."""
        timestamp=self.served/'metadata/timestamp.json'
        if not self.state['version']:
            require(not os.path.lexists(timestamp),'public_catalog_version_changed')
            return None
        require(timestamp.exists(),'public_catalog_checkpoint_missing')
        _public_existing(timestamp)
        prior=read_json(self.root/'receipts'/f"{self.state['version']}.json",maximum=256*1024)
        with bounded_file(timestamp,16384) as stream:raw=stream.read()
        require(hashlib.sha256(raw).hexdigest()==prior['timestamp_sha256'],'public_catalog_version_changed')
        from tuf.api.metadata import Metadata
        metadata=Metadata.from_bytes(raw);Metadata.from_bytes(self.bootstrap).verify_delegate('timestamp',metadata)
        require(metadata.signed.version==self.state['version'],'public_catalog_version_changed')
        return prior['timestamp_sha256']

    def _renewal_data(self,cursor):
        fields(cursor,('abandoned_version','clock','source_timestamp_sha256','previous_abandoned_version'),'renewal cursor')
        version=cursor['abandoned_version']
        require(type(version) is int and 0<version<=self.state['reserved_version'],'catalog_renewal_refused')
        require(cursor['previous_abandoned_version'] is None or type(cursor['previous_abandoned_version']) is int
                and 0<cursor['previous_abandoned_version']<version,'catalog_renewal_refused')
        _time(datetime.fromisoformat(cursor['clock']))
        folder=private(self.root/f'renewal-{version}',directory=True)
        identity=read_json(folder/'identity.json')
        require(identity=={'format':1,'repository_id':self.state['repository_id'],'cursor':cursor},'catalog_renewal_refused')
        source=read_json(folder/'source.json',maximum=256*1024)
        audit=read_json(self.root/'abandoned'/f'{version}.json',maximum=256*1024)
        require(audit['repository_id']==self.state['repository_id'] and audit['source_sha256']==hashlib.sha256(canonical(source)).hexdigest()
                and source['version']==version and source['timestamp_sha256']==cursor['source_timestamp_sha256'],
                'catalog_renewal_source_changed')
        bundles={mode:folder/mode for mode in source['content_ids']}
        for mode,bundle in bundles.items():
            manifest=inspect_bundle(bundle)
            require(manifest['content_id']==source['content_ids'][mode],'catalog_renewal_source_changed')
            for name in ('manifest.json','bundle.tar.gz',*(row['name'] for row in manifest['files'])):
                path=bundle/name;row=source['files']['targets/'+mode+'/'+sha256(path)+'.'+name]
                require(path.stat().st_size==row['bytes'],'catalog_renewal_source_changed')
        return folder,source,audit,bundles

    def _finish_renewal(self,replacement):
        cursor=self.state['renewal'];folders=[];seen=set()
        while cursor is not None:
            require(cursor['abandoned_version'] not in seen and len(seen)<256,'catalog_renewal_refused')
            seen.add(cursor['abandoned_version'])
            folder,source,audit,_=self._renewal_data(cursor)
            require(replacement>cursor['abandoned_version'] and self.state['version']==replacement,'catalog_renewal_refused')
            audit.update(phase='renewed',replacement_version=replacement)
            atomic_json(self.root/'abandoned'/f"{cursor['abandoned_version']}.json",audit)
            folders.append(folder)
            previous=cursor['previous_abandoned_version']
            cursor=read_json(self.root/f'renewal-{previous}/identity.json')['cursor'] if previous else None
        self.state['renewal']=None;self._save()
        for folder in folders:shutil.rmtree(folder)

    def _cleanup_completed_renewals(self):
        for folder in self.root.glob('renewal-*'):
            if not re.fullmatch(r'renewal-[1-9][0-9]{0,9}',folder.name):continue
            private(folder,directory=True)
            cursor=read_json(folder/'identity.json')['cursor']
            audit=read_json(self.root/'abandoned'/f"{cursor['abandoned_version']}.json",maximum=256*1024)
            if audit['phase']=='renewed':
                require(type(audit['replacement_version']) is int and audit['replacement_version']<=self.state['version'],
                        'catalog_renewal_refused')
                self._renewal_data(cursor);shutil.rmtree(folder)

    def _continue_renewal(self):
        cursor=self.state['renewal'];folder,source,audit,bundles=self._renewal_data(cursor)
        self._old_public_checkpoint()
        pending=self.root/'pending'
        if pending.exists():
            require(self.state['pending'] is None,'pending_catalog_publication_requires_resume')
            # The state transition reserved the old version before deletion.
            # Recheck the exact abandoned payload after an interrupted cleanup.
            self.state['pending']=source
            try:self._check_pending()
            finally:self.state['pending']=None
            shutil.rmtree(pending)
        now=_time(_wall_now())
        planned=_time(datetime.fromisoformat(cursor['clock']))
        require(now>=datetime.fromisoformat(self.state['clock_high_water']),'catalog_publication_clock_rollback')
        # Renew operation supplies a fresh time; fixture/caller may use a later
        # explicit planned time. Never go backward or weaken role expirations.
        when=max(now,planned)
        return self._publish_locked(bundles,exclusions=source['exclusions'],now=when)

    def renew_pending(self,*,now=None):
        """Abandon only a proven unadvertised expired attempt, reserve its version.

        Keep a compact attributable audit, all immutable public metadata, the
        complete source bytes, root chain and current cumulative exclusions.
        Interrupted renewal can continue; advertised candidates require resume.
        """
        with file_lease(self.root/'publisher.lock'):
            self._reload()
            if self.state['pending'] is None:
                require(self.state['renewal'] is not None,'pending_catalog_publication_required')
                return self._continue_renewal()
            record,pending=self._check_pending()
            from tuf.api.metadata import Metadata
            when=_time(now or _wall_now())
            candidate=Metadata.from_file(str(pending/'metadata/timestamp.json'))
            timestamp=self.served/'metadata/timestamp.json'
            require(not timestamp.exists() or sha256(timestamp)!=record['timestamp_sha256'],
                    'catalog_publication_already_advertised_resume_required')
            checkpoint=self._old_public_checkpoint()
            require(candidate.signed.expires<=when,'pending_catalog_metadata_not_expired')
            require(when>=datetime.fromisoformat(record['clock']),'catalog_publication_clock_rollback')
            version=record['version'];previous=self.state['renewal']
            cursor={'abandoned_version':version,'clock':when.isoformat(),'source_timestamp_sha256':record['timestamp_sha256'],
                    'previous_abandoned_version':previous['abandoned_version'] if previous else None}
            folder=self.root/f'renewal-{version}'
            if folder.exists():
                identity=read_json(folder/'identity.json')
                require(identity['format']==1 and identity['repository_id']==self.state['repository_id']
                        and identity['cursor']['abandoned_version']==version
                        and identity['cursor']['source_timestamp_sha256']==record['timestamp_sha256']
                        and identity['cursor']['previous_abandoned_version']==cursor['previous_abandoned_version'],
                        'catalog_renewal_refused')
                cursor=identity['cursor']
                require(read_json(folder/'source.json',maximum=256*1024)==record,'catalog_renewal_source_changed')
            else:
                folder.mkdir(mode=0o700)
                atomic_json(folder/'identity.json',{'format':1,'repository_id':self.state['repository_id'],'cursor':cursor})
                atomic_json(folder/'source.json',record)
            from .catalog_export import read_manifest
            for mode in record['content_ids']:
                bundle=folder/mode;bundle.mkdir(mode=0o700,exist_ok=True)
                manifest_row=next((name,row) for name,row in record['files'].items()
                                  if name.startswith('targets/'+mode+'/') and name.endswith('.manifest.json'))
                _copy_target(bundle/'manifest.json',pending/manifest_row[0],manifest_row[1],mode=0o600)
                manifest=read_manifest(bundle/'manifest.json')
                for name in ('bundle.tar.gz',*(row['name'] for row in manifest['files'])):
                    row=next((relative,info) for relative,info in record['files'].items()
                             if relative.startswith('targets/'+mode+'/') and relative.endswith('.'+name))
                    usage=os.statvfs(self.root)
                    require(usage.f_bavail*usage.f_frsize>=5*1024**3+row[1]['bytes'],'catalog_publication_disk_reserve_required')
                    _copy_target(bundle/name,pending/row[0],row[1],mode=0o600)
                inspect_bundle(bundle)
            # Skipped attempts retain their root links even if failure preceded
            # public copying; root versions are never reset or reused.
            root_name=f'metadata/{version+1}.root.json'
            _write(self.served/root_name,(pending/root_name).read_bytes())
            audits=self.root/'abandoned';audits.mkdir(mode=0o700,exist_ok=True)
            audit={'format':1,'repository_id':self.state['repository_id'],'phase':'prepared','abandoned_version':version,
                   'last_committed_version':self.state['version'],'committed_timestamp_sha256':checkpoint,
                   'pending_timestamp_sha256':record['timestamp_sha256'],'renewal_time':cursor['clock'],
                   'source_sha256':hashlib.sha256(canonical(record)).hexdigest(),'source_files':record['files'],
                   'content_ids':record['content_ids'],'exclusions_sha256':hashlib.sha256(canonical(record['exclusions'])).hexdigest(),
                   'previous_abandoned_version':cursor['previous_abandoned_version'],'replacement_version':None}
            path=audits/f'{version}.json'
            if path.exists():require(read_json(path,maximum=256*1024)==audit,'catalog_renewal_audit_changed')
            else:atomic_json(path,audit)
            self.state['renewal']=cursor;self.state['pending']=None;self._save()
            return self._continue_renewal()

    def resume(self):
        with file_lease(self.root/'publisher.lock'):
            self._reload()
            if self.state['pending'] is None:
                if self.state['renewal'] is not None:
                    if self.state['version']>self.state['renewal']['abandoned_version']:
                        self._discard_unjournaled();self._finish_renewal(self.state['version'])
                        return {'ok':True,'version':self.state['version'],'renewal_finalized':True,'client_trust_changed':False}
                    return self._continue_renewal()
                self._cleanup_completed_renewals()
                require(self._discard_unjournaled(),'pending_catalog_publication_required')
                return {'ok':True,'version':self.state['version'],'construction_cleaned':True,'client_trust_changed':False}
            return self._commit()

    def republish(self,version,*,now=None):
        """Owner rollback of prepared content under NEW versions and current policy.

        Restoring an older timestamp is never a rollback mechanism. Read only
        retained signed targets, reconstruct complete prepared bundles privately,
        and use the ordinary monotonic publication path with current exclusions.
        """
        self._reload()
        require(type(version) is int and 1<=version<=self.state['version'],'retained_catalog_version_refused')
        require(self.state['pending'] is None,'pending_catalog_publication_requires_resume')
        from tuf.api.metadata import Metadata
        with bounded_file(self.served/f'metadata/{version}.targets.json',METADATA_LIMIT) as handle:
            targets=Metadata.from_bytes(handle.read())
        Metadata.from_bytes(self.bootstrap).verify_delegate('targets',targets)
        require(targets.signed.version==version,'retained_catalog_version_refused')
        workspace=Path(tempfile.mkdtemp(prefix='.catalog-rollback-',dir=self.root))
        try:
            bundles={}
            from .catalog_export import read_manifest
            for mode in (m for m in MODES if m+'/manifest.json' in targets.signed.targets):
                bundle=workspace/mode;bundle.mkdir(mode=0o700);bundles[mode]=bundle
                info=targets.signed.targets[mode+'/manifest.json']
                source=self.served/'targets'/mode/(info.hashes['sha256']+'.manifest.json')
                _copy_target(bundle/'manifest.json',source,{'bytes':info.length,'sha256':info.hashes['sha256']},mode=0o600)
                manifest=read_manifest(bundle/'manifest.json');require(manifest['mode']==mode,'retained_catalog_mode_refused')
                for name in ('bundle.tar.gz',*(row['name'] for row in manifest['files'])):
                    info=targets.signed.targets[mode+'/'+name]
                    require(set(info.hashes)=={'sha256'},'retained_catalog_identity_mismatch')
                    source=self.served/'targets'/mode/(info.hashes['sha256']+'.'+name)
                    usage=os.statvfs(self.root)
                    require(usage.f_bavail*usage.f_frsize>=5*1024**3+info.length,'catalog_publication_disk_reserve_required')
                    _copy_target(bundle/name,source,{'bytes':info.length,'sha256':info.hashes['sha256']},mode=0o600)
            result=self.publish(bundles,now=now);result['republished_content_from']=version;return result
        finally:shutil.rmtree(workspace)

    def publish(self,bundles,*,exclusions=None,now=None):
        """All modes, complete prepared bytes; never sampling/research DB export."""
        require(complete_modes(bundles),'all_prepared_catalog_modes_required')
        with file_lease(self.root/'publisher.lock'):
            self._reload()
            require(self.state['renewal'] is None,'catalog_renewal_requires_continue')
            return self._publish_locked(bundles,exclusions=exclusions,now=now)

    def _publish_locked(self,bundles,*,exclusions=None,now=None):
        require(self.state['pending'] is None,'pending_catalog_publication_requires_resume')
        self._cleanup_completed_renewals()
        now=_time(now or datetime.now(timezone.utc))
        require(now>=datetime.fromisoformat(self.state['clock_high_water']),'catalog_publication_clock_rollback')
        version=self.state['reserved_version']+1
        incoming=exclusions if exclusions is not None else self.state['exclusions'] or _default_feed()
        feed=_cumulative(self.state['exclusions'],incoming)
        if self.state['exclusions'] is not None:
            old=self.state['exclusions']
            require(feed['revision']>=old['revision'] and (feed['revision']!=old['revision'] or feed==old),
                    'catalog_exclusion_revision_rollback')
        apply_feed(initial_state(self.state['bootstrap_sha256']),feed,hashlib.sha256(canonical(feed)).hexdigest())
        manifests={mode:inspect_bundle(path) for mode,path in bundles.items()}
        require(all(manifests[m]['mode']==m for m in bundles),'prepared_catalog_mode_mismatch')
        budget=sum(sum(row['bytes'] for row in manifest['files'])+manifest['archive']['bytes']+65536
                   for manifest in manifests.values())+1024*1024
        same_device=self.root.stat().st_dev==self.served.stat().st_dev
        for path in (self.root,self.served):
            usage=os.statvfs(path)
            require(usage.f_bavail*usage.f_frsize>=5*1024**3+budget*(2 if same_device else 1),
                    'catalog_publication_disk_reserve_required')
        pending=self.root/'pending'
        self._discard_unjournaled()
        pending.mkdir(mode=0o700)
        atomic_json(pending/'.building.json',{'format':1,'repository_id':self.state['repository_id'],
                                            'previous_version':self.state['reserved_version']})
        journaled=False
        try:
            for directory in ('metadata','targets'):(pending/directory).mkdir(mode=0o700)
            from tuf.api.metadata import Metadata,Root,Targets,Snapshot,Timestamp,TargetFile,MetaFile
            root=Metadata.from_bytes(self.bootstrap);root.signed.version=version+1
            root.signed.expires=now+timedelta(days=EXPIRY_DAYS['root'])
            targets=Metadata(Targets(version=version,expires=now+timedelta(days=EXPIRY_DAYS['targets'])))
            for mode in bundles:
                manifest=manifests[mode]
                for name in ('manifest.json','bundle.tar.gz',*(row['name'] for row in manifest['files'])):
                    if name=='manifest.json':
                        data=canonical(manifest);info=TargetFile.from_data(mode+'/'+name,data,['sha256'])
                    else:
                        source=no_links(Path(bundles[mode])/name)
                        expected=manifest['archive'] if name=='bundle.tar.gz' else next(row for row in manifest['files'] if row['name']==name)
                        info=TargetFile.from_file(mode+'/'+name,str(source),['sha256'])
                        require(info.length==expected['bytes'] and info.hashes['sha256']==expected['sha256'],'prepared_catalog_changed')
                    targets.signed.targets[info.path]=info
                    destination=pending/'targets'/mode/(info.hashes['sha256']+'.'+name)
                    destination.parent.mkdir(mode=0o700,exist_ok=True)
                    if name=='manifest.json':_write(destination,data,mode=0o600)
                    else:_copy_target(destination,source,{'bytes':info.length,'sha256':info.hashes['sha256']},mode=0o600)
            feeds={'exclusions.json':_legacy_feed(feed)}
            if 'catch' in bundles or any('catch' in e['modes'] for e in feed['events']):
                feeds['exclusions-v2.json']=_scoped_feed(feed,set(CORE_MODES)|{'catch'})
            if 'loved' in bundles or any('loved' in e['modes'] for e in feed['events']):
                feeds['exclusions-loved.json']=_scoped_feed(feed,{'loved'})
            for name,body in feeds.items():
                raw=canonical(body);info=TargetFile.from_data(name,raw,['sha256'])
                targets.signed.targets[info.path]=info
                _write(pending/'targets'/(info.hashes['sha256']+'.'+name),raw,mode=0o600)
            for index,signer in enumerate(self.signers['root']):root.sign(signer,append=index>0)
            targets.sign(self.signers['targets'][0]);targets_raw=targets.to_bytes()
            snapshot=Metadata(Snapshot(version=version,expires=now+timedelta(days=EXPIRY_DAYS['snapshot'])))
            snapshot.signed.meta={'targets.json':MetaFile.from_data(version,targets_raw,['sha256'])}
            snapshot.sign(self.signers['snapshot'][0]);snapshot_raw=snapshot.to_bytes()
            timestamp=Metadata(Timestamp(version=version,expires=now+timedelta(days=EXPIRY_DAYS['timestamp'])))
            timestamp.signed.snapshot_meta=MetaFile.from_data(version,snapshot_raw,['sha256']);timestamp.sign(self.signers['timestamp'][0])
            for name,data in ((f'{version+1}.root.json',root.to_bytes()),(f'{version}.targets.json',targets_raw),
                              (f'{version}.snapshot.json',snapshot_raw),(f'{version}.timestamp.json',timestamp.to_bytes()),
                              ('timestamp.json',timestamp.to_bytes())):
                limit=ROOT_LIMIT if name.endswith('.root.json') else 16384 if name.endswith('.timestamp.json') or name=='timestamp.json' else METADATA_LIMIT
                require(len(data)<=limit,'catalog_metadata_budget_exceeded')
                _write(pending/'metadata'/name,data,mode=0o600)
            files={p.relative_to(pending).as_posix():{'bytes':p.stat().st_size,'sha256':sha256(p)}
                   for p in pending.rglob('*') if p.is_file() and p.name!='.building.json'}
            self.state['reserved_version']=version
            self.state['pending']={'version':version,'clock':now.isoformat(),
                'content_ids':{m:manifests[m]['content_id'] for m in bundles},'exclusions':feed,'files':files,
                'timestamp_sha256':files['metadata/timestamp.json']['sha256']}
            self._save();journaled=True
            result=self._commit();result['resumed']=False;return result
        finally:
            if not journaled and pending.exists():shutil.rmtree(pending)
