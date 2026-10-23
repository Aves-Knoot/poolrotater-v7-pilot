# Private bookings: controlled pilot checklist

**Status: proposal only. This pilot has not been approved or executed.** No
production portal, OAuth client, public route, service, private slot, or live
booking has been configured. This checklist grants no permission to perform
those actions.

The isolated source includes the v7.2.14 catalog-rollout merge. Unique source
evidence is 1,577 passed and 1 skipped, with no failures/errors; all 310
original aggregate failure/error cases have case-specific resolution evidence.
Dedicated source and exact-package Chromium runs passed, as did exact-hash
installed donor upgrade and full booking/catalog qualification. Frozen
unpublished v7.3.0 runtime SHA-256 is
`fa15a041bb3ae7469ff668fc306f69b8b61e63c46a4e43dd447a32cbfe1cef92`, from
source commit `1a713e9`, superseding `1c4ca...` and `67d545...`. Candidate
handoff archives and checksums are verified and independently reproducible. It is unsigned, unpublished,
not queued, and has no actual osu! room acceptance. See
PRIVATE_BOOKING_ACCEPTANCE.md for evidence and limits.

## Human approvals required before pilot preparation

- [ ] A named human operator approves the pilot scope, test accounts, host,
  alias, time window, and the live-room actions included in the pilot.
- [ ] The OAuth application owner configures a client ID and secret, the exact
  redirect `https://poolrotater.com/private-lobbies/oauth/callback`, and the
  `identify` scope. The secret stays in the private local portal configuration.
- [ ] The site owner separately approves publication of the portal route and
  any required reverse-proxy configuration. A release approval does not imply
  website publication permission, and website publication permission does not
  imply release approval.
- [ ] The release owner approves signing and distributing a
  candidate after the frozen-candidate evidence below is complete.
- [ ] The host owner approves the specific private alias and the host-agent
  restart needed after enabling its booking policy.

If any approval is absent, leave both opt-ins disabled and do not register
credentials, add a public route, install/start the portal service, configure a
slot, or enable a host alias.

## Candidate and host preflight

- [ ] Record the exact source revision and proposed release version. Build one
  candidate and record its final artifact and runtime hashes only after the
  build is frozen. Do not substitute hashes from a later rebuild.
- [ ] Complete the required isolated feature, browser, compatibility, native,
  and regression suites. Preserve the runner outputs and record failures,
  skips, and environment limits. Do not describe component fixtures as a real
  browser or live-service test. Source, exact-package browser, and installed
  donor qualification now pass; preserve the current exact-hash receipts.
- [ ] Review current-hash installed-upgrade preservation and service-stopped
  receipts against representative donor state; retain the original state.
  Immediate rollback is feature-off with booking ledgers retained. Binary
  downgrade is outside this pilot checklist and requires separate review.
- [ ] Confirm complete, verified catalogs for every preset offered by the
  pilot. Supported presets are Standard Auto, Standard Fixed, Standard Loved,
  Mania 4K, Mania 7K, and Catch. A missing or partial catalog makes that preset
  unavailable; do not silently fall back to another ruleset or public room.
- [ ] Confirm the selected host is enrolled, reports a fresh matching
  installation/generation, and has no unresolved update, recovery, native
  operation, or ownership state.
- [ ] Confirm the chosen alias is one of `room1` through `room4`, is private,
  stopped, positively closed in the ownership journal, and has no pending or
  failed native operation. Do not convert or take over a public room.
- [ ] Back up the hub ledger, host booking ledger, host policy, and relevant
  managed state using their supported backup procedures. Store backups with
  the same private access controls as the originals.

The host opt-in is explicit and local:

```text
printf '%s' '{"format":1,"enabled":true,"aliases":["room1"]}' | \
  pr managed-bookings --managed-stdin
```

Use the protected installed administrator entry point. The command changes
only local booking policy. Review its response and verify it reports no
provider dispatch or room start. Enabling a previously disabled host policy
requires the separately approved existing network-agent restart before the
host can exchange booking messages.

The hub portal and slot are separate opt-ins. `booking-check` must show the
intended local portal configuration without printing credentials;
`booking-slots` must show only the approved alias and preset inventory. Keep
`enabled:false` until the publication approval is in hand. Do not put OAuth
secrets or room credentials in command arguments, shell history, tickets, or
this checklist.

## Behavior and privacy checks

- [ ] The public page shows only the safe slot label, state, supported presets,
  and freshness. Inspect public API and page responses for room IDs, join URLs,
  passwords, player names/IDs, maps, queue entries, and private settings; none
  may appear.
- [ ] A signed-in owner can reserve one available slot. A second reservation
  for that owner or slot is refused. Repeating the same request ID and payload
  returns the same booking; changing its payload is refused.
- [ ] The room remains unavailable while preparation is pending, the host
  observation is stale, or native ownership is uncertain. Restart the host
  exchange and portal with active state present; neither restart may erase a
  lease or make the alias appear free.
- [ ] Confirm the ready room's claim deadline is 600 seconds from its first
  fresh, authoritative, ready-and-owned observation. The owner or explicitly
  designated friend can claim it by joining. Other friends can join and play
  after the room is claimed, but their arrival does not satisfy the claim
  requirement. A designated friend remains a Player until the owner grants
  Controller access.
- [ ] Exercise claim expiry at 600 seconds with neither eligible claimant
  present. Verify the warning appears at its fixed time, is not repeated on
  every poll, and that expiry requests closure only once. The slot stays
  unavailable until the host confirms closure and cleanup.
- [ ] Exercise inactivity after a successful claim: 1,800 seconds continuously
  empty or without meaningful gameplay triggers closure. An unknown/stale
  roster breaks empty-time continuity and cannot authorize expiry. Test that a
  completed map or at least 60 seconds of verified participation renews the
  gameplay clock, while rapid start/abort activity does not. A verified active
  map may defer inactivity only to its bounded expected end plus 120 seconds;
  expected map length is bounded to 24 hours. A stuck playing flag cannot renew
  this limit indefinitely.
- [ ] Move the test clock backwards, restart the host ledger, and resume with
  fresh observations. Expiry must remain suppressed until the durable clock
  high-water mark is reached; a forward clock jump alone must not authorize
  closure.
- [ ] Verify warning timing and deduplication: claim warning at 60 seconds
  remaining; inactivity warnings at 300 and 60 seconds remaining. Messages
  use only the fixed approved text and never interpolate user or provider
  strings.
- [ ] Test roles with at least an owner, designated friend, ordinary player,
  and invited guest. Owner and Controller can start, abort, skip, and configure
  supported settings. Only Owner can end, invite, grant, or revoke. Player and
  invited guest have no settings or role controls. A revoked Controller loses
  local authority immediately, queued actions are cancelled, and execution
  rechecks authorization after restart.
- [ ] Verify invite creation is Owner-only, an invite can be revoked by issuing
  a replacement, and an invite expires after 30 minutes. HTML
  content is escaped and join links use only the fixed osu! multiplayer host.
  Invited players receive join details only, not the owner/controller panel,
  roster, settings, or operation history. Revoked, expired, and ended invites
  return no password or join URL.
- [ ] Verify no public website card, public Discord status, community event,
  or log displays a private join link, password, roster, map, or queue. Confirm
  the existing private statistics collector and `.stats` behavior continue
  under their existing consent and visibility rules without adding booking
  details to public output.

## Settings, catalogs, and version checks

- [ ] Test settings draft versus applied state. No native change occurs before
  Apply; the website shows queued, failed, live, and next-map receipts
  accurately. The player allowlist has no room-recreation control.
  Apply is limited by the shared 10-second cooldown and revision checks. A
  stale settings revision is refused rather than silently overwriting a newer
  local change.
- [ ] Check all six advertised presets against complete catalogs and native
  selection. Verify preset-specific supported mods and settings. Standard
  exposes DT mode only through its approved selector; clock mods and Mania-only
  FI/MR are not offered in Standard. Catch keeps its fixed settings. Mania and
  Catch do not offer AR filtering. OD and BPM filtering remain unavailable
  unless the complete catalog later qualifies those fields.
- [ ] Try supported hard and preferred map-length filters and supported
  Standard AR filters. Try unsupported OD/BPM and Mania/Catch AR filters;
  expect a fixed refusal and no partial application.
- [ ] Test expiry and safe reuse separately: let a claim expire, then verify
  the alias cannot be reused during uncertain closure or incomplete cleanup.
  After a fresh authoritative closed observation and completed cleanup, verify
  the old booking is terminal and the slot may be reserved again. Repeat with
  an ended booking whose host cleanup is withheld; reuse must remain blocked.
- [ ] Test mixed versions with the frozen candidate hub and a supported older
  host in an isolated environment. Older hosts remain booking-disabled by
  default; an unsupported host or route is reported unavailable. No fallback
  can create a public room, repurpose an alias, or bypass host eligibility.
  The v7.2.14 exact-version fleet rollout gate remains in force; mixed hosts may
  wait at that gate until eligible. Test the exact supported upgrade chain and
  legacy behavior before making any compatibility claim.

## Uncertainty, rollback, and acceptance

When host, roster, ownership, closure, cleanup, or database state is unknown,
stop new reservations and retain every ledger, lease, role, receipt, and
ownership record. Do not clear a policy, delete a database, stop a live room,
or reuse an alias to get past an uncertainty. Keep the slot held and escalate
through the reviewed operator recovery process.

For an approved immediate rollback, use feature-off: first disable new
reservations in the local portal configuration and stop the portal only
through its reviewed service-manager
procedure. Preserve the hub and host state. Remove a host alias from booking
policy only after its booking cleanup is confirmed. Do not delete OAuth or
booking history as a rollback shortcut. A binary downgrade is separate and
requires a reviewed stopped transition or consistent closed-state backup; do
not overwrite active ledgers.

Isolated source, compatibility, privacy/role and lifecycle evidence is recorded
in PRIVATE_BOOKING_ACCEPTANCE.md. Exact-package browser and donor qualification passed. Actual live acceptance remains pending and requires separately
approved real OAuth/browser/gameplay, expiry, reused-slot and operator review
steps in this checklist. Passing isolated tests alone is not pilot authorization
or release acceptance.
