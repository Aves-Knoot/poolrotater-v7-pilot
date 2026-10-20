"""Owned DNS/TLS/HTTP deadline, shared pacing and trust snapshot qualification."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from email.message import Message
import http.client
import json
import multiprocessing
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from poolrotater_host.layout import atomic_json, read_json, setup
from poolrotater_host.oauth import AuthError, ClientCredentials, OfflineOAuth
from poolrotater_host import provider_transport as transport
from poolrotater_host.provider_contract import OWNED_PATH
from poolrotater_host.ownership_recovery import OfflineOwnershipRecovery, digest, require_settled
from poolrotater_host.testing.provider_https import TLSProvider, certificates


def stalled_dns(pipe, port, trust, request, timeout, parent_pid):
    # Actual spawned process stalls inside getaddrinfo, before connect or HTTP.
    with patch('socket.getaddrinfo', side_effect=lambda *a, **k: time.sleep(30)):
        transport._worker(pipe, port, trust, request, timeout, parent_pid)


def partial_frame(pipe, *args):
    os.write(pipe.fileno(), b'\x00\x00\x01\x00')
    time.sleep(30)


class DeadlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='deadline-tls-')
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.certs = Path(cls.tmp.name); certificates(cls.certs)
        cls.trust = transport.TrustSnapshot((cls.certs/'ca.crt').read_bytes())

    def setUp(self):
        self.provider = TLSProvider(self.certs).__enter__()
        self.addCleanup(self.provider.__exit__, None, None, None)
        self.origin = 'https://127.0.0.1:'+str(self.provider.server.server_port)
        self.client = transport.DeadlineFixtureTransport(self.origin, self.trust)
        self.children = {p.pid for p in multiprocessing.active_children()}

    def tearDown(self):
        self.assertEqual({p.pid for p in multiprocessing.active_children()}, self.children)

    def code(self, expected, function):
        with self.assertRaises(AuthError) as error: function()
        self.assertEqual(error.exception.code, expected)
        self.assertNotIn('invented', str(error.exception))
        self.assertIsNone(error.exception.__cause__)

    def get(self, path='/api/v2/me'):
        return self.client.request('GET', path, bearer='invented-secret')

    def fault_deadline(self, worker):
        self.client.timeout = .4
        started = time.monotonic()
        with patch.object(transport, '_worker', worker):
            self.code('provider_deadline_exceeded', self.get)
        self.assertLess(time.monotonic()-started, 1.4)
        self.assertEqual(self.provider.calls, [])

    def test_actual_dns_stall_is_killed_reaped_and_never_sends_http(self):
        self.fault_deadline(stalled_dns)

    def test_partial_child_frame_cannot_escape_deadline(self):
        self.fault_deadline(partial_frame)

    def test_queue_and_pacing_use_same_deadline_without_dispatch(self):
        self.client.gate = transport.RequestGate(minimum_interval=1)
        self.get(); self.client.timeout = .2
        self.code('provider_pacing_deferred', self.get)
        self.assertEqual(self.client.gate.snapshot()['dispatched'], 1)
        self.client.gate._lock.acquire()
        try: self.code('provider_deadline_exceeded', self.get)
        finally: self.client.gate._lock.release()

    def test_shared_gate_serializes_concurrent_adapters_and_spaces_calls(self):
        second = transport.DeadlineFixtureTransport(self.origin, self.trust)
        self.assertIs(second.gate, self.client.gate)
        seen = []; failures = []
        self.provider.on_read = lambda path: seen.append(time.monotonic())
        self.provider.rooms = []
        self.client.gate.interval = .3
        def run(client):
            try: client.request('GET', OWNED_PATH, bearer='invented-secret')
            except Exception as e: failures.append(e)
        threads = [threading.Thread(target=run, args=(c,)) for c in (self.client, second)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(4)
        self.assertFalse(failures); self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(len(seen),2); self.assertGreaterEqual(seen[1]-seen[0], .23)
        self.assertEqual(self.client.gate.snapshot(), {'attempted':2,'dispatched':2,'rate_limited':0})

    def test_429_html_cools_all_endpoints_and_clients_without_retry(self):
        self.provider.wire('/api/v2/me',429,b'invented-secret',headers=[('Retry-After','3'),('Content-Type','text/html')])
        self.assertEqual(self.get(),(429,None))
        second=transport.DeadlineFixtureTransport(self.origin,self.trust,timeout=.2)
        for method,path,kwargs in [('GET',OWNED_PATH,{'bearer':'invented'}),
              ('POST','/oauth/token',{'form':{'grant_type':'refresh_token','client_id':123,
               'client_secret':'invented','refresh_token':'invented'}}),
              ('DELETE','/api/v2/oauth/tokens/current',{'bearer':'invented'})]:
            self.code('provider_pacing_deferred',lambda:second.request(method,path,**kwargs))
        self.assertEqual(second.gate.snapshot(),{'attempted':4,'dispatched':1,'rate_limited':1})

    def test_long_retry_after_halts_instead_of_retrying_early(self):
        self.provider.wire('/api/v2/me',429,headers=[('Retry-After','999999999999999999999')])
        self.assertEqual(self.get(),(429,None));self.client.timeout=.1
        self.code('provider_pacing_deferred',self.get)

    def test_discarding_adapters_cannot_discard_shared_cooldown(self):
        self.provider.wire('/api/v2/me',429,headers=[('Retry-After','3')])
        self.get();del self.client
        self.client=transport.DeadlineFixtureTransport(self.origin,self.trust,timeout=.1)
        self.code('provider_pacing_deferred',self.get)

    def test_spawn_failure_has_redacted_error_and_no_live_child(self):
        with patch('multiprocessing.process.BaseProcess.start',side_effect=OSError('invented-secret')):
            self.code('provider_transport_uncertain',self.get)

    def test_oauth_tls_refresh_and_revoke_use_shared_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'host';setup(root)
            auth=OfflineOAuth(root,ClientCredentials(123,'invented-client-secret'),self.client)
            state=parse_qs(urlsplit(auth.begin('http://127.0.0.1:51111/oauth/callback')).query)['state'][0]
            auth.complete('/oauth/callback?code='+self.provider.code()+'&state='+state,'127.0.0.1:51111')
            old=read_json(auth.token_path);old['expires_at']=1;atomic_json(auth.token_path,old)
            with auth.verified_owner():pass
            auth.revoke()
            self.assertEqual(read_json(auth.token_path)['phase'],'revoked')
            self.assertEqual(self.client.gate.snapshot()['dispatched'],5)

    def test_timeout_after_oauth_rotation_remains_durably_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'host';setup(root)
            auth=OfflineOAuth(root,ClientCredentials(123,'invented-client-secret'),self.client)
            state=parse_qs(urlsplit(auth.begin('http://127.0.0.1:51111/oauth/callback')).query)['state'][0]
            self.provider.code();self.client.timeout=.4
            # Mutation takes place but the provider delays its reply beyond deadline.
            original=self.provider.handle
            def delayed(*args):
                result=original(*args);time.sleep(.6);return result
            self.provider.handle=delayed
            self.code('transport_reply_uncertain',lambda:auth.complete(
                '/oauth/callback?code=invented-code&state='+state,'127.0.0.1:51111'))
            self.assertEqual(read_json(auth.pending_path)['phase'],'exchange_pending')
            self.code('authorization_reply_lost_reauthorize',auth.recover_authorization)
            self.assertEqual(len(self.provider.calls),1)

    def test_known_ended_recovery_uses_deadline_adapter_and_shared_read_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'host';setup(root)
            path=root/'state/rolling/ownership.json';path.parent.mkdir(mode=0o700)
            journal={'format':1,'rooms':{'room1':{'room_id':17,'operation':'owned'}}}
            atomic_json(path,journal)
            auth=OfflineOAuth(root,ClientCredentials(123,'invented-client-secret'),self.client)
            state=parse_qs(urlsplit(auth.begin('http://127.0.0.1:51111/oauth/callback')).query)['state'][0]
            auth.complete('/oauth/callback?code='+self.provider.code()+'&state='+state,'127.0.0.1:51111')
            self.provider.details['17']=(200,{'id':17,'user_id':99,'type':'head_to_head',
                'active':False,'ends_at':'2000-01-01T00:00:00Z'})
            result=OfflineOwnershipRecovery(auth).apply(expected_journal_sha256=digest(journal))
            self.assertTrue(result['recovery_applied']);self.assertFalse(result['live_ready'])
            require_settled(root)
            self.assertEqual(self.client.gate.snapshot()['dispatched'],7)
            self.assertTrue(all(call['method']=='GET' for call in self.provider.calls[2:]))

    def test_wrong_hostname_and_untrusted_ca_refuse_before_http(self):
        with TLSProvider(self.certs,wrong_host=True) as provider:
            client=transport.DeadlineFixtureTransport('https://127.0.0.1:'+str(provider.server.server_port),self.trust)
            self.code('provider_transport_uncertain',lambda:client.request('GET','/api/v2/me',bearer='invented'))
            self.assertEqual(provider.calls,[])
        with tempfile.TemporaryDirectory() as other:
            certificates(other)
            self.client.trust=transport.TrustSnapshot((Path(other)/'ca.crt').read_bytes())
            self.code('provider_transport_uncertain',self.get)
            self.assertEqual(self.provider.calls,[])

    def test_tls_handshake_headers_and_trickled_body_have_one_deadline(self):
        # Real socket peer stalls three different protocol stages. Each lasts 2 s
        # without cancellation; short per-byte sleeps would defeat per-I/O limits.
        for phase in ('handshake','headers','body'):
            with self.subTest(phase=phase):
                listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen(1)
                done=threading.Event();errors=[]
                context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(self.certs/'server.crt',self.certs/'server.key')
                def peer():
                    connection=None
                    try:
                        connection,_=listener.accept();connection.settimeout(2)
                        if phase=='handshake':done.wait(2);return
                        connection=context.wrap_socket(connection,server_side=True)
                        connection.recv(32768)
                        wire=b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 102\r\n\r\n'
                        if phase=='body':connection.sendall(wire);wire=b' '+b'{}'+b' '*99
                        for byte in wire:
                            if done.wait(.015):break
                            connection.sendall(bytes([byte]))
                    except (OSError,ssl.SSLError):pass
                    except Exception as e:errors.append(e)
                    finally:
                        if connection:connection.close()
                thread=threading.Thread(target=peer);thread.start()
                self.client=transport.DeadlineFixtureTransport('https://127.0.0.1:'+str(listener.getsockname()[1]),self.trust,timeout=.4)
                try:
                    started=time.monotonic();self.code('provider_deadline_exceeded',self.get)
                    self.assertLess(time.monotonic()-started,1.4)
                finally:done.set();listener.close();thread.join(3)
                self.assertFalse(thread.is_alive());self.assertFalse(errors)

    def test_interruption_after_spawn_always_reaps_child(self):
        with patch('multiprocessing.connection.Connection.poll',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.get()

    def test_killed_requester_does_not_leave_network_worker_running(self):
        original=self.provider.handle
        waiting=threading.Event();finished=threading.Event()
        def delayed(*args):
            result=original(*args);waiting.set();finished.wait(3);return result
        self.provider.handle=delayed
        with tempfile.TemporaryDirectory() as tmp:
            script=Path(tmp)/'requester.py'
            script.write_text('''
from pathlib import Path
from poolrotater_host.provider_transport import DeadlineFixtureTransport, TrustSnapshot
if __name__ == '__main__':
    client=DeadlineFixtureTransport(ORIGIN, TrustSnapshot(Path(CA).read_bytes()),timeout=5)
    client.request('GET','/api/v2/me',bearer='invented-secret')
'''.replace('ORIGIN',repr(self.origin)).replace('CA',repr(str(self.certs/'ca.crt'))))
            process=subprocess.Popen([sys.executable,str(script)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            children=[]
            try:
                self.assertTrue(waiting.wait(3))
                children=[int(p) for p in Path(f'/proc/{process.pid}/task/{process.pid}/children').read_text().split()]
                # spawn also owns a stdlib resource tracker; identify the network
                # worker explicitly and check both children stop after parent death.
                workers=[pid for pid in children if b'spawn_main' in Path(f'/proc/{pid}/cmdline').read_bytes()]
                self.assertEqual(len(workers),1)
                process.kill();process.wait(1)
                deadline=time.monotonic()+1
                def running(pid):
                    try:return Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[0]!='Z'
                    except FileNotFoundError:return False
                while any(running(pid) for pid in children) and time.monotonic()<deadline:time.sleep(.01)
                self.assertFalse(any(running(pid) for pid in children))
            finally:
                if process.poll() is None:process.kill()
                process.wait(1);finished.set()

    def test_origin_env_request_and_timeout_checks_precede_child(self):
        for origin in ('https://osu.ppy.sh','http://127.0.0.1:44','https://127.0.0.1:44/a',
                       'https://user@127.0.0.1:44','https://localhost:44',
                       'https://127.0.0.1:invented-secret','https://127.0.0.1:99999'):
            self.code('offline_verified_tls_required',lambda:transport.DeadlineFixtureTransport(origin,self.trust))
        with patch.dict(os.environ,{'POOLROTATER_OFFLINE_SANDBOX':'0'}):
            self.code('offline_verified_tls_required',self.get)
        for timeout in (True,0,-1,6,float('nan'),float('inf')):
            self.client.timeout=timeout;self.code('invalid_provider_timeout',self.get)
        self.client.timeout=2
        self.code('provider_endpoint_refused',lambda:self.get('https://attacker.invalid'))
        self.assertEqual(self.provider.calls,[])

    def test_proxy_environment_is_ignored(self):
        with patch.dict(os.environ,{'HTTPS_PROXY':'https://attacker.invalid','ALL_PROXY':'https://attacker.invalid'}):
            self.assertEqual(self.get(),(401,None))

    def test_large_room_reply_crosses_pipe_without_blocking_or_leaks(self):
        body=json.dumps([{'id':i,'extra':'x'*3000} for i in range(250)]).encode()
        self.provider.wire(OWNED_PATH,200,body)
        self.assertEqual(len(self.get(OWNED_PATH)[1]),250)


class PolicyTests(unittest.TestCase):
    def test_retry_after_numeric_date_duplicate_invalid_and_excessive(self):
        for values,expected in [([],60),(['0'],0),(['3'],3),(['bad'],60),(['-3'],60),
              (['1','2'],60),(['100000'],None),(['Thu, 01 Jan 1970 00:00:10 GMT'],10),
              (['Thu, 01 Jan 1970 00:00:00 GMT'],0),(['9'*81],None)]:
            headers=Message()
            for value in values:headers['Retry-After']=value
            self.assertEqual(transport.retry_after(headers,now=0),expected)

    def test_private_trust_empty_invalid_oversized_and_no_environment_fallback(self):
        for pem in (b'',b'invalid',b'x'*(transport.MAX_TRUST+1),'not bytes',b'-----BEGIN PRIVATE KEY-----\nAAA=\n-----END PRIVATE KEY-----'):
            with self.assertRaises(AuthError):transport.TrustSnapshot(pem).context()
        with patch('ssl.get_default_verify_paths') as paths:
            paths.return_value.openssl_cafile='/nonexistent/invented'
            with self.assertRaises(AuthError):transport.TrustSnapshot.system()

    def test_system_trust_ignores_environment_and_reports_only_summary(self):
        # /etc/ssl is intentionally hidden by scripts/offline: use an invented
        # compiled CA location here. Real system inventory is a separate RO probe.
        with tempfile.TemporaryDirectory() as tmp:
            certificates(tmp);path=str(Path(tmp)/'ca.crt')
            with patch('ssl.get_default_verify_paths') as paths, patch.dict(os.environ,
                  {'SSL_CERT_FILE':'/attacker/invalid','SSL_CERT_DIR':'/attacker/invalid'}):
                paths.return_value.openssl_cafile=path
                snapshot=transport.TrustSnapshot.system()
                summary=snapshot.summary()
                self.assertEqual(summary['ca_count'],1)
                self.assertFalse(summary['environment_overrides_used'])
                self.assertNotIn('BEGIN',repr(snapshot)+json.dumps(summary))

    def test_pacing_configuration_and_inherited_gate_refuse(self):
        for interval in (True,0,-1,61,float('nan'),float('inf')):
            with self.assertRaises(AuthError):transport.RequestGate(minimum_interval=interval)
        gate=transport.RequestGate();gate._pid=-1
        with self.assertRaises(AuthError):
            with gate.admission(time.monotonic()+1):pass


if __name__=='__main__':unittest.main()
