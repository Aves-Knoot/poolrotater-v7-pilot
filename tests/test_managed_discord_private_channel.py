"""Staff-only fleet output must reject individual nonstaff viewing grants."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
from pathlib import Path
from types import SimpleNamespace
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from poolrotater_host.discord_relay.bridge import CommunityDiscord

class Subject:
 def __init__(self,id,name='',admin=False):self.id=id;self.name=name;self.permissions=SimpleNamespace(administrator=admin)

class PrivateFleetChannel(unittest.TestCase):
 def setUp(self):
  self.bot=CommunityDiscord({'discord_control_guild_id':1,'discord_control_admin_role_names':['Top dev'],'discord_control_moderator_role_names':['mods']});self.bot.fleet_channel=10
  self.roles=[Subject(1,'@everyone'),Subject(2,'Top dev'),Subject(3,'mods'),Subject(4,'Other')]
  self.guild=SimpleNamespace(id=1,roles=self.roles,me=Subject(20),owner_id=30)
  self.channel=SimpleNamespace(id=10,guild=self.guild,overwrites={},permissions_for=lambda role:SimpleNamespace(view_channel=role.name in ('Top dev','mods')))
 def test_role_only_staff_channel_is_allowed(self):self.assertTrue(self.bot._private_fleet_channel(self.channel))
 def test_individual_nonstaff_view_grant_is_refused(self):
  self.channel.overwrites={Subject(40):SimpleNamespace(view_channel=True)}
  self.assertFalse(self.bot._private_fleet_channel(self.channel))
 def test_unknown_individual_view_grant_is_refused(self):
  self.channel.overwrites={Subject(987654):SimpleNamespace(view_channel=True)}
  self.assertFalse(self.bot._private_fleet_channel(self.channel))
 def test_guild_owner_and_bot_implicit_staff_access_is_allowed(self):
  self.channel.overwrites={self.guild.me:SimpleNamespace(view_channel=True),Subject(self.guild.owner_id):SimpleNamespace(view_channel=True)}
  self.assertTrue(self.bot._private_fleet_channel(self.channel))
 def test_staff_role_override_is_not_mistaken_for_individual_grant(self):
  self.channel.overwrites={self.roles[1]:SimpleNamespace(view_channel=True)}
  self.assertTrue(self.bot._private_fleet_channel(self.channel))
 def test_individual_deny_or_unrelated_overwrite_does_not_grant_view(self):
  for value in (False,None):
   with self.subTest(value=value):
    self.channel.overwrites={Subject(40):SimpleNamespace(view_channel=value)}
    self.assertTrue(self.bot._private_fleet_channel(self.channel))
 def test_public_role_still_refuses_and_nonfleet_panels_keep_existing_route(self):
  self.channel.permissions_for=lambda role:SimpleNamespace(view_channel=True)
  self.assertFalse(self.bot._private_fleet_channel(self.channel));self.channel.id=99
  self.assertTrue(self.bot._private_fleet_channel(self.channel))
 def managed_role(self,bot_id=20,managed=True):
  role=Subject(50,'PoolRotater');role.managed=managed;role.tags=SimpleNamespace(bot_id=bot_id);self.roles.append(role)
  self.channel.permissions_for=lambda value:SimpleNamespace(view_channel=value.name in ('Top dev','mods','PoolRotater'))
  return role
 def test_own_managed_bot_role_can_view_without_becoming_a_human_staff_role(self):
  self.managed_role();self.assertTrue(self.bot._private_fleet_channel(self.channel))
 def test_other_bot_managed_role_does_not_qualify_as_own_access(self):
  self.managed_role(bot_id=21);self.assertFalse(self.bot._private_fleet_channel(self.channel))
 def test_own_bot_id_on_unmanaged_role_does_not_qualify(self):
  self.managed_role(managed=False);self.assertFalse(self.bot._private_fleet_channel(self.channel))
 def test_own_managed_bot_role_does_not_allow_nonstaff_member_override(self):
  self.managed_role();self.channel.overwrites={Subject(40):SimpleNamespace(view_channel=True)}
  self.assertFalse(self.bot._private_fleet_channel(self.channel))
 def test_incomplete_fleet_channel_permission_metadata_is_refused(self):
  del self.channel.overwrites
  self.assertFalse(self.bot._private_fleet_channel(self.channel))

if __name__=='__main__':unittest.main()
