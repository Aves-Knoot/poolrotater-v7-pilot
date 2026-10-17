# v7.1.8 installation and upgrade

Read MAINTENANCE_OPERATOR_GUIDE.md for maintenance, cancellation, recovery and
incident procedures. Keep the existing instance, complete catalogs, OAuth,
configuration, enrollment and all state. Do not reinstall to repair a failed start.

Running opted-in hosts use their existing signed update channel. The controller
update waits for every configured room to be safely empty, rechecks the roster,
then preserves the original service enablement/network state. No manual GitHub
step is needed for a healthy running host. Individual future worker-only changes
retain per-room empty rollout. Explicit opt-outs and pauses are unchanged.

For a fresh host, verify and extract the release installer ZIP, then follow
README.md, LIVE_AUTHORIZATION.md and the supplied catalog/network setup documents.
Use the downloaded installer asset rather than GitHub's generated source ZIP.
All analysis stays at the hub; clients download complete prepared selection data.

For an existing stopped host, retain state and capture support/ownership-review.
Resolve any uncertain room creation/closure using fresh owned/ended-room evidence
and the documented clear/resume commands. The pinned repair launcher supports the
existing operator repair flow; do not apply an old launcher pinned to an older
release. Normal stopped upgrades use install.py with the explicit current and new
runtime digests. Use the newly registered administrator entry afterward. Start
once after checks; keep state and report an uncertain result before retrying.

The exact current registered entry can be located from the installation's protected
releases directory and upgrade result. Do not reuse a historical release path after
a successful upgrade or execute a writable checkout as a privileged administrator.
The bot account's OAuth flow and all credentials remain local to its operator.

## Fresh host: exact initial sequence

This section is for a genuinely new installation only. Keep an existing install
and its configured updater/network/OAuth state when upgrading. Qualified platform:
Ubuntu 24.04 x86_64, Python 3.12 and systemd, including the tested LXC environment.
ARM/new rulesets are outside this release. The optional .NET PP calculator remains
separate; its absence does not prevent ordinary hosting.

After independently verifying the installer ZIP digest and all internal checksums,
run these commands from its extracted kit in Bash. Choose your own unused instance
name; do not make a new instance to bypass recovery on the same hosting account.

```bash
pilot_instance=community
runtime_pin=$(/usr/bin/python3.12 -I -S -c 'import json; print(json.load(open("release.json"))["runtime_sha256"])')
sudo /usr/bin/python3.12 -I -S -B install.py --instance "$pilot_instance" \
  --bundle runtime.zip --sha256 "$runtime_pin" --live-pilot --preset standard-auto
pilot_release="/opt/poolrotater-v7-pilot/$pilot_instance/releases/v7.1.8-${runtime_pin:0:16}"
pr() {
  sudo /usr/bin/python3.12 -I -S -B \
    "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance "$pilot_instance" "$@"
}
pr check
pr catalog-import --origin https://91.107.237.182:9445/ \
  --ca-file "$PWD/ca.crt" --ca-sha256 60a0dac90855a1b0006062e9820c2144ba2240fc097cd685a6ca0b6fcb445f39 \
  --trust-root "$PWD/bootstrap.json" --trust-sha256 2892f9fda822c2aade05fe5b28f354a57623b66555f75f2fb09fd4c0101f7a74 \
  --mode standard --mode mania4 --mode mania7
```

One Standard Auto room is the default. To choose Standard plus Mania 4K on a
fresh install, supply both `--preset standard-auto --preset mania-4k` on that
initial installer command. This is one controller with two rooms, not two accounts
or independent installations. Review `pr room-settings-status` for the selected
presets, titles and ranges; use the supported room-settings commands to customize.

Complete LIVE_AUTHORIZATION.md locally using this instance/release, your own osu!
user ID and your own OAuth client. It documents the terminal credentials prompt,
SSH callback tunnel and browser authorization. Do not share OAuth secrets. Then:

```bash
pr unit
pr ownership-review
pr status
pr update-configure --updates-stdin < release-updates.json
pr update-unit
pr update-start
```

Review the supplied update policy before applying it on a new host. It defaults
to pilot-channel download/install/controller updates; adjust explicit choices if
needed. For hub enrollment, send the maintainer your hosting account ID, instance
installation/generation IDs and desired public operator label. Follow the bound
invitation packet and NETWORK_OPERATOR_GUIDE.md locally; keep any pending
operation and use network-resume rather than enrolling twice. Confirm contribution
permission and ratings configuration as directed. Existing enrolled hosts retain
those bindings and should not repeat this step. Network integration is independent
of local OAuth and does not itself start rooms.

When authorization, catalogs and ownership checks are clean and you intend to
open public rooms, run `pr start` once. Inspect `pr status` and `pr support`; retain
state if startup is uncertain. A successful status supplies the room IDs for
`https://osu.ppy.sh/multiplayer/rooms/ROOM_ID`. Leave healthy rooms running and let
automatic updates use safe empty windows. Use supported maintenance commands for
explicit announcements/draining. `pr stop` performs a local requested shutdown;
service enablement stays an explicit operator choice.
