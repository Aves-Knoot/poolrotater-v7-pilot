# PoolRotater v7 — private pilot installation rehearsal

This snapshot prepares an independent operator to rehearse installation. It is
**offline only**: it does not authorize an osu! account, create rooms, install or
enable systemd units, synchronize ratings, enroll with a hub or fetch updates.
The accepted runtime is dev25. Real provider dispatch remains disabled in code.
See [LIVE_ACTIVATION.md](LIVE_ACTIVATION.md) for the finite next step.

Do not use the legacy `run_*.py` launchers in this source snapshot. They are
included to preserve the accepted runtime source closure, not as supported pilot
commands. Use the verified rehearsal kit below. The source and catalog publication
policy is pending; [SOURCE_POLICY.md](SOURCE_POLICY.md) is not a source license.

## Supported first pilot machine

- Ubuntu **24.04**, x86-64, system CPython **3.12** with `venv`/`ensurepip`.
- An ordinary non-root user with an owned writable parent directory.
- At least **5 GiB free disk** before installation/catalog checks. The rehearsal
  has no asserted live-room CPU/RAM minimum. Development qualification budgets
  and synthetic load measurements do not establish live capacity.
- No osu! client ID, secret, access token or authorization code is needed for
  this rehearsal. Do not send account credentials to the maintainer.

Debian, other Ubuntu versions, ARM and other Python versions are not qualified
for this pilot kit. No .NET installation is required for the offline selection
rehearsal; the protected full runtime still needs its separately qualified
external prerequisites for optional PP calculations.

If prerequisites are absent, an administrator can install the OS's Python 3.12
venv package. The rehearsal itself never installs OS packages or invokes sudo.
An OS upgrade that changes the Python executable requires requalification before
using the protected runtime archive; the offline wheel rehearsal is separate.

## Verify and install

The maintainer supplies `pilot-rehearsal.zip`, its SHA-256 and the independently
confirmed `release.json` SHA-256. Obtain those identities through a trusted
maintainer channel, not solely from the candidate download. This is manual pilot
trust, not deployed release signing. Verify the archive **before executing its
Python code**. The internal inventory/digests alone do not prove its publisher.

Example commands below use placeholders. Replace the two digest placeholders
with the actual maintainer values; they are not environment secrets. Run as your
ordinary user. Keep the unpacked release and installation directories separate.

```sh
sha256sum pilot-rehearsal.zip
# Compare with the independently confirmed archive SHA-256 first.
mkdir -m 700 release
unzip -q pilot-rehearsal.zip -d release
mkdir -p "$HOME/poolrotater-pilot"
python3.12 -I -B -S release/pilot.py verify --manifest-sha256 MANIFEST_SHA256
python3.12 -I -B -S release/pilot.py preflight --manifest-sha256 MANIFEST_SHA256 --destination "$HOME/poolrotater-pilot/rehearsal"
python3.12 -I -B -S release/pilot.py install --manifest-sha256 MANIFEST_SHA256 --destination "$HOME/poolrotater-pilot/rehearsal"
python3.12 -I -B -S release/pilot.py catalog-check --manifest-sha256 MANIFEST_SHA256 --destination "$HOME/poolrotater-pilot/rehearsal"
python3.12 -I -B -S release/pilot.py support --manifest-sha256 MANIFEST_SHA256 --destination "$HOME/poolrotater-pilot/rehearsal"
```

The installer uses the seven pinned wheels bundled in the kit, with hashes,
`--no-index` and no dependency downloads. It clears inherited credential/proxy/
Python/pip environment input for its child commands. Work runs at nice 15 or
higher. Installation/state and child temporary files stay in the explicit new
rehearsal directory, with private permissions. The existing offline simulator
also uses its ordinary same-UID `/tmp/poolrotater-v7-locks-UID` lease namespace;
it does not use v6/production locks. No service, authenticated account ownership
or central trust is created.

Initial installation creates a venv at its final path and a host with invented
catalogs. It checks dependencies and three selected rounds in all four presets:
Standard automatic, Standard fixed, Mania 4K and Mania 7K. Running `install`
again verifies the existing receipt/environment/host and makes no configuration
or catalog changes. Changed releases, a foreign/nonempty destination, missing
receipts, symlinked managed paths and incomplete installations refuse adoption.

An interrupted installation is retained as `installing`. Inspect your own
rehearsal, then use another fresh destination or remove only that incomplete
directory before trying again. There is no automatic repair, upgrade or deletion
of existing state. Do not edit receipts or delete ownership journals to bypass
refusals. This prototype is not an administrator-proof immutable installation;
the operator owns its private rehearsal environment.

`catalog-check` extracts the three independently digest-bound M2 exports into a
private temporary directory. It validates exact schemas, hashes, analysis and
companion bindings, checks real selection and the missing-audio exclusion, then
removes that temporary data. It preserves your host catalogs. The exports contain
prepared map/analysis data, not beatmap/audio files, player votes or chat logs.
The handoff is **not** an initial production TUF root or managed catalog store.

`support` performs the same checks as `check` and prints a small allowlisted JSON
result. Share that JSON and the `catalog-check` JSON with the maintainer if desired.
They include version/digest/capability summaries, never account identities, paths,
environment dumps, credentials, player names or raw chat. Nothing is uploaded
automatically. Errors report fixed codes or exception types without child output.
Checks/reruns verify the initial fake-round evidence; they do not play new rounds
or write additional synthetic ratings into the existing rehearsal host.

## Removal and moving to a real pilot

Because no service or OS user is installed, removal consists of deleting **your
explicit rehearsal directory** after retaining any reports you want. The release
directory is separate. No automated uninstall command is supplied, and this
procedure must not be applied to a live or production installation.

This receipt, host identity and fixture catalog data cannot become live authority.
A later approved live pilot needs a fresh protected live installation and the
operator's own account/client authorization; it must not adopt this rehearsal.

## Maintainer publication route

Use the prepared fresh source snapshot to initialize a **new** GitHub repository
after review and the owner's source-policy/destination decisions. Do not push
the original development repository/history or internal validation directory.
The source snapshot intentionally omits catalogs, wheels, release ZIPs, handoffs,
operational configuration, logs, donor access files and raw chat. Supply the kit
as a separately reviewed release asset after approval, with its exact hashes.

There is no auto-updater, distributed hub, signing-key custody or public release
approval in this preparation. M3 adds enrollment/status/directory; shared ratings
are M4. [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) indexes exact dependency
license/notice texts without granting the project's source license.
