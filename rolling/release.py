"""Release identity and compatibility. No migrations execute in this module."""
from __future__ import annotations

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
STORES = ("bot/ratings.py", "bot/ppstats.py", "bot/player_preferences.py",
          "bot/process_control.py", "bot/config_lock.py")


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
                or os.access(path, os.W_OK) or os.access(path.parent, os.W_OK)):
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
    return {"format": 1, "version": version, "protocol": PROTOCOL,
            "python": str(Path(python).absolute()), "python_hash": file_hash(Path(python).resolve()), "files": files,
            "environment_hash": environment_hash(python),
            "shared": {k: files[k] for k in SHARED},
            "storage": {k: files[k] for k in STORES},
            "storage_epoch": "poolrotater-6.7-v1", "migrations": [],
            "requirements": files["requirements.txt"]}


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
    for key, names in (("shared", SHARED), ("storage", STORES)):
        if manifest.get(key) != {n: expected[n] for n in names}:
            raise IncompatibleRelease(f"invalid {key} contract fingerprint")
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
