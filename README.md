# PoolRotater dev27 — download the prepared databases

This pilot installs an ordinary-user Python environment and downloads the prepared
Standard, Mania 4K and Mania 7K catalogs **from the maintainer's VPS over HTTPS**.
The ZIP contains code, seven pinned dependency wheels and public trust material;
it contains no map database, beatmap or audio files. Collection and analysis stay
on the maintainer's machine. The downloaded catalogs remain on your machine.

This is a separate download pilot. It does not authorize an osu! account, create
rooms, install a client service or submit votes/chat. No account credentials or
OAuth code are needed. It does not upgrade the previous offline rehearsal.

## Requirements

Ubuntu 24.04, x86-64, system Python 3.12 with venv/ensurepip, an ordinary non-root
user and at least 5 GiB free disk. HTTPS access to the IPv4 address and port in
release.json is required. Dependencies install from the ZIP without contacting
pip or osu!. All pilot subprocesses run at nice 15 or higher.

The bundled CA and signed bootstrap describe this fixed pilot publication only.
The independently confirmed release manifest digest pins both. Do not disable
TLS verification or change the origin/trust to work around an error. Metadata
expires on the date shown by support and release.json; a later publication needs
explicit fresh release trust. This is not an automatic catalog refresh service.

## Install and test

Download catalog-download-pilot.zip and SHA256SUMS.txt from the private GitHub
prerelease. Confirm the archive and release.json SHA-256 with the maintainer,
then replace MANIFEST_SHA256 below. Use a fresh directory separate from the
previous rehearsal and the unpacked release.

```sh
sha256sum catalog-download-pilot.zip
mkdir -m 700 catalog-release
unzip -q catalog-download-pilot.zip -d catalog-release
mkdir -p "$HOME/poolrotater-pilots"
python3.12 -I -B -S catalog-release/catalog_pilot.py verify --manifest-sha256 MANIFEST_SHA256
python3.12 -I -B -S catalog-release/catalog_pilot.py preflight --manifest-sha256 MANIFEST_SHA256 --destination "$HOME/poolrotater-pilots/db-download"
python3.12 -I -B -S catalog-release/catalog_pilot.py install --manifest-sha256 MANIFEST_SHA256 --destination "$HOME/poolrotater-pilots/db-download"
python3.12 -I -B -S catalog-release/catalog_pilot.py install --manifest-sha256 MANIFEST_SHA256 --destination "$HOME/poolrotater-pilots/db-download"
python3.12 -I -B -S catalog-release/catalog_pilot.py cache-check --manifest-sha256 MANIFEST_SHA256 --destination "$HOME/poolrotater-pilots/db-download"
python3.12 -I -B -S catalog-release/catalog_pilot.py support --manifest-sha256 MANIFEST_SHA256 --destination "$HOME/poolrotater-pilots/db-download"
```

The first install verifies dependencies, downloads all three catalogs, checks
signatures/expiry/hashes/schema/analysis companions and selects three maps per
mode. It excludes map 1549496. Candidate counts are capped, not catalog totals.
Nothing is activated for live rooms. Initial network failure leaves signed cache
and partial bytes for retrying **the same install command**. An interrupted Python
environment installation is refused; use a fresh destination in that case.

Repeating a completed install checks existing files and makes no network requests.
cache-check explicitly contacts the server again and uses the cached signed target
bytes without downloading unchanged payloads. It removes its temporary duplicate
catalogs. support/check only verify existing local files and work during an outage.

Send the **second install**, **cache-check** and **support** JSON manually to the
maintainer, plus OS/Python/virtualization, free disk and any confusing steps or
errors. Nothing is automatically uploaded. Reports omit local paths and credentials.
Removal: delete only your explicitly chosen db-download installation and
catalog-release directory when finished. No service or OS package was installed.
