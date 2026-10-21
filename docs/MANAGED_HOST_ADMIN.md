# Managed-host administration — v7.2.9 operator guide

v7.2.9 is published and its protected installed qualification is complete within
the scope recorded in the release's VALIDATION.json. This does not mean every
host has installed it: v7.2.8 hosts keep legacy behaviour until their safe update
finishes and their capabilities prove migration. See
[V7_2_9_MANAGED_HOST.md](V7_2_9_MANAGED_HOST.md) for exact validation attribution.

The current hub needs a dedicated, verified staff-only `fleet_channel_id` before
Discord fleet controls are enabled. Channel setup is a separate administrative
prerequisite; it must not be bypassed by using public lobby panels or a channel
visible to unrelated bots. Until that channel is configured and validated,
`.fleet` is unavailable on that hub. Ordinary existing room controls remain
separate.

## Staff Discord surface

The independent hub bridge accepts `.fleet` in its explicitly configured
`fleet_channel_id`. This must be separate from owner/community lobby panels.
The bot checks the guild and configured administrator role at receipt, and checks
channel visibility for both incoming commands and actual outgoing messages.
Non-staff roles must not be able to view this channel. Individual viewing grants are refused except for the guild owner and bot; use staff-role permissions. Existing server
administrators retain Discord's inherent access. Ordinary lobby panel commands,
room links, artwork and player counts keep their existing routes. Map progress
stays on the website and is omitted from Discord lobby panels.

The staff bridge and the host's independent agent do not depend on a running
room worker. The fixed installed root reconciler starts an admitted stopped
controller when a valid operation needs it; it has no arbitrary shell or unit-name
interface. Older hosts without managed capabilities report unsupported/stale
capabilities and keep their existing operation.

Examples (`owner` selects the owner's bound hub identity):

```text
.fleet hosts
.fleet status owner
.fleet status owner room1
.fleet settings owner room1
.fleet settings owner room1 countdown_seconds
.fleet greeting owner room1 Frogma
.fleet open owner room1
.fleet close owner room1
.fleet restart owner room1
.fleet configure owner room1 {"countdown_seconds":25,"selection_styles":["stream","jump"]}
.fleet configure owner room1 {"custom_join_greeting_changes":{"Example player":"Welcome!","Old player":null}}
.fleet cancel owner room1 OPERATION_UUID
```

A host may be selected by `owner`, its UUID, or an exact unique operator label.
Labels containing spaces can be inspected with `.fleet hosts` and addressed by
UUID. Duplicate labels never select an arbitrary host.

Close and restart normally wait for the target's verified empty/idle boundary.
Live-safe configuration goes through the native main-loop hotloader. A mode,
privacy or constructor-owned pool change needs target-only recreation. Existing
visible playlist entries are retained for future-pick default changes. Changing
the queue target downward lets the already-visible queue drain naturally.

`--immediate` explicitly permits interrupting the target room:

```text
.fleet close owner room1 --immediate
```

Optional compare-and-set selectors prevent a stale selection from being replaced
by fresher observations:

```text
.fleet close owner room1 --revision 8 --generation ROOM_INCARNATION_HEX
.fleet open owner room1 --revision 9 --generation none
```

Without selectors, the hub captures its current fresh revision and incarnation.
`none` explicitly means no observed room; it never means infer a different room.
An explicit private conversion requires `visibility: private` and a nonempty
password in the typed configuration request. Passwords are not echoed in replies,
settings snapshots, notices, public panels or the public directory. Such changes
belong only in this verified private staff channel or protected local stdin.

## Receipts and settings

Discord's gateway message ID becomes a durable operation UUID. Redelivery of
that same message returns the original request and receipt, even after revisions
or expiry change. Editing it into another operation is refused. This prevents
reconnection/replay from issuing a second open or restart.

A queued response is not proof of provider completion. The staff bridge posts
changed host receipts, including waiting, applied, refused or uncertain outcomes.
After an uncertain result, inspect the existing operation and retain state before
issuing another request. A room ID is successful lifecycle evidence only after
native ownership/closure checks; leaving the referee is not closure.

`.fleet settings` distinguishes desired saved defaults from last confirmed
effective defaults. It labels historical snapshots as synchronizing when they do
not match the latest capability revisions. Temporary player selections remain
separate from persistent defaults. Constructor/main-loop qualification of all
fields and effective acknowledgments is covered by the published qualification;
see VALIDATION.json for the distinction between exact final-runtime checks and
retained earlier native-lifecycle evidence.

Large greeting dictionaries are transferred as authenticated 24 KiB pages on
`/v1/hosts/self/managed-settings`, outside the frequent capability heartbeat.
Each document has a content digest, installation/generation/host binding and
per-room revisions. The hub exposes no public settings-download endpoint.
Partial documents are not adopted; replayed pages are idempotent, and stale
snapshots cannot roll back newer revisions. `network-support` includes fixed
administration/settings phases without payloads or private identifiers.

Use `custom_join_greeting_changes` to add, replace or delete individual entries
without replacing the whole dictionary. A JSON null deletes an entry. Other
existing/default greetings remain. The native 1,000-entry bound still applies.

## Local host ceiling and service intent

The default concurrent room ceiling is four, further bounded by the provider's
allowance. Public, private, pending and uncertain creations share that ceiling.
Central commands cannot raise it. The operator can use the protected local entry:

```sh
printf '%s\n' '{"max_concurrent_rooms":4}' |
  sudo /usr/bin/python3.12 -I -S -B \
    "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
    --instance two managed-limit --managed-stdin
```

Zero blocks new creation and drains rooms naturally once empty. Lowering a limit
does not abruptly remove occupied players. Explicit ceilings survive upgrades.
The managed release's stopped preparation adds addressable slots through room4 when
a retained installation has fewer aliases. Existing room order, saved defaults,
intent and channel bindings remain. New slots start deliberately stopped with no
room identity or creation reservation; provisioning a slot does not open a room.
Installed upgrade qualification is recorded in VALIDATION.json. Do not edit
`host.json` manually to work around that preparation.

An explicit whole-service operator stop saves desired-off before systemd is
called. A later explicit operator start can resume only the previously running
rooms covered by that global stop, provided no later room decision changed their
revisions. Individually stopped rooms stay stopped. Automatic update, recovery
and reconciler starts preserve the saved intents. Replayed Discord `.shutdown
all` messages do not shut down rooms opened after the original request.

A deliberately stopped managed host can accept a controller update without
creating temporary rooms. The installer requires a complete confirmed-closed
ownership journal, the stopped owner lease and no reserved creations. A zero
ceiling remains zero. Queued operations retain their original IDs and revisions.
Only a provable stop-intent change may update an already-queued configuration
pin; unrelated edits still hold the deployment for review. The independent
network agent remains reachable while an update waits or is held.

Managed provider startup can rebase an idle pacing ledger after a genuine
machine reboot. It retains request counters, halts and the complete stored
cooldown, then performs the usual fresh provider identity and room-ownership
checks. It does not recover an unknown request reservation, pending OAuth
exchange/refresh, same-boot clock discontinuity or uncertain room operation.
Local status does not perform this recovery. These unresolved states remain
visible for the existing scoped recovery workflow.

First managed startup and stopped upgrades prepare every supported full catalog
through the pinned catalog TLS/TUF origin. Preparation adds missing modes and
preserves already-selected content IDs; there is no local collection or analysis.
A durable catalog transaction handles interrupted staging and activation.
Configuration migration likewise pins exact planned bytes before replacement.
These paths passed installed qualification with the attribution recorded in
VALIDATION.json; synthetic provider tests are not actual osu! gameplay acceptance.

## Managed participation and rollout

Managed membership requires administration, shared ratings/statistics, staff-only
redacted diagnostic/chat reporting, and signed automatic updates. The stopped
installer/upgrade records this versioned policy and prepares data before workers
start. There are no separate sharing/update opt-outs in managed settings.
Enrollment alone is not proof that the hub has accepted the contribution policy;
the authenticated managed exchange establishes that authority. Hosting-account
OAuth and Discord credentials stay local and are preserved.

This release changes shared controller contracts. Its rollout uses the proven
host-wide empty/idle admission path, including intentionally stopped slots,
rather than a worker-only swap. Original automatic updater paths from published
v7.2.6, v7.2.7 and v7.2.8 passed installed qualification with the exact reference
revisions and limits recorded in VALIDATION.json. Queued, waiting-empty,
installed and healthy remain distinct states. Never force an occupied-host
restart or clear ownership journals to accelerate deployment.

Managed slot preparation: retained hosts gain four addressable aliases during
stopped root preparation, while missing slots remain deliberately stopped. The
local concurrent ceiling (default4) remains separate from intended active room
count and is preserved, including zero. Extra slots do not inherit Discord
channel IDs or credentials. First-start and interrupted preparation reuse a
root-owned exact plan. Installed slot/upgrade coverage is recorded in VALIDATION.json.


## Owner chart-exclusion distribution

The origin can bind its root-owned catalog-refresh configuration to the owner's
existing chart-exclusion file with `configure-refresh --operator-exclusions`.
The binding pins the file owner; it does not copy chat evidence, player names or
other descriptive fields into public metadata. Exact chart IDs apply across
Ranked, DT, Loved, Catch and Mania selections, matching the native local policy.

The new catalog-exclusions timer checks every five minutes and signs a new
publication only when exclusions are added. Existing client refresh schedules
then fetch the compatible signed feed; other operators need no manual import.
Unchanged lists do not republish full catalogs. Removing a local row does not
silently undo a signed exclusion; an explicit signed reversal remains required.
Invalid or changed-owner source files leave the last signed publication intact.
The listener and room services do not restart for an exclusion publication.

The owner origin binding and five-minute timer are deployed. All sixteen current
owner exclusions were independently verified in the three compatible signed
feeds with native pinned TUF verification and all five full catalogs unchanged.
That proves publication; a particular host receives the feed during its normal
refresh. Do not report a community host as synchronized without its receipt.

The original v7.2.9 installer archive retains an earlier copy of this guide with
candidate labels. This published guide supersedes those labels. Runtime and
installer digests remain immutable; no reinstall or new runtime is needed for
this documentation correction.
