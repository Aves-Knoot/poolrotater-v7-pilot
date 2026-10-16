# Owner v7.1 activation queue

Authorized by the owner: prepare and apply the qualified v7.1 update once lobbies
are empty. No forced occupied-room shutdown and no remote host consent change.

## Installed one-time queue

Protected package:
`/opt/poolrotater-v7-cutover-tools/v71-activation-20261016`

Exact before runtime:
`fd4f082e7b746dedc6d4e93d40eab6b1bc86f546daf50a9a6202d8cb01c23dcb`

Exact after runtime:
`8c0971747c77a22a992749a6171363d60eb4a3bdc5f87460c55f88d881a21d35`

`migration-files.json` pins the root-owned helper, unchanged published installer
and exact runtime. State is `migration.json`; evidence stays under `evidence/`.
The timer is `poolrotater-v71-owner-migration.timer`; it invokes one bounded root
oneshot roughly every20seconds, nice15 with idle I/O,1.5GiB RAM/512MiB swap caps.
No source tree, production credential or mutable module is loaded from the
development checkout. Existing registered v7.0.7 modules supply the normal
roster, ownership and maintenance-gate behavior.

```bash
sudo /usr/bin/python3.12 -I -S -B /opt/poolrotater-v7-cutover-tools/v71-activation-20261016/migration.py status
systemctl status poolrotater-v71-owner-migration.timer poolrotater-v71-owner-migration.service
```

The exact three configured rooms are room2 fixedStandard, room3 Mania7K and
room4 Mania4K. AutomaticStandard/room1 remains intentionally disabled. Every
room must have current independent broker/worker evidence of zero players, an
IDLE healthy worker, known roster and positive ownership. Both the queue's
continuous empty observation and native empty duration must reach30seconds.

The native supervisor then temporarily password-protects admission, pauses the
empty rooms and acknowledges the matching nonce. At least15seconds of drain
follows acknowledgement. Late joins, replaced workers, stale rosters or a
changed journal cancel admission. Artifact/registration/configuration/oldprocess
identity are rechecked; fresh observations after hashing are required before stop.
Uncertain private-gate operations hold for review. There is no forced match abort.

## Commit and recovery

Only after that barrier: temporarily pause the analytics timer/read-only ledger
reader, stop the independent network agent and normally close the owner rooms.
Require clean stopped ownership and no dedicated-UID processes. Back up the
complete host and code prefix under
`/var/backups/poolrotater-v71-owner/<operation-UUID>/owner-and-prefix.tar`, verify
the archive against the stopped source, then run the unchanged qualified
installer's exact7.0.7→7.1 stopped migration.

Before restarting, configuration, credential and catalog file fingerprints must
match their stopped pre-upgrade values. The additive migration separately keeps
a verified native-ledger backup and durable recovery transaction. The new
protected administrative entry starts the same installation and restores prior
network/enablement intent. The analytics timer is restored. Saved defaults,
titles, permissions, OAuth, Discord, histories and catalogs remain local. New
sharing/consumption/log/chat choices remain off unless independently configured.

A partial commit/startup failure is `held`; no automatic retry, blind rollback,
journal deletion or secondroomcreation follows. Read the private installer
outputs, exact upgrade transition and ownership status before choosing supported
recovery. Merely restarting the timer cannot replay a committing transaction.
A held or completed job disables its own timer.

Do not edit the signed feed/storage policies or publish a fake ordinary update
to bypass this first migration. Compatible worker updates can still roll
individually after7.1; controller/storage changes retain their proper maintenance.
This queue belongs only to the owner's exact installation, not other operators.

## Evidence

Twelve new admission-state-machine tests passed: occupied/stale/missing rooms,
continuous emptiness, exact worker identity, privateack/drain, gate uncertainty,
fresh post-hash barrier, last-moment join, journal change, configuration change,
interrupted commit and no automatic failed-commit replay. Existing exact root
donor7→1 installer/storage proof remains unchanged. Systemd unit verification
and several live waiting ticks passed without altering owner PID1932467.
Hub/website activation receipts are in this package's private evidence folder.

Actual eventual deployment status must be read from the protected queue and
registered release; creation of this document does not mean the owner is on7.1.
