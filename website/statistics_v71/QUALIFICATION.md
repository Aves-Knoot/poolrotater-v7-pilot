# Staged website qualification

38 targeted tests passed inside `scripts/offline` after the final publisher and
private-file reader changes:

```
scripts/offline .venv/bin/python -m unittest \
  tests.test_network_statistics tests.test_network_statistics_web \
  tests.test_network_statistics_deployment tests.test_network_statistics_publisher -q
```

Coverage includes real authenticated hub enrollment/consent/ingestion through
`Repository.snapshot` into a private atomic projection and public aggregation;
real PHP rendering; read-only owner SQL history; stable-ID uniqueness; overlapping
cross-host intervals; duplicate/replayed matches and tracked PP achievements;
reviewed cumulative baseline replacement; owner duplicate exclusion; privacy
across named views with anonymous totals; independent consent revocation; stale
snapshots; malicious host/player labels; symlink/FIFO/permission refusal; no-JS
host filters; and guarded apply/rollback in a staging filesystem.

The original source was copied from the active exporter/template as code only.
`baseline.json` pins those original hashes. No production database, credential,
actual provider dispatch, room operation, live website write or service restart
was used for this qualification. PHP syntax and Python compilation also passed.

Not executed here: full unrelated unit suites, browser screenshots, donor
installed common-runtime upgrades, full-catalog live-engine validation, or
production deployment. Those remain integration/release acceptance items owned
by the coordinating agent. This staged code is not evidence that community
statistics sharing has been enabled or that public totals currently include it.
