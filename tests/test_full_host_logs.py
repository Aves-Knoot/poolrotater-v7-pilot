"""Full diagnostic scope, redaction, rotation and actual bounded journal child."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock,patch

from poolrotater_host import reporting,reporting_logging,reporting_external as external
from poolrotater_host.layout import setup,atomic_json
from poolrotater_host.diagnostic_logs import LocalLogs,Source,SanitizedJSONFormatter
from poolrotater_host.diagnostic_logs.common import unpack
from poolrotater_host.network.contracts import NetworkError

UNIT='poolrotater-v7-pilot-fixture.service'


class FullLogsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'host';setup(self.root)
        self.local=reporting.logs(self.root);self.local.configure(operational=True,public_chat=True)

    def rows(self):
        batch=self.local.batch()
        return unpack(batch['payload']) if batch else []

    def test_default_full_scope_accepts_private_and_unscoped_chat(self):
        self.assertTrue(all(reporting.profile(self.root)[k] for k in reporting.CHOICES))
        self.assertTrue(self.local.append('private debug diagnostic',room='room1',private=True))
        self.assertTrue(self.local.append('unscoped chat',kind='chat'))
        self.assertEqual(len(self.rows()),2)

    def test_source_inventory_includes_every_room_up_to_the_host_limit(self):
        for index in range(1,51):
            (self.root/'logs'/('reporting-room'+str(index)+'.jsonl')).touch(mode=0o600)
        local=reporting.logs(self.root)
        self.assertEqual(len(local.sources),51)
        self.assertIn('room50',local.sources)

    def test_explicit_chat_optout_covers_all_rooms_and_unknown_room_context(self):
        self.local.configure(operational=True,public_chat=False)
        for room in (None,'room1'):
            self.assertFalse(self.local.append('do not share chat',room=room,kind='chat',private=True))
        self.assertTrue(self.local.append('private operational failure',room='room1',private=True))

    def test_explicit_all_logs_optout_prevents_even_private_diagnostics(self):
        self.local.configure(operational=False,public_chat=False)
        self.assertFalse(self.local.append('do not share',private=True))

    def test_integration_environment_and_saved_passwords_redacted_without_inheriting_env(self):
        env=self.root/'secrets/integrations.env'
        env.write_text('CUSTOM_TOKEN="opaque-environment-canary"\nCUSTOM_KEY="two words secret"\n');env.chmod(0o600)
        atomic_json(self.root/'config/gameplay.json',{'settings':{'room_password':'opaque-password-canary'}})
        values=reporting.secrets(self.root,provider=True)
        for secret in ('opaque-environment-canary','two words secret','opaque-password-canary'):self.assertIn(secret,values)
        formatter=SanitizedJSONFormatter(secret_provider=reporting.CachedSecrets(self.root))
        record=logging.LogRecord('test',logging.DEBUG,'',0,' '.join(values),(),None)
        output=formatter.format(record)
        for value in values:self.assertNotIn(value,output)

    def test_cache_reloads_on_same_size_credential_replacement_and_keeps_old_value(self):
        path=self.root/'secrets/provider-osu-token.json'
        atomic_json(path,{'access_token':'first-opaque-canary'})
        cache=reporting.CachedSecrets(self.root)
        with patch.object(reporting,'secrets',wraps=reporting.secrets) as loader:
            for _ in range(100):self.assertIn('first-opaque-canary',cache())
            self.assertEqual(loader.call_count,1)
            atomic_json(path,{'access_token':'other-opaque-canary'})
            values=cache();self.assertIn('first-opaque-canary',values);self.assertIn('other-opaque-canary',values)
            self.assertEqual(loader.call_count,2)

    def test_secret_loader_failure_omits_whole_message_instead_of_leaking_it(self):
        path=self.root/'secrets/integrations.env';path.write_text('TOKEN=private-canary');path.chmod(0o644)
        formatter=SanitizedJSONFormatter(secret_provider=reporting.CachedSecrets(self.root))
        text=formatter.format(logging.LogRecord('test',logging.DEBUG,'',0,'private-canary',(),None))
        self.assertNotIn('private-canary',text);self.assertIn('omitted',text)

    def test_large_traceback_scrubbed_before_it_is_fragmented(self):
        formatter=SanitizedJSONFormatter(secret_provider=lambda:['opaque-large-canary'])
        message='A'*4090+'opaque-large-canary'+'B'*9000
        raw=formatter.format(logging.LogRecord('test',logging.ERROR,'',0,message,(),None))
        rows=[json.loads(line)for line in raw.splitlines()]
        self.assertGreater(len(rows),1);self.assertTrue(all(len(r['text'])<=4096 for r in rows))
        self.assertNotIn('opaque-large-canary',raw);self.assertIn('[REDACTED]',''.join(r['text']for r in rows))

    def test_native_sink_includes_debug_and_private_chat(self):
        logger=logging.getLogger('full-log-test');before=list(logging.getLogger().handlers)
        level=logging.getLogger().level;logging.getLogger().setLevel(logging.DEBUG)
        self.addCleanup(logging.getLogger().setLevel,level)
        try:
            reporting_logging.witness(self.root,'room1',1,room_id=44,public=False)
            reporting_logging.install(self.root,alias='room1',version='v7.1.4')
            logger.debug('private DEBUG lifecycle');logger.info('CHAT <Invented:1> private conversation')
            for h in logging.getLogger().handlers:
                if h not in before:h.flush()
            text=(self.root/'logs/reporting-room1.jsonl').read_text()
            self.assertIn('private DEBUG lifecycle',text);self.assertIn('private conversation',text)
        finally:
            for h in list(logging.getLogger().handlers):
                if h not in before:logging.getLogger().removeHandler(h);h.close()

    def test_rotation_drains_unread_old_tail_then_current_file_without_duplicate_or_gap(self):
        source=Source('rotated',self.root/'logs/raw.log',start_at_end=False,rotations=4)
        local=LocalLogs(self.root/'state',installation_id=self.local.installation_id,
            generation=self.local.generation,sources=(source,),full_stream=True)
        path=Path(source.path);path.write_text('first\n');local.collect(source)
        with path.open('a')as stream:stream.write('unread old tail\n')
        path.rename(Path(str(path)+'.1'));path.write_text('new active\n')
        local.collect(source);local.collect(source);local.collect(source)
        records=unpack(local.batch()['payload']);text=''.join(r['text']for r in records)
        for value in ('first','unread old tail','new active'):self.assertEqual(text.count(value),1)
        self.assertNotIn('source_rotated_unread_tail_unknown',text)

    def test_journal_entries_and_cursor_commit_together_and_hub_ack_does_not_reset_cursor(self):
        at=time.time();rows=[{'at':at,'kind':'operational','room':None,'text':'journal early stderr access_token=journal-canary'}]
        self.local.append_journal(rows,previous=None,cursor='s=one;i=1')
        batch=self.local.batch();self.assertNotIn('journal-canary',str(unpack(batch['payload'])))
        self.local.acknowledge({'ok':True,'accepted':True,'chunk_id':batch['chunk_id']})
        self.assertEqual(self.local.external_cursor(),'s=one;i=1')
        with self.assertRaises(NetworkError):self.local.append_journal(rows,previous=None,cursor='s=one;i=2')
        self.assertIsNone(self.local.batch())

    def test_invalid_journal_batch_rolls_back_both_records_and_cursor(self):
        row={'at':time.time(),'kind':'operational','room':None,'text':'first'}
        with self.assertRaises(NetworkError):self.local.append_journal([row,{'invalid':True}],previous=None,cursor='s=bad')
        self.assertIsNone(self.local.external_cursor());self.assertIsNone(self.local.batch())

    def test_journal_timestamp_overlap_deduplicates_even_after_hub_ack(self):
        row={'at':time.time(),'kind':'operational','room':None,'text':'old once','cursor':'s=old;i=1'}
        self.local.append_journal([row],previous=None,cursor=row['cursor'])
        batch=self.local.batch();self.local.acknowledge({'ok':True,'accepted':True,'chunk_id':batch['chunk_id']})
        other={**row,'text':'new once','cursor':'s=new;i=2','at':row['at']+.1}
        self.local.append_journal([row,other],previous=row['cursor'],cursor=other['cursor'])
        text=str(self.rows());self.assertNotIn('old once',text);self.assertIn('new once',text)
        self.assertEqual(self.local.external_since(),other['at'])

    def test_worker_stderr_registered_before_initialization_scrubbed_and_captured_once(self):
        generation='a'*32;external.register_worker(self.root,'room1',generation,'v7.1.4')
        folder=self.root/'logs/workers';folder.mkdir()
        (folder/('room1-'+generation+'.log')).write_text('Traceback: import failed access_token=early-canary\n')
        self.local.secret_provider=reporting.CachedSecrets(self.root)
        self.assertEqual(external.collect_workers(self.root,self.local)['sources'],1)
        self.assertEqual(external.collect_workers(self.root,self.local)['sources'],0)
        text=str(self.rows());self.assertIn('import failed',text);self.assertNotIn('early-canary',text)


class JournalChildTests(unittest.TestCase):
    def synthetic(self, rows, *, suffix=''):
        original=subprocess.Popen;children=[]
        def launch(arguments,**kwargs):
            self.assertEqual(arguments[0],'/usr/bin/journalctl')
            self.arguments=arguments
            code='import sys,time;'+''.join('print('+repr(json.dumps(r))+',flush=True);'for r in rows)+suffix
            child=original([sys.executable,'-c',code],**kwargs);children.append(child);return child
        self.children=children
        return patch.object(external.subprocess,'Popen',side_effect=launch)

    def row(self,index=1,unit=UNIT):
        return {'__CURSOR':'s=fixture;i='+str(index),'__REALTIME_TIMESTAMP':str(int(time.time()*1000000)),
            '_SYSTEMD_UNIT':unit,'_PID':'123','MESSAGE':'early stderr '+str(index)}

    def test_actual_owned_child_and_fixed_unit_forward_cursor(self):
        with self.synthetic([self.row()]):rows,cursor=external.read_journal((UNIT,),cursor=None,since=1)
        self.assertEqual(len(rows),1);self.assertEqual(cursor,'s=fixture;i=1')
        self.assertIn('--since=@1.0',self.arguments)
        self.assertTrue(all(p.poll() is not None for p in self.children))
        with self.synthetic([self.row(2)]):rows,cursor=external.read_journal((UNIT,),cursor='s=fixture;i=1',since=1)
        self.assertIn('--after-cursor=s=fixture;i=1',self.arguments)

    def test_wrong_unit_is_excluded_and_pid1_unit_message_is_included(self):
        system=self.row(2);system.update(_SYSTEMD_UNIT='init.scope',_PID='1',UNIT=UNIT)
        with self.synthetic([self.row(1,unit='unrelated.service'),system]):
            rows,cursor=external.read_journal((UNIT,),cursor=None,since=1)
        self.assertEqual(len(rows),1);self.assertIn('early stderr 2',rows[0]['text'])

    def test_invalid_unit_and_cursor_never_start_a_child(self):
        with patch.object(external.subprocess,'Popen')as launch:
            for units,cursor in ((('ssh.service',),None),((UNIT,),'--directory=/etc')):
                with self.assertRaises(NetworkError):external.read_journal(units,cursor=cursor,since=1)
            launch.assert_not_called()

    def test_failed_child_reply_does_not_expose_stderr(self):
        with self.synthetic([],suffix='sys.stderr.write("opaque-stderr-canary");sys.exit(2)'),self.assertRaises(NetworkError)as error:
            external.read_journal((UNIT,),cursor=None,since=1)
        self.assertNotIn('opaque-stderr-canary',str(error.exception))
        self.assertTrue(all(p.poll()is not None for p in self.children))

    def test_page_limit_reaps_child_and_keeps_last_complete_cursor(self):
        rows=[self.row(i)for i in range(1,25)]
        with patch.object(external,'MAX_ENTRIES',8),self.synthetic(rows):
            result,cursor=external.read_journal((UNIT,),cursor=None,since=1)
        self.assertEqual(len(result),8);self.assertEqual(cursor,'s=fixture;i=8')
        self.assertTrue(all(p.poll()is not None for p in self.children))

    def test_timeout_reaps_only_our_child(self):
        with patch.object(external,'JOURNAL_TIMEOUT',.05),self.synthetic([],suffix='time.sleep(5)'),self.assertRaises(NetworkError)as error:
            external.read_journal((UNIT,),cursor=None,since=1)
        self.assertEqual(error.exception.code,'journal_read_timeout')
        self.assertTrue(all(p.poll()is not None for p in self.children))

    def test_byte_limit_returns_complete_prefix_instead_of_stalling_on_large_backlog(self):
        rows=[self.row(i)for i in range(1,20)]
        size=len((json.dumps(rows[0])+'\n').encode())+10
        with patch.object(external,'MAX_BYTES',size),self.synthetic(rows):
            result,cursor=external.read_journal((UNIT,),cursor=None,since=1)
        self.assertEqual(len(result),1);self.assertEqual(cursor,'s=fixture;i=1')
        self.assertTrue(all(p.poll()is not None for p in self.children))


if __name__=='__main__':unittest.main()
