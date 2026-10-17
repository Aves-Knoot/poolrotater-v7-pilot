# v7.1.7 live room startup and refresh repair

Fixes a live-only adapter mismatch introduced by the v7.1.5 owner-verification admission checks. The fixed HTTP transport now exposes its existing configured request timeout through the same read-only interface as the fixture transport. No deadline, pacing budget, credential, ownership check, storage schema or gameplay setting changes.

Without this repair, initial room creation could succeed but owner verification raised an error before its read, leaving the saved creation candidate in recovery. Automatic empty-room refresh could also fail at the same interface. v7.1.6 prompt feedback and all existing gameplay features are retained.

Existing running hosts receive a controller update through the signed channel when all configured rooms are empty. Keep healthy occupied rooms running. A stopped or recovery-required host must resolve saved ownership using the documented checks before starting once. Never discard journals, reinstall, reset OAuth or blindly repeat room creation.

Use the v7.1.7 kit and its repair launcher for an existing stopped operator installation. The systemd timer repair and safe failure codes from v7.1.6-repair1 are included. The older repair launchers retain their original pins and should not be used for a new repair attempt.

Validation uses the actual fixed transport classes for successful startup owner verification, bounded cooldown waits, refresh readiness and owner-mismatch refusal. A regression reproduces the failure against the exact published v7.1.6 source. A protected root/service-UID upgrade with complete Standard, Mania 4K and Mania 7K catalogs preserves configuration, credentials, explicit opt-outs, catalog rows, ledger schema/history and incident ownership bytes. Affected provider/room/feedback regressions are run; unchanged whole-repository and donor suites are not repeated. Real gameplay acceptance remains separate from startup checks.
