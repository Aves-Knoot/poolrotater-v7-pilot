"""Portable unit tests for bounded authenticated catalog-stage retries."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poolrotater_host import catalog_trust
from poolrotater_host.config import ConfigError
from poolrotater_host.managed_host import catalogs
from tuf.api.exceptions import DownloadError

assert Path(catalogs.__file__).resolve().is_relative_to(ROOT), catalogs.__file__
tuf_errors = type('TufErrors', (), {'DownloadError': DownloadError})


class FakeTrust:
    identity = 'a' * 64
    fixture = True


class FakeStore:
    def __init__(self, path):
        self.path = Path(path)
        self.root = self.path / 'root'
        self.fixture = True
        self.binding = {'installation_id': 'install', 'generation': 'generation',
                        'service_uid': 955, 'host_root': '/host'}
        self.trust = FakeTrust()
        self.reference = {'serial': 4, 'modes': {'standard': 'old'}}
        self.candidate = None
        self.pending = None
        self.calls = []
        self.outcomes = []

    def current(self, allow_pending=False):
        return self.reference

    def _read(self, name, optional=False):
        return {'candidate.json': self.candidate,
                'pending.json': self.pending}.get(name)

    def stage(self, repository, modes):
        self.calls.append((repository, tuple(modes)))
        outcome = self.outcomes.pop(0) if self.outcomes else 'ok'
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def wrapped_download_error(cause=None, *, message='transport failure'):
    try:
        if cause is None:
            raise tuf_errors.DownloadError(message)
        try:
            raise cause
        except BaseException as transport:
            raise tuf_errors.DownloadError(message) from transport
    except tuf_errors.DownloadError as download:
        error = ConfigError('catalog trust/validation failed: DownloadError')
        error.__cause__ = download
        return error


class ManagedCatalogTransferRetryFollowupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = FakeStore(self.tmp.name)
        cache = self.store.path / 'cache'
        (cache / 'metadata').mkdir(parents=True)
        self.repository = object()
        self.dependencies_patch = patch.object(
            catalog_trust, 'dependencies', return_value=(None, None, None, tuf_errors))
        self.dependencies_patch.start()
        self.addCleanup(self.dependencies_patch.stop)

    def transient(self, exc):
        return wrapped_download_error(exc)

    def test_only_exact_transient_wrapped_download_failures_retry(self):
        for exc in (TimeoutError(), ConnectionResetError(), ConnectionAbortedError(),
                    BrokenPipeError(), __import__('http.client').client.IncompleteRead(b'x'),
                    __import__('http.client').client.RemoteDisconnected('closed')):
            with self.subTest(exception=type(exc).__name__):
                self.assertTrue(catalogs._transient_stage_failure(self.transient(exc)))
        self.assertTrue(catalogs._transient_stage_failure(
            wrapped_download_error(message='truncated fixture response')))

    def test_tls_and_other_trust_or_store_failures_never_retry(self):
        tls = __import__('ssl').SSLCertVerificationError('untrusted certificate')
        self.assertFalse(catalogs._transient_stage_failure(self.transient(tls)))
        try:
            raise tls
        except __import__('ssl').SSLCertVerificationError as certificate_error:
            try:
                raise TimeoutError('outer transport timeout') from certificate_error
            except TimeoutError as outer_timeout:
                self.assertFalse(catalogs._transient_stage_failure(
                    self.transient(outer_timeout)))
        for error in (ConfigError('catalog staging would consume disk reserve'),
                      ConfigError('catalog target digest mismatch'),
                      ConfigError('catalog trust/validation failed: DownloadError'),
                      ConfigError('catalog trust/validation failed: ValueError'),
                      OSError('connection reset'),
                      wrapped_download_error(message='HTTP status refused')):
            with self.subTest(error=str(error)):
                self.assertFalse(catalogs._transient_stage_failure(error))

    def test_transient_retry_reuses_exact_store_and_repository_then_succeeds(self):
        self.store.outcomes = [self.transient(ConnectionResetError()), 'done']
        sleeps = []
        result = catalogs._stage_with_transient_retry(
            self.store, self.repository, ['standard'], sleep=sleeps.append)
        self.assertEqual(result, 'done')
        self.assertEqual(self.store.calls,
                         [(self.repository, ('standard',)),
                          (self.repository, ('standard',))])
        self.assertEqual(sleeps, [0.2])

    def test_retry_refuses_changed_current_candidate_or_pending(self):
        for changed in ('reference', 'candidate', 'pending', 'binding', 'trust'):
            with self.subTest(changed=changed):
                self.store = FakeStore(self.tmp.name)
                self.store.outcomes = [self.transient(TimeoutError())]

                def mutate(_delay):
                    if changed == 'reference':
                        self.store.reference = {'serial': 5, 'modes': {'standard': 'new'}}
                    elif changed == 'candidate':
                        self.store.candidate = {'format': 1}
                    elif changed == 'pending':
                        self.store.pending = {'format': 1}
                    elif changed == 'binding':
                        self.store.binding['generation'] = 'other-generation'
                    else:
                        self.store.trust.identity = 'b' * 64

                with self.assertRaisesRegex(ConfigError, 'managed_catalog_retry_state_changed'):
                    catalogs._stage_with_transient_retry(
                        self.store, self.repository, ['standard'], sleep=mutate)
                self.assertEqual(len(self.store.calls), 1)

    def test_retry_refuses_metadata_change_or_bad_custody(self):
        metadata_file = self.store.path / 'cache/metadata/snapshot.json'
        metadata_file.write_bytes(b'first')

        def change(_delay):
            metadata_file.write_bytes(b'changed')

        self.store.outcomes = [self.transient(ConnectionResetError()), 'done']
        with self.assertRaisesRegex(ConfigError, 'managed_catalog_retry_metadata_changed'):
            catalogs._stage_with_transient_retry(
                self.store, self.repository, ['standard'], sleep=change)
        self.assertEqual(len(self.store.calls), 1)

        self.store = FakeStore(self.tmp.name)
        (self.store.path / 'cache/metadata').mkdir(parents=True, exist_ok=True)
        bad = self.store.path / 'cache/metadata/unexpected.json'
        bad.write_text('{}')
        self.store.outcomes = [self.transient(TimeoutError()), 'done']
        with self.assertRaises(ConfigError):
            catalogs._stage_with_transient_retry(
                self.store, self.repository, ['standard'], sleep=lambda _delay: None)
        self.assertEqual(len(self.store.calls), 1)

    def test_retry_budget_is_three_and_exhaustion_preserves_last_failure(self):
        failure = self.transient(BrokenPipeError())
        self.store.outcomes = [failure, failure, failure]
        sleeps = []
        with self.assertRaises(ConfigError) as raised:
            catalogs._stage_with_transient_retry(
                self.store, self.repository, ['standard'], sleep=sleeps.append)
        self.assertIs(raised.exception, failure)
        self.assertEqual(len(self.store.calls), 3)
        self.assertEqual(sleeps, [0.2, 0.4])

    def test_both_preparation_paths_route_stage_through_retry_wrapper(self):
        import inspect
        self.assertIn('_stage_with_transient_retry(store,repository,sorted(',
                      inspect.getsource(catalogs.prepare_store).replace(' ', '').replace('\n', ''))
        standard_source = inspect.getsource(catalogs._refresh_standard_store_locked)
        self.assertGreaterEqual(standard_source.count('_stage_with_transient_retry('), 2)


if __name__ == '__main__':
    unittest.main()
