"""Signed release publication and actual bounded HTTP delivery in isolation."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from poolrotater_host.catalog_delivery import FixtureOrigin
from poolrotater_host.config import ConfigError
from poolrotater_host.updates import contracts as c
from poolrotater_host.updates.client import Client
from poolrotater_host.updates.publisher import Publisher, initialize, classify
from poolrotater_host.updates.server import ReleaseServer, route
from poolrotater_host.updates.trust import verify


def spec(version='v7.0.2', bot='before'):
    return {'execution':'offline-fixture','version':version,'target':{'python':'3.12'},
        'interpreter_sha256':'a'*64,'wheels':[], 'environment_id':'b'*64,
        'files':{'payload/bot/bot.py':bot, 'payload/controller.py':'same'}}


class ReleaseDeliveryTests(unittest.TestCase):
    def setUp(self):
        workspace=Path('scratch/update-delivery-tests');workspace.mkdir(mode=0o700,parents=True,exist_ok=True)
        temp=tempfile.TemporaryDirectory(dir=workspace);self.addCleanup(temp.cleanup)
        self.base=Path(temp.name);self.base.chmod(0o700);self.now=1000
        self.custody=self.base/'signers';self.served=self.base/'served'
        initialized=initialize(self.custody,self.served,now=self.now)
        self.bootstrap=(self.custody/'bootstrap.json').read_bytes()
        self.root=json.loads(self.bootstrap);self.bootstrap_sha=initialized['bootstrap_sha256']
        self.publisher=Publisher(self.custody,clock=lambda:self.now)
        self.before=self.base/'before.zip';self.before.write_bytes(b'before')
        self.after=self.base/'after.zip';self.after.write_bytes(b'new-runtime'*120000)
        self.before_sha=hashlib.sha256(self.before.read_bytes()).hexdigest()
        self.after_sha=hashlib.sha256(self.after.read_bytes()).hexdigest()
        self.specs={self.before_sha:spec(),self.after_sha:spec('v7.0.3','after')}
        self.enterContext(patch('poolrotater_host.updates.publisher.inspect_runtime',
            side_effect=lambda path,sha,**kw:self.specs[sha]))
        self.enterContext(patch('poolrotater_host.updates.client.inspect_runtime',
            side_effect=lambda path,sha,**kw:self.specs[sha]))

    def publish(self):
        return self.publisher.publish(self.after,self.after_sha,before_bundle=self.before,
            before_sha256=self.before_sha,execution='offline-fixture')

    def envelope(self):
        return (self.served/'metadata/releases.json').read_bytes()

    def client(self, name='host'):
        directory=self.base/name;directory.mkdir(mode=0o700)
        origin=FixtureOrigin(self.url,directory/'transport',deadline=2)
        return Client(directory,self.bootstrap,bootstrap_sha256=self.bootstrap_sha,
            origin=origin,binding={k:str(uuid.uuid4())for k in ('installation_id','generation','host_id')},
            clock=lambda:self.now)

    def serve(self):
        server=ReleaseServer(self.served,('127.0.0.1',0),protected_uid=os.geteuid())
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join()))
        self.url=f'http://127.0.0.1:{server.server_address[1]}/'
        return server

    def test_signed_pilot_first_then_explicit_stable_promotion(self):
        self.publish();payload,watermark=verify(self.envelope(),self.root,now=self.now)
        self.assertEqual(payload['channels'],{'pilot':self.after_sha})
        self.assertEqual(payload['releases'][0]['kind'],'worker')
        self.publisher.promote(self.after_sha,channel='stable')
        promoted,_=verify(self.envelope(),self.root,now=self.now,previous=watermark)
        self.assertEqual(promoted['channels']['stable'],self.after_sha)
        self.assertEqual((self.served/'targets'/(self.after_sha+'.runtime.zip')).read_bytes(),self.after.read_bytes())
        self.assertFalse(list(self.served.rglob('*.pem')))
        self.assertEqual(self.custody.stat().st_mode&0o777,0o700)
        self.assertTrue(all(p.stat().st_mode&0o777==0o600 for p in (self.custody/'keys').iterdir()))

    def test_actual_transfer_verified_repeat_uses_cache_and_live_data_is_unmodified(self):
        self.publish();self.serve();client=self.client()
        sentinel=self.base/'operator.json';sentinel.write_bytes(b'private oauth/settings/votes sentinel')
        self.assertEqual(client.check('pilot',self.before_sha)['phase'],'available')
        self.assertEqual(client.download()['phase'],'downloaded')
        path,item=client.artifact();self.assertEqual(path.read_bytes(),self.after.read_bytes())
        self.assertEqual(item['artifact']['sha256'],self.after_sha)
        client.download()
        self.assertEqual(client.origin.feedback['stats']['bytes'],0)
        self.assertEqual(client.origin.feedback['stats']['cached_targets'],1)
        self.assertEqual(sentinel.read_bytes(),b'private oauth/settings/votes sentinel')
        self.assertEqual(client.check('pilot',self.after_sha)['available_version'],None)

    def test_missing_stable_opt_in_does_not_install_pilot(self):
        self.publish();self.serve();client=self.client()
        self.assertIsNone(client.check('stable',self.before_sha)['available_version'])
        self.assertFalse(list((client.root/'downloads').iterdir()))

    def test_unsigned_threshold_duplicate_and_wrong_key_signatures_refused(self):
        self.publish();original=json.loads(self.envelope())
        for attack in ('payload','threshold','duplicate','wrong-key','garbage'):
            envelope=copy.deepcopy(original)
            if attack=='payload':envelope['signed']['sequence']+=1
            if attack=='threshold':envelope['signatures'].pop()
            if attack=='duplicate':envelope['signatures'][1]=envelope['signatures'][0]
            if attack=='wrong-key':envelope['signatures'][0]['keyid']='0'*64
            if attack=='garbage':envelope['signatures'][0]['signature']='g'*128
            with self.subTest(attack=attack),self.assertRaises(ConfigError):
                verify(c.canonical(envelope),self.root,now=self.now)

    def test_expired_future_clock_rollback_and_old_signed_replay_refused(self):
        self.publish();old=self.envelope();_,previous=verify(old,self.root,now=self.now)
        self.now+=5;self.publisher.renew();_,new=verify(self.envelope(),self.root,now=self.now,previous=previous)
        for raw,now,prior in ((old,self.now,new),(old,999,None),(old,1000+7*86400,None),
                              (self.envelope(),self.now-1,new)):
            with self.subTest(now=now,prior=prior),self.assertRaises(ConfigError):
                verify(raw,self.root,now=now,previous=prior)

    def test_signer_custody_and_catalog_trust_cannot_overlap_or_substitute(self):
        with self.assertRaises(ConfigError):initialize(self.served/'keys',self.served/'public',now=self.now)
        self.publish();wrong=copy.deepcopy(self.root);wrong['purpose']='poolrotater-catalog-root'
        with self.assertRaises(ConfigError):verify(self.envelope(),wrong,now=self.now)

    def test_publisher_interruption_requires_explicit_resume_and_preserves_identity(self):
        from poolrotater_host.updates import publisher as module
        real=module._write
        def interrupted(path,*args,**kwargs):
            if path.name=='releases.json':raise OSError('simulated power loss')
            return real(path,*args,**kwargs)
        with patch.object(module,'_write',side_effect=interrupted),self.assertRaises(OSError):self.publish()
        with self.assertRaisesRegex(ConfigError,'requires_resume'):self.publish()
        self.assertEqual(self.publisher.resume()['sequence'],1)
        self.assertEqual(verify(self.envelope(),self.root,now=self.now)[0]['channels']['pilot'],self.after_sha)

    def test_disk_pressure_and_missing_baseline_refuse_before_activation(self):
        self.publish();self.serve();client=self.client()
        with self.assertRaisesRegex(ConfigError,'baseline'):client.check('pilot','c'*64)
        client.check('pilot',self.before_sha)
        with patch('poolrotater_host.updates.client.shutil.disk_usage',return_value=type('Usage',(),{'free':1})()),self.assertRaisesRegex(ConfigError,'disk_reserve'):
            client.download()
        self.assertFalse(list((client.root/'downloads').iterdir()))

    def test_endpoint_never_exposes_signers_private_paths_or_directory_listing(self):
        for name in ('/','/keys/test.pem','/metadata/../keys/test.pem','//metadata/releases.json',
                     '/metadata/releases.json?secret=1','/targets/not-a-digest.runtime.zip'):
            with self.subTest(name=name),self.assertRaises(ConfigError):route(name)

    def test_environment_storage_and_version_changes_cannot_masquerade_as_worker(self):
        before=spec();after=spec('v7.0.3','new')
        self.assertEqual(classify(before,after),'worker')
        changed=copy.deepcopy(after);changed['files']['payload/controller.py']='new'
        self.assertEqual(classify(before,changed),'controller')
        for key in ('target','interpreter_sha256','environment_id','wheels'):
            changed=copy.deepcopy(after);changed[key]='different'
            with self.subTest(key=key),self.assertRaises(ConfigError):classify(before,changed)
        changed=copy.deepcopy(after);changed['version']='v7.0.1'
        with self.assertRaises(ConfigError):classify(before,changed)


if __name__=='__main__':unittest.main()
