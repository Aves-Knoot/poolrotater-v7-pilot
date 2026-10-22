"""Bounded M3 API server; administrator actions stay local, never public routes."""
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import ssl
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

from .contracts import NetworkError, require, decode, canonical, fields, base, integer, verifier


class HubServer(ThreadingHTTPServer):
    daemon_threads=False  # server_close joins requests before releasing migration lease.
    block_on_close=True
    allow_reuse_address=True

    def __init__(self,address,store,*,tls_context,background_statistics=False):
        from ..ratings.hub_migration import require_settled
        require_settled(store)
        self.store=store;self.capacity=threading.BoundedSemaphore(8)
        self.requests=deque();self.quota_lock=threading.Lock()
        from ..activity.repository import Repository as Activity
        from ..activity.replica import Publisher as Statistics
        self.statistics=Statistics(Activity(store),background=background_statistics)
        super().__init__(address,Handler)
        require(isinstance(tls_context,ssl.SSLContext),'https_required')
        self.socket=tls_context.wrap_socket(self.socket,server_side=True)

    def server_close(self):
        try:super().server_close()
        finally:self.statistics.close()

    def get_request(self):
        # Socket handshake/slow clients must not stop accept indefinitely.
        self.socket.settimeout(2)
        connection,address=super().get_request();connection.settimeout(2)
        return connection,address

    def process_request(self,request,client_address):
        if not self.capacity.acquire(blocking=False):self.shutdown_request(request);return
        try:super().process_request(request,client_address)
        except BaseException:self.capacity.release();raise

    def process_request_thread(self,request,client_address):
        try:super().process_request_thread(request,client_address)
        finally:self.capacity.release()

    def handle_error(self,request,client_address):pass

    def admission(self):
        with self.quota_lock:
            now=time.monotonic()
            while self.requests and self.requests[0]<now-60:self.requests.popleft()
            require(len(self.requests)<240,'hub_rate_limited',429);self.requests.append(now)


class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.0'
    server_version='PoolRotaterM3'
    sys_version=''

    def log_message(self,*args):pass  # No IP, URL, credentials, bodies or private IDs in logs.
    def do_GET(self):self.dispatch('GET')
    def do_POST(self):self.dispatch('POST')

    def token(self):
        headers=self.headers.get_all('Authorization',[])
        require(len(headers)==1 and headers[0].startswith('Bearer '),'credential_required',401)
        token=headers[0][7:];verifier(token);return token

    def body(self, maximum=65536):
        require(self.headers.get_all('Transfer-Encoding') is None,'transfer_encoding_refused')
        require(self.headers.get_all('Content-Type',[])==['application/json'],'json_content_type_required',415)
        values=self.headers.get_all('Content-Length',[])
        require(len(values)==1 and values[0].isdigit(),'content_length_required',411)
        length=int(values[0]);integer(length,1,maximum)
        raw=self.rfile.read(length);require(len(raw)==length,'incomplete_request');return decode(raw, maximum=maximum)

    def dispatch(self,method):
        html=None;target=None
        try:
            require(len(self.path)<=256,'invalid_route')
            target=urlsplit(self.path);require(not target.scheme and not target.netloc and not target.fragment,'invalid_route')
            path=target.path;store=self.server.store
            # Enrolled data transfers have no requests/minute or bandwidth cap.
            # Public/control routes retain admission protection and all requests
            # retain bounded size, concurrency, deadlines and authentication.
            bulk=path.startswith(('/v1/activity/','/v1/logs/','/v1/ratings/','/v2/ratings/','/v2/activity/')) or path in ('/v1/events/batches','/v1/transfers/capabilities','/v1/hosts/self/managed-settings')
            if bulk:
                with store.transaction() as db:store._authenticate(db,verifier(self.token()),'heartbeat')
            else:self.server.admission()
            if method=='GET' and path=='/v1/transfers/capabilities':
                from .transfer_policy import CAPABILITY
                require(not target.query,'unexpected_query');result=dict(CAPABILITY)
            elif method=='GET' and path=='/v2/capabilities':
                from .display import CAPABILITIES
                require(not target.query,'unexpected_query');result=dict(CAPABILITIES)
            elif method=='GET' and path=='/v1/capabilities':
                require(not target.query,'unexpected_query');result=store.capabilities()
            elif method=='GET' and path in ('/v1/rooms','/v2/rooms','/directory'):
                params=parse_qs(target.query,keep_blank_values=True,strict_parsing=True)
                require(set(params)<={'offset','limit'} and all(len(v)==1 and v[0].isdigit() and len(v[0])<=7 for v in params.values()),'invalid_pagination')
                result=store.directory(offset=int(params.get('offset',['0'])[0]),limit=int(params.get('limit',['50'])[0]),protocol_version=2 if path=='/v2/rooms' else 1)
                if path=='/directory':
                    from .directory import render
                    html=render(result)
            elif method=='GET' and path in ('/v1/activity/replica/current','/v2/activity/replica/current'):
                require(not target.query,'unexpected_query');result=self.server.statistics.current(self.token(),include_catch=path.startswith('/v2/'))
            elif method=='GET' and path in ('/v1/activity/replica/page','/v2/activity/replica/page'):
                params=parse_qs(target.query,keep_blank_values=True,strict_parsing=True)
                require(set(params)=={'content_id','index'} and all(len(v)==1 for v in params.values())
                        and params['index'][0].isdigit() and len(params['index'][0])<=4,'invalid_snapshot_query')
                result=self.server.statistics.read_page(self.token(),params['content_id'][0],int(params['index'][0]),include_catch=path.startswith('/v2/'))
            elif method=='GET' and path=='/v1/hosts/self':
                require(not target.query,'unexpected_query');result=store.status(self.token())
            elif method=='GET' and path in ('/v1/ratings/snapshots/current','/v1/ratings/snapshot-page',
                                              '/v2/ratings/snapshots/current','/v2/ratings/snapshot-page'):
                from ..ratings.repository import Repository
                from ..ratings.snapshots import Publisher,digest
                repository=Repository(store)
                with store.transaction() as db:repository._authorized(db,self.token(),'ratings-read')
                publisher=Publisher(repository)
                context=path.startswith('/v2/')
                if path.endswith('/snapshots/current'):
                    require(not target.query,'unexpected_query');result=publisher.current(context=context)
                else:
                    params=parse_qs(target.query,keep_blank_values=True,strict_parsing=True)
                    require(set(params)=={'content_id','index'} and all(len(v)==1 for v in params.values())
                            and params['index'][0].isdigit() and len(params['index'][0])<=3,'invalid_snapshot_query')
                    result=publisher.read_page(digest(params['content_id'][0]),int(params['index'][0]),context=context)
            elif method=='POST':
                require(not target.query,'unexpected_query')
                if path=='/v1/enrollments':
                    require(not self.headers.get_all('Authorization'),'unexpected_authorization');result=store.enroll(self.body())
                elif path in ('/v1/hosts/self/heartbeat','/v2/hosts/self/heartbeat'):result=store.heartbeat(self.token(),self.body(),protocol_version=2 if path.startswith('/v2/') else 1)
                elif path=='/v1/hosts/self/token-rotation':result=store.rotate(self.token(),self.body())
                elif path=='/v1/hosts/self/leave':
                    token=self.token();value=self.body();fields(value,('protocol_version',));base(value);result=store.leave(token)
                elif path=='/v1/hosts/self/managed-rooms':
                    from ..managed_host.hub import Repository
                    result=Repository(store).accept(self.token(),self.body())
                elif path=='/v1/hosts/self/managed-settings':
                    from ..managed_host.details import Repository
                    result=Repository(store).accept(self.token(),self.body())
                elif path=='/v1/hosts/self/discord':
                    from ..discord_relay.repository import Repository
                    result=Repository(store).accept(self.token(),self.body())
                elif path=='/v1/hosts/self/updates':
                    from ..updates.telemetry import Reports
                    result=Reports(store).accept(self.token(),self.body())
                elif path=='/v1/hosts/self/recovery':
                    from ..rescue.repository import Repository
                    result=Repository(store).accept(self.token(),self.body())
                elif path in ('/v1/activity/consent','/v1/activity/events','/v1/activity/query'):
                    from ..activity.repository import Repository as Activity
                    repository=Activity(store);token=self.token();body=self.body()
                    result=(repository.consent(token,body) if path.endswith('/consent') else
                            repository.query(token,body) if path.endswith('/query') else repository.ingest(token,body))
                elif path in ('/v1/logs/consent','/v1/logs/chunks'):
                    from ..diagnostic_logs import HubLogs
                    repository=HubLogs(store);token=self.token();body=self.body()
                    result=repository.consent(token,body) if path.endswith('/consent') else repository.ingest(token,body)
                elif path in ('/v1/events/batches','/v1/ratings/state-query','/v1/ratings/stream-state'):
                    from ..ratings.repository import Repository
                    repository=Repository(store)
                    token=self.token()
                    if path=='/v1/events/batches':result=repository.ingest(token,self.body(512*1024))
                    elif path=='/v1/ratings/state-query':result=repository.state_query(token,self.body())
                    else:result=repository.stream_state(token,self.body())
                else:raise NetworkError('route_not_found',404)
            else:raise NetworkError('route_not_found',404)
            status=200
        except NetworkError as error:status=error.status;result={'ok':False,'error':error.code}
        except (ValueError,socket.timeout):status=400;result={'ok':False,'error':'invalid_request'}
        except Exception:status=503;result={'ok':False,'error':'hub_unavailable'}
        raw=html if status==200 and html is not None else canonical(result)
        compressed=False
        if status==200 and target is not None and target.path.startswith(('/v1/activity/replica/','/v2/activity/replica/')) and self.headers.get('Accept-Encoding')=='gzip':
            import gzip
            raw=gzip.compress(raw,compresslevel=1,mtime=0);compressed=True
        try:
            self.send_response(status);self.send_header('Content-Type','text/html; charset=utf-8' if status==200 and html is not None else 'application/json');self.send_header('Content-Length',str(len(raw)))
            if compressed:self.send_header('Content-Encoding','gzip')
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Request-ID',str(uuid.uuid4()))
            self.send_header('Content-Security-Policy',"default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            if status==429:self.send_header('Retry-After','5')
            self.end_headers();self.wfile.write(raw)
        except OSError:pass
        self.close_connection=True


def tls(cert,key):
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert,key);return context
