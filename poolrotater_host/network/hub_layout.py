"""Explicit hub initialization: separate private root, never an operator host."""
from pathlib import Path
import os
import stat
import uuid

from ..layout import checked_root, read_json, atomic_json
from .contracts import require, identifier

NAME = '.poolrotater-network-hub.json'


def check(value):
    root=checked_root(Path(value));require(root.is_dir() and not stat.S_IMODE(root.stat().st_mode)&0o077,'private_hub_root_required')
    require(not (root/'.poolrotater-host.json').exists(),'operator_host_is_not_hub')
    marker=read_json(root/NAME)
    require(type(marker) is dict and set(marker)=={'format','purpose','hub_id'} and type(marker['format']) is int and marker['format']==1 and marker['purpose']=='poolrotater-network-hub','foreign_hub_root')
    identifier(marker['hub_id']);return root,marker


def initialize(value):
    root=checked_root(Path(value))
    if root.exists() and (root/NAME).exists():return check(root)[1]
    if root.exists():require(root.is_dir() and not any(root.iterdir()) and not stat.S_IMODE(root.stat().st_mode)&0o077,'fresh_private_hub_root_required')
    else:root.mkdir(mode=0o700)
    marker={'format':1,'purpose':'poolrotater-network-hub','hub_id':str(uuid.uuid4())}
    atomic_json(root/NAME,marker);return marker
