# v7.1.10 — update recovery and shared-history reliability

Qualified controller release. Publication, deployment and historical-data
acceptance are separate observations. See the final release receipt and
published checksums for the immutable artifact hashes.

## Controller update reliability

Controller updates retain their natural all-configured-rooms-empty admission,
private gate, late-join cancellation and native ownership checks. The controller
now records its stage before lifecycle operations and retains a safe failure
code instead of discarding every exception behind a generic hold.

Two identified local cleanup conditions have bounded waits: a dedicated service
process still exiting, or the existing owner lock still held. Every observation
reruns the real process and stopped-ownership guards. Unsafe lock permissions,
unreadable process evidence and unknown room ownership are not transient waits.
The updater never kills a process to make these checks pass.

Newly admitted transactions can resume through the existing protected recovery
workflow, with fresh registration, artifact, configuration and ownership checks.
Automatic continuation has a durable three-attempt budget and a 30-minute
eligibility window from commit admission. A healthy started successor is
observed without starting it again. A recorded startup attempt with no currently
verified running successor remains held for review; no uncertain room creation
is automatically replayed. Legacy held jobs acquire no automatic authority. A still-running predecessor
keeps its rooms while recovery restores saved reporting-service intent and
returns the update to fresh empty-room admission. An owned manual timer is
rearmed; sharing opt-outs are retained.

The original October 17 update failure discarded its underlying exception.
Cleanup timing remains a hypothesis for that incident, not a proven cause.

## Activity and historical statistics

Internal ledger identities that cannot identify an osu! player are excluded
from wire events without deleting or rewriting native records. One such privacy
row previously blocked the entire owner's capture batch, including historical
backfill. Filtering applies to privacy, presence, joins/leaves, matches,
achievements and historical baselines.

Real-player privacy preferences remain authoritative. Malformed preferences for
a valid player hold ordinary uploads for review, including an older pending
public preference. Correcting the preference requeues it safely. Hidden choices,
sharing opt-outs, private-room exclusions, durable cursors, receipts and
idempotent historical source snapshots remain in place.

Support exposes bounded exclusion counts without the excluded identities, privacy
review needs and stale reporting observations. A locally queued history scan is
still not proof of hub acceptance or availability in another host's `.stats`.
Historical import continues to require an explicit reviewed source manifest.

## Operations and preservation

Local status distinguishes waiting for an empty window, active update recovery
and a held update that left services unexpectedly offline. The operations
collector integration distinguishes stale or blocked reporting from current
reporting, and respects the actual configured room topology.

Existing gameplay, commands, map-selection tuning, catalogs, storage contracts,
OAuth, Discord, enrollment, website integration and operator policies are
retained. Dependencies and native gameplay/storage code are unchanged. This is
a controller release; it requires each host's configured rooms to empty together.
A signed offer, local queue, activation and confirmed history delivery are
separate observations.

## Qualification

The frozen artifact passed 289 affected tests, five operations-report checks,
six isolated root/service-UID cleanup cases and an exact protected upgrade from
v7.1.9 preserving settings, credentials, opt-outs, catalogs, ledger and ownership.
Full-catalog native lifecycle scenarios completed for three-room owner and
two-room community configurations: occupied/late-join preservation, actual
shutdown and delayed child cleanup, registration upgrade, two deliberate process
interruptions, restart/readiness and repeated recovery without duplicate starts.

Both lifecycle runs reached the final summary write, which exposed a fixture
lookup error. That reporting defect was corrected. Original failures were kept;
independent read-only verification of the native jobs, interruption receipts,
configuration, clean ownership and reaped processes reconciled the completed
results without replaying room operations. Qualification records this explicitly.

Complete prepared catalogs contain 105,502 Standard, 19,334 Mania 4K and 4,347
Mania 7K maps plus their companions. Unchanged long gameplay/style fixtures and
whole-repository discovery were not repeated. No new actual upstream room or
gameplay test was performed during development; donor providers were invented
and network-isolated. Live historical-data delivery still requires post-activation
capture, upload acknowledgments and consumer/website observations.
