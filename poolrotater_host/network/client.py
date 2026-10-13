"""Opt-in host network state, independent from provider authorization/room locks."""
import hashlib
import os
from pathlib import Path
import time
import uuid

from ..layout import load, contained, read_json, atomic_json, file_lease
from ..provider_transport import TrustSnapshot
from .contracts import (require, fields, identifier, verifier, integer, number, identity,
                        fingerprint, secret, enrollment, rotation, heartbeat, base, NetworkError)
from .transport import Transport, origin


class Client:
    def __init__(self,root,*,transport_factory=Transport):
        self.root=Path(root);self.marker,self.config=load(self.root);self.factory=transport_factory
        self.profile_path=contained(self.root,'config/network.json')
        self.credential_path=contained(self.root,'secrets/network.json')
        self.pending_path=contained(self.root,'secrets/network-pending.json')
        self.sequence_path=contained(self.root,'state/network-heartbeat.json')
        self.receipt_path=contained(self.root,'state/network-receipt.json')
        self.lock_path=contained(self.root,'state/network.lock')

    def read(self,path,optional=False):
        if optional and not os.path.lexists(path):return None
        return read_json(path,maximum=2*1024**2)

    def unlink(self,path):
        if os.path.lexists(path):
            self.read(path);path.unlink()
            fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)

    def _bound(self,value):
        require(value['installation_id']==self.marker['installation_id'] and value['generation']==self.marker['generation'],'foreign_network_installation')

    def profile(self):
        value=self.read(self.profile_path);fields(value,('format','installation_id','generation','operator_id','account_id','directory','origin','ca_pem','ca_sha256'))
        require(type(value['format']) is int and value['format']==1,'invalid_network_profile');self._bound(value);identity(value);origin(value['origin'])
        require(type(value['ca_pem']) is str and hashlib.sha256(value['ca_pem'].encode('ascii')).hexdigest()==value['ca_sha256'],'network_trust_changed')
        TrustSnapshot(value['ca_pem'].encode('ascii')).context();return value

    def check_account(self,account_id):
        _,config=load(self.root);expected=config['account']['expected_user_id']
        require(expected is None or expected==account_id,'network_account_mismatch')
        path=contained(self.root,'state/provider-oauth-account.json')
        if os.path.lexists(path):
            account=self.read(path);self._bound(account)
            require(type(account.get('format')) is int and account['format']==1
                    and account.get('execution') in ('provider-pilot','provider-live-pilot')
                    and type(account.get('user_id')) is int and account['user_id']==account_id,'network_account_mismatch')

    def credential(self):
        value=self.read(self.credential_path,True)
        if value is None:return None
        fields(value,('format','installation_id','generation','host_id','credential','credential_expires_at','server_epoch'))
        require(type(value['format']) is int and value['format']==1,'invalid_network_credential');self._bound(value)
        identifier(value['host_id']);identifier(value['server_epoch']);verifier(value['credential']);number(value['credential_expires_at']);return value

    def pending(self,profile):
        value=self.read(self.pending_path,True)
        if value is None:return None
        fields(value,('format','installation_id','generation','profile_sha256','kind','body','authorizer','host_id'))
        require(type(value['format']) is int and value['format']==1,'invalid_network_pending');self._bound(value)
        require(value['profile_sha256']==fingerprint(profile),'network_profile_changed')
        if value['kind']=='enroll':
            enrollment(value['body']);require(value['authorizer'] is None and value['host_id'] is None,'invalid_network_pending')
            require(all(value['body'][k]==profile[k] for k in ('installation_id','generation','operator_id','account_id','directory')),'foreign_network_pending')
        elif value['kind']=='rotate':rotation(value['body']);verifier(value['authorizer']);identifier(value['host_id'])
        elif value['kind']=='leave':
            fields(value['body'],('protocol_version',));base(value['body']);verifier(value['authorizer']);identifier(value['host_id'])
        elif value['kind']=='reset':
            fields(value['body'],('protocol_version','acknowledge_central_revocation_required'));base(value['body'])
            require(value['body']['acknowledge_central_revocation_required'] is True and value['authorizer'] is None,'invalid_network_pending')
            if value['host_id'] is not None:identifier(value['host_id'])
        else:raise NetworkError('invalid_network_pending')
        return value

    def transport(self,profile):return self.factory(profile['origin'],TrustSnapshot(profile['ca_pem'].encode('ascii')))

    def configure(self,*,url,trust,operator_id,account_id,directory=False):
        require(type(trust) is TrustSnapshot,'explicit_hub_trust_required');trust.context()
        value={'format':1,**self.marker,'operator_id':operator_id,'account_id':account_id,'directory':directory,
               'origin':origin(url),'ca_pem':trust.pem.decode('ascii'),'ca_sha256':hashlib.sha256(trust.pem).hexdigest()}
        identity(value)
        self.check_account(account_id)
        with file_lease(self.lock_path):
            old=self.read(self.profile_path,True)
            if old is not None and old!=value:require(self.credential() is None and self.read(self.pending_path,True) is None,'leave_network_before_reconfiguration')
            atomic_json(self.profile_path,value)
        return self.status()

    def _prepare(self,profile,kind,body,credential=None):
        value={'format':1,**self.marker,'profile_sha256':fingerprint(profile),'kind':kind,'body':body,
               'authorizer':credential['credential'] if credential else None,'host_id':credential['host_id'] if credential else None}
        atomic_json(self.pending_path,value);return value

    def enroll(self,invitation):
        verifier(invitation,'pri_')
        with file_lease(self.lock_path):
            profile=self.profile();self.check_account(profile['account_id']);pending=self.pending(profile)
            if pending is not None:
                require(pending['kind']=='enroll' and pending['body']['invitation']==invitation,'network_pending_requires_resume')
            else:
                require(self.credential() is None,'network_already_enrolled')
                body={'protocol_version':1,'request_id':str(uuid.uuid4()),'invitation':invitation,'credential':secret(),
                      **{k:profile[k] for k in ('installation_id','generation','operator_id','account_id','directory')}}
                pending=self._prepare(profile,'enroll',body)
            return self._resume(profile,pending)

    def rotate(self):
        with file_lease(self.lock_path):
            profile=self.profile();self.check_account(profile['account_id']);require(self.pending(profile) is None,'network_pending_requires_resume');credential=self.credential();require(credential is not None,'network_not_enrolled')
            pending=self._prepare(profile,'rotate',{'protocol_version':1,'request_id':str(uuid.uuid4()),'credential':secret()},credential)
            return self._resume(profile,pending)

    def leave(self):
        with file_lease(self.lock_path):
            profile=self.profile();pending=self.pending(profile)
            if pending is not None:require(pending['kind']=='leave','network_pending_requires_resume')
            else:
                credential=self.credential()
                if credential is None:return {'ok':True,'enrolled':False,'live_rooms_affected':False}
                pending=self._prepare(profile,'leave',{'protocol_version':1},credential)
            return self._resume(profile,pending)

    def resume(self):
        with file_lease(self.lock_path):
            profile=self.profile();pending=self.pending(profile);require(pending is not None,'network_no_pending_operation')
            return self._resume(profile,pending)

    def reset(self,*,expected_host_id,acknowledge_central_revocation_required=False):
        """Explicit local abandonment. Does not claim remote revocation succeeded."""
        require(acknowledge_central_revocation_required is True,'central_revocation_acknowledgement_required')
        if expected_host_id is not None:identifier(expected_host_id)
        with file_lease(self.lock_path):
            profile=self.profile();credential=self.credential();pending=self.pending(profile)
            host_id=credential['host_id'] if credential else (pending['host_id'] if pending else None)
            require(host_id==expected_host_id,'network_reset_host_mismatch')
            if pending is not None and pending['kind']=='reset':return self._resume(profile,pending)
            pending=self._prepare(profile,'reset',{'protocol_version':1,'acknowledge_central_revocation_required':True})
            pending['host_id']=host_id;atomic_json(self.pending_path,pending)
            return self._resume(profile,pending)

    def _resume(self,profile,pending):
        if pending['kind']=='reset':
            # Delete heartbeat first: a crash must not strand a ledger after
            # its credential is removed. Durable reset intent is resumable.
            self.unlink(self.sequence_path);self.unlink(self.credential_path)
            result={'ok':True,'action':'reset','host_id':pending['host_id'],'phase':'complete',
                    'central_revocation_required':True,'live_rooms_affected':False}
            atomic_json(self.receipt_path,{'format':1,**result});self.unlink(self.pending_path)
            return result
        if pending['kind'] in ('enroll','rotate'):self.check_account(profile['account_id'])
        route={'enroll':'/v1/enrollments','rotate':'/v1/hosts/self/token-rotation','leave':'/v1/hosts/self/leave'}[pending['kind']]
        result=self.transport(profile).request('POST',route,body=pending['body'],token=pending['authorizer'])
        if pending['kind']=='enroll':
            fields(result,('ok','protocol_version','host_id','generation','credential_expires_at','directory','server_epoch'))
            base(result)
            require(result['generation']==self.marker['generation'] and type(result['directory']) is bool and result['directory']==profile['directory'],'network_enrollment_reply_mismatch')
            record={'format':1,**self.marker,'host_id':identifier(result['host_id']),'credential':pending['body']['credential'],
                    'credential_expires_at':number(result['credential_expires_at']),'server_epoch':identifier(result['server_epoch'])}
            current=self.credential();require(current is None or current==record,'network_credential_changed')
            atomic_json(self.credential_path,record)
        elif pending['kind']=='rotate':
            fields(result,('ok','host_id','credential_expires_at'));require(result['host_id']==pending['host_id'],'network_rotation_reply_mismatch')
            current=self.credential();require(current is not None and current['host_id']==pending['host_id'] and current['credential'] in (pending['authorizer'],pending['body']['credential']),'network_credential_changed')
            atomic_json(self.credential_path,{**current,'credential':pending['body']['credential'],'credential_expires_at':number(result['credential_expires_at'])})
        else:
            fields(result,('ok','host_id','revoked'));require(result['host_id']==pending['host_id'] and result['revoked'] is True,'network_leave_reply_mismatch')
            self.unlink(self.credential_path);self.unlink(self.sequence_path)
        atomic_json(self.receipt_path,{'format':1,'action':pending['kind'],'host_id':result['host_id'],'phase':'complete','live_rooms_affected':False})
        self.unlink(self.pending_path)
        return {'ok':True,'action':pending['kind'],'host_id':result['host_id'],'phase':'complete','live_rooms_affected':False}

    def publish(self,observation):
        with file_lease(self.lock_path):
            profile=self.profile();self.check_account(profile['account_id']);require(self.pending(profile) is None,'network_pending_requires_resume');credential=self.credential();require(credential is not None,'network_not_enrolled')
            require(type(observation) is dict and set(observation)=={'code_version','catalog_version','rooms'},'invalid_observation')
            if not profile['directory']:observation={**observation,'rooms':[]}
            state=self.heartbeat_state(credential) or {'format':1,**self.marker,'host_id':credential['host_id'],'next_sequence':0,'pending':None,'last_received':None}
            body={'protocol_version':1,**self.marker,'sequence':state['next_sequence'],**observation};body.pop('format')
            heartbeat(body)
            # A new observation supersedes an uncertain heartbeat. Its higher
            # durable sequence also prevents replaying formerly public data.
            if state['pending'] is not None:
                heartbeat(state['pending']);require(state['pending']['sequence']<state['next_sequence'],'invalid_pending_sequence')
            state={**state,'next_sequence':state['next_sequence']+1,'pending':body}
            atomic_json(self.sequence_path,state)
            result=self.transport(profile).request('POST','/v1/hosts/self/heartbeat',body=body,token=credential['credential'])
            fields(result,('ok','sequence','replayed','received_at'));integer(result['sequence'],0);require(result['sequence']==body['sequence'] and type(result['replayed']) is bool,'network_heartbeat_reply_mismatch')
            atomic_json(self.sequence_path,{**state,'pending':None,'last_received':number(result['received_at'])})
            return {'ok':True,'sequence':result['sequence'],'pending':False,'live_rooms_affected':False}

    def heartbeat_state(self,credential):
        state=self.read(self.sequence_path,True)
        if state is None:return None
        fields(state,('format','installation_id','generation','host_id','next_sequence','pending','last_received'));self._bound(state)
        require(credential is not None and state['host_id']==credential['host_id'] and type(state['format']) is int and state['format']==1,'foreign_network_heartbeat')
        integer(state['next_sequence'],0)
        if state['last_received'] is not None:number(state['last_received'])
        if state['pending'] is not None:
            heartbeat(state['pending']);self._bound(state['pending'])
            require(state['pending']['sequence']<state['next_sequence'],'invalid_pending_sequence')
        return state

    def status(self):
        if not os.path.lexists(self.profile_path):return {'ok':True,'configured':False,'enrolled':False,'live_rooms_affected':False}
        profile=self.profile();credential=self.credential();pending=self.pending(profile);state=self.heartbeat_state(credential)
        try:self.check_account(profile['account_id']);matches=True
        except NetworkError:matches=False
        return {'ok':True,'configured':True,'enrolled':credential is not None,'host_id':credential['host_id'] if credential else None,
                'account_binding_matches':matches,
                'pending_operation':pending['kind'] if pending else None,'directory':profile['directory'] and not (pending and pending['kind'] in ('leave','reset')),
                'credential_expires_at':credential['credential_expires_at'] if credential else None,
                'last_heartbeat_receipt_at':state['last_received'] if state else None,'pending_heartbeat':bool(state and state['pending']),
                'live_rooms_affected':False,'remote_health_verified':False}
