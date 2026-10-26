"""Approved staged successors retain proof-gated recovery on older agents."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')
import hashlib
from unittest.mock import patch
from poolrotater_host import controller_update as update
from poolrotater_host import pilot_operations as pilot


def test_approved_waiting_jobs_arm_a_recovery_timer():
    with patch.object(update, 'arm', return_value={'ok': True}) as arm:
        assert update._ensure_automatic_recovery_timer('fixture', {
            'phase': 'waiting_empty', 'release_approval_required': True}) is True
        arm.assert_called_once_with('fixture')


def test_nonautomatic_or_terminal_jobs_do_not_arm():
    with patch.object(update, 'arm') as arm:
        for job in ({'phase': 'waiting_empty', 'release_approval_required': False},
                    {'phase': 'waiting_empty'},
                    {'phase': 'held', 'release_approval_required': True},
                    {'phase': 'committing', 'release_approval_required': True},
                    {'phase': 'complete', 'release_approval_required': True}):
            assert update._ensure_automatic_recovery_timer('fixture', job) is False
        arm.assert_not_called()


def test_exact_active_timer_is_reused_and_inactive_timer_rearmed(tmp_path, monkeypatch):
    timer=tmp_path/'poolrotater-v7-controller-update-fixture.timer'
    timer.write_text('invented owned timer')
    digest=hashlib.sha256(timer.read_bytes()).hexdigest()
    monkeypatch.setattr(pilot, 'UNIT_BASE', tmp_path)
    monkeypatch.setattr(update, 'protected', lambda p:p)
    monkeypatch.setattr(pilot, '_systemctl', lambda *args: b'ActiveState=active\n')
    job={'phase':'waiting_empty','release_approval_required':True,'timer_sha256':digest}
    with patch.object(update, 'arm') as arm:
        assert update._ensure_automatic_recovery_timer('fixture',job) is True
        arm.assert_not_called()
        monkeypatch.setattr(pilot, '_systemctl', lambda *args: b'ActiveState=inactive\n')
        assert update._ensure_automatic_recovery_timer('fixture',job) is True
        arm.assert_called_once_with('fixture')


def test_changed_timer_cannot_be_silently_adopted(tmp_path, monkeypatch):
    timer=tmp_path/'poolrotater-v7-controller-update-fixture.timer';timer.write_text('unexpected timer')
    monkeypatch.setattr(pilot, 'UNIT_BASE', tmp_path)
    monkeypatch.setattr(update, 'protected', lambda p:p)
    with patch.object(update, 'arm', side_effect=update.ConfigError('unrecognized controller update unit')) as arm:
        try:
            update._ensure_automatic_recovery_timer('fixture', {'phase':'waiting_empty',
                'release_approval_required':True,'timer_sha256':'0'*64})
        except update.ConfigError:
            pass
        else:
            raise AssertionError('unexpected timer must be refused')
        arm.assert_called_once_with('fixture')


def test_staged_successor_step_checks_timer_before_native_admission(tmp_path):
    job={'phase':'waiting_empty','release_approval_required':True}
    target={'prefix':tmp_path}
    class Seen(Exception):
        pass
    with patch.object(update, '_binding', return_value=(target, {}, {}, {})), \
         patch.object(update, '_control', return_value=job), \
         patch.object(update, '_ensure_automatic_recovery_timer', side_effect=Seen) as ensure:
        try:
            update.step('fixture')
        except Seen:
            pass
        else:
            raise AssertionError('timer must be checked before admission')
        ensure.assert_called_once_with('fixture',job)
