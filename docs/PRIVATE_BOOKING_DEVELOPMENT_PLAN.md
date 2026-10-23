# Self-service private booking — development contract

Status: technical acceptance REOPENED. Implementation and an unpublished v7.3.0 candidate exist, but remaining regression classification, real-browser qualification and integrated acceptance must finish before technical completion. See PRIVATE_BOOKING_CLOSEOUT.md. No booking feature is deployed or publicly enabled.
Source baseline e217ccf / v7.2.13. Authoritative checkout:
work/validation/private-booking/source. Objective is the complete user attachment
387a9d87-b6e7-483d-a5d2-063bf318a3d8/pasted-text-1.txt, all ten sections.
No live room/provider calls, production services/configuration/data writes,
credentials, remote code pushes or release publication are authorized.

## Architecture and implementation batches

1. Hub booking ledger + pure host lifecycle + website UI components.
2. Authenticated host wire, native managed-controller adapter, browser OAuth/API,
   session permissions in website/game, bounded settings/selection integration.
3. Full integration/privacy/compatibility/installed qualification and candidate
   packaging, operator/rollback/wiki drafts, controlled pilot procedure.
Only milestone tests; fix and rerun relevant failures. Heavy jobs nice19.
Workers gpt-6-luna implement bounded owned files; primary owns contracts,
security design, integration and review. Use existing native managed APIs.

## Durable state and permissions

Public slot states: available/preparing/reserved/active/closing/unavailable.
Freshness is independent: a delayed observation never releases a reservation.
Internal booking terminal state ended; unresolved failures retain the slot.
One active booking per requester, one per slot; transactions/unique indexes.
Every envelope binds protocol1, booking UUID, host/installation/generation,
slot alias room1..4, session generation32hex, request UUID and sequence/revision.
Only explicitly configured booking slots, never a public slot. Host quota and
provider allowance always apply; pending and uncertain rooms count. Defaults
are disabled and configuration APIs require explicit local administration.

Requester is Owner; Controller can edit safe settings/start/abort/skip; Player
has normal dot commands/votes. Only Owner grants/revokes using a live roster ID
or ends the booking. Delegated first arrival claims but does not grant control.
Roles last for this booking through rejoin; never global admins/moderators.
Revocation cancels undispatched actions and execution rechecks latest role.
Native operator/global moderation remains authoritative. No automatic failover
until confirmed closure. No mutable browser field can select admin role/path.

## Host lifecycle contract

Pure reducer advance(state, observation, now) -> (new_state, effects).
Observation: {ready:bool, room_id:int|null, incarnation:str|null,
 roster_known:bool, players:[{user_id:int,username:str}],
 match:{id:str,started_at:number,expected_end:number,ended_at:number|null,
        completed:bool,aborted:bool}|null,
 closed:bool, ownership_known:bool, updated_at:number}.
Only fresh authoritative observations (0..30s old) affect timers/roster.
Persistent state includes booking/session binding, phase, ready_at,
claim_deadline, claimed_at, empty_since, last_gameplay_at, match identity,
clock_high_water, warning markers, close_requested and terminal reason.
10minute claim clock begins on first confirmed ready+owned room. Owner or
explicit designated friend must appear in verified roster. Afterwards30minutes
continuously empty OR without meaningful gameplay. A completed map or >=60s
verified real participation renews gameplay; rapid start/abort does not. Active
verified matches suspend no-gameplay expiry only through bounded expected_end
plus120s grace; length bounds from qualified pool (max24h), no endless clock
from a stuck PLAYING flag. Unknown/stale observations never infer empty or
close. Clock rollback suppresses expiry until high-water catches up; forward
jump requires fresh native observation. Fixed claim warning60s, inactivity
warnings300s/60s, deduplicated; effects only fixed warning/close, never raw speech.
Close requested once durably; only independent closed+ownership-known+no room
ends booking. Cleanup/restore must complete before central slot release.
Website/browser/command/config activity never updates gameplay clocks.

## Interfaces owned by primary / workers

Common error is network.contracts.NetworkError(code,status). Booking validators
will live in bookings/contracts.py. Do not change generic network validation.
Hub repository: bookings/repository.py, Repository(root, clock=time.time,
 eligibility=None). Root dedicated private directory; private SQLite
bookings.sqlite3, no service/account actions. eligibility(host_id,alias) returns
{supported:bool,fresh:bool,host_id,installation_id,generation,
 effective_limit:int,capacity_used:int,observed_booking_ids:[str]}.
None callback is fail-closed. Concurrency allocation adds active reservations
not yet counted in observed_booking_ids to native capacity_used.
Methods (keyword args allowed):
 configure_slot(slot_id,host_id,alias,label,presets,base_settings,enabled=False)
 public_slots() -> list of minimal public documents (no room IDs/roster/secrets)
 reserve(user_id,request_id,slot_id,preset,settings,designated_friend_id=None)
   -> authorized full session document; generated password/booking UUID/session
   generation, payload-idempotent for same requester/request UUID.
 view(booking_id,user_id) -> owner/controller private document or403/404.
 submit(booking_id,user_id,request_id,action,payload,expected_revision)
   -> operation. Actions configure/start/abort/skip/end/grant/revoke.
 pending(host_id) -> pending command envelopes for current exact host binding.
 observe(host_id,booking_id,observation) -> monotonic validated host receipt;
   observation includes sequence, phase, room_id, incarnation, roster,
   effective_settings, cleanup_complete, ownership_known, operation_receipts.
   No ended release without cleanup_complete+known closure. Stale/replayed
   observations cannot move backwards; changed same sequence is409.
 Authorized roles checked before queue; grant/revoke Owner-only and roster
 verified; ten-second configure cooldown shared, one configure pending,
 expected_revision compare-and-set. Fixed operation UUID/idempotency applies
 also when repeat follows a lost response after a revision has advanced.

Host pure lifecycle owned independently in bookings/lifecycle.py, testable
without source imports/threads/network. Persist via local host ledger primary.
Frontend owned in bookings/static/{index.html,app.js,app.css}; no inlineJS.
Base URL /private-lobbies/. API same-origin GET api/me,api/slots,
api/session?id=UUID; POST api/reserve,api/action,api/invite (CSRF header).
me: {authenticated,user:{id,username}|null,csrf_token?,booking_id?}.
slots: {slots:[{slot_id,label,state,fresh,supported_presets,reason?}]}.
reserve body {request_id,slot_id,preset,settings,designated_friend_id:null|int}.
action body {request_id,booking_id,expected_revision,action,payload}.
session response {booking:{id,phase,revision,role,slot_id,preset,room_id,
 join_url,password,claim_deadline,players:[{user_id,username,role}],settings,
 operations:[{id,action,phase,error?,application?}],cooldown_until}}.
Signed-in unprivileged invitees see joining information only through a separate
revocable invite token, never the controller API. Owner/controller panel escape
all user content; no innerHTML interpolation. No remote assets/telemetry.
UI show draft vs applied, range+numeric filters, explicit Apply, roster role
buttons, actions, inactivity/preparation states. No unrestricted text fields.

## Security and deployment gates

Website identify-only OAuth, fixed approved HTTPS redirects, browser-bound
single-use state, secure HttpOnly SameSite cookies, CSRF on mutations, no CORS,
no secrets/raw errors in logs/public. Separate site sessions from host tokens.
Provider client stays local to deployed portal; invented test provider duringdev.
New host endpoint /v1/hosts/self/bookings uses existing enrollment auth and
separate capability/envelope; legacy managed wire unchanged. Legacy hosts
cannot book. Defaults off; no routes create sessions without explicit policy.
Booking updates must block ordinary updater admission while preparing,
awaiting arrival or active; override remains explicit operator recovery.
Session passwords/rosters/links only authorized private views. Public Discord
and website share minimal booking projection with website links only.
Private statistics continue using existing collector without new disclosure.

## Isolated acceptance evidence (live acceptance remains pending)

- [ ] All requirements independently audited against current implementation and acceptance evidence (reopened).
- [x] Concurrent reservation/quota/idempotency/no-show abuse.
- [x] Host restart/claim/inactivity/gameplay/clock/closure/cleanup/update holds.
- [x] Ownership/revocation/stale sessions/website and in-game parity.
- [x] Atomic settings/cooldown/revisions/full catalog/filter support.
- [x] OAuth/browser/CSRF/private/public status and mixed host versions.
- [ ] Complete integrated packaged acceptance, real-browser qualification and final donor qualification (partial receipts exist).
- [x] Reproducible candidate + hashes/docs/rollback/pilot, no publication.
- [x] Human-required OAuth configuration and live pilot explicitly pending.

## Integration checkpoint — 2026-10-22

Implemented the disabled-by-default hub ledger, explicit host eligibility,
identify-only browser session/API/static UI, host lease/role/command ledgers,
local lifecycle reducer, replayable host exchange and safe status projections.
Primary added native orchestration through existing managed configure/open/close
operations, runtime-only session overlays, worker-main-loop controls, in-game
session authorization, and update admission holds. These integration changes are
not yet comprehensively qualified; no live acceptance is implied.

Evidence in ../integration-ledgers-tests.log: first integration run **78 passed,
8 failed**. The 3 local/agent failures and 5 private-fixture/native failures were
fixed; targeted reruns **3 passed** and **5 passed**. Native selection/worker
checks **18 passed**, UI Node checks **3 passed**, syntax checks passed. Further
changes since these receipts require the next coherent milestone validation.
Earlier first foundation run was45/46; the missing revoke query fields were fixed
and the failing test passed. No expensive full regression suite has run yet.

Review found and is addressing immutable creation-lease replay, immutable
command envelopes independent from UI revisions, local-vs-hub command sequences,
late revocation receipts, claim-deadline propagation, ended-state settings
normalization, and closure-before-creation confusion. Hard filters currently
qualify effective duration and Standard AR. OD/global BPM and Mania/Catch AR
are explicitly unavailable with present metadata; the UI explains this.

Next: complete native/host/browser end-to-end seams, privacy projections and
operator enablement; run combined feature and relevant native compatibility
checks, then full-catalog and donor installed qualification. Finish candidate
packaging, migration/rollback, wiki drafts and pilot procedure only after that.
Website OAuth app configuration, live testing and publication remain later
human gates; none blocks isolated implementation now.

Narrow user exception: owner Lobby2 duplicate native close request was queued
while the user separately used `.closewhenempty`. Our duplicate operation
c4d95d07-36af-45bc-b8d5-2b19b68c7f30 is confirmed cancelled by operation
8f74ffe1-7030-4fd8-823b-9679e0c07576. Their native request was left untouched.
This exception does not authorize any booking development deployment.

## Second isolated milestone and narrow timer change — 2026-10-22

The combined feature/native-foundation/standalone-hub run in
../feature-milestone-tests.log completed **188 passed, 3 failed** (191 total).
All three failures were fixed with focused reruns: full catalog fixture hardlink
EXDEV (same-filesystem temporary directory; **1 passed**, ~65s, outputs removed),
immutable-lease test wrongly expecting settings before host application (**2
passed**, including new ended-owner receipt privacy check), and malformed
update-guard test fixture (**4 passed**, including new admission-lock cases).
Separate real loopback TLS exchange and simulated browser HTTP tests **5
passed**; game/agent/security slice **17 passed**; explicit policy/installed CLI
slice **9 passed**. These are milestone receipts, not final technical acceptance.

Full catalog preview now exercises all six presets (Standard Auto/Fixed/Loved,
Mania4K/7K and Catch), with ten actual native simulated selections per preset.
Portal preview uses read-only native pool constructors, cached verified exports,
no account/provider requests, bounded title-free samples. Added explicit
installed `managed-bookings --managed-stdin`, protected hub booking entry actions,
packaged static assets and stdlib native preview closure, optional trusted export
root for portal config, safe community Discord integration, and session-only
owner Discord status redaction. These latest packaging/entry additions still
need installed qualification. No candidate release has been built/published.

Next coding priorities before donor/candidate qualification:
- Complete root update admission's shared booking-admission mutex to match the
  agent's new-lease mutex/gate; test booking-vs-update races and stopped updates.
- Verify native Coordinator end-to-end with real managed State/Engine and fake
  provider, including refused preparation cleanup, unexpected closure, queued
  control restart ambiguity, password/settings restoration, and update holds.
- Complete public owner-slot available/unavailable display when no worker exists;
  community panels and website public booking API are already additive.
- Qualify portal startup/static assets/preview in the standalone installed kit,
  browser DOM behavior and invitation UX, bounded operation queues/settings mods.
- Comprehensive relevant regression milestone, donor install with full catalogs,
  reproducible artifacts, documentation/rollback/wiki/pilot drafts, no publication.

User explicitly requested an independent production configuration exception:
set recreation interval to two hours across every host/lobby. Used only the
existing verified hub SDK/native live configuration API; **all 12 configured
slots across owner, Xikeon and Strohm reported applied**, including dormant
slots. No manual room/service restart or code deployment. Owner room4's first
request was refused during a routine empty-room refresh; confirmed its new
owned room then applied a new revision/generation-bound config request. Evidence
is protected outside development at
/root/.codex-v7-operator-enrollment/recreation-2h-20261022/{queue.jsonl,retry-room4.jsonl,verification.json}.
The active development checkout's new-install gameplay defaults and native
fallback now use7200s too; they remain unpublished. This exception does not
relax the private-booking goal's production exclusions. Booked sessions retain
session-specific maintenance holds required by the booking contract.

## Third integration checkpoint — 2026-10-22

Implemented shared booking/update admission mutex including stopped controller
updates and stopped installer refusal while leases remain held. Added booking
modules to the shared runtime contract so future authority/lifecycle changes
cannot silently roll only one worker. Latest focused update/admission slice:
**10 passed** (`../stopped-upgrade-tests.log`). Real managed State/Engine native
and TLS exchange slice **13 passed**, browser/invite/revision slice **9 passed**,
settings/local/agent slice **45 passed**, agent telemetry slice **22 passed**,
owner public-safe panels **6 passed**, isolated package **3 passed**, real UI
module custom-DOM fixture **1 passed**. Earlier contract/recovery slice **34
passed** (`../contract-recovery-tests.log`). Receipts overlap; do not sum them.

Primary fixed recurring ended-report replay after slot reuse, preserved original
claim deadline during hub outages, surfaced host-applied settings and independent
settings revisions, bounded pending operation queues, and added private current
map/queue telemetry without entering lifecycle evidence or public projections.
Invites now have escaped friendly HTML joining instructions with fixed osu! URLs.
Operations/UI docs updated; pilot and wiki drafts prepared but not approved or
published. Installed root bootstrap behavior remains for donor qualification.

Broader source regression started; initial collection found four harness-package
imports resolving to outer tests rather than this checkout. Fixed runner search
path and restarted collection; no product failure implied by that collection error.
Donor preflight passed, but existing disposable instance's process census was not
fully quiescent; inspecting its own dedicated helper units before upgrade. No
production resources or credentials involved. Candidate7.3.0 proposed, not yet
frozen. Next: resolve genuine central End Session delivery seam, comprehensive
regression results, unpublished reproducible candidate, installed donor upgrade/
fake-provider lifecycle/full catalogs, acceptance matrix and local commits.

## Candidate and regression checkpoint — 2026-10-22

Unpublished proposed v7.3.0 runtime is frozen reproducibly at
`work/validation/private-booking/candidate/runtime.zip`, SHA256
`9bfda7f3d85e6ff17bc2bef197d7e4326e8e8745ba161f43574c5587cf678937`.
Exact stopped transitions from published12/13 and the preceding donor-only
candidate are encoded; storage/interpreter/dependencies are unchanged. Host
and standalone-hub code stay disabled by default. No signing/publication/queue.

Final feature slice **220 passed, 1 full-catalog case deselected** (~7s). Prior
full-catalog case passed; installed donor full-catalog preview is now running.
Read-only worker native binding fixed a real guarded-worker migration attempt;
fixed-path booking DB admission preserves schema protection and managed DB
readonly status (**16 guard/game checks passed**). Stopped private-only hosts
can now admit first leases with conclusive zero-capacity/closed-slot proof
(**20 eligibility checks passed**). Shared authority modules are fingerprinted.
Settings now durably reauthorize/ack immediately before swapping a qualified
pool; cancellation/timeout/revocation during build keeps old config (**34
related checks passed**). Accepted queue commands expire to a fixed unknown
outcome after120s, without replay (**9 native checks passed**). Preview caches
are2 entries with eviction before construction; portal template512MiB/Nice19/
CPU50%. Expired update approval is checked before helper quiescence and again
before service mutation.

Broad one-process source run was NOT green:1132passed/285failed/25errors,
mostly 1GiB virtual-memory/thread exhaustion and absent archived fixtures;
retained comprehensive-tests.log and triage, not acceptance. Smaller fresh
regressions: rolling59passed+2 fixture failures (full Mania path missing), then
both failures passed with validated full exports; reporting/statistics38passed;
controller/public compatibility26passed+1 stale fixture, then failing testpassed.
The central End Session TLS issue was only missing fresh managed report in its
fixture, not implementation; corrected genuine TLS end and reuse13checks pass.

Final candidate native stopped donor upgrade passed; all63checked config,
credential-state, ownership and full catalog files unchanged. Evidence
reports/donor-installed.json and donor-upgrade-final.log, donor stage
/opt/poolrotater-v7-test/runs/booking-6e8f2cf69d. Dedicated synthetic helper
network service has a Requires dependency on its external fixture, explaining
reactivation between separate admissions; quiescence now occurs inside the
same QA admission with its timers disabled. No production resource touched.
Installed booking probe is now running via qualify_booking_donor.py under
PrivateNetwork=yes/Nice19 on donor; inspect donor-booking.log and reports next.

Next: finish/fix donor installed private native/browser/guard/full-catalog
qualification, acceptance mapping and candidate kit/checksums, local commits,
final docs/handoffs and controlled pilot readiness. No genuine human blocker
until those independent tasks are complete. Actual OAuth website registration,
real room pilot and publication remain separately unauthorized.

## Isolated technical acceptance and preparation complete — 2026-10-22

Core local commit:a44bc33. The final runtime hash remains
9bfda7f3d85e6ff17bc2bef197d7e4326e8e8745ba161f43574c5587cf678937.
All payload code matches the checkout and two builds produced identical bytes.
The standalone hub kit contains264 inventoried files and the portal bootstrap
successfully refused untrusted staging and accepted protected donor staging.

Final installed donor receipt `../reports/donor-booking.json` is green:
9native State/Engine lifecycle cases,3simulated OAuth/browser HTTP cases,
1actual worker database-guard/game-command case, and all6preset previews with
full5mode exports. Preview candidates under the qualified180s hard filter:
Standard Auto11963, Fixed7154, Loved622, Mania4K3459,7K641,Catch1655.
No provider accounts or actual rooms were involved. Exact preserved-file
upgrade proof is `../reports/donor-installed.json` (63files unchanged).
Peak combined QA RSS737556KiB showed that512MiB was insufficient sizing;
the optional, still-uninstalled portal template now uses1GiB, CPU50%,Nice19.
Community hosts do not run this hub browser preview process.

Host kit and standalone hub kit are prepared locally in `../candidate`, with
checksums in HANDOFF.json and each kit's reviewed inventory. No GitHub push,
release, signed feed, production update queue, live wiki/site change or private
booking enablement occurred. Operations, staged rollout, rollback, acceptance,
UI limitations, wiki draft and real pilot procedure are recorded in this folder.
The earlier broad source-run failures remain honestly recorded and were not
represented as a green full suite; affected feature/compatibility failures were
fixed and rerun in bounded groups. See PRIVATE_BOOKING_ACCEPTANCE.md and the
candidate VALIDATION.json for exact scope and tests not executed.

Next human gate: review the candidate, authorize a one-slot real private pilot,
and arrange separate website identify-only OAuth credentials in the private
local config (never chat/source). Approve website/proxy changes and any host
opt-in/agent restart as part of that pilot. Real browser/OAuth/gameplay/expiry/
reuse checks remain unexecuted. Publication/signing/feed promotion and wider
public booking rollout need separate explicit approval after pilot evidence.
No independent coding blocker remains for the authorized isolated batch.

## Final candidate checkpoint — 2026-10-22

Supersedes the preceding9bf runtime only for a website config-validation fix:
OAuth secrets now validate independently of whether previews are configured;
disabled preview-only config needs no OAuth pair. Local fix commit:e0cb9ff.
Final runtime SHA256:
67d545890aad4de8b3ae2829cf8bfc8f791b0b15903ff6223672fea63902db59.
Final feature suite:223passed,1full-catalog case deselected. Final installed
native upgrade again preserved63files. Exact installed donor booking receipt
is green:9native,3browser,1guard plus6full-catalog preset previews. PeakRSS
737060KiB. Donor stages:upgrade /opt/poolrotater-v7-test/runs/booking-d9585900a9;
booking /opt/poolrotater-v7-booking-129e92117c. The native/provider/bookkeeping
behavior otherwise retains the preceding qualification and unchanged full
catalog contents. Runtime payload matches source, builds reproducibly, and
remains unqueued/unpublished. Final kit hashes are in ../candidate/HANDOFF.json;
archive/inventory/reproducibility validation in ARTIFACT_VALIDATION.json.
All authorized isolated implementation/preparation is complete. Proceed only
through the documented separately approved OAuth/site/one-slot live pilot and
publication gates. Do not resume autonomous production deployment from this
checkpoint. Independent two-hour timer request remains applied and verified
for all12configured owner/community slots without occupied-room restarts.
