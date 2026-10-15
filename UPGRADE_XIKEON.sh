#!/usr/bin/env bash
# Same existing `two`, stopped admission only. Leave healthy rooms running afterward.
set -Eeuo pipefail
umask 077
kit="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
case "${1:-}" in
  dev10) old_sha=199e4e228e833d6ce051d76b47817e558f261d0224d2cd9c88e3d7eadf0b7b81 ;;
  dev11) old_sha=54b662a08bbfda597ae8db59f21e78a50f1dbf3fe266e7a8f9ca7991c2cef78f ;;
  *) printf 'Usage: bash UPGRADE_XIKEON.sh dev10  (or dev11 if already upgraded)\n' >&2; exit 2 ;;
esac
new_sha=53a1e98f0057a965cdbbb4d1afa4d1434296f91a7492e0d704fdf409717fae64
pilot_release="/opt/poolrotater-v7-pilot/two/releases/v7.0.1-${old_sha:0:16}"
reports="$(mktemp -d "$HOME/poolrotater-dev12-reports.XXXXXX")"
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
# Explicit owner-authorized gameplay change; preserve all unrelated operator state.
capture tuned-defaults sudo /usr/bin/python3.12 -I -S "$kit/apply_owner_defaults.py" \
    --instance two --expected-user-id 36451 --release "$pilot_release"
for action in check status support room-settings-status network-support ratings-support ownership-review; do
    capture "tuned-$action" pr "$action"
done
/usr/bin/python3.12 -I -S - "$reports" "$kit" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]);load=lambda name:json.loads((p/name).read_bytes())
assert load('tuned-defaults.json')['ok'] and not load('tuned-defaults.json')['services_started']
expected=json.loads((Path(sys.argv[2])/'owner-defaults.json').read_bytes())['presets']
before={r['alias']:r for r in load('before-room-settings-status.json')['rooms']}
for room in load('tuned-room-settings-status.json')['rooms']:
 settings=room['configured_settings'];old=before[room['alias']]['configured_settings']
 for key,value in expected[room['preset']]['settings'].items():assert settings[key]==value,(room['alias'],key)
 assert settings.get('room_name')==old.get('room_name'),'operator title changed'
s=load('tuned-support.json');assert s['ok'] and s['token_phase']=='ready' and s['authorization_ok'] and s['room_phase']=='stopped' and not s['catalog_recovery_required']
n=load('tuned-network-support.json');assert n['ok'] and n['enrolled'] and n['account_binding_matches']
r=load('tuned-ownership-review.json');assert len(r['rooms'])==2 and all(x['result']=='local_clean' for x in r['rooms']) and not r['untracked_active_room_ids'] and not r['untracked_unconfirmed_room_ids']
PY
capture network-start pr network-start
capture start pr start  # Once only. No stop/restart retry loop.
for action in status support network-support ratings-support; do
    capture "running-$action" pr "$action"
done
printf 'Dev12 startup completed. Obtain both room links and leave healthy rooms running.\nReports: %s\nProtected entry: %s\n' "$reports" "$pilot_release/poolrotater_host/pilot_admin_entry.py"
