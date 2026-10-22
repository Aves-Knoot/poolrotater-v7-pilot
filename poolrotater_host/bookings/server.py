"""Dedicated loopback portal listener: browser polling cannot consume hub slots."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import threading
from .web import Portal

class PortalServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,portal,*,fixture=False):
        if not fixture and address[0]not in('127.0.0.1','::1'):raise ValueError('booking_portal_loopback_required')
        self.portal=portal;self.capacity=threading.BoundedSemaphore(16)
        super().__init__(address,Handler)
    def process_request(self,request,address):
        if not self.capacity.acquire(blocking=False):self.shutdown_request(request);return
        request.settimeout(15)
        try:super().process_request(request,address)
        except BaseException:self.capacity.release();raise
    def process_request_thread(self,*args):
        try:super().process_request_thread(*args)
        finally:self.capacity.release()
    def handle_error(self,*args):pass

class Handler(BaseHTTPRequestHandler):
    server_version='PoolRotaterBooking'
    sys_version=''
    def log_message(self,*args):pass
    def do_GET(self):self.dispatch('GET')
    def do_POST(self):self.dispatch('POST')
    def dispatch(self,method):
        portal=self.server.portal
        headers={k:self.headers[k]for k in('Cookie','Origin','Content-Type','X-CSRF-Token','Accept')if self.headers.get(k)is not None}
        try:
            if any(len(self.headers.get_all(k,[]))>1 for k in('Cookie','Origin','Content-Type','X-CSRF-Token','Content-Length')):raise ValueError()
            raw=b''
            if method=='POST':
                length=self.headers.get('Content-Length','')
                if not length.isdigit()or not 1<=int(length)<=16384 or self.headers.get('Transfer-Encoding'):raise ValueError()
                raw=self.rfile.read(int(length))
                if len(raw)!=int(length):raise ValueError()
            result=portal.handle(method,self.path,headers,raw)
        except Exception:result=portal._response({'ok':False,'error':'booking_invalid_request'},400)
        try:
            self.send_response(result.status)
            for key,value in result.headers:self.send_header(key,value)
            self.send_header('Content-Length',str(len(result.body)));self.end_headers();self.wfile.write(result.body)
        except OSError:pass
        self.close_connection=True
