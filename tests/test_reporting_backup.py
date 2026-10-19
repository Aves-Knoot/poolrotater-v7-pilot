"""Whole reporting backups retain private evidence without restoring contribution authority."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch
from poolrotater_host.network.hub_layout import initialize
from poolrotater_host.network.store import Store
from poolrotater_host.network.hub_service import backup_package,restore_package
from poolrotater_host.network.contracts import secret,NetworkError
from poolrotater_host.activity.repository import Repository
from poolrotater_host.diagnostic_logs.local import LocalLogs
from poolrotater_host.diagnostic_logs.hub import HubLogs


class ReportingBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
        self.root=self.base/'hub';initialize(self.root);self.store=Store(self.root);self.now=self.store.clock()
        self.binding={'operator_id':str(uuid.uuid4()),'account_id':123,'installation_id':str(uuid.uuid4()),'generation':str(uuid.uuid4()),'directory':True}
        invitation=self.store.invite(self.binding,label='Invented public operator')['invitation'];self.token=secret()
        self.host=self.store.enroll({'protocol_version':1,'request_id':str(uuid.uuid4()),'invitation':invitation,'credential':self.token,**self.binding})['host_id']
        self.repo=Repository(self.store);self.consent={'protocol_version':1,'consent_id':str(uuid.uuid4()),'consent_at':self.now-100,'enabled':True}
        self.repo.consent(self.token,self.consent)
        envelope={'event_id':str(uuid.uuid4()),'installation_id':self.binding['installation_id'],'generation':self.binding['generation'],'consent_id':self.consent['consent_id'],'kind':'privacy','payload':{'user_id':2,'hidden':True,'updated_at':self.now-10}}
        self.repo.ingest(self.token,{'protocol_version':1,'events':[envelope]})
        self.logs=HubLogs(self.store)
        logroot=self.base/'local';logroot.mkdir(mode=0o700)
        local=LocalLogs(logroot,installation_id=self.binding['installation_id'],generation=self.binding['generation'])
        local.configure(operational=True,public_chat=False)
        self.logs.consent(self.token,{'format':1,'installation_id':self.binding['installation_id'],'generation':self.binding['generation'],'operational':True,'public_chat':False})
        local.append('INFO invented retained record',room='room1',version='v7.1.0')
        self.logs.ingest(self.token,local.batch())

    def test_manifest_exactly_covers_both_reporting_databases(self):
        backup=self.base/'backup';out=backup_package(self.root,backup)
        self.assertTrue(out['activity_history_included']);self.assertTrue(out['diagnostic_logs_included'])
        manifest=json.loads((backup/'manifest.json').read_bytes())
        self.assertEqual(set(manifest['files']),{'network.sqlite3','activity.sqlite3','diagnostic-logs.sqlite3'})
        for name,digest in manifest['files'].items():self.assertEqual(hashlib.sha256((backup/name).read_bytes()).hexdigest(),digest)

    def test_restore_retains_history_privacy_and_explicitly_disables_all_sharing(self):
        backup=self.base/'backup';backup_package(self.root,backup);destination=self.base/'restored'
        out=restore_package(backup,destination)
        self.assertTrue(out['all_hosts_revoked']);self.assertTrue(out['activity_history_retained']);self.assertTrue(out['diagnostic_history_retained'])
        self.assertFalse(out['restored_reporting_consents_enabled']);restored=Store(destination)
        repo=Repository(restored);snap=repo.snapshot();self.assertEqual(snap['hidden_user_ids'],[2]);self.assertEqual(snap['presence'],[])
        with sqlite3.connect(destination/'activity.sqlite3') as db:
            self.assertEqual(db.execute('SELECT enabled FROM consents').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)
        with sqlite3.connect(destination/'diagnostic-logs.sqlite3') as db:
            self.assertEqual(db.execute('SELECT operational,public_chat FROM consent').fetchone(),(0,0))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM logs').fetchone()[0],1)
        self.assertEqual(HubLogs(restored).search()['records'],[])
        with self.assertRaisesRegex(NetworkError,'host_revoked'):repo.consent(self.token,self.consent)

    def test_damaged_reporting_digest_refuses_without_partial_destination(self):
        backup=self.base/'backup';backup_package(self.root,backup)
        with (backup/'activity.sqlite3').open('ab') as stream:stream.write(b'corruption')
        destination=self.base/'restored'
        with self.assertRaisesRegex(NetworkError,'identity_mismatch'):restore_package(backup,destination)
        self.assertFalse(destination.exists());self.assertEqual(list(self.base.glob('.hub-package-restore-*')),[])

    def test_valid_digest_for_wrong_schema_still_refuses_atomically(self):
        backup=self.base/'backup';backup_package(self.root,backup)
        target=backup/'activity.sqlite3';target.unlink()
        with sqlite3.connect(target) as db:db.execute('CREATE TABLE foreign_table(value TEXT)')
        target.chmod(0o600)
        manifest=json.loads((backup/'manifest.json').read_bytes());manifest['files']['activity.sqlite3']=hashlib.sha256(target.read_bytes()).hexdigest()
        (backup/'manifest.json').write_text(json.dumps(manifest));(backup/'manifest.json').chmod(0o600)
        destination=self.base/'restored'
        with self.assertRaisesRegex(NetworkError,'backup_verification_failed'):restore_package(backup,destination)
        self.assertFalse(destination.exists());self.assertEqual(list(self.base.glob('.hub-package-restore-*')),[])

    def test_restore_copy_failure_cleans_unpublished_whole_package(self):
        backup=self.base/'backup';backup_package(self.root,backup);destination=self.base/'restored'
        original=__import__('os').rename
        def fail_publish(source,target):
            if Path(target)==destination:raise OSError('invented final publication failure')
            return original(source,target)
        with patch('poolrotater_host.network.hub_service.os.rename',side_effect=fail_publish):
            with self.assertRaises(OSError):restore_package(backup,destination)
        self.assertFalse(destination.exists());self.assertEqual(list(self.base.glob('.hub-package-restore-*')),[])

    def test_recovery_history_is_backed_up_but_never_restored_as_live_commands(self):
        from poolrotater_host.rescue.repository import Repository as Recovery
        from poolrotater_host.rescue.contracts import exchange
        repo=Recovery(self.store)
        body=exchange({'protocol_version':1,**{k:self.binding[k] for k in ('installation_id','generation')},
            'sequence':1,'sent_at':self.now,'receipt':None,
            'observation':{'phase':'absent','operation_id':None,'candidate_sha256':None,'job_sha256':None,
                'failure_stage':None,'failure_code':None,'service_state':'failed','startup_attempted':False}})
        repo.accept(self.token,body);repo.queue(self.host,'inspect')
        backup=self.base/'backup';backup_package(self.root,backup)
        manifest=json.loads((backup/'manifest.json').read_bytes())
        self.assertIn('recovery.sqlite3',manifest['files'])
        destination=self.base/'restored';restore_package(backup,destination)
        self.assertFalse((destination/'recovery.sqlite3').exists())
        self.assertTrue((destination/'retained-recovery.sqlite3').exists())
        with sqlite3.connect(destination/'retained-recovery.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone()[0],1)
        with self.assertRaises(NetworkError):
            Recovery(Store(destination)).accept(self.token,body)

if __name__=='__main__':unittest.main()
