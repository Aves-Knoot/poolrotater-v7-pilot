"""Predictable native staging pressure defers before operation allocation."""
import unittest
from unittest.mock import Mock

from poolrotater_host.config import ConfigError
from tests.test_update_agent import UpdateAgentTests
from tests import test_update_release_delivery as delivery


class UpdateStagingCapacityTests(unittest.TestCase):
    setUp = delivery.ReleaseDeliveryTests.setUp
    publish = delivery.ReleaseDeliveryTests.publish
    serve = delivery.ReleaseDeliveryTests.serve
    client = delivery.ReleaseDeliveryTests.client
    setup_agent = UpdateAgentTests.setup_agent

    def test_low_space_defers_without_operation_then_retries_and_stages_once(self):
        agent = self.setup_agent(automatic_download=True, automatic_install=True)
        readiness = iter((False, True))
        self.backend.staging_ready = lambda path, item: next(readiness)

        deferred = agent.step()
        self.assertEqual(deferred['phase'], 'downloaded')
        self.assertEqual(deferred['error'], 'update_staging_capacity_wait')
        self.assertEqual(deferred['next_check_at'], self.now + 60)
        self.assertIsNone(agent.state()['operation'])
        self.backend.stage.assert_not_called()
        self.backend.queue.assert_not_called()

        self.now += 60
        self.assertEqual(agent.step()['phase'], 'waiting_empty')
        operation = agent.state()['operation']
        self.assertIsNotNone(operation)
        self.backend.stage.assert_called_once()
        self.backend.queue.assert_called_once_with(
            self.backend.stage.call_args.args[0],
            self.backend.stage.call_args.args[1], operation['id'])

        # Normal reconciliation of the accepted operation does not preflight
        # or stage it a second time.
        self.assertEqual(agent.step()['phase'], 'waiting_empty')
        self.backend.stage.assert_called_once()
        self.backend.queue.assert_called_once()

    def test_stage_exception_remains_held_with_the_durable_operation(self):
        agent = self.setup_agent(automatic_download=True, automatic_install=True)
        self.backend.staging_ready = lambda path, item: True
        self.backend.stage.side_effect = ConfigError('native stage failed')

        result = agent.step()
        self.assertEqual(result['phase'], 'held')
        self.assertEqual(result['error'], 'update_installation_requires_review')
        self.assertIsNotNone(agent.state()['operation'])
        self.backend.queue.assert_not_called()

    def test_preexisting_deployment_busy_guard_runs_before_capacity_hook(self):
        agent = self.setup_agent(automatic_download=True, automatic_install=True)
        readiness = Mock(return_value=True)
        self.backend.staging_ready = readiness
        self.backend.current.return_value['deployment_busy'] = True

        result = agent.step()
        self.assertEqual(result['error'], 'update_other_deployment_pending')
        readiness.assert_not_called()
        self.assertIsNone(agent.state()['operation'])
        self.backend.stage.assert_not_called()

    def test_installed_version_downgrade_guard_runs_before_capacity_hook(self):
        agent = self.setup_agent(automatic_download=True, automatic_install=True)
        readiness = Mock(return_value=True)
        self.backend.staging_ready = readiness
        self.backend.current.return_value['version'] = 'v7.0.4'

        result = agent.step()
        self.assertEqual(result['phase'], 'held')
        readiness.assert_not_called()
        self.assertIsNone(agent.state()['operation'])
        self.backend.stage.assert_not_called()

    def test_capacity_hook_failure_is_held_with_fixed_error(self):
        agent = self.setup_agent(automatic_download=True, automatic_install=True)
        self.backend.staging_ready = Mock(side_effect=OSError('private filesystem detail'))

        result = agent.step()
        self.assertEqual(result['phase'], 'held')
        self.assertEqual(result['error'], 'update_installation_requires_review')
        self.assertNotIn('private filesystem detail', repr(result))
        self.assertIsNone(agent.state()['operation'])
        self.backend.stage.assert_not_called()
        self.backend.queue.assert_not_called()

    def test_truthy_string_capacity_result_is_not_accepted_as_true(self):
        self._assert_invalid_capacity_result('ready')

    def test_integer_capacity_result_is_not_accepted_as_true(self):
        self._assert_invalid_capacity_result(1)

    def test_none_capacity_result_is_not_accepted_as_true(self):
        self._assert_invalid_capacity_result(None)

    def _assert_invalid_capacity_result(self, value):
        agent = self.setup_agent(automatic_download=True, automatic_install=True)
        self.backend.staging_ready = Mock(return_value=value)
        result = agent.step()
        self.assertEqual(result['phase'], 'held')
        self.assertEqual(result['error'], 'update_installation_requires_review')
        self.assertIsNone(agent.state()['operation'])
        self.backend.stage.assert_not_called()
        self.backend.queue.assert_not_called()

    def test_class_declared_capacity_method_is_discovered_and_can_defer(self):
        agent = self.setup_agent(automatic_download=True, automatic_install=True)
        delegate = self.backend

        class DeclaredHookBackend:
            def __init__(self, wrapped):
                self.wrapped = wrapped
                self.calls = []

            def staging_ready(self, path, item):
                self.calls.append((path, item))
                return False

            def __getattr__(self, name):
                return getattr(self.wrapped, name)

        backend = DeclaredHookBackend(delegate)
        agent.backend = backend
        result = agent.step()
        self.assertEqual(result['phase'], 'downloaded')
        self.assertEqual(result['error'], 'update_staging_capacity_wait')
        self.assertEqual(len(backend.calls), 1)
        self.assertIsNone(agent.state()['operation'])
        delegate.stage.assert_not_called()
        delegate.queue.assert_not_called()


if __name__ == '__main__':
    unittest.main()
