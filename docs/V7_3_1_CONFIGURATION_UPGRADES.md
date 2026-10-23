# v7.3.1 — configuration-safe controller upgrades

Prepared operator build; private handoff, not a signed feed promotion. This
retains the qualified v7.3.0 private-booking foundation and v7.2.14 catalog
rollout. Installing it does not enable website booking, designate private slots,
change enrollment or activate a new catalog.

Normal configuration changes no longer leave an awaiting controller upgrade
held. The successor validates the latest host/settings/catalog bindings,
records a new configuration baseline and repeats fresh empty/idle admission.
A changed private admission gate is released before revalidation. Invalid or
continuously changing settings wait without stopping rooms or overwriting data.
Preferences saved during graceful shutdown are captured while services are
quiescent, before any migration starts. Confirmed native progress also clears
an obsolete generic updater error.

A configuration-only hold can return automatically to observation when the
same predecessor is still registered/running and no commit was admitted.
Prepared migrations, changed runtime identity, uncertain room ownership,
expired release approval and active booking leases retain their safety guards.
This is a controller update, so the final binary switch happens once all rooms
have closed. The operator drain closes each selected room independently as it
empties; it does not wait for simultaneous emptiness before beginning closure.

## Qualification

The affected update/controller/maintenance/recovery/catalog and booking-admission
regression passed 354 cases across 28 fresh processes. Full-size catalogs were
used for the actual protected donor upgrade from v7.3.0 to this build, preserving
63 configuration/credential/catalog/ownership files and 64 logical database-table
rowsets, including invented ratings/player history. An installed real root/service
UID probe validated current settings without overwriting them and exercised the
actual maintenance coordinator with two invented rooms: the first empty room
closed while its occupied sibling stayed running, then the sibling closed when
it became empty. No actual osu! account/rooms or production services were tested.

Exact v7.2.12, v7.2.13, v7.2.14 and v7.3.0 predecessor contracts are embedded.
No database-storage, interpreter or dependency contract changed. Runtime and
operator ZIP checksums are in the private handoff. The full unchanged browser,
calculator and historical repository suites were not repeated for this localized
update; the previous v7.3.0 acceptance remains recorded separately.

## Existing queued v7.2.13 jobs

Installing this build does not authorize editing a held job or deleting its
journal. Use `update-recover --decision review` for the exact operation, and
cancel it through the supported updater only when the old registration remains
current and no matching prepared/committed upgrade has begun. The installer
kit's `DRAIN_UPGRADE.md` and `OPERATOR_LLM_PROMPT.txt` give the full workflow.
Keep the existing instance, OAuth, catalog stores, network/Discord configuration,
ratings/history and desired room intents. No reinstall or sign-in is required.

After verification, the existing automatic updater remains enabled. Public
GitHub publication and signed-feed promotion are separate maintainer decisions;
this handoff does not move a channel or restart a production host remotely.
