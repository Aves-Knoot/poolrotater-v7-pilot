# Maintenance and recovery administration

v7.1.8 operator guide. These commands are available after the qualified
v7.1.8 controller is installed; older controllers do not expose them.
Use the current registered protected administrator entry from the release README;
below, `pr` means that existing command with `--instance YOUR_INSTANCE` supplied.
Do not execute a copied checkout, reuse an old release path after an upgrade,
or launch another OAuth/provider helper next to the running controller.

## Ordinary automatic updates

Keep the existing update timer and policy. `pr update-status` explains discovery,
download, staging and installation. Worker changes use each selected room's
independent empty/idle gate. Shared controller changes require the coordinated
all-configured-rooms-empty window. Spectators count as occupants. Late joins
postpone admission; an overdue update never authorizes a kick or occupied closure.

`pr status` and `pr support` distinguish the installed controller, a fresh running
controller observation, individual workers, queued target/blocker, maintenance
progress and the operator's next action. A worker may have a newer version than
its controller after a compatible independent rollout. Old service failures and
lifetime uncertainty counters are historical evidence, not proof of a current
fault. Ownership remains authoritative in the journal/recovery receipt.

## Explicit planned maintenance

This stops selected rooms; it does not change saved settings or automatically
restart the service. An active worker/controller deployment must finish or be
cancelled through its existing documented controls before requesting maintenance.
Use one UUID for a request and retain it for retries and cancellation:

```sh
maintenance_id=$(python3 -c 'import uuid; print(uuid.uuid4())')
pr maintenance-request --operation-id "$maintenance_id" --mode empty
pr maintenance-status
```

Omitting `--room` selects every configured room. Repeat `--room room1 --room room2`
to select a subset. Empty mode keeps occupied rooms operating, pauses a room only
at an empty idle boundary, independently checks the referee roster before closure,
then parks the confirmed closed alias. A late join resumes gameplay/postpones
closure. Already closed rooms remain closed while the remaining rooms drain.

An operator who explicitly wants a one-map notice and closure uses:

```sh
pr maintenance-request --operation-id "$maintenance_id" --mode one-map
pr maintenance-status
```

This explicitly authorizes closing selected occupied rooms **after** a confirmed
notice and the current map's match/scoring cycle. If requested while idle,
starting or already scoring, it waits for the next observed running map and its
return to idle. An empty idle room closes through the independently rechecked
empty-room path without waiting for a new player or match. A late join postpones
that closure and requires the warning/match sequence before any occupied close.
This mode is never chosen by ordinary automatic updates. It does not abort a running map to meet a deadline.

Notices run on the existing gameplay thread and use its existing provider
connection. Positively undispatched pressure retries within a bounded budget.
Unknown notice delivery is not replayed and does not authorize closure; the
progress reports review required. No reply text, credentials or API token belongs
in these commands or an auxiliary maintenance script.

## Cancellation and interruption

```sh
pr maintenance-cancel --operation-id "$maintenance_id"
pr maintenance-status
```

Cancellation is acknowledged by affected live workers before reporting terminal
completion. It releases an uncommitted drain. It cannot undo a closure that
already committed and never silently reopens a closed alias. Repeating the same
request ID is idempotent; changing its mode/room selection requires a new request
after cancellation completes. A different ID cannot cancel the current request.

Pending maintenance found after a controller restart requires review rather than
replaying notices or closing another map. Retain the instance and receipt; cancel
the retained request before requesting a fresh one. Terminal completed/cancelled
receipts remain evidence and are not replayed. Requests expire after24hours;
workers release uncommitted pauses on expiry. An uncertain provider closure still
requires the ordinary stopped ownership recovery, not cancellation or journal edits.

## After draining and during upgrades

When all selected aliases report closed/complete, capture `pr support`. If the
purpose is a shared controller upgrade, drain every configured room, run `pr stop`,
and follow the qualified release README's same-instance stopped upgrade procedure.
Keep OAuth, catalogs, enrollment and all state. Use the newly registered entry
afterward. The updater does not remotely start an operator's stopped installation.
If the purpose was a shutdown, leave it stopped; boot enablement is not changed.

Consecutive stopped upgrades retain their exact committed transition receipts.
Old launch hints, including confirmed closed parked aliases, can retire only
through positively closed journal rows and a bounded
verified chain of immutable predecessor runtimes/approved workers. Missing,
changed, aborted or unresolved history is a review condition. Upgrade history
directories are readable by the dedicated service account even under a restrictive
installer umask. A recognized stopped controller-update service is rebound to the
new qualified launcher automatically; custom units or active services are refused. Never remove
ownership journals, discard queued votes, edit release paths, or reinstall just
to get past recovery.

For a failed start: retain state, read `pr support`, and run the documented
stopped `ownership-review`. Follow its precise clear/resume procedure only with
verified owned/ended-room evidence. Do not blindly retry uncertain MakeRoom.
`systemd failed` and a clean ownership journal can coexist; resetting a failed
unit is not ownership recovery. Normal local `start` performs fresh authorization
and ownership admission and reports its safe failure code/HTTP status.

For a fresh host, use the published install/configure/authorize/catalog/enrollment
steps first. Maintenance requires an already running controller and never starts
an unconfigured or stopped host. Existing sharing opt-outs, update pauses and
service-enable intent survive installation upgrades.

## Fresh installations and current release selection

Download the current installer ZIP asset from the GitHub release; do not use the
source ZIP. Verify the release checksum, extract into a new directory, then run
`sha256sum -c SHA256SUMS.txt`. Use its README and authorization/catalog/network
setup guides. OAuth setup remains local to the hosting account. Catalog import
fetches the complete prepared selection databases from the hub; no local research
collection or analysis is started. Existing installations should follow the
same-instance upgrade instructions and keep their state, not repeat fresh setup.

Automatic sharing/update defaults apply to new configuration only. Explicit
opt-outs, update pauses and service enablement choices remain authoritative.
Stopped community installations are never remotely started by publication.

## Evidence to return after an incident

Return redacted support, update/maintenance status, stopped ownership-review,
the affected room IDs/times and the bot/network/updater journal around the event.
Keep original state locally. Never send token/secret files, OAuth callback URLs,
or raw configuration containing credentials. Central sanitized diagnostics help,
but a missing historical journal cannot establish a process's exit/signal/OOM cause.
