# Independent recovery setup

This installs a separate recovery timer for an existing enrolled installation.
It preserves OAuth, catalogs, titles, settings, statistics, ratings and update
state. It does not reinstall the bot or clear room ownership.

The setup grants the hub maintainer three bounded actions:

- Collect diagnostics and upload logs under your existing reporting choices.
- Continue the exact signed update already admitted on your machine.
- Start an exited, failed service after proving the installed release is intact,
  there is no unfinished update and all configured rooms are locally confirmed
  closed. Fresh provider checks still happen during normal native startup.

There is no arbitrary shell, file download, supplied code, remote OAuth access,
running-room restart or unrestricted start/stop endpoint. Recovery requests are
bound to your enrolled host, installation, generation and exact observed state;
they expire and have durable receipts. An interrupted request is reported as
uncertain and is not automatically dispatched a second time.

The separate timer keeps running if the bot or network agent fails. It uses a
separately pinned release, nice17 and the shared50%-of-one-core transfer slice.
Three attempts per failed update/state are allowed, at least60seconds apart.
Only narrowly classified transient interrupted updates qualify for automatic
continuation. Other failures report back for maintainer review.

## One-time setup

Use the hash-checked `poolrotater-install-recovery-v7.1.14.py` asset from the
maintainer's recovery release with system Python:

```
sudo /usr/bin/python3.12 -I -S -B ./poolrotater-install-recovery-v7.1.14.py --instance two
```

The published instructions include the exact download URL and SHA256. Do not
use an unpinned source ZIP. The helper downloads the pinned runtime, stages only
recovery code, and starts only the independent recovery timer. A preexisting
unfinished controller transition remains intact. The maintainer can then obtain
redacted failure evidence through the hub without another manual log bundle.

Keep the existing instance; do not repeat OAuth, ownership-clear or room start.
The helper's first recovery tick may continue an already-admitted transient
update if all native checks pass. Otherwise the bot stays stopped for review.
Return just its final JSON if the maintainer does not see the recovery heartbeat.

## Local control

An explicit native bot stop or updater stop disables recovery mutations while
retaining diagnostics. To explicitly pause or resume assisted recovery:

```
sudo /usr/bin/python3.12 -I -S -B /opt/poolrotater-v7-pilot/two/recovery-bootstrap.py --instance two --action pause
sudo /usr/bin/python3.12 -I -S -B /opt/poolrotater-v7-pilot/two/recovery-bootstrap.py --instance two --action resume
```

To stop the entire independent channel, including diagnostics:

```
sudo systemctl disable --now poolrotater-v7-recovery-two.timer
```

Changing reporting consent, revoking hub enrollment or turning off automatic
controller installation continues to limit its existing authority. Unknown room
creation, an unclosed room, changed configuration, broken credentials, exhausted
retries or an unqualified rollback can still require review. The recovery system
preserves that evidence rather than fabricating a clean state.

## Maintainer commands

The protected hub bootstrap exposes local `admin recovery-inspect` and
`admin recovery-request` commands. Inputs are private stdin JSON. Requests accept
only `inspect`, `complete-update` and `start-failed-service`; starting/completing
requires the exact fresh observation digest. There is no public administrative
HTTP route. Saved recovery databases are restored only as historical evidence,
never as executable requests in a new hub credential epoch.

No actual provider or gameplay acceptance is implied by installing this channel.

## Published bootstrap

Run this once on the existing operator host. It installs the separate recovery
channel and preserves the current rooms' installation and unfinished update.
It does not require OAuth, reinstall or another ownership-clear. It grants the
bounded maintainer actions described above, under existing sharing choices.

```sh
curl -fL https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/download/v7.1.14/poolrotater-install-recovery-v7.1.14.py -o poolrotater-install-recovery-v7.1.14.py &&
echo '2e5510d0ef2e6ed4db9e1392e68894522b6c4c216fe825f88e273262eb64ec4d  poolrotater-install-recovery-v7.1.14.py' | sha256sum -c - &&
sudo /usr/bin/python3.12 -I -S -B ./poolrotater-install-recovery-v7.1.14.py --instance two
```

The sudo command runs only if the download and SHA256 verification succeed.
The maintainer can then retrieve diagnostics and request qualified recovery
through the hub. Retain instance state. If the independent channel cannot
report, return its final JSON rather than repeating room creation.

Runtime SHA256: `52bdb351458b0f008d24189ee8d302d1ae0c3f25f622c1586b82def675f1a17b`

Bootstrap SHA256: `2e5510d0ef2e6ed4db9e1392e68894522b6c4c216fe825f88e273262eb64ec4d`
