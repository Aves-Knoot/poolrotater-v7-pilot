"""Known local guards must not label a dispatched operation as retryable."""
import queue
import unittest

from bot.hub import HubDisconnected, HubInvocationUncertain, HubOperationDeferred
from bot.multiroom import SharedReferee, RoomHub
from rolling.adapters import call
from rolling.protocol import RemoteError


class Upstream:
    def __init__(self):
        self.connected = True
        self.calls = []
        self.error = None
        self._connect_generation = 1

    def is_connected(self):
        return self.connected

    def invoke(self, method, args, **kwargs):
        self.calls.append(method)
        if self.error is not None:
            raise self.error
        return None


def room():
    upstream = Upstream()
    shared = SharedReferee(upstream, queue.Queue())
    view = RoomHub(shared, 'room2', queue.Queue())
    view.owned.add(102)
    view.joined[102] = (upstream._connect_generation, {'room_id': 102})
    return upstream, shared, view


class LocalDeferralContract(unittest.TestCase):
    def test_fenced_roster_does_not_dispatch(self):
        upstream, _, view = room()
        view.roster_fenced = True
        with self.assertRaises(HubOperationDeferred) as caught:
            view.invoke('ChangeRoomSettings', [102, {'password': 'test-only'}])
        self.assertEqual(caught.exception.reason, 'roster_snapshot_pending')
        self.assertEqual(upstream.calls, [])

    def test_disconnected_precheck_does_not_dispatch(self):
        upstream, _, view = room()
        upstream.connected = False
        with self.assertRaises(HubOperationDeferred) as caught:
            view.invoke('ChangeRoomSettings', [102, {'password': 'test-only'}])
        self.assertEqual(caught.exception.reason, 'referee_not_connected')
        self.assertEqual(upstream.calls, [])

    def test_shared_operation_busy_does_not_dispatch(self):
        class Busy:
            def acquire(self, timeout): return False
        upstream, shared, view = room()
        shared.lock = Busy()
        with self.assertRaises(HubOperationDeferred) as caught:
            view.invoke('ChangeRoomSettings', [102, {'password': 'test-only'}])
        self.assertEqual(caught.exception.reason, 'shared_referee_operation_busy')
        self.assertEqual(upstream.calls, [])

    def test_stale_membership_does_not_dispatch(self):
        upstream, _, view = room()
        view.joined.clear()
        with self.assertRaises(HubOperationDeferred) as caught:
            view.invoke('ChangeRoomSettings', [102, {'password': 'test-only'}])
        self.assertEqual(caught.exception.reason, 'referee_membership_stale')
        self.assertEqual(upstream.calls, [])

    def test_identical_disconnected_text_after_dispatch_is_not_reclassified(self):
        upstream, _, view = room()
        error = HubDisconnected('shared referee transport is disconnected')
        upstream.error = error
        with self.assertRaises(HubDisconnected) as caught:
            view.invoke('ChangeRoomSettings', [102, {'password': 'test-only'}])
        self.assertIs(caught.exception, error)
        self.assertNotIsInstance(caught.exception, HubOperationDeferred)
        self.assertEqual(upstream.calls, ['ChangeRoomSettings'])

    def test_uncertain_downstream_result_remains_the_same_exception(self):
        upstream, _, view = room()
        error = HubInvocationUncertain('test-only-lost-completion')
        upstream.error = error
        with self.assertRaises(HubInvocationUncertain) as caught:
            view.invoke('ChangeRoomSettings', [102, {'password': 'test-only'}])
        self.assertIs(caught.exception, error)
        self.assertEqual(upstream.calls, ['ChangeRoomSettings'])

    def test_rpc_deferral_requires_a_finite_known_message(self):
        class Peer:
            def __init__(self, message): self.message = message
            def call(self, *args, **kwargs):
                raise RemoteError('HubOperationDeferred', self.message)
        with self.assertRaises(HubOperationDeferred) as caught:
            call(Peer('room is quiesced'), 'hub.invoke', 'ChangeRoomSettings', [])
        self.assertEqual(caught.exception.reason, 'room_quiesced')
        with self.assertRaises(ValueError):
            call(Peer('arbitrary-untrusted-payload'), 'hub.invoke', 'ChangeRoomSettings', [])

    def test_wrong_room_authority_is_not_a_transient_deferral(self):
        upstream, _, view = room()
        with self.assertRaises(ValueError):
            view.invoke('ChangeRoomSettings', [999, {'password': 'test-only'}])
        self.assertEqual(upstream.calls, [])
