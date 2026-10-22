"""Same-origin private booking portal. Separate from staff and host credentials."""
import json
import mimetypes
from dataclasses import dataclass
from http.cookies import SimpleCookie,CookieError
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from .auth import Sessions,digest
from .settings import inventory
from ..network.contracts import NetworkError,canonical,decode,fields,identifier,integer,require
from ..diagnostic_logs.common import transaction

BASE='/private-lobbies/'
COOKIE='__Host-poolrotater-booking'
OAUTH_COOKIE='__Host-poolrotater-booking-oauth'

@dataclass
class Response:
    status:int
    headers:list
    body:bytes

class Portal:
    def __init__(self,root,repository,*,origin,oauth=None,enabled=False,preview=None,clock=None):
        url=urlsplit(origin)
        require(url.scheme=='https'and url.netloc and not url.username and not url.password and url.path in('','/')and not url.query and not url.fragment,'booking_site_origin_refused')
        self.origin=origin.rstrip('/');self.repository=repository;self.oauth=oauth;self.enabled=enabled;self.preview=preview
        self.sessions=Sessions(root,**({'clock':clock}if clock is not None else {}))
        with transaction(self.sessions.path)as db:
            db.execute('CREATE TABLE IF NOT EXISTS join_invites(token TEXT PRIMARY KEY,booking TEXT NOT NULL,owner INTEGER NOT NULL,expires REAL NOT NULL,revoked INTEGER NOT NULL)')

    def _cookies(self,headers):
        cookie=SimpleCookie()
        try:cookie.load(headers.get('Cookie',''))
        except CookieError:return {}
        return {k:v.value for k,v in cookie.items()}

    def _response(self,value,status=200,headers=(),content_type='application/json; charset=utf-8'):
        raw=value if type(value)is bytes else canonical(value)
        return Response(status,[('Content-Type',content_type),('Cache-Control','no-store'),('X-Content-Type-Options','nosniff'),('Referrer-Policy','no-referrer'),('Content-Security-Policy',"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),*headers],raw)

    @staticmethod
    def _cookie(name,value,max_age):
        return ('Set-Cookie',f'{name}={value}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age={max_age}')

    def _view(self,booking_id,user_id):
        value=self.repository.view(booking_id,user_id)
        value['settings_schema']=inventory(value['preset'])
        value['players']=[{**p,'role':self.repository.role(booking_id,p['user_id'])}for p in value.get('players',[])]
        return value

    def _joining_page(self,info):
        from html import escape
        phase=info.get('phase')
        content='<p>This private booking has ended.</p>'if phase=='ended'else'<p>The private room is being prepared. Reload this page shortly.</p>'
        url=info.get('join_url')
        if type(url)is str and url.startswith('https://osu.ppy.sh/multiplayer/rooms/'):
            suffix=url.rsplit('/',1)[-1]
            require(suffix.isascii()and suffix.isdigit()and int(suffix)>0,'booking_join_refused')
            content='<p><a class="button" href="'+escape(url,quote=True)+'" rel="noreferrer">Join private lobby</a></p>'
            if info.get('password'):
                content+='<p>Password: <code>'+escape(info['password'])+'</code></p>'
        page='<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Join private lobby · PoolRotater</title><link rel="stylesheet" href="/private-lobbies/app.css"></head><body><main><h1>Join private lobby</h1>'+content+'<p>This invitation lets you join the room. Settings access is reserved for its owner and controllers.</p><p><a href="/private-lobbies/">Private lobby bookings</a></p></main></body></html>'
        return self._response(page.encode('utf-8'),content_type='text/html; charset=utf-8')

    def handle(self,method,target,headers,body=b''):
        try:return self._handle(method,target,headers,body)
        except NetworkError as exc:return self._response({'ok':False,'error':exc.code},exc.status)
        except Exception:return self._response({'ok':False,'error':'booking_temporarily_unavailable'},503)

    def _handle(self,method,target,headers,raw):
        require(method in('GET','POST'),'booking_method_refused',405)
        url=urlsplit(target);require(not url.scheme and not url.netloc and not url.fragment and url.path.startswith(BASE),'booking_route_not_found',404)
        path=url.path[len(BASE):];query=parse_qs(url.query,keep_blank_values=True,strict_parsing=True)
        require(all(len(v)==1 for v in query.values()),'booking_invalid_query')
        cookies=self._cookies(headers);token=cookies.get(COOKIE,'');session=self.sessions.get(token)
        if method=='GET'and path in('','index.html','app.js','app.css'):
            require(not query,'booking_invalid_query');name=path or'index.html';data=(Path(__file__).parent/'static'/name).read_bytes()
            return self._response(data,content_type={'app.js':'text/javascript; charset=utf-8','app.css':'text/css; charset=utf-8'}.get(name,'text/html; charset=utf-8'))
        if method=='GET'and path=='api/me':
            require(not query,'booking_invalid_query');value={'authenticated':session is not None,'enabled':self.enabled,'user':session['user']if session else None}
            if session:
                active=self.repository.active_for_user(session['user']['id']);value.update(csrf_token=session['csrf_token'],booking_id=active['id']if active else None)
            return self._response(value)
        if method=='GET'and path=='api/slots':
            require(not query,'booking_invalid_query');return self._response({'slots':self.repository.public_slots()if self.enabled else[],'enabled':self.enabled})
        require(self.enabled,'booking_disabled',503)
        if method=='GET'and path=='oauth/start':
            require(not query and self.oauth is not None,'booking_signin_unavailable',503)
            pending=self.sessions.begin();return self._response(b'',302,[('Location',self.oauth.authorization_url(pending['state'])),self._cookie(OAUTH_COOKIE,pending['browser'],600)])
        if method=='GET'and path=='oauth/callback':
            require(set(query)=={'state','code'}and self.oauth is not None,'booking_oauth_callback_refused')
            self.sessions.consume(query['state'][0],cookies.get(OAUTH_COOKIE,''))
            user=self.oauth.exchange(query['code'][0]);token=self.sessions.create(user)
            return self._response(b'',303,[('Location',BASE),self._cookie(COOKIE,token,86400),self._cookie(OAUTH_COOKIE,'',0)])
        if method=='GET'and path=='api/session':
            require(session is not None,'booking_signin_required',401);require(set(query)=={'id'},'booking_invalid_query')
            return self._response({'booking':self._view(identifier(query['id'][0]),session['user']['id'])})
        if method=='GET'and path=='api/settings':
            require(set(query)=={'preset'},'booking_invalid_query');return self._response(inventory(query['preset'][0]))
        if method=='GET'and path=='join':
            require(set(query)=={'invite'},'booking_invalid_query');key=digest(query['invite'][0])
            with transaction(self.sessions.path)as db:invite=db.execute('SELECT * FROM join_invites WHERE token=?',(key,)).fetchone()
            require(invite is not None and not invite['revoked']and self.sessions.clock()<invite['expires'],'booking_invite_expired',403)
            info=self.repository.join_info(invite['booking'])
            if 'text/html'in headers.get('Accept',''):return self._joining_page(info)
            return self._response({'joining':info})
        require(method=='POST'and path.startswith('api/'),'booking_route_not_found',404)
        require(not query and headers.get('Origin')==self.origin,'booking_origin_refused',403)
        require(headers.get('Content-Type','').split(';',1)[0]=='application/json','booking_json_required',415)
        session=self.sessions.check(token,headers.get('X-CSRF-Token',''));body=decode(raw,maximum=16384);user=session['user']['id']
        if path=='api/reserve':
            fields(body,('request_id','slot_id','preset','settings','designated_friend_id'))
            value=self.repository.reserve(user_id=user,**body);return self._response({'booking':self._view(value['id'],user)},201)
        if path=='api/action':
            fields(body,('request_id','booking_id','expected_revision','action','payload'))
            result=self.repository.submit(user_id=user,**body)
            return self._response({'operation':{k:result.get(k)for k in('id','action','phase','error','application')},'booking':self._view(body['booking_id'],user)},202)
        if path=='api/preview':
            require(type(body)is dict and not set(body)-{'booking_id','slot_id','preset','settings','request_id'}and'preset'in body and'settings'in body,'booking_invalid_query')
            if body.get('booking_id'):
                booking=self.repository.view(body['booking_id'],user)
                require(booking['preset']==body['preset'],'booking_preset_unsupported')
            require(self.preview is not None,'booking_preview_unavailable',503)
            self.sessions.admit(user,'preview',5)
            from .selection import SelectionError
            try:value=self.preview(body['preset'],body['settings'])
            except SelectionError:raise NetworkError('booking_settings_refused')from None
            return self._response(value)
        if path=='api/invite':
            fields(body,('booking_id',));value=self.repository.view(body['booking_id'],user);require(value['role']=='owner','booking_owner_required',403)
            self.sessions.admit(user,'invite',10)
            import secrets
            invite=secrets.token_urlsafe(32)
            with transaction(self.sessions.path)as db:
                db.execute('UPDATE join_invites SET revoked=1 WHERE booking=?',(body['booking_id'],))
                db.execute('INSERT INTO join_invites VALUES(?,?,?,?,0)',(digest(invite),body['booking_id'],user,self.sessions.clock()+1800))
            return self._response({'invite_url':self.origin+BASE+'join?invite='+invite,'expires_in':1800})
        if path=='api/logout':
            fields(body,());self.sessions.logout(token);return self._response({'ok':True},headers=[self._cookie(COOKIE,'',0)])
        raise NetworkError('booking_route_not_found',404)
