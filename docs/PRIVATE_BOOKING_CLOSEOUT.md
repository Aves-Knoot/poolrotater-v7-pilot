# Private booking technical acceptance reopened — 2026-10-22

The prior completion statement was premature. The focused 223-test receipt,
installed donor checks and prepared candidate are real evidence, but they do
not close unexplained broad-regression failures or substitute for a real browser.

Active objective: user attachment
0528b727-8dcb-40b4-a24a-d4ca57c39812/pasted-text-1.txt, incorporating all ten
sections of attachment 387a9d87-b6e7-483d-a5d2-063bf318a3d8/pasted-text-1.txt.
No production change, publication, feed update or actual room test is authorized.
The two-hour recreation change is already complete and excluded from this batch.

## Current acceptance work

| Requirement group | Existing evidence | Work still required |
|---|---|---|
| Original 1: state/contract/settings inventory | Development contract; pure tests | Check each original invariant against execution and tests |
| Original 2: atomic reservations/capacity/replays | Repository, eligibility, exchange tests | Concurrency and integrated reservation/reuse audit |
| Original 3: independent lifecycle/cleanup/update holds | Reducer, native-engine, guard tests | Integrated HTTP-to-native restart/outage/closure scenario |
| Original 4: session roles/parity/revocation | Repository/local/game tests | Pending grant cancellation, execution and late ACK audit |
| Original 5: settings/filters/Apply/revisions | Selection, preview, worker tests; full catalogs | Concurrent dirty drafts, failed apply, real-browser controls |
| Original 6: OAuth/private UI/security | Simulated HTTP and custom DOM | Real browser with simulated OAuth; keyboard/focus/forms/layout/clipboard |
| Original 7: public availability/privacy/statistics | Projection/panel/reporting tests | Browser-public privacy and unchanged public behavior review |
| Original 8: mixed versions/preservation/recovery | Donor 63-file preservation; compatibility slices | Relevant broad regressions and final exact candidate checks |
| Original 9: comprehensive acceptance | 223 focused passes; prior broad run 1132 passes,285 failures,25 errors | Per-case or proven-group resolution; fresh-process suites; no unexplained relevant failures |
| Original 10: release artifacts/docs/pilot | Reproducible old candidate and drafts | Rebuild after repairs, qualify final hash, update all receipts/handoffs |

## Execution and receipts

New regression receipts live outside source under ../reports/ and ../regression-*.log.
Tests run in the verified ordinary namespace; fixture providers are loopback-only.
Use fresh test processes to avoid cross-suite contamination and capture JUnit
node IDs so the original 310 failures/errors can be reconciled to exact evidence.
A bounded dedicated browser namespace may relax the virtual-address-space limit
only with an actual-memory bound; production filesystem/network remains absent.

Parallel ownership: authority repository/local; real browser/static UI; native
lifecycle/agent/system integration. Primary owns security review, regression
infrastructure, compatibility, artifact freezing and acceptance audit.

## Remaining human gates

Production website OAuth registration/configuration, publication, deployment,
actual osu! room/private-pilot authorization and public enablement remain separate.
None blocks this isolated technical closeout.

## Concurrent v7.2.14 compatibility

User reported the separate v7.2.14 catalog-rollout release. Its clean frozen
source commit bd99502 was read without modification. The catalog feature was
merged into this isolated booking checkout, preserving the future successor
behavior rather than reverting it. Final candidate freeze must qualify the exact
v7.2.14 runtime as an additional predecessor. Native catalog handoff now also
holds the shared booking admission mutex and refuses booked aliases, including
empty rooms awaiting arrival. No v7.2.14 checkout, feed, controller or deployment
was altered. Donor qualification must inspect current binding before any use.
