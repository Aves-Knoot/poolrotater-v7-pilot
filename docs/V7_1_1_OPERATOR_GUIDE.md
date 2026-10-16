# Installing and operating v7.1.1

Use the attached installer ZIP from the public GitHub release; verify its SHA-256
and `sha256sum -c SHA256SUMS.txt` before running any installer. Ubuntu 24.04 x86_64,
Python 3.12 and systemd remain the qualified platform; the existing systemd LXC
operator is supported within those tested constraints. New installs default to one
Standard Auto room. Use supported presets for two rooms, never a second controller
on the same osu! account. OAuth and Discord credentials stay on your host.

## Existing installations

Already on v7.1.0 with the signed pilot updater enabled? Leave healthy rooms and
the network agent running. The updater discovers v7.1.1 on its normal polling
schedule and waits for a safe all-configured-rooms-empty controller window.
Use `update-status`, `status` and `support` to distinguish discovery, waiting and
installation. Publication does not prove installation. Keep explicit pauses or
opt-outs if you do not want automatic delivery.

Still on v7.0.6/7/8? Follow the qualified v7.1.0 README's one-time stopped storage
migration, keeping the same instance, OAuth, catalogs, titles, enrollment, votes
and history. v7.1.1 cannot automatically bypass that migration. Existing enabled
updater configuration is preserved by v7.1.0 and can then receive this successor.
If no updater is configured, use the shipped public release-root/CA pins and
`release-updates.json`, with the desired policy, to configure it explicitly once.

For a manual v7.1.0 -> v7.1.1 upgrade, record the actual registered entry/instance
and before support/settings. Wait until all its rooms are empty; pause updater
polling, cleanly stop bot and network agent, confirm clean ownership, keep a
private backup, then run the shipped installer with `--upgrade-from-sha256`
`8c0971747c77a22a992749a6171363d60eb4a3bdc5f87460c55f88d881a21d35`
and this release's exact `--sha256`. Use the newly printed protected entry after
upgrade. Compare configuration/authorization/catalogs/enrollment/history before
restarting. Restore prior updater intent. Never rerun authorization, re-enroll,
replace catalogs or delete journals solely to upgrade.

## Sharing is enabled by default; opt out locally

Review these defaults before first starting the network agent. Existing explicit
choices are retained. Missing reporting profiles enable four independent fields:
`share_statistics`, `consume_statistics`, `operational_logs`, `public_chat`.
Only public, positively identified rooms contribute activity/chat. Private logs
are sanitized operational diagnostics, not a public chat archive. Local limits
remain seven days / 32 MiB, central fourteen days / 64 MiB per host / 512 MiB total.

To opt out of all four, send this object to the currently registered protected
entry's `network-reporting-configure` action on stdin:

```json
{"share_statistics":false,"consume_statistics":false,"operational_logs":false,"public_chat":false}
```

You may choose individual true/false values; public_chat requires operational_logs.
Capture stops locally when disabled. Keep the network agent running until central
withdrawal is acknowledged; an outage leaves it pending. `network-reporting-support`
shows progress. Statistics consumption is bound when workers are constructed:
change it before startup or apply a normal safe empty-worker replacement. Cached
privacy is freshness-limited; unknown third-party privacy fails closed.

Rating contribution and selection are separate: use `ratings-configure` with
`{"share_ratings":false}` and `ratings-reader-configure` with
`{"mode":"independent"}` for independent local feedback. A server contribution
grant is still required. An existing opt-out is never silently enabled. The
historical import helper remains a separate reviewed action, not automatic.

## Automatic update controls

The signed pilot channel is the default. Existing configured trust and policy are
not overwritten. On a newly configured/enrolled v7.1.1 host, local `network-start`
also configures a missing updater from packaged public pins and starts polling.
Existing updater configuration or an explicitly stopped updater is preserved.
Report incomplete bootstrap state instead of deleting it.

`update-stop` disables polling without killing an already committing transaction;
`update-pause` pauses the policy. To choose download/install/controller/reporting
preferences, use `update-configure` with the shipped `release-updates.json` after
editing its explicit booleans. Preserve origin/CA/bootstrap pins. A controller
update waits for every configured room, while compatible worker-only changes can
roll individually. Unknown ownership or interrupted commits require review.

## Useful feedback

Return redacted before/after support, network/reporting/ratings/update support,
version/runtime SHA, room links, timestamps and errors. Check `.discord`, `.help`,
`.stats`, good/bad votes and completed maps. Leave healthy rooms running normally;
clean-stop checks can wait for scheduled maintenance. Never send tokens, OAuth
codes/URLs, private invitations, backups or raw unredacted state by chat.
