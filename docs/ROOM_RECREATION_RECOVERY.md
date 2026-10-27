# Room recreation and stopped recovery

This describes the recreation repair under development. It does not authorize
clearing unknown ownership, retrying an uncertain creation, or installing an
unqualified artifact. Use the release's exact verified kit and registered entry.

Scheduled empty-room recreation retains the two-hour setting. The worker first
checks provider readiness and selects/verifies a usable native starting map,
while the existing room remains available. A failed preparation defers recreation
with backoff. Updates, maintenance and managed operations share lifecycle
admission; booked sessions continue to suppress scheduled recreation.

If replacement creation fails after the old room closes, the controller can
contain that alias only while shared connection and account authority remain
qualified. Healthy sibling rooms keep playing. The affected alias stays fenced;
unknown creation is never automatically replayed or cleared. Shared connection
or authority failure still requires its normal qualified recovery path.

## Stopped ownership commands

Use the existing instance, credentials, enrollment, configuration and state.
`pr` is a convenience name, not an installed command on every host; on some
systems it is the Linux print utility. Invoke the registered
`poolrotater_host/pilot_admin_entry.py` with the instance argument using
`/usr/bin/python3.12 -I -S -B` and `nice -n 19`.

Ownership review, inventory and explicit recovery require the controller to be
stopped, its unit disabled as required by the installed policy, and all dedicated
service-UID helper processes to have exited. Record original network/timer
states, pause admitted helper schedulers through the supported maintenance
procedure, and stop the network agent last. Do not stop occupied rooms to obtain
an inventory. Preserve deliberately stopped slots and all existing settings.

During each stopped ownership command the root deployment and network-admin
leases prevent reconciliation or an explicit network operation from starting
agents. They do not stop services or change timer enablement. Between commands,
a longer repair must retain supported helper coordination and restore the exact
original preferences; a sequence of unrelated shell commands is not a recovery
transaction.

`ownership-review` retains the existing bounded active-room and known-ID review.
An empty active list is not proof that an unknown creation did not occur.

`ownership-inventory` is an explicit, longer, read-only provider discovery step.
It traverses owned realtime history twice, including ended and future rooms,
using reviewed pinned/ID pagination and a required terminal empty page. It refuses
incomplete, capped, malformed, changing or foreign-owner history. The journal,
configuration and provider authority must remain unchanged. It returns public
room identities/times plus journal/evidence digests; it omits room titles,
passwords, player lists and credentials. OAuth refresh and the normal request
ledger may still be updated, just as for other authenticated read operations.

The inventory is limited to non-deleted API rows. `history_complete` and
`history_stable` do **not** authorize clearing ownership. `start_allowed`,
`recovery_applied` and `absence_recovery_qualified` remain false even if no room
is returned. A discovered actual replacement ID can be reviewed through the
existing explicit candidate path, which performs fresh ownership and ended-room
checks and uses the existing prepared/committed recovery transaction. Never use
the ID of the previously closed room as the replacement candidate.

If no candidate is discoverable and upstream visibility/deletion semantics
cannot prove absence, retain uncertainty. The required upstream action is to
confirm the attempted creation outcome against the hosting account, including
soft-deleted room records and referee-server associations, or provide the actual
replacement ID for direct ownership/end verification. Do not reinstall or edit
journals to manufacture clean ownership.

## Recovery helper pins

A matching version or bundle digest alone is insufficient to replace a recovery
pin. The exact installation identity, registration, transitions and retained
upgrade history must establish lineage. Read-only lineage diagnosis can identify
a missing ancestor, a foreign identity or registration-field differences without
exposing private record values or changing the pin. History gaps and unqualified
pins remain refusals. Only the supported verified-lineage pin refresh may write
an updated recovery record.

Keep redacted support output, the exact failure code, operation UUID and evidence
digests. Never share token files, OAuth codes, private backups or credentials.
