# Private booking acceptance matrix

This matrix maps the ten goals in the attached objective and
[`PRIVATE_BOOKING_DEVELOPMENT_PLAN.md`](PRIVATE_BOOKING_DEVELOPMENT_PLAN.md) to
the current isolated implementation and evidence. It separates focused test
receipts from complete acceptance. The project is not published or deployed;
OAuth registration, a live pilot, actual room testing, and public enablement
remain human-gated and pending. Unique source evidence is **1,577 passed,
1 skipped, 0 failures, 0 errors**: the 1,564-pass/1-skip base regression plus
13 distinct cases (6 settings, 3 activity, 4 closeout), without summing
overlapping focused receipts. All 310 cases from the original broad run have
case-specific resolution evidence, but that historical aggregate is not called
green. Source commit is `1a713e9` (prior `6cbf86d`). Frozen candidate runtime
SHA-256 is `fa15a041bb3ae7469ff668fc306f69b8b61e63c46a4e43dd447a32cbfe1cef92`,
replacing `1c4ca...` and `67d545...`. Exact-package Chromium and installed
upgrade preservation passed; full-system donor booking/catalog qualification
also passed.

| # | Objective section | Implementation in this checkout | Evidence and limits |
|---|---|---|---|
| 1 | Establish the implementation contract | `poolrotater_host/bookings/contracts.py`, `settings.py`, `lifecycle.py`, `selection.py`; [`PRIVATE_BOOKING_DEVELOPMENT_PLAN.md`](PRIVATE_BOOKING_DEVELOPMENT_PLAN.md) records protocol, role, lifecycle and compatibility contracts. | `tests/test_booking_lifecycle.py`, `test_booking_settings.py`, `test_booking_selection.py`, and `test_booking_repository.py` cover the pure state machine, preset inventory, selection rules and wire/repository boundaries. The matrix records current evidence; it does not replace final primary review of every requirement. |
| 2 | Durable reservations and capacity allocation | `bookings/repository.py`, `hub.py`, `eligibility.py`, `admission.py`, and `local.py` implement atomic slot/requester allocation, immutable request/session bindings, capacity accounting, cooldowns and held uncertain state. | `tests/test_booking_repository.py`, `test_booking_eligibility.py`, `test_booking_local.py`, `test_booking_exchange.py`, and `test_booking_agent.py` cover idempotency, quota, request limits, replay, capacity, and no-show handling. These suites pass in the fresh-process source regression. The earlier aggregate invocation remains historically non-green; all of its recorded failures/errors were resolved and individually evidenced. |
| 3 | Host-enforced session lifecycles | `bookings/lifecycle.py`, `local.py`, `agent.py`, `native.py`, `game.py`, `update_guard.py`, and `managed_host/operations.py` implement durable timers, authoritative roster/gameplay observations, closure and update holds. | `tests/test_booking_lifecycle.py`, `test_booking_native_engine.py`, `test_booking_agent.py`, `test_booking_game.py`, `test_booking_update_guard.py`, and `test_booking_update_admission.py` cover claim/inactivity timing, long maps, restart state, clock rollback, closure confirmation and update admission. `test_portal_authenticated_hub_exchange_and_native_engine_lifecycle` verifies a playing friend and long map hold the session beyond the ordinary 1,800-second inactivity deadline, then allows closure after completion and continuous emptiness. Current-hash donor qualification passed 9 native lifecycle cases and 1 authenticated TLS end-to-end case; no live host or room was used. |
| 4 | Session-specific permissions | `bookings/repository.py`, `local.py`, `game.py`, `web.py`, and the session serializer enforce Owner, Controller and Player boundaries, roster-bound grants, revocation and execution-time checks. | `tests/test_booking_repository.py`, `test_booking_game.py`, `test_booking_browser.py`, `test_booking_browser_integration.py`, and `test_booking_local.py` cover immutable user IDs, role scope, revocation, rejoin persistence and stale session commands. The dedicated Chromium scenario separately tests owner/friend account access with synthetic identities. Actual human-user/live-room acceptance remains pending. |
| 5 | Safe gameplay configuration and map filters | `bookings/settings.py`, `selection.py`, `worker.py`, `preview.py`, and `bookings/static/{index.html,app.js,app.css}` provide schema-driven controls, staged Apply, revision/cooldown checks, bounded previews and qualified filters. | `tests/test_booking_settings.py`, `test_booking_selection.py`, `test_booking_worker.py`, `test_booking_preview.py`, and `tests/bookings_dom_fixture.js` cover validators, filters, apply receipts, cache behavior and UI. The application-contract suite passed 19 cases, including six preset cases of `test_booking_settings_do_not_recreate_room_or_change_ruleset_mid_session`. The player allowlist exposes live and next-map changes; no recreation-required controls are offered. Exact-hash donor previews passed for all six presets using all five full-mode exports. OD/BPM are unavailable; AR is unavailable for Mania and current Catch catalogs; length and supported AR filters are documented in `PRIVATE_BOOKING_OPERATIONS.md`. |
| 6 | Website authentication and private panel | `bookings/auth.py`, `oauth.py`, `web.py`, `server.py`, `portal_entry.py`, and `bookings/static/*` implement identify-only OAuth flow, same-origin CSRF-protected APIs, private session views, invite joining and owner/controller controls. | Unit/loopback suites cover local configuration, simulated OAuth, cookies/CSRF, invitations, and APIs. Dedicated source Chromium and exact-package Chromium each passed `test_real_browser_booking_lifecycle_and_stale_draft_protection` (1 test each; exact-package run 33.950 seconds against `fa15...`), covering routes, forms, keyboard/focus, cookies, foreign-Origin/no-CORS, roles/privacy, settings effects and inactivity estimate freshness. Production OAuth and actual human-user sign-in remain separate live-pilot gates. |
| 7 | Discord and website availability displays | `bookings/presentation.py`, `panels.py`, `game.py`, `repository.py`, and the portal public slot view provide a minimal public projection with freshness separate from lifecycle state. | `tests/test_booking_presentation.py`, `test_booking_panels.py`, `test_booking_bridge.py`, `test_owner_discord_panel.py`, dedicated Chromium public-response/activity checks, and fresh reporting regression cover privacy-safe projection, held stale state, coalescing and private/public separation. No public site/wiki or Discord service was changed. |
| 8 | Compatibility, recovery and upgrade handling | `bookings/contracts.py`, `agent.py`, `discord_relay/bridge.py`, `native.py`, `policy.py`, `update_guard.py`, `rolling/storage.py`, `rolling/worker.py`, and hub package/bootstrap wiring keep private authority versioned and preserve installed state. | Fresh source regression passes rolling/provider/booking compatibility suites; all 310 original fixture/resource failures have case-specific resolution evidence. `test_v7214_hub_keeps_legacy_routes_and_refuses_booking_exchange` and `test_new_hub_accepts_prior_host_heartbeat_and_managed_report_without_booking_capabilities` exercise both version directions. The v7.2.14 catalog rollout and exact-version fleet gate are preserved; mixed hosts may wait there. Current-hash donor upgrade preserved 63 files and 64 database-table rowsets, including seeded rating/history; the service remained stopped. |
| 9 | Validate at major milestones | Focused booking, native, rolling, reporting, browser, package and UI fixtures are kept alongside the source, with receipts under `../reports`. | Unique source evidence is **1,577 passed, 1 skipped, 0 failures, 0 errors** (base fresh-process 1,564 passed/1 skipped plus 13 distinct newer cases). All **310** original failure/error cases have case-specific resolution evidence in `../reports/REGRESSION_RECONCILIATION.json`; the original aggregate is not called green. The ordinary browser-module skip is separately covered by source and exact-package Chromium passes. The focused 110-case closeout receipt overlaps these tests and is not added again. |
| 10 | Prepare controlled pilot and release handoff | Drafts are in `PRIVATE_BOOKING_OPERATIONS.md`, `PRIVATE_BOOKING_UI_VALIDATION.md`, `PRIVATE_BOOKING_PILOT.md`, and `PRIVATE_BOOKING_WIKI_DRAFT.md`; `pilot/bookings-portal.service.template` is a review artifact only. | Frozen v7.3.0 runtime hash is `fa15a041bb3ae7469ff668fc306f69b8b61e63c46a4e43dd447a32cbfe1cef92`, superseding `1c4ca...` and `67d545...`. Exact-package browser and installed donor upgrade/browser/native/TLS/catalog qualification passed. Candidate host/hub archives, inventories and checksums passed independent reproducibility validation. Signing, publication, OAuth configuration, deployment and actual room pilot remain separate human gates. |

## Receipt interpretation

The current source regression summary is `../reports/final-regression/summary.json`;
the original-case mapping is `../reports/REGRESSION_RECONCILIATION.json`. The
dedicated source Chromium receipt is documented in
`PRIVATE_BOOKING_UI_VALIDATION.md`. Earlier focused logs include
`../rolling-regression-tests.log`, `../reporting-regression-tests.log`,
`../compatibility-focused-tests.log`, and `../update-admission-tests.log`.
Receipts overlap and should not be summed. The original aggregate run remains
non-green, but all 310 recorded failures/errors now map to case-specific
resolution evidence. Current `../reports/donor-booking.json` is bound to
`fa15a041...` and passed 9 native lifecycle cases, 3 simulated browser HTTP
cases, 1 authenticated TLS system integration from portal through
hub/agent/coordinator/engine, 1 real database-guard case, and all 6 preset
previews across 5 full-mode catalogs. It ran with `PrivateNetwork=yes`, used no
real provider or rooms, and peaked at 740,156 KiB RSS. The installed upgrade
receipt records 63 files and 64 database-table rowsets preserved, including
synthetic ratings/player history, with `service_started: false`. An earlier
admission refusal reflected the required 5-GiB reserve; two old QA exports were
removed (292 MB (about 279 MiB) freed) without weakening guards or changing the donor journal
before the final upgrade. No production multi-user load test was performed.

Booking development changed no production room, provider operation, service, website route, OAuth client, credential or public booking feature. Separately, the user explicitly authorized the existing native hot-configuration interface to apply the two-hour recreation interval across12 owner/community slots; its verified receipt is retained outside development. The next human
actions, only after review, are to decide whether to configure an identify-only
OAuth client and approve a controlled private pilot. Publication and public
enablement require separate approval.
