# v7.2.6 — distributed Loved rooms and matching fresh-host titles

- New explicit `standard-loved` preset serves native osu!standard Loved rooms
  through the existing engine, including gameplay commands, feedback and shared
  activity/statistics. It downloads the complete signed owner-prepared catalog;
  community hosts do not collect or analyze maps.
- Initial catalog:3,845 qualified difficulties from a complete1,287-mapset
  collection. Eight zero/unqualified-star rows are omitted; download-disabled
  rows remain marked and are not selectable. No scores/player tables/audio are
  distributed. Loved DT/converted charts are not advertised without evidence.
- New host title defaults match the owner's Standard/4K/7K/Catch templates;
  Loved uses `AutoHost Loved|Type .help|`. Existing saved/custom room names,
  settings, topology, account credentials, enrollment and history are retained.
- DT picks preserve the room's configured freestyle flag. DT is still required
  per playlist item, with the existing speed/rating/variant checks retained.
- Public catalog directories retain listener traversal permissions even when
  publication runs under the administrator's restrictive umask; signing custody
  remains private.
- Fresh installs of current versions now initialize shared-activity storage;
  they no longer depend on the historical v7.1.0/1 installer version branch.

## Installing a Loved room

Use the installer ZIP asset and Loved instructions in LOVED_OPERATOR_GUIDE.md.
Select `--preset standard-loved` for a NEW instance, import signed `--mode loved`,
complete your own local OAuth and return installation binding details for a private
hub invitation. The maintainer grants contribution after enrollment. No room is
started by installation or authorization. No OAuth secret/token is sent to the hub.

The protected installer currently enforces Ubuntu24.04 x86_64, system CPython3.12
and systemd. Existing qualified LXC support remains. ARM/other OS support is not
introduced. The database is small because it contains finished selection data.

## Existing hosts and compatibility

This is a controller update from v7.2.5. Healthy opted-in hosts wait for all of
THEIR configured rooms to be empty; occupied rooms and late joins postpone it.
Nothing adds Loved or changes a saved title merely because a host updates.
Older hosts retain the signed predecessor chain; no reinstall or repeated OAuth,
enrollment, ownership-clear or state reset is required. Explicit opt-outs remain.
A stopped/held host still uses its established recovery workflow.

The original Standard/Mania/Catch catalog identities and trust root are unchanged.
Loved has a separate signed exclusion target; legacy core/Catch targets contain
no unknown Loved mode. The directory uses the compatible Standard fixed-difficulty
wire preset, with Loved identified by its title/selection style. Ratings/activity
remain native Standard. Downgrading an explicit Loved configuration to an older
runtime that lacks that preset is not supported.

## Validation

470 unique focused regression cases passed. See VALIDATION.json for full-catalog protected owner and
community upgrades, and the protected fresh Loved install/native lifecycle case.
The latter uses the real sealed runtime and service UID with an invented TLS/
WebSocket provider in an isolated network namespace. Actual operator gameplay and
a fresh external machine's systemd acceptance remain separate observations.
Full repository suites and actual community-account gameplay are not claimed.
