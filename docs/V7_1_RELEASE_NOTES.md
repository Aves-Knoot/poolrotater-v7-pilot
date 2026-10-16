# PoolRotater v7.1.0

Technically qualified for manual installation and stopped migration on the
tested platform. Hub/website activation and each operator’s choices are separate
steps. Production has not been changed by this release preparation.

v7.1 adds shared player statistics and private diagnostic reporting to the
retained room engine. Existing room titles, topology, effective settings,
OAuth/Discord configuration, catalogs, ratings, history, enrollment and updater
choices stay with the installation.

## Changes

- Shared statistics capture completed participation, sessions and existing PP
  history, with replay protection, cross-host overlap handling and hidden-player
  privacy. `.stats` reads a local cache and requests background refresh; command
  handlers make no new HTTP requests.
- Independent statistics sharing, statistics consumption, operational logging
  and public chat/command logging choices all **default off**. Enrollment does
  not enable them. Public chat needs its own choice in addition to operational
  logging; private and unknown-visibility room content is excluded.
- Private administrator diagnostics include ordinary information, lifecycle
  records, warnings and tracebacks. Credentials are scrubbed at source, during
  upload and at the hub. Durable bounded queues survive outages; quota/expiry
  losses are visible. Hub revocation and separate consent remain enforced.
- `.discord` works when the configured invite is missing or blank, using
  `https://discord.gg/KmNhHbDEbv`. Nonempty operator overrides stay intact;
  intentionally blank automatic reminders remain disabled. Optional AI/PP
  commands give useful unavailable replies. Help output, malformed inputs and
  privacy-store failures no longer produce the audited command crashes.
- Ratings retain separate contribution/consumption choices and existing local
  selection behavior. Backup/restore includes new reporting stores, and restore
  disables new sharing until reviewed again.

## Installing or upgrading

The qualified reference platform is **Ubuntu 24.04 x86_64, system CPython 3.12
with venv, and systemd**. Keep at least **5 GiB free disk reserve**. The kit
includes 27 pinned wheels; it does not install OS packages or resolve online
Python dependencies. Optional official PP projection still needs a separately
installed .NET 8 runtime. Other systems need their own qualification.

The first upgrade from the reviewed v7.0.6/v7.0.7/v7.0.8 artifacts is a
**manual stopped additive migration**. It cannot be sent as an ordinary signed
worker update. Keep the same instance and state; do not reinstall or repeat
OAuth setup. Safely empty and stop the affected rooms, follow the bundled
upgrade guide, and retain migration receipts and backups. The updater remains
included for subsequent compatible releases.

Fresh installations default to one Standard automatic room; explicitly chosen
configurations can use up to four rooms. They configure their own OAuth locally.
Catalogs are the full prepared selection databases: **105,502 Standard,
19,334 Mania 4K and 4,347 Mania 7K rows**, including their selection companions.
Research databases are not downloaded or rebuilt on client hosts.

Central reporting requires the compatible hub/routes to be activated first.
Historical statistics are **not automatically uploaded**: the separate history
helper requires an explicit review of public intervals and an independent
sharing choice. Unknown historical privacy is never inferred from room names.
Owner/community activation and history import are conditional steps in the
bundled guides, not side effects of downloading this archive.

## Validation and artifact identity

The 2,995-test source sweep initially exposed 50 failure/error diagnostics across
47 methods. Every diagnostic has a passing relevant rerun; two root-only source
tests remain skipped, and the entire suite was not repeated after localized
fixes. Full-catalog final2 donor acceptance passed fresh installation, stopped
owner/operator migration, retained configuration/history, native workers,
Standard/4K/7K matches, HTTPS reporting replay, ratings consumption, secret-canary
checks and clean shutdown. Provider fixtures and credentials were invented;
these checks did not create real osu! rooms or establish an overnight live soak.
See `V7_1_VALIDATION.md` for exact evidence, benchmarks and limits.

Runtime SHA-256:

`8c0971747c77a22a992749a6171363d60eb4a3bdc5f87460c55f88d881a21d35`

Installer ZIP SHA-256: see the separately published `RELEASE_SHA256SUMS.txt` asset.
The checksum is external because an archive cannot contain its own digest.

Use the installer ZIP attached to the qualified release, rather than GitHub's
automatic source archive. Follow its stopped migration and activation sequence;
publication does not opt a host into sharing or restart any room.
