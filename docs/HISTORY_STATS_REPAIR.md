# Shared historical statistics repair — v7.1.9 candidate

Implemented in v7.1.9. Release publication, empty-room activation, historical capture and hub acknowledgment are separate stages; consult operator support for current progress.

The hub is receiving new community telemetry and ratings. Owner legacy cumulative playtime and tracked PP have not been backfilled. A missing shared username also receives a repeated syncing message after the agent has already completed an empty lookup.

## Acceptance

- Import explicitly reviewed public legacy PP and cumulative time using the existing activity event protocol, native source keys and source cutoffs.
- Preserve hidden-user choices, host opt-outs, mode separation and same-room deduplication. Withhold cumulative totals for players with excluded or unreviewed room participation; do not guess private contributions from scalar totals.
- Resume bounded background capture after restart or queue pressure, without touching rooms, provider OAuth, native gameplay data or service ownership. Historical review is installation- and consent-bound; changing the review or consent requires a fresh review.
- Distinguish completed absent/hidden name lookups from requests still waiting on the hub. Negative cache expires so subsequent imports become visible. Player commands continue to perform only local cache operations.
- Preserve the prepared Stream/Jump/Aim mix, separate DT controls and .t alias.
- Build from the exact published v7.1.8 artifact with an explicit changed-file inventory. Requalify controller compatibility and supported upgrade paths before publication; the earlier three-file worker-only v7.1.9 candidate is not this release.
- Qualify representative full catalogs and reviewed history, cross-host personal stats and website owner-baseline deduplication. Do not interrupt occupied rooms.

## Current implementation

- Reader keeps bounded short-lived negative name lookups; hidden and absent names have the same public result.
- .stats no longer calls a validated empty response syncing.
- Collector supports conservative per-player exclusion for mixed private/public legacy ledgers, retains historical cursors at EOF, and reports capture completion.
- Independent reporting agent processes only an explicit bound config/activity-history.json manifest in small chunks. Queued means captured locally, not yet acknowledged by the hub. Normal durable uploads provide acknowledgment.

## Validation

402 affected tests, 2 full-catalog native cases, and 1 protected v7.1.8 → v7.1.9 installed upgrade pass against the exact frozen candidate (405 total, no skips). Real isolated HTTPS import/query, cross-host lookup, privacy/exclusion, queue/restart recovery and existing website owner-baseline deduplication are covered. Complete repository discovery and new actual upstream gameplay were not repeated.

Runtime SHA256: `6e0d7260a803cdded105b086a04c1740e1dd2e389d6bd85aa6ad552fbedcc601`. Evidence: `validation/v7.1.9-history-stats/qualification.json`.

The fixed preserved migration backup can be selected explicitly with `source: "migration-backup"` and `source_sha256`; current hidden choices still apply. Owner public-room review is prepared root-private outside the development source, excluding three documented private test rooms. A queued history receipt confirms local capture only; hub acknowledgment remains a separate acceptance check.

## Delivery and acceptance

The source operator installs an independently reviewed, installation-bound manifest using the checksum-verified kit’s `install_history_review.py`. See [operator instructions](V7_1_9_OPERATOR_GUIDE.md). This does not enable sharing or start services. The reporting agent captures and uploads bounded chunks with no provider or room operations. An empty lookup may remain unavailable until the historical backlog reaches the hub and its two-minute negative cache expires.

Healthy opted-in hosts discover the signed controller update and wait for all configured rooms to be empty together. Preserve explicit opt-outs, update pauses and interrupted-recovery state. Do not use the earlier unpublished style-only worker candidate as this combined update.

Acceptance requires observing actual hub baseline/PP ingestion, cross-host lookups, hidden-player behavior and unchanged website owner-source totals after delivery. Offline qualification does not claim upstream gameplay acceptance or completion of a live import.
