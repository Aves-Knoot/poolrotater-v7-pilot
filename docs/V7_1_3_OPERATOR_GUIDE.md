# v7.1.3 room-worker update

This is a compatible worker update for hosts whose controller is already
v7.1.2. The controller and its account connection stay on v7.1.2; individual
room workers and their title/version labels advance to v7.1.3.

## Automatic updates

Running enrolled hosts on the pilot channel with automatic download/install
enabled discover the signed release on their existing update schedule (normally
about 30 minutes). Each room waits for the existing empty, idle, fresh-observation
and admission/drain checks. Occupied siblings continue on their current workers.
A late player arrival cancels that room's handoff. No players are kicked to make
an update window, and no simultaneous all-rooms-empty window is required for this
worker release.

Hosts still on v7.1.0 or v7.1.1 first follow the signed controller upgrade path to
v7.1.2, which requires its coordinated empty-room window. Existing update pauses,
channel choices and opt-outs remain effective. Stopped/failed hosts are not
started by publication or the update agent; resolve ownership before a local
restart. v7.0 hosts still require the documented one-time v7.1 migration.

## Manual worker staging

Use the current installed protected admin entry as root for the existing instance.
`$E` below represents that complete instance-specific admin command. Download the
`poolrotater-7.1.3-worker.zip` asset from the v7.1.3 GitHub release and verify its
SHA-256 against the release notes. This ZIP is the worker runtime archive itself,
not an installer kit; pass it intact rather than unpacking it into an installation.

```sh
$E worker-stage --bundle /path/to/poolrotater-7.1.3-worker.zip --sha256 <published-runtime-sha256>
$E worker-queue --sha256 <published-runtime-sha256>
$E worker-status
```

Do not submit a second manual worker job while an automatic one is pending.
Omitting `--room` queues all configured rooms, each admitted independently.
No reinstall, OAuth sign-in, catalog import, tuning change or controller restart
is needed. For a fresh installation, use the v7.1.2 installer and then this worker
update. Do not use this worker archive as a controller upgrade.

## Observing results

Use `worker-status` and `update-status` for queue/receipt state. Room titles and
worker status can show v7.1.3 while the installed controller still reports
v7.1.2; that is expected. During rollout, different rooms can temporarily show
different worker versions. Inspect both the queue and individual room status
before concluding that all rooms upgraded.

The room log adds a command-handler timing summary at most once per minute when
commands are processed, retaining at most 64 samples. It contains no player IDs,
command names or arguments. It measures handler execution only, including any
synchronous work that handler performs. It excludes socket transit and the
delivery time of replies deferred into the outgoing queue. Historical/reconnect
messages still use the existing ordering and deduplication rules.

Useful feedback: room/mode, time with timezone, command used, approximate observed
delay, whether a map/countdown was active, and any nearby timing or safe failure
lines. Retain state if an update is held or ownership becomes uncertain.
