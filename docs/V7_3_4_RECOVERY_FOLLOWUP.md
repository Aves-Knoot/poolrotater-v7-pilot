# v7.3.4: catalog, maintenance and independent recovery

Published on GitHub and the signed pilot feed on October 26 UTC. Runtime:
`1392bb5226526228c6f5d02803f1cfaac5dc438ee7be403c0dd570469d1006ce`;
matching source: `8fe7dd8837f700d11ea5cd75ff56e9f5fcf3ca3f`.
The sole direct predecessor is the exact published v7.3.3 runtime
`45254e8a24bee3b4c86259a31528e3e06a11b54aa201afca54e63981f0e45a41`.
The v7.3.2 cold peer and historical v7.2.6 reader are not direct predecessors.

## Changes

- Managed catalog staging retries known temporary transport failures at most
  three times, preserving verified partial data/cache. Certificate, hash,
  schema, identity, custody and policy failures remain refused.
- After a settled verified upgrade, managed reconciliation refreshes only the
  independent helper's registered-runtime pin. Exact identity, committed
  lineage, matching inventories, unchanged launcher and root-owned policy are
  required. Installer and recovery-policy locks serialize the operation.
  Existing disabled/recovery-permission choices remain preserved; the refresh
  does not start or restart a room or grant an update.
- Known local pre-dispatch maintenance deferrals retain the gate and retry.
  The same error after dispatch remains uncertain and fenced. Unknown outcomes
  are never treated as permission to repeat a mutation.
- Preparation/restoration diagnostics use finite reasons and bounded public
  storage/catalog metadata. Primary failures are retained; arbitrary exception
  contents and credentials are excluded.

## Validation

298 affected tests and 223 maintenance integration tests passed, followed by
45 overlapping focused checks. These counts are not additive. Four finalizer
contract checks, five promotion metadata tests, and real local artifact
promotion also passed. The frozen runtime remained unchanged throughout
packaging-tool corrections.

The exact installed v7.3.3-to-v7.3.4 donor run used all six full catalogs and
invented providers/test credentials. Genuine native queue/commit receipts prove
booking/occupied-match protection, independently drained siblings, deliberately
stopped slots, preserved waiting configuration edits, resumed runtime download,
interrupted-helper recovery, and unchanged credentials/history/settings.
Registration, runtime and updater agreed after commit. The second read-only
post-health observation verified reporting, ratings, statistics and synthetic
Discord routing after they caught up naturally. Scheduled managed reconciliation
refreshed the recovery pin for that same completed operation without changing
controller PID, room IDs or MakeRoom count.

Acceptance receipt SHA-256:
`b776c53c9b2cd0508ba3c243a213529538834844675abdf13a1acb8c9d2d43a2`.
The separate signed HTTPS full-Standard fixture also proved interrupted catalog
range resume; this is component evidence, not installed catalog-fault evidence.

Initial observations are retained, including the first not-yet-current health
sample and a pin observer that refused before mutation because the recovery
helper was not quiescent. A later bounded observer paused/restored only the test
recovery timer and produced the positive pin receipt. The subsequent explicit
donor teardown encountered an uncertain fake-room close. Its journal was
retained unchanged; the synthetic namespace was retired with no service-UID
processes remaining. That teardown is not claimed as clean shutdown acceptance.

Earlier R1 and R25 failure trials remain historical unaccepted evidence. In
particular, the old pre-admission controller fault is not silently relabeled as
recovered. No zero-failure guarantee is made.

## Publication and adoption

Matching source/tag and eight checksummed assets are published at
https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.3.4 .
Signed metadata sequence 46 / release sequence 35 offers this controller update
from v7.3.3, preserving prior descriptors and the existing trust root. Compatible
hub wire components are unchanged, so no hub restart was needed.

Existing hosts use their saved automatic-update policy at safe empty-room
boundaries. Occupied rooms and bookings remain protected, and intentionally
stopped slots stay off. Publication and a completed download are not evidence
that a host installed the release. Consult current host reports and live room
versions for rollout status; no new operator command is required for the
qualified path.

## Website-only follow-up

The deployed website prefers bounded total length and falls back to labelled
Play length when only hit length is known. Six PHP/QuickJS/mode fixtures and a
fresh public website/Discord observation passed. This documentation commit
mirrors that deployed presentation change separately from frozen runtime8fe7.
Owner L3 is Taiko automatic, L2 is reserved, L1 Catch and L4 Mania4. Three real
scored Taiko matches and one earlier abort are recorded; each completed match
had one participant. Multiplayer and every-command acceptance remain unproven.
