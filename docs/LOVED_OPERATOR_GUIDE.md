# Fresh Standard Loved host — v7.2.8

Use this guide for a NEW hosting installation. Existing hosts retain their current
instances, rooms, OAuth, trust, configuration and state. Downloading this release
never adds a Loved room or renames an existing room automatically.

## Platform and room

The current protected installer enforces Ubuntu24.04 x86_64, system Python3.12 and
systemd (including the qualified systemd LXC environment). ARM/other operating
systems are not newly supported by this release. Inspect the machine before setup.
A previously installed host must use its retained instance and supported upgrade
path; do not install another controller against the same hosting account.

New preset: `standard-loved`. It creates one native osu!standard Loved room with
fixed4.00–6.00★ difficulty and player difficulty votes. Default title template:
`AutoHost Loved|Type .help|`. The engine appends difficulty/range/version text.
Other fresh titles match the owner's Standard/4K/7K/Catch title templates. Explicit
saved titles, including locally edited native room titles, survive upgrades.

This preset uses the existing Loved reader and commands. Only native Standard
Loved maps are admitted; Mania/Catch Loved charts and unqualified DT companions
are not advertised. Loved uses neutral structural/farm weights, plus the existing
feedback/selection behavior. No fictitious farm or DT evidence is generated.

## Install and download the complete catalog

Download the **installer ZIP asset**, verify its SHA256 against the release
checksum file, extract into a new folder and enter `kit`. Verify internal files:

```sh
sha256sum -c SHA256SUMS.txt
sudo /usr/bin/python3.12 -I -S -B install.py --instance first \
  --bundle runtime.zip --sha256 f4691af18cd06165ff7e17d9e4eadd4ccefe13096e060be00cd9f042945b8a8f --live-pilot --preset standard-loved
```

Keep the root-owned release directory printed by installation as `pilot_release`.
Use the same instance name throughout; the examples use `first`. Define the
administrator function in the shell where subsequent commands run:

```sh
pr() { sudo /usr/bin/python3.12 -I -S -B "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance first "$@"; }
```

Neither installation nor authorization starts rooms. Download/import the complete
signed Loved selection database from the owner VPS with the kit's public pins:

```sh
pr catalog-import --origin https://91.107.237.182:9445/ \
  --ca-file "$PWD/ca.crt" \
  --ca-sha256 60a0dac90855a1b0006062e9820c2144ba2240fc097cd685a6ca0b6fcb445f39 \
  --trust-root "$PWD/bootstrap.json" \
  --trust-sha256 2892f9fda822c2aade05fe5b28f354a57623b66555f75f2fb09fd4c0101f7a74 \
  --mode loved
```

This is a full catalog, not a rehearsal sample. Initial export:3,845 qualified
Loved difficulties from1,287sets;8zero/unqualified-star rows are omitted from the
3,853-row collection. Download-disabled rows remain marked and cannot be selected.
The full database is small because it contains map-selection fields, not research
scores, player tables, audio or `.osu` files. Collection/export is central; this
command performs no local collection, analysis or osu! account operation.

## Authorize locally

Follow LIVE_AUTHORIZATION.md. Create/use your own osu! OAuth client with callback
`http://127.0.0.1:51111/oauth/callback`; enter credentials only in the local helper:

```sh
python3.12 -I -S -B configure_live.py --instance first --release "$pilot_release" \
  --expected-user-id YOUR_NUMERIC_HOSTING_USER_ID \
  --redirect-uri http://127.0.0.1:51111/oauth/callback
```

On the browser computer, keep the documented SSH tunnel open. Run `pr authorize
--operator-mode ssh` on the host, then open its locally printed URL when the
listener is ready. Tokens and client secrets stay local. Never send them, OAuth
codes/URLs, private configuration, or token files to the maintainer.

## Bound hub invitation — the one maintainer handoff

Before starting network services, return these details privately:

- Hosting username and numeric osu! ID; public operator label.
- OS/architecture/Python/systemd summary, chosen instance and installed runtime SHA.
- `installation_id` and `generation`, read with the exact command below.
- Desired public directory listing, sharing/reporting/update choices and boot intent.

```sh
sudo /usr/bin/python3.12 -I -S -B -c 'import json; v=json.load(open("/var/lib/poolrotater-v7-pilot/first/.poolrotater-host.json")); print(json.dumps({k:v[k] for k in ("installation_id","generation")}))'
```

The maintainer issues an expiring private poolrotater.com download link with the
bound invitation, hub profile/public trust and exact enrollment instructions.
These identifiers grant no room/account access. An invitation cannot be generated
before the account/installation binding is known. Do not reuse another host's
invitation or initialize a separate hub. If enrollment is interrupted, inspect
network status and resume the same operation rather than deleting state.

Return `host_id` and redacted `network-support` after enrollment. The maintainer
checks the binding and grants ratings contribution. Public-directory enrollment
alone does not grant that permission. Follow the private package to finish local
sharing and start the network agent; sharing and automatic updates default on,
with explicit opt-outs preserved. Do not change the advertised update trust pins.

## Start and return useful feedback

Once local authorization, catalogs and hub enrollment/grants are ready:

```sh
pr unit
pr check
pr ownership-review
pr support
pr start
pr status
pr network-support
pr ratings-support
pr update-status
```

Startup runs fresh identity/client/scopes/ownership checks. Local status may show
`start_allowed:false` because startup admission has not yet run. That field alone
is not an authorization failure; read its reason. Start once and retain state if
the outcome is uncertain. Never erase journals or blindly retry room creation.

Return redacted support/network/ratings/update output, the public room link,
startup time and any errors. Confirm the room appears on poolrotater.com under
the chosen operator. Loved is projected as Standard fixed difficulty for older
hub/directory compatibility; its title and selection style identify Loved.

Observe joins, `.help`, `.discord`, `.stats`, `.good`/`.bad`, skip, ready/AFK,
countdown and several completed maps. Healthy rooms can remain running. During
planned stopping, use `pr stop` and confirm clean ownership/reaped workers. Choose
boot enablement explicitly; installation does not silently enable the bot at boot.
Controller updates wait until all of this host's configured rooms are empty.

Actual public gameplay is the operator's acceptance step. Offline invented-provider
qualification does not claim that new host has already played a real match.
