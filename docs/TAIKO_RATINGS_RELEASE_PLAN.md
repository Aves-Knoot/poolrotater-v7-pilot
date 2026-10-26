# PoolRotater ratings and Taiko release plan

This plan preserves the eight original work areas and records current evidence
without treating old queued/held snapshots as current state. It is internal
planning material. R25 v7.3.3 and the accepted v7.3.4 follow-up are published.
Production v7.3.4 adoption is observed separately from publication.

## Current status

- Owner, Xikeon and Strohm are on the published v7.3.3 runtime
  `45254e8a24bee3b4c86259a31528e3e06a11b54aa201afca54e63981f0e45a41`.
  R25 normal installed automatic-upgrade paths passed on H (v7.3.0) and I
  (v7.3.2), 2/2. The separate B pre-admission controller-interruption case
  failed closed before candidate code ran; it remains a disclosed legacy
  failure, not accepted recovery. See
  `reports/r25-final-acceptance-manifest.json` and
  `reports/r25-final-installed-acceptance.json`.
- Current owner rooms are Catch L1, Taiko L3, and Mania 4K L4; L2 remains
  reserved/stopped. The latest presentation capture identifies Taiko room 4821188 and map
  84910. Empty-room refresh can replace room IDs; use the live site for the
  current join link. Gameplay evidence records three
  completed scored matches and one abort across successive L3 room IDs; each
  completed match had one participant. This proves observed gameplay, not
  multiplayer or broader player acceptance. See
  `reports/r25-owner-taiko-actual-gameplay.json` and
  `reports/r25-post-gameplay-live-taiko-presentation.json`.
- Wiki/public documentation revision `0e860c2` and the five-page verification
  are retained in `reports/r25-taiko-gameplay-wiki-verification.json`.
  Website and Discord presentation were checked against the current L3. The
  capture exposed a website-length display bug when `hit_length` is present
  without `total_length`. A reviewed PHP/JS correction is deployed: valid total
  length is preferred, otherwise the site labels the value as Play length.
  Six offline PHP/QuickJS/mode fixtures passed. A fresh HTTP/Discord capture
  verifies map, range/settings, zero-player idle state, artwork, queue and length;
  idle playback is correctly null and does not establish a current playing timer.
- v7.3.4 is published with exact installed acceptance from v7.3.3. Runtime
  `1392bb5226526228c6f5d02803f1cfaac5dc438ee7be403c0dd570469d1006ce`, source
  `8fe7dd8837f700d11ea5cd75ff56e9f5fcf3ca3f`. Acceptance receipt
  `R734_INSTALLED_ACCEPTANCE.json` SHA
  `b776c53c9b2cd0508ba3c243a213529538834844675abdf13a1acb8c9d2d43a2`
  binds genuine native queue/commit, positive post-health and scheduled pin
  refresh. Signed metadata46/release35 is active. Do not replay terminal run
  phases. See `V7_3_4_RECOVERY_FOLLOWUP.md` for limits and retained failures.
- Strohm's R25 held transaction ultimately completed through the installed
  v7.3.3 native entry, resuming the same verified partial Standard download
  from 19,890,176 to 21,077,643 bytes. The recovery helper's v7.2.6 pin
  mismatch and R2 bounded retry/pin-refresh follow-up remain separately
  documented. The follow-up is published separately as v7.3.4; it does not
  change the v7.3.3 artifact.

Earlier pre-pilot plans, queue reports, live holds, and interim host snapshots
are historical evidence only. In particular, the R25 pre-pilot audit and older
sections of the source plan that say clients are queued, the owner pilot is
pending, or no Taiko gameplay occurred have been superseded by the receipts
above. The failed B trial remains a real exception and must not be relabeled as
success.

## Goal and acceptance rule

Fix ratings synchronization delays, integrate the verified Taiko research into
a working Taiko lobby, and deliver updates through the existing automatic
system. Preserve installed configurations and require no new install command,
OAuth authorization, enrollment, manual migration, rescue script, or new
operator credential setup. A download or queued update alone is not acceptance.
Preserve passwords, administrators, visibility, room limits, catalog state,
enrollment, OAuth, ratings, history, and intended room state. Do not weaken
inventory, ownership, authority, or booking checks to force progress.

## 1. Baseline and implementation control

Use verified source, release-feed and installed-host registrations as the
baseline. Check concurrent development, pending transactions, room intent, and
booking reservations before changing a host. Work in isolated checkouts without
modifying another development thread's workspace. Use bounded Luna-agent tasks
with explicit file ownership and acceptance criteria. Keep shared contracts,
signing, integration review, and deployment under coordinator control. Review
and integrate each contribution before publication.

Current baseline evidence: `reports/r25-current-source-recheck.json`,
`reports/r25-final-installed-acceptance.json`, the R25 owner/fleet read-only
receipts, and the exact R2 freeze and run artifacts. The final installed-path
and publication qualification for R2 passed; production adoption remains a
separate current-state observation.

## 2. Ratings synchronization and shared snapshots

Preserve strict authorization, identity, epoch, corruption, and sharing policy
while addressing both scheduler bugs and shared-snapshot contention:

- Retry temporary `LeaseBusy` contention briefly and boundedly; it must not
  become a 30–33 minute review hold.
- Separate successful uploads from failed readback/reconciliation so a
  temporary follow-up query failure cannot block new votes on the fast path.
- Retry temporary snapshot-lock contention promptly, especially after an
  accepted upload.
- Preserve durable events, retry idempotency, changed-opinion behavior,
  cross-host deduplication, ignored-user policy, and independent gameplay and
  provider pacing. Network transfers must not block command handling.
- Show good/bad totals only from a verified shared snapshot containing the
  vote. Never add a local opinion to an older snapshot. Acknowledge saved votes
  truthfully during outages and sync later without another command.
- Emit safe scheduler reason, contention, retry deadline, pending-event age,
  and snapshot-progress diagnostics without credentials or arbitrary exception
  text.

Source/scheduler and historical regression evidence covers lock contention,
failed readback, concurrent votes, duplicates, changed opinions, authority
failures, mixed-version snapshots, and outage recovery. The real-time loopback
HTTP component probe used canonical background scheduling and recorded
verified `.good`/`.bad` replies in 5.078s and 5.074s against a 12-second
deadline, plus same-event HTTP 503 recovery without a second command:
`reports/ratings-http-r21-supplemental-run12/ratings-http-report.json`.
This is controlled component evidence, not installed TLS/systemd or a
production multiplayer command test. R25 owner/Xikeon ratings observations
verify accepted synchronization and watermarks, not player-command latency.

## 3. Complete distributable Taiko catalog

Verify research receipts and calculations before export. Distribute compact
production metadata with qualified Taiko difficulty/features, not raw research
databases, score pages, player records, or calibration rows.

Preserve native-versus-converted provenance. Admit a conversion only with its
official Taiko NM calculation and exact source identity; never substitute
Standard stars. Exclude unsupported/unresolved charts and experimental style
claims. Use authenticated verified distribution and preserve older-client
discovery for the modes they know.

The validated export contains 37,635 Ranked/Approved NM charts (37,447 native,
188 qualified conversions), 16 aggregate calibration points, and no private
research tables. The shipped Taiko catalog has `taiko_maps` and the aggregate
calibration table/indexes only. Evidence: `catalog/taiko-export-validation.json`,
`catalog/taiko-export-run-receipt.json`,
`reports/catalog-service-publication-r4.json`, and old-reader compatibility in
`source/tests/test_taiko_upgrade733.py`. Catalog publication 17 is complete;
do not replay it.

## 4. Integrate Taiko through the existing product

Keep Taiko in the existing engine with supported `taiko-fixed` and `taiko-auto`
presets. Qualify and preserve these behaviors:

- Taiko-player-driven fixed and automatic difficulty, Taiko SR and length
  filters, exclusions, song spacing, and sparse-pool fallback.
- Queue progression, countdown, ready/AFK, difficulty and skip votes, and
  interruption recovery.
- Ruleset-separated good/bad ratings and personal statistics, including old
  client snapshot compatibility.
- Greetings, help, supported commands, and safe refusal of unsupported filters,
  mods, styles, and commands without pool mutation.
- Fleet configuration, capability gates, booking-aware admission, room
  creation/closure, managed restart, and catalog prewarm.
- Website and Discord title/settings/player presentation, qualified artwork,
  and timing. Website timing remains approximate; Discord must not expose map
  progress.

The published qualifier is Taiko NM at rate 1.0 with no mods. Do not apply those
stars to unsupported modified variants. Optional PP-calculator failures must
degrade gracefully, using only qualified catalog NM reference information.
Do not change an existing room's ruleset, title, settings, or desired state to
add Taiko.

Source evidence includes `test_taiko_catalog733.py`, `test_taiko_engine733.py`,
`test_taiko_scope733.py`, `test_taiko_rating_snapshot733.py`,
`test_taiko_activity733.py`, `test_taiko_directory733.py`,
`test_taiko_website733.py`, `test_taiko_owner_feed733.py`,
`test_taiko_service_assets733.py`, and
`test_taiko_wiring733.py`. The wiring test uses the retained live-lobby fixture
for Taiko spacing/fallback, profile-to-auto-range, ready/countdown/AFK/skip,
greeting/map output, and optional PP failure. Generic shared-engine tests are
not a substitute for the owner gameplay receipt. Three scored matches were
observed; all had one participant, so multiplayer player acceptance remains
unproven.

The website length fallback is deployed and verified: bounded total length is
preferred, otherwise hit length is labelled Play length. The current Taiko
website/Discord receipt binds map, artwork, settings, queue and player count;
idle playback is correctly null. This does not establish a current playing
clock or multiplayer acceptance.

## 5. Prove exact automatic upgrades before promotion

Use exact frozen artifacts and production-sized catalogs with invented
providers/test credentials. Verify the native update path, not just download or
queue. Required scenarios include occupied rooms playing while empty siblings
drain; active-booking protection; deliberately stopped rooms; valid config
edits during a wait; interrupted-download resume; and helper/controller
interruption recovery without duplicate rooms or repeated creation attempts.
After commit, registration, runtime, updater status, and actual version must
agree. Verify network reporting, ratings, statistics, and Discord routing
recover automatically. Preserve mixed-version compatibility and every
credential, setting, history, and room intent.

R25 accepted normal installed paths H v7.3.0 and I v7.3.2 (2/2). Separate B
pre-admission controller interruption failed before candidate code could run;
it is retained and explicitly not accepted. The exact v7.3.4 R2 run passed native completion, post-health and scheduled
recovery-pin refresh. Registration/runtime/updater agreed; preserved settings,
intent, history, OAuth, enrollment and all six catalogs were verified. The first
health sample needed convergence, and the initial pin observer refused before
mutation until its own helper precondition was satisfied. Both remain retained.
A later explicit donor teardown encountered an uncertain fake-room close;
synthetic services were retired with the ownership journal unchanged. It is not
clean-shutdown acceptance. No acceptance is inferred from that teardown.

## 6. Coherent validation

Implement related changes in substantial milestones. Reuse unchanged passing
suites only where payload identity proves reuse; avoid repeated unchanged
broad runs. Run expensive work at nice 19 and within memory limits. Clean
task-created temporary output while retaining essential evidence.

Retain deterministic coverage for ratings lock contention, failed post-upload
queries, snapshot recovery, concurrent votes, duplicates, changed opinions,
authority failures, and mixed-version operation. Validate Taiko selection and
provenance against the full exported catalog. Label evidence as source/unit,
component HTTP, catalog service, installed upgrade, or live gameplay; never
promote one category by inference. R2 exact installed acceptance passed and is hash-bound; earlier failed trials
and the separate teardown uncertainty remain explicitly retained.

## 7. Publish and deploy safely

R25 source, checksummed assets, signed feed, compatible hub, catalog service,
website, and public wiki documentation are already deployed. All three
production hosts now run v7.3.3. Preserve the explicit B failure disclosure.
v7.3.4 source, checksummed assets and signed feed are now published after exact
installed acceptance. Catalog metadata remains current and unchanged by this
patch. Observe adoption through supported mechanisms at safe empty-room boundaries. Respect occupied rooms and bookings;
preserve all intended room states. Use a controller restart only where
technically necessary.

The owner selected L3 for the Taiko pilot; L2 remains reserved. Verify
capacity/booking/ownership before any future change. The current owner L3 room
is shown by the latest live observation; do not silently replace a public room or consume a booked/private
allocation. Recheck real startup, catalog load, chat, selection, ownership,
website, and Discord after any relevant deployment.

## 8. Evidence and completion report

Update this plan, both handoffs, GitHub release information, wiki/changelog,
and final audit from authoritative current receipts. Report installed versus
safely queued hosts, pilot link and status, test evidence, legacy failure, and
remaining acceptance. Keep public documentation honest about gameplay and
presentation evidence.

Current wiki verification: `reports/r25-taiko-gameplay-wiki-verification.json`
records five pages with HTTP 200 and gameplay text. Current actual gameplay is
recorded in `reports/r25-owner-taiko-actual-gameplay.json` (three completed
scored matches, one abort, one participant per completed match). The newest
website/Discord observation is
`reports/r25-post-gameplay-live-taiko-presentation.json`; the website length correction is verified by
`reports/r25-length-fallback-website-deployment.json`. v7.3.4 is accepted and published. Follow-up production adoption and the final
requirement-by-requirement audit remain separate completion checks.
