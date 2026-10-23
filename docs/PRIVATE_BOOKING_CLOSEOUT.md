# Private booking technical closeout — 2026-10-22

Source, exact-package browser, and installed donor validation have passed for
the current candidate. The unpublished host/hub handoff archives, inventories and checksums are verified and reproducible. This closeout records isolated technical evidence;
it does not authorize production deployment or public enablement.

## Current evidence

| Area | Result | Scope limit |
|---|---|---|
| Source tests | **1,577 passed, 1 skipped, 0 failures, 0 errors** unique evidence: 1,564-pass/1-skip fresh-process regression plus 13 distinct cases (6 settings, 3 activity, 4 closeout). All 310 original aggregate failures/errors have case-specific resolution evidence. `final-booking-closeout.xml` has 110 passing cases that overlap those receipts and is not summed again. | The historical aggregate invocation remains non-green; resolved cases were individually replayed and mapped. No actual rooms or production services were used. |
| Browser | Dedicated source Chromium and exact-package Chromium each passed 1 test; exact package passed in 33.950 seconds against the runtime hash below. Coverage includes forms, keyboard/focus, cookie flags, foreign-Origin denial/no CORS, owner/friend access, privacy, settings effects and inactivity estimate freshness. | Synthetic OAuth and observations only; production OAuth and live-room acceptance remain human gates. |
| Compatibility | Pinned v7.2.14 HubServer with new host and new hub with prior-version host both pass isolated TLS tests. The v7.2.14 catalog-rollout merge and exact-version fleet gate are preserved. | Mixed-version hosts may wait at the gate until eligible. No fleet deployment was performed. |
| Settings | Six per-preset tests assert booking settings do not recreate a room or change ruleset mid-session. Player controls use live or next-map application only; no recreation-required setting is exposed. | The separate host-reported recreate branch is not reachable through the player allowlist. |
| Installed upgrade | Current-hash donor upgrade passed. It preserved 63 files and 64 database-table logical rowsets, including synthetic rating/player-history records, and did not start a service. | Isolated donor fixture; no actual provider or rooms. |
| Full donor booking | Current-hash receipt passed 9 native lifecycle cases, 3 simulated browser HTTP cases, 1 authenticated TLS portal-to-hub/agent/coordinator/engine integration, 1 real database-guard case, and all 6 preset previews over 5 full-mode catalogs. `PrivateNetwork=yes`; no real provider/rooms; peak RSS 740,156 KiB. | No production multi-user load test. |
| Candidate | Frozen v7.3.0 runtime SHA-256: `fa15a041bb3ae7469ff668fc306f69b8b61e63c46a4e43dd447a32cbfe1cef92`. Source commit `1a713e9`, parent `6cbf86d`. This includes the v7.2.14 catalog-rollout merge and supersedes `1c4ca...` and `67d545...`. | Host/hub archive checks and independent archive reproducibility passed; final hashes are in `../candidate/HANDOFF.json`. |

The donor admission initially refused because the disposable host did not have
the required 5-GiB reserve. Two old QA exports were freed (292 MB (about 279 MiB)) without
weakening guards or changing the donor journal; the final installed upgrade and
booking qualification then passed.

## Release and rollback boundaries

Both portal and host booking opt-ins remain off. No actual room, production
OAuth app, public route, service, deployment, signing, publication, or feed
promotion was performed.

The supported immediate rollback is feature-off: disable new reservations,
retain hub/host ledgers, and resolve existing sessions with current code. A
binary downgrade is outside this qualification and requires a separately
reviewed stopped transition or consistent closed-state backup; do not blindly
downgrade or overwrite active ledgers. Keep the v7.2.14 exact-version fleet
gate; mixed hosts may wait there until eligible.

Remaining human gates are production identify-only OAuth configuration, an
explicit one-slot live pilot (gameplay, expiry, confirmed closure and reuse),
and separate publication/signing/feed approval. The unpublished candidate handoff and checksums are verified. Passing isolated validation does not
authorize those actions.

## Prepared result and next action

The final unpublished kits are in `validation/private-booking/candidate/`:
`poolrotater-v7.3.0-private-booking-candidate.zip` and
`poolrotater-v7.3.0-hub-candidate.zip`. `HANDOFF.json` records their SHA-256
hashes; `ARTIFACT_VALIDATION.json` confirms internal inventories and identical
independent archive rebuilds. Runtime bytes are pinned above. No feed is signed
or changed. Both handoffs point here, and disposable duplicate candidate archives
were removed while retaining manifests, receipts and the required predecessor.

`../reports/donor-quiescence.json` confirms all six dedicated service MainPIDs
are zero and no process remains under the synthetic instance UID. The v7.2.14
checkout remains clean at `bd995021eb1ef170e8e932baf0c67ded3e9b8c44`.

No independent coding or isolated acceptance blocker remains. The next human
action is to authorize the controlled one-slot pilot and configure its separate
identify-only website OAuth client locally. Publication/deployment and public
booking enablement require their own explicit approvals. Real osu! OAuth and
rooms, production load, Firefox/WebKit and assistive-technology certification
were not executed.
