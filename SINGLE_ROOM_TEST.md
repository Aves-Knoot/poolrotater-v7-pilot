# Full catalogs and a supervised one-room test

Existing dev5 tester: first upgrade using REPAIR_UPGRADE.md section1 and2A. Keep
the committed recovery; do not repeat ownership-clear or authorize. Start only
within the agreed one-room observation window, with consenting participants.

## 1. Confirm complete catalog download and activation

Work in the extracted kit directory. Set pilot_release to the exact release path
printed by the upgrade/installation. The same signed origin and trust are retained.

```sh
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first catalog-import --origin https://91.107.237.182:9443/ \
  --trust-root bootstrap.json \
  --trust-sha256 60d2846968d842c9838679ee79536d5963c219e08273eccaca0fc374434b207d \
  --ca-file ca.crt \
  --ca-sha256 20cc74596892f2db0be5e8954ae1f313ca31fce41bf0ea7a6ab59d6fe326025b \
  --mode standard --mode mania4 --mode mania7
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first status
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first support
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first ownership-review
```

An existing complete catalog set can be reused through the verified cache. This
command does not recollect/analyze maps, reauthorize or start a service. Check
catalog_rows: standard/catalog.db/beatmaps105510 and dt.db/dt_maps105137;
mania4/catalog.db/maps19335; mania7/catalog.db/maps4348, and all companions listed
in README. Catalog recovery must be false. Room phase must be stopped, committed
recovery remains committed, fresh review reports local_clean. A stale systemd
failed state with MainPID0 is historical, as explained in the repair guide.
Support start_allowed:false/start_allowed_reason means local status has not
performed startup admission; it does not mean committed recovery was undone.
If any command refuses, send its redacted failure_code/failure_stage and support,
retain all state, and hold startup. Do not clear journals or create a replacement
instance. A pending catalog transaction requires the documented explicit
catalog-recover command and a successful recheck before startup.

## 2. Start once and find the room

Record timestamp/timezone and support before start. Agree no conflicting owned
active rooms. Then explicitly start once:

```sh
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first start
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first status
```

Expect one room and room_phase:running. Look under osu!'s All/Owned room filters;
a Public-only filter may hide it. The title begins PoolRotater pilot and the
standard-auto title includes Auto Difficulty. Verify the hosting account locally.
To print only the local room aliases and room links, without private journal data:

```sh
sudo /usr/bin/python3.12 -I -S -c 'import json; j=json.load(open("/var/lib/poolrotater-v7-pilot/first/state/rolling/ownership.json")); print(json.dumps({a:("https://osu.ppy.sh/multiplayer/rooms/"+str(s["room_id"]) if s.get("room_id") else None) for a,s in j["rooms"].items()}))'
```

Share the room link with the maintainer/consenting players. The hosting referee
account should not join as a player; another account can join. Never repeat start
because the room is missing from a filter. On uncertain startup, retain state,
return support/errors and stop for review; an upstream room may exist.

## 3. Gameplay and clean shutdown

Use ONE_ROOM_CHECKLIST.md: join and play3–5 maps, help/queue/skip/.g/.b, countdown,
ready/unready/AFK behavior with at least two players, language, map/song spacing,
normal joins/leaves. Players download beatmaps/audio through their own clients.
Do not deliberately crash the service or inject ownership uncertainty in this test.

At the end stop explicitly:

```sh
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first stop
sudo /usr/bin/python3.12 -I -S "$pilot_release/poolrotater_host/pilot_admin_entry.py" \
  --instance first support
```

Confirm the room closed upstream and no dedicated worker/referee processes remain.
Capture support after stop. If stop is uncertain, preserve state and report it;
never delete ownership state, force-clear recovery or blindly create another room.

Return upgrade/catalog results, before/after support, room link, start/stop times
and timezone, maps played, checklist items actually exercised, command/countdown
results, errors and concise usability notes. Redact account/client IDs, credentials,
authorization URLs/codes, tokens, private state files and private chat. Reports are
sent manually; no test feedback is automatically uploaded.
