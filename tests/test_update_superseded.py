from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import copy
import unittest
import uuid
import zipfile
from pathlib import Path
from poolrotater_host.config import ConfigError
from poolrotater_host.updates.superseded import qualify
from tests.test_update_agent import UpdateAgentTests


class SupersededEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.op = {'id': str(uuid.uuid4()), 'queued_at': 100, 'before_sha256': 'a'*64,
                   'release': {'kind': 'controller', 'version': 'v7.1.4', 'artifact': {'sha256': 'b'*64}}}
        self.parent = {'bundle_sha256': 'c'*64, 'release': '/invented/current'}
        self.manifest = {'version': 'v7.1.11'}
        self.job = {'id': str(uuid.uuid4()), 'phase': 'complete', 'queued_at': 200,
                    'completed_at': 300, 'before': {'bundle_sha256': 'd'*64},
                    'after': self.parent, 'rooms': ['room2']}
        self.upgrade = {'phase': 'committed', 'before': self.job['before'], 'after': self.parent}
        row = {'phase': 'running', 'operation': 'owned', 'ownership_unknown': False,
               'release': self.parent['release'], 'room_id': 77, 'error': None}
        self.rows = {'room2': copy.deepcopy(row)}
        self.journal = {'rooms': {'room2': copy.deepcopy(row)}}

    def run_check(self):
        return qualify(self.op, self.parent, self.manifest, self.job, self.upgrade, self.rows, self.journal)

    def test_later_committed_healthy_current_controller_qualifies_without_mutation(self):
        before = {k:copy.deepcopy(getattr(self,k)) for k in ('op','parent','manifest','job','upgrade','rows','journal')}
        result = self.run_check()
        self.assertEqual(result['phase'], 'superseded')
        self.assertFalse(result['services_restarted'])
        self.assertFalse(result['live_rooms_affected'])
        for key in ('op','parent','manifest','job','upgrade','rows','journal'):
            self.assertEqual(getattr(self,key),before[key])

    def test_incomplete_foreign_or_old_controller_never_qualifies(self):
        for field, value in [('phase','held'), ('id',self.op['id']), ('queued_at',99),
                             ('completed_at',100), ('after',{'different':True})]:
            with self.subTest(field=field):
                old=self.job[field];self.job[field]=value
                with self.assertRaises(ConfigError):self.run_check()
                self.job[field]=old

    def test_unsettled_upgrade_or_same_version_refuses(self):
        self.upgrade['phase']='prepared'
        with self.assertRaises(ConfigError):self.run_check()
        self.upgrade['phase']='committed';self.manifest['version']='v7.1.4'
        with self.assertRaises(ConfigError):self.run_check()

    def test_unknown_ownership_and_stale_worker_release_are_not_discarded(self):
        for where in (self.rows['room2'],self.journal['rooms']['room2']):
            for field, value in [('ownership_unknown',True), ('room_id',None),
                                 ('release','/old'), ('phase','parked'), ('target','/pending')]:
                with self.subTest(field=field):
                    present=field in where;old=where.get(field);where[field]=value
                    with self.assertRaises(ConfigError):self.run_check()
                    if present:where[field]=old
                    else:del where[field]

    def test_topology_or_current_registration_mismatch_refuses(self):
        self.journal['rooms']['other']=copy.deepcopy(self.rows['room2'])
        with self.assertRaises(ConfigError):self.run_check()


class SupersededAgentTests(UpdateAgentTests):
    # Reuse the delivery fixture, without repeating inherited scheduler tests.
    def test_explicit_supersession_preserves_evidence_and_replay_does_not_dispatch(self):
        agent=self.setup_agent(automatic_download=True,automatic_install=True)
        agent.step();op=agent.state()['operation']
        self.backend.recover.return_value={'ok':True,'phase':'superseded','installed_version':'v7.1.11',
                                          'services_restarted':False,'live_rooms_affected':False}
        result=agent.resolve(op['id'],'supersede')
        self.assertEqual(result['phase'],'idle');self.assertEqual(result['installed_version'],'v7.1.11')
        self.assertIsNone(agent.state()['operation'])
        self.assertTrue((agent.client.root/'history'/(op['id']+'.superseded.json')).exists())
        self.assertEqual(agent.state()['last_reviewed'],op)
        self.assertTrue(agent.resolve(op['id'],'supersede')['replayed'])
        self.backend.recover.assert_called_once_with(op,'supersede')
        # A predecessor still polling before the queued controller switches
        # must be able to read the reconciled state without any new enum.
        with zipfile.ZipFile(Path(__file__).resolve().parents[1]/'inputs/v7.1.11-runtime.zip') as archive:
            code=archive.read('payload/poolrotater_host/updates/agent.py')
        namespace={'__name__':'poolrotater_host.updates.old_agent_compat','__package__':'poolrotater_host.updates'}
        exec(compile(code,'verified_predecessor_agent.py','exec'),namespace)
        older=namespace['Agent'](self.host,self.backend,clock=lambda:self.now)
        self.assertIsNone(older.state()['operation'])
        self.assertEqual(older.state()['installed_version'],'v7.1.11')
        self.backend.queue.assert_called_once()

    def test_refused_supersession_retains_original_pending_intent(self):
        agent=self.setup_agent(automatic_download=True,automatic_install=True)
        agent.step();op=agent.state()['operation']
        self.backend.recover.side_effect=ConfigError('unsettled native operation')
        result=agent.resolve(op['id'],'supersede')
        self.assertEqual(result['phase'],'held');self.assertEqual(agent.state()['operation'],op)
        self.assertFalse((agent.client.root/'history'/(op['id']+'.superseded.json')).exists())


if __name__=='__main__':unittest.main()
