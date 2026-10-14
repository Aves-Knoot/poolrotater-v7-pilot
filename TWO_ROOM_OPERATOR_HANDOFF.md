# Community display pilot - state-preserving dev11 upgrade

Existing operators: follow COMMUNITY_DISPLAY_UPGRADE.md first. Keep your current instance, OAuth, catalogs, titles, ratings and hub enrollment. The kit starts no services. New display timing requires the coordinated hub activation. The following setup reference is cumulative; do not reinstall an existing instance.

# Standard + Mania 4K operator handoff

Already installed `two` on dev9: use OWNER_HUB_UPGRADE.md to upgrade that exact
instance. Keep its existing OAuth, complete catalogs, chosen titles, enrollment
and ratings sharing. Do not follow the fresh-install steps below again. Xikeon's
enrollment and contribute grant are already complete; do not request a second
invitation or repeat enrollment. Keep the old `first` stopped.

If recovering the October14 crash, stop the network agent before stopped
ownership-review. Confirm any orphaned room has actually ended and follow the
supported ownership-clear checks before starting once. Never discard journals.

Prepared for Xikeon and their Claude Code/Codex assistant. This is a requested
two-room setup, separate from the maintainer's four running rooms. Work on the
operator's VPS only. Keep credentials local; do not alter the owner VPS.

## Send the maintainer these inputs first

- Hosting osu! username and numeric user ID; chosen public operator label.
- OS, architecture, Python version, systemd availability, CPU, RAM and free disk.
- Existing `first` release, redacted `status`, `support`, `ownership-review`, and
  whether it is already hub-enrolled. State whether any rooms remain active.
- Confirm Standard automatic difficulty plus Mania 4K, and any preferred ranges
  or language. Defaults are Standard initially 4.5–5.5★ and 4K 2.5–4.5★.
- Separate yes/no choices for public hub directory listing and sharing `.g`/`.b`
  ratings. These choices are independent; hosting does not require either.

Ubuntu 24.04 x86_64, system CPython 3.12 and systemd are qualified, including the
tester's existing privileged LXC. Other Debian/Ubuntu builds must pass installer
checks; do not bypass an interpreter or integrity refusal. Recommend 2 vCPU,
2 GiB RAM and at least 5 GiB free disk with room for upgrades. Allow outbound
HTTPS to osu!, the catalog origin on 9445 and optionally the hub on 9444.

## Important: this changes the room topology

An in-place upgrade preserves the old one-room topology. Adding `--preset` to an
upgrade is refused. Do not edit `host.json`, delete runtime/ownership journals,
copy credentials between instances or reinstall over the existing instance.

The currently supported route is a fresh **`two`** instance, while retaining the
old **`first`** instance and its history intact and stopped. It requires one local
OAuth authorization for `two`; the existing locally owned OAuth application may
be reused. Old local votes/preferences remain in `first`; this procedure does
not migrate them. If preserving them in the new topology is required, stop and
request a coordinated topology migration rather than improvising one.

Before installing, use the old instance's protected entry to `stop`, then run
`status`, `support` and stopped `ownership-review`. Confirm real upstream rooms
closed and no dedicated workers remain. If its network service was installed,
`network-stop` it. Preserve all old files. Never run both instances on the same
osu! account. If `first` is hub-enrolled, coordinate its leave/revocation before
enrolling `two`; the hub permits one active installation per account.

## Download and verify the published kit

Use the private release:
https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.0.1-dev10-operator-onboarding

Download **`poolrotater-7.0.1.dev10-live-pilot.zip`**, not GitHub's source ZIP.
Extract into a fresh folder. From that folder, verify the archive separately and
then `sha256sum -c SHA256SUMS.txt` for the extracted contents.

| Object | SHA256 |
| --- | --- |
| Archive | Independently verify against the published GitHub release notes before extracting. |
| runtime.zip | `54b662a08bbfda597ae8db59f21e78a50f1dbf3fe266e7a8f9ca7991c2cef78f` |
| bootstrap.json | `2892f9fda822c2aade05fe5b28f354a57623b66555f75f2fb09fd4c0101f7a74` |
| ca.crt | `60a0dac90855a1b0006062e9820c2144ba2240fc097cd685a6ca0b6fcb445f39` |

```sh
sudo /usr/bin/python3.12 -I -S install.py --instance two \
  --bundle runtime.zip \
  --sha256 54b662a08bbfda597ae8db59f21e78a50f1dbf3fe266e7a8f9ca7991c2cef78f \
  --live-pilot --preset standard-auto --preset mania-4k
```

Set `pilot_release` to the exact installed path printed by the installer. For
this release it should be:

```sh
pilot_release=/opt/poolrotater-v7-pilot/two/releases/v7.0.1-54b662a08bbfda59
pr() {
  sudo /usr/bin/python3.12 -I -S \
    "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance two "$@"
}
```

Installation starts nothing. Aliases are **room1 = Standard Auto** and
**room2 = Mania 4K**. Do not use the maintainer's room4 alias for this instance.

## Import the complete prepared databases

Run from the extracted kit folder so the two trust files resolve correctly:

```sh
pr catalog-import --origin https://91.107.237.182:9445/ \
  --trust-root bootstrap.json \
  --trust-sha256 2892f9fda822c2aade05fe5b28f354a57623b66555f75f2fb09fd4c0101f7a74 \
  --ca-file ca.crt \
  --ca-sha256 60a0dac90855a1b0006062e9820c2144ba2240fc097cd685a6ca0b6fcb445f39 \
  --mode standard --mode mania4 --mode mania7
```

These are complete selection databases and analysis companions, not capped
samples. The current release expects Standard 105,502 maps / 105,129 DT rows,
4K 19,334 maps and 7K 4,347 maps. Preserve exclusions and supplied signatures.
Their small size is expected: raw player-score research databases stay on the
owner VPS. No collector or analysis pipeline runs on the operator VPS.

## Set the two public titles before the first start

Keep titles already selected on an existing instance. The native bot inserts
`(Auto Difficulty)` or `(Set Difficulty)` after the first title segment and
appends the active star range and runtime family version. For example,
`PoolRotator Community | Xikeon | Standard` becomes
`PoolRotator Community (Auto Difficulty) | Xikeon | Standard (4.50-5.50*) [v7.0.1]`.

Use these names, replacing Xikeon only if a different public label was agreed:

- `PoolRotater Community | Xikeon | Standard`
- `PoolRotater Community | Xikeon | Mania 4K`

The native bot adds its range/version information. Configure the titles through
this protected stopped command; it preserves other saved defaults and native
preferences. Do not modify Python code or manually create overlay records.

```sh
sudo -v
pr room-settings-configure --settings-stdin <<'JSON'
{
  "rooms": {
    "room1": {"room_name": "PoolRotater Community | Xikeon | Standard"},
    "room2": {"room_name": "PoolRotater Community | Xikeon | Mania 4K"}
  }
}
JSON
pr room-settings-status
```

The command accepts partial patches for existing room aliases and validates the
existing gameplay allowlist. Examples include `countdown_seconds`, `max_length`
and `queue_ahead`. Unknown settings, aliases, secrets, invalid values and topology
changes are refused. Bot and network services must be disabled/stopped, ownership
clean and workers absent. Changes apply at the next start; this is not a live
room-count migration. `room-settings-status` is safe while players are present
and describes saved defaults, not their current temporary preferences.

## Configure and authorize locally

Use the hosting account's own OAuth application. Register the exact redirect URI
`http://127.0.0.1:51111/oauth/callback`. Replace the numeric ID below locally:

```sh
sudo -v
/usr/bin/python3.12 -I -S configure_live.py --instance two \
  --release "$pilot_release" --expected-user-id YOUR_NUMERIC_USER_ID \
  --redirect-uri http://127.0.0.1:51111/oauth/callback
```

This prompts for the client ID and hidden secret. On the browser computer, open
an SSH tunnel (substitute their actual SSH user/host):

```sh
ssh -N -L 127.0.0.1:51111:127.0.0.1:51111 user@their-vps
```

Then on the VPS run `pr authorize --operator-mode ssh`; open its authorization
URL in the browser and finish the callback. `token_phase: ready` indicates sign-in
completed. Local `start_allowed: false` with startup-admission-not-evaluated is
expected; actual start performs fresh identity/client/scope/ownership checks.

## Preflight, start once, collect useful evidence

```sh
pr check
pr status
pr support
pr ownership-review
pr ratings-status
```

If the fresh ratings ledger is unprepared, run `pr ratings-migrate` once while
stopped, then `pr ratings-status`. Do not seed historical ratings.
Ownership must be clean, authorization ready and catalogs complete. Install the
disabled service unit with `pr unit`, then run **`pr start` once**, followed by
`pr status` and `pr support`. Return both room links from the observed IDs.
Do not enable automatic startup as part of this first pilot.

If startup/ownership is uncertain, retain state and report safe stage, failure
code and HTTP status before retrying. No force-clear/reinstall. The hosting
account should remain a referee; other accounts join and play via osu!lazer.

Play 3–5 maps in each room. Exercise help/queue, `.g`/`.b`, skip, Standard
easier/harder, countdowns, ready/unready/AFK with at least two players, normal
joins/leaves and correct 4K maps. Do not mark untried features passed. While
players are present, `status` and `support` should work promptly.

## Hub enrollment and shared votes (separate from room startup)

Send only the new installation UUID and generation, numeric hosting ID, chosen
label and the two consent decisions. The marker is
`/var/lib/poolrotater-v7-pilot/two/.poolrotater-host.json`; extract only those two
UUID fields. Do not send the state directory or secret/config files.

The maintainer assigns an operator UUID and issues a short-lived invitation
bound to this account/installation/generation/directory choice. This is the
remaining maintainer step; do not invent an invitation or self-assign authority.
Rooms may run independently while awaiting enrollment.

Follow the kit's `docs/NETWORK_OPERATOR_GUIDE.md`. Supply exact configure JSON
on private stdin to `pr network-configure` (no extra stdin CLI flag):
`origin`, `ca_pem`, `ca_sha256`, `operator_id`, `account_id`, `directory`.
Origin is `https://91.107.237.182:9444/`; use the pinned public kit CA above.
Supply `{"invitation":"PRIVATE_VALUE"}` on private stdin to
`pr network-enroll`. Keep private files 0600, disable shell tracing, and avoid
placing invitations in argv/environment/chat/Git. Pending enrollment uses
`network-resume`, not a second registration.

Have the maintainer grant the enrolled host **contribute** permission before
enabling local sharing. Then provide `{"share_ratings":true}` on stdin to
`pr ratings-configure --ratings-stdin`. No historical seed for this pilot.
Install `pr network-unit`, then `pr network-start`. Capture `network-support`
and `ratings-support`. Nominal public heartbeat is ~60 seconds; vote upload
is ~30 minutes with jitter. Keep independent/local map weights for the first run.
Uploading votes does not by itself switch map selection to network weighting.

Directory consent and a successful fresh heartbeat permit listing in the hub and
the owner's separate Community Lobbies website section. The existing four owner
cards remain separate. Fresh community joins expire at180 seconds without a new
observation; listings disappear by600 seconds, and a failed publisher refresh
withdraws remote links. Installation alone does not enroll or list a host. Discord integration is optional and
requires the operator's own settings; the owner's Discord credentials are not
distributed.

## Return to the maintainer

- Release/runtime digest, both links, start/end times and timezone.
- Redacted before/active/after `support` and `status`; stopped ownership review.
- Tests completed per mode, map IDs and concise gameplay/command observations.
- Safe failure stage, code, HTTP status and elapsed diagnostic time if any.
- Good/bad vote map IDs/times, outbox/sync/snapshot counters after an agreed
  interval; the maintainer checks central receipt/deduplication separately.
- Whether both titles/ranges were right and instructions caused confusion.

Do not send tokens, OAuth secrets/codes/URLs, invitation files, private pacing
hashes, player identities or unredacted logs. Hosting user ID and installation
UUIDs are sent separately only for the enrollment binding above.

When the agreed test ends, empty both rooms and `pr stop`; if started, also
`pr network-stop`. Verify actual upstream closure, no workers and clean stopped
ownership, then capture after-support. Leave running only if the maintainer and
operator explicitly agree on the continuing public pilot. Updates use a clean,
stopped same-topology upgrade; keep state and do not reinstall.
