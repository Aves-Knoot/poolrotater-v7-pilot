# v7.1.0 validation evidence

The initial source sweep ran **2,995 tests** across four offline shards. It was
not green: it recorded **26 failure and 24 error diagnostics**, affecting
**47 distinct test methods**, plus **two root-only skips**. Four subtests of one
method account for the difference between diagnostic entries and methods.
Every initial diagnostic now has relevant passing rerun evidence. The full
suite was **not repeated** after the localized fixes.

The machine-readable [resolution receipt](../validation/v7.1.0/regression-resolution.json)
records each original test ID and subtest, its original log hash, its passing
rerun evidence and the provenance of that selection. It retains failed
intermediate runs; their totals must not be presented as additional passing
suites. Test counts from overlapping runs must not be summed into a new
unique-test total.

## Original sweep and resolution

| Shard | Tests run | Original result |
| --- | ---: | --- |
| 0 | 733 | 5 failures, 5 errors |
| 1 | 674 | 18 failures, 6 errors, 1 skip |
| 2 | 691 | 1 failure, 7 errors |
| 3 | 897 | 2 failures, 6 errors, 1 skip |

The resolution evidence is:

| Evidence | Result | Original diagnostics covered |
| --- | --- | ---: |
| `legacy-fixture-regression-fixes.log` | 53 run; 52 passed, 1 root-only skip | 21 |
| `legacy-fixture-worker-regression-fixes.log` | 92 passed | 18 |
| Passing individual methods in `failing-boundary-retries.log` | 4 named methods passed; the seven-test run also had three failures | 4 |
| `maintenance-fix-retry.log` | 2 passed | 2 |
| `legacy-mock-boundaries-retry.log` | 2 passed | 2 |
| `oauth-fixture-final-retry.log` | 2 passed | 1 |
| Single-room module tool receipt `2ab6f2` | 4 passed; output was not redirected | 2 |

The two whole-module fixture reruns have exact selections, commands and
matching log hashes in
[`activity-regression-triage.json`](../validation/v7.1.0/activity-regression-triage.json).
Historical fixtures used invented map strengths outside the retained default
ranges, mismatched owner topology, or omitted effective settings required by
exhaustive parity. Their repairs preserve the production defaults and supplied
native values. The current room contract remains one room by default, with up
to four explicitly configured rooms. The archived bounded builder is checked
against its exact historical manifest without admitting that old policy as a
current runtime.

The sweep also exposed actual reporting-hook regressions: a missing room ID
raised `KeyError`, and assigning the broker's initial-private flag during a
password update could prevent the maintenance gate from releasing. The exact
missing-roster and two real worker maintenance cases passed after those fixes.
Other stale fake-supervisor, admission and import seams were corrected at their
specific test boundaries. The receipt links the exact passing methods.

The two skipped methods remain skipped, rather than claimed as passes:

- `test_sqlite_resilience.OwnerReaderTests.test_threaded_root_reader_refuses_credential_switch`
  requires root credential switching.
- `test_rating_migration.MigrationIdentityTests.test_identity_drops_supplementary_groups_and_restores_after_failure`
  requires root supplementary-group restoration.

Actual root donor installation and migration are separate evidence and do not
mean those two exact source tests executed.

## Feature coverage

The resolution JSON inventories observed verbose test IDs by module. Its groups
overlap; the counts are inventories, not independent acceptance totals.

| Area | Concrete coverage |
| --- | --- |
| Activity and personal statistics | Consent boundary, restart and lost-ack replay, completed matches and late scores, aborted-match exclusion, hidden-player suppression, source identity, private visibility timelines, cross-host overlap, PP history kept separate from canonical playtime, queued local `.stats` queries and fail-closed cache recovery. |
| Activity migration and SQL | 20 migration cases, including WAL-consistent backup, additive schema preparation, stopped ownership and account leases, interruption before/after commit, backup tamper, rollback/resume, native ruleset cache preparation and lost-cache startup fencing. SQLite resilience tests also cover bounded connection retries and avoiding replay of application SQL. |
| Ratings | Existing ledger, schema, policy, outbox, conflict, snapshot, seed, restore, scheduler and installed-agent suites; nine v7.1 parity cases. Whole-module fixture reruns restore actual Standard/Mania pool and display coverage. |
| Community commands | 17 command robustness tests and seven Discord invite tests passed again in the 47-test worker-boundary milestone, together with 20 migration and three worker-boundary tests. Real handlers cover registry/help/aliases, prefixes, role and mode gates, optional integrations, invalid finite-number input, bounded output and preservation of provider uncertainty. See [command audit](V7_1_COMMAND_AUDIT.md) for the matrix and limits. |
| Diagnostic logs | 26 cases cover complete operational INFO/lifecycle/warning/traceback streams, current and historical credential scrubbing locally and centrally, separate public-chat consent, emission-time private exclusion, allowlisted files, symlink/hardlink refusal, rotation/truncation/restarts, replay/deduplication, quotas/expiry with visible gaps, compressed bounds, host revocation and bounded private search. |
| Reporting and privacy | Real HTTPS consent/capture/upload, worker SQL guard paths, independent chat consent, source-side secret canaries and traceback preservation, credential-free support, opt-out purge, visibility at emission time, unknown-host refusal and complete backup/restore with sharing disabled after restore. |
| Capacity and outage | Activity agent's recorded 29-case collector/personal run covers durable pause without advancing the unsaved cursor, privacy priority and bounded historical baselines. Additional capacity integration, capture isolation and source/sink retry logs have passing counts but quiet output without complete selection witnesses; no more specific coverage is inferred from those logs alone. |

`worker-boundary.log` has a positive executing-agent selection witness for its
47 tests. Other quiet supplemental logs retain their exact counts and outcomes
in the JSON, with unknown selections explicitly marked. The failed 73-test
reporting run's outage test has a later one-test passing retry; failed import
milestones and the intermediate 34-test core run remain visible. The latter's
named fake-supervisor failure passed in the exact two-method mock retry.

## Frozen artifact and installed acceptance

The immutable final2 runtime is:

`8c0971747c77a22a992749a6171363d60eb4a3bdc5f87460c55f88d881a21d35`

Its [freeze receipt](../validation/v7.1.0-release-final2/freeze.json) records
215 source files, 27 unchanged wheels and the unchanged interpreter/environment
identity. Exact predecessor 7.0.6, 7.0.7 and 7.0.8 transitions admit only the
reviewed stopped additive migration; tampered storage and initial automatic
feed migration remain refused. This does not disable future compatible updates.

The [final2 donor run](../validation/v7.1.0-donor-final2/run-report.json) reports
success for fresh, operator and owner installations. The separate install and
upgrade receipts cover actual root direct installation and exact stopped 6/7
upgrades, retained authority/settings/history/enrollment, pending votes and
private activity backup/cache preparation. All roles used the full prepared
catalogs: **105,502 Standard, 19,334 Mania 4K and 4,347 Mania 7K rows**. These are
the prepared donor manifest's counts; no cause is inferred for differences from
older catalog reports.

The donor ran native workers and guards, completed two Standard/4K matches for
each of fresh/operator and three matches across the owner's Standard/4K/7K
rooms, uploaded completed participation, exercised native Discord/`.stats`
paths, ratings upload/aggregate consumption, and HTTPS statistics/PP-history/log
replay exactly once. Credential canaries were absent from hub logs. Recoverable
commands did not terminate sibling workers and every room shut down cleanly.
The provider and credentials were invented; no real osu! room was created.

The staged public archive audit found no unresolved high-confidence secret
findings, reviewed every scanner finding, verified catalog trust, and retained
unchanged public dependency hashes. The kit verification checked 55 checksums,
23 shell examples and archive CRC/inventory. Those shell examples were syntax
checked, not all executed. The staged archive remains distinct from publishing
or production activation; see [release readiness](V7_1_RELEASE_READINESS.md).

## Responsiveness and resource measurements

Two separate measurements bound different parts of the command path. Both name
immutable final2 runtime
`8c0971747c77a22a992749a6171363d60eb4a3bdc5f87460c55f88d881a21d35`.

The [donor benchmark](../validation/v7.1.0-donor-final2/benchmark-report.json)
used the full-catalog owner installation with native workers, guarded local
SQLite, unchanged intentional provider pacing and simulated provider responses.
Workers stayed alive with reporting disabled, enabled and during a full-queue
hub outage. Reporting-on/outage retained the ready statistics cache.

| Scenario | Reported whole-batch command roundtrip upper bound |
| --- | ---: |
| Reporting off | 75.553 s |
| Reporting on | 57.519 s |
| Hub outage / full queue | 73.484 s |

These whole-batch observations include intentional provider pacing and are
**not individual `.stats` handler latency**. They do not prove that reporting
makes commands faster. The off workload lasted 161.901 s; the on/outage workload
lasted 235.653 s and performed 467 agent steps, including 212 expected failed
attempts while the hub was unavailable. Those failures were outage retry
observations, not worker crashes. Workloads and sample windows differ, so their
CPU totals and elapsed times are not a controlled overhead comparison.

Peak summed UID RSS was **387,874,816 bytes (369.9 MiB)** with reporting off and
**413,351,936 bytes (394.2 MiB)** in the longer on/outage workload: an observed
24.3 MiB difference. This measures those workloads, not an isolated causal
memory cost per host. Sampled native workers remained alive in every scenario.

The separate [local handler measurement](../validation/v7.1.0/local-command-latency.json)
executed **120 real `.stats` handler calls per scenario** using the native
SQLite/cache path. Response transport was mocked; every scenario made **zero
HTTP calls**. The recorded hashes of `bot/bot.py`, `bot/ppstats.py` and
`poolrotater_host/activity/reader.py` were checked directly against their exact
bytes in the frozen final2 archive.

| Local scenario | Median | p95 | Maximum |
| --- | ---: | ---: | ---: |
| Local only | 0.2431 ms | 0.5049 ms | 0.5844 ms |
| Shared cache | 0.9048 ms | 1.2209 ms | 2.3142 ms |
| Hub unavailable, cached | 0.9131 ms | 1.2853 ms | 1.5618 ms |

These results establish bounded local cached-handler work in the measured
fixture. They exclude provider admission, response delivery, real chat polling,
network latency and gameplay, and are not a production latency percentile.

## Limits and remaining checks

- No second green 2,995-test run is claimed. Initial passing cases were not all
  repeated on final2 after localized changes; targeted fixes and frozen donor
  acceptance provide the subsequent evidence.
- The two root-only skipped methods are still an explicit source-test gap.
- No real OAuth account, live Discord availability, occupied production upgrade,
  external operator first activation or overnight internet soak was established
  by these offline/donor checks. Obtain post-activation operator support and
  gameplay results through the release runbook.
- Earlier native donor command bounds (19.855–37.794 s) and the later paced
  whole-batch benchmark bounds measure fixture delivery windows. The separate
  local-handler timings above do not establish production wire responsiveness.
- The command matrix is not every argument/state/role combination. Live wire
  acceptance and sustained use remain distinct from real-handler unit coverage.
- The separate reviewed-history helper has 12 targeted passing tests and actual
  protected-root/registered-environment/service-UID donor qualification. The exact
  helper queued three reviewed synthetic records, then zero on repeat, with no
  service start or provider dispatch. See `history-helper-review.json` and
  `history-helper-report.json`; this helper has its own checksum outside the
  immutable runtime.

This consolidation changed only documentation and evidence. It ran no new
runtime tests and touched no production service or credentials.

## Owner activation follow-up (October16)

Production activation preflight found deployment-only assumptions absent from the
standalone donor fixture: the active hub unit used the historical v706 prefix,
loaded timers omit MainPID, and the analytics input needed an explicit matching
snapshot environment path. The maintainer helper now validates the actual loaded
unit/prefix, handles timer/missing-unit state correctly and wires that private
input explicitly. Nine helper tests and three provisioning fixtures passed.
The active hub exchange then preserved stopped private state byte-for-byte and
passed protected readiness, HTTPS and privatepublisher checks. Operator choices
and running owner rooms were unchanged.

Actual full owner-history export exceeded the old192MiB/90second analytics
budget. Replaced full-history deep copies with copy-on-write sets and bounded
ranking selection, preserving complete owner input before top50. Thirty-eight
website tests and one additional1200-player/large-native-set memory and exact
ranking case passed. A private read-only production profile completed in8.21s
(1CPU cap, profiling overhead included). The live publisher subsequently passed
with6.33sCPU under a40%quota,384MiB RAM/128MiBswap cap and120s deadline; bot
resource settings were not altered. Existing owner view, combined filtering and
private snapshot reading were verified on the live statistics page.

Twelve one-time owner-migration admission tests passed for the new root-owned
maintenance job. It uses native broker/worker/privategate/ownership checks, then
the unchanged qualified stopped installer; it does not weaken the signedfeed
storage gate. Several timer ticks have confirmed waiting while users remain.
Actual eventual owner upgrade is established only by the protected migration
receipt and installed registration, not by these tests or queue creation.

The operator runtime ZIP and existing GitHub release assets remain immutable.
Only maintainer tools, website code and documentation changed in this follow-up.
