# v7.1.14 independent recovery preview

A failed controller update could close its rooms and stop network reporting,
then leave the operator to retrieve logs and run another recovery command.
This preview adds an independent recovery timer that retains a separate code
pin and can report and assist recovery while both services are stopped.

The maintainer can request sanitized diagnostics, continuation of an already
approved exact update, or startup of an exited failed service with confirmed
closed ownership. Requests are authenticated, expiring, bound to exact local
state and durably recorded before dispatch. Running services and uncertain room
ownership are refused. Operator stop, pause and opt-outs take priority.

Future automatic controller updates execute the integrity-checked staged
successor entry rather than the installed predecessor's admission code. The
recovery code can be staged without modifying an unfinished transition, runtime
registration, service bootstrap or player state. Retention keeps its pin.
Backup/restore retains recovery history without reviving old requests.

This is a recovery-bootstrap preview. It is not placed on the automatic release
feed until installed transition acceptance is complete. The one-time helper
installs only the independent channel; preserve the existing instance and state.
See INDEPENDENT_RECOVERY.md for the grant, checks and local opt-out controls.

Owner lobbies are excluded from manual restart. A release asset alone is not
evidence that the remote recovery agent is installed or that rooms are running.
