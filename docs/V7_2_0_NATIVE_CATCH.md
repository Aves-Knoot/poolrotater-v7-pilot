# v7.2.0 — optional native Catch

This release adds optional native osu!catch to the retained PoolRotater engine.
Existing hosts retain their rooms; adding Catch is an explicit operator choice.
See the accompanying VALIDATION.json for exact installation checks and limits.

The first profile is `catch-fixed`: native NoMod, fixed 3.00–4.00 stars,
Ranked/Approved only. Loved, converted charts, automatic difficulty, farm
weighting and pattern/style controls remain disabled. Normal queue, readiness,
countdown, skip, difficulty votes, moderation and good/bad feedback remain.

The complete production catalog contains 13,395 checked native records; 12,317
Ranked/Approved records are eligible before room filters. With a four-minute
maximum, the initial 3–4-star range contains 1,951 charts. Selection and PP use
pinned official NM calculations and file identity. Public metadata includes art
and provider song duration when known. Unknown total duration stays unknown.
Research player/score/cohort data, map files and audio are not distributed.
The separate production projection records the owner's attested permission for
this use; it does not change the original research export or its restrictions.

## Existing operators

Upgrading keeps every configured room, setting, authorization, Discord identity,
enrollment, sharing choice and update/boot preference. It does not add a Catch
room. This is a controller update: use the existing empty/drained maintenance
procedure. Occupied matches must not be interrupted. Compatible later worker
releases can still update one empty room at a time.

Catch selection catalogs are optional signed additions to the existing catalog
root. Standard and Mania clients keep their original three modes and compatible
ratings/statistics routes. The Catch-capable paths add explicit ruleset/variant
context. Feedback never treats the same numeric map ID as a cross-ruleset vote.
Catch opinions are captured, acknowledged and synchronized in that domain;
selection retains the accepted neutral baseline rather than enabling unqualified
style, farm or representation weights.

The reviewed direct predecessor is the exact published v7.1.20 runtime. Older
hosts finish their existing signed upgrade chain first; do not replace an
outstanding controller transaction with this release.

Fresh operators can explicitly select `--preset catch-fixed` at installation,
then import the Catch catalog through the documented pinned HTTPS catalog
workflow. Defaults still select the ordinary Standard profile. Linux x86_64,
CPython 3.12, glibc and systemd remain the qualified installer target; this
release does not claim new ARM or operating-system qualification.

Fresh shared-ledger setup prepares Catch's optional opinion/statistics tables
before workers read them. Cold Catch startup receives a bounded longer status
observation window for full-catalog validation; worker, ownership and provider
deadlines remain unchanged. If a startup result is uncertain, inspect the
existing service and retain its state rather than repeating room creation.

## Fresh Catch installation commands

After verifying the release ZIP and its internal checksums, run from the
extracted kit in Bash. Choose an unused instance name. This installs one room's
configuration and imports verified data; it does not start a room:

```bash
pilot_instance=catch
runtime_pin=$(/usr/bin/python3.12 -I -S -c 'import json; print(json.load(open("release.json"))["runtime_sha256"])')
sudo /usr/bin/python3.12 -I -S -B install.py --instance "$pilot_instance" \
  --bundle runtime.zip --sha256 "$runtime_pin" --live-pilot --preset catch-fixed
pilot_release="/opt/poolrotater-v7-pilot/$pilot_instance/releases/v7.2.0-${runtime_pin:0:16}"
pr() {
  sudo /usr/bin/python3.12 -I -S -B \
    "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance "$pilot_instance" "$@"
}
pr check
pr catalog-import --origin https://91.107.237.182:9445/ \
  --ca-file "$PWD/ca.crt" --ca-sha256 60a0dac90855a1b0006062e9820c2144ba2240fc097cd685a6ca0b6fcb445f39 \
  --trust-root "$PWD/bootstrap.json" --trust-sha256 2892f9fda822c2aade05fe5b28f354a57623b66555f75f2fb09fd4c0101f7a74 \
  --mode standard --mode mania4 --mode mania7 --mode catch
pr ratings-migrate
```

The original three catalogs remain the repository's required compatibility
foundation. Importing them does not configure additional rooms. Complete
LIVE_AUTHORIZATION.md locally and use NETWORK_OPERATOR_GUIDE.md for enrollment
and sharing. Then run `pr unit`, `pr ownership-review`, `pr support` and the
documented start once. Keep your authorization and enrollment credentials local.
An existing Standard/Mania installation should use its upgrade path instead of
this new-install example.

## Explicit owner pilot addition

The protected `catch-pilot-prepare` operation accepts JSON on stdin with `alias`,
`sr_min`, `sr_max`, `title` and the current `expected_host_sha256`, obtained from
`catch-pilot-status`. It requires unused numbered capacity, verified activated
Catch data, disabled/stopped bot and network services, no remaining service
processes, and clean stopped ownership. It starts nothing. Existing shared
Discord identities/greetings are reused; the unused alias must have its retained
control channel. No credentials are supplied to this operation.

A root-held transaction preserves the previous configuration. Interrupted
preparation fences startup; retain state and use `catch-pilot-resume`, rather
than reinstalling, deleting state or retrying room creation. Normal startup then
performs fresh provider identity, ownership and capacity checks before MakeRoom.

## Rollback

Before Catch is configured or initializes state, the exact reviewed stopped
runtime rollback to v7.1.20 remains available. After Catch initialization,
legacy code rollback is refused: old agents cannot read pending Catch feedback
or activity safely.

The supported post-pilot rollback is `catch-pilot-rollback` while all owned rooms
are confirmed closed and bot/network services are disabled and stopped. It
removes only the added Catch topology/routing, restores the previous complete
catalog reference and retains current preferences on existing rooms. It archives
Catch ownership/configuration and keeps Catch catalogs, opinions, history and
pending events. Keep the compatible v7.2 controller and hub so that retained
Catch history remains readable and can sync; then start the original rooms
normally. No database rows or credentials are deleted.

## Validation and live acceptance

The release gate requires the exact frozen artifact to pass a fresh full-catalog
Catch installation, retained owner upgrade/addition/rollback, and an installed
two-room signed automatic update from v7.1.20. The latter preserves occupied
matches and tests late joins before admitting an empty controller transition.
Legacy catalog readers and both hub feedback/activity formats are checked.

These installed checks use a separate donor and a simulated provider, without
production credentials or actual rooms. They do not prove osu! gameplay. Owner
startup, Discord/website presentation and player-exercised gameplay are recorded
separately. A running room alone does not establish countdown, completed-match,
skip or feedback acceptance. Full-repository tests were not repeated after the
final localized changes; the validation report lists the remaining fixture and
legacy-migration limits explicitly.
