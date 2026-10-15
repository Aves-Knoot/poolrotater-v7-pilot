"""Bounded read-only HTTPS catalog origin; no directory or private-key routes.

The owner publisher replaces timestamp atomically. Every request retains an open
descriptor, so an in-flight old response remains internally consistent.
"""
from collections import OrderedDict
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import ssl
import stat
import threading

from .config import ConfigError
from .catalog_export import MAX_FILE
from .catalog_trust import no_links, ROOT_LIMIT, METADATA_LIMIT


def route(value):
    if type(value) is not str or len(value)>1024 or not value.startswith('/'):
        raise ConfigError('catalog_route_refused')
    name=value[1:]
    pieces=name.split('/')
    if any(p in ('','.','..') or p.startswith('.') for p in pieces):
        raise ConfigError('catalog_route_refused')
    if re.fullmatch(r'metadata/(timestamp\.json|[1-9][0-9]{0,9}\.(root|targets|snapshot|timestamp)\.json)',name):
        return name,ROOT_LIMIT if name.endswith('.root.json') else METADATA_LIMIT
    if re.fullmatch(r'targets/([a-f0-9]{64}\.exclusions\.json|(standard|mania4|mania7)/[a-f0-9]{64}\.(manifest\.json|bundle\.tar\.gz|catalog\.db|dt\.db|farm\.db|converts\.db))',name):
        return name,MAX_FILE
    raise ConfigError('catalog_route_refused')


class CatalogServer(ThreadingHTTPServer):
    """Eight bounded workers, streamed bytes, bounded digest cache and timeouts."""
    daemon_threads=False
    block_on_close=True
    request_queue_size=8
    route = staticmethod(route)

    def __init__(self,root,address,*,context=None,protected_uid=None):
        self.root=no_links(root);info=self.root.stat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode)&0o022
                or protected_uid is not None and info.st_uid!=protected_uid):
            raise ConfigError('catalog_public_root_refused')
        if set(p.name for p in self.root.iterdir())!={'metadata','targets'}:
            raise ConfigError('catalog_public_root_refused')
        self.protected_uid=protected_uid;self.context=context
        self.slots=threading.BoundedSemaphore(8);self.cache=OrderedDict();self.cache_lock=threading.Lock()
        self.root_fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:super().__init__(address,CatalogHandler)
        except BaseException:os.close(self.root_fd);raise

    def cache_control(self,name):
        return 'no-cache'if name=='metadata/timestamp.json'else'public, max-age=86400, immutable'

    def get_request(self):
        incoming,address=super().get_request();incoming.settimeout(10)
        # Defer TLS handshake to a bounded worker rather than the accept loop.
        return incoming,address

    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request);return
        try:super().process_request(request,address)
        except BaseException:self.slots.release();raise

    def process_request_thread(self,request,address):
        try:
            if self.context is not None:
                request=self.context.wrap_socket(request,server_side=True)
                request.settimeout(10)
            super().process_request_thread(request,address)
        except (OSError,ssl.SSLError):self.shutdown_request(request)
        finally:self.slots.release()

    def handle_error(self,*args):
        # A disconnect is ordinary. Tracebacks can expose local filesystem paths.
        pass

    def open_target(self,name,limit):
        current=os.dup(self.root_fd)
        try:
            pieces=name.split('/')
            for piece in pieces[:-1]:
                child=os.open(piece,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=current)
                os.close(current);current=child
                info=os.fstat(current)
                if stat.S_IMODE(info.st_mode)&0o022 or self.protected_uid is not None and info.st_uid!=self.protected_uid:
                    raise ConfigError('catalog_public_file_refused')
            fd=os.open(pieces[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=current)
            handle=os.fdopen(fd,'rb')
            info=os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or not 0<info.st_size<=limit
                    or stat.S_IMODE(info.st_mode)&0o022
                    or self.protected_uid is not None and info.st_uid!=self.protected_uid):
                handle.close();raise ConfigError('catalog_public_file_refused')
            return handle,info
        finally:os.close(current)

    def etag(self,handle,info):
        key=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
        with self.cache_lock:
            if key in self.cache:self.cache.move_to_end(key);return self.cache[key]
        digest=hashlib.sha256()
        while chunk:=handle.read(1024*1024):digest.update(chunk)
        handle.seek(0);tag='"'+digest.hexdigest()+'"'
        with self.cache_lock:
            self.cache[key]=tag
            while len(self.cache)>256:self.cache.popitem(last=False)
        return tag

    def server_close(self):
        try:super().server_close()
        finally:
            if self.root_fd is not None:os.close(self.root_fd);self.root_fd=None


class CatalogHandler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    server_version='PoolRotaterCatalog'
    sys_version=''

    def log_message(self,*args):pass

    def parse_request(self):
        result=super().parse_request()
        if result:
            # BaseHTTPRequestHandler normalizes leading //; preserve the raw
            # target for our explicit route allowlist instead.
            pieces=self.requestline.split()
            if len(pieces)>1 and pieces[1].startswith('//'):self.path=pieces[1]
        return result

    def send_error(self,code,*args):self.error(code)

    def error(self,code):
        self.send_response(code);self.send_header('Content-Length','0')
        self.send_header('Connection','close');self.end_headers();self.close_connection=True

    def do_GET(self):self.transfer(head=False)
    def do_HEAD(self):self.transfer(head=True)
    def do_POST(self):self.error(405)
    def do_PUT(self):self.error(405)
    def do_DELETE(self):self.error(405)
    def do_OPTIONS(self):self.error(405)

    def transfer(self,*,head):
        try:name,limit=self.server.route(self.path)
        except ConfigError:self.error(404);return
        try:handle,info=self.server.open_target(name,limit)
        except (OSError,ConfigError):self.error(404);return
        with handle:
            etag=self.server.etag(handle,info);size=info.st_size
            if self.headers.get('If-None-Match')==etag:
                self.send_response(304);self.send_header('ETag',etag);self.end_headers();return
            start,end=0,size-1;partial=False
            value=self.headers.get('Range')
            if value and self.headers.get('If-Range',etag)==etag:
                match=re.fullmatch(r'bytes=([0-9]{1,20})-([0-9]{1,20})?',value)
                if match:
                    start=int(match[1]);end=min(int(match[2]) if match[2] else size-1,size-1)
                if not match or start>=size or end<start:
                    self.send_response(416);self.send_header('Content-Range',f'bytes */{size}')
                    self.send_header('Content-Length','0');self.end_headers();return
                partial=True
            self.send_response(206 if partial else 200)
            self.send_header('Content-Length',str(end-start+1));self.send_header('ETag',etag)
            self.send_header('Accept-Ranges','bytes');self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Type','application/json' if name.endswith('.json') else 'application/octet-stream')
            self.send_header('Cache-Control',self.server.cache_control(name))
            if partial:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
            self.end_headers()
            if head:return
            handle.seek(start);remaining=end-start+1
            try:
                while remaining:
                    chunk=handle.read(min(1024*1024,remaining))
                    if not chunk:break
                    self.wfile.write(chunk);remaining-=len(chunk)
            except (BrokenPipeError,ConnectionResetError,TimeoutError):pass


def tls_context(cert,key):
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert),str(key));return context


def serve(root,bind,port,cert,key,*,protected_uid=0):
    import signal
    server=CatalogServer(root,(bind,port),context=tls_context(cert,key),protected_uid=protected_uid)
    stop=threading.Event()
    def requested(*args):stop.set()
    old={kind:signal.signal(kind,requested) for kind in (signal.SIGTERM,signal.SIGINT)}
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.1});thread.start()
    try:stop.wait()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=12)
        for kind,handler in old.items():signal.signal(kind,handler)
