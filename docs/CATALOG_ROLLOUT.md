# Signed catalog rollout

Catalog data is independent of controller code, credentials, and player ratings.
First deploy the controller release implementing this protocol on every enrolled
host. A stale, offline, mixed-version, failed, or newly enrolled host blocks the
central batch before activation requests are sent. Running workers, not just
controller processes, must report the qualified version.

The hub administrator calls managed_host.catalog_fleet.queue with an explicit
reviewed enrolled-host inventory, required version, mode, exact signed content
ID, and batch UUID. This is a local administrator API, not an unauthenticated
HTTP command. The existing authenticated managed-host channel delivers typed
generation/revision-bound requests. No remote request can carry a path, URL,
trust key, executable, shell command, or force/interrupt option.

Root independently verifies the existing pinned catalog HTTPS origin and TUF
trust, stages only the requested mode, preserves other modes and cumulative
exclusions, and commits the desired catalog reference. Existing workers remain
pinned to their old immutable catalog. No ratings database is imported/replaced.

For a running room, the native controller prepares a candidate worker and checks
its exact target content ID before closing anything. It waits for the existing
continuous empty-room dwell and rechecks emptiness at provider closure. Occupied
or changed rooms defer. Other rooms can keep playing on their old generation.
Deliberately stopped rooms never reopen as a side effect.

An applied receipt includes both catalog_content_id and catalog_set_id. Verify
the receipt and a fresh room heartbeat whose catalog_version matches that set
ID before calling a room migrated. Neither publication, staging, nor receipt
delivery alone proves activation.

Requests can wait up to seven days. Expired, cancelled, stale-generation and
changed-configuration requests are not blindly replayed. Root records a durable
review fence before acquisition/commit; interrupted or uncertain work requires
review. Preparation failure retains the existing worker. No new collector or
osu! attributes requests are started by catalog rollout.

Current Jump classifications are sealed reader evidence, not a table silently
overwritten by catalog import. Changing that generated evidence requires a
separately qualified reader release and old/new catalog compatibility checks.
