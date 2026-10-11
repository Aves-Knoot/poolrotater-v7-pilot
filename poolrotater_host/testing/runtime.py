"""Invented referee/API and full-runtime fixture check; no live transport factory."""
import copy
import os
import threading


class RuntimeHub:
    offline_fixture = True
    def __init__(self):
        self._connect_generation = 1
        self.connected = True
        self.calls = []
        self.rooms = {}
        self.next_id = 100
        self.next_item = 200
        self.shared = None
    def is_connected(self): return self.connected
    def wait_connected(self, timeout=30): return self.connected
    def connect(self): self.connected = True
    def stop(self): self.connected = False
    def item(self,bid,mode=3):
        self.next_item += 1
        return {'id':self.next_item,'beatmap_id':bid,'ruleset_id':mode,'required_mods':[],
                'allowed_mods':[],'freestyle':False,'was_played':False,'order':0}
    def make_room(self,name,bid,ruleset_id=0):
        self.next_id += 1
        row = {'room_id':self.next_id,'chat_channel_id':self.next_id+1000,'players':[],
               'referees':[{'user_id':99}],'password':'invented-private','name':name,'playlist':[self.item(bid,ruleset_id)]}
        self.rooms[self.next_id] = row
        self.calls.append(('MakeRoom',[name,bid]))
        return copy.deepcopy(row)
    def invoke(self,method,args,**kwargs):
        self.calls.append((method,copy.deepcopy(args)))
        rid = int(args[0]); row = self.rooms[rid]
        if method == 'CloseRoom': del self.rooms[rid]
        elif method == 'JoinRoom': return copy.deepcopy(row)
        elif method == 'ChangeRoomSettings': row.update(args[1])
        elif method == 'EditCurrentPlaylistItem': row['playlist'][0].update(args[1])
        elif method == 'AddPlaylistItem':
            item = self.item(args[1]['beatmap_id'],args[1]['ruleset_id']); item['order'] = len(row['playlist'])
            row['playlist'].append(item)
            if self.shared: self.shared.route('PlaylistItemAdded',{'room_id':rid,'playlist_item':item})
        elif method == 'RemovePlaylistItem': row['playlist'] = [p for p in row['playlist'] if p['id'] != args[1]['playlist_item_id']]


class RuntimeApi:
    offline_fixture = True
    def __init__(self,hub):
        self.hub = hub; self.cancel_pending = threading.Event(); self.messages = []; self.next_message = 0
    def beatmap(self,bid): return {'id':bid,'beatmapset_id':bid,'beatmapset':{'availability':{'download_disabled':False}}}
    def beatmapset(self,bid): return {'id':bid,'availability':{'download_disabled':False}}
    def chat_join(self,*args): return {'channel_id':args[0]}
    def chat_send(self,*args,**kwargs):
        self.next_message += 1; self.messages.append(args); return {'message_id':self.next_message}
    def chat_messages(self,*args,**kwargs): return []
    def chat_ack(self): pass
    def room(self,rid): return copy.deepcopy(self.hub.rooms.get(rid))
    def room_probe(self,rid): return ('ok',self.room(rid)) if rid in self.hub.rooms else ('missing',None)
    def configure_rate_limit(self,*args): pass
    def configure_profile_cache(self,*args): pass
    def invalidate_profile_cache(self,*args): pass
    def user(self,uid,**kwargs): return {'id':uid,'username':'invented-player','statistics':{'pp':100}}
    def user_top_scores(self,*args,**kwargs): return []


def run(root,release):
    from ..config import ConfigError
    if os.environ.get('POOLROTATER_PROVISIONING_SANDBOX') != '1': raise ConfigError('protected controller demo is fixture-only')
    from offline_host_controller import host_controller
    from ..oauth import ClientCredentials, OfflineOAuth
    from ..testing.oauth import OAuthProvider
    from urllib.parse import parse_qs, urlsplit
    hub = RuntimeHub(); api = RuntimeApi(hub)
    with OAuthProvider() as provider:
        auth = OfflineOAuth(root,ClientCredentials(123,'invented-client-secret'),provider.transport)
        # This explicit fixture flow uses only a fresh, invented operator root.
        state = parse_qs(urlsplit(auth.begin('http://127.0.0.1:51111/oauth/callback')).query)['state'][0]
        auth.complete('/oauth/callback?code=' + provider.code() + '&state=' + state,'127.0.0.1:51111')
        with host_controller(root,release,api=api,hub=hub,authentication=auth) as supervisor:
            workers = []
            for alias in supervisor.configs:
                worker = supervisor.prepare(alias,release); supervisor.activate(worker); workers.append(worker)
            result = {'ok':True,'uid':os.geteuid(),'worker_count':len(workers),'room_count':len(hub.rooms),
                      'worker_pids':[w.process.pid for w in workers],'rulesets':[w.status['ruleset'] for w in workers],
                      'keys':[w.broker.cfg.get('mania_keys') for w in workers],'live_ready':False,'immutable_guard_mocked':False}
        if hub.rooms or any(w.process.poll() is None for w in workers): raise ConfigError('protected fixture cleanup failed')
        result['workers_reaped'] = True
        return result
