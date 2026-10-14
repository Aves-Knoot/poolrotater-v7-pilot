"""Build a fresh public display index from independently pinned central snapshots.

No APIs, source pages, players, scores or operator state are copied. Promotion of
an existing live index is a separate root custody operation.
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile

FIELDS=('mode','beatmap_id','beatmapset_id','title','artist','version','total_length','hit_length','sr')
MODES={'standard':'beatmaps','mania_4k':'maps','mania_7k':'maps'}
DEFAULT_EXCLUDED=frozenset({1549496})
MAX_SOURCE_BYTES=512*1024**2
MAX_INDEX_BYTES=64*1024**2
MAX_ROWS=500000
HASH=re.compile(r'[a-f0-9]{64}\Z')
SCHEMA='CREATE TABLE display_maps(mode TEXT, beatmap_id INTEGER, beatmapset_id INTEGER, title TEXT, artist TEXT, version TEXT, total_length INTEGER, hit_length INTEGER, sr REAL, PRIMARY KEY(mode,beatmap_id)) WITHOUT ROWID'


class IndexError(ValueError):pass


def require(condition,code):
    if not condition:raise IndexError(code)


def checked_path(value):
    path=Path(value).absolute()
    require('..' not in path.parts,'unsafe_path')
    for ancestor in (path,*path.parents):
        if os.path.lexists(ancestor):require(not stat.S_ISLNK(ancestor.lstat().st_mode),'symlink_refused')
    return path


def identity(info):return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def digest_fd(fd):
    os.lseek(fd,0,os.SEEK_SET);digest=hashlib.sha256()
    while block:=os.read(fd,1024**2):digest.update(block)
    return digest.hexdigest()


def open_pinned(path,pin,*,maximum):
    require(type(pin) is str and HASH.fullmatch(pin),'independent_sha256_required')
    path=checked_path(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink==1 and 0<info.st_size<=maximum,'regular_bounded_input_required')
        require(identity(info)==identity(path.lstat()),'input_replaced')
        require(digest_fd(fd)==pin,'input_sha256_mismatch')
        return path,fd,identity(info)
    except BaseException:os.close(fd);raise


def verify_pin(path,fd,before,pin):
    require(identity(os.fstat(fd))==before and identity(checked_path(path).lstat())==before and digest_fd(fd)==pin,'input_changed_during_build')


def closed(value,keys):require(type(value) is dict and set(value)==set(keys),'unexpected_manifest_fields')


def unique(pairs):
    result={}
    for key,value in pairs:require(key not in result,'duplicate_manifest_field');result[key]=value
    return result


def valid_id(value):return type(value) is int and 0<value<=2**53-1


def validate_row(row):
    bid,set_id,title,artist,version,total,hit,sr=row
    require(valid_id(bid),'invalid_beatmap_id')
    require(set_id is None or valid_id(set_id),'invalid_beatmapset_id')
    for value in (title,artist,version):require(value is None or type(value) is str and len(value)<=4096 and '\0' not in value,'invalid_public_text')
    for value in (total,hit):require(value is None or type(value) is int and 0<=value<=86400,'invalid_public_duration')
    require(sr is None or type(sr) in (int,float) and math.isfinite(sr) and 0<=sr<=100,'invalid_public_stars')


def build(sources_json,sources_sha256,destination,report_path,*,extra_exclusions=()):
    destination=checked_path(destination);report_path=checked_path(report_path)
    require(destination!=report_path,'distinct_output_paths_required')
    require(not os.path.lexists(destination) and not os.path.lexists(report_path),'fresh_outputs_required')
    require(destination.parent.is_dir() and report_path.parent.is_dir(),'existing_output_parents_required')
    excluded=DEFAULT_EXCLUDED|frozenset(extra_exclusions)
    require(all(valid_id(value) for value in excluded),'invalid_exclusion')
    temporary=None;report_temporary=None;promoted=False;report_promoted=False
    with ExitStack() as stack:
        manifest,mfd,midentity=open_pinned(sources_json,sources_sha256,maximum=65536);stack.callback(os.close,mfd)
        os.lseek(mfd,0,os.SEEK_SET)
        try:value=json.loads(os.read(mfd,65537),object_pairs_hook=unique,parse_constant=lambda _: (_ for _ in ()).throw(IndexError('invalid_json')))
        except (ValueError,UnicodeError) as exc:raise IndexError('invalid_manifest_json') from exc
        closed(value,('format','sources'));require(type(value['format']) is int and value['format']==1,'invalid_manifest_format')
        require(type(value['sources']) is list and len(value['sources'])==3,'three_full_modes_required')
        sources=[];modes=set();paths=set()
        for item in value['sources']:
            closed(item,('mode','table','path','sha256'));mode=item['mode']
            require(type(mode) is str and mode in MODES and item['table']==MODES[mode] and mode not in modes,'invalid_mode_or_table')
            require(type(item['path']) is str and Path(item['path']).is_absolute(),'absolute_source_path_required')
            path,fd,before=open_pinned(item['path'],item['sha256'],maximum=MAX_SOURCE_BYTES);stack.callback(os.close,fd)
            require(path not in paths and path not in (manifest,destination,report_path),'duplicate_or_output_source')
            modes.add(mode);paths.add(path)
            require(not any(os.path.lexists(str(path)+suffix) for suffix in ('-wal','-shm','-journal')),'unsettled_source_refused')
            source=sqlite3.connect(Path('/proc/self/fd').joinpath(str(fd)).as_uri()+'?mode=ro&immutable=1',uri=True,timeout=1);stack.callback(source.close)
            source.execute('PRAGMA query_only=ON');source.execute('PRAGMA trusted_schema=OFF')
            require(source.execute('PRAGMA quick_check').fetchall()==[('ok',)],'corrupt_source_refused')
            table=source.execute("SELECT type,sql FROM sqlite_master WHERE name=?",(item['table'],)).fetchone()
            require(table is not None and table[0]=='table' and not table[1].lstrip().upper().startswith('CREATE VIRTUAL TABLE'),'plain_source_table_required')
            columns={r[1] for r in source.execute('PRAGMA table_info('+item['table']+')')}
            require(set(FIELDS[1:])<=columns,'public_columns_missing')
            sources.append((item,path,fd,before,source))
        fd,tempname=tempfile.mkstemp(prefix='.display-index-',suffix='.db',dir=destination.parent);os.close(fd);temporary=Path(tempname)
        try:
            counts=[]
            with sqlite3.connect(temporary) as output:
                output.execute('PRAGMA trusted_schema=OFF');output.execute('PRAGMA max_page_count='+str(MAX_INDEX_BYTES//4096));output.execute(SCHEMA)
                for item,path,sourcefd,before,source in sources:
                    seen=0;exported=0
                    rows=source.execute('SELECT '+','.join(FIELDS[1:])+' FROM '+item['table']+' ORDER BY beatmap_id')
                    previous=None
                    while batch:=rows.fetchmany(1000):
                        ready=[]
                        for row in batch:
                            seen+=1;require(seen<=MAX_ROWS,'source_row_limit');validate_row(row)
                            require(row[0]!=previous,'duplicate_map_per_mode');previous=row[0]
                            if row[0] not in excluded:ready.append((item['mode'],*row))
                        output.executemany('INSERT INTO display_maps VALUES(?,?,?,?,?,?,?,?,?)',ready);exported+=len(ready)
                    counts.append({'mode':item['mode'],'table':item['table'],'sha256':item['sha256'],'source_rows':seen,'exported_rows':exported})
                require(output.execute('PRAGMA quick_check').fetchall()==[('ok',)],'output_integrity_failed')
            for item,path,sourcefd,before,_ in sources:
                verify_pin(path,sourcefd,before,item['sha256'])
                require(not any(os.path.lexists(str(path)+suffix) for suffix in ('-wal','-shm','-journal')),'source_became_unsettled')
            verify_pin(manifest,mfd,midentity,sources_sha256)
            require(temporary.stat().st_size<=MAX_INDEX_BYTES,'index_size_limit')
            with temporary.open('rb') as handle:index_pin=hashlib.file_digest(handle,'sha256').hexdigest();os.fsync(handle.fileno())
            report={'format':1,'kind':'community-display-index-build','ok':True,'fields':list(FIELDS),'sources_manifest_sha256':sources_sha256,'sources':counts,'excluded_beatmap_ids':sorted(excluded),'index_sha256':index_pin,'index_bytes':temporary.stat().st_size,'sources_unchanged':True,'private_tables_exported':False,'provider_requests':0,'production_services_changed':False}
            rfd,rname=tempfile.mkstemp(prefix='.display-report-',suffix='.json',dir=report_path.parent);report_temporary=Path(rname)
            with os.fdopen(rfd,'wb') as handle:handle.write((json.dumps(report,sort_keys=True,indent=2)+'\n').encode());handle.flush();os.fsync(handle.fileno())
            report_temporary.chmod(0o444);temporary.chmod(0o444)
            # link() is atomic and refuses a raced existing destination.
            os.link(temporary,destination,follow_symlinks=False);promoted=True;temporary.unlink();temporary=None
            os.link(report_temporary,report_path,follow_symlinks=False);report_promoted=True;report_temporary.unlink();report_temporary=None
            for parent in {destination.parent,report_path.parent}:
                directoryfd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(directoryfd)
                finally:os.close(directoryfd)
            return report
        except BaseException:
            if promoted and destination.exists():destination.unlink()
            if report_promoted and report_path.exists():report_path.unlink()
            raise
        finally:
            for path in (temporary,report_temporary):
                if path is not None and path.exists():path.unlink()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources-json',required=True,type=Path);parser.add_argument('--sources-sha256',required=True)
    parser.add_argument('--destination',required=True,type=Path);parser.add_argument('--report',required=True,type=Path)
    parser.add_argument('--exclude-beatmap-id',type=int,action='append',default=[])
    args=parser.parse_args()
    try:report=build(args.sources_json,args.sources_sha256,args.destination,args.report,extra_exclusions=args.exclude_beatmap_id)
    except (IndexError,OSError,sqlite3.Error):print(json.dumps({'ok':False,'error':'display_index_build_refused'}));return 1
    print(json.dumps(report,sort_keys=True));return 0

if __name__=='__main__':raise SystemExit(main())
