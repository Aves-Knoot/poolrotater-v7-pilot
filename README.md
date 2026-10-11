# PoolRotater 7.0.1.dev1 — one-room live pilot candidate

This unpublished candidate is separate from **7.0.0.dev27**, which only checks
HTTPS database downloads. The new kit can authorize your own account and run one
room after release review and explicit pilot consent. Installing it downloads no
dependencies, authorizes no account, starts no service and creates no room.

All catalog collection and analysis stays on the owner's VPS. This host downloads
prepared signed catalogs; it does not rebuild them or download beatmap/audio
files. Shared ratings/enrollment and automatic fleet updates are later features.

## Before the consenting live test

Confirm Ubuntu 24.04 x86_64, system CPython 3.12.3 plus venv/ensurepip, systemd and
administrator access. Debian and unprivileged Proxmox containers are not qualified
for this protected installation. Reserve 5 GiB disk space; service bounds are
1 GiB RAM, 512 MiB swap, 128 tasks and nice 15. Optional .NET calculations are
not required for this prepared-catalog pilot and are not installed by the kit.

Use your own osu! account and OAuth client. Confirm its expected numeric user ID
and register `http://127.0.0.1:51111/oauth/callback` exactly, or use another explicit
loopback port and the identical URI in configure. Requested scopes are `public`,
`identify`, `chat.read`, `chat.write`, `chat.write_manage`, `multiplayer.write_manage`.
The authorization-code flow must act as the client owner; it does not request
delegation. [Official OAuth scopes](https://osu.ppy.sh/docs/index.html#scopes),
[referee authentication](https://ppy.sh/osu-server-spectator/referee-hub-api.html).

Agree one room, consenting participants and an observation window. Keep your
client secret, authorization URL/callback/code, tokens, private room data and chat
on your machine. None belong in GitHub or a shared report. The service refuses
startup if its conservative owned-room listing finds existing open rooms; use an
account/window with unused inventory. Never use the owner's production OAuth.

## Verify and install

Obtain the kit and independent reviewed archive/runtime digests from the owner.
Compare them with `SHA256SUMS.txt`; a digest in an untrusted download alone does
not establish trust. Extract into a new directory and work there. `release.json`
contains the exact runtime identity and independently reviewed catalog root/CA.

```sh
sha256sum -c SHA256SUMS.txt
sudo /usr/bin/python3.12 -I -S install.py \
  --live-pilot --instance first --bundle runtime.zip \
  --sha256 APPROVED_RUNTIME_SHA256 --preset standard-auto
```

Replace `APPROVED_RUNTIME_SHA256` with the independently confirmed value. The
installer prints the exact `release` path. Set the following variable to that
path, including its digest suffix:

```sh
pilot_release=/opt/poolrotater-v7-pilot/first/releases/v7.0.1-EXACT_DIGEST_PREFIX
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance first check
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance first unit
```

The unit is disabled/stopped. Exactly one preset is allowed; other supported
choices are `standard-fixed`, `mania-4k`, `mania-7k`. For an interrupted installer,
retry the exact same artifact/instance/preset. It preserves the reserved UID and
installation identity; it refuses unexpected existing state or another artifact.

## Download prepared databases and configure locally

The independently confirmed initial trust digests are:

- Bootstrap: `60d2846968d842c9838679ee79536d5963c219e08273eccaca0fc374434b207d`
- HTTPS CA: `20cc74596892f2db0be5e8954ae1f313ca31fce41bf0ea7a6ab59d6fe326025b`

After confirming these values separately with the owner:

```sh
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance first catalog-import \
  --origin https://91.107.237.182:9443/ \
  --trust-root bootstrap.json --trust-sha256 60d2846968d842c9838679ee79536d5963c219e08273eccaca0fc374434b207d \
  --ca-file ca.crt --ca-sha256 20cc74596892f2db0be5e8954ae1f313ca31fce41bf0ea7a6ab59d6fe326025b \
  --mode standard --mode mania4 --mode mania7
sudo -v
/usr/bin/python3.12 -I -S configure_live.py --instance first --release "$pilot_release" \
  --expected-user-id YOUR_NUMERIC_USER_ID --redirect-uri http://127.0.0.1:51111/oauth/callback
```

The private console prompts for your client ID and hides the client secret. The
helper checks the protected installation first and sends credentials only through
stdin. No credential file or environment variable is required. Initial signed
catalog metadata expires 2026-11-10; expired metadata refuses import. The owner
must publish renewed signed metadata, not ask clients to override their clocks.

## Authorize and start only after consent

For a browser on the same machine, authorize with `--operator-mode local`.
For a VPS/browser on another machine, open a separate SSH terminal on your browser
machine first, replacing the SSH destination with your own VPS:

```sh
ssh -N -L 127.0.0.1:51111:127.0.0.1:51111 your-user@your-vps
```

Then on the pilot VPS, keep this private console open:

```sh
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance first authorize --operator-mode ssh
```

Open the displayed URL in your browser and authorize as the expected account.
The URL is displayed immediately while the local callback listener waits. Do not
redirect this console into a shared log. The listener uses loopback; no public
callback port/firewall rule is needed. The flow never opens a browser or SSH itself.

After success, inspect status and explicitly start the dedicated unit:

```sh
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance first status
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance first start
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance first support
```

Start waits for the current service PID's one-room observation. Capability does
not prove live acceptance: `live_ready` remains false until the separate observed
pilot is reviewed. Follow `ONE_ROOM_CHECKLIST.md`. No automatic restart or boot
enablement is part of this first pilot.

## Failure, recovery and removal

Inspect `status` after an interrupted command; never repeat start to resolve an
unknown creation. `cancel` closes a waiting authorization attempt. `recover`
verifies an already persisted candidate without replaying its authorization code.
If the exchange reply is lost and no candidate exists, use an explicitly new
authorization with `authorize --reauthorize --replace-pending`; persistent pacing
may require a conservative wait first. `pacing-recover --pacing-state-sha256
EXACT_STATE_HASH` is an explicit stopped ledger recovery, never a pacing reset.
Use the hash from the private status, not the shareable report. `catalog-recover`
resolves a stopped interrupted complete-set import. None resets unknown ownership.

If room ownership is uncertain, stop and retain private journals for inspection.
If players are present, let the room become empty before graceful removal. Do
not clear state, forcibly kill workers, replace a DB in use or reuse fixture data.

```sh
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance first stop
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance first support
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance first remove
```

`remove` removes only the stopped/disabled reviewed unit and retains private
recovery data, catalogs, code and service UID. Destructive purge is a separate
deliberate operation. It cannot erase uncertainty to make startup succeed.

## Upgrade from dev27 or another installation

Dev27 has no installed live service/authorization. Keep its downloaded rehearsal
directory separate and install this release into a fresh instance. Do not copy its
fixture profiles, receipts or state into the new host. The owner supplies catalog
trust independently and this release imports verified catalogs itself.

An existing v6 bot, fixture host or previous candidate is not an in-place upgrade.
Stop/remove that dedicated pilot unit only after known clean room closure, then
use a new unused instance for the reviewed live release. Keep recovery data and
authorize locally afresh. Production v6 replacement/migration is outside this kit.
