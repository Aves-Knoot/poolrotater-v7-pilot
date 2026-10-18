"""Installed live hosts accelerate chat with the existing account socket."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import ast,unittest
from pathlib import Path

class LiveChatDefaultTests(unittest.TestCase):
 def test_live_socket_default_is_conditional_and_offline_stays_disabled(self):
  tree=ast.parse(Path('poolrotater_host/runtime_config.py').read_text())
  values={}
  for node in ast.walk(tree):
   if isinstance(node,ast.Dict):
    for key,value in zip(node.keys,node.values):
     if isinstance(key,ast.Constant)and key.value in ('chat_websocket_enabled','chat_websocket_endpoint'):values[key.value]=value
  expression=ast.Expression(values['chat_websocket_enabled']);ast.fix_missing_locations(expression)
  endpoint=ast.Expression(values['chat_websocket_endpoint']);ast.fix_missing_locations(endpoint)
  for live in (True,False):
   self.assertIs(eval(compile(expression,'<default>','eval'),{},dict(live=live)),live)
   self.assertEqual(eval(compile(endpoint,'<endpoint>','eval'),{},dict(live=live)),'wss://notify.ppy.sh'if live else '')
