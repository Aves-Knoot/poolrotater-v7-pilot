# Central community-display hub activation and rollback

This is a **staged procedure for a later authorized maintenance window**, not an
instruction to activate during the current display-development task. No activation,
service stop or production-state migration was performed to prepare this document.

The running owner and Xikeon gameplay services stay running. Only the central
registration/ratings hub pauses briefly. Directory/rating requests may retry during
that pause; existing rooms do not depend on this listener to play. Keep the owner
bot, operator network agents, Caddy, catalog listeners and all other services running.

## Exact reviewed identities

| Item | Reviewed identity |
| --- | --- |
| Hub service | `poolrotater-v7-hub.service` |
| Hub account | `poolrotater-hub` |
| Runtime prefix | `/opt/poolrotater-v7-hub` |
| Private state | `/var/lib/poolrotater-v7-hub` |
| Protected entry | `/opt/poolrotater-v7-hub/bootstrap.py` |
| Prepared archive | `validation/community-display/poolrotater-community-display-hub.zip` |
| Archive SHA-256 | `a478a09170e57b4fd02eb81f198fdc216d7f99742c974bf8189c26a08a413dab` |
| New inventory SHA-256 | `c34fce96ba22d2c45c481d25f54f3efa6572b979f888eaacb99ed4da663ec36f` |
| Current/rollback inventory SHA-256 | `cc4ddb3d733238f14ad0051ed39168b1109469e609ca8b1d9e691fd6f133497d` |
| Bootstrap SHA-256, unchanged | `42eaa1064563ca1935f588fb614b34c5e777364d345f61f5c64e4e2bd1e447e8` |
| Public TLS CA | `/etc/poolrotater-community/ca.crt` |
| Public TLS CA SHA-256 | `60a0dac90855a1b0006062e9820c2144ba2240fc097cd685a6ca0b6fcb445f39` |
| Existing origin | `https://91.107.237.182:9444` |

There is **no database schema migration**. The same `network.sqlite3`, current WAL,
ratings policy, enrollment/credential bindings, contribution grants, receipt history,
server epoch, TLS pair, service user, unit and configuration remain authoritative.
The protected bootstrap checks complete code inventory before importing runtime code.

These pins apply to this prepared archive only. If any changes or identities differ,
stop and qualify a new artifact/procedure rather than substituting a digest.

## 1. Stage and verify code without stopping anything

Save the following snippet as a root-owned private file, for example
`/root/poolrotater-community-hub-stage.py`. It extracts only the closed, independently
pinned runtime inventory to a fresh root-owned sibling of the existing prefix.
It does not run a service or touch the hub database.

```python
import hashlib, json, os, pathlib, re, shutil, stat, sys, tempfile, zipfile

ARCHIVE_SHA = 'a478a09170e57b4fd02eb81f198fdc216d7f99742c974bf8189c26a08a413dab'
NEW_SHA = 'c34fce96ba22d2c45c481d25f54f3efa6572b979f888eaacb99ed4da663ec36f'
OLD_SHA = 'cc4ddb3d733238f14ad0051ed39168b1109469e609ca8b1d9e691fd6f133497d'
BOOTSTRAP_SHA = '42eaa1064563ca1935f588fb614b34c5e777364d345f61f5c64e4e2bd1e447e8'
LIVE = pathlib.Path('/opt/poolrotater-v7-hub')
STAGE = pathlib.Path('/opt/poolrotater-v7-hub-display-dev11.staged')
BACKUP = pathlib.Path('/opt/poolrotater-v7-hub-pre-display-dev11')


def secure(path, directory=False):
    for item in (path, *path.parents):
        info = item.lstat()
        assert not item.is_symlink() and info.st_uid == 0 and not info.st_mode & 0o022
        if item != path or directory:
            assert stat.S_ISDIR(info.st_mode)
        else:
            assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1


def manifest(raw, expected):
    assert len(raw) <= 1048576 and hashlib.sha256(raw).hexdigest() == expected
    records = json.loads(raw)
    assert set(records) == {'format', 'files'} and type(records['format']) is int and records['format'] == 1
    assert type(records['files']) is dict and records['files']['bootstrap.py'] == BOOTSTRAP_SHA
    for name, digest in records['files'].items():
        p = pathlib.PurePosixPath(name)
        assert name == p.as_posix() and not p.is_absolute() and '\\' not in name
        assert p.parts and not any(part in ('.', '..') for part in p.parts) and name != 'inventory.json'
        assert type(digest) is str and re.fullmatch('[a-f0-9]{64}', digest)
    return records['files']


def payload(archive):
    secure(archive)
    assert archive.stat().st_size <= 64 * 1024**2
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == ARCHIVE_SHA
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        assert len(names) == len(set(names)) and sum(i.file_size for i in z.infolist()) <= 64 * 1024**2
        for item in z.infolist():
            assert not item.is_dir() and item.file_size <= 1048576
            assert stat.S_IFMT(item.external_attr >> 16) in (0, stat.S_IFREG)
        raw = z.read('runtime/inventory.json')
        files = manifest(raw, NEW_SHA)
        assert set(names) == {'runtime/inventory.json', *('runtime/' + name for name in files)}
        data = {name: z.read('runtime/' + name) for name in files}
        assert all(hashlib.sha256(data[name]).hexdigest() == digest for name, digest in files.items())
        return raw, data


def verify_tree(base, expected):
    secure(base, directory=True)
    secure(base / 'inventory.json')
    files = manifest((base / 'inventory.json').read_bytes(), expected)
    actual = set()
    for item in base.rglob('*'):
        if item.is_dir():
            secure(item, directory=True)
        else:
            secure(item)
            name = item.relative_to(base).as_posix()
            assert item.stat().st_size <= 1048576
            if name != 'inventory.json':
                actual.add(name)
                assert name in files and hashlib.sha256(item.read_bytes()).hexdigest() == files[name]
    assert actual == set(files)


def main():
    assert os.geteuid() == 0 and len(sys.argv) == 2
    verify_tree(LIVE, OLD_SHA)
    assert not os.path.lexists(STAGE) and not os.path.lexists(BACKUP)
    raw, data = payload(pathlib.Path(sys.argv[1]).absolute())
    temporary = pathlib.Path(tempfile.mkdtemp(prefix='.poolrotater-display-hub-', dir='/opt'))
    try:
        for name, content in {'inventory.json': raw, **data}.items():
            target = temporary / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o644)
            with target.open('rb') as handle: os.fsync(handle.fileno())
        for item in sorted(temporary.rglob('*'), key=lambda p: len(p.parts), reverse=True):
            if item.is_dir():
                item.chmod(0o755)
                fd = os.open(item, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(fd)
                finally: os.close(fd)
        temporary.chmod(0o755)
        fd = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
        verify_tree(temporary, NEW_SHA)
        assert not os.path.lexists(STAGE)
        os.rename(temporary, STAGE)
        fd = os.open('/opt', os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
        print('verified staging complete; no service or state changed')
    finally:
        if temporary.exists(): shutil.rmtree(temporary)


if __name__ == '__main__': main()
```

Run it with isolated Python and the exact archive path:

```bash
sudo chmod 600 /root/poolrotater-community-hub-stage.py
sudo nice -n 15 /usr/bin/python3.12 -I -S /root/poolrotater-community-hub-stage.py /root/poolrotater-v7.0/validation/community-display/poolrotater-community-display-hub.zip
```

Any existing staging/backup directory is a reason to inspect the earlier transition,
not overwrite it. All staged code is `root:root`, files 0644, directories 0755,
without symlinks, hard-linked files or group/other write permission.

## 2. Capture baseline and obtain the maintenance window

Do not proceed to section 3 until the owner has authorized this central-hub pause.
Create a fresh root-only audit directory and retain redacted operator diagnostics.
Record the hub inventory digest, service configuration and owner/network/Caddy PIDs.
The unchanged running gameplay/network services are the comparison baseline.

The following supported protected commands inspect the existing hub:

```bash
sudo /usr/sbin/runuser -u poolrotater-hub -- /usr/bin/python3.12 -I -S /opt/poolrotater-v7-hub/bootstrap.py --root /var/lib/poolrotater-v7-hub check
sudo /usr/sbin/runuser -u poolrotater-hub -- /usr/bin/python3.12 -I -S /opt/poolrotater-v7-hub/bootstrap.py --root /var/lib/poolrotater-v7-hub admin inspect
sudo /usr/sbin/runuser -u poolrotater-hub -- /usr/bin/python3.12 -I -S /opt/poolrotater-v7-hub/bootstrap.py --root /var/lib/poolrotater-v7-hub admin ratings-inspect
sudo systemctl show poolrotater-v7-hub.service poolrotater-v7-pilot-owner.service poolrotater-v7-network-owner.service caddy.service --property=Id,ActiveState,MainPID,NRestarts
```

Confirm the public CA hash in the table. Record `/v1/rooms?limit=100` and current
operator `network-support`/`ratings-support`. Keep private hashes, identity fields,
raw state and backups out of public reports. No new invitation or contribution grant
is needed for an enrolled operator.

The staged protected bootstrap can also run `check` against the existing state
before cutover:

```bash
sudo /usr/sbin/runuser -u poolrotater-hub -- /usr/bin/python3.12 -I -S /opt/poolrotater-v7-hub-display-dev11.staged/bootstrap.py --root /var/lib/poolrotater-v7-hub check
```

It does not listen or initialize a fresh hub. If it refuses, resolve that refusal
before stopping the current hub.

## 3. Stop only the hub and take the supported private backup

```bash
sudo systemctl stop poolrotater-v7-hub.service
sudo systemctl show poolrotater-v7-hub.service --property=ActiveState,MainPID
sudo pgrep -a -u poolrotater-hub
```

Require `inactive`, `MainPID=0` and no remaining process for this dedicated hub user.
`pgrep` exiting 1 means no process was found. Do not rename code while a process or
request is still active. Do not kill an unidentified process to make this check pass.

Create a private external backup parent if it does not already exist. If it exists,
first check its purpose, ownership and mode instead of changing an unrelated path.

```bash
sudo install -d -m 0700 -o poolrotater-hub -g poolrotater-hub /var/backups/poolrotater-v7-hub
sudo /usr/sbin/runuser -u poolrotater-hub -- /usr/bin/python3.12 -I -S /opt/poolrotater-v7-hub/bootstrap.py --root /var/lib/poolrotater-v7-hub backup --destination /var/backups/poolrotater-v7-hub/community-display-dev11
```

Require a successful `backup_created: true` and `ratings_policy_included: true`
for this ratings-enabled hub. The destination must be new. This supported backup
includes a consistent SQLite image including WAL and the policy/deletion fence.
It contains private identity data and remains private; it includes no TLS keys or
osu! OAuth credentials. If backup fails, retain evidence and restart the unchanged
old hub; do not proceed with a partial backup.

## 4. Replace only code, with recoverable rename edges

Recheck the old and staged inventory pins and complete tree protection immediately
before both renames using the `verify_tree` function above. Ensure the stopped check
still holds. The rollback prefix must be absent. Use root-owned `/opt` siblings only.
The saved reviewed snippet exposes the same tree verifier without executing its
staging entry:

```bash
sudo /usr/bin/python3.12 -I -S -c 'import os,runpy;m=runpy.run_path("/root/poolrotater-community-hub-stage.py",run_name="verify_only");m["verify_tree"](m["LIVE"],m["OLD_SHA"]);m["verify_tree"](m["STAGE"],m["NEW_SHA"]);assert not os.path.lexists(m["BACKUP"])'
```

The first rename retains all old code; the second publishes the fully checked new
code at the identical prefix. These commands do not modify state, TLS or the unit:

```bash
sudo /usr/bin/python3.12 -I -S -c 'import os;os.rename("/opt/poolrotater-v7-hub","/opt/poolrotater-v7-hub-pre-display-dev11");fd=os.open("/opt",os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)'
sudo /usr/bin/python3.12 -I -S -c 'import os;os.rename("/opt/poolrotater-v7-hub-display-dev11.staged","/opt/poolrotater-v7-hub");fd=os.open("/opt",os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)'
```

Require the new inventory pin at the live prefix, then run the new protected `check`
command from section 2. It validates all code before reading existing hub state.
No `daemon-reload`, reinstall, database restore, grant change or client reenrollment
is needed because the unit and paths are unchanged.

If this staged check succeeds:

```bash
sudo systemctl start poolrotater-v7-hub.service
sudo systemctl show poolrotater-v7-hub.service --property=ActiveState,MainPID,NRestarts
```

Require active/healthy startup, the same server epoch and unchanged owner/network/
Caddy PIDs. Inspect the hub journal for failures without publishing private data.
A failed start is a reason for code rollback, not restoring an old database.

## 5. Qualify both protocol versions and preserved state

These are credential-free public read-only probes using the existing pinned CA:

```bash
curl --fail --silent --show-error --max-time 4 --cacert /etc/poolrotater-community/ca.crt https://91.107.237.182:9444/v2/capabilities
curl --fail --silent --show-error --max-time 4 --cacert /etc/poolrotater-community/ca.crt 'https://91.107.237.182:9444/v1/rooms?limit=100'
curl --fail --silent --show-error --max-time 4 --cacert /etc/poolrotater-community/ca.crt 'https://91.107.237.182:9444/v2/rooms?limit=100'
```

Require the exact v2 capability response:

```json
{"ok":true,"protocol_version":2,"display_version":1,"heartbeat_path":"/v2/hosts/self/heartbeat","directory_path":"/v2/rooms"}
```

V1 must still have `protocol_version: 1` and its old closed room fields. V2 must
have `protocol_version: 2`; an old operator's `display: null` is expected. Verify
private/stale/revoked listings remain suppressed; an empty list is valid when no
fresh public rooms have been observed. The directory should reappear after the
existing agents' next successful heartbeat, without bot or agent restarts.

Repeat protected `check`, `admin inspect` and `admin ratings-inspect`. Verify the
same server epoch, active enrolled identities and existing contribution permissions.
Processing watermark must not regress; event phase counts can change as work is
processed, so do not require every phase count to remain numerically identical.
For owner and Xikeon, collect fresh redacted `network-support` and `ratings-support`:
existing enrollment/bindings/sharing are retained, heartbeat receipt advances, no
pending uncertainty is silently discarded, and pending votes are acknowledged by
the supported sync agent. Do not create artificial votes or replay batches merely
to produce acceptance evidence. Actual remote progress accuracy still requires a
new operator runtime and observed gameplay; this hub activation alone does not
upgrade anyone's running worker.

## 6. Code-only rollback and interruption recovery

For an unhealthy new hub, stop **only** `poolrotater-v7-hub.service` and again verify
`inactive`, PID 0 and no dedicated-user processes. Verify the new live code and old
rollback code against their exact inventory pins. Require that the failed-code
prefix below is absent, then retain the new code and restore the old prefix:

```bash
sudo /usr/bin/python3.12 -I -S -c 'import os;os.rename("/opt/poolrotater-v7-hub","/opt/poolrotater-v7-hub-display-dev11.failed");fd=os.open("/opt",os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)'
sudo /usr/bin/python3.12 -I -S -c 'import os;os.rename("/opt/poolrotater-v7-hub-pre-display-dev11","/opt/poolrotater-v7-hub");fd=os.open("/opt",os.O_RDONLY|os.O_DIRECTORY);os.fsync(fd);os.close(fd)'
sudo /usr/sbin/runuser -u poolrotater-hub -- /usr/bin/python3.12 -I -S /opt/poolrotater-v7-hub/bootstrap.py --root /var/lib/poolrotater-v7-hub check
sudo systemctl start poolrotater-v7-hub.service
```

If systemd reports `start-limit-hit`, clear only this stopped hub unit's failed
status with `sudo systemctl reset-failed poolrotater-v7-hub.service`, then retry the
verified old code once. This does not clear ledger or ownership recovery and must
not be used to bypass a refused protected `check`.

Use the **latest existing database and policy**, including any new votes/receipts,
revocations and deletion fences committed after activation. Never copy back the
pre-upgrade SQLite backup for an ordinary code rollback. The compatible storage
format keeps legacy room observations readable; newer clients negotiate a confirmed
unknown v2 route back to v1. Retain any pending heartbeat state and let the supported
client handle that downgrade rather than editing it manually.

After process interruption or host reboot, first stop the hub and inspect all exact
prefixes and pins. Do not start an absent or partially copied runtime.

| Verified filesystem state | Recovery |
| --- | --- |
| Live=old, staged=new, rollback absent | Activation has not crossed the first rename. Start old code, or resume the explicitly authorized procedure after backup verification. |
| Live absent, staged=new, rollback=old | First activation rename completed. Restore rollback→live to resume old code, or complete staged→live if the authorized activation and successful private backup are confirmed. |
| Live=new, rollback=old, staged absent | Second activation rename completed. Verify new protected check and qualify startup, or perform code-only rollback. |
| Live absent, failed=new, rollback=old | First rollback rename completed. Restore rollback→live, verify old code against its pin, and start it against latest state. |
| Live=old, failed=new, rollback absent | Rollback completed. Qualify v1, preserved ratings/enrollment and unchanged gameplay PIDs. |
| Any unexpected files, symlinks, conflicting prefixes or unknown digest | Retain all state, leave the hub stopped and obtain review. Do not delete fragments or restore a stale ledger to force progress. |

Fsync `/opt` after the recovery rename exactly as above. Keep old/failed code and
the private backup until post-activation acceptance is recorded. Delete only clearly
owned obsolete code staging afterward; backup retention follows the private hub
recovery policy, and credentials/state are never included in public release kits.
