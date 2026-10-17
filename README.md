# PoolRotater v7.1.10

Use the [installer release](https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/tag/v7.1.10), [operator guide](docs/V7_1_10_OPERATOR_GUIDE.md) and [release notes](docs/V7_1_10_RELEASE_NOTES.md). Download the installer asset, not GitHub’s source ZIP.

This controller release hardens cleanup/recovery and unblocks reviewed historical statistics. Existing settings, credentials, catalogs, enrollment, histories and privacy/update choices remain. Healthy opted-in hosts wait for every configured room to empty naturally. A held or deliberately stopped host requires its supported local checks; publishing does not start it.

## Existing hosts

Keep your current instance and state. Use your existing signed updater and saved policy. Inspect status/support/update status if a transaction is held; do not reinstall, delete journals, clear unknown ownership or retry uncertain room creation. See the current operator guide for bounded automatic recovery and reporting health. Older repair launchers retain their original pins.

## Fresh install

Verify all checksums first. The default is one Standard Auto room. For an explicit
two-room Standard Auto + Mania4K configuration:

```bash
sudo /usr/bin/python3.12 -I -S -B install.py --instance two \
  --preset standard-auto --preset mania-4k --bundle runtime.zip \
  --sha256 4dac79a77705494df51a34a0e25e8cd9c6e9bba2c6e61a02ef2292bd156be2e9 --live-pilot
```

Follow NETWORK_OPERATOR_GUIDE.md, LIVE_AUTHORIZATION.md and the current operator
guide to import complete signed catalogs, set titles, authorize your own account,
and redeem a privately supplied bound hub invitation. No local collection/analysis
is needed. Do not run a second controller for the same hosting account.

## Enabled defaults and opting out

Before starting, review docs/V7_1_10_OPERATOR_GUIDE.md. Missing profiles enable public
statistics sharing/consumption, sanitized operational/public-chat diagnostics,
enrolled rating contribution and verified shared rating selection. New directory
configuration defaults to public listing when omitted. Existing explicit false
values, independent readers and directory choices remain unchanged. The server
contribution grant is separate. Private/unknown-room content is excluded; raw
chat is never a public website resource. Historical data is not automatically
imported. Scrubbing, bounded retention and withdrawal receipts remain in place.

New updater configuration defaults to signed pilot downloads/installations,
controller updates and private update reports. A local `network-start` on an
enrolled v7.1.1 host initializes a missing updater from the packaged public root
and TLS pins. Existing trust/policies and stopped timers are not replaced. Use
`update-stop`, `update-pause`, or explicit `update-configure` preferences to opt out.
Publication does not prove installation, and polling never forces occupied rooms
closed or starts a stopped bot only to update it.

`network-reporting-configure` accepts all four explicit booleans on stdin:

```json
{"share_statistics":false,"consume_statistics":false,"operational_logs":false,"public_chat":false}
```

Keep the network agent running until central withdrawal is acknowledged. Shared
statistics consumption changes apply to newly constructed workers. Rating
contribution/reading remain separate supported settings. See the current guide.

## Useful feedback and qualification

Return redacted support, network/reporting/ratings/update status, version/hash,
room links and times. Try .discord, .help, .stats, feedback and completed maps.
Never post credentials, token files, private invitations, backups or unredacted
state. V7_1_1_VALIDATION.md distinguishes isolated qualification from real-player
soak. Website publication and independent operator activation are separate facts.
