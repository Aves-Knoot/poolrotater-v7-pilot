#!/usr/bin/python3.12
"""Download one pinned repair kit and run its verified stopped upgrade workflow."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

URL = 'https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/download/v7.1.8/poolrotater-7.1.8-live-pilot.zip'
SHA256 = '69a81b6c6c8edcae34a85de883e4ba97207ae23fa3bbc4c31da345edfff7e8f7'
LIMIT = 128 * 1024**2
HOSTS = frozenset(('github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'))


def allowed(url):
    value=urllib.parse.urlsplit(url)
    if (value.scheme!='https' or value.hostname not in HOSTS or value.port not in (None,443)
            or value.username is not None or value.password is not None):
        raise ValueError('repair_download_destination_refused')


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        allowed(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def download(target):
    allowed(URL)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),Redirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    digest=hashlib.sha256();count=0;deadline=time.monotonic()+180
    with opener.open(urllib.request.Request(URL,headers={'User-Agent':'PoolRotater-pinned-repair'}),timeout=30) as response,target.open('xb') as stream:
        allowed(response.url)
        if response.status!=200:raise ValueError('repair_download_failed')
        while chunk:=response.read(1024**2):
            count+=len(chunk)
            if count>LIMIT or time.monotonic()>deadline:raise ValueError('repair_download_limit')
            digest.update(chunk);stream.write(chunk)
    if digest.hexdigest()!=SHA256:raise ValueError('repair_download_digest_mismatch')


def unpack(source,destination):
    with zipfile.ZipFile(source) as archive:
        rows=archive.infolist()
        if (len(rows)>4096 or len({r.filename for r in rows})!=len(rows)
                or sum(r.file_size for r in rows)>LIMIT):raise ValueError('repair_archive_refused')
        for row in rows:
            name=row.filename;path=PurePosixPath(name)
            if (path.is_absolute() or any(x in ('','.','..') for x in name.split('/'))
                    or not stat.S_ISREG(row.external_attr>>16) or row.flag_bits&1):
                raise ValueError('repair_archive_refused')
            target=destination/name;target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            with target.open('xb') as stream:stream.write(archive.read(row))


def main():
    if os.geteuid()!=0:raise ValueError('run_verified_launcher_using_sudo')
    if not sys.argv[1:]:raise ValueError('explicit_instance_and_start_choices_required')
    os.umask(0o077);os.chdir('/tmp')
    with tempfile.TemporaryDirectory(prefix='poolrotater-pinned-repair-') as directory:
        stage=Path(directory);archive=stage/'kit.zip';kit=stage/'kit';kit.mkdir(mode=0o700)
        download(archive);unpack(archive,kit)
        result=subprocess.run(['/usr/bin/python3.12','-I','-S',str(kit/'repair_and_start.py'),*sys.argv[1:]],
            cwd='/tmp',env={'PATH':'/usr/sbin:/usr/bin:/bin','LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1','MALLOC_ARENA_MAX':'2'})
        raise SystemExit(result.returncode)


if __name__=='__main__':
    try:main()
    except Exception:
        print(json.dumps({'ok':False,'phase':'pinned_repair_download_or_preflight_failed',
                          'next_action':'retain_existing_instance; inspect_verified_kit_instructions'}))
        raise SystemExit(2)
