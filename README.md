# PoolRotater dev14 — resilience repair

Existing dev12/dev13 `two`: use RESILIENCE_UPGRADE.md. Download the attached
`poolrotater-7.0.1.dev14-live-pilot.zip`, not Source ZIP. Preserve existing settings,
OAuth, titles, ranges, catalogs, ratings and enrollment; no retuning or reinstall.

Ports the owner v7.0.5 chat-timeout and scoped failed-worker containment fixes.
No unfinished automatic updater is bundled or enabled. Known uncertainty and
ownership safeguards remain. Keep healthy rooms running after acceptance.

The following cumulative installation reference is retained for NEW setups only.
Existing operators should use the upgrade instructions above instead.

# Community display pilot - state-preserving dev11 upgrade

Existing operators: follow COMMUNITY_DISPLAY_UPGRADE.md first. Keep your current instance, OAuth, catalogs, titles, ratings and hub enrollment. The kit starts no services. New display timing requires the coordinated hub activation. The following setup reference is cumulative; do not reinstall an existing instance.

# PoolRotater integrated owner-hub pilot

The dev10 operator onboarding release adds protected stopped room settings and
clearer network/ratings progress. For the requested Standard Auto + Mania4K
fresh installation, follow [TWO_ROOM_OPERATOR_HANDOFF.md](TWO_ROOM_OPERATOR_HANDOFF.md)
and [TWO_ROOM_TEST.md](TWO_ROOM_TEST.md). The no-preset installer default remains
one room; ordinary upgrades retain the original room count and all state.
Use `room-settings-configure --settings-stdin` for validated partial title/gameplay
patches while the bot/network services are disabled and stopped. It starts
nothing, changes no topology and preserves unrelated native preferences. Active
`room-settings-status` reports saved defaults rather than live temporary choices.

Dev10 also repairs fatal local pacing deferrals in routine chat acknowledgments
and reads. Positively identified pre-dispatch refusals retry with bounded backoff;
authentication errors, corrupt state and uncertain requests remain strict. A
fatal service exit coordinates closure of known occupied rooms before shared
transports stop; uncertain ownership still requires explicit recovery. Fresh
ownership review no longer requires installing a unit first, and status/support
avoid inspecting each full catalog twice.

Directory opt-in with fresh accepted heartbeats can appear in the owner's separate
Community Lobbies website section. Its cards/counts never change the four owner
cards or controls. OAuth approval, a bound hub invitation, a central contribution
grant and actual human gameplay remain explicit operator/maintainer steps.

This kit adds optional host registration, shared good/bad votes and full prepared
catalog downloads to the existing bot. Fresh installation creates **one Standard
automatic-difficulty room configuration**. Installation starts no room or service
and performs no account authorization. Existing installations keep their settings,
authorization, catalogs and resolved ownership history during the stopped upgrade.

Existing dev7, dev8 or dev9 testers: follow [OWNER_HUB_UPGRADE.md](OWNER_HUB_UPGRADE.md). Do not
reinstall, repeat ownership-clear or sign in again for a clean existing instance.
Owner four-room migration has a separate private backup/configuration parity gate;
this kit never discovers or replaces the owner's currently running v6 service.

The dev9 repair accepts existing decimal-string Discord identifiers without
changing their saved representation, imports supported historical native SQLite
columns by name, and retains the native language-offer sidecar. It preserves
saved settings and player history; it does not turn temporary difficulty/style
choices into new administrator defaults. Owner adoption still requires a stopped,
private source/configuration preflight before changing the running installation.
See [docs/OWNER_V7_MIGRATION.md](docs/OWNER_V7_MIGRATION.md) for the public boundary.
Startup queries active owned rooms, so ended room history does not exhaust the
bounded listing. Existing active rooms and uncertain creation still block startup;
authorization and exclusive ownership checks remain required.

The liveness repair prevents a worker observation older than 15 seconds from
closing every room when healthy workers are waiting for the shared authorization
mutex. Stale observations remain stale and cannot qualify an empty room for
maintenance or be published as current. Explicit worker connection, process,
failure and ownership-recovery signals still stop unsafe operation. Upgrade the
same stopped instance; retain its OAuth, catalogs, settings, ratings and journal.

The reconnect repair preserves native missing-channel retry behavior during
bounded transport recovery: a temporary HTTP 404 remains a missing channel,
while startup still requires verified chat membership. Other HTTP denials and
invalid channel evidence keep their typed refusal. Room mutations are not
replayed to repair chat access. Shared referee failures now log a source-owned
code and local generation without response bodies or credentials. The original
wire-disconnection cause remains unknown; real pilot acceptance is still limited
to the modes and scenarios actually observed.

The private-controls repair restores native room-local `.admin` and `.mod`
persistence and hot reload, including private-session staff lists. Existing staff
remain in place, and admins inherit moderator permissions. Private preferences
remain separate from the saved public-room defaults. An explicitly requested
service stop can now close a known owned room with players present; uncertain
ownership, unresolved roster ordering and normal empty-room maintenance stay
guarded. A manual `.start` countdown now cancels when the last ready player
withdraws readiness. This does not establish the cause of every unexpected late
advance; report any recurrence with times, before/after readiness and support.

## Fresh independent operator

This frozen pilot targets Ubuntu24.04 Linux x86_64, the pinned system CPython3.12
and systemd. A different distribution or interpreter is not qualified by the
Ubuntu evidence and must pass the protected installer checks. Reserve5GiB disk,
1GiB RAM and512MiB swap for the tester service. Verify the archive,
runtime and public trust digests against the private GitHub release notes. Use the
release archive, rather than GitHub's source-code ZIP. Extract into a fresh folder:

```sh
sudo /usr/bin/python3.12 -I -S install.py --instance first \
  --bundle runtime.zip --sha256 182615d79647a68dfca9f75ed440b77081dee52682a7d1bc737e913fd36bbd96 --live-pilot
```

Set `pilot_release` to the exact protected release path printed by installation.
Download all prepared modes with this explicit command:

```sh
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first catalog-import --origin https://91.107.237.182:9445/ \
  --trust-root bootstrap.json --trust-sha256 2892f9fda822c2aade05fe5b28f354a57623b66555f75f2fb09fd4c0101f7a74 \
  --ca-file ca.crt --ca-sha256 60a0dac90855a1b0006062e9820c2144ba2240fc097cd685a6ca0b6fcb445f39 \
  --mode standard --mode mania4 --mode mania7
```

The owner collects and analyzes maps centrally. Clients receive every prepared
selection row and its verified companions; they do not receive raw player-score
research databases or rebuild the collection pipeline. Prepared files are much
smaller than research databases. Player clients download beatmaps/audio normally.

Configure/sign in locally using the steps below. Account credentials remain local.
Obtain manual hub enrollment approval using docs/NETWORK_OPERATOR_GUIDE.md. Directory
sharing and ratings sharing are separate explicit choices. Read
docs/RATINGS_OPERATOR_GUIDE.md before migration/configuration; M3 enrollment alone
does not upload votes or change map weights.

For this private pilot, the hub origin is `https://91.107.237.182:9444/`.
Its independently pinned CA is the public `ca.crt` included in this kit,
SHA256 `60a0dac90855a1b0006062e9820c2144ba2240fc097cd685a6ca0b6fcb445f39`. The catalog origin is `https://91.107.237.182:9445/`.
Send the installation binding and sharing choices described in the network guide
to the maintainer; the maintainer then supplies your operator UUID and a bound
short-lived invitation privately. Installing or upgrading does not enroll you.

## Local account configuration and authorization (fresh installs)

Use your own osu! account and an OAuth client owned by that account. Register
`http://127.0.0.1:51111/oauth/callback` exactly in its OAuth settings. Know your
numeric osu! user ID. Existing dev7/dev8/dev9 operators keep their authorization and skip
these steps unless the protected status explicitly requires reauthorization.

From the extracted kit folder, run:

```sh
sudo -v
/usr/bin/python3.12 -I -S configure_live.py --instance first --release "$pilot_release" \
  --expected-user-id YOUR_NUMERIC_USER_ID \
  --redirect-uri http://127.0.0.1:51111/oauth/callback
```

The console prompts for the client ID and hides the client secret. Keep them
local; they are sent to the protected command through stdin. Configuration stores
a client profile. `token_phase: absent` and `begin_authorization` are expected
until you complete sign-in.

For a VPS with your browser on another machine, open a separate terminal on your
browser machine and establish this tunnel to your own VPS:

```sh
ssh -N -L 127.0.0.1:51111:127.0.0.1:51111 your-user@your-vps
```

Keep it open. Then, in a private console on the pilot VPS, run:

```sh
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first authorize --operator-mode ssh
```

Open the displayed authorization URL in your browser and sign in as the expected
account. For a browser on the pilot machine, use `--operator-mode local` instead.
The callback listener binds only loopback; no public callback port is needed.
The command does not launch SSH or your browser. Do not share its authorization
URL, callback code or console output containing them.

Successful authorization returns a ready token. Run protected `status`, `support`
and `ownership-review`; local `start_allowed: false` can still be expected because
startup checks happen on explicit start. Resolve any actual ownership recovery
requirement before retrying. Neither configuring nor authorizing starts a room.

## Agreed live test

Follow [SHARED_HUB_TEST.md](SHARED_HUB_TEST.md) during the agreed test window.
Local diagnostics never grant startup admission. A valid token, clean ownership
review and `start_allowed:false` can coexist: start performs fresh checks.

During hub outages, rooms continue using their verified local catalogs. Network
ratings use the last verified snapshot only while it obeys every learned policy
watermark; a newly learned exclusion can require neutral weighting until a new
snapshot is complete. Independent/local weighting remains the default.
Unverified/incomplete snapshots are never substituted. Catalog expiry
blocks acceptance of a fresh download, rather than invalidating already admitted
local catalogs. Recovery and trust replacement are explicit stopped operations.

This is a limited pilot. Automatic software upgrades and owner cutover are not
enabled. Return redacted support, checklist results and errors manually; no chat,
token files or private configuration is automatically sent to the maintainer.

## Optional private Standard test

This profile is for an explicitly arranged private test. Keep the existing
instance and original room settings. It selects only configured Standard room1
without replacing the other room configurations. Both bot and network services
must be disabled/stopped, all owned processes gone and ownership clean before
configuration or clear. It starts no service and performs no authorization.

```sh
printf '%s' '{"alias":"room1"}' | sudo /usr/bin/python3.12 -I -S \
  "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first private-test-configure --private-test-stdin
sudo /usr/bin/python3.12 -I -S \
  "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first private-test-status
```

The alias is required. With alias-only input, osu!'s native MakeRoom generates
the password. You may optionally choose a password through bounded JSON stdin:
8–128 ASCII letters, digits, underscores or hyphens only. Never put it in command
arguments or environment variables. A hidden terminal prompt can feed the JSON:

```sh
sudo -v
/usr/bin/python3.12 -I -c 'import getpass,json; print(json.dumps({"alias":"room1","password":getpass.getpass("Private room password: ")}))' | \
  sudo -n /usr/bin/python3.12 -I -S \
  "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first private-test-configure --private-test-stdin
```

The chosen secret is stored in a separate installation/session-bound mode0600
record. The parent creates the selected private room with its native generated
password first. Only after confirmed positive ownership does it apply the chosen
password with one native room-settings request and wait for the typed completion.
It updates the captured access record only after that acknowledgement. An uncertain
settings reply is not replayed; retain the known room and recovery evidence.
Credentials, URLs, alternate aliases and start flags remain forbidden. The parent
retains actual access in a private mode0600 record; neither these commands,
status/support, child arguments, logs nor public projections may expose it.
Share the confirmed private link/password only with the intended participant.
Use the normal protected start exactly once during the agreed private test window.
The runtime creates only this Standard room. Public Discord status is suppressed
by default while configured staff controls and moderation remain available.

You may explicitly show the private room on its existing Discord status board:

```sh
printf '%s\n' '{"alias":"room1","discord_status":true}' | \
  sudo /usr/bin/python3.12 -I -S \
  "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first private-test-configure --private-test-stdin
```

This is another stopped/disabled-service operation, not a live visibility switch.
The selected room uses its usual configured board and password-protected join
link. The password is never published there; share it separately with the
intended players. `discord_status: false` disables this opt-in. The website feed
and hub directory continue to hide the private room, and the other saved room
panels/configuration are unchanged. If no Discord status channel is configured,
leave this option off. The same bounded JSON may include both `password` and
`discord_status`; neither changes the OAuth account or launches a room.

Native osu! commands such as `.admin add <id>` and `.mod add <id>` now save and
hot-load staff lists with the existing authorization rules. Admins already have
moderator permissions. Private-session grants and other `.set` preferences stay
in the session shadow file; they do not alter the public-room defaults. Use
positive integer IDs in room JSON, preserving the existing staff list when
adding a user. Transport, OAuth and Discord routing settings still require the
existing protected reconfiguration workflow.

After testing, stop cleanly, verify room closure and process absence, disable
both units if necessary, then run the protected entry with action
`private-test-clear`. Clear deletes the profile/access/chosen-secret records and retains isolated
session preferences for review. It neither overwrites the original four-room
configuration nor starts the public rooms. Public activation needs a separate
instruction. Return redacted times, before/active/after support and the commands
and gameplay actually tested; keep the password/access record and tokens private.
