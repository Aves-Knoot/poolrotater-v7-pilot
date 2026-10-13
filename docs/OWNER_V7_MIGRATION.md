# Owner v7 migration and rollback preparation

Status: preparation only, 2026-10-13. Production remains unchanged. This document
is an admission checklist, not authorization to stop/start services, read OAuth
files, create osu! rooms or migrate live state.

The owner explicitly requires preserving the **current configuration at all
costs** and adding distributed hub capability. A successful tester one-room
pilot or generic v7 preset does not prove owner migration parity. An enabled
feature, setting or retained state that differs without an explicit owner
decision blocks owner cutover. Running the hub beside v6 remains a useful
intermediate step if any owner migration requirement is unresolved.

## Read-only evidence

`validation/owner-pilot/inventory_owner.py` writes
`validation/owner-pilot/owner-inventory.json`. It imports no live bot module,
reads no environment/credential/token file and makes no provider call. It uses
only explicitly projected configuration values, release manifests, a redacted
ownership-journal projection, allowlisted systemd metadata and file statistics.
Live SQLite files are not opened: even SQLite `mode=ro` can update WAL/SHM.
Ratings schema evidence comes from literal source DDL executed in RAM, not from
the live ledger. Sealed root-owned catalog files whose content matches release
manifests and has no sidecars are inspected with immutable read-only SQLite.

The supervisor symlink resolves to v6.7.33. The local rolling journal reports all
four workers running/owned at v6.7.34. This difference is expected under rolling
deployment; the symlink alone does not identify current gameplay code. The
journal observation is not a fresh upstream ownership qualification and does
not by itself authorize a second supervisor or cutover.

| Alias | Current room identity | Current band | Principal tuning |
| --- | --- | --- | --- |
| room1 | Standard automatic difficulty | 4.0–5.0★ | Half-width 0.35, age 8 years, minimum passcount 10,000, pool 1,200 |
| room2 | Standard fixed difficulty | 3.5–4.5★ | Half-width 0.30, age 8 years, minimum passcount 10,000, pool 1,200 |
| room3 | Native Mania **7K** | 3.0–4.0★ | Age unlimited, minimum passcount 1,000, pool 400, farm threshold 44.0 |
| room4 | Native Mania **4K** | 3.0–4.0★ | Age unlimited, minimum passcount 1,000, pool 400, farm threshold 60.6 |

Preserve the alias order, including room3=7K and room4=4K; the old generic presets
use the opposite Mania order. Shared configured pacing is 15-second countdown,
10-second auto-start, queue-ahead 1, minimum players 1, empty-room reset 45 seconds,
30-minute hard recreation, 240-second map limits and API rate/burst 50/60.
Standard difficulty votes use step 0.5/max 15; Mania's nested local overrides use
max 10. The existing map/song spacing algorithm estimates an hour from completed
rounds and relaxes when pools are small; it is behavior in the existing selection
engine, not an invented configurable `song_cooldown_seconds` setting.

Discord control/modlog, incoming chat websocket, PP display and automatic PP
announcement are currently enabled. AI is currently disabled. Admin/moderator
lists, custom greetings, temporary bans, language preferences and map exclusions
require private owner-local preservation; their values are omitted from evidence.
Headless supervisor overrides already force legacy lazer coordination/patrol and
rejoin flags off even when raw universal.json still says true. The report applies
that existing override instead of incorrectly requiring a Windows coordinator.

## Deterministic configuration parity gate

The inventory classifies **every key** from current worker defaults, room JSON,
universal JSON and nested Mania overrides. A final stopped, private audit must:

1. Capture the exact current defaults + room + universal configuration and
   resolve it with the working `bot.mode.merge_room_config`, then apply actual
   supervisor overrides. Do not use a partial inventory as a complete import.
2. Classify each source key as preserved gameplay, explicit host identity,
   separately configured integration, managed path mapping, private owner policy
   or reviewed non-effective legacy setting. Unclassified keys fail admission.
3. Compare source and target **effective values and types**, privately. Preserve
   floats/booleans/ranges/mods, titles, admin permissions, greetings and aliases.
   Remap only host-local storage destinations through a reviewed explicit map;
   never normalize or silently drop a field because a preset supplies a default.
4. Separate osu! interactive authorization from configuration preservation.
   Existing credentials are not silently read/reused. Report only gate booleans,
   source/target code identities and mismatch categories; no credentials, voter
   IDs, greetings or token contents enter public/support artifacts.
5. Qualify active behavior on installed donor services. A projection passing
   schema checks proves safe preparation, not Discord/websocket/PP/gameplay parity.

`poolrotater_host.owner_settings.project_legacy` is a pure implementation of the
existing room/universal/Mania precedence. It returns host room identity/ranges,
a strictly validated gameplay overlay and named integration requirements. It
does not read production paths, configure account credentials, write files or
declare owner admission. Its optional `config/gameplay.json` record binds the
installation/generation and exact aliases/presets. Runtime materialization
revalidates it, preserves native preference edits when source tuning is unchanged
and keeps catalog/auth/transport/storage fields pinned. It permits PP enablement
without copying a cache path or arbitrary download URL. Installed PP qualification
still requires the pinned calculator and .NET runtime.

## Closed owner integration preparation now implemented

`owner_integrations.prepare_adoption(defaults, room_configs, universal, marker)`
resolves the existing headless supervisor precedence and produces two private
migration inputs: gameplay projection and `config/integrations.json`. Only its
count-only `summary` is suitable for support output. Current reference keys are
fully classified: room1/room2 each 169 keys; room3/room4 each 175 keys, with zero
unclassified keys. This proves coverage of the inventory, not final effective
value parity or operational acceptance.

The integration record preserves explicitly declared Discord control/modlog
flags, channel/guild IDs, role names, admin/moderator IDs, greetings, public labels,
invite links and all inactive AI preferences. It contains credential **names**
only, never credential values. AI contexts map independently to
`config/ai-context-<alias>.md` so Standard and Mania context files cannot overwrite
one another. Shared ban/exclusion policies and public/health/cache stores have
fixed host-local destinations. Distinct source policies/caches refuse common-path
projection pending an explicit mapping; they are never silently merged.

The existing runtime bridge now applies this private record instead of its old
disabled defaults when present. With no record, tester defaults remain unchanged.
Enabled Discord/modlog/AI require explicit provisioning through the private,
service-owned `secrets/integrations.env` reference and declared environment names.
The module checks file metadata and required names at startup without reading an
environment file or emitting values. `environment_names(root)` supplies the exact
verified-bootstrap allowlist. Missing credentials fail before native Discord
configuration, worker creation or room creation; diagnostics and read-only preview
do not require integration secrets.
Credential references cannot name interpreter/linker, process, proxy or runtime
control variables (`PYTHON*`, `LD_*`, `PATH`, `HOME`, `SYSTEMD_*`, and related
controls). The three existing PoolRotater credential names remain explicitly
allowed; other `POOLROTATER_*` names cannot bypass live/runtime gates.

Configured websocket remains enabled only with the existing supported endpoint
`wss://notify.ppy.sh` and a qualified provider-notification adapter capability. The
controller must explicitly provide `provider_chat_websocket`; a flag with no real
adapter refuses startup. Parent integration supplies the existing notification
implementation with qualified provider authority; installed behavior still needs
the combined-release donor qualification.

Declared existing bans/exclusions and room AI context files must be imported to
the new private host paths before startup. Metadata and local policy formats are
checked; a missing policy cannot silently become an empty list. This preparation
does **not** copy live private state. The stopped importer must verify exact source
snapshot content and mappings with a migration receipt.
The native ban writer publishes 0640 files. Admission preserves this behavior
only for its fixed host-local ban path, same service UID/GID, single regular file,
no symlinks, and service-owned 0700 host/state/moderation parents. Other policies,
contexts and environment files retain the strict private permission requirement.

`owner_integrations.parity(source, target, mappings=..., additions=...)` checks
every effective key's exact value and Python type, including disabled features.
Unknown/secret-value fields, omissions and changes refuse. Only explicitly
reviewed identity/managed-path pairs can remap; gameplay and integration values
cannot be waived by a mapping. Explicit additions are limited to named M4 rating
reader/preparation fields and verified Standard companions; this does not allow
silently overwriting an existing field. Neither private values nor hashes of
private values appear in the returned audit.

Evidence: `validation/owner-pilot/full-classification.json` and the isolated owner
tests in `owner-tests.log`, including complete current default-key coverage,
inactive-setting/type parity, private output redaction, prohibited credential/URL
injection, pre-room missing-credential and missing-policy failures, per-room
context preservation, actual native Discord configuration and websocket gating.

## Offline stopped adoption and rollback foundation

`poolrotater_host.owner_migration` accepts explicitly supplied **offline copies**.
It has no production discovery, source-configuration path following, OAuth/env
reading, provider call, or service mutation. `/opt/poolrotator` and the existing
production runtime/release roots are expressly refused. This is a preparation
primitive; creating a real stopped source snapshot remains a separately reviewed
and approved operational step.

`create_snapshot(destination, source_config=..., ledger=..., state_files=...,
state_complete=True)` seals a new private snapshot directory. The source config
contains reviewed exact defaults/rooms/universal objects; source paths inside
these objects are never opened. `state_files` explicitly maps allowlisted native
targets to supplied private copy files. The caller must review that enumeration
as complete, including all language and PP-cache files. Declared bans/exclusions
and per-room contexts are required. The SQLite backup includes WAL, passes
integrity and closed current native schema checks, and retains history, human and
shadow ratings, ignored-user policy, download evidence, solo difficulty recall,
Standard/Mania PP achievements, lobby time and statistics privacy. The final
manifest binds every file's bytes/hash. Source files remain untouched; partial
snapshot outputs are cleaned up.

`import_snapshot(root, snapshot, mappings=..., stopped=True)` acquires the existing
installation/account owner lease and refuses existing native preferences or
untracked target state. Installed administration may pass
`owner_lease_held=True` only inside its independently checked `_stopped` lease;
this assertion is not a CLI or configuration option. The root-owned wrapper must
first verify disabled/stopped bot/network units, no service processes, private
snapshot ownership and an independently reviewed manifest digest, then perform
file/SQLite work as the service UID.

The private receipt transitions `prepared → files_imported → ledger_migrated →
committed`. Exact file identities are journaled before copying. Interrupted
copies, rating DDL, historical seeding or final receipt publication resume against
the same snapshot; changed recovery inputs refuse. `migrate_ledger` and
`seed_ledger` retain their existing backup, identity, shadow-policy and repeat-safe
event semantics. Both native materialization and worker loading refuse an
uncommitted owner receipt. OAuth remains separate and is never copied.

Before commit, the importer compares **every effective parent configuration key
and type** against the current native defaults + rendered owner overlay. This is
the headless parent configuration authority, not a claim about every subsequent
worker override. It reports only key
names/categories/counts. Missing fields or changed gameplay, enabled features,
disabled preferences, IDs and greetings cannot be waived. Reviewed per-alias
source/target pairs permit only explicit managed path/identity changes. A declared
conversion path cannot be waived to an empty companion. Nested Mania source syntax
is archived exactly; the already-validated applied leaves are the effective audit
inputs. A partial source configuration is not accepted as a complete parity proof.

Separate installed qualification must compare actual `worker.hello` values after
the supervisor applies its code-owned overrides: the worker manifest version,
ownership/close behavior, shared parent Discord routing, pinned catalog generation
and the fixed managed `state/controller-map-pp` cache. The managed supervisor now
uses this imported cache; legacy unmanaged workers retain their original
version-specific cache. Version mappings approved for parent rendering must not
be presented as worker-version evidence. The public synthetic fixture in
`poolrotater_host/testing/owner_fixture.py` provides four aliases with the documented
room tuning and optionally enabled Discord, notification socket and PP features,
plus invented policies/context/languages/cache. It never reads real configuration.

`rollback_ledger(source, destination, stopped=True)` accepts a standalone isolated
v7 snapshot and exclusively exports every row and native column of current v6
compatible tables, including optional difficulty/PP/privacy tables and native
autoincrement sequence state. New or changed local v7 votes are retained. Unknown
tables, columns or incompatible native types require review. V7 synchronization
tables stay in the archived source rather than being handed to v6 code.

`rollback_state(root, destination, stopped=True, owner_lease_held=...)` creates a
new complete rollback archive while stopped. It includes that compatible ledger,
the entire current v7 ledger/outbox, exact old source configuration, current host
overlays and native room edits, language choices, temporary bans/audit, exclusions,
AI contexts, Discord health/public state and both native PP-cache locations. Its
final `rollback.json` manifest is published only after all copies are durable. A
partial rollback archive is retained for review and has no completion manifest.
The two explicitly declared Discord health/public observer files may use the
native writer's0640 mode. Rollback validates stable service-owned single-link
JSON beneath the private namespace and preserves its exact bytes in0600archive
files. Other private-state, ledger and credential permission guards stay strict;
no source permissions are changed. An interrupted archive is retained, and a
retry creates a new archive rather than overwriting it or reseeding votes.
It does not restore anything or start services. Keeping both old source syntax and
current native edits is deliberate: rollback must reconcile current preferences
into the old runtime, rather than erase post-cutover choices with the old backup.

Validation uses realistically sized invented data (10,001 human votes plus shadow
data), interruption injection, schema/tamper refusal, current-policy mutation,
post-migration changed/new votes, PP/privacy/difficulty history and full current
private-state rollback. The installed four-room donor and actual owner cutover
must still qualify the exact combined artifact and approved target path mapping.

## Current integration and behavior gaps

The following records the initial audit findings. Their implementation is now
integrated into the dev8 candidate; installed qualification is still required.
They are not approved differences or permission to drop existing functionality.

- Generic v7 live configuration initially supported one room only. The integrated
  owner release must explicitly support all four while fresh tester installs
  remain one room by default.
- The initial v7 bridge disabled Discord/control/modlog and chat websocket. The
  closed integration record and pre-room gates now preserve explicit configuration;
  protected unit/verified-bootstrap environment forwarding, provider notification
  wiring and installed behavior still need combined-artifact qualification.
- PP configuration can now project, but pinned .NET8/calculator, cache mapping
  and installed `.pp` behavior need qualification. Existing player-language state,
  custom greetings and owner admin/moderator policy need explicit mapped adoption.
- Current v6.7.34 implements temporary project bans and local operator map
  exclusions. At the initial source audit, the development bot lacked
  `bot/moderation.py`, `bot/exclusions.py`, their bot/Discord command routes,
  `MapPool.apply_operator_exclusions` and separate download-vs-operator exclusion
  state. Merge working behavior while retaining v7's provider provenance and
  ratings outbox; verify temp bans and exclusion reversal do not undo download
  quarantine or signed global exclusions.
- v6.7.34 retains verified DT evidence while scheduling a 30-day research refresh;
  initial development code invalidated evidence after 30 days. Preserve usability
  and managed content/SHA verification without silently dropping DT maps at expiry.
- The pure projection lists Loved/conversion companion requirements when present.
  These are not silently replaced by native Ranked catalogs. Current prepared
  exports need fresh exclusions review even when row-count differences are small.

Conversion follow-up: both configured Mania rooms use `mania_source_mode=variety`
and `mania_convert_max_share=0.30`; this source mode is a style enum, not a
native/converted/mixed enum. Their shared conversion candidate database is a
12,288-byte mutable service-owned runtime file (stat-only inspection). The sealed
v6.7.34 validation companion is 8,192 bytes and contains **11** verified rows.
The worker independently checks candidate keys/rating/revision against this
evidence, plus actual conversion keys and both original/converted difficulty
inside the requested band. A non-empty path is therefore a real capability to
preserve, not an obsolete configuration reference to discard. The actual current
eligible candidate count was subsequently qualified from stable byte copies,
without opening the live SQLite files. All eleven candidates are rejected by
the current owner reader: 4K validation reports actual 7K, while the 7K reader
rejects the candidates' 4K key label. The prepared conversion companions preserve
this exact zero-admission result and the configured 0.30 share. Valid invented
fixtures cover future compatible rows. See
`validation/owner-pilot/conversion-catalog-qualification/report.json`.

`validation/owner-pilot/source-differences.json` pins owner/development code hashes
and changed/owner-only function names for ten differing bot modules. Source was
not copied. It distinguishes required latest gameplay merges from intentional
v7 ratings/outbox/provider changes. Re-run code parity after merges; an old diff
report does not prove final release behavior.

## Catalogs and central analysis

Current v6.7.33/v6.7.34 sealed selections contain Standard 105,502 beatmaps,
105,502 stream features, 40,842 farm report rows and DT 105,129 maps; Mania4 has
19,334 maps/features, 18,952 overrepresentation rows, 16,326 farm rows; Mania7 has
4,347 maps/features, 3,035 overrepresentation rows and 1,777 farm rows.

The previously published tester catalogs contain slightly more rows (Standard
105,510, DT 105,137, Mania4 19,335, Mania7 4,348). Therefore the old signed exports
cannot stand in for the owner's latest sealed snapshot/exclusions without review.
Prepare/re-sign consistent full exports from current sealed source, preserve
required selection features and exclusions, and qualify NM/DT plus all styles.
Do not open live research databases to regenerate selections on client machines.
Raw farm/Mania research files remain central. Stat-only observation found roughly
1.79GB Standard research, 0.78GB Mania research and 42MB local ratings, plus ratings
WAL; these are not transferred as public catalogs. Prepared exports are smaller
because they omit raw research/player-score rows.

Keep collector/analysis services running with their existing identities/paths.
Publish through a separate catalog role, retain signatures/expiry/CA/bootstrap
identities and a last-good client snapshot. Signing-key ownership, renewal and
retirement need an explicit operational procedure; bypassing expired trust is
not an update strategy.

## Consistent backups, adoption and interrupted migration

### Explicit adoption of the existing 50/60 provider budget

The reserved `owner` unit also preserves the read-only inventoried v6 scheduling
and resource fields: nice0, unlimited memory/swap,256 tasks and no CPU quota.
Generic independent tester units retain nice15/1GiB/512MiB/128 tasks. Qualification
always stays nice15 and bounded; no production-priority process is run during
preparation. Only exact known prior owner-unit resource variants may upgrade;
arbitrary edited units still refuse. The new protected lifecycle/admission and
cleanup behavior remains an explicit reviewed cutover change.

The owner configuration uses 50 requests per minute and burst capacity 60.
The previous v7 provider default spaced every call by 1.2 seconds (burst one).
The integrated release retains that default for other installations and adds a
protected, explicit owner budget adoption. It never infers permission from an
editable room setting or silently changes the tester's pacing.

After the owner snapshot import has committed and the new local operator client
profile has been configured, run the installed root administration command
`owner-budget-prepare` with both bot and network services disabled and stopped.
It verifies the preserved source configuration, successful parity receipt,
installation generation and local client-profile binding before installing the
root-owned 50/60 policy. There must be no process under the service UID. This
step starts no service, performs no provider request and reads no old credentials.
Owner startup refuses a missing or mismatched budget. A required-policy marker
precedes publication so interruption cannot silently fall back to burst one.
Changing the client profile requires stopped requalification; do not delete the
protected marker to bypass it.

Provider usage is recorded separately from pacing authority. Diagnostics expose
bounded recent, UTC daily and lifetime counters, numeric HTTP status and coverage
gaps without token/account/client IDs. Historical v6 usage remains a separate
archive; it is not relabeled as v7 observations. Telemetry failure cannot cause
a provider request retry. The staged operations-report adapter understands this
new coverage and must be adopted with the exact reviewed service-path changes.

### Source snapshot and cutover sequence

No live snapshot/copy has been made by this preparation. The final migration
procedure must be reviewed and qualified before production copying:

1. Before the agreed cutover, pin current supervisor/worker manifests, immutable
   release/environment paths, configuration precedence and sidecar service
   dependencies. Save private configuration/policy files with restricted modes.
   Record the source inventory/hash receipt privately; do not bundle credentials.
2. Drain/close all existing owner rooms using the current supported lifecycle,
   stop the current bot only after explicit approval, and prove upstream closure,
   clean local ownership and absence of its supervisor/workers. Stop only writers
   relevant to the migrated stores under the approved change list. No v6/v7 overlap
   on the hosting account. A stale observer or systemd failed flag is not ownership.
3. As the database-owner identity, use SQLite online backup to new private,
   exclusive destinations. Include current WAL through SQLite; never `cp` only
   ratings.db or use immutable mode on changing live files. Existing
   `ratings.migration.snapshot_ledger` already provides bounded backup, integrity,
   fsync, digest and exclusive publication without source checkpoint/DDL. It
   requires private source/destination ownership and does not establish quiescence.
4. Back up ratings/player-language/moderation and required caches separately.
   Transient API/process-control/account lease files and active ownership must
   not be adopted as v7 authority. Preserve source files untouched. Re-home only
   explicit mapped stores into the new service-owned private installation.
5. Prepare the M4 ledger on the copy under stopped-service/owner admission;
   retain the exact pre-migration backup and receipt. Seed historical eligible
   human votes once with stable identities; retain neutral votes, timestamps,
   ignored-user policy and shadow data locally. Do not turn skips/performance
   evidence/shadow votes into public human votes or upload the whole database.
   Keep the owner reader in its independent/local mode at crossover. Preserve
   all local historic weighting while the separately granted historical backlog
   is synchronized. The normal uploader sends at most100 events about every30
   minutes; bulk manual sync loops can exceed unchanged hub quotas and quarantine
   contributions. A seed receipt proves local event creation, not central
   acceptance. Before a later, explicit switch to canonical network weighting,
   reconcile eligible historical outcomes, accepted shared totals and the verified
   snapshot. Do not replace complete local history with a cold partial global
   snapshot or relabel quarantined opinions as accepted. Sharing new votes and
   storing other operators' contributions in the hub do not require changing
   owner map-weighting mode during this migration.
6. A prepared migration receipt fences startup until complete. Repeating after
   an injected crash must reconcile the same backup and journal, never overwrite
   recovery input, reseed or silently create a fresh stream. Restore lineage/epoch
   requires explicit review, so a restored database cannot duplicate accepted votes.
7. Perform operator account authorization interactively as required, complete
   private parity audit and start v7 only after cutover approval. Capture count-only
   local/hub evidence before enabling explicit sharing and supervised gameplay.

Each ledger backup is transactionally consistent. Consistency across different
stores requires stopped relevant writers and a common migration receipt; SQLite
backup of several files taken while writers run is not a cross-store snapshot.

## Donor rollback demonstration

Use full prepared selection catalogs and realistically sized **invented** ratings
on disposable donor identities; do not copy real votes/credentials to qualify.
The exact combined release must demonstrate:

- Four owner aliases/settings preserved and separate one-room tester upgrade;
  before/after private effective-configuration parity, mapped-path checks and no
  provider credentials/access outside the invented namespace.
- Consistent backup with a non-empty WAL, exact eligible historical vote counts,
  repeat-safe migration/seed and interrupted receipt/DDL publication.
- Active diagnostics while the same account/service locks are held by the running
  invented lifecycle, then clean stop/referee/worker cleanup and clean ownership.
- Restore the original **unmigrated** backup to the old donor runtime after proving
  the new worker/service stopped. Never point old code at the migrated live v7
  ledger. Use preserved old release/config/environment; keep hub events already
  accepted durable and do not rewind its ledger to hide client rollback.
- Re-enter v7 through reviewed lineage/epoch recovery after rollback, demonstrating
  no duplicated historical seed/post-boundary vote and no resurrected suppressed
  contribution. Hub outage leaves old verified catalogs/ratings usable locally.
- Force errors at each approved transition, retain bounded receipt/status evidence,
  remove disposable units/users/staging and leave no workers or fragments.

Production rollback after v7 gameplay requires a decision about **new votes and
state written after cutover**: a raw pre-cutover restore loses them. Preserve the
v7 ledger first, use a reviewed compatible export/reconciliation into a new v6
copy, and verify policy/timestamps/deduplication before switching. Until this path
is qualified, rapid rollback with no post-cutover data loss is not proven.

## Admission still requiring human input

After all coding/qualification and the concrete change/rollback review, request
explicit owner cutover approval and any interactive osu! authorization required.
Real startup/gameplay/Discord/user experience and operator sharing decisions
require the owner/tester. Development may continue independently on code/donor
qualification and a separate hub without touching production lobbies.

## Existing operational services: source bindings and retirement

The read-only public-code/unit inventory is recorded in
`validation/owner-pilot/operational-dependencies.json`, produced by
`inventory_operations.py`. It read fixed public source files and sanitized unit
metadata only: no env/config credential files, logs, live SQLite, provider calls,
job execution or production changes. This is surrounding operational dependency
evidence; those readers cannot remain pointed at v6 after owner cutover.

The explicitly selected owner source namespace is
`/var/lib/poolrotater-v7-pilot/owner` and unit
`poolrotater-v7-pilot-owner.service`. The reviewed source selector is
`POOLROTATER_OWNER_OPERATIONS_INSTANCE=owner`, accepted only for that fixed
namespace. With the selector absent, all existing v6 roots and service behavior
remain unchanged. A conflicting arbitrary legacy-root override refuses owner
activation. Existing output/Discord delivery/report model settings stay separate
and must be retained by the existing operational units; no bot credential is
copied into an operational reader.

| Existing reader | Current input | Explicit owner input | Required reviewed change |
| --- | --- | --- | --- |
| Four-hour report and five-minute health | `poolrotator.service`, runtime `logs/roomN.log` | Owner unit; `logs/room-roomN.log` | Apply source patch, bind read-only namespace and preserve report private delivery configuration |
| Statistics/history/lobby links | Runtime `state/ratings.db` | `state/controller-ratings.db` | Keep native ledger schema; owner-identity read-only connection, never schema migration by a reporter |
| Discord health | `state/discord_control_health.json`, `logs/multiroom.log` | `state/controller-discord-health.json`, qualified shared log sink | Preserve current heartbeat and detailed error history; verify shared log destination rather than inventing healthy observations |
| API window report | Runtime `state/api_rate.db` | `state/provider-usage.db` | Actual observed HTTP exchanges, separate epoch, explicit gaps/unfinished replies and authority totals |
| Wiki live and analytics | `instances/roomN.json`, `instances/universal.json` | `config/rooms/roomN.json`, empty native universal | Keep exact room settings; read public invite from owner room1, not empty universal |
| Wiki catalog metadata | Runtime `data/farm.db`, `mania.db`, `mania_7k.db` | Pinned native room catalog paths in protected owner catalog store | Fixed role/namespace and immutable metadata qualification; no stale symlink or invented metadata |
| Wiki lobby trigger | Old universal file and report snapshot | Owner room1 config and same current report snapshot | Update path trigger in cutover change list; no extra provider account connection |
| Wiki command/features publisher | v6 sealed release/deploy attestation | Fresh registered v7 code proof, current worker evidence and reviewed v7 source manifest | Preserve source fingerprint/manifest ownership checks; v6 attestation cannot qualify a v7 installation |
| Join redirect service | Published room link passed to landing handler | Same behavior | No runtime source binding required |

`validation/owner-pilot/operations-adapters` contains isolated copies and unified
patches of the allowlisted public readers, plus exact source/staged hashes and an
offline test driver. **They are not installed or automatically applied.** The
report/health and wiki analytics/live/lobby source changes preserve existing parsing,
chat review, public filtering, privacy and output behavior. The provider window
query reads completed HTTP status updates directly so a later 429 is not missed
by an old cursor. Coverage starts at its observed epoch, never includes old v6
history by implication, and reports incomplete when telemetry has gaps, discarded
history or unfinished replies. The original v6 telemetry database/history must
be retained as a separate archive; it must not be relabeled a v7 observation.

Preparation remains incomplete in these explicit places:

- The staged docs publisher now consumes the fresh installed code-proof action,
  checks the closed source inventory/hashes in the fixed protected namespace,
  requires four fresh healthy worker versions matching that registration, and
  rechecks both before preparing or publishing. It retains the old semantic
  validator and wiki ownership/revision pipeline. That code/environment proof
  does not prove live operational acceptance. A missing reviewed v7 command/feature
  manifest yields `ready: false` and retains existing wiki content. A reviewed
  static v7 player manifest is now prepared at
  `operations-adapters/FILES/manifests/v7.0.1.json`: 35 commands and 18 features,
  with current merged-source hashes, routing/style/language checks, legacy function
  witnesses and the existing semantic validator. It explicitly reviews managed
  DT identity, configured language fallback and shared human feedback instead of
  claiming identical code. The frozen candidate must rerun that generator/check
  before installing this separately root-owned manifest.
- The old API-usage cursor watcher cannot track asynchronous status completion
  safely. Its staged owner branch now reads genuine persisted v7 epoch/day/lifetime
  rollups directly without a second writer or cursor copy. Late replies stay
  attributed to their original UTC request day; scheduled and observed elapsed
  waits stay distinct. Raw six-hour and 366-day retention do not reset lifetime
  totals. The unit must use the fixed provider telemetry source for both
  source/database arguments, and preserve the old v6 archive separately. This
  source binding and installed read access still need qualification.
- Installed unit `ReadOnlyPaths`, owner UID traversal and native read-only SQLite
  sidecar access, config path triggers and shared Discord log sink must be tested
  with the exact installed combined candidate before production approval. The
  staged copies alone do not establish operational parity.

Keep central Standard/Mania collector and analysis services, source credentials,
working research DB paths and central analysis pipelines in their existing
roles. The approved crossover must retire or reroute only terminal triggers
that roll out or restart the old v6 bot, as enumerated below. They build centrally;
distributed hosts only consume finished catalogs.
Public inventory shows no need to transfer those research/player-score files.
Existing Standard collection/acquire and Mania pipeline units were inactive at
observation; preserve their configuration rather than restarting them to test.

The legacy deployment path is a distinct ownership risk: `poolrotator-deploy`
can stop/start the old unit and switch its symlink/runtime; the refresh-release
helper produces v6 releases and queues old `state/rolling` requests. Its timer
was disabled, but that alone is not an account-ownership fence. The approved
cutover manifest must disable/retire or mask the old deployment/refresh/adoption
targets and pending old rollout queue before allowing v7 rooms. Record their
previous enabled state for rollback; do not rerun a failed refresh or reset a
queue during preparation. Legacy collectors stay intact while legacy automatic
bot restart/rollout paths are retired. The owner machine must never run both
versions on the same hosting account.

The dedicated hub/catalog donor integration has a separate receipt-bound driver:
`scripts/owner_hub_donor_qualification.py` and `owner_hub_donor_probe.py`.
`plan` prepares its local phase plan without opening the donor access file.
`stage` requires independently confirmed runtime, hub and catalog inventory
digests plus explicit full prepared exports. The canonical owner inputs are
`validation/owner-pilot/conversion-catalog-qualification/prepared`; capped or
invented selection catalogs are refused. Both Mania conversion companions are
required. No signer custody or existing trust root is copied into the donor.

The disposable run installs two stopped genuine pilot instances and independent
dedicated service identities. Its exercise uses installed protected hub/catalog
bootstrap entrypoints, a fresh TLS pair and fresh signing authority in a single
isolated loopback cgroup (Nice 15, CPU quota 400%, memory 2 GiB, swap 1 GiB,
1200-second maximum). It imports every full mode over HTTPS twice, then exercises
real enrollment, native privacy projection, historical human feedback, concurrent
votes, lost acknowledgment retry/dedup, cached last-good state through corruption
and outage, stopped backup packages, explicit latest deletion policy restoration,
revocation of all prior hosts and replacement enrollment. Invented room IDs are
directory observations only; this driver never contacts osu! or authorizes rooms.

The driver retains private bounded failure details on the disposable donor and
exports only count-based progress/acceptance receipts. A failed mutating phase
cannot replay automatically. Cleanup refuses while the exact owned cgroup or
any recorded service UID still runs; purge requires its completed cleanup
receipt, verifies the original incoming digest, then removes only that run and
its input archive. The final donor report remains separate from actual gameplay,
owner credential provisioning, operational-reader integration and production
cutover acceptance.

The hub fixture deliberately submits more quickly than the ordinary scheduler.
Its first retained run captured 4,000 historical human opinions and 2,000 new
opinions, but those counts are **submitted/captured**, not accepted aggregates.
The installed hub enforced its unchanged 500-events-per-minute quota: before
continuation, 1,500 of 6,001 unique events were accepted and 4,501 were quarantined
for `host_quota`, with 1,000 canonical maps. Terminal draining is not complete
historical seed acceptance. Owner network-reader activation remains blocked until
all eligible owner historical opinions have been accepted or explicitly
reconciled; independent local reading preserves those opinions in the meantime.

The initial lost-response assertion encountered one of those quota quarantines.
An explicit continuation validates the exact failed 18-check boundary, both
ledger sizes and central outcome counts; it refuses any changed boundary or
unexpired quota window. It does not replay catalog imports, enrollment, seeds or
concurrent submissions. A fresh unique opinion is then committed through the
actual installed hub, its response deliberately dropped, and its retry checked
against both immutable local payload identity and unchanged central event and
canonical row counts. The original quota outcome remains recorded. This
continuation also uses `make_pool` against the actual full managed catalogs and
native statistics display in independent/network modes with an eligible catalog
map; it never replaces them with small rehearsal catalogs.

Dedicated hub/catalog qualification completed against runtime
`3d5b1d0f5a21e0c6f74a85c36387ee3cb25c963a085dda0d30ea2d08fcb65c51`.
The count-only receipt is
`validation/owner-hub-donor/full-final/qualification-report.json`: 41 checks,
574.26 seconds of recorded exercise work across explicitly guarded continuation
phases, both clients sharing the same full three-mode content, and real TLS
bootstrap/signing custody isolation. Scope includes the default installed Standard
preset's independent and canonical native pool/stat readers, unchanged catalog
content IDs through outage, corruption recovery, a three-page restored snapshot,
authenticated denial of both revoked old hosts, replacement epoch review and
latest user-deletion policy preventing resurrection. Full owner four-worker,
external operational-reader and actual gameplay acceptance remain separate.

The second fixture repair uses an authenticated installed snapshot fetch to test
revoked access. An exhausted, empty local sync tick can correctly return success
without sending any HTTP and therefore cannot establish credential validity.
The explicit restored-authority checkpoint keeps the original package restore,
policy and revocations; it refuses changed ledger sizes or a partially replayed
check list. Future fresh runs now wait for the genuine quota minute to expire
before the lost-ack opinion rather than weakening the quota. Eleven focused
isolation-bound driver tests and final syntax checks passed; unchanged product
unit suites were not repeated for these fixture-only changes.

Scoped donor cleanup and purge also completed. The final
`validation/owner-hub-donor/full-final/purge-report.json` confirms both client
instances, dedicated listener users, owned processes, private synthetic authority
and incoming archives are absent. Only compact local receipts and bounded private
failure evidence remain. Earlier failed runs were likewise purged; they are not
counted as successful acceptance runs.


### Confirmed legacy lifecycle triggers before cutover

Read-only installed systemd metadata confirms Standard analysis OnSuccess points
at poolrotator-refresh-release.service. The adoption, two prior supervisor
maintenance and results-intermission jobs are inactive/disabled timer pairs, but
their services can still be started directly. The review-only cutover manifest
now enumerates all of these targets for private backup and reviewed masking;
no old unit was changed. At the approved boundary, clear only the obsolete
analysis OnSuccess target, preserve central analysis execution, and publish
new full prepared exports through the explicit reviewed catalog procedure.
The new daily timer currently renews already sealed content, not research data.
The existing join landing service has no legacy DB/config binding and remains.
