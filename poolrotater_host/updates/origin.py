"""Separate read-only HTTPS release origin, reusing the sealed catalog service.

Preparing/installing assets does not register/start units or enable renewal.
The web UID never has release-signing custody or osu! credentials.
"""
import hashlib,json,os,re,signal,threading
from pathlib import Path
from .. import catalog_service as catalog
from ..catalog_export import sha256
from ..catalog_trust import no_links
from ..layout import atomic_json
from .contracts import require

PREFIX=Path('/opt/poolrotater-v7-releases')
STATE=Path('/var/lib/poolrotater-v7-releases')
USER='poolrotater-release'
UNIT='poolrotater-v7-releases.service'
REFRESH='poolrotater-v7-releases-refresh.service'
TIMER='poolrotater-v7-releases-refresh.timer'
CUSTODY=Path('/var/lib/poolrotater-v7-release-signers/authority')
SERVED=Path('/srv/poolrotater-v7-releases')
BOOTSTRAP=catalog.BOOTSTRAP.replace('from poolrotater_host.catalog_service import main',
                                  'from poolrotater_host.updates.origin import main')


def units(*,prefix=PREFIX,state=STATE,user=USER,custody=CUSTODY,served=SERVED):
    old=catalog.units(prefix=prefix,state=state,user=user,custody=custody,served=served)
    mapping={catalog.UNIT:UNIT,catalog.REFRESH:REFRESH,catalog.TIMER:TIMER};result={}
    # Catalog-only background jobs are not executable-release authority. Select
    # the reviewed listener/renewal trio explicitly when catalog assets grow.
    for name in mapping:
        text=old[name]
        text=text.replace('Unit='+catalog.REFRESH,'Unit='+REFRESH)
        text=text.replace('signed read-only catalog HTTPS origin','signed read-only executable-release HTTPS origin')
        text=text.replace('sealed catalog metadata refresh','sealed release metadata renewal').replace('signed catalog metadata renewal','signed release metadata renewal')
        result[mapping[name]]=text
    return result


def prepare(destination,*,wheel_cache):
    destination=Path(destination)
    catalog.prepare(destination,wheel_cache=wheel_cache)
    runtime=destination/'runtime';(runtime/'bootstrap.py').write_text(BOOTSTRAP)
    inventory={p.relative_to(runtime).as_posix():sha256(p)for p in sorted(runtime.rglob('*'))if p.is_file()and p!=runtime/'inventory.json'}
    atomic_json(runtime/'inventory.json',{'format':1,'files':inventory});(runtime/'inventory.json').chmod(0o644)
    for name in catalog.units():(destination/name).unlink()
    for name,text in units().items():(destination/name).write_text(text)
    return {'ok':True,'inventory_sha256':sha256(runtime/'inventory.json'),'runtime_files':len(inventory),
            'service_installed':False,'listener_started':False,'signing_authority_created':False}


def install_assets(kit,*,expected_inventory_sha256,cert,key,served=SERVED,custody=CUSTODY,
                   prefix=PREFIX,state=STATE,user=USER,bind='127.0.0.1',port=9446):
    return catalog._install_assets(kit,expected_inventory_sha256=expected_inventory_sha256,cert=cert,key=key,
        served=served,custody=custody,prefix=prefix,state=state,user=user,bind=bind,port=port,
        bootstrap=BOOTSTRAP,unit_factory=units,unit_names=(UNIT,REFRESH,TIMER))


def configure_renewal(state,*,custody):
    require(os.geteuid()==0,'release_administrator_required')
    state=catalog.protected(state,directory=True);custody=catalog.protected(custody,directory=True,private=True)
    from .publisher import Publisher
    # Verify identity/config before activation. Does not renew or publish bytes.
    publisher=Publisher(custody);status=publisher.status()
    value=catalog.public_json(custody/'state.json')
    atomic_json(state/'renewal.json',{'format':1,'custody':str(custody),
        'served':value['served'],'bootstrap_sha256':value['bootstrap_sha256']})
    (state/'renewal.json').chmod(0o644)
    return {'ok':True,'renewal_configured':True,'timer_started':False,'publication_pending':status['publication_pending']}


def renewal(state,*,dry_run=False):
    require(os.geteuid()==0,'release_administrator_required')
    state=catalog.protected(state,directory=True);catalog.protected(state/'renewal.json')
    value=catalog.public_json(state/'renewal.json');catalog.fields(value,('format','custody','served','bootstrap_sha256'))
    require(type(value['format'])is int and value['format']==1,'release_renewal_configuration_refused')
    from .publisher import Publisher
    publisher=Publisher(catalog.protected(value['custody'],directory=True,private=True))
    status=publisher.status();current=catalog.public_json(Path(value['custody'])/'state.json')
    require(current['served']==value['served']and current['bootstrap_sha256']==value['bootstrap_sha256'],
            'release_renewal_authority_changed')
    if dry_run:return {'ok':True,'renewal_valid':True,'publication_pending':status['publication_pending'],'published':False}
    return publisher.renew()


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('action',choices=('check','serve','refresh','check-refresh','configure-refresh'))
    parser.add_argument('--custody',type=Path)
    args=parser.parse_args(argv)
    try:
        if args.action=='configure-refresh':result=configure_renewal(args.state,custody=args.custody)
        elif args.action in('refresh','check-refresh'):result=renewal(args.state,dry_run=args.action=='check-refresh')
        else:
            state=catalog.protected(args.state,directory=True);catalog.protected(state/'listener.json')
            value=catalog.listener_settings(catalog.public_json(state/'listener.json'))
            require(os.geteuid()==value['service_uid'],'release_listener_identity_refused')
            catalog.protected(value['served'],directory=True)
            for name in('tls.crt','tls.key'):catalog.protected(state/name,uid=os.geteuid(),maximum=65536,private=True)
            from ..catalog_server import tls_context
            from .server import ReleaseServer
            context=tls_context(state/'tls.crt',state/'tls.key')
            address=('127.0.0.1',0)if args.action=='check'else(value['bind'],value['port'])
            server=ReleaseServer(value['served'],address,context=context,protected_uid=0)
            if args.action=='check':server.server_close();result={'ok':True,'listener_started':False,'tls_pair_valid':True}
            else:
                def stop(*_):threading.Thread(target=server.shutdown,daemon=True).start()
                previous={n:signal.signal(n,stop)for n in(signal.SIGINT,signal.SIGTERM)}
                print(json.dumps({'ok':True,'release_origin_starting':True}),flush=True)
                try:server.serve_forever(poll_interval=.25)
                finally:
                    server.server_close()
                    for n,handler in previous.items():signal.signal(n,handler)
                result={'ok':True,'release_origin_stopped':True}
    except Exception as error:
        code=str(error)
        if not re.fullmatch('[a-z][a-z0-9_]{1,100}',code):code='release_origin_operation_refused'
        result={'ok':False,'error':code,'provider_access':False}
    print(json.dumps(result,sort_keys=True));return 0 if result['ok']else 1
