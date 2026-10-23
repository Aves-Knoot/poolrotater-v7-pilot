"""Dedicated, credential-free browser sandbox for the private portal tests."""
import os
from pathlib import Path
import shutil
import subprocess
import sys


HERE = Path(__file__).resolve()
if HERE.as_posix().startswith("/work/harness/"):
    HARNESS = Path("/work/harness")
    SOURCE = Path("/work/source")
    WORK = Path("/work")
else:
    HARNESS = HERE.parents[1]
    WORK = HERE.parents[4]
    SOURCE = Path(os.environ.get("PRIVATE_BOOKING_SOURCE", str(HARNESS))).resolve()
BROWSER_ASSETS = WORK / "validation/development-tools/private-booking-browser"


def inner():
    sys.argv = [sys.argv[0]]
    sys.path.insert(0, "/work/source")
    sys.path.insert(0, "/work/harness/tests")
    sys.path.insert(0, "/work/scripts")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/work/tools/browsers"
    from test_isolation import require_test_isolation
    boundary = require_test_isolation("ordinary")
    print(f"browser isolation: {boundary}; Chromium and simulated portal share isolated network namespace")
    from test_booking_real_browser import unittest
    unittest.main(module="test_booking_real_browser", verbosity=2)


def outer():
    workspace = WORK.resolve()
    if not SOURCE.is_relative_to(workspace) or SOURCE == workspace:
        raise SystemExit("browser app source must be a dedicated tree under the isolated work directory")
    if not (SOURCE / "poolrotater_host/bookings/static/index.html").is_file():
        raise SystemExit("browser app source must contain the private booking portal assets")
    if not (BROWSER_ASSETS / "python-packages/playwright").is_dir():
        raise SystemExit("browser tools missing: stage Playwright and Chromium as documented")
    if shutil.which("bwrap") is None or shutil.which("systemd-run") is None:
        raise SystemExit("browser qualification requires bubblewrap and systemd-run cgroup limits")
    if not (WORK / "scripts/test_isolation.py").is_file():
        raise SystemExit("verified test isolation helper is unavailable")
    sys.path.insert(0, str(WORK / "scripts"))
    from test_isolation import anchor_fd
    anchor = anchor_fd("ordinary")
    bwrap = [
        "/usr/bin/bwrap", "--unshare-all", "--uid", "1000", "--gid", "1000",
        "--die-with-parent", "--new-session", "--cap-drop", "ALL",
        "--ro-bind", "/usr", "/usr", "--tmpfs", "/usr/local", "--remount-ro", "/usr/local",
        "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64", "--proc", "/proc", "--dev", "/dev",
        "--tmpfs", "/tmp", "--dir", "/tmp/home", "--dir", "/etc",
        "--ro-bind", "/etc/ld.so.cache", "/etc/ld.so.cache",
        "--ro-bind", str(WORK / "scripts/sandbox-passwd"), "/etc/passwd",
        "--ro-bind", str(WORK / "scripts/sandbox-group"), "/etc/group",
        "--perms", "0444", "--ro-bind-data", str(anchor), "/etc/poolrotater-test-boundary.json",
        "--dir", "/work", "--dir", "/work/source", "--dir", "/work/tools", "--dir", "/work/scripts",
        "--dir", "/work/harness", "--dir", "/work/harness/tests",
        "--ro-bind", str(SOURCE), "/work/source",
        "--ro-bind", str(HARNESS / "tests/run_private_booking_browser.py"), "/work/harness/tests/run_private_booking_browser.py",
        "--ro-bind", str(HARNESS / "tests/test_booking_real_browser.py"), "/work/harness/tests/test_booking_real_browser.py",
        "--ro-bind", str(BROWSER_ASSETS), "/work/tools",
        "--ro-bind", str(WORK / "scripts/test_isolation.py"), "/work/scripts/test_isolation.py",
        "--chdir", "/work/source", "--clearenv",
        "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "HOME", "/tmp/home",
        "--setenv", "TMPDIR", "/tmp",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "PYTHONPATH", "/work/source:/work/source/tests:/work/tools/python-packages",
        "--setenv", "PRIVATE_BOOKING_APP_SOURCE", "/work/source",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        "--setenv", "MALLOC_ARENA_MAX", "2",
        "--setenv", "PLAYWRIGHT_BROWSERS_PATH", "/work/tools/browsers",
        "--setenv", "PRIVATE_BOOKING_BROWSER_SANDBOX", "1",
        "--setenv", "PRIVATE_BOOKING_CHROMIUM", "/work/tools/browsers/chromium_headless_shell-1248/chrome-headless-shell-linux64/chrome-headless-shell",
        "--", "/usr/bin/python3", "-S", "/work/harness/tests/run_private_booking_browser.py", "--inner",
    ]
    unit = f"private-booking-browser-{os.getpid()}"
    command = [
        "/usr/bin/nice", "-n", "19", "/usr/bin/systemd-run", "--scope", "--collect", "--quiet",
        f"--unit={unit}", "--property=MemoryMax=1G", "--property=MemorySwapMax=256M",
        "--property=CPUQuota=100%", "--", *bwrap,
    ]
    try:
        return subprocess.run(command, check=False, env={"PATH": "/usr/bin:/bin"},
                              pass_fds=(anchor,)).returncode
    finally:
        os.close(anchor)


if __name__ == "__main__":
    if sys.argv[1:] == ["--inner"]:
        inner()
    else:
        raise SystemExit(outer())
