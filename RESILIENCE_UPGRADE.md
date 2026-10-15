# Dev14 — chat timeout resilience and failed-room containment

This is the operator port of the owner's deployed v7.0.5 repair. The tester
runtime retains the v7.0.1 family and identifies the package as 7.0.1.dev14.
It contains no unfinished automatic updater and does not enable automatic updates.

Keep the existing `two` installation, its two rooms, titles, tuned ranges,
OAuth/Discord settings, catalogs, enrollment, sharing, votes and historical state.
Keep the old `first` stopped. Do not reinstall, reauthorize, reapply defaults,
repeat ownership-clear, clear pacing state or recreate votes.

## Upgrade from dev12 or dev13

Download `poolrotater-7.0.1.dev14-live-pilot.zip` from this private GitHub release
(not GitHub's Source ZIP). Extract into a NEW directory. Verify the archive digest
against the release notes, then run `sha256sum -c SHA256SUMS.txt` in that directory.

If rooms are healthy, arrange an empty-room maintenance window and use your
CURRENT registered protected entry to stop the bot cleanly. Confirm closure with
`ownership-review`, then stop the network agent with `network-stop`. Both units
must be stopped and disabled. The upgrade script checks this and never forcibly
stops a room. Retain all state if closure/ownership is uncertain.

From the extracted kit run ONE of these, matching the installed package:

```sh
bash UPGRADE_XIKEON.sh dev13
# If still on dev12, use this instead:
# bash UPGRADE_XIKEON.sh dev12
```

The script checks the exact predecessor digest, verifies stopped clean ownership,
upgrades the same instance, checks unchanged room settings and valid retained
account/catalog/enrollment state, and starts the network agent and bot once.
It does not retune the installation. Normal startup refreshes account/ownership
checks; local status alone does not grant start admission.

Runtime SHA256: `182615d79647a68dfca9f75ed440b77081dee52682a7d1bc737e913fd36bbd96`
New protected entry:
`/opt/poolrotater-v7-pilot/two/releases/v7.0.1-182615d79647a68d/poolrotater_host/pilot_admin_entry.py`

If a step fails, keep its report folder and all installation state. Send the
redacted failure and stop there; do not loop startup attempts. Use the included
installer's documented explicit upgrade recovery if a stopped upgrade was
interrupted, after maintainer review. Never delete upgrade/ownership journals.

## Leave healthy rooms running

Return both room links, start time and redacted support, network-support and
ratings-support. Confirm the existing titles/ranges stayed intact. Leave healthy
rooms running continuously and send a summary after several hours or any incident.
Try normal joins/leaves, READY/unready/AFK, countdown/map progression, `.help`,
`.skip`, `.start`, `.easier`/`.harder`, `.good`/`.bad`/`.ok` when convenient.
Do not deliberately induce networking failures or abort occupied matches.

Known transient chat history and ACK failures now defer routine maintenance,
preserving pacing cooldown/counters and chat cursors. An uncertain chat send is
not replayed. Positively undispatched sends enter the existing bounded queue.
Profile enrichment can wait behind pacing without losing authoritative occupancy.
An unexpected dead worker whose room ownership can be positively reconciled is
closed and parked alone; healthy sibling rooms continue. Unsafe cleanup, uncertain
ownership and shared referee failure still require explicit recovery. This is not
a guarantee that every possible provider failure can be recovered unattended.

If only one room is parked, leave its healthy sibling alone and report it; do not
restart everything to repair a single room. If the entire bot is already stopped
and a positively closed parked row blocks maintenance, the included root helper
can normalize descriptive status only:

```sh
sudo /usr/bin/python3.12 -I -S normalize_closed_parked.py --instance two \
  --release /opt/poolrotater-v7-pilot/two/releases/v7.0.1-182615d79647a68d
```

The helper refuses running processes/services, unknown ownership or non-closed
rooms. It never contacts osu!, clears uncertain ownership or starts services.
Follow it with `ownership-review`; retain state and report refusal.

For incidents send timestamps/timezone, both links, relevant journal lines,
redacted support/ratings/network outputs and failure codes/HTTP status if present.
Do not send OAuth credentials, tokens, raw state databases, private configuration,
account IDs or pacing-state hashes. Pending votes remain local until hub receipt.

Complete prepared catalogs and previous gameplay/playlist/help/display fixes remain.
No research databases or credentials are bundled. Optional PP projection still
requires the separately installed .NET 8 runtime; no calculator change is included.
