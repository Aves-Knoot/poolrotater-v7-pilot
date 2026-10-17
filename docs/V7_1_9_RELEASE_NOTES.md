# v7.1.9 — shared historical statistics and independent style controls

Community rooms can retrieve reviewed public historical playtime and tracked PP
from the hub, alongside new activity from participating hosts. A completed empty
username lookup now says no shared stats are available instead of remaining
“syncing.” The short cache expires so newly imported history becomes visible.

Historical backfill uses the independent reporting agent, durable uploads and
the existing activity protocol. A reviewed installation-bound public-room
manifest is required. A checksum-pinned migration backup can supply stable
legacy history while current hidden-player choices remain authoritative.
Cumulative totals are withheld for players whose recorded history includes
excluded or unreviewed rooms; private activity is never guessed from totals.
Retries, restarts and full queues retain progress without multiplying imports.
Existing website owner-source deduplication keeps owner history from being
added a second time. Captured locally and accepted by the hub are separate
stages; history does not appear instantly when an update is installed.

Standard Farm, Stream and Jump are independent toggles that can be mixed.
`.aim` aliases the existing `.jump`; DT remains separately controlled with
`.dt on` / `.dt off`. `.variety` clears style toggles while keeping the DT
choice. Changes affect future selections and preserve the visible queue,
filters, reservations, exclusions and normal permission/flood guards. Mania
style controls retain their existing behavior. `.t` aliases `.time` with the
same permissions, throttle and translations.

This combined release is a **controller update**. Healthy opted-in hosts wait
for all configured rooms to be empty together before replacing the controller
and reopening rooms. It does not require reinstalling, signing in again,
re-enrolling or resetting configuration, history, catalogs or ratings. Existing
opt-outs, update pauses and service enablement remain authoritative. A stopped
or recovery-required host is never remotely started by publishing a release.

Validation: 402 affected tests, two native full-catalog gameplay cases and a
protected root/service-UID v7.1.8 → v7.1.9 upgrade passed against the exact
runtime. Four additional history-review installer checks passed. Standard,
Mania 4K and Mania 7K fixtures use complete prepared catalogs. Full repository
discovery and new upstream gameplay testing were not repeated. No actual
gameplay acceptance is claimed by the release qualification.

See [operator instructions](V7_1_9_OPERATOR_GUIDE.md),
[style controls](PLAYER_STYLE_CONTROLS.md) and
[history repair details](HISTORY_STATS_REPAIR.md).
