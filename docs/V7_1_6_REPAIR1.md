# v7.1.6-repair1: stopped-host installer repair

This replaces the v7.1.5 repair launcher for hosts whose installed systemd update
timer has no MainPID property. Stock Ubuntu24.04/systemd255 returns three unit
properties for a timer; the old helper wrongly required all four service fields.
It refused before stopping agents, upgrading, recovering ownership or starting.
There is no reason to reinstall, reset OAuth, discard state or repeat that old
launcher. Keep its reports as incident evidence.

The corrected helper treats a missing timer MainPID as zero. Loaded services
must still report their actual PID. A malformed/missing service PID, duplicate
property, unexpected property or bad PID refuses. Failure JSON/report includes a
closed `failure_code` and `failed_operation`; arbitrary exception text is not
printed. A refusal retains state and reports and never blindly repeats startup.

Use the new independently checksum-pinned one-command launcher supplied with
this release on the operator's own Ubuntu24.04 x86_64/Python3.12 container. It
installs the exact already-qualified v7.1.6 runtime, not a new bot engine version:

Runtime SHA-256:
`554ac796491d912ee6c94965f09e65897bd3eb2559785540b5304e9a4a15d9ee`

For the existing stopped `two` installation, the maintainer command supplies:

```
--instance two --room-alias room2 --created-room-id 4716142 --start --resume-agents
```

The bot must be stopped and disabled. The helper stops only the existing
auxiliary timer/network agent, refuses an in-flight updater, upgrades the same
instance and preserves OAuth, Discord, titles, tuning, full catalogs, enrollment,
ratings, player history, permissions and opt-outs. Exact v7.1.1–v7.1.5 predecessors
are supported. It uses fresh stopped ownership review and the current journal
digest; if necessary it checks ended room4716142 belongs to the hosting account
before clearing that one unknown creation. A committed clean recovery is not
repeated. An empty owned-room list is not proof of a lost creation's identity.

Startup is attempted once only after clean recovery. Existing two-room settings
are retained. If requested, the enrolled network agent and previously installed
update polling timer resume without changing unit enablement or consent/pauses.
A rerun against a running exact v7.1.6 host reports that state without restarting.
If startup or ownership is uncertain, retain the state/report rather than retry.

Send the maintainer the final JSON, redacted support, and the two room links if
it starts. If it refuses, send failure_code, failed_operation and the report
location; no OAuth codes, secrets or token files are needed. Before/after details
remain in the root-private `/var/lib/poolrotater-v7-repair-reports` tree.

Validation: 18 focused repair cases including the complete workflow with the
reported timer shape, service-PID refusal, unchanged ownership/start boundaries
and private exception reporting. A real isolated root/service-UID v7.1.1→v7.1.6
stopped upgrade with all full catalogs preserved configuration, credentials,
opt-outs, ledger schema/rows and unknown-creation ownership state. Prior v7.1.6
313-case regression and full-catalog v7.1.2/v7.1.5 upgrades remain applicable
because the runtime is byte-identical. No actual osu! dispatch, live room test,
new host controller/worker update or production restart is needed for this kit
repair. The earlier v7.1.5 launcher/archive remain immutable and are superseded
for the stopped repair workflow.
