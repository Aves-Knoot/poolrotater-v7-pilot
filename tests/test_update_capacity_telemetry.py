"""Safe fixed updater errors must survive local projection and hub ingestion."""
from scripts.test_isolation import require_test_isolation
require_test_isolation("ordinary")

from contextlib import contextmanager
import copy
from pathlib import Path
import tempfile
import unittest
import uuid

from poolrotater_host.network.contracts import NetworkError, secret, verifier
from poolrotater_host.updates.telemetry import ERRORS, Reports, projection, report


class Authority:
    def __init__(self, root):
        self.root = root
        self.now = 2_000_000_000
        self.token = secret()
        self.host_id = str(uuid.uuid4())

    def clock(self):
        return self.now

    @contextmanager
    def transaction(self):
        yield None

    def _authenticate(self, db, digest, scope):
        if digest != verifier(self.token) or scope != "heartbeat":
            raise NetworkError("credential_refused", 401)
        return {"id": self.host_id}, {}


class UpdateCapacityTelemetryTests(unittest.TestCase):
    def summary(self, error):
        return dict(channel="pilot", phase="downloaded", paused=False,
                    installed_version="v7.3.8", available_version="v7.3.10",
                    available_kind="controller", waiting_rooms=[], error=error,
                    download={"bytes": 56051838, "total": 56051838})

    def payload(self, error, sequence=1, now=2_000_000_000):
        return projection(self.summary(error), sequence=sequence, now=now)

    def test_capacity_wait_projects_without_relabeling_or_dropping_state(self):
        value = self.payload("update_staging_capacity_wait")
        self.assertEqual(value["error"], "update_staging_capacity_wait")
        self.assertEqual(value["phase"], "downloaded")
        self.assertEqual(value["installed_version"], "v7.3.8")
        self.assertEqual(value["protocol_version"], 1)

    def test_stopped_worker_release_refusal_projects_as_fixed_code(self):
        value = self.payload("update_stopped_host_requires_controller_release")
        self.assertEqual(value["error"], "update_stopped_host_requires_controller_release")

    def test_new_fixed_codes_are_accepted_and_read_back_by_hub(self):
        with tempfile.TemporaryDirectory(dir="scratch") as directory:
            root = Path(directory); root.chmod(0o700)
            authority = Authority(root)
            repository = Reports(authority)
            for sequence, error in enumerate(
                    ("update_staging_capacity_wait",
                     "update_stopped_host_requires_controller_release"), 1):
                authority.now += 10
                value = self.payload(error, sequence, authority.now)
                receipt = repository.accept(authority.token, value)
                self.assertTrue(receipt["ok"])
                self.assertFalse(receipt["replayed"])
                observed = repository.local(authority.host_id)["report"]
                self.assertEqual(observed, value)

    def test_existing_error_codes_remain_accepted(self):
        old = (None, "update_installation_requires_review",
               "update_deployment_receipt_missing", "update_other_deployment_pending",
               "update_check_or_download_failed", "update_host_not_running",
               "update_release_requires_manual_retry")
        for error in old:
            with self.subTest(error=error):
                self.assertEqual(report(self.payload(error))["error"], error)

    def test_arbitrary_error_text_and_unknown_codes_remain_refused(self):
        good = self.payload(None)
        for error in ("private token detail", "update_invented_error", "",
                      "update_staging_capacity_wait\nsecret", "x" * 4096):
            value = {**good, "error": error}
            with self.subTest(error=error[:40]), self.assertRaises(NetworkError):
                report(value)

    def test_new_codes_do_not_accept_extra_fields_or_protocol_changes(self):
        value = self.payload("update_staging_capacity_wait")
        for changed in ({**value, "private_detail": "no"},
                        {**value, "protocol_version": 2},
                        {**value, "download_bytes": value["download_total"] + 1}):
            with self.assertRaises(NetworkError):
                report(changed)

    def test_new_codes_do_not_bypass_authentication(self):
        with tempfile.TemporaryDirectory(dir="scratch") as directory:
            root = Path(directory); root.chmod(0o700)
            authority = Authority(root); repository = Reports(authority)
            with self.assertRaises(NetworkError):
                repository.accept(secret(), self.payload("update_staging_capacity_wait"))
            self.assertIsNone(repository.local(authority.host_id)["report"])

    def test_capacity_wait_replay_is_idempotent_and_conflict_refused(self):
        with tempfile.TemporaryDirectory(dir="scratch") as directory:
            root = Path(directory); root.chmod(0o700)
            authority = Authority(root); repository = Reports(authority)
            value = self.payload("update_staging_capacity_wait")
            repository.accept(authority.token, value)
            self.assertTrue(repository.accept(authority.token, copy.deepcopy(value))["replayed"])
            changed = {**value, "error": None}
            with self.assertRaises(NetworkError):
                repository.accept(authority.token, changed)
            self.assertEqual(repository.local(authority.host_id)["report"], value)


if __name__ == "__main__":
    unittest.main()
