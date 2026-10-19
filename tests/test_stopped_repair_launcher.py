"""Pinned launcher stages a predecessor once and observes its repaired successor."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import hashlib,os,sys,tempfile,unittest,zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from poolrotater_host import pilot_installation,provisioning,runtime_lifecycle

class Launcher(unittest.TestCase):
 def exercise(self,already_installed):
  with tempfile.TemporaryDirectory()as temp:
   root=Path(temp);archive=root/'runtime.zip'
   with zipfile.ZipFile(archive,'w')as z:
    info=zipfile.ZipInfo('payload/invented.py');info.external_attr=0o100644<<16;z.writestr(info,b'# invented\n')
   pin=hashlib.sha256(archive.read_bytes()).hexdigest()
   source=(Path(__file__).resolve().parents[1]/'pilot/stopped_repair.py.template').read_text().replace('__RUNTIME_SHA256__',pin)
   ns={'__name__':'test_launcher'};exec(compile(source,'stopped_repair_launcher','exec'),ns)
   realpath=Path
   def local_path(value):
    return root/'reports' if str(value)=='/var/lib/poolrotater-v7-repair-reports' else realpath(value)
   ns['Path']=local_path
   candidate={'installation_id':'invented','bundle_sha256':pin,'service_uid':1000,'execution':'pilot-live'}
   current={**candidate,'bundle_sha256':pin if already_installed else '98bc2e7a9a127062d7bbff451dbef743395dfe3dfb3c8453a4b53e268fa3010c'}
   def control(path):
    if path.name=='pilot-install.json':return {'installation_id':'invented'}
    return candidate if 'candidates'in path.parts else current
   oldpath=sys.path[:]
   try:
    with patch.object(pilot_installation,'paths',return_value={'root':root/'host','prefix':root/'prefix'}), \
         patch.object(provisioning,'_control',side_effect=control), \
         patch.object(provisioning,'protected',side_effect=lambda p:p), \
         patch.object(provisioning,'_provision',autospec=True)as stage, \
         patch.object(runtime_lifecycle,'validate_runtime',return_value={'release':root/'source'}), \
         patch('subprocess.run',return_value=SimpleNamespace(returncode=0))as child, \
         patch('os.geteuid',return_value=0),patch('os.chdir'), \
         patch.object(sys,'argv',['repair','--instance','two','--bundle',str(archive),'--start','--resume-agents']):
     with self.assertRaises(SystemExit)as exit_code:ns['main']()
     self.assertEqual(exit_code.exception.code,0)
     self.assertEqual(stage.call_count,0 if already_installed else 1)
     child.assert_called_once()
     self.assertIn('stopped_repair_entry.py',str(child.call_args.args[0]))
     self.assertIn('--resume-agents',child.call_args.args[0])
   finally:sys.path[:]=oldpath
 def test_predecessor_stages_candidate_and_dispatches_protected_entry(self):self.exercise(False)
 def test_installed_successor_skips_distinct_controller_staging_and_observes(self):self.exercise(True)
