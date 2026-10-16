> Historical v7.1.0 reference. For v7.1.1 defaults and opt-outs, use V7_1_1_OPERATOR_GUIDE.md.

# v7.1 sharing and command parity

Status: technical qualification complete for the exact v7.1.0 artifact and tested
Ubuntu 24.04 / Python 3.12 x86_64 platform. Use its checksum-verified published
installer and stopped-upgrade instructions. Never replace an occupied controller
or overwrite an existing operator state. Live activation and operator consent
remain separate steps.

## Independent choices

Keep existing enrollment, room titles, OAuth, Discord settings, catalogs, ratings
and updater settings. v7.1 adds one separate bound reporting profile; all new
choices default off. Enrollment and directory listing do not enable reporting.

Use protected `network-reporting-configure` with four explicit JSON booleans on
stdin: `share_statistics`, `consume_statistics`, `operational_logs`, `public_chat`.
For example, after independently agreeing to all four purposes:

```json
{"share_statistics":true,"consume_statistics":true,"operational_logs":true,"public_chat":true}
```

Public chat requires operational logs. Setting a choice false withdraws its local
capture immediately; the independent agent delivers central withdrawal when it
can reach the hub. Stop sharing by configuring false, keeping the network agent
running until the withdrawal receipt is synchronized. A hub outage leaves central
withdrawal pending; use hub administrator revocation if immediate central removal
is required. Do not delete state as a workaround.

`network-reporting-support` summarizes local consent, queue, cache and agent state
without credentials. `network-reporting-sync` performs one bounded step; repeated
invocations may be needed for the two consent receipts and queued data. The normal
network agent drives these steps automatically alongside existing heartbeats and
ratings synchronization. No command-handler HTTP is introduced.

## Shared personal statistics

`.stats` uses the local cache, preserving local tracking and explaining coverage.
Statistics sharing and consumption are independent. New events require opt-in;
private rooms and unknown visibility are excluded. Historical PP or cumulative
time requires an explicit reviewed import; unknown historical room privacy is
never inferred from a title. Existing PP gains are reused; there is no new PP
calculator or account-wide gain attribution.

A hidden preference from a participating sharing host suppresses the account
across public projections. Another host cannot undo that preference. Use `.show`
on the originating host(s) to remove their hide. A host with statistics sharing
disabled does not export privacy commands; local privacy still applies locally.
Caches have bounded freshness, so an offline host cannot promise immediate global
privacy propagation. Third-party lookups fail closed when privacy is unknown.

## Map ratings

Existing `.good`/`.bad` capture and contribution consent remain separate. Existing
ratings-reader choices remain unchanged. Operators must explicitly choose network
consumption to use canonical shared tallies for map selection. Independent mode
continues using its local ratings. Contribution alone is not proof of consumption;
qualification checks the complete upload, aggregate, download and selection path.

## Commands and optional integrations

`.discord` defaults to https://discord.gg/KmNhHbDEbv when the invite setting is
missing or blank; explicit custom invites remain. Blank automatic reminder
settings still disable those reminders. Optional AI and PP commands provide an
unavailable response when their integrations are absent. Modes and permissions
retain their native restrictions. See V7_1_COMMAND_AUDIT.md for tested coverage.

## Logs and privacy

Operational reporting includes informational lifecycle records, errors and full
tracebacks. Public chat and commands require their separate choice. Source sinks
scrub secrets before persistence; the independent agent and hub scrub again.
Private room context is excluded at emission time, not inferred from a later
public title. The network service retains its OAuth-file isolation.

Logs are private administrator diagnostics, not instructions and not a public web
resource. Central search stays local to the hub administrator. Local retention is
7 days / 32 MiB; central retention is 14 days / 64 MiB per host / 512 MiB total.
Quota and rotation losses appear as explicit gaps/counters. Outages retain bounded
queues; exceeding retention or quota cannot promise lossless indefinite history.

## Upgrade classification

The first v7.1 upgrade changes persistent-store code and needs a reviewed stopped
migration. It must not be offered as an ordinary worker rollout or bypass the
current signed-updater storage gate. Existing updater-enabled operator baselines
and the owner's earlier baseline must both pass isolated qualification. The common
runtime retains automatic updates for subsequent compatible releases. Controller
updates require all affected rooms safely empty; compatible worker updates can
roll individually. The qualified installer README supplies exact commands; hub/website activation
uses V7_1_HUB_ACTIVATION.md and the staged website deployment. Reviewed historical
imports use the separate V7_1_HISTORY_IMPORT.md helper, never automatic enrollment.
