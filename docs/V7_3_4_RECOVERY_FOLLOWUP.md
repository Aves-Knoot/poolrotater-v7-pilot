# v7.3.4: catalog transfer and independent recovery follow-up

Status: revision 1 is unaccepted and retired from donor execution. Its retained runtime SHA-256 is
`3048267e3f0cb5228604507167463b823ba069b4ad2d4fcfb6af3434c5572960`.
A revised candidate is being prepared; nothing from v7.3.4 is published or queued.
Only the exact published v7.3.3 predecessor is newly admitted. The v7.3.2
qualification peer is retained for isolation checks and is not an advertised
v7.3.4 predecessor. The v7.2.6 archive is a historical recovery-reader fixture,
not evidence of a tested direct upgrade.

The live v7.3.3 rollout completed on the owner, Xikeon and Strohm. Strohm's
retained Standard download resumed successfully using the installed native
completion. Its independent helper was still pinned to v7.2.6, whose older
manifest reader refused the newer contract before dispatch. This follow-up
addresses that demonstrated future recovery gap without changing room settings.

## Changes

- Managed catalog staging retries known transient transport failures at most
  three times, reusing the same verified cache and partial download. Hash,
  schema, certificate, identity, custody and policy failures stay refused.
- After a verified completed upgrade, managed reconciliation can refresh only
  the independent helper's registered-runtime pin. Exact installation identity,
  complete committed lineage, current inventories, unchanged launcher bytes,
  settled transactions and root-owned policy are required. Existing disabled
  and recovery-permission choices are preserved.
- Pin refresh serializes with both the direct installer and the recovery policy
  lock. Contention defers without changing config; no room start, update grant
  or room mutation is added by the refresh.
- Restoration and preparation diagnostics report finite known reasons, bounded
  public catalog/storage state and allowlisted config path names. They preserve
  primary failures and expose no arbitrary exception contents or secrets.

## Validation

The affected integration matrix passed 298 tests in 76.641 seconds. The final
pin/managed subset passed 9 tests after the unsafe-directory and installer-lock
review fixes; these overlapping counts are not added together. A separate real
signed TUF/HTTPS full-catalog fixture passed with a truncated Standard response,
verified HTTP range resume, one staged candidate and an unchanged current
reference. This is transport-component evidence, not installed-update evidence.

The retained revision-1 artifact is reproducible, classifies as a controller upgrade, and
keeps storage and environment fingerprints unchanged. All 322 nongenerated
payload files match the reviewed source. Exact installed automatic upgrade
qualification is pending; publication and safe empty-room rollout require its
positive native commit and post-health evidence. The previous failed legacy
pre-admission controller-fault case remains disclosed, not counted as recovered.

## Remaining work

1. Qualify the exact v7.3.3 to v7.3.4 installed automatic path with full catalogs
   and invented providers. Preserve room intent, valid waiting config changes,
   occupied rooms/bookings, helper retry, native commit and health checks.
2. Verify the stale recovery pin advances through guarded installed code and
   that independent recovery still operates afterward.
3. Publish matching immutable source/artifacts and the signed update only after
   acceptance; queue safe room draining and observe actual client adoption.
4. Keep owner L2 reserved; the selected Taiko pilot is owner L3. Actual Taiko
   gameplay is verified: three completed scored matches and one earlier abort;
   this does not claim every command or gameplay edge case has been exercised.

## Maintenance qualification finding and revised candidate

The first installed donor run did not pass. After a separate observer timed
out during SCORING, its native job entered an admission hold because a room
reported an uncertain maintenance lock. No commit or registration change
occurred. The original referee reply was not retained, so its precise cause
is unproven. HTTP pacing counters do not establish a referee mutation outcome.
All failed-run receipts and the held native job were retained. Only this
disposable run's units were gracefully stopped; production was unaffected.

A reproduced latent problem is now fixed in the revised source: finite local
referee refusals that occur before dispatch have a typed no-dispatch result.
Maintenance may defer and retry these known refusals while retaining the
room's fence; a genuinely unknown mutation still requires reconciliation and
is never blindly replayed. Wrong-room authority and malformed remote error
payloads remain refused. Lock, roster, connection, membership and quiescence
guards are covered. Existing wire messages and the four maintenance status
fields remain compatible.

The maintenance integration milestone passed 223 tests in 413.76 seconds.
After adding the stale-membership guard, 45 relevant focused tests passed in
299.87 seconds; these overlapping counts must not be added together.
A fresh installed qualification will use a new v7.3.3 primary, the existing
v7.3.2 cold preservation peer, full catalogs and bounded QA-only call tracing.
The cold peer is not evidence of live mixed-version wire operation. Publication
requires the revised exact artifact's native commit, post-health and scheduled
recovery-pin refresh receipts; revision 1 supplies none of those.
