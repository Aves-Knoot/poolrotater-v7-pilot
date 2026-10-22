"""Atomic booking-vs-update admission; no services or provider calls."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import os
import sqlite3
import uuid
from unittest.mock import patch

import pytest

from poolrotater_host import controller_update as updates
from poolrotater_host.bookings import policy
from poolrotater_host.bookings.admission import update_admission
from poolrotater_host.layout import LeaseBusy, atomic_json, file_lease


def setup(root, *, feature=True):
    for name in ('config','state','state/rolling'):
        (root/name).mkdir(mode=0o700,exist_ok=True)
    if feature:atomic_json(root/policy.NAME,{'format':1,'enabled':True,'aliases':['room1']})
    target={'root':root,'prefix':root/'prefix','unit':'invented.service'}
    parent={'service_uid':os.geteuid(),'release':'/invented/release'}
    job={'nonce':uuid.uuid4().hex,'before':parent,'configuration':{'cfg':'digest'},'phase':'waiting_empty'}
    return target,parent,job


def lease_row(root, cleanup=0):
    path=root/'state/bookings.sqlite3';path.touch(mode=0o600,exist_ok=True)
    with sqlite3.connect(path)as db:
        db.execute('CREATE TABLE IF NOT EXISTS host_bookings(alias TEXT,cleanup_complete INTEGER)')
        db.execute('DELETE FROM host_bookings')
        db.execute('INSERT INTO host_bookings VALUES(?,?)',('room1',cleanup))


def identity():
    return patch('poolrotater_host.ratings.operations._identity',lambda uid:nullcontext())


def test_legacy_gate_creates_no_booking_lock_or_state(tmp_path):
    target,parent,job=setup(tmp_path,feature=False)
    with identity():
        assert updates._gate(target,parent,job,'locking')is True
        assert (tmp_path/updates.GATE).exists()
        assert not (tmp_path/'state/booking-admission.lock').exists()
        assert updates._gate(target,parent,job,None)is True
    assert not (tmp_path/updates.GATE).exists()


def test_reservation_before_gate_blocks_update_and_cleanup_releases_it(tmp_path):
    target,parent,job=setup(tmp_path);lease_row(tmp_path)
    with identity():
        assert updates._gate(target,parent,job,'locking')is False
        assert not (tmp_path/updates.GATE).exists()
        lease_row(tmp_path,cleanup=1)
        assert updates._gate(target,parent,job,'locking')is True
        assert (tmp_path/updates.GATE).exists()


def test_controller_and_agent_use_the_same_exclusive_lock(tmp_path):
    target,parent,job=setup(tmp_path)
    # A new booking admission cannot pass while this protected check/write runs.
    with identity(),update_admission(tmp_path):
        with pytest.raises(LeaseBusy):
            with file_lease(tmp_path/'state/booking-admission.lock'):pass
    with file_lease(tmp_path/'state/booking-admission.lock'),identity():
        with pytest.raises(LeaseBusy):updates._gate(target,parent,job,'locking')
    assert not (tmp_path/updates.GATE).exists()


def test_malformed_booking_state_refuses_update_without_modifying_it(tmp_path):
    target,parent,job=setup(tmp_path)
    path=tmp_path/'state/bookings.sqlite3';path.write_bytes(b'invented corrupt DB');path.chmod(0o600)
    before=path.read_bytes()
    with identity():assert updates._gate(target,parent,job,'committing')is False
    assert path.read_bytes()==before
    assert not (tmp_path/updates.GATE).exists()


def test_stopped_update_sets_booking_gate_before_commit(tmp_path,monkeypatch):
    target,parent,job=setup(tmp_path);job.pop('nonce')
    proof={'confirmed':'closed'};mutations=[]
    monkeypatch.setattr('poolrotater_host.managed_host.admission.stopped_host',lambda *a,**k:proof)
    monkeypatch.setattr('poolrotater_host.managed_host.admission.stop_only_configuration',lambda *a:True)
    monkeypatch.setattr('poolrotater_host.updates.recovery._approval',lambda *a:None)
    monkeypatch.setattr('poolrotater_host.pilot_operations._service_state',lambda *a:{'MainPID':0,'ActiveState':'inactive'})
    monkeypatch.setattr(updates,'_config_hashes',lambda root:job['configuration'])
    def commit(*args):
        assert (tmp_path/updates.GATE).exists()
        mutations.append('approved-commit')
    monkeypatch.setattr(updates,'_commit',commit)
    with identity():assert updates._step_managed_stopped('invented',target,parent,job,lambda **v:job.update(v))is True
    assert mutations==['approved-commit']
    assert job['phase']=='committing'


def test_stopped_update_with_new_booking_never_calls_commit(tmp_path,monkeypatch):
    target,parent,job=setup(tmp_path);lease_row(tmp_path)
    monkeypatch.setattr('poolrotater_host.managed_host.admission.stopped_host',lambda *a,**k:{'confirmed':'closed'})
    monkeypatch.setattr('poolrotater_host.managed_host.admission.stop_only_configuration',lambda *a:True)
    monkeypatch.setattr('poolrotater_host.updates.recovery._approval',lambda *a:None)
    monkeypatch.setattr('poolrotater_host.pilot_operations._service_state',lambda *a:{'MainPID':0,'ActiveState':'inactive'})
    monkeypatch.setattr(updates,'_config_hashes',lambda root:job['configuration'])
    monkeypatch.setattr(updates,'_commit',lambda *a:pytest.fail('booking admitted update'))
    with identity():assert updates._step_managed_stopped('invented',target,parent,job,lambda **v:job.update(v))is True
    assert job['phase']=='waiting_empty'
    assert job['reason']=='private_booking_reserved_or_active'


def test_stopped_installer_refuses_outstanding_booking_before_yield(tmp_path,monkeypatch):
    from poolrotater_host import pilot_upgrade
    from poolrotater_host.config import ConfigError
    target,parent,job=setup(tmp_path);lease_row(tmp_path)
    target['prefix'].parent.mkdir(parents=True,exist_ok=True)
    marker={'installation_id':str(uuid.uuid4())}
    record={'service_uid':os.geteuid()}
    monkeypatch.setattr(pilot_upgrade,'_admin',lambda:None)
    monkeypatch.setattr(pilot_upgrade,'_binding',lambda instance:(target,marker,tmp_path/'record.json',record))
    monkeypatch.setattr(pilot_upgrade,'_unit_state',lambda target:None)
    monkeypatch.setattr(pilot_upgrade,'_admission_guard',lambda *a:nullcontext())
    with identity(),pytest.raises(ConfigError,match='private booking blocks stopped upgrade'):
        with pilot_upgrade._locked('invented'):
            pytest.fail('outstanding reservation admitted stopped upgrade')
    lease_row(tmp_path,cleanup=1)
    with identity(),pilot_upgrade._locked('invented'):
        pass
