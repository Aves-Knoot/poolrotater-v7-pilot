"""Logging setup for one or many lobby instances."""
from __future__ import annotations

import logging
import logging.handlers
import os
from datetime import datetime, timezone
from pathlib import Path

OUR_LOGGERS = (
    "run", "auth", "api", "hub", "bot", "pool", "ratings",
    "fake", "close", "rooms"
)
NOISY_LIBS = ("httpx", "httpcore", "SignalRCoreClient", "urllib3")
LOG_MAX_BYTES = 50_000_000


class PersistentRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotate at a bounded size while keeping every dated archive indefinitely."""

    def __init__(self, filename, *, encoding="utf-8"):
        super().__init__(
            filename, mode="a", maxBytes=LOG_MAX_BYTES, backupCount=0,
            encoding=encoding,
        )

    def doRollover(self):
        if self.stream:
            self.stream.flush()
            self.stream.close()
            self.stream = None

        if os.path.exists(self.baseFilename):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            destination = self.rotation_filename(
                f"{self.baseFilename}.{stamp}"
            )
            suffix = 1
            while os.path.exists(destination):
                destination = self.rotation_filename(
                    f"{self.baseFilename}.{stamp}.{suffix}"
                )
                suffix += 1
            self.rotate(self.baseFilename, destination)

        if not self.delay:
            self.stream = self._open()


def configure(verbose: bool = False, debug_libs: bool = False,
              log_dir: str = "logs", log_file: str | None = None) -> Path:
    """Set up console + rotating file logging.

    ``log_file`` is preferred for multi-room hosting, for example
    /var/log/osu-farm-bot/room1.log. If omitted, logs/bot.log is used.
    """
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        log_path = Path(log_dir) / "bot.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(name)-8s %(levelname)-7s %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = PersistentRotatingFileHandler(log_path)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    if not debug_libs:
        for name in NOISY_LIBS:
            logging.getLogger(name).setLevel(logging.WARNING)

    logging.getLogger("logsetup").info(
        "logging to %s (console=%s, libs=%s)",
        log_path.resolve(),
        "DEBUG" if verbose else "INFO",
        "shown" if debug_libs else "suppressed",
    )
    return log_path


def extract_problems(log_path: str = "logs/bot.log", context: int = 3,
                     max_chars: int = 6000) -> str:
    """Pull WARNING/ERROR lines and traceback blocks out of one room log."""
    p = Path(log_path)
    if not p.exists():
        return f"No log file at {p.resolve()} yet -- run the bot first."

    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    flagged: set[int] = set()

    for i, line in enumerate(lines):
        if " ERROR " in line or " WARNING " in line or line.startswith("Traceback"):
            for j in range(max(0, i - context), min(len(lines), i + context + 1)):
                flagged.add(j)
        if line.startswith("Traceback (most recent call last):"):
            j = i
            while j < len(lines) and (
                lines[j].startswith((" ", "Traceback"))
                or lines[j].strip().endswith("Error")
                or ":" in lines[j][:40]
            ):
                flagged.add(j)
                j += 1
                if j - i > 40:
                    break

    if not flagged:
        return (
            f"No WARNING/ERROR lines found in {p.resolve()} "
            f"({len(lines)} lines total)."
        )

    ordered = sorted(flagged)
    out, last = [], -2
    for i in ordered:
        if i != last + 1:
            out.append("...")
        out.append(lines[i])
        last = i

    text = "\n".join(out)
    if len(text) > max_chars:
        text = text[-max_chars:]
        if "\n" in text:
            text = "...(truncated)...\n" + text[text.index("\n") + 1:]
    return text
