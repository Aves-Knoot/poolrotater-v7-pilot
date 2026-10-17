# v7.1.5: stopped repair and one supervised startup

Use the independently pinned launcher supplied with the v7.1.5 GitHub release.
This repairs an existing installation; do not reinstall or authorize OAuth again.
The package supports the exact published v7.1.1, v7.1.2, v7.1.3 and v7.1.4 runtimes.
Ubuntu24.04 x86_64, Python3.12 and systemd remain required.

For the stopped `two` instance, the maintainer's one-command launcher supplies:

```
--instance two --room-alias room2 --created-room-id 4716142 --start --resume-agents
```

Those arguments explicitly identify the independently found lost creation. The
repair makes fresh read-only osu! checks proving the exact room belongs to the
hosting account and has ended. An empty owned-room listing is not sufficient.
If recovery is already clean, the historical clear is not repeated.

The command verifies the launcher and complete installer archive, verifies the
runtime and internal kit checksums, checks that the bot is stopped, pauses only
the existing network agent and update timer, and refuses an in-flight updater.
It upgrades in place, preserving OAuth, titles, presets, tuning, catalogs,
enrollment, ratings, history, explicit opt-outs and update policy. It then uses
the supported stopped ownership commands and a fresh journal digest. Startup
is attempted exactly once only after a clean review and local status checks.

The two rooms come from the existing configuration. No presets, ranges, titles,
passwords, feature defaults or permission lists are reset. Current live rooms
are never shut down by this helper. Rerunning against an already-running exact
v7.1.5 installation reports that state without restarting it.

`--resume-agents` starts the existing enrolled network agent and preinstalled
update timer after successful bot startup. It does not enable units, change
sharing/update consent, remove opt-outs or clear updater pauses. Existing unit
enablement is preserved. Omit `--start --resume-agents` for a stopped repair only.

If any step refuses, preserve all state and report the returned phase and report
directory. Do not blindly retry startup after an uncertain result. Private
before/after status, support and recovery output stays under
`/var/lib/poolrotater-v7-repair-reports/<instance>/<run>/` with restrictive permissions.
Send redacted support and the helper's final JSON; omit private account IDs,
pacing hashes and credential files. Include room links, start time and any errors.

The installed old updater cannot apply this repair while the bot is stopped.
The operator must explicitly run this one command locally; publication is not
remote shell access. The helper requests no new credential or login.

The repair defers scheduled refresh before closing a healthy room when local
HTTP admission is unavailable. If a cooldown starts after creation, it saves the
new room identity immediately and waits within a bounded window for fresh owner
verification outside the account's shared routing lock. Occupied sibling rooms
can continue processing events. Failed verification never repeats MakeRoom or
grants authority from an unverified candidate. Permanent refusal still fences
ownership and may require stopped operator review.

The broader maintenance/admin workflow remains separate. This bounded package
uses the published v7.1.4 engine plus the refresh/recovery repair and typed
undispatched lock handling; it does not include the unpublished maintenance UI,
controller-owned one-map mailbox or consecutive-upgrade-chain work.
