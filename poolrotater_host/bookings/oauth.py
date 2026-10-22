"""Identify-only website OAuth. Tokens are discarded after identity verification."""
import json
import ssl
import urllib.request
from urllib.parse import urlencode,urlsplit
from ..network.contracts import NetworkError,require,integer,text

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None

class OAuthClient:
    def __init__(self,client_id,client_secret,redirect_uri):
        require(type(client_id)is str and client_id.isdigit()and 0<len(client_id)<=20,'booking_oauth_configuration_required')
        require(type(client_secret)is str and 8<=len(client_secret)<=256,'booking_oauth_configuration_required')
        url=urlsplit(redirect_uri)
        require(url.scheme=='https'and url.hostname and not url.username and not url.password and not url.query and not url.fragment and url.path=='/private-lobbies/oauth/callback','booking_oauth_configuration_required')
        self.client_id=client_id;self.client_secret=client_secret;self.redirect_uri=redirect_uri
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context()))

    def authorization_url(self,state):
        return 'https://osu.ppy.sh/oauth/authorize?'+urlencode({'client_id':self.client_id,'redirect_uri':self.redirect_uri,'response_type':'code','scope':'identify','state':state})

    def _request(self,url,*,data=None,token=None):
        headers={'Accept':'application/json','User-Agent':'PoolRotater-booking/1'}
        if data is not None:headers['Content-Type']='application/x-www-form-urlencoded'
        if token is not None:headers['Authorization']='Bearer '+token
        try:
            with self.opener.open(urllib.request.Request(url,data=urlencode(data).encode()if data is not None else None,headers=headers),timeout=15)as response:
                require(response.status==200,'booking_signin_unavailable',503);raw=response.read(65537)
            require(len(raw)<=65536,'booking_signin_unavailable',503);result=json.loads(raw)
            require(type(result)is dict,'booking_signin_unavailable',503);return result
        except Exception:raise NetworkError('booking_signin_unavailable',503)from None

    def exchange(self,code):
        require(type(code)is str and 1<=len(code)<=2048 and not any(ord(c)<32 for c in code),'booking_oauth_code_refused')
        token=self._request('https://osu.ppy.sh/oauth/token',data={'client_id':self.client_id,'client_secret':self.client_secret,'code':code,'grant_type':'authorization_code','redirect_uri':self.redirect_uri})
        access=token.get('access_token');require(type(access)is str and 8<=len(access)<=8192 and not any(ord(c)<=32 for c in access),'booking_signin_unavailable',503)
        user=self._request('https://osu.ppy.sh/api/v2/me',token=access)
        integer(user.get('id'),1,2**63-1);text(user.get('username'),80)
        return {'id':user['id'],'username':user['username']}
