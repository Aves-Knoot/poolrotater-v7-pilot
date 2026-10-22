"""Release identity and compatibility. No migrations execute in this module."""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from . import PROTOCOL

# Conservatively require identical shared services and persistent store code.
# An intentional contract change requires a coordinated supervisor upgrade.
SHARED = ("rolling/protocol.py", "rolling/broker.py", "rolling/supervisor.py",
          "rolling/release.py", "rolling/state.py", "rolling/storage.py", "rolling/adapters.py",
          "run_rolling.py", "run_multiroom.py", "run_bot.py", "bot/multiroom.py", "bot/hub.py",
          "bot/auth.py", "bot/api.py", "bot/chat_websocket.py", "bot/discord_control.py",
          "poolrotater_host/config.py", "poolrotater_host/layout.py", "poolrotater_host/runtime_config.py",
          "poolrotater_host/catalogs.py", "poolrotater_host/__init__.py", "offline_host_controller.py")
SHARED += ("poolrotater_host/oauth.py", "poolrotater_host/oauth_callback.py")
SHARED += ("poolrotater_host/runtime_package.py", "poolrotater_host/provisioning.py", "check_protected_runtime.py")
SHARED += ("deployment/sqlite_readonly.py",)
SHARED += ("poolrotater_host/runtime_lifecycle.py", "poolrotater_host/service_bootstrap.py")
SHARED += ("poolrotater_host/managed_host/__init__.py", "poolrotater_host/managed_host/admission.py",
           "poolrotater_host/managed_host/agent.py", "poolrotater_host/managed_host/contracts.py",
           "poolrotater_host/managed_host/engine.py", "poolrotater_host/managed_host/hub.py",
           "poolrotater_host/managed_host/details.py",
           "poolrotater_host/managed_host/catalogs.py", "poolrotater_host/managed_host/catalog_source.py",
           "poolrotater_host/managed_host/upgrade.py", "poolrotater_host/managed_host/topology.py",
           "poolrotater_host/managed_host/migration.py", "poolrotater_host/managed_host/native.py",
           "poolrotater_host/managed_host/operations.py", "poolrotater_host/managed_host/policy.py",
           "poolrotater_host/managed_host/projection.py", "poolrotater_host/managed_host/secrets.py",
           "poolrotater_host/managed_host/settings.py", "poolrotater_host/managed_host/state.py",
           "poolrotater_host/managed_bootstrap.py")
SHARED += ("poolrotater_host/discord_relay/fleet.py",)
# Booking ledgers, authorization and lifecycle are controller contracts.
# A future change cannot silently roll only one worker against older authority.
SHARED += (
    'poolrotater_host/bookings/__init__.py',
    'poolrotater_host/bookings/admin.py',
    'poolrotater_host/bookings/admission.py',
    'poolrotater_host/bookings/agent.py',
    'poolrotater_host/bookings/auth.py',
    'poolrotater_host/bookings/contracts.py',
    'poolrotater_host/bookings/eligibility.py',
    'poolrotater_host/bookings/game.py',
    'poolrotater_host/bookings/hub.py',
    'poolrotater_host/bookings/lifecycle.py',
    'poolrotater_host/bookings/local.py',
    'poolrotater_host/bookings/native.py',
    'poolrotater_host/bookings/oauth.py',
    'poolrotater_host/bookings/panels.py',
    'poolrotater_host/bookings/policy.py',
    'poolrotater_host/bookings/portal_entry.py',
    'poolrotater_host/bookings/presentation.py',
    'poolrotater_host/bookings/preview.py',
    'poolrotater_host/bookings/repository.py',
    'poolrotater_host/bookings/selection.py',
    'poolrotater_host/bookings/server.py',
    'poolrotater_host/bookings/settings.py',
    'poolrotater_host/bookings/update_guard.py',
    'poolrotater_host/bookings/web.py',
    'poolrotater_host/bookings/worker.py',
)
STORES = ("bot/ratings.py", "bot/ppstats.py", "bot/player_preferences.py",
          "bot/process_control.py", "bot/config_lock.py")


# Exact manifest inventory emitted by immutable v7.2.6/7/8 stagers. This is
# only a read-format compatibility rule; every file is still checked, and the
# returned fingerprint covers the complete candidate-declared shared contract.
LEGACY_STAGER_SHARED = ('rolling/protocol.py', 'rolling/broker.py', 'rolling/supervisor.py', 'rolling/release.py', 'rolling/state.py', 'rolling/storage.py', 'rolling/adapters.py', 'run_rolling.py', 'run_multiroom.py', 'run_bot.py', 'bot/multiroom.py', 'bot/hub.py', 'bot/auth.py', 'bot/api.py', 'bot/chat_websocket.py', 'bot/discord_control.py', 'poolrotater_host/config.py', 'poolrotater_host/layout.py', 'poolrotater_host/runtime_config.py', 'poolrotater_host/catalogs.py', 'poolrotater_host/__init__.py', 'offline_host_controller.py', 'poolrotater_host/oauth.py', 'poolrotater_host/oauth_callback.py', 'poolrotater_host/runtime_package.py', 'poolrotater_host/provisioning.py', 'check_protected_runtime.py', 'deployment/sqlite_readonly.py', 'poolrotater_host/runtime_lifecycle.py', 'poolrotater_host/service_bootstrap.py')


class IncompatibleRelease(ValueError):
    pass


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix="." + path.name,
                                         mode="w", delete=False) as f:
            temporary = Path(f.name)
            json.dump(value, f, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def environment_hash(python):
    """Pin the venv configuration and installed distribution metadata."""
    root = Path(python).absolute().parent.parent.resolve()
    paths = [root / "pyvenv.cfg"] if (root / "pyvenv.cfg").exists() else []
    paths += sorted(root.glob("lib/python*/site-packages/*.dist-info/RECORD"))
    return hashlib.sha256(json.dumps([(str(p.relative_to(root)), file_hash(p)) for p in paths]).encode()).hexdigest()


def require_immutable(root, manifest):
    # Evaluated as the service account. Root-owned releases/venvs prevent a
    # worker's ordinary file writes from replacing sibling versions or imports.
    paths = [Path(root), Path(root) / "release.json", Path(manifest["python"]).resolve(),
             Path(manifest["python"]).absolute().parent.parent.resolve()]
    paths += [Path(root) / name for name in manifest["files"]]
    environment = Path(manifest["python"]).absolute().parent.parent.resolve()
    if not (environment / "pyvenv.cfg").is_file():
        raise IncompatibleRelease("live releases require a pinned virtual environment")
    paths += [p for folder in ("bin", "lib") for p in (environment / folder).rglob("*")]
    for path in paths:
        if (path.stat().st_uid != 0 or path.parent.stat().st_uid != 0
                or os.access(path, os.W_OK, effective_ids=True)
                or os.access(path.parent, os.W_OK, effective_ids=True)):
            raise IncompatibleRelease(f"service account can modify release/environment: {path}")


def source_files(root):
    root = Path(root)
    names = {p.relative_to(root).as_posix() for folder in ("bot", "rolling", "tests", "tools", "deployment", "poolrotater_host")
             for p in (root / folder).rglob("*.py")}
    names.update(p.name for p in root.glob("*.py"))
    names.add("requirements.txt")
    if (root / "ai_context.md").is_file():
        names.add("ai_context.md")
    names.update(p.relative_to(root).as_posix() for folder in ("catalog-data", "farm-data")
                 for p in (root / folder).glob("*.db"))
    return sorted(names)


def build_manifest(root, version, python):
    root = Path(root).resolve()
    if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
        raise ValueError("use a full release identifier, e.g. v6.7.0")
    files = {}
    for name in source_files(root):
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("release code must be local immutable files")
        files[name] = file_hash(path)
    # A protected predecessor may stage a coordinated successor whose literal
    # shared inventory has grown. Build that successor's contract, not the
    # already-imported predecessor module's globals. Parse only verified local
    # bytes; never execute the candidate to discover its contract.
    contract = declared_contract(root, files)
    return {"format": 1, "version": version, "protocol": PROTOCOL,
            "python": str(Path(python).absolute()), "python_hash": file_hash(Path(python).resolve()), "files": files,
            "environment_hash": environment_hash(python),
            "shared": {k: files[k] for k in contract['SHARED']},
            "storage": {k: files[k] for k in contract['STORES']},
            "storage_epoch": "poolrotater-6.7-v1", "migrations": [],
            "requirements": files["requirements.txt"]}


def declared_contract(root, expected):
    """Read a verified release's literal contract, without executing its code.

    A controller successor may add shared modules that do not exist in an old
    protected release. Using the successor's module globals to validate that
    predecessor would make every such coordinated upgrade impossible. Complete
    file hashes are checked first; stopped upgrade admission still requires an
    exact reviewed before/after pair, and worker promotion remains stricter.
    """
    name='rolling/release.py';path=Path(root)/name
    if name not in expected or path.stat().st_size>256*1024 or file_hash(path)!=expected[name]:
        raise IncompatibleRelease('release contract source changed')
    try:
        values={}
        for node in ast.parse(path.read_bytes()).body:
            if isinstance(node,ast.Assign):
                names=[n.id for n in node.targets if isinstance(n,ast.Name) and n.id in ('SHARED','STORES')]
                if names:
                    if len(node.targets)!=1 or len(names)!=1 or names[0] in values:raise ValueError()
                    value=ast.literal_eval(node.value)
                    if type(value) is not tuple:raise ValueError()
                    values[names[0]]=value
            elif isinstance(node,ast.AugAssign) and isinstance(node.target,ast.Name) and node.target.id in ('SHARED','STORES'):
                name=node.target.id;value=ast.literal_eval(node.value)
                if not isinstance(node.op,ast.Add) or name not in values or type(value) is not tuple:raise ValueError()
                values[name]+=value
        if set(values)!={'SHARED','STORES'}:raise ValueError()
        for names in values.values():
            if not 1<=len(names)<=128 or any(type(n) is not str or n not in expected for n in names) or len(set(names))!=len(names):raise ValueError()
        if not {'rolling/release.py','rolling/protocol.py'}<=set(values['SHARED']):raise ValueError()
        if not {'bot/ratings.py','bot/ppstats.py'}<=set(values['STORES']):raise ValueError()
    except (SyntaxError,ValueError,TypeError,UnicodeError,RecursionError):
        raise IncompatibleRelease('invalid declared release contract') from None
    return values


def load_release(root):
    root = Path(root).resolve()
    manifest = json.loads((root / "release.json").read_text())
    if manifest.get("format") != 1 or manifest.get("protocol") != PROTOCOL:
        raise IncompatibleRelease("worker protocol requires a coordinated upgrade")
    if not re.fullmatch(r"v\d+\.\d+\.\d+", str(manifest.get("version", ""))):
        raise IncompatibleRelease("invalid release version")
    if manifest.get("migrations") != []:
        raise IncompatibleRelease("rolling deployments cannot run database migrations")
    if not isinstance(manifest.get("validation"), dict) or not manifest["validation"].get("tests"):
        raise IncompatibleRelease("release lacks a passing validation record")
    expected = manifest.get("files", {})
    if sorted(expected) != source_files(root):
        raise IncompatibleRelease("release file inventory changed after sealing")
    for name, digest in expected.items():
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root) or file_hash(path) != digest:
            raise IncompatibleRelease(f"release changed after sealing: {name}")
    contract=declared_contract(root,expected)
    shared = {n: expected[n] for n in contract['SHARED']}
    if manifest.get('shared') != shared:
        # Old protected stagers seal the full file inventory but populate this
        # derived field using their own 30-key table. Recognize ONLY that exact
        # known table, with matching hashes and a declared superset, then derive
        # the full fingerprint in memory. No sealed file is rewritten. Missing
        # keys, altered hashes, unknown subsets and storage changes still fail.
        release_version = tuple(map(int, manifest['version'][1:].split('.')))
        legacy = ({n: expected[n] for n in LEGACY_STAGER_SHARED}
                  if set(LEGACY_STAGER_SHARED) <= set(contract['SHARED']) else None)
        if (release_version[0] != 7 or release_version < (7, 2, 9)
                or legacy is None or manifest.get('shared') != legacy):
            raise IncompatibleRelease('invalid shared contract fingerprint')
        manifest['shared'] = shared
    if manifest.get('storage') != {n: expected[n] for n in contract['STORES']}:
        raise IncompatibleRelease('invalid storage contract fingerprint')
    python = Path(manifest["python"])
    if not python.is_absolute() or not os.access(python, os.X_OK):
        raise IncompatibleRelease("release interpreter is unavailable")
    if file_hash(python.resolve()) != manifest.get("python_hash"):
        raise IncompatibleRelease("release interpreter changed after sealing")
    if environment_hash(python) != manifest.get("environment_hash"):
        raise IncompatibleRelease("release dependency environment changed after sealing")
    return manifest


def require_compatible(current, candidate):
    for key in ("protocol", "shared", "storage", "storage_epoch", "requirements", "python_hash", "environment_hash"):
        if current.get(key) != candidate.get(key):
            raise IncompatibleRelease(f"{key} changed; current rooms must remain on their release")
