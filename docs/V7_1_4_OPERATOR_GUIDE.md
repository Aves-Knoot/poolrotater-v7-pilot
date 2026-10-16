# v7.1.4 full host diagnostics

Running enrolled hosts share all emitted PoolRotater diagnostic categories by
default: native DEBUG through CRITICAL messages, tracebacks, public and private
room messages/commands, early worker stdout/stderr, and systemd journal messages
for this instance's bot, network and updater services. Known credentials and
credential-shaped text are scrubbed before entering the upload queue and again
at the hub. Raw credentials and unrelated host services/files are not uploaded.
Third-party protocol dumps that the bot never enabled remain suppressed.

Private-room diagnostics stay in the authenticated maintainer diagnostics store.
They do not make private rooms, player activity or conversations visible on the
public website. Directory/statistics visibility keeps its existing rules.

## Existing choices and opt-out

The existing `operational_logs` setting controls all diagnostic sharing. Setting
it false (with `public_chat` false) disables capture/upload and purges queued
diagnostics. Existing explicit opt-outs survive the upgrade. The historical
`public_chat` name now controls chat/command sharing in every room, including
private rooms and service output; false keeps operational diagnostics but omits
chat/commands. The documentation makes this expanded scope explicit.

Use the existing protected administrator entry for the installed instance and
`network-reporting-configure`, supplying the complete four-choice JSON via its
documented stdin interface. `share_statistics` and `consume_statistics` remain
separate choices. `network-reporting-support` reports local queue, loss counters
and the last external-journal collection receipt.

## Collection and limits

Native diagnostic files are sanitized at emission and collected by the independent
network agent. The source sink now includes DEBUG and drains up to four numbered
rotations before the active file. Large messages are scrubbed in full before
being split, preserving long tracebacks without splitting a credential first.
Credential metadata/environment changes invalidate the redaction cache immediately;
there is no time-only window that trusts stale credential values.

The existing root updater/maintenance tick reads only the three fixed instance
systemd units. It never grants the network agent journal/root/provider access and
never makes an osu! request. This collection also runs when automatic installation
is paused or disabled while the maintenance timer remains running. An explicitly
stopped maintenance timer cannot collect new journal/worker-stderr records;
native application logging continues. No separate root service is installed.

Journal pages have bounded runtime/size and a cursor committed atomically with
their sanitized records. A vacuumed/volatile journal resumes from the last
committed timestamp with bounded cursor deduplication and a visible coverage
notice. Missing historical journal content cannot be reconstructed. Worker sources
are registered before launch so failures before normal logging are captured.
Collection errors do not stop rooms or change the updater's installation decision.

Logs use the existing compressed, acknowledged, retryable upload protocol. The
local pending queue is128MiB with seven-day retention; native sanitized sinks
rotate at8MiB with four backups. The existing hub defaults remain14days,64MiB
per host and512MiB overall. Quota/retention losses are counted; all categories
does not mean unlimited storage or a guarantee against machine/disk loss.

## Upgrade and acceptance

This changes shared host/controller code. The signed path follows v7.1.3; the
controller waits until all configured rooms are safely empty before its normal
coordinated switch. Occupied rooms are not forced closed. The v7.1.3 worker
responsiveness improvements remain included. Paused updates and opt-outs stay
effective. Stopped hosts are not remotely started by publication.

For a manual stopped upgrade from v7.1.2, use this release's installer kit and the
retained repair procedure, preserving the same instance, state, credentials,
catalogs and tuning. Do not perform a fresh install over an existing instance.
Older hosts follow their existing supported upgrade chain first.

After activation, inspect `network-reporting-support` and the maintenance service
journal. Useful maintainer feedback is the redacted support output and test times;
credentials and raw token/environment files are never needed. Receiving new
DEBUG, service-journal and stderr records centrally is operational acceptance;
publishing an artifact alone does not prove remote installation or upload.
