# Executable-release authority and rollout

This is the activation review for the isolated automatic-updates implementation.
It does not authorize production installation, new signing custody, listener
activation, queued-update changes or another operator's activation. Technical
acceptance is tracked in AUTOMATIC_UPDATES_PLAN.md. Synthetic worker/controller archives under validation/automatic-updates/fixtures
are test artifacts and must never be published as pilot updates.

## Current owner activation — October 15, 2026

The owner subsequently authorized activation of the complete release package.
The standalone hub code was upgraded against its existing state with unchanged
credentials, grants, ratings and epoch; the hub-only window was2.441seconds.
No room controller/worker was restarted, no owner settings/queued rollout changed,
and the operator's machine/consent has not been modified. The owner engine remains
v7.0.5. This activation supersedes the earlier development-only exclusion for
these standalone services; it does not authorize arbitrary future room restarts.

Active executable-release origin: `https://91.107.237.182:9446/`.
TLS CA SHA256: `58ec13427d16a40aaac091c6d64aa531a295e67a939a6cb77fa977e8b673c35e`.
Release root SHA256: `d88125b6a8406d607307ee9403989b30b02331f2335b9a2bad8c49ff64dff027`.
The feed initially approves only the exact qualified v7.0.6 runtime on `pilot`;
`stable` has not been promoted. No synthetic15–19 fixture is served/published.

Runtime: `/opt/poolrotater-v7-releases`; state:
`/var/lib/poolrotater-v7-releases`; immutable served tree:
`/srv/poolrotater-v7-releases`; independent root-only signer custody:
`/var/lib/poolrotater-v7-release-signers/authority`. The dedicated
`poolrotater-release` UID cannot read custody. Listener unit
`poolrotater-v7-releases.service` and daily signed metadata-renewal timer
`poolrotater-v7-releases-refresh.timer` are enabled. Renewal uses sealed code and
an isolated network namespace, and does not approve releases or move channels.
TLS certificate renewal and reviewed root rotation remain planned maintenance
before expiry; daily metadata renewal does not extend TLS/root lifetimes.

The current owner bot does not contain updater CLI actions. Use the protected
root-only standalone publisher, which verifies its entire sealed code inventory:

```bash
release_admin=/opt/poolrotater-v7-cutover-tools/updater-activation-20261015/release-admin.py
sudo /usr/bin/python3.12 -I -S -B "$release_admin" status
# Only after qualifying a genuine successor and exact baseline:
sudo /usr/bin/python3.12 -I -S -B "$release_admin" publish \
  --bundle /REVIEWED/next-runtime.zip --sha256 NEXT_VERIFIED_SHA256 \
  --before-bundle /REVIEWED/previous-runtime.zip --before-sha256 PREVIOUS_VERIFIED_SHA256 \
  --channel pilot
# Promote the SAME artifact only after actual consenting pilot acceptance:
sudo /usr/bin/python3.12 -I -S -B "$release_admin" promote \
  --sha256 ACCEPTED_PILOT_SHA256 --channel stable
```

A private signer/TLS backup exists outside development/publication under
`/var/backups/poolrotater-v7-releases/`. It has not been copied off this VPS.
Retain signer custody and arrange a separate private off-host backup through the
owner's normal backup process; neither GitHub nor the public origin contains it.
The stopped hub backup is separate and must not be restored just to change code.

Actual public-origin proof used the separate donor VPS, public trust only, a full
52,862,288byte SHA-verified download, threshold signatures and real range resume.
It created no enrollment or room and removed temporary downloads. Evidence:
`validation/automatic-updates/activation-preparation/public-origin-proof.json`.
Private production checkpoints remain under
`/opt/poolrotater-v7-cutover-tools/updater-activation-20261015/evidence/`.
The operator must perform the same-instance bootstrap and consent in
`PILOT_UPGRADE_706.md`. A first genuine successor's real-player automatic roll
remains pending; do not fabricate one by publishing a synthetic QA artifact.

## Roles and trust

Release signing uses a new independent threshold Ed25519 root with two keys.
Keep root-only signing custody separate from the public served tree, installed
listener code, catalogs and all provider credentials. Only a local administrator
can initialize, publish, promote, renew or resume publication. The HTTPS origin
has no publishing route and its service identity cannot read signing custody.
Published artifacts are addressed by their SHA-256 and cannot be overwritten.

Operators independently confirm the release-root hash and TLS CA hash, then
choose their channel and download/install/controller/reporting consent. Enrollment
authenticates host telemetry; it does not grant code-signing authority. A host
never supplies osu! credentials to the hub to receive releases.

## Preparation and installation review

Prepare these from the exact qualified bootstrap source, using the existing
pinned catalog wheel cache without resolving or downloading dependencies:

1. A same-instance manually pinned bootstrap kit for accepted owner/tester
   predecessors, plus a fresh-install kit. Include the installer, runtime ZIP,
   checksum manifest, operator guide and exact supported predecessor hashes.
2. A separate release-origin kit from `updates.origin.prepare`, with frozen
   dependency and code inventory; record the independent inventory hash.
3. The revised standalone hub kit from `network.hub_service.prepare`. It adds
   authenticated private update diagnostics without changing public directory
   consent or rating contribution policy. Preserve and back up existing hub data.
4. An activation checklist showing the intended installation paths, service
   identities, confirmed TLS inputs, signer location and served location. Do not
   import synthetic fixture keys, certificates, bindings or enrollment state.

The release origin's `install_assets` requires an already created dedicated
nonlogin account, fresh root-controlled code/state paths, protected TLS files,
separate custody and an externally confirmed frozen inventory SHA-256. It installs
assets only; it does not create accounts, register units or start listeners.
The prepared listener defaults to loopback port9446; expose it only through the
reviewed TLS/firewall/reverse-proxy arrangement. Never copy OAuth or Discord files.

Its listener runs as the dedicated service UID, has read-only public targets and
root-owned immutable code, and is blocked from the signer directory. The separate
root renewal unit uses a private network namespace. After explicit activation
approval, register the exact prepared units, run protected `check` and
`check-refresh`, start the listener and enable the daily renewal timer. Renewal
maintains metadata freshness; it neither approves new code nor changes channels.

`updates.origin.configure_renewal(state, custody=...)` binds the renewal job to the
current root and served tree without starting a timer. The protected origin entry
supports `configure-refresh`, `check`, `check-refresh`, `serve` and `refresh`.
Retain signer backup separately; public artifacts and inventories are insufficient
to recreate authority. Root expiry/rotation requires a separately reviewed local
trust change; the server cannot silently replace an operator's root.

## Upgrade an existing hub without re-enrollment

An ordinary hub code upgrade must retain its existing state directory, service
UID, TLS files, host bindings, credential rows, grants, ratings and server epoch.
Do not use fresh `install_assets`, `init` or a disaster-recovery restore for this:
restore intentionally fences old credentials and is a different operation.

The code-only stager is `scripts/stage_automatic_update_hub_code.py`. Its reviewed
kit is `validation/automatic-updates/hub-kit-final`, containing145 runtime files;
the independently confirmed `runtime/inventory.json` SHA-256 is
`c868320e2b114f14c78be2ffd9ff4616fb7aacf71984b3311b254183f4372909`.
It verifies every code file, refuses existing/nested destinations and writes only
a fresh root-owned sibling code tree. It never opens hub state, changes units or
starts a service. Keep the original sealed tree intact for a code rollback.

After a separate production activation review, use the real recorded hub paths
and UID; the following names are placeholders, not guessed production paths:

```bash
sudo /usr/bin/python3.12 -I -S -B stage_automatic_update_hub_code.py \
  --kit /ROOT_PROTECTED_REVIEWED_KIT \
  --inventory-sha256 c868320e2b114f14c78be2ffd9ff4616fb7aacf71984b3311b254183f4372909 \
  --existing-prefix /CURRENT_SEALED_HUB_CODE \
  --prefix /FRESH_SIBLING_SEALED_HUB_CODE
sudo -u EXISTING_HUB_USER /usr/bin/python3.12 -I -S -B \
  /FRESH_SIBLING_SEALED_HUB_CODE/bootstrap.py --root /EXISTING_HUB_STATE check
```

Before switching, record the checked existing unit and its active/enabled state,
current code inventory, server epoch, host/grant counts and private backup using
the supported backup procedure. Retain that backup for disaster recovery only;
do not restore it to perform this switch. Review the unit diff: only the sealed
bootstrap code path changes; UID, state path, TLS/bind/port, sandbox, restart
policy and enablement remain the same. Arrange a short hub-only maintenance
window, stop only that hub unit, install the reviewed unit change, daemon-reload,
and restart it only if it was previously running. Bot room services are separate
and are not stopped. No host receives a new invitation or provider credential.

Run protected `check` as the same hub UID and compare the epoch, host/grant and
rating state. Ask an already enrolled consenting client for its existing network
status/heartbeat and private update-report receipt. Do not start its room service
as part of this check. If the new listener fails, retain the evidence and restore
only the previous unit code path against the same state after reviewing any
schema changes. This qualified release changes no authority/rating schema.

The installed donor test performed this code staging, protected old/new checks
and real HTTPS authenticated reporting with an existing credential. Every
network/credential/rating row except the normal monotonically advancing safety
clock, plus TLS files and original code, remained intact. It did not modify any
production unit. Report: `donor/hub-code-upgrade-report.json`.

## Approve a release

Use the protected current pilot admin entry and its pinned environment. Example
paths below are the proposed separate authority, not existing production state.
Set `pilot_release` and `pilot_instance` from the reviewed installation first.

```bash
pr() {
  sudo /usr/bin/python3.12 -I -S -B \
    "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
    --instance "$pilot_instance" "$@"
}
release_custody=/var/lib/poolrotater-v7-release-signers/authority
release_served=/srv/poolrotater-v7-releases

# One-time creation, only after explicit activation approval and path review.
pr release-init --custody "$release_custody" --served "$release_served"

# Approve immutable code against an exact supported prior artifact.
pr release-publish --custody "$release_custody" \
  --bundle /REVIEWED/new-runtime.zip --sha256 REPLACE_NEW_SHA256 \
  --before-bundle /REVIEWED/previous-runtime.zip --before-sha256 REPLACE_PREVIOUS_SHA256 \
  --channel pilot
pr release-status --custody "$release_custody"
```

The `release-*` names above are native admin-entry actions. The module CLI spells
the corresponding commands `pilot-release-*`. Default publication channel is
pilot. Publishing inspects both real archives and classifies the payload: only
worker-local bot changes with identical controller/storage/environment contracts
qualify as worker updates. A release containing controller/shared changes requires
coordinated maintenance. Unsupported schema, interpreter or dependency migrations
are refused, even if signed. Exact installation-level native checks run again at
stage, queue, admission and controller commit.

The current signed descriptor has one exact baseline. A release for a different
lineage requires an explicitly reviewed compatible artifact/baseline, rather than
silently admitting arbitrary predecessor hashes. The manual bootstrap kit is how
accepted owner/tester families first converge on the common updater baseline.

Keep the manual GitHub kit and previous recovery releases available. Do not push
the dirty development checkout or signer files into the private repository. Audit
the curated source inventory, credentials exclusion and archive checksums before
manual publication. The executable release origin and GitHub are separate
distribution routes; publishing to one does not activate the other.

## Pilot and stable promotion

Publish to pilot, observe explicitly opted-in hosts and their private reports,
and obtain real-use results before promoting the same immutable SHA to stable:

```bash
pr release-promote --custody "$release_custody" \
  --sha256 REPLACE_APPROVED_PILOT_SHA256 --channel stable
```

Promotion cannot move a channel backward in release sequence. Hosts verify
signatures, expiry, repository identity, durable sequence/content high-water marks
and local installed versions. A channel switch or replay cannot authorize a
downgrade. Controller consent remains separate on every host. Occupied rooms can
stay on the previous worker until eligible; do not promise simultaneous rollout.

`release-renew` refreshes expiring metadata with unchanged channel choices. If
publication is interrupted, inspect `release-status` and use `release-resume` to
finish the same durable signed publication. Do not initialize a replacement root
at the existing location or delete its pending state.

## Diagnostics, recovery and retention

Local `update-status` includes installed/available versions, transfer progress,
waiting rooms, durable operation and reviewed outcome. Independent opt-in hub
reports contain only a closed coarse diagnostic contract. Inspect a host through
the protected hub admin `updates-inspect` action with the existing explicit private
stdin contract `{"host_id":"CONFIRMED_HOST_UUID"}`. It never publishes player data
or config, and does not authorize a room action. Backup includes the optional
private update-report DB. Restore retains that DB as evidence without activating
stale host reports after authority revocation/reset.

Use the operator guide for paused/held operations and the exact operation UUID.
Known compatible worker activation failure may restore the previous worker.
Automatic controller downgrade, state/schema rollback and unknown-ownership
clearing are deliberately refused. Explicit recovery can complete a qualified
prepared transition or abort it while stopped; committed/active rollback needs a
separate reviewed repair release. Never erase journals to make a host retry.

Automatic local cleanup is daily, with bounded retry when native locks are busy.
It retains active/pending/last-good/rollback releases, private-pilot and owned
process references, at least the newest three code releases, and a minimum
one-day age. It attributes staging and installed deletion to root receipts,
preserves full recovery metadata, and retains environments, config, credentials,
catalogs, votes and history. A suspicious link or conflicting inventory is held.
Cache/transport cleanup uses the same references and preserves trust high-water
state. Operators can request `update-cleanup` manually.

The initial publisher supports a bounded signed history (128 descriptors and
256KiB metadata). It refuses an over-capacity publication rather than dropping
approved baselines or exceeding the protocol. Plan a separately reviewed signed
history compaction/rotation before reaching that bound; do not delete currently
referenced targets manually. This limitation does not affect host cleanup.

## Acceptance and activation gates

Before publication, verify the original goal against frozen code and full-catalog
installed evidence: repeated worker/controller updates, occupied matches, outage,
signature/replay/expiry/tamper refusal, interrupted transfer, process interruption,
restart reconciliation, rollback/refusal and configuration/vote/history retention.
Include fresh and both accepted predecessor installs, separate installed hub/origin,
the stable launcher after a switch, telemetry and receipt-bound cleanup. Record
tests not run. Then update the canonical plan and both handoffs.

Production hub/origin changes, signer creation, bootstrap maintenance, pilot trust
delivery, each operator's explicit consent and real osu! gameplay acceptance remain
separate activation steps. Do not change current rooms or an existing queued
deployment as part of this isolated development batch.
