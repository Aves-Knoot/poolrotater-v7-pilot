import pytest
from types import SimpleNamespace

from poolrotater_host.bookings import policy
from poolrotater_host.bookings import local as local_module
from poolrotater_host.bookings.update_guard import holds_updates, require_update_allowed
from poolrotater_host.layout import atomic_json
from poolrotater_host.network.contracts import NetworkError


def enable(root, aliases=('room1',)):
    (root / 'config').mkdir(mode=0o700, exist_ok=True)
    atomic_json(root / 'config/private-bookings.json',
                {'format': 1, 'enabled': True, 'aliases': list(aliases)})


def test_update_guard_is_inert_without_explicit_slot_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, 'holds_updates', lambda root: True)
    assert holds_updates(tmp_path, 'room1') is False
    assert require_update_allowed(tmp_path, 'room1') is True


def test_update_guard_checks_enabled_alias_and_wraps_root_hold(tmp_path, monkeypatch):
    enable(tmp_path)
    (tmp_path / 'state').mkdir(mode=0o700, exist_ok=True)
    (tmp_path / 'state/bookings.sqlite3').write_bytes(b'ledger')
    monkeypatch.setattr(policy, 'holds_updates', lambda root: True)
    monkeypatch.setattr(local_module, 'LocalSessions', lambda root, binding: SimpleNamespace(
        leases=lambda include_ended=False: [
            {'lease': {'id': 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'alias': 'room1'},
             'state': {'phase': 'active'}, 'cleanup_complete': False},
        ]))
    import poolrotater_host.managed_host.policy as managed_policy
    monkeypatch.setattr(managed_policy, 'state', lambda root: SimpleNamespace(binding={}))
    assert holds_updates(tmp_path, 'room1') is True
    assert holds_updates(tmp_path, 'room2') is False
    with pytest.raises(NetworkError) as blocked:
        require_update_allowed(tmp_path, 'room1')
    assert blocked.value.code == 'booking_update_blocked'


def test_malformed_lease_state_and_alias_fail_closed(tmp_path, monkeypatch):
    def malformed(_root):
        raise ValueError('private state cannot be decoded')

    monkeypatch.setattr(policy, 'holds_updates', malformed)
    assert holds_updates(tmp_path, 'room1') is False  # No ledger means no lease to hold.
    (tmp_path / 'state').mkdir(mode=0o700, exist_ok=True)
    (tmp_path / 'state/bookings.sqlite3').write_bytes(b'corrupt')
    assert holds_updates(tmp_path, 'room1') is True
    assert holds_updates(tmp_path, 'unknown') is True
