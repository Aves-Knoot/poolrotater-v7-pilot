"""Browser identity and request boundaries using invented accounts only."""
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from poolrotater_host.bookings.auth import Sessions
from poolrotater_host.bookings.web import Portal,COOKIE,OAUTH_COOKIE
from poolrotater_host.bookings.repository import Repository
from poolrotater_host.bookings.settings import validate_player_patch
from poolrotater_host.bookings.preview import PreviewProvider
from poolrotater_host.network.contracts import NetworkError

class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)/'private';self.now=1000
        self.binding={'host_id':str(uuid.uuid4()),'installation_id':str(uuid.uuid4()),'generation':str(uuid.uuid4())}
        self.repo=Repository(root,clock=lambda:self.now,eligibility=lambda h,a:{**self.binding,'fresh':True,'supported':True,'effective_limit':4,'capacity_used':0,'observed_booking_ids':[],'supported_presets':['standard-fixed']})
        self.repo.configure_slot('test-slot',self.binding['host_id'],'room1','Private slot',['standard-fixed'],{'sr_min':3,'sr_max':4},enabled=True)
        class OAuth:
            def authorization_url(self,state):return'https://example.invalid/authorize?state='+state
            def exchange(self,code):
                if code!='invented-valid-code':raise NetworkError('booking_oauth_code_refused')
                return{'id':123,'username':'Invented player'}
        self.portal=Portal(root,self.repo,origin='https://example.invalid',oauth=OAuth(),enabled=True,clock=lambda:self.now)

    def login(self):
        pending=self.portal.sessions.begin();self.portal.sessions.consume(pending['state'],pending['browser'])
        token=self.portal.sessions.create({'id':123,'username':'Invented player'})
        session=self.portal.sessions.get(token)
        return {'Cookie':COOKIE+'='+token,'Origin':'https://example.invalid','Content-Type':'application/json','X-CSRF-Token':session['csrf_token']}

    def test_oauth_is_browser_bound_and_single_use(self):
        result=self.portal.handle('GET','/private-lobbies/oauth/start',{})
        self.assertEqual(result.status,302);headers=dict(result.headers)
        state=parse_qs(urlsplit(headers['Location']).query)['state'][0]
        cookie=headers['Set-Cookie'].split(';')[0]
        query='/private-lobbies/oauth/callback?state='+state+'&code=invented-valid-code'
        self.assertEqual(self.portal.handle('GET',query,{}).status,403)
        result=self.portal.handle('GET',query,{'Cookie':cookie});self.assertEqual(result.status,303)
        self.assertEqual(self.portal.handle('GET',query,{'Cookie':cookie}).status,403)
        self.assertNotIn(b'access_token',result.body)

    def test_mutations_require_identity_origin_csrf_and_json(self):
        body=json.dumps({'request_id':str(uuid.uuid4()),'slot_id':'test-slot','preset':'standard-fixed','settings':{},'designated_friend_id':None}).encode()
        headers=self.login()
        for key,status in [('Cookie',401),('Origin',403),('X-CSRF-Token',403),('Content-Type',415)]:
            altered=dict(headers);altered.pop(key)
            self.assertEqual(self.portal.handle('POST','/private-lobbies/api/reserve',altered,body).status,status,key)
        result=self.portal.handle('POST','/private-lobbies/api/reserve',headers,body)
        self.assertEqual(result.status,201,result.body)
        booking=json.loads(result.body)['booking'];self.assertEqual(booking['role'],'owner')
        other=self.portal.sessions.create({'id':999,'username':'Other invented'})
        denied=self.portal.handle('GET','/private-lobbies/api/session?id='+booking['id'],{'Cookie':COOKIE+'='+other})
        self.assertEqual(denied.status,403)
        public=self.portal.handle('GET','/private-lobbies/api/slots',{})
        for secret in ('password','room_id','players','join_url','user_id'):self.assertNotIn(secret.encode(),public.body)

    def test_disabled_feature_cannot_sign_in_or_reserve(self):
        self.portal.enabled=False
        self.assertEqual(self.portal.handle('GET','/private-lobbies/oauth/start',{}).status,503)
        self.assertEqual(self.portal.handle('POST','/private-lobbies/api/reserve',self.login(),b'{}').status,503)
        value=json.loads(self.portal.handle('GET','/private-lobbies/api/me',{}).body)
        self.assertFalse(value['enabled'])

    def test_cookie_flags_expiry_and_logout(self):
        headers=self.login();token=headers['Cookie'].split('=',1)[1]
        self.assertIsNotNone(self.portal.sessions.get(token))
        self.now+=86401;self.assertIsNone(self.portal.sessions.get(token))
        header=self.portal._cookie(COOKIE,'token',10)[1]
        for flag in('Secure','HttpOnly','SameSite=Lax','Path=/'):self.assertIn(flag,header)

    def test_unsafe_setting_fields_and_nonfinite_values_refused(self):
        for patch in ({'room_name':'unsafe'},{'admins':[123]},{'custom_join_greetings':{}},{'requests_per_minute':60},{'sr_max':float('nan')},{'queue_ahead':100}):
            with self.assertRaises(NetworkError):validate_player_patch(patch,preset='standard-fixed')
        self.assertEqual(validate_player_patch({'sr_min':3,'sr_max':4},preset='standard-fixed'),{'sr_min':3,'sr_max':4})

    def test_preview_is_bound_to_booking_preset_and_rejects_unsupported_filter(self):
        headers=self.login()
        reserve_body={'request_id':str(uuid.uuid4()),'slot_id':'test-slot',
                      'preset':'standard-fixed','settings':{},'designated_friend_id':None}
        reserved=self.portal.handle('POST','/private-lobbies/api/reserve',headers,
                                    json.dumps(reserve_body).encode())
        self.assertEqual(reserved.status,201,reserved.body)
        booking_id=json.loads(reserved.body)['booking']['id']

        def preview(body):
            return self.portal.handle('POST','/private-lobbies/api/preview',headers,
                                      json.dumps(body).encode())

        mismatch=preview({'booking_id':booking_id,'preset':'standard-auto','settings':{}})
        self.assertEqual(mismatch.status,400)
        self.assertEqual(json.loads(mismatch.body)['error'],'booking_preset_unsupported')

        self.portal.preview=PreviewProvider(lambda preset:{},lambda config:None)
        unsupported=preview({'booking_id':booking_id,'preset':'standard-fixed','settings':{
            'selection_filters':{'od':{'min':2,'max':5,'mode':'hard'}}}})
        self.assertEqual(unsupported.status,400)
        self.assertEqual(json.loads(unsupported.body)['error'],'booking_settings_refused')
