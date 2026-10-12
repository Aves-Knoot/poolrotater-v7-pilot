# PoolRotater 7.0.1.dev6 — full-catalog single-room pilot

Download the installer asset, not GitHub's source ZIP. Verify its published archive
SHA256 before extraction. The kit includes the pinned runtime and public catalog
trust, not databases or operator secrets. Installing/upgrading starts no service
and creates no room. One standard-auto room is the default; other presets are
explicit choices, and this pilot rejects multiple rooms.

## Existing dev5 tester

Follow REPAIR_UPGRADE.md section1 and2A. Preserve the same first instance, account
configuration, authorization, catalogs and committed recovery. Do not reinstall,
reauthorize or repeat ownership-clear. Then follow SINGLE_ROOM_TEST.md for full
catalog verification and the separately scheduled one-room test.

## Complete prepared databases

The existing owner origin https://91.107.237.182:9443/ already serves complete,
sanitized runtime-selection projections of the isolated catalog snapshots:

| Mode | Map rows | Analysis companions |
| --- | ---: | --- |
| Standard | 105510 | 105510 stream, 40846 farm, 105137 DT |
| Mania 4K | 19335 | 19335 structural, 18953 overrepresentation, 16327 farm |
| Mania 7K | 4348 | 4348 structural, 3036 overrepresentation, 1778 farm |

Earlier reports showing 400/200 eligible maps were capped selection smoke checks,
not truncated databases. No new trust or collection is needed to obtain these
complete exports. These counts describe the recorded snapshots, not a claim that
the catalog includes subsequently collected maps. Collection/analysis stays on
the owner machine; clients only download and activate verified prepared databases.
Research/private player tables, beatmap/audio files and credentials are not shipped.
Known missing-audio beatmap1549496 is excluded.

The independent trust digests remain:
bootstrap60d2846968d842c9838679ee79536d5963c219e08273eccaca0fc374434b207d;
CA20cc74596892f2db0be5e8954ae1f313ca31fce41bf0ea7a6ab59d6fe326025b.
Fixed signed metadata expires2026-11-10T19:04:09.224574+00:00. After expiry, stop
and obtain a reviewed publication; do not disable expiry/signature checks.

## Fresh consenting operator only

Supported reference: Ubuntu24.04 x86_64, system CPython3.12.3 with venv/ensurepip,
systemd and administrator access, at least5GiB free disk. Privileged LXC was
externally tested; Debian/unprivileged LXC are not qualified by this release.
Service limits:1GiB RAM,512MiB swap,128tasks,nice15. Optional .NET is not required.

After extracting and checking the independent archive digest:

```sh
sudo /usr/bin/python3.12 -I -S install.py --live-pilot --instance first \
  --bundle runtime.zip --sha256 e4db2fbe600ccc14ad323ba2d6ded80840718744613ff5de876a9765890219ab
```

Use the exact release path printed by the installer as pilot_release. This creates
one standard-auto room configuration with a disabled/stopped service. Existing
instances must use the upgrade guide, not this fresh-install command.

Obtain full catalogs using SINGLE_ROOM_TEST.md. For a fresh operator only, create
your own OAuth client and register http://127.0.0.1:51111/oauth/callback exactly.
Use your own numeric account ID. Configure locally:

```sh
sudo -v
/usr/bin/python3.12 -I -S configure_live.py --instance first \
  --release "$pilot_release" --expected-user-id YOUR_NUMERIC_USER_ID \
  --redirect-uri http://127.0.0.1:51111/oauth/callback
```

Client ID/secret are entered locally; keep them private. The requested scopes are
public,identify,chat.read,chat.write,chat.write_manage,multiplayer.write_manage.
Do not use the maintainer's account. For SSH authorization, establish a local
forward before opening the browser URL:

```sh
ssh -N -L 127.0.0.1:51111:127.0.0.1:51111 YOUR_SERVER
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first authorize --operator-mode ssh
```

The SSH command runs on the browser machine in a separate terminal. Authorize
runs on the server; the displayed URL is private. A short refused-forward message
before the callback listener binds does not establish authorization failure;
wait for the command's result. Do not repeat an uncertain token exchange.

Authorization and local status do not grant a startup permit. A clean review may
still report start_allowed:false; startup performs fresh identity/client/scopes
and ownership checks. Agree the test window and use SINGLE_ROOM_TEST.md.
M3/M4 enrollment/shared ratings and automatic fleet updates are not included.
