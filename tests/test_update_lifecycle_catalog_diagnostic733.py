"""Catalog preparation failures stay identifiable without affecting recovery policy."""
from scripts.test_isolation import require_test_isolation
require_test_isolation('ordinary')

import unittest

from poolrotater_host.config import ConfigError
from poolrotater_host.update_lifecycle import (
    PREPARATION_REVIEW_CODES,automatic_eligible,diagnostic)


class CatalogPreparationDiagnosticTests(unittest.TestCase):
    def test_closed_catalog_review_codes_are_preserved_as_diagnostics(self):
        for code in PREPARATION_REVIEW_CODES:
            with self.subTest(code=code):
                self.assertEqual(diagnostic(ConfigError(code)),code)

    def test_unknown_configuration_errors_remain_generic_and_do_not_leak_text(self):
        error=ConfigError('private path or secret must not appear in diagnostics')
        self.assertEqual(diagnostic(error),'update_guard_requires_review')
        self.assertNotIn('private path',diagnostic(error))

    def test_catalog_review_codes_do_not_expand_automatic_recovery(self):
        for code in PREPARATION_REVIEW_CODES:
            job={'automatic_recovery':{'format':1,'attempts':0},
                'commit_admitted_at':__import__('time').time(),'phase':'committing',
                'failure_code':code}
            with self.subTest(code=code):
                self.assertFalse(automatic_eligible(job))


if __name__=='__main__':unittest.main()
