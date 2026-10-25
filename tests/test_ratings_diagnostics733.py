from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import unittest
import tempfile
from pathlib import Path

from poolrotater_host.layout import atomic_json, load, setup
from poolrotater_host.ratings.operations import scheduler_diagnostics


class RatingsDiagnostics733Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'installed-root';setup(self.root)

    def write_agent(self, root, marker, **changes):
        value={'format':1,'installation_id':marker['installation_id'],'generation':marker['generation'],
               'attempted_at':1000.0,'reason':'lease-contention','lock_contention':True,
               'retry_deadline':1008.0,'pending_event_age_seconds':3600,'uploaded':0,'queried':0}
        atomic_json(root/'state/ratings-agent-diagnostics.json',{**value,**changes})

    def write_fetch(self, root, marker, **changes):
        value={'format':1,'installation_id':marker['installation_id'],'generation':marker['generation'],
               'attempted_at':2000.0,'reason':'snapshot-downloading','lock_contention':False,
               'retry_deadline':2005.0,'downloaded_pages':1,'pending_pages':4}
        atomic_json(root/'state/ratings-fetch-agent-diagnostics.json',{**value,**changes})

    def test_status_addition_exposes_only_validated_scheduler_fields(self):
        root=self.root;marker,_=load(root)
        self.write_agent(root,marker);self.write_fetch(root,marker)
        result=scheduler_diagnostics(root,marker)
        self.assertEqual(result['upload'],{'available':True,'reason':'lease-contention',
            'lock_contention':True,'attempted_at':1000.0,'retry_deadline':1008.0,
            'pending_event_age_seconds':3600,'uploaded':0,'queried':0})
        self.assertEqual(result['snapshot'],{'available':True,'reason':'snapshot-downloading',
            'lock_contention':False,'attempted_at':2000.0,'retry_deadline':2005.0,
            'downloaded_pages':1,'pending_pages':4})

    def test_missing_or_malformed_optional_diagnostics_do_not_break_status(self):
        root=self.root;marker,_=load(root)
        result=scheduler_diagnostics(root,marker)
        self.assertEqual(result,{'upload':{'available':False},'snapshot':{'available':False}})
        self.write_agent(root,marker,reason='private exception: token=secret',retry_deadline=999999.0)
        self.write_fetch(root,marker,installation_id='00000000-0000-0000-0000-000000000000',
                         pending_pages=True)
        result=scheduler_diagnostics(root,marker)
        self.assertEqual(result,{'upload':{'available':False},'snapshot':{'available':False}})
        self.assertNotIn('secret',str(result))

    def test_numeric_types_and_bindings_are_strict_but_failure_is_local(self):
        root=self.root;marker,_=load(root)
        self.write_agent(root,marker,pending_event_age_seconds=-1)
        self.write_fetch(root,marker,downloaded_pages=2.5)
        result=scheduler_diagnostics(root,marker)
        self.assertFalse(result['upload']['available'])
        self.assertFalse(result['snapshot']['available'])


if __name__=='__main__':unittest.main()
