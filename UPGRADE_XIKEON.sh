#!/usr/bin/env bash
# Same existing `two`, stopped admission only. Leave healthy rooms running afterward.
set -Eeuo pipefail
umask 077
kit="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
case "${1:-}" in
    dev12) old_sha=53a1e98f0057a965cdbbb4d1afa4d1434296f91a7492e0d704fdf409717fae64 ;;
    dev13) old_sha=ad33e8eea1ca5dcd8367d6bce7658ba21e6a84432d271b2dbe979420f6a11ec8 ;;
    *) printf 'Usage: bash UPGRADE_XIKEON.sh dev12|dev13\n' >&2; exit 2 ;;
esac
new_sha=182615d79647a68dfca9f75ed440b77081dee52682a7d1bc737e913fd36bbd96
pilot_release="/opt/poolrotater-v7-pilot/two/releases/v7.0.1-${old_sha:0:16}"
reports="$(mktemp -d "$HOME/poolrotater-dev14-reports.XXXXXX")"
trap 'printf "Stopped at line %s. Retain state and inspect reports: %s. Do not retry startup blindly.\n" "$LINENO" "$reports" >&2' ERR
(cd "$kit" && sha256sum -c SHA256SUMS.txt)
sudo -v
for unit in poolrotater-v7-pilot-two.service poolrotater-v7-network-two.service; do
    pid="$(systemctl show "$unit" -p MainPID --value)"
    state="$(systemctl show "$unit" -p ActiveState --value)"
    enabled="$(systemctl show "$unit" -p UnitFileState --value)"
    if [[ "$pid" != 0 || ( "$state" != inactive && "$state" != failed ) || "$enabled" != disabled ]]; then
        printf 'Refusing: %s must already be stopped and disabled. No service was stopped.\n' "$unit" >&2
        exit 1
    fi
done
pr() { sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" --instance two "$@"; }
capture() { local label="$1"; shift; "$@" >"$reports/$label.json" 2>"$reports/$label.stderr"; }
for action in status support room-settings-status network-support ratings-support ownership-review; do
    capture "before-$action" pr "$action"
done
/usr/bin/python3.12 -I -S - "$reports" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]);load=lambda name:json.loads((p/name).read_bytes())
s=load('before-support.json');assert s['ok'] and s['token_phase']=='ready' and s['authorization_ok'] and s['room_phase']=='stopped' and not s['catalog_recovery_required']
n=load('before-network-support.json');assert n['ok'] and n['enrolled'] and n['account_binding_matches']
r=load('before-ownership-review.json');assert len(r['rooms'])==2 and all(x['result']=='local_clean' for x in r['rooms']) and not r['untracked_active_room_ids'] and not r['untracked_unconfirmed_room_ids']
settings=load('before-room-settings-status.json');assert [(r['alias'],r['preset']) for r in settings['rooms']]==[('room1','standard-auto'),('room2','mania-4k')]
PY
capture upgrade sudo /usr/bin/python3.12 -I -S "$kit/install.py" --instance two \
    --bundle "$kit/runtime.zip" --sha256 "$new_sha" --live-pilot --upgrade-from-sha256 "$old_sha"
pilot_release="/opt/poolrotater-v7-pilot/two/releases/v7.0.1-${new_sha:0:16}"
capture upgraded-settings pr room-settings-status
/usr/bin/python3.12 -I -S - "$reports" "$new_sha" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]);load=lambda name:json.loads((p/name).read_bytes())
u=load('upgrade.json');assert u['status']=='upgraded' and u['operator_state_preserved'] and not u['service_started'] and u['bundle_sha256']==sys.argv[2]
assert load('before-room-settings-status.json')['rooms']==load('upgraded-settings.json')['rooms']
PY
# No tuning step: preserve every saved setting, title and range byte for byte.
for action in check status support room-settings-status network-support ratings-support ownership-review; do
    capture "upgraded-$action" pr "$action"
done
/usr/bin/python3.12 -I -S - "$reports" <<'PY_CHECK'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]);load=lambda name:json.loads((p/name).read_bytes())
assert load('before-room-settings-status.json')['rooms']==load('upgraded-room-settings-status.json')['rooms']
s=load('upgraded-support.json');assert s['ok'] and s['token_phase']=='ready' and s['authorization_ok'] and s['room_phase']=='stopped' and not s['catalog_recovery_required']
n=load('upgraded-network-support.json');b=load('before-network-support.json')
assert n['ok'] and n['enrolled'] and n['account_binding_matches'] and n['directory']==b['directory']
r=load('upgraded-ownership-review.json');assert len(r['rooms'])==2 and all(x['result']=='local_clean' for x in r['rooms']) and not r['untracked_active_room_ids'] and not r['untracked_unconfirmed_room_ids']
PY_CHECK
capture network-start pr network-start
capture start pr start  # Once only. No stop/restart retry loop.
for action in status support network-support ratings-support; do
    capture "running-$action" pr "$action"
done
printf 'Dev14 startup completed. Obtain both room links and leave healthy rooms running.\nReports: %s\nProtected entry: %s\n' "$reports" "$pilot_release/poolrotater_host/pilot_admin_entry.py"
