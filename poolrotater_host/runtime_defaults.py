"""Preserve each installation's inherited defaults across controller upgrades.

Only public, typed gameplay defaults enter this additive state. No credentials,
room titles, account IDs, integrations or player preferences are captured.
"""
import ast
import copy
import os
from pathlib import Path

from .config import ConfigError,PRESETS,DEFAULT_PRESETS
from .owner_settings import validate_settings
from .provisioning import _control,_write,protected

NAME='state/gameplay-defaults.json'


def _validated(value,marker):
    if (type(value)is not dict or set(value)!={'format','installation_id','generation','profiles'}
            or type(value['format'])is not int or value['format']!=1
            or any(value.get(k)!=marker[k]for k in('installation_id','generation'))
            or type(value['profiles'])is not dict or set(value['profiles'])not in(set(),set(DEFAULT_PRESETS),set(PRESETS))):
        raise ConfigError('gameplay defaults binding requires review')
    for preset,settings in value['profiles'].items():validate_settings(settings,preset=preset)
    return value


def prepare(root,marker,*,uid,previous_release=None):
    """Root stopped-install seam; retain every existing config/state byte."""
    root=Path(root);path=root/NAME
    existing=_control(path,uid,private=True,optional=True)
    if existing is not None:return _validated(existing,marker)
    if previous_release is None:
        from .gameplay_defaults import settings_for
        profiles={preset:settings_for(preset)for preset in DEFAULT_PRESETS}
    else:
        old=Path(previous_release)/'poolrotater_host/gameplay_defaults.py'
        profiles={}
        if old.exists():
            # The caller has validated the complete protected predecessor. Read
            # its public literal, rather than importing old executable code or
            # broad runtime configurations which may contain integrations.
            protected(old)
            nodes=[n for n in ast.parse(old.read_text()).body if isinstance(n,ast.Assign)
                   and any(isinstance(t,ast.Name)and t.id=='PROFILES'for t in n.targets)]
            if len(nodes)!=1:raise ConfigError('predecessor gameplay defaults require review')
            table=ast.literal_eval(nodes[0].value)
            if type(table)is not dict or set(table) not in (set(DEFAULT_PRESETS),set(PRESETS)):raise ConfigError('predecessor gameplay defaults require review')
            profiles={preset:copy.deepcopy(profile['settings'])for preset,profile in table.items()}
    value=_validated({'format':1,'installation_id':marker['installation_id'],'generation':marker['generation'],
                      'profiles':profiles},marker)
    _write(path,value,owner=uid,private=True)
    return value


def settings_for(root,marker,preset):
    value=_control(Path(root)/NAME,os.geteuid(),private=True,optional=True)
    if value is None:
        if (Path(root)/'state/runtime-installation.json').exists():
            raise ConfigError('installed gameplay defaults require stopped upgrade preparation')
        # Credential-free setup/simulation has no installed predecessor.
        from .gameplay_defaults import settings_for as fresh
        return fresh(preset)
    profiles = _validated(value,marker)['profiles']
    return copy.deepcopy(profiles.get(preset,{}))
