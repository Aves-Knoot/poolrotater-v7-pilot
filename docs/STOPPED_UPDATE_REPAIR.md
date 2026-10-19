# Existing stopped instance: one repair command

Keep the existing two instance and all its state. This command is intended for
an installed v7.1.13 successor held at controller_start after the signed update
from v7.1.12, with every old room confirmed closed. It refuses live rooms,
unknown ownership, unrelated failures, changed settings or a different runtime.

Run on the operator's existing machine:

```sh
curl -fL https://github.com/Aves-Knoot/poolrotater-v7-pilot/releases/download/v7.1.16/poolrotater-repair-v7.1.16.py -o poolrotater-repair-v7.1.16.py &&
echo 'aa0b58cc474fbba6afce464f064b021af44f66c27c1437db4609ec7ac9232c53  poolrotater-repair-v7.1.16.py' | sha256sum -c - &&
sudo /usr/bin/python3.12 -I -S -B ./poolrotater-repair-v7.1.16.py --instance two --start --resume-agents
```

Expect a download and integrity checks, a stopped upgrade, catalog loading and
one startup attempt. Allow several minutes; do not launch another copy while
it runs. Success ends with ok:true, phase:started, version:v7.1.16, room IDs and
a private reports directory. Start attempts are reported explicitly. Send the
final JSON and both room links, plus whether commands and normal map progression
work. The maintainer can inspect subsequent authenticated hub telemetry.

If it refuses, retain the reports and state and return the final JSON. Do not
reinstall, reauthorize, clear ownership or repeatedly start. An already-running
repaired instance returns phase:already_running without restarting rooms.

No bot/network boot enablement is added. The already installed recovery timer's
enabled choice and local pause policy are retained. Current sharing choices,
custom room names and difficulty ranges remain unchanged.

Runtime SHA256: `d53e96f110107c6ca991067450ba090f9637d55c8861e52d03234450739b43ad`

Launcher SHA256: `aa0b58cc474fbba6afce464f064b021af44f66c27c1437db4609ec7ac9232c53`
