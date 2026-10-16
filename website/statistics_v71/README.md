# v7.1 website statistics integration

This is a staged extension of the existing `statistics:start` page and its
read-only owner-ledger exporter. It does not create a separate statistics page.
No live deployment or bot/service changes are performed by importing these files.

## Data flow

1. The included `publish_snapshot.py` private, unprivileged hub-side job calls
   `poolrotater_host.activity.repository.Repository(store).snapshot(now=now,
   include_hidden_for_totals=True)`. `store` is the existing authenticated hub
   store. The repository binds uploaded activity to the enrolled host and its
   independently selected statistics-sharing consent. A public directory choice
   or ratings-sharing grant does not opt the operator into statistics.
2. Publish that complete snapshot atomically to a **private** local file readable
   by the existing analytics service, by default
   `/var/lib/poolrotater-network-statistics/network-statistics.json`. Use mode `0640`
   and a dedicated service group. Do not expose this intermediate through HTTP;
   it includes player IDs needed for privacy filtering and exact aggregation.
   `POOLROTATER_NETWORK_STATISTICS_SNAPSHOT` selects an alternative mounted
   read-only path for isolated qualification. Snapshot generation must not use a
   publicly truncated directory or top-50 leaderboard as its input.
3. `analytics_exporter.py` reads the existing owner ledger in its established
   short read-only snapshot transaction. It passes **all** owner rows to
   `network_statistics.compose` before any leaderboard truncation, merges the
   local hub snapshot, and publishes the existing `statistics.json` allowlist.
   There is no hub HTTP request or osu! API request in this export operation.
4. `analytics.php` is the existing renderer extended with a host filter. Existing
   lobby-type, period, ranking, achievement, player details, accessibility and
   no-JavaScript GET form remain. Existing `script.js` submits FormData and
   replaces the same escaped `.pr-stats-body`, so the host selection works in the
   existing progressive-enhancement refresh path without a second renderer.

The included private publisher adds `owner_host_ids`, a private list
of enrolled host IDs whose authenticated account equals the current owner's
bound public account ID. This prevents a reviewed owner cumulative import from
being added on top of the live owner ledger. The merger also recognizes an owner
source when its activity references a room already present in that ledger. This
field is never copied into public output.

## Public schema compatibility

The existing `schema: 1`, timestamps, availability and `scopes` keys remain.
`scopes[all|standard|mania_4k|mania_7k][all|7d|14d|30d]` now describes the combined
view. Each group retains `totals`, `public_players` and the allowlisted union of
exact top-50 rankings. Stable osu! IDs merge owner and community players before
that truncation, so someone outside every old owner top 50 can still qualify for
a combined ranking.

Additional fields:

- `hosts.owner`: existing owner-only scopes, with its original cumulative time,
  recorded PP, map metadata and privacy behavior.
- `hosts.<host_id>`: `label`, `kind`, `consent_at`, `updated_at`, `coverage`, and the
  same scopes for one enabled community host. Account IDs, credentials, raw
  intervals, raw event IDs and the private hidden-ID list are never exported.
- `coverage`: snapshot freshness, opted-in host count, map/time evidence basis,
  whether tracked PP records are supported, and whether explicitly reviewed
  historical imports are present.
- `totals.seconds` / player `seconds`: union of verified interval telemetry in
  combined/community views. Same-player overlapping host intervals are counted
  once. A reviewed cumulative import replaces time before its cutoff on that
  host; later intervals supply new verified time.
- `legacy_owner_seconds`: separately displayed owner cumulative historical time.
  Historical chronology is unavailable, so this is never added into a claimed
  exact interval union.
- `legacy_community_seconds`: separately displayed, explicitly reviewed
  cumulative imports. Latest per-host/source snapshots replace older values,
  imported copies of the same source deduplicate, and they are not presented as
  overlap-verified durations. No historical time is inferred from chat logs.

Public-room telemetry starts after independent opt-in. A separate operator
review is required for legacy backfill: old ledgers cannot prove that every old
room was public. Raw source provenance and dates remain intact. Mania history
without key-count evidence appears only in all-lobby totals, never invented as
4K or 7K activity.

Map participations deduplicate by canonical native match key and osu! user ID.
The current owner ledger is authoritative for its rooms and is not re-added
from its network upload. Owner history preserves map starts; community records
are completed-match participation. Submitted scores/accuracy appear only when
observed. Existing tracked PP achievements are shared as their original records,
deduplicated by room/playlist item/user (native achievement key fallback). This
adds no new PP attribution algorithm and does not claim complete profile PP
progression. The capability flag keeps older snapshots explicitly owner-only.

Every privacy-hidden player is removed from every named ranking, spotlight and
player Details across all hosts. Hidden players can still contribute anonymous
totals, matching the previous page. If the owner privacy query fails, the normal
exporter fails closed for named publication. Missing/malformed/>10-minute-old
network snapshots remove community publication while preserving owner history.

## Qualification and installation

Run the meaningful targeted checks inside the existing offline boundary:

```
scripts/offline .venv/bin/python -m unittest \
  tests.test_network_statistics tests.test_network_statistics_web \
  tests.test_network_statistics_deployment tests.test_network_statistics_publisher -q
```

The PHP tests invoke the actual existing PHP renderer in disposable fixtures;
assertions cover escaped untrusted host/player names, hidden-player omissions,
no-JavaScript host selection, stale output, owner achievements and details.
No production ledger, credentials, API or room is read or used by these tests.

`baseline.json` pins the exact live source bytes used for this staged delta.
`deploy.py` defaults to a read-only hash check. Its `--root` switch qualifies the
same operation in a donor/staging filesystem. An explicit maintainer `--apply`
installs only three website/exporter code files with atomic writes and a private
backup/receipt; no unit is restarted, and no bot state is modified. If any source
baseline changed, deployment refuses before writing. `--rollback` requires the
exact installed digests and refuses to overwrite a later maintainer edit.

Example maintainer commands, only after qualification and publisher wiring:

```
python3 website/statistics_v71/deploy.py --backup /var/backups/poolrotater-statistics-v71
python3 website/statistics_v71/deploy.py --backup /var/backups/poolrotater-statistics-v71 --apply
python3 website/statistics_v71/deploy.py --backup /var/backups/poolrotater-statistics-v71 --rollback
```

The hub job also needs its own runtime code, database permissions/read-only
export binding, private output directory and scheduling. Those are intentionally
not silently installed by this website-only deploy helper. The existing analytics
service needs a read-only mount/access to the private snapshot if its unit has an
explicit read-path allowlist. A browser visit is useful for visual confirmation;
it does not substitute for authenticated ingestion/replay/privacy tests.

## Exact private publisher integration

Import `publish_snapshot` from this staged module into the sealed hub-admin
entry, or install the module alongside its qualified v7.1.0 runtime. Invoke it
under the existing hub service UID, never the bot UID, network-agent UID or PHP
worker UID:

```python
from website.statistics_v71.publish_snapshot import publish_snapshot
receipt = publish_snapshot(
    authenticated_store,
    Path("/var/lib/poolrotater-network-statistics/network-statistics.json"),
    owner_account_id=owner_public_account_id,
)
```

`authenticated_store` is the existing validated `network.store.Store`; this
function does not accept arbitrary credentials, invitations or host-provided
owner bindings. It derives owner host IDs by comparing the authenticated host
account binding with `owner_account_id`. The owner ID is public, nonsecret and
should come from the already verified local owner binding, not a web query.

For an isolated checkout or a qualified module installation the CLI equivalent
is `python -m website.statistics_v71.publish_snapshot --hub-root HUB_ROOT
--output PRIVATE_JSON --owner-account-id OWNER_PUBLIC_ID`. The CLI rejects root;
use the established hub service identity. A root-admin wrapper may drop to that
UID before calling the function. There are no provider/API requests and no bot
or network-agent restarts.

Provision a dedicated directory owned by the hub UID with group access for the
analytics exporter only, mode `2750`. The publisher requires setgid inheritance,
writes a temporary file in the same directory, fsyncs it, atomically replaces
one mode-`0640` JSON file and fsyncs the directory. It refuses symlinked paths,
world access, group writes, foreign ownership, unsafe existing files and output
over 128 MiB. Do not add the web server/PHP UID to this group: the PHP renderer
needs only the already public, scrubbed `statistics.json`.

Run the publisher every five minutes **before** the existing analytics export,
using a separate hub-owned systemd oneshot/timer or existing hub task wrapper.
The analytics exporter receives only a read-only bind/access to the private
snapshot directory, not to the hub root. Keep both jobs at nice 15 or higher.
Concurrent readers see either the previous complete file or the new complete
file. Missing or older-than-ten-minute snapshots fail closed for community
publication while preserving owner data. No persistent credential file or token
is passed to this job: it uses its normal OS permission to the hub store.

The stdout receipt contains `ok`, `kind`, format/timestamp, byte length/SHA256,
host and event counts, and explicit false flags for HTTP exposure/consent
changes. It contains no usernames, osu! IDs, host IDs, raw rows or privacy list.
Raw private JSON is never printed to stdout or mounted into public docroot.
Deployment of this private job is distinct from `deploy.py`'s three-file
website transition; qualify both before the maintenance action. Rollback should
stop only the new private publishing timer, restore the website receipt's exact
old bytes, remove its analytics read-only mount if added, and retain the private
snapshot for investigation or delete that disposable projection. It must not
roll back/erase activity databases, sharing choices, ratings or local bot state.
