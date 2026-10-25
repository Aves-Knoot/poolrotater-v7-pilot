# Managed automatic updates

Signed automatic updates are part of managed participation. Discovery, verified download, queued, waiting-empty, installed and healthy are distinct states. Compatible worker updates can replace one empty room at a time. A controller update uses the host-wide safe gate; occupied siblings remain open until that gate is met. Explicitly stopped rooms stay stopped. An all-off host needs positive clean ownership evidence; the updater does not create temporary rooms just to permit an update.

Retain journals, receipts and reports after an uncertain or interrupted update. The exact staged successor prepares policy, data and configuration under the stopped owner lease. It preserves saved ceilings, account authorization, Discord settings, catalog IDs, ratings and history. An older unsupported host stays operational and reports missing capabilities; it is not silently treated as administered. Never use an old one-time repair helper as a general upgrade command.

## Existing hosts

R25 v7.3.3 passed both normal installed automatic-upgrade paths: H from v7.3.0
and fresh I from v7.3.2. Their exact receipts verify native automatic
installation, preserved room intent/settings, and post-upgrade health without
operator rescue commands. A separate B pre-admission controller-interruption
case failed before the incoming code could run and is not accepted as recovered.
Proceed with the normal supported update path and disclose this legacy
limitation. Only matching published release assets and the signed feed authorize installation. Existing
operators should retain their installation, OAuth, enrollment and state.

## Staging capacity

After validating the signed artifact and installed baseline, the updater checks the space needed for the downloaded archive and its expanded candidate while preserving the existing 5 GiB free-space reserve. If that read-only check finds a temporary shortage, the candidate stays downloaded and the updater waits about 60 seconds before checking again. No durable update operation is allocated, no files are staged, and no rooms or services are stopped by this deferral. The existing reserve is unchanged. The check uses existing signing pins, installation binding and update consent; it changes no trust or authority.

A successful capacity check is only permission to begin staging. Once the updater records an operation, a staging, queue, or lifecycle error is not automatically treated as a disk-space delay. If the native outcome may have changed, the operation remains held until its exact state is reviewed.

## Unsubmitted stage recovery

The updater can retry the same operation only when it can positively establish that the native queue was never submitted. It verifies that the current installation registration and effective code match the exact parent baseline, there is no matching native job or result, no maintenance gate or pending runtime transition is open, and the fresh room set is unchanged and complete.

Every configured room must be either a healthy running worker with matching process, generation, room, release, roster and ownership-journal evidence, or a positively closed stopped/capacity-paused alias. A closed alias has no room ID or worker PID, creation candidate, pending operation, unknown ownership or reservation; its managed intent and journal both confirm closure, and any retained generation/release identity agrees between them. If a native job already exists, it must be an older, unrelated terminal cancellation bound to the same parent installation, with no commit, service-stop or startup-attempt markers and with drain restoration complete. That prior cancellation is evidence only; it does not authorize retry when any current identity or state differs. On success the existing operation ID is reused. Otherwise the updater holds for review; it does not invent a new operation or guess that a mutation failed.

For a genuinely unknown outcome, retain the exact operation ID and use the existing read-only review action, for example `pilot-update-recover --instance INSTANCE --operation-id OPERATION_ID --decision review`. Review reports native state; it does not retry, cancel, complete or roll back the transaction. Use other recovery decisions only through the documented exact-operation recovery path after the native outcome is known. Do not use recovery as a general retry button.

## Taiko and catalogs

Taiko catalog support is included in R25. H's and I's accepted normal-path
receipts verify the Taiko catalog and the latest full Standard selection
catalog (Standard content `03928b8f8f5999bd7e0e7517007319abe4f4294e63f821479b547dffc3c04af9`;
Taiko content `71ececd64c6b8f3a34deed1008e0f80054c2e6de129add8a327ee2f3d3288f49`).
The separate B failure is retained but not accepted. Catalog checks do not
change an existing room's ruleset or intent. R25 is published. Normal donor acceptance does not establish every live
host upgrade or gameplay acceptance; see V7_3_3_TAIKO_RATINGS.md for the current
production rollout hold and the unlaunched owner Taiko pilot.
