"""Bounded cache cleanup plus receipt-attributed installed code retirement."""
import os
import re
import time

from ..catalog_trust import bounded_file, no_links
from ..layout import file_lease
from .client import read
from .contracts import require


def cleanup(client, backend, *, keep_downloads=3):
    require(type(keep_downloads)is int and keep_downloads>=3,'update_retention_minimum_required')
    with file_lease(client.root/'agent.lock'),file_lease(client.root/'client.lock'):
        from .agent import Agent
        state=client.state();agent=Agent(client,backend).state()
        pinned=set(backend.references())
        for value in (state.get('available'),(agent.get('operation')or{}).get('release'),
                      (agent.get('last_completed')or{}).get('release'),
                      (agent.get('last_reviewed')or{}).get('release')):
            if value:pinned.add(value['artifact']['sha256'])
        downloads=[]
        for path in (client.root/'downloads').iterdir():
            no_links(path)
            match=re.fullmatch(r'([a-f0-9]{64})\.(zip|writing)',path.name)
            require(match is not None,'update_unattributed_download_retained')
            with bounded_file(path,128*1024**2) as stream:
                info=os.fstat(stream.fileno())
                require(info.st_uid==os.geteuid() and not info.st_mode&0o077,'update_download_custody_refused')
            if match[2]=='zip':downloads.append((info.st_mtime,path))
        downloads.sort(reverse=True)
        pinned.update(path.stem for _,path in downloads[:keep_downloads])
        installed=backend.cleanup_releases(pinned=pinned,keep=keep_downloads)
        deleted=0;freed=0
        for _,path in downloads:
            if path.stem in pinned or time.time()-path.stat().st_mtime<86400:continue
            freed+=path.stat().st_size;path.unlink();deleted+=1
        for path in(client.root/'downloads').glob('*.writing'):
            if path.stem in pinned or time.time()-path.stat().st_mtime<86400:continue
            no_links(path)
            with bounded_file(path,128*1024**2)as stream:freed+=os.fstat(stream.fileno()).st_size
            path.unlink();deleted+=1
        # The transport caches a second verified copy plus resumable parts. Its
        # name hashes the signed target route, not the artifact alone. Retain
        # pinned identities and all metadata/rollback high waters. Only old,
        # attributable unpinned target records and their exact companions go.
        transport=client.origin.cache
        for record in transport.glob('*.json'):
            if record.name=='origin.json':continue
            item=read(record)
            name=item.get('name','')
            match=re.fullmatch(r'targets/([a-f0-9]{64})\.runtime\.zip',name)
            if not match or match[1] in pinned or time.time()-record.stat().st_mtime<86400:continue
            for suffix in ('.data','.part','.json'):
                path=record.with_suffix(suffix)
                if path.exists():
                    no_links(path)
                    with bounded_file(path,128*1024**2):pass
                    freed+=path.stat().st_size;path.unlink();deleted+=1
        return {'ok':True,'removed_files':deleted,'freed_bytes':freed,
                'protected_downloads':len(pinned),**installed,
                'retained_evidence':True,'live_rooms_affected':False}
