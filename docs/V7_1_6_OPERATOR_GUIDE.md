# v7.1.6 operator upgrade

Existing enrolled hosts with automatic controller updates enabled receive this
through the signed pilot channel once published. Leave settings and state in
place. The updater uses its existing all-configured-rooms-empty admission gate;
occupied, unknown or recovery-required rooms must never be force-restarted.
Explicit sharing/update opt-outs are preserved. The operator's network agent
must be running for prompt shared feedback delivery.

For a manual stopped upgrade, download the v7.1.6 installer asset from the public
GitHub release and verify its published SHA-256 and internal SHA256SUMS.txt.
Do not use GitHub's generated source archive as the installer. Keep the existing
instance and all its state. Use its existing administrator `code-proof` output
to obtain the currently installed runtime SHA-256, and use the new kit's `install.py`:

```sh
sudo /usr/bin/python3.12 -I -S "$kit/install.py" \
  --instance "$instance" --bundle "$kit/runtime.zip" \
  --sha256 554ac796491d912ee6c94965f09e65897bd3eb2559785540b5304e9a4a15d9ee \
  --live-pilot --upgrade-from-sha256 "$installed_runtime_sha256"
```

Stop the existing bot/network/updater through their documented controls before
manual upgrade; do not stop occupied rooms without an agreed maintenance window.
Exact published v7.1.1 through v7.1.5 predecessors are supported. Do not reinstall,
reauthorize OAuth, replace catalogs or rerun completed ownership recovery.
An existing uncertain creation still requires its documented fresh stopped owner
checks; this release does not make an unresolved room safe to start.
The separate v7.1.5 one-command repair launcher remains pinned to v7.1.5 and is
not a v7.1.6 launcher. Do not alter its checksums or substitute a new archive.

After normal startup, send redacted support and ratings-support if anything
fails. For a feedback issue include the room link, map, approximate time, command
and bot reply. Do not send credentials or token files. A useful successful check
is two players rating a completed map, followed by an opinion change, while map
progression and other rooms continue normally. No special player instructions
are needed; `.good`/`.bad` and `.g`/`.b` keep their meanings.
