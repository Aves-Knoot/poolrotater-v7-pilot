# v7.3.4: catalog transfer and independent recovery follow-up

Status: candidate frozen, unpublished and not queued. Runtime SHA-256:
`3048267e3f0cb5228604507167463b823ba069b4ad2d4fcfb6af3434c5572960`.
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

The frozen artifact is reproducible, classifies as a controller upgrade, and
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
   gameplay observation remains separate from technical startup/selection proof.
