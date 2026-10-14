# PoolRotater integrated owner-hub pilot

This kit adds optional host registration, shared good/bad votes and full prepared
catalog downloads to the existing bot. Fresh installation creates **one Standard
automatic-difficulty room configuration**. Installation starts no room or service
and performs no account authorization. Existing installations keep their settings,
authorization, catalogs and resolved ownership history during the stopped upgrade.

Existing dev7 or dev8 testers: follow [OWNER_HUB_UPGRADE.md](OWNER_HUB_UPGRADE.md). Do not
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

## Fresh independent operator

This frozen pilot targets Ubuntu24.04 Linux x86_64, the pinned system CPython3.12
and systemd. A different distribution or interpreter is not qualified by the
Ubuntu evidence and must pass the protected installer checks. Reserve5GiB disk,
1GiB RAM and512MiB swap for the tester service. Verify the archive,
runtime and public trust digests against the private GitHub release notes. Use the
release archive, rather than GitHub's source-code ZIP. Extract into a fresh folder:

```sh
sudo /usr/bin/python3.12 -I -S install.py --instance first \
  --bundle runtime.zip --sha256 0b8f12a7b8e2dfc9a708f8fc331e08583bcabccaa1a1132adb971b7319b81481 --live-pilot
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
numeric osu! user ID. Existing dev7/dev8 operators keep their authorization and skip
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
