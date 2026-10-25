"""Versioned canonical-only aggregates and bounded atomic host activation."""
import os
from pathlib import Path
import re
import shutil
import tempfile

from ..layout import read_json,atomic_json,file_lease,contained,load
from ..network.contracts import require,fields,integer,number,identifier,canonical,fingerprint
from ..network.contracts import NetworkError
from ..config import ConfigError
from .migration import _private
from bot.rating_outbox import (public_key, storage_key, CATCH_KEY_BASE,
                               TAIKO_KEY_BASE, MAX_PROVIDER_MAP_ID)

PAGE_MAPS=500
MAX_MAPS=100000
MAX_PAGE=512*1024
SCHEME='canonical-map-v1'
CONTEXT_SCHEME='canonical-map-context-v2'
TAIKO_CONTEXT_SCHEME='canonical-map-context-v3'


def digest(value):
    require(type(value) is str and re.fullmatch('[a-f0-9]{64}',value),'invalid_rating_snapshot')
    return value


def manifest(value):
    fields(value,('ok','format','protocol_version','scheme','server_epoch','watermark','policy_watermark','generated_at','maps','pages','content_id'))
    require(value['ok'] is True and type(value['format']) is int and value['format'] in (1,2,3)
            and type(value['protocol_version']) is int and value['protocol_version']==1
            and (value['format'],value['scheme']) in ((1,SCHEME),(2,CONTEXT_SCHEME),(3,TAIKO_CONTEXT_SCHEME)),
            'incompatible_rating_snapshot')
    identifier(value['server_epoch']);integer(value['watermark'],0);number(value['generated_at']);integer(value['maps'],0,MAX_MAPS)
    integer(value['policy_watermark'],0,value['watermark'])
    require(type(value['pages']) is list and len(value['pages'])<=(MAX_MAPS+PAGE_MAPS-1)//PAGE_MAPS,'invalid_rating_snapshot')
    total=0
    for index,page in enumerate(value['pages']):
        fields(page,('index','sha256','bytes','maps'));require(type(page['index']) is int and page['index']==index,'invalid_rating_snapshot')
        digest(page['sha256']);integer(page['bytes'],1,MAX_PAGE);integer(page['maps'],1,PAGE_MAPS);total+=page['maps']
    require(total==value['maps'] and fingerprint({k:v for k,v in value.items() if k!='content_id'})==digest(value['content_id']),
            'invalid_rating_snapshot')
    return value


def page(value,entry):
    fields(value,('ok','format','index','rows'))
    require(value['ok'] is True and type(value['format']) is int and value['format'] in (1,2,3)
            and type(value['index']) is int and value['index']==entry['index'] and type(value['rows']) is list
            and len(value['rows'])==entry['maps'] and len(canonical(value))==entry['bytes']
            and fingerprint(value)==entry['sha256'],'invalid_rating_snapshot')
    previous=0
    for row in value['rows']:
        expected=('beatmap_id','good','bad','neutral')
        if value['format'] in (2,3):expected+=('ruleset_id','variant')
        fields(row,expected);integer(row['beatmap_id'])
        key = snapshot_key(row, value['format'])
        require(key>previous,'invalid_rating_snapshot');previous=key
        for key in ('good','bad','neutral'):integer(row[key],0)
    return value


def snapshot_key(row, format):
    try:
        if format == 1:
            return storage_key(row['beatmap_id'])
        if row['ruleset_id'] is None and row['variant'] == 'legacy-map':
            return storage_key(row['beatmap_id'])
        if row['ruleset_id']==2 and row['variant']=='native-NM':
            return storage_key(row['beatmap_id'], {'ruleset_id':2}, map_origin='catch-native-nm')
        require(format==3 and row['ruleset_id']==1 and row['variant']=='taiko',
                'invalid_rating_snapshot')
        return storage_key(row['beatmap_id'], {'ruleset_id':1}, map_origin='taiko-native-nm')
    except ValueError:
        require(False, 'invalid_rating_snapshot')


def directory(path):
    path.mkdir(mode=0o700,exist_ok=True);return _private(path,directory=True)


def validate_bundle(base,value):
    manifest(value);previous=0;result={}
    for entry in value['pages']:
        body=page(read_json(base/f"page-{entry['index']}.json",maximum=MAX_PAGE),entry)
        require(body['format']==value['format'],'invalid_rating_snapshot')
        rows=body['rows']
        for row in rows:
            key=snapshot_key(row,value['format'])
            require(key>previous,'invalid_rating_snapshot');previous=key
            result[key]=(row['good'],row['bad'])
    return result


def prune(base,keep,*,marker=None):
    """Remove only self-identifying owned snapshot folders, never unknown files."""
    removed=0
    for candidate in base.iterdir():
        if not re.fullmatch('[a-f0-9]{64}',candidate.name) or candidate.name in keep:continue
        try:
            _private(candidate,directory=True)
            if marker is None:value=manifest(read_json(candidate/'manifest.json'))
            else:
                receipt=read_json(candidate/'download.json')
                fields(receipt,('format','installation_id','generation','manifest'))
                require(type(receipt['format']) is int and receipt['format']==1
                        and all(receipt[k]==marker[k] for k in ('installation_id','generation')),'foreign_rating_snapshot')
                value=manifest(receipt['manifest'])
            require(value['content_id']==candidate.name,'invalid_rating_snapshot')
            allowed={'manifest.json',*[f"page-{e['index']}.json" for e in value['pages']]}
            if marker is not None:allowed.add('download.json')
            require({p.name for p in candidate.iterdir()}<=allowed,'unrecognized_snapshot_files')
            for p in candidate.iterdir():_private(p)
        except (OSError,ValueError,NetworkError,ConfigError):continue
        shutil.rmtree(candidate);removed+=1
    return removed


class Publisher:
    def __init__(self,repository):self.repository=repository;self.store=repository.store

    def current(self, *, context=False, taiko=False):
        # v1 stays byte-compatible for older hosts even after Catch votes exist.
        require(not taiko or context,'invalid_snapshot_context')
        snapshot_format = 3 if taiko else 2 if context else 1
        base=directory(self.store.root/('ratings-snapshots-v3' if taiko else 'ratings-snapshots-v2' if context else 'ratings-snapshots'))
        with file_lease(self.store.root/'ratings-snapshot.lock'),self.store.transaction() as db:
            policy,_=self.repository.policy.check(db)
            policy_watermark=policy['operations'][-1]['watermark'] if policy['operations'] else 0
            watermark=int(db.execute("SELECT value FROM meta WHERE key='ratings_watermark'").fetchone()[0])
            epoch=db.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()[0]
            pointer=base/'current.json'
            old=None
            if os.path.lexists(pointer):
                old=manifest(read_json(pointer))
                if old['watermark']==watermark and old['server_epoch']==epoch:
                    validate_bundle(_private(base/old['content_id'],directory=True),old);return old
            query = ('SELECT beatmap_id,SUM(rating>0) good,SUM(rating<0) bad,SUM(rating=0) neutral '
                     'FROM ratings_current WHERE suppressed=0 AND rating IS NOT NULL AND ')
            if taiko:
                query += 'beatmap_id<? '
                params=(2**63-1,MAX_MAPS+1)
            elif context:
                query += '(beatmap_id<? OR beatmap_id>?) AND beatmap_id<? '
                params=(TAIKO_KEY_BASE,TAIKO_KEY_BASE+MAX_PROVIDER_MAP_ID,2**63-1,MAX_MAPS+1)
            else:
                query += '(beatmap_id<? OR beatmap_id>?) AND beatmap_id<? '
                params=(TAIKO_KEY_BASE,TAIKO_KEY_BASE+MAX_PROVIDER_MAP_ID,CATCH_KEY_BASE,MAX_MAPS+1)
            query += 'GROUP BY beatmap_id ORDER BY beatmap_id LIMIT ?'
            rows=[dict(row) for row in db.execute(query,params)]
            require(len(rows)<=MAX_MAPS,'rating_snapshot_capacity_required',503)
            context_format=context
            format=snapshot_format
            if context_format:
                for row in rows:
                    row['beatmap_id'],row['ruleset_id'],row['variant']=public_key(row['beatmap_id'])
            pages=[];entries=[]
            for start in range(0,len(rows),PAGE_MAPS):
                item={'ok':True,'format':format,'index':len(pages),'rows':rows[start:start+PAGE_MAPS]};pages.append(item)
                entries.append({'index':item['index'],'sha256':fingerprint(item),'bytes':len(canonical(item)),'maps':len(item['rows'])})
            scheme=TAIKO_CONTEXT_SCHEME if taiko else CONTEXT_SCHEME if context_format else SCHEME
            value={'ok':True,'format':format,'protocol_version':1,'scheme':scheme,'server_epoch':epoch,'watermark':watermark,
                   'policy_watermark':policy_watermark,'generated_at':self.store.clock(),'maps':len(rows),'pages':entries}
            value['content_id']=fingerprint(value);manifest(value)
            target=base/value['content_id'];stage=Path(tempfile.mkdtemp(prefix='.snapshot-',dir=base))
            try:
                for item in pages:atomic_json(stage/f"page-{item['index']}.json",item)
                atomic_json(stage/'manifest.json',value);validate_bundle(stage,value)
                if os.path.lexists(target):validate_bundle(_private(target,directory=True),value)
                else:
                    os.rename(stage,target)
                    fd=os.open(base,os.O_RDONLY|os.O_DIRECTORY)
                    try:os.fsync(fd)
                    finally:os.close(fd)
                atomic_json(pointer,value)
                prune(base,{value['content_id'],old['content_id'] if old is not None else value['content_id']})
            finally:
                if stage.exists():shutil.rmtree(stage)
        return value

    def read_page(self,content_id,index,*,context=False,taiko=False):
        digest(content_id);integer(index,0,199)
        require(not taiko or context,'invalid_snapshot_context')
        directory_name='ratings-snapshots-v3' if taiko else 'ratings-snapshots-v2' if context else 'ratings-snapshots'
        require(os.path.lexists(self.store.root/directory_name/content_id),'rating_snapshot_not_found',404)
        base=_private(self.store.root/directory_name/content_id,directory=True)
        value=manifest(read_json(base/'manifest.json'));require(value['content_id']==content_id and index<len(value['pages']),
                                                              'rating_snapshot_not_found',404)
        return page(read_json(base/f'page-{index}.json',maximum=MAX_PAGE),value['pages'][index])


class Cache:
    def __init__(self,root,*,taiko=False):
        self.root=Path(root);self.marker,_=load(self.root)
        suffix='-v3' if taiko else ''
        self.base=contained(self.root,'state/ratings-cache'+suffix)
        self.pointer=contained(self.root,'state/ratings-snapshot'+suffix+'.json')
        self.recovery_pointer=contained(self.root,'state/ratings-snapshot'+suffix+'-last-good.json')
        self.policy_floor=contained(self.root,'state/ratings-snapshot'+suffix+'-policy-floor.json')

    def floor_value(self):
        if not os.path.lexists(self.policy_floor):return None
        value=read_json(_private(self.policy_floor))
        fields(value,('format','installation_id','generation','server_epoch','watermark','policy_watermark'))
        require(type(value['format']) is int and value['format']==1
                and all(value[k]==self.marker[k] for k in ('installation_id','generation')),'foreign_rating_snapshot_floor')
        identifier(value['server_epoch']);integer(value['watermark'],0)
        integer(value['policy_watermark'],0,value['watermark']);return value

    def pointer_value(self,path=None):
        value=read_json(path or self.pointer);fields(value,('format','installation_id','generation','current','previous',
                                                        'server_epoch','watermark','minimum_policy_watermark'))
        require(type(value['format']) is int and value['format']==1
                and all(value[k]==self.marker[k] for k in ('installation_id','generation')),'foreign_rating_snapshot')
        digest(value['current'])
        identifier(value['server_epoch']);integer(value['watermark'],0)
        integer(value['minimum_policy_watermark'],0,value['watermark'])
        if value['previous'] is not None:digest(value['previous'])
        return value

    def read(self):
        if not os.path.lexists(self.pointer) and not os.path.lexists(self.recovery_pointer):return {},{'phase':'cold','watermark':0}
        pointer_phase='current'
        try:value=self.pointer_value()
        except (OSError,ValueError,NetworkError,ConfigError):
            try:value=self.pointer_value(self.recovery_pointer);pointer_phase='last-good'
            except (OSError,ValueError,NetworkError,ConfigError):return {},{'phase':'unavailable','watermark':0}
        try:
            floor=self.floor_value()
            require(floor is None or floor['server_epoch']==value['server_epoch'],'rating_snapshot_floor_epoch_requires_review')
        except (OSError,ValueError,NetworkError,ConfigError):return {},{'phase':'unavailable','watermark':0}
        minimum=max(value['minimum_policy_watermark'],floor['policy_watermark'] if floor else 0)
        for name,phase in (('current','current'),('previous','last-good')):
            if value[name] is None:continue
            try:
                base=_private(self.base/value[name],directory=True);metadata=manifest(read_json(base/'manifest.json'))
                require(metadata['content_id']==value[name],'invalid_rating_snapshot')
                require(metadata['server_epoch']==value['server_epoch']
                        and metadata['policy_watermark']>=minimum,
                        'rating_snapshot_policy_rollback')
                return validate_bundle(base,metadata),{'phase':pointer_phase if name=='current' else phase,
                           'watermark':metadata['watermark'],'policy_watermark':metadata['policy_watermark'],
                           'server_epoch':metadata['server_epoch'],'generated_at':metadata['generated_at']}
            except (OSError,ValueError,NetworkError,ConfigError):continue
        return {},{'phase':'unavailable','watermark':0}

    def download(self,transport,token,*,server_epoch,max_pages=2,context=None,ruleset_id=None):
        integer(max_pages,1,10);identifier(server_epoch)
        require(ruleset_id in (None,0,1,2,3),'invalid_snapshot_ruleset')
        taiko = ruleset_id == 1
        with file_lease(contained(self.root,'state/ratings-snapshot.lock')):
            if context is None:
                context=taiko or any(r.get('ruleset_id') in (1,2) or r.get('preset')=='catch-fixed'
                            for r in load(self.root)[1]['rooms'])
            require(type(context) is bool,'invalid_snapshot_context')
            require(not taiko or context,'invalid_snapshot_context')
            route='/v3/ratings' if taiko else '/v2/ratings' if context else '/v1/ratings'
            try:
                reply=transport.request('GET',route+'/snapshots/current',token=token)
            except NetworkError as exc:
                if exc.status!=404 or not context or taiko:raise
                route='/v1/ratings';context=False
                reply=transport.request('GET',route+'/snapshots/current',token=token)
            value=manifest(reply)
            require(value['format']==(3 if taiko else 2 if context else 1),'incompatible_rating_snapshot')
            require(value['server_epoch']==server_epoch,'ratings_snapshot_epoch_requires_review',409)
            floor=self.floor_value()
            require(floor is None or (floor['server_epoch']==server_epoch
                    and value['watermark']>=floor['watermark'] and value['policy_watermark']>=floor['policy_watermark']),
                    'ratings_snapshot_restore_requires_review',409)
            old=None
            if os.path.lexists(self.pointer) or os.path.lexists(self.recovery_pointer):
                try:old=self.pointer_value()
                except (OSError,ValueError,NetworkError,ConfigError):old=self.pointer_value(self.recovery_pointer)
                require(old['server_epoch']==server_epoch and value['watermark']>=old['watermark']
                        and value['policy_watermark']>=old['minimum_policy_watermark'],
                        'ratings_snapshot_restore_requires_review',409)
            _,last=self.read()
            if last.get('server_epoch') is not None:
                require(last['server_epoch']==server_epoch and value['watermark']>=last['watermark'],
                        'ratings_snapshot_restore_requires_review',409)
            # Persist a learned deletion/suppression floor BEFORE any page IO.
            # An interrupted larger download cannot leave known-excluded cached
            # counts active, or recover them through its previous pointer.
            atomic_json(self.policy_floor,{'format':1,**{k:self.marker[k] for k in ('installation_id','generation')},
                        **{k:value[k] for k in ('server_epoch','watermark','policy_watermark')}})
            base=directory(self.base);target=directory(base/value['content_id']);downloaded=0;pending=0;reused=0
            receipt={'format':1,**{k:self.marker[k] for k in ('installation_id','generation')},'manifest':value}
            ownership=target/'download.json'
            if os.path.lexists(ownership):require(read_json(ownership)==receipt,'foreign_rating_snapshot')
            else:
                if list(target.iterdir()):
                    previous=manifest(read_json(target/'manifest.json'))
                    require(previous==value,'foreign_rating_snapshot');validate_bundle(target,previous)
                atomic_json(ownership,receipt)
            cached={}
            for oldbase in base.iterdir():
                if not re.fullmatch('[a-f0-9]{64}',oldbase.name) or oldbase==target:continue
                try:
                    _private(oldbase,directory=True)
                    if os.path.lexists(oldbase/'download.json'):
                        owned=read_json(oldbase/'download.json');fields(owned,('format','installation_id','generation','manifest'))
                        require(type(owned['format']) is int and owned['format']==1
                                and all(owned[k]==self.marker[k] for k in ('installation_id','generation')),'foreign_rating_snapshot')
                        previous=manifest(owned['manifest'])
                    else:
                        require(old is not None and oldbase.name in (old['current'],old['previous']),'foreign_rating_snapshot')
                        previous=manifest(read_json(oldbase/'manifest.json'))
                    require(previous['content_id']==oldbase.name,'invalid_rating_snapshot')
                    for entry in previous['pages']:cached[entry['sha256']]=oldbase/f"page-{entry['index']}.json"
                except (OSError,ValueError,NetworkError,ConfigError):continue
            for entry in value['pages']:
                path=target/f"page-{entry['index']}.json"
                if os.path.lexists(path):
                    try:page(read_json(path,maximum=MAX_PAGE),entry);continue
                    except (OSError,ValueError,NetworkError,ConfigError):pass
                if entry['sha256'] in cached:
                    try:
                        body=page(read_json(cached[entry['sha256']],maximum=MAX_PAGE),entry)
                        require(shutil.disk_usage(base).free>16*1024**2+entry['bytes'],'rating_snapshot_disk_reserve_required',503)
                        atomic_json(path,body);reused+=1;continue
                    except (OSError,ValueError,NetworkError,ConfigError):pass
                if downloaded>=max_pages:pending+=1;continue
                require(shutil.disk_usage(base).free>16*1024**2+entry['bytes'],'rating_snapshot_disk_reserve_required',503)
                body=transport.request('GET',f"{route}/snapshot-page?content_id={value['content_id']}&index={entry['index']}",token=token)
                page(body,entry);atomic_json(path,body);downloaded+=1
            if pending:
                prune(base,{value['content_id'],*[old[k] for k in ('current','previous') if old and old[k] is not None]},marker=self.marker)
                return {'ok':True,'phase':'downloading','downloaded_pages':downloaded,'reused_pages':reused,'pending_pages':pending}
            validate_bundle(target,value);atomic_json(target/'manifest.json',value)
            pointer={'format':1,**{k:self.marker[k] for k in ('installation_id','generation')},'current':value['content_id'],
                     'previous':old['current'] if old and old['current']!=value['content_id'] else old['previous'] if old else None,
                     'server_epoch':server_epoch,'watermark':value['watermark'],
                     'minimum_policy_watermark':max(value['policy_watermark'],old['minimum_policy_watermark'] if old else 0)}
            atomic_json(self.recovery_pointer,pointer);atomic_json(self.pointer,pointer)
            prune(base,{pointer['current'],pointer['previous']},marker=self.marker)
            return {'ok':True,'phase':'activated','downloaded_pages':downloaded,'reused_pages':reused,'watermark':value['watermark']}
