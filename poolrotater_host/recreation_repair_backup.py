"""Private consistent backup of stopped mutable state, never live SQLite opens.

Caller holds stopped recovery admission and proves dedicated UID quiescence.
Immutable runtime/catalog payloads stay in place; their pointers are included.
Raw databases and WALs are copied before SQLite touches only private work copies.
"""
import hashlib
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid

from .config import ConfigError
from .layout import atomic_json,checked_root,read_json

RESERVE=5*1024**3
MAX_BYTES=2*1024**3
MAX_FILES=20000


def _need(value):
    if not value:raise ConfigError('repair_backup_requires_review')


def _sha(path):
    with path.open('rb')as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def _private(path):
    if not path.exists():path.mkdir(mode=0o700)
    info=path.lstat()
    _need(stat.S_ISDIR(info.st_mode)and info.st_uid==os.geteuid()and not info.st_mode&0o077)
    return path


def _files(root,uid):
    result={};size=0
    def visit(path):
        nonlocal size
        info=path.lstat()
        _need(info.st_uid in (uid,os.geteuid()))
        if stat.S_ISDIR(info.st_mode):
            for child in sorted(path.iterdir()):visit(child)
        else:
            _need(stat.S_ISREG(info.st_mode)and info.st_nlink==1)
            size+=info.st_size
            _need(len(result)<MAX_FILES and size<=MAX_BYTES)
            result[path.relative_to(root).as_posix()]={'bytes':info.st_size,'sha256':_sha(path)}
    for name in ('.poolrotater-host.json','config','secrets','state'):
        path=root/name
        if os.path.lexists(path):visit(path)
    _need(result)
    return result,size


def _verify(directory,manifest):
    _need(type(manifest)is dict and manifest.get('format')==1
          and type(manifest.get('files'))is dict and type(manifest.get('sqlite'))is dict)
    for category in ('files','sqlite'):
        for name,row in manifest[category].items():
            relative=Path(name)
            _need(not relative.is_absolute()and '..'not in relative.parts and name==relative.as_posix())
            path=directory/('raw'if category=='files'else 'sqlite')/relative
            info=path.lstat()
            _need(stat.S_ISREG(info.st_mode)and info.st_uid==os.geteuid()and info.st_nlink==1
                  and not info.st_mode&0o077 and info.st_size==row['bytes']and _sha(path)==row['sha256'])


def snapshot(root,prefix,uid,operation_id,binding):
    """Run only after stopped UID proof. Returns metadata, never private contents."""
    root=Path(root);prefix=checked_root(prefix)
    _need(str(uuid.UUID(operation_id))==operation_id)
    base=_private(prefix/'recreation-repair-backups');destination=base/operation_id
    if os.path.lexists(destination):
        _private(destination);manifest=read_json(destination/'manifest.json',maximum=8*1024**2)
        _need(manifest.get('binding')==binding and manifest.get('operation_id')==operation_id)
        _verify(destination,manifest)
        return {'path':str(destination),'reused':True,'files':len(manifest['files']),
                'sqlite_databases':len(manifest['sqlite'])}
    files,total=_files(root,uid)
    free=os.statvfs(base);_need(free.f_bavail*free.f_frsize>=RESERVE+3*total)
    stage=Path(tempfile.mkdtemp(prefix='.pending-',dir=base));os.chmod(stage,0o700)
    try:
        raw=_private(stage/'raw');databases=[]
        for name,row in files.items():
            target=raw/name;target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            with (root/name).open('rb')as source,target.open('xb')as output:
                os.fchmod(output.fileno(),0o600);shutil.copyfileobj(source,output,1024**2)
                output.flush();os.fsync(output.fileno())
            _need(target.stat().st_size==row['bytes']and _sha(target)==row['sha256'])
            with target.open('rb')as stream:
                if stream.read(16)==b'SQLite format 3\x00':databases.append(name)
        canonical=_private(stage/'sqlite');sqlite_files={}
        for name in databases:
            key=hashlib.sha256(name.encode()).hexdigest()+'.sqlite3';output=canonical/key
            with tempfile.TemporaryDirectory(prefix='.verify-',dir=stage)as folder:
                working=Path(folder)/'database'
                shutil.copyfile(raw/name,working)
                for suffix in ('-wal','-shm'):
                    if (raw/(name+suffix)).exists():shutil.copyfile(raw/(name+suffix),str(working)+suffix)
                started=time.monotonic()
                def timeout(*_):
                    if time.monotonic()-started>=120:raise ConfigError('repair_backup_validation_timeout')
                with closing(sqlite3.connect(working,timeout=1))as source,closing(sqlite3.connect(output,timeout=1))as target:
                    source.backup(target,pages=256,progress=timeout)
                    target.set_progress_handler(lambda:int(time.monotonic()-started>=120),10000)
                    _need(target.execute('PRAGMA integrity_check').fetchall()==[('ok',)])
                os.chmod(output,0o600)
                with output.open('rb')as stream:os.fsync(stream.fileno())
            sqlite_files[key]={'source':name,'bytes':output.stat().st_size,'sha256':_sha(output)}
        # Detect a writer that escaped quiescence rather than publish a mixed snapshot.
        _need(_files(root,uid)[0]==files)
        manifest={'format':1,'operation_id':operation_id,'binding':binding,'files':files,'sqlite':sqlite_files}
        atomic_json(stage/'manifest.json',manifest)
        _verify(stage,manifest)
        # Persist nested directory entries before publishing the final name.
        directories=[path for path in stage.rglob('*')if path.is_dir()]+[stage]
        for directory in sorted(directories,key=lambda path:len(path.parts),reverse=True):
            fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
        os.rename(stage,destination)
        fd=os.open(base,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        return {'path':str(destination),'reused':False,'files':len(files),'sqlite_databases':len(sqlite_files)}
    finally:
        if stage.exists():shutil.rmtree(stage)
