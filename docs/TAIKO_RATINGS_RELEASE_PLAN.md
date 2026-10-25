# PoolRotater v7.3.3 ratings and Taiko release plan

> R25 v7.3.3 passed normal installed automatic-upgrade acceptance on H (v7.3.0) and I (v7.3.2), 2 of 2. The unchanged validator checked all six evidence references for each path. A separate B pre-admission controller-interruption case failed before candidate code could be installed; it is a known legacy limitation, excluded from acceptance and disclosed for release. Proceed with the proven normal update path while retaining that exception. R25 is not yet published or deployed, and the full release goal remains incomplete.

## Goal and acceptance rule

Complete the next PoolRotater release by fixing ratings synchronization delays,
integrating the retained Taiko research into a working Taiko lobby, and
releasing it through the existing automatic update system. Use the latest
verified source and installed registrations; check concurrent work before
assigning the tentative v7.3.3 version. Reuse the existing engine, catalog,
fleet, and deployment mechanisms.

This remains a tested release requirement: preserve existing host configuration
and require no new installation commands, OAuth authorization, enrollment,
manual migration, rescue script, or operator credential setup. A download or
queued update is not acceptance. Preserve passwords, administrators, visibility,
room ceilings, catalogs, enrollment, OAuth, ratings, and history; do not weaken
inventory, identity, booking, or ownership checks to force an update through.

## Current state

- R25 is frozen at the runtime hash above with 324 payload files and 27 wheels;
  storage and environment are unchanged. The source preflight confirms that
  the source files match the candidate.
- H's exact v7.3.0 automatic-upgrade path is accepted using the six retained
  receipts in `donor-tools/reports/accepted-H-r25/references.json`. They verify
  automatic queueing, occupied-match and booking protection, a valid waiting
  configuration change, interrupted-download resume, automatic helper retry,
  native commit, and post-upgrade health. The preserved room3 countdown edit,
  intended running/stopped rooms, ratings, and catalog references were checked.
  The extra controller-fault hook was not exercised; it is not claimed as a
  completed scenario.
- The normal v7.3.2 path passed on fresh I under `taiko733-d7d658189a`, with the
  required updater-helper interruption enabled. Its native update completed
  automatically and post-upgrade health passed; six sealed references are in
  `donor-tools/reports/accepted-I-r25/references.json`. The unchanged validator
  accepts both normal predecessor paths (2 of 2). A separate B pre-admission
  controller interruption failed before the incoming candidate could run; that
  extra case is not accepted as recovery and remains a documented legacy limit.
  The release proceeds on the two validated normal paths with this exception
  disclosed. Hub deployment, publication, owner pilot and final documentation
  remain incomplete.
- Catalog service publication 17 is complete and must not be replayed. Matching
  R25 hub/catalog kits are prepared, but hub deployment, signed client offers,
  owner/community rollout, and the selected owner L3 pilot remain pending.
- Historical broad and feature results are reused only for unchanged code.
  The loopback ratings component proof records verified replies in 5.078s and
  5.074s, plus same-event recovery after HTTP 503. It is not installed TLS,
  actual Discord, or live gameplay proof. Retained acceptance receipts and
  requirement evidence reports separate evidence categories from remaining
  release gates.
- Supplemental Taiko-configured difficulty voting, AFK/countdown, in-place
  transport recovery, missed-start recovery, and Discord label/card checks now
  cover 55 effective passing cases with the existing related tests. The initial
  batch had 54 passes and one incorrect new test assertion about retained
  one-time AFK grace history; that assertion was corrected and its test passed.
  Unchanged passing tests were not repeated. Both XML outcomes are retained in
  `reports/r25-taiko-parity-supplement.json`. All 324 source payloads still match
  frozen R25; test files are outside its runtime payload.

## 1. Baseline and implementation control

Inspect current source, feeds, host versions, pending transactions, room intent,
and booking reservations before changes. Use isolated worktrees/checkouts and
avoid modifying another development thread. Keep version selection coordinated
with concurrent work. Use GPT-6 Luna workers for bounded tasks with explicit file ownership and
acceptance criteria; review/integrate each contribution before publication.
Keep shared contracts, signing credentials, integration review, and production
deployment under the coordinator's control with no overlapping file ownership.

## 2. Ratings synchronization and shared snapshots

Use the reproductions in `work/validation/controller-resilience732/reports/`.
Fix both scheduler bugs and snapshot contention while preserving strict
authorization, identity, epoch, corruption, and policy checks:

- Retry temporary `LeaseBusy` contention briefly and boundedly; it must not
  become a 30–33 minute “local state requires review” hold.
- Separate successful uploads from failed readback/reconciliation. A transient
  follow-up query failure must not block new votes from the fast upload path.
- Give shared snapshot lock contention its own short retry; accepted uploads
  should be able to wake due shared-count refreshes without waiting half an hour.
- Preserve durable events, retry idempotency, changed-opinion handling,
  cross-host deduplication, ignored-user policy, and independent provider/gameplay
  pacing. Never block commands on network transfers.
- Show familiar good/bad counts only from verified shared data containing the
  vote. Never add a local opinion blindly to an older snapshot. During outages,
  acknowledge the saved vote honestly and synchronize it automatically without
  requiring another command.
- Emit safe diagnostics for scheduler reason, contention, retry deadline,
  pending-event age, and snapshot progress; do not expose credentials or
  arbitrary exception text.
- Demonstrate prompt upload and a verified-count reply within the existing
  reply window under controlled healthy conditions; show bounded retry and
  automatic recovery under failure. Include contention, failed query, concurrent
  votes, duplicates, changed opinions, authority failures, and mixed-version
  cases. The supplemental loopback result is not installed TLS/systemd proof.

## 3. Complete distributable Taiko catalog

Verify the research repository, calculations, selection artifacts, and final
receipts before export. Ship a compact production catalog with qualified
Taiko difficulty/features and useful map metadata, never raw research databases,
score pages, profile records, or player-level calibration rows.

Retain native-versus-converted provenance. Admit conversions only with official
Taiko NM calculations; never substitute Standard stars. Explicitly exclude
unsupported/unresolved charts and do not present experimental style labels as
established preferences. Publish via existing authenticated, verified catalog
distribution. Keep discovery backward compatible so old clients still import
all modes they know; new/upgraded clients obtain required catalogs automatically.

Current export receipts describe 37,635 Ranked/Approved NM charts (37,447 native,
188 qualified converted) and aggregate calibration only. Publication 17's six
  content IDs are retained in its publication receipt. H's and I's R25 post-upgrade
  receipts verify Taiko and latest full Standard catalog references on both
  isolated installed hosts.

## 4. Integrate Taiko through the existing product

Add supported `taiko-fixed` and `taiko-auto` presets and ruleset-aware selection
on the retained engine. Qualify and test:

- Taiko-player-driven fixed/automatic difficulty, Taiko SR and length windows,
  exclusions, song spacing, and sparse-pool fallback.
- Playlist/queue progression, countdown, ready/AFK handling, difficulty and
  skip voting, and recovery after interruption.
- Shared good/bad ratings and personal statistics in a disjoint Taiko ruleset
  namespace, including old-client snapshot compatibility.
- Greetings, help, supported commands, and safe refusal of unsupported filters,
  styles, mods, or commands without mutating the pool.
- Fleet configuration, capability/version gates, room creation, booking-aware
  admission, graceful closure, managed restart, and catalog prewarm.
- Website and Discord titles/settings/player counts, qualified map artwork,
  and Taiko presentation. Website map timing remains approximate; Discord
  status cards omit map progress.

Qualify mods and clock rates explicitly. Current selection is verified NM at
rate 1.0; do not apply NM calculations to unqualified variants. Optional PP
calculator failure must not crash a room; Taiko uses qualified catalog NM
reference PP and must remain independent of the Standard PP projection. Add
Taiko without changing existing room ruleset, title, settings, or desired state.

Code-level evidence includes the Taiko catalog/engine/scope/activity/fleet/
directory/website/owner-feed tests and `test_taiko_wiring733.py`. Generic lobby
ready/AFK/vote/recovery tests are useful shared-engine evidence, but do not by
themselves prove a Taiko-configured end-to-end match. The new wiring test covers
Taiko song-spacing/sparse fallback, profile-to-auto-range, shared countdown/skip,
greeting/map output, and a failing optional calculator. A live pilot remains
required.

## 5. Prove exact automatic upgrades before promotion

The real installed automatic-upgrade paths have passed for both exact predecessor
versions, v7.3.0 and v7.3.2, with the exact R25 artifacts and full production-sized
catalogs using invented providers and test credentials. The failed R21 D730
registration remains held and is not acceptance. H and fresh I are the accepted
normal cases. A separate B pre-admission controller interruption failed before
the candidate was installed; the incoming recovery code cannot execute in that
older controller. Do not claim that extra case recovered. Proceed with the normal
supported path and disclose this specific limitation. The accepted paths include
occupied rooms playing
while empty siblings drain, active-booking protection, deliberately stopped
rooms, valid configuration edits while waiting, interrupted-download resumption,
and helper/controller interruption recovered only through supported
transactions without duplicate room creation or repeated `MakeRoom` attempts.

After each commit, verify registration, runtime, updater status, and actual
running version agree. Verify network reporting, ratings, and statistics
recover automatically; retain mixed-host-version compatibility throughout
rollout. H and I health receipts verify synthetic Discord routing; actual
Discord was not configured on either test host. Real Discord recovery and
presentation remain to be checked during rollout. Preserve all
credentials/settings/history and room intent.
Fix any reproduced path defect that would otherwise need operator rescue before
release. Failed trials are not acceptance; retain their authority and do not replay
transactions. Both normal H730 and fresh I732 paths are accepted at R25. A
separate B pre-admission controller interruption remains failed and is not
reused as normal-path evidence. Proceed with the proven normal path and document
that this older updater cannot run the incoming recovery code before installation.
No claim is made that the separate failed case recovered.

## 6. Coherent validation

Implement related changes in substantial milestones. Run targeted integration
batches and appropriate comprehensive validation before publication; reuse
unchanged passing results rather than repeatedly rerunning broad suites. Run
expensive work at nice 19 and parallelize within memory limits. Clean task-created
disposable outputs while retaining raw evidence and custody records.

Deterministically test ratings lock contention, failed post-upload queries,
snapshot recovery, concurrent votes, duplicates, changed opinions, authority
failures, and mixed-version operation. Validate Taiko selection and provenance
against the full exported catalog. Record which evidence is source/unit,
component HTTP, catalog service, installed upgrade, or live gameplay; do not
promote one category to another by inference.

## 7. Publish and deploy safely

After acceptance, publish matching source, checksummed install artifacts,
catalog metadata, and a signed automatic-update release. Deploy a compatible
hub before offering the signed client. Queue owner/community hosts through
supported mechanisms and apply at safe empty-room boundaries; respect occupied
rooms and bookings and preserve every intended room state. Use controller
restart only when technically necessary.

Use only genuinely unused, unreserved owner capacity for the selected Lobby 3
Taiko pilot. Never silently replace an existing public room or allocate a
booked/private slot. Verify real startup, catalog load, chat, map selection,
website/Discord presentation, and clean ownership. Observe actual gameplay when
players are available and label that acceptance separately from technical setup.

## 8. Evidence, documentation, and completion report

Update this plan, both handoffs, GitHub release information, and wiki
changelog/documentation after verified rollout. Report which hosts installed,
which remain safely queued, pilot-room link, validation results, and all
outstanding acceptance. Preserve evidence of exact versions, receipts, hashes,
room/booking state, and failure recovery. Do not promise zero failures or conceal
unresolved deployment issues.

The release is complete only after both exact automatic-upgrade paths pass,
compatible hub/source/artifact/feed rollout is verified, intended host state is
preserved, the pilot and presentation are verified, and required public
documentation and final audit are updated. Continue autonomously until then;
stop only for genuinely required human input or an unsafe unresolved condition.
