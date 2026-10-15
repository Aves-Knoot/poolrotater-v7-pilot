"""Receipt-attributed installed-code cleanup under native lifecycle locks.

Never deletes operator state, credentials, catalogs, environments or evidence.
Plans are durable and bounded; interrupted tree removal can resume only after
remaining immutable files match the retained original inventory.
"""
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid

from ..catalog_trust import bounded_file,no_links
from ..layout import file_lease
from ..provisioning import _control,_write,protected
from .contracts import digest,require,identifier,timestamp


def records(target,parent):
    prefix=target['prefix'];found={parent['bundle_sha256']:{'record':parent,'metadata':[],'mtime':float('inf')}}
    for relative in('candidates','worker-releases'):
        folder=prefix/relative/parent['installation_id']
        if not folder.exists():continue
        protected(folder)
        for path in folder.glob('*.json'):
            value=_control(protected(path));record=value.get('candidate',value)
            if not isinstance(record,dict)or'release'not in record:continue
            for key in('installation_id','generation','host_root','service_uid','prefix'):
                require(record.get(key)==parent[key],'update_retention_foreign_record')
            sha=digest(record['bundle_sha256']);release=Path(record['release'])
            require(path.name==sha+'.json'and release.parent==prefix/'releases'
                    and re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+-'+sha[:16],release.name),
                    'update_retention_record_path_refused')
            if sha in found:
                require(found[sha]['record']==record,'update_retention_registration_conflict')
            else:found[sha]={'record':record,'metadata':[],'mtime':path.stat().st_mtime}
            found[sha]['metadata'].append(path.relative_to(prefix).as_posix())
            found[sha]['mtime']=max(found[sha]['mtime'],path.stat().st_mtime)
    return found


def references(target,parent,*,catalog=None):
    catalog=records(target,parent)if catalog is None else catalog
    prefix=target['prefix'];result={parent['bundle_sha256']};paths={parent['release']}
    for name in('pilot-upgrade.json','controller-update.json'):
        value=_control(prefix/name,optional=True)
        if value:
            for key in('before','after'):
                record=value.get(key,{})
                if record.get('bundle_sha256'):
                    result.add(digest(record['bundle_sha256']));paths.add(record['release'])
    for name in('ownership.json','status.json'):
        value=_control(target['root']/'state/rolling'/name,parent['service_uid'],optional=True)or{}
        for row in value.get('rooms',{}).values():
            for key in('release','last_good_release','previous','target'):
                if row.get(key):paths.add(row[key])
    from ..worker_deployment import read_job
    job=read_job(parent)
    if job:result.add(job['candidate_sha256'])
    # Old disabled native controller units can still be a manual recovery route.
    # Keep their exact release code without changing any unit or service.
    from ..pilot_operations import UNIT_BASE
    for suffix in('.service','.timer'):
        unit=UNIT_BASE/('poolrotater-v7-controller-update-'+prefix.name+suffix)
        if not unit.exists():continue
        protected(unit)
        with bounded_file(unit,16384)as stream:
            raw=stream.read().decode('ascii')
        for value in re.findall(re.escape(str(prefix/'releases'))+r'/v[0-9]+\.[0-9]+\.[0-9]+-[a-f0-9]{16}',raw):paths.add(value)
    # Keep code referenced by an owned child even between journal observations.
    # Never emit command arguments: only canonical release paths enter pins.
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():continue
        try:
            if process.stat().st_uid!=parent['service_uid']:continue
            with(process/'cmdline').open('rb')as stream:arguments=stream.read(65537)
            if len(arguments)>65536:continue
            for raw in arguments.split(b'\0'):
                word=raw.decode('utf-8',errors='ignore');base=str(prefix/'releases')+'/'
                if not word.startswith(base):continue
                release=word[len(base):].split('/',1)[0]
                if re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+-[a-f0-9]{16}',release):paths.add(str(prefix/'releases'/release))
        except(OSError,ValueError):continue
    for value in paths:
        path=Path(value)
        require(path.parent==prefix/'releases','update_retention_foreign_reference')
        matching=[sha for sha,row in catalog.items()if row['record']['release']==value]
        if matching:result.update(matching)
        else:
            # The committed predecessor can live only in the upgrade journal,
            # not candidates. Match its exact path to its digest above. Unknown
            # paths are never guessed from a 16-character suffix.
            require(any(value==v.get('record',{}).get('release')for v in catalog.values())
                    or path.exists(),'update_retention_missing_referenced_release')
    return result,paths


def _tree(path,expected=None):
    protected(path);files={};total=0
    for item in path.rglob('*'):
        protected(item);info=item.lstat()
        require(info.st_nlink==1 if stat.S_ISREG(info.st_mode)else stat.S_ISDIR(info.st_mode),
                'update_retention_link_or_type_refused')
        if item.is_file():
            total+=info.st_size
            with bounded_file(item,128*1024**2)as stream:
                files[item.relative_to(path).as_posix()]=hashlib.file_digest(stream,'sha256').hexdigest()
    if expected is not None:
        require(not expected.get('links')and all(expected['files'].get(n)==v for n,v in files.items()),
                'update_retention_code_inventory_changed')
    return files,total


def _remove_tree(path,expected=None):
    _,size=_tree(path,expected)
    require(shutil.rmtree.avoids_symlink_attacks,'update_retention_safe_removal_required')
    for directory in(path,*[p for p in path.rglob('*')if p.is_dir()]):directory.chmod(0o755)
    shutil.rmtree(path)
    return size


def cleanup(target,parent,*,pinned=(),keep=3,minimum_age=86400,clock=time.time):
    require(type(keep)is int and keep>=3,'update_retention_minimum_required')
    require(type(minimum_age)in(int,float)and minimum_age>=86400,'update_retention_age_required')
    prefix=target['prefix'];now=clock();freed=0;removed=[];staging_removed=[]
    with ExitStack()as locks:
        # Same nonblocking locks as native staging/admission/upgrades. Busy work
        # is left alone rather than stopped. No room/process operation occurs.
        for path in(prefix.parent/'.install.lock',prefix/'deployment-admission.lock',
                    prefix/'worker-admin.lock',prefix/'controller-update-admin.lock',prefix/'.provision.lock'):
            locks.enter_context(file_lease(path))
        from ..private_pilot import PROFILE
        if os.path.lexists(target['root']/PROFILE):
            return {'installed_releases_removed':0,'staging_directories_removed':0,'installed_freed_bytes':0,
                'protected_installed_releases':0,'retained_evidence':True,'environments_retained':True,
                'cleanup_deferred':'private_pilot_configured','live_rooms_affected':False}
        catalog=records(target,parent);live,paths=references(target,parent,catalog=catalog)
        live.update(pinned);live.update(sha for sha,row in sorted(catalog.items(),key=lambda kv:kv[1]['mtime'],reverse=True)[:keep])
        control=prefix/'updates';control.mkdir(mode=0o700,exist_ok=True);protected(control)
        plan_path=control/'retention.json';plan=_control(plan_path,optional=True)
        if plan is not None:identifier(plan['id'])
        if plan is None or plan['phase']=='complete':
            entries=[]
            for sha,row in sorted(catalog.items(),key=lambda kv:kv[1]['mtime']):
                if sha in live or row['record']['release']in paths or now-row['mtime']<minimum_age:continue
                entries.append({'sha256':sha,'record':row['record'],'metadata':row['metadata']})
                if len(entries)==16:break
            if plan is not None:
                folder=control/'retention-history';folder.mkdir(mode=0o700,exist_ok=True);protected(folder)
                _write(folder/(plan['id']+'.json'),plan)
            plan={'format':1,'id':str(uuid.uuid4()),'installation_id':parent['installation_id'],
                'generation':parent['generation'],'phase':'prepared','created_at':now,'entries':entries,'finished':[]}
            _write(plan_path,plan)
        require(plan['format']==1 and plan['installation_id']==parent['installation_id']
                and plan['generation']==parent['generation']and plan['phase']=='prepared'
                and len(plan['entries'])<=16,'update_retention_plan_refused')
        for entry in plan['entries']:
            sha=digest(entry['sha256']);record=entry['record'];release=Path(record['release'])
            require(record['bundle_sha256']==sha and release.parent==prefix/'releases'
                    and all(record[k]==parent[k]for k in('installation_id','generation','prefix','host_root','service_uid')),
                    'update_retention_plan_binding_refused')
            if sha in plan['finished']:continue
            if sha not in live and str(release)not in paths:
                # Persist original metadata before deleting any bytes. Its full
                # snapshots remain available for interrupted cleanup inspection.
                for relative in entry['metadata']:
                    source=prefix/relative
                    require(source.parent in(prefix/'candidates'/parent['installation_id'],
                        prefix/'worker-releases'/parent['installation_id'])and source.name==sha+'.json',
                        'update_retention_metadata_path_refused')
                    if not source.exists():continue
                    value=_control(protected(source));actual=value.get('candidate',value)
                    require(actual==record,'update_retention_metadata_changed')
                    destination=prefix/'retired-release-records'/relative
                    destination.parent.mkdir(parents=True,mode=0o755,exist_ok=True);protected(destination.parent)
                    previous=_control(destination,optional=True)
                    require(previous is None or previous==value,'update_retention_evidence_conflict')
                    _write(destination,value)
                if release.exists():freed+=_remove_tree(release,record['release_snapshot']);removed.append(sha)
                for relative in entry['metadata']:
                    path=prefix/relative
                    if path.exists():protected(path);path.unlink()
                archive=prefix/('controller-'+sha+'.zip')
                if archive.exists():
                    protected(archive)
                    with bounded_file(archive,128*1024**2)as stream:
                        size=os.fstat(stream.fileno()).st_size
                        require(hashlib.file_digest(stream,'sha256').hexdigest()==sha,'update_retention_archive_changed')
                    archive.unlink();freed+=size
            plan['finished'].append(sha);_write(plan_path,plan)
        # Only our own attributed crash leftovers are eligible. Older/unmarked
        # directories remain for review; a filename alone is not ownership proof.
        staging=prefix/'.staging'
        if staging.exists():
            protected(staging)
            for path in list(staging.glob('package-*'))[:32]:
                protected(path);marker=_control(path/'.retention.json',optional=True)
                if marker is None:continue
                require(marker.get('purpose')=='poolrotater-runtime-stage'and marker.get('installation_id')==parent['installation_id']
                        and marker.get('generation')==parent['generation']and marker.get('stage')==str(path),
                        'update_retention_stage_binding_refused')
                timestamp(marker['created_at'])
                sha=digest(marker['bundle_sha256'])
                if sha in live or now-marker['created_at']<minimum_age:continue
                evidence=control/'staging-history';evidence.mkdir(mode=0o700,exist_ok=True);protected(evidence)
                _write(evidence/(path.name+'.json'),marker)
                freed+=_remove_tree(path);staging_removed.append(path.name)
        _write(plan_path,{**plan,'phase':'complete','completed_at':now})
    return {'installed_releases_removed':len(removed),'staging_directories_removed':len(staging_removed),
            'installed_freed_bytes':freed,'protected_installed_releases':len(live),'retained_evidence':True,
            'environments_retained':True,'live_rooms_affected':False}
