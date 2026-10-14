"""Network HTTPS with explicit trust, no redirects/proxies and whole-call deadline."""
import hashlib
import os
from pathlib import Path
import selectors
import signal
import ssl
import subprocess
import sys
import time
from urllib.parse import urlsplit

from ..provider_transport import TrustSnapshot
from .contracts import NetworkError, require, canonical, decode, number

RATING_ROUTES={'/v1/events/batches','/v1/ratings/state-query','/v1/ratings/snapshots/current','/v1/ratings/snapshot-page'}
ROUTES={'/v2/capabilities','/v2/rooms','/v2/hosts/self/heartbeat','/v1/capabilities','/v1/enrollments','/v1/rooms','/v1/hosts/self','/v1/hosts/self/heartbeat','/v1/hosts/self/token-rotation','/v1/hosts/self/leave','/v1/ratings/stream-state'}|RATING_ROUTES
# Diagnostics never repeat arbitrary strings supplied by a remote endpoint.
REMOTE_ERRORS=frozenset(('credential_required','credential_refused','credential_expired',
    'credential_retired','host_revoked','scope_refused','invitation_refused',
    'invitation_identity_mismatch','invitation_used','invitation_expired',
    'identity_or_credential_conflict','host_identity_mismatch','directory_not_opted_in',
    'stale_sequence','sequence_conflict','request_id_conflict','heartbeat_rate_limited',
    'hub_rate_limited','hub_clock_rollback','hub_storage_unavailable','hub_capacity_required',
    'hub_unavailable','unexpected_fields','unsupported_protocol','invalid_request',
    'route_not_found','future_room_observation','ratings_migration_required','ratings_schema_refused',
    'ratings_policy_refused','rating_query_limit','duplicate_rating_query','rating_batch_limit',
    'ratings_revision_exhausted','ratings_migration_recovery_required','request_too_large',
    'ratings_lineage_refused','ratings_lineage_requires_review','ratings_policy_recovery_required',
    'ratings_policy_requires_review','rating_snapshot_not_found','rating_snapshot_capacity_required',
    'invalid_rating_snapshot','incompatible_rating_snapshot'))


def origin(value):
    require(type(value) is str and len(value)<=256 and not any(ord(c)<=32 or ord(c)==127 for c in value),'invalid_hub_origin')
    try:url=urlsplit(value);port=url.port
    except ValueError:raise NetworkError('invalid_hub_origin') from None
    require(url.scheme=='https' and url.hostname and url.username is None and url.password is None and url.path in ('','/') and not url.query and not url.fragment,'https_hub_origin_required')
    require(url.hostname not in ('osu.ppy.sh','spectator.ppy.sh','notify.ppy.sh'),'provider_is_not_network_hub')
    require(port is None or 1<=port<=65535,'invalid_hub_port');return value.rstrip('/')


class Transport:
    def __init__(self,url,trust,*,timeout=5):
        self.origin=origin(url);require(type(trust) is TrustSnapshot,'explicit_hub_trust_required');trust.context()
        number(timeout,.1,30);self.trust=trust;self.timeout=timeout
        self.last_worker_reaped=True

    def request(self,method,path,*,body=None,token=None):
        from ..process_target import OwnedChild, require_child_platform
        require_child_platform()
        require(method in ('GET','POST') and path.split('?',1)[0] in ROUTES and len(path)<=256,'invalid_network_route')
        if token is not None:
            from .contracts import verifier
            verifier(token)
        maximum=512*1024 if path.split('?',1)[0] in RATING_ROUTES else 65536
        require(body is None or len(canonical(body))<=maximum,'request_too_large',413)
        payload=canonical({'origin':self.origin,'ca_pem':self.trust.pem.decode('ascii'),'timeout':self.timeout,'method':method,'path':path,'body':body,'token':token})
        require(len(payload)<=(786432 if maximum>65536 else 262144),'trust_or_request_too_large')
        worker=Path(__file__).with_name('transport_worker.py');deadline=time.monotonic()+self.timeout
        interrupts=(signal.SIGINT,signal.SIGTERM)
        target=None;process=None;selector=selectors.DefaultSelector();result=bytearray();sent=0
        try:
            mask=signal.pthread_sigmask(signal.SIG_BLOCK,interrupts)
            try:
                process=subprocess.Popen([sys.executable,'-I','-S',str(worker)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
                                         env={'PYTHONDONTWRITEBYTECODE':'1','PATH':'/usr/bin:/bin'},start_new_session=True)
                self.last_worker_reaped=False
                target=OwnedChild(process.pid)
            finally:signal.pthread_sigmask(signal.SIG_SETMASK,mask)
            os.set_blocking(process.stdin.fileno(),False);os.set_blocking(process.stdout.fileno(),False)
            selector.register(process.stdin,selectors.EVENT_WRITE,'write');selector.register(process.stdout,selectors.EVENT_READ,'read')
            while selector.get_map():
                left=deadline-time.monotonic();require(left>0,'hub_deadline_exceeded',503)
                ready=selector.select(left);require(ready,'hub_deadline_exceeded',503)
                for key,_ in ready:
                    if key.data=='write':
                        try:sent+=os.write(key.fd,payload[sent:])
                        except BlockingIOError:continue
                        except BrokenPipeError:raise NetworkError('hub_transport_failed',503) from None
                        if sent==len(payload):selector.unregister(process.stdin);process.stdin.close()
                    else:
                        chunk=os.read(key.fd,8192)
                        if chunk:result.extend(chunk);require(len(result)<=(1114112 if maximum>65536 else 150000),'hub_response_too_large',503)
                        else:selector.unregister(process.stdout)
            require(process.wait(timeout=max(.01,deadline-time.monotonic()))==0,'hub_transport_failed',503)
            # Worker envelope encodes a separately bounded wire reply.
            import json
            envelope=json.loads(result);raw=bytes.fromhex(envelope['body_hex']);reply=decode(raw, maximum=maximum)
            require(type(reply) is dict and type(envelope['status']) is int,'invalid_hub_response',503)
            if envelope['status']!=200:
                code=reply.get('error');require(type(code) is str and code in REMOTE_ERRORS,'invalid_hub_error',503)
                error=NetworkError(code,envelope['status']);error.retry_after=5 if envelope['status']==429 else 0;raise error
            require(reply.get('ok') is True,'invalid_hub_response',503);return reply
        except (OSError,ValueError,KeyError,subprocess.TimeoutExpired):raise NetworkError('hub_transport_failed',503) from None
        finally:
            selector.close()
            mask=signal.pthread_sigmask(signal.SIG_BLOCK,interrupts)
            try:
                if target is not None:
                    target.kill()
                    process.wait(timeout=5);self.last_worker_reaped=True
            finally:
                if target is not None:target.close()
                for stream in ((process.stdin,process.stdout) if process is not None else ()):
                    if stream and not stream.closed:stream.close()
                signal.pthread_sigmask(signal.SIG_SETMASK,mask)
