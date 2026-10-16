# v7.1.1 — shared defaults, automatic delivery and complete activity totals

v7.1.1 retains the exact v7.1.0 gameplay engine, storage schema, catalogs and
27 dependency wheels. The signed release is a controller update: it waits for
all configured rooms of an installation to be healthy, empty and idle, completes
the private admission/drain checks, then performs one coordinated restart. A
late join cancels admission. It never forces occupied rooms closed.

## Defaults and opt-outs

Missing reporting profiles now enable public-room statistics sharing, cached
shared-statistics consumption, sanitized operational diagnostics and sanitized
public-room chat diagnostics. Existing explicit profiles, including all-false
v7.1.0 profiles, remain unchanged. Private and unknown-visibility room content is
excluded. Diagnostics remain private to the hub administrator, with the existing
scrubbing, bounded queues and retention. Historical player data is not imported
automatically.

An enrolled host with no ratings-sharing profile defaults to contribution, and a
missing ratings-reader choice defaults to verified network ratings. Existing
explicit sharing=false and independent-reader choices stay unchanged. Server
contribution grants, enrollment, account binding and epoch recovery remain
required; defaults cannot grant central authority.

New network configuration defaults to public directory listing when that field
is omitted. Existing directory choices and invitation bindings remain unchanged.

New updater configuration defaults to the signed pilot channel with downloads,
installation, controller maintenance and private update reporting enabled.
A local `network-start` on an enrolled v7.1.1 installation initializes and starts
a missing updater using the packaged public authority/TLS pins. Existing trust,
policies and stopped/paused updater intent remain untouched; incomplete bootstrap
state requires review. No updater starts a stopped bot just to update it.

The first v7.1.0 storage migration still requires the supported one-time stopped
upgrade. The signed v7.1.1 feed supports the exact published v7.1.0 baseline;
there is no automatic path across the old v7.0 storage boundary.

## Website and documentation

The homepage/live occupancy and Lobby activity total include owner and listed
community public rooms, with duplicate room IDs removed. These are room occupancy
counts including spectators, not a deduplicated unique-person statistic. The four
native owner history series remain intact. Combined history starts with actual
network observations; earlier community occupancy is never fabricated. Stale,
unknown or truncated network coverage leaves a gap instead of reporting zero.

Hosting, operation, opt-out and upgrade guides now describe the current release.
Documentation verification uses the configured owner room set, supporting the
intentionally disabled fourth room. A separately hash-bound documentation review
identifies the newest reviewed release without claiming it is already running.

## Validation and limits

See V7_1_1_VALIDATION.md for exact runtime hash, affected test results, protected
root/service-UID upgrade and publication evidence. Actual operator installation
and real-player soak remain observations to collect after automatic delivery.
