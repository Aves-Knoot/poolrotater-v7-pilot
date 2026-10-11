"""OAuth token management.

The single most important behaviour here: osu! returns a NEW refresh token on
every refresh and invalidates the old one. If we fail to persist the new value
we are locked out permanently and the browser consent flow has to be redone.
So the write happens BEFORE the new access token is returned to callers, and
it is atomic (temp file + replace) so a crash mid-write cannot corrupt it.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
import tempfile
import threading
import time
from pathlib import Path

import httpx

log = logging.getLogger("auth")

TOKEN_URL = "https://osu.ppy.sh/oauth/token"


class TokenManager:
    def __init__(self, token_file: str, client_id: int, client_secret: str):
        self.path = Path(token_file)
        self.client_id = int(client_id)
        self.client_secret = client_secret
        self._lock = threading.Lock()
        self._access: str | None = None
        self._expires_at = 0.0
        self._rejected_access: str | None = None

        if not self.path.exists():
            raise SystemExit(
                f"{self.path} not found. Run get_token.py first (on a machine "
                "with a browser) to produce it."
            )
        data = json.loads(self.path.read_text())
        self._refresh_token = data.get("refresh_token")
        if not self._refresh_token:
            raise SystemExit(f"No refresh_token in {self.path}.")
        # Reuse a persisted, unexpired access token. A second reader must not
        # rotate credentials out from under the account-owning supervisor.
        log.info("loaded refresh token from %s", self.path)

    @contextmanager
    def _file_lock(self):
        """Serialize refresh-token rotation across processes, not just threads."""
        with self.path.with_suffix(".refresh.lock").open("a+b") as stream:
            os.chmod(stream.name, 0o600)
            if os.name == "nt":
                import msvcrt
                stream.seek(0, os.SEEK_END)
                if not stream.tell():
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _persist(self, refresh_token: str, access_token: str, expires_in: int) -> None:
        payload = {
            "client_id": self.client_id,
            "refresh_token": refresh_token,
            "access_token": access_token,
            "expires_in": expires_in,
            "updated_at": time.time(),
        }
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent,
                                             prefix=".oauth-", delete=False) as stream:
                tmp = Path(stream.name)
                os.chmod(tmp, 0o600)
                json.dump(payload, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    def _refresh(self) -> None:
        log.info("refreshing access token")
        r = httpx.post(TOKEN_URL, json={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        }, timeout=30)

        if r.status_code != 200:
            # Do not kill an RPC thread or include credential-bearing bodies.
            raise RuntimeError(f"OAuth token refresh failed (HTTP {r.status_code})")
        data = r.json()
        new_refresh = data.get("refresh_token", self._refresh_token)

        # Persist FIRST. If the process dies right after this line we still
        # hold a usable token; if we returned the access token first and then
        # died, the rotated refresh token would be lost forever.
        self._persist(new_refresh, data["access_token"], data.get("expires_in", 86400))

        self._refresh_token = new_refresh
        self._access = data["access_token"]
        self._expires_at = time.time() + data.get("expires_in", 86400)
        self._rejected_access = None
        log.info("token refreshed, valid for %.1f hours",
                 data.get("expires_in", 86400) / 3600)

    def access_token(self) -> str:
        with self._lock, self._file_lock():
            # Another process may have rotated both tokens. Always re-read the
            # persisted refresh token while holding the interprocess lock.
            data = json.loads(self.path.read_text())
            if data.get("client_id") not in (None, self.client_id):
                raise RuntimeError("OAuth token file belongs to a different client")
            self._refresh_token = data["refresh_token"]
            access = data.get("access_token")
            expires_at = float(data.get("updated_at") or 0) + float(data.get("expires_in") or 0)
            if access and access != self._rejected_access and time.time() < expires_at - 300:
                self._access, self._expires_at = access, expires_at
            else:
                self._refresh()
            return self._access

    def invalidate_access_token(self, rejected_token: str | None = None) -> None:
        """Reload a newer persisted token or refresh once after a definite 401."""
        with self._lock:
            self._rejected_access = rejected_token or self._access
            self._expires_at = 0.0
