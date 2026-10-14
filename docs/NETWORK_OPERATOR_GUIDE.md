# Distributed hub operator guide (M3)

This optional integration enrolls an installation in an invite-only hub, sends
bounded observations and lists consenting public rooms. It neither authorizes
an osu! account nor starts/stops a lobby. These instructions accompany the integrated owner-hub pilot, including the
current qualified private releases. The original dev7 does not contain these commands.
The combined release also includes optional M4 feedback synchronization, described
in RATINGS_OPERATOR_GUIDE.md. Use OWNER_HUB_UPGRADE.md for state-preserving upgrade
and explicit trust adoption. Technical qualification uses invented identities and
isolated donor processes; it does not establish actual osu! room acceptance or
authorize the owner's production crossover.

## Operator inputs and trust

The hub administrator manually verifies the intended operator and hosting account.
Obtain the hub HTTPS origin, public CA PEM and independently confirmed SHA256,
operator UUID, and a short-lived invitation. The invitation is bound to your
account, installation UUID, generation and explicit directory-sharing choice.
Invitation/account approval is administrative trust, not remote attestation.
One active installation may enroll per account; transferring requires central
revocation first. No account password, osu! access/refresh token or OAuth client
secret is sent to the hub. The agent reads only local account identity metadata.

Before the administrator issues an invitation, obtain your installation binding
with this read-only command (replace `first` with your installed instance):

```bash
sudo /usr/bin/python3.12 -I -S -c 'import json; v=json.load(open("/var/lib/poolrotater-v7-pilot/first/.poolrotater-host.json")); print(json.dumps({k:v[k] for k in ("installation_id","generation")}))'
```

Send only those two UUIDs, your instance name/current registered runtime digest,
intended numeric hosting account ID and directory-sharing choice to the hub
administrator. Separately state whether you consent to ratings contribution;
directory enrollment does not imply that consent. The administrator assigns
the operator UUID and checks the complete binding before issuing the invitation.
These UUIDs identify the installation; they grant no account or room access.
Do not send configuration, token files or the entire state directory.

Use the root-owned release path printed by the installer for `pilot_release`.
Run administration through the verified system-Python entry, for example:

```bash
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance first network-status
```

The same entry accepts `network-configure`, `network-enroll`, `network-resume`,
`network-rotate`, `network-leave`, `network-reset`, `network-support`,
`network-unit`, `network-start`, `network-stop`, and `network-remove`.
Configure/enroll/reset read one bounded JSON document on stdin. Supply it through
a private file or a private interactive helper; keep shell tracing disabled and
never put invitations or credentials in command arguments, environment variables,
chat, Git or support reports. The registered interpreter and complete pinned
runtime are verified at the actual service UID before input is consumed.

Configure JSON has exactly these fields:

```json
{
  "origin": "https://network.example:9444",
  "ca_pem": "PUBLIC_CA_PEM_WITH_NEWLINES",
  "ca_sha256": "INDEPENDENTLY_CONFIRMED_SHA256",
  "operator_id": "ADMIN_ASSIGNED_OPERATOR_UUID",
  "account_id": 123,
  "directory": false
}
```

The full document must fit 65536 bytes; use a dedicated small trust bundle.
An unchanged profile can be configured again. Changing the origin, trust or
identity while enrolled or while an operation is pending is refused. Leave first,
or use the explicit local reset workflow after coordinating central revocation.
`directory: false` keeps all room observations out of uploads. Changing this
bound sharing decision requires a new invitation/enrollment.

Enrollment JSON is exactly `{"invitation":"pri_…"}`. The client generates its own
random network credential and persists the request before dispatch. If a reply is
lost, inspect `network-status` and run `network-resume`; do not create a second
identity or remove pending files. Rotation likewise persists the new credential
before contacting the hub. A completed exact retry recovers the same operation.

Configure and enroll through the same protected entry with the closed JSON on
private stdin (`network-configure`, then `network-enroll`). Confirm
`network-status`; if enrollment is pending, use `network-resume` instead of
creating a second enrollment. For the first shared-vote test, have the maintainer
grant the enrolled host `contribute` before enabling local ratings sharing.
Historical seed permission is separate and is unnecessary for fresh player votes.
The maintainer's already enrolled owner installation retains its existing binding
and sharing state during compatible upgrades; do not reenroll or regrant it just
to use a newer runtime.

## Heartbeat service and local rooms

`network-unit` installs only `poolrotater-v7-network-first.service`, disabled and
stopped. `network-start` explicitly starts the enrolled agent; `network-stop`
stops it; `network-remove` removes its reviewed stopped unit and retains state.
There is no enable command in this bounded pilot workflow. These commands address
no lobby service. The agent has its own process, locks, limits and cgroup; its
systemd namespace hides local OAuth secret files.

Default cadence is 60 seconds plus bounded jitter. Each HTTPS request has a whole-
call deadline, including DNS, and its worker is reaped. Failures back off from
5 seconds to 300 seconds plus jitter. No failure triggers room closure, account
authorization, ownership recovery or a map change. Restarting the agent reads its
durable sequence and sends a new observation; it never replays an old private-room
snapshot after sharing was withdrawn.

Only fresh, healthy, matching worker/broker observations with a confirmed empty
password and public admission are projected. Unknown, private, password-protected,
stale and missing rooms contribute no ID, title, link or map before transmission.
No player identities, chat, passwords, arbitrary URLs, host paths or IP addresses
are uploaded. Current ranges and catalog set IDs come from the running workers;
unknown catalog identity is `unavailable`. Directory entries are self-reported.
Stale means unknown availability at 180 seconds; entries disappear at 600 seconds.
Stale entries have no join link/current occupancy/current map. Revocation and leave
hide immediately when accepted by the hub. A disconnected leave suppresses further
local sharing, but old central entries can remain until a retry or freshness expiry.

Before code upgrades or stopped ownership-recovery commands, explicitly stop the
network agent as well as the lobby service: both run under the dedicated service
UID, and the existing upgrade/recovery orphan-process guard must remain intact.
Ordinary local room operation is independent of hub availability.

## Revocation, expiry and local abandonment

`network-support` returns bounded, redacted local configuration/enrollment/agent
status and timestamps. It does not contact the hub and does not prove remote
health. Share it with the maintainer rather than sharing private files.

After credential expiry, central revocation or hub restore, obtain renewed
approval and an invitation. Use `network-status` privately for the old host UUID.
`network-reset` accepts exactly:

```json
{
  "expected_host_id": "OLD_HOST_UUID",
  "acknowledge_central_revocation_required": true
}
```

This removes only local network authority/heartbeat state. It does not revoke the
hub record, bypass the one-account rule or affect rooms/OAuth/catalogs. An
interrupted reset is resumed with `network-resume`. If abandoning an uncertain
initial enrollment without a local host UUID, use `expected_host_id: null` and
have the hub administrator find/revoke the enrollment by its bound installation
before attempting another invitation. Do not treat a local reset as proof that
remote authority or directory information was removed.

## Dedicated staging hub administration

The hub uses its own private 0700 directory, marker and 0600 SQLite database.
It refuses operator-host roots and unrelated databases. Run the module from the
trusted candidate environment, under a dedicated staging identity:

```bash
python -m poolrotater_host.network.admin --root /private/staging-hub initialize
python -m poolrotater_host.network.admin --root /private/staging-hub inspect
python -m poolrotater_host.network.admin --root /private/staging-hub serve --cert /private/server.crt --key /private/server.key --bind 127.0.0.1 --port 9444
```

`serve` defaults to loopback and requires explicit TLS files; it installs no unit.
These commands describe a separate staging hub. The pilot already has a dedicated
owner hub at the approved9444 origin; testers enroll there instead of initializing
or serving their own hub. Do not deploy these staging commands into the website.
`invite` reads JSON with `binding` (operator_id/account_id/installation_id/
generation/directory), `label` and `expires_seconds` (60..86400). Its stdout includes
the invitation: handle it privately. `revoke` takes `{"host_id":"…"}` on stdin.
Administration exists only locally; there is no administrative HTTP route.

`backup` accepts `{"filename":"snapshot.sqlite3"}` and creates a fresh private
snapshot inside the hub directory. Backups contain private identity data and
credential verifiers. Keep them private. `restore` targets a fresh, absent root and
reads `{"source":"/private/staging-hub/snapshot.sqlite3"}`. It validates/copies the
snapshot in a private unpublished staging directory, revokes every restored host
and invitation, changes the server epoch, then publishes the new root. It never
overwrites a running hub. Restore requires every host to reenroll; old credentials
cannot regain authority from an old snapshot. A failed restore removes its staging
directory and leaves the source untouched.

Protocol: [NETWORK_API_V1.json](NETWORK_API_V1.json) and
[network.v1.schema.json](../schemas/network.v1.schema.json). Historical draft
schemas are separate design records. HTTP has bounded bodies, deadlines,
concurrency and quotas; redirects and ambient proxies/trust are disabled on hosts.
Earlier M3-only qualification excluded M4 ratings/events and actual rooms. Each
combined release requires its own M3/M4 installed qualification; consult
the release notes and SHARED_HUB_TEST.md for evidence and remaining gates. Remote room commands
and actual owner crossover are not authorized by this guide.
