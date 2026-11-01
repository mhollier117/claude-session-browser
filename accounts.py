"""Several Claude accounts for the Session Browser and the Clawdmeter.

Claude Code keeps exactly one sign-in: the ``claudeAiOauth`` block in
``<claude dir>/.credentials.json`` plus the ``oauthAccount`` block in
``.claude.json``. This module keeps the other accounts' sign-ins in a private
store, swaps them in and out without ever logging out, keeps them renewed and
reads every account's usage.

Behaviour this relies on (tested against Claude Code 2.1.316):

* Swapping those two blocks switches Claude Code to the other account, even in
  windows that are already open.
* A sign-in's access token lasts 8 h. Renewing it ROTATES the renewal token,
  and the old copy dies at once. A dead copy is only refused; it doesn't cancel
  the account.
* Each sign-in has a fixed end date (``refreshTokenExpiresAt``, about 27 days)
  that renewing does not move, so every account needs a real re-login about
  once a month.

Rules (see Clawdmeter/plan.md):

1. Renew only STORED accounts. The live one belongs to Claude Code, which
   renews it and rewrites the file.
2. Re-read and store the live sign-in right before every swap, because an older
   stored copy of it may already be dead.
3. Lock, write atomically and verify after the swap.
4. Keep the store outside the Claude folder (default ``~/.claude-accounts``).
5. Never touch anything else in those files (``mcpOAuth`` holds connector
   sign-ins).

The store is encrypted with Windows DPAPI (current user only).
"""

from __future__ import annotations

import ctypes
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"   # Claude Code's own app id
API_BASE = "https://api.anthropic.com"
OAUTH_BETA = "oauth-2025-04-20"
# Cloudflare blocks Python's default User-Agent on the token endpoint.
USER_AGENT = "ClaudeSessionBrowser-accounts"

RENEW_BEFORE_S = 3600        # renew a stored account when < 1 h is left
SWAP_MIN_VALID_S = 300       # a swapped-in token must be good for 5 more min
LIVE_POLL_S = 300            # usage of the live account (the Bluetooth link
                             # already polls it every minute on its own)
PARKED_POLL_S = 600          # usage of stored accounts
RELOGIN_WARN_DAYS = 5
LOCK_STALE_S = 30
NICKNAME_MAX = 24

_UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")


class AccountError(Exception):
    """An account operation was refused or failed. The message is user-facing."""


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

class Paths:
    """Where Claude Code keeps its sign-in, and where our store lives."""

    def __init__(self, claude_dir=None, config_file=None, store_dir=None):
        home = Path.home()
        env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        if claude_dir:
            self.claude_dir = Path(claude_dir)
        elif env_dir:
            self.claude_dir = Path(env_dir)
        else:
            self.claude_dir = home / ".claude"
        self.credentials = self.claude_dir / ".credentials.json"
        if config_file:
            self.config = Path(config_file)
        elif env_dir or claude_dir:
            # With CLAUDE_CONFIG_DIR set, .claude.json lives inside that folder.
            self.config = self.claude_dir / ".claude.json"
        else:
            self.config = home / ".claude.json"
        self.store = Path(store_dir or os.environ.get("CSB_ACCOUNTS_DIR")
                          or home / ".claude-accounts")


# --------------------------------------------------------------------------
# Encryption of the store
# --------------------------------------------------------------------------

class PlainCodec:
    """No encryption. Used in tests and on non-Windows systems."""

    def encode(self, data: bytes) -> bytes:
        return data

    def decode(self, data: bytes) -> bytes:
        return data


class DpapiCodec:
    """Windows DPAPI: only the current Windows user can decrypt."""

    _FLAGS = 0x1  # CRYPTPROTECT_UI_FORBIDDEN

    def __init__(self):
        from ctypes import wintypes

        class _Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        self._Blob = _Blob
        self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    def _run(self, fn, data: bytes) -> bytes:
        buf = ctypes.create_string_buffer(data, len(data))
        inp = self._Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out = self._Blob()
        if not fn(ctypes.byref(inp), None, None, None, None, self._FLAGS,
                  ctypes.byref(out)):
            raise AccountError(
                f"Windows couldn't encrypt/decrypt the account store "
                f"(error {ctypes.get_last_error()})")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            self._kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))

    def encode(self, data: bytes) -> bytes:
        return self._run(self._crypt32.CryptProtectData, data)

    def decode(self, data: bytes) -> bytes:
        return self._run(self._crypt32.CryptUnprotectData, data)


def default_codec():
    # Elsewhere the store is plain text with 0600 files, the same protection
    # Claude Code gives its own .credentials.json there.
    return DpapiCodec() if sys.platform == "win32" else PlainCodec()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _ssl_ctx():
    import ssl
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class Http:
    """Tiny JSON-over-HTTPS client. Returns (status, parsed body or {})."""

    def _do(self, req, timeout):
        try:
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=_ssl_ctx()) as r:
                raw, code = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, code = e.read(), e.code
        except (urllib.error.URLError, OSError) as e:
            return 0, {"error": "network", "error_description": str(e)[:120]}
        try:
            return code, json.loads(raw or b"{}")
        except ValueError:
            return code, {}

    def get_json(self, url, token, timeout=20):
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json"})
        return self._do(req, timeout)

    def post_json(self, url, body, timeout=30):
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": USER_AGENT})
        return self._do(req, timeout)


# --------------------------------------------------------------------------
# File helpers
# --------------------------------------------------------------------------

def fingerprint(secret: str | None) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()[:16] if secret else ""


def _read_json(path: Path):
    """Parsed JSON, None if the file is missing. Raises on unreadable JSON so
    that a half-written file is never mistaken for an empty one."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return json.loads(raw) if raw.strip() else {}


def _atomic_write(path: Path, data: bytes) -> None:
    """Write via temp file + rename. Retries briefly because Windows refuses
    the rename while another program has the file open."""
    # Owner-only from the start (0700 folders, 0600 files). On Windows these
    # modes are mostly ignored; there the user-profile ACLs plus DPAPI protect
    # the store. On other systems they're what keeps sign-ins private.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    try:
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if tmp.exists():
            tmp.unlink()


def _write_json(path: Path, data) -> None:
    _atomic_write(path, (json.dumps(data, indent=2, ensure_ascii=False)
                         + "\n").encode("utf-8"))


class _FileLock:
    """Cross-process lock: a lock file created exclusively. A lock older than
    LOCK_STALE_S is treated as left behind by a crashed process."""

    def __init__(self, path: Path, timeout: float = 10.0):
        self.path = path
        self.timeout = timeout

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        start = time.time()
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > LOCK_STALE_S:
                        self.path.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.time() - start > self.timeout:
                    raise AccountError("Another account operation is still running.")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _iso_to_epoch(value) -> float:
    if not value:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


# --------------------------------------------------------------------------
# The manager
# --------------------------------------------------------------------------

class AccountManager:
    """All account operations. Thread-safe; swaps are also locked across
    processes, so the app and the command line can't collide."""

    def __init__(self, paths: Paths | None = None, http=None, codec=None,
                 clock=time.time, log=None):
        self.paths = paths or Paths()
        self.http = http or Http()
        self.codec = codec or default_codec()
        self.clock = clock
        self.log = log or (lambda msg: None)
        self._lock = threading.RLock()

    # -- store records --------------------------------------------------------

    def _dir(self, uuid: str) -> Path:
        if not uuid or not _UUID_RE.match(uuid):
            raise AccountError(f"Not a valid account id: {uuid!r}")
        return self.paths.store / uuid

    def _uuids(self) -> list[str]:
        if not self.paths.store.is_dir():
            return []
        return sorted(p.name for p in self.paths.store.iterdir()
                      if p.is_dir() and _UUID_RE.match(p.name)
                      and (p / "signin.bin").exists())

    def _load_signin(self, uuid: str) -> dict | None:
        path = self._dir(uuid) / "signin.bin"
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            return None
        return json.loads(self.codec.decode(blob).decode("utf-8"))

    def _save_signin(self, uuid: str, rec: dict) -> None:
        data = json.dumps(rec).encode("utf-8")
        _atomic_write(self._dir(uuid) / "signin.bin", self.codec.encode(data))

    def _meta(self, uuid: str) -> dict:
        try:
            return _read_json(self._dir(uuid) / "meta.json") or {}
        except ValueError:
            return {}

    def _update_meta(self, uuid: str, **changes) -> dict:
        meta = self._meta(uuid)
        meta.update(changes)
        meta["uuid"] = uuid
        _write_json(self._dir(uuid) / "meta.json", meta)
        return meta

    def _state(self) -> dict:
        try:
            return _read_json(self.paths.store / "state.json") or {}
        except ValueError:
            return {}

    def _set_state(self, **changes) -> None:
        st = self._state()
        st.update(changes)
        _write_json(self.paths.store / "state.json", st)

    # -- live sign-in -----------------------------------------------------------

    def _live_oauth(self) -> dict | None:
        creds = _read_json(self.paths.credentials) or {}
        oauth = creds.get("claudeAiOauth")
        return oauth if isinstance(oauth, dict) and oauth.get("refreshToken") else None

    def _profile(self, access_token: str) -> dict | None:
        code, data = self.http.get_json(f"{API_BASE}/api/oauth/profile", access_token)
        if code != 200 or not isinstance(data, dict):
            return None
        acct = data.get("account") or {}
        org = data.get("organization") or {}
        if not acct.get("uuid"):
            return None
        return {"uuid": acct["uuid"], "email": acct.get("email") or "",
                "display_name": acct.get("display_name") or "",
                "plan": org.get("plan_display_name") or ""}

    def _known_uuid_for(self, refresh_fp: str) -> str | None:
        """Which stored account has this exact renewal token?"""
        st = self._state()
        if refresh_fp and st.get("live_fp") == refresh_fp and st.get("live_uuid"):
            return st["live_uuid"]
        for uuid in self._uuids():
            if self._meta(uuid).get("refresh_fp") == refresh_fp:
                return uuid
        return None

    def sync_live(self) -> str | None:
        """Identify the account Claude Code is signed into and store an
        up-to-date copy of its sign-in. Returns its id, or None if nobody is
        signed in or the account couldn't be identified yet (offline, expired
        token): in that case nothing is stored, rather than risking filing one
        account's sign-in under another's id."""
        with self._lock:
            oauth = self._live_oauth()
            if not oauth:
                self._set_state(live_uuid=None, live_fp=None)
                return None
            rfp = fingerprint(oauth["refreshToken"])
            uuid = self._known_uuid_for(rfp)
            profile = None
            if not uuid:
                profile = self._profile(oauth.get("accessToken", ""))
                if not profile:
                    self.log("accounts: couldn't identify the signed-in account yet")
                    return None
                uuid = profile["uuid"]
            self._dir(uuid)  # validates the id

            try:
                cfg_acct = (_read_json(self.paths.config) or {}).get("oauthAccount")
            except ValueError:
                cfg_acct = None
            rec = self._load_signin(uuid) or {}
            if isinstance(cfg_acct, dict) and cfg_acct.get("accountUuid") == uuid:
                rec["oauthAccount"] = cfg_acct
            rec["claudeAiOauth"] = oauth
            self._save_signin(uuid, rec)

            meta = self._meta(uuid)
            changes = {"refresh_fp": rfp, "live_seen_at": self.clock(),
                       "relogin_by": (oauth.get("refreshTokenExpiresAt") or 0) / 1000,
                       "needs_relogin": False}
            if profile:
                changes.update(email=profile["email"], plan=profile["plan"],
                               display_name=profile["display_name"])
            if "added_at" not in meta:
                changes["added_at"] = self.clock()
            if not meta.get("nickname"):
                email = changes.get("email") or meta.get("email") or ""
                changes["nickname"] = (email.split("@")[0] or uuid[:8])[:NICKNAME_MAX]
            self._update_meta(uuid, **changes)
            self._set_state(live_uuid=uuid, live_fp=rfp)
            return uuid

    # -- swapping -------------------------------------------------------------

    def swap_to(self, uuid: str) -> dict:
        """Make `uuid` the account Claude Code uses. Stores the current one first."""
        with self._lock, _FileLock(self.paths.store / ".lock"):
            rec = self._load_signin(uuid)
            if not rec or not rec.get("claudeAiOauth"):
                raise AccountError("That account isn't in the store.")
            if self._meta(uuid).get("needs_relogin"):
                raise AccountError("That account's sign-in has expired; sign in to it again.")

            live = self.sync_live()
            if live == uuid:
                return {"ok": True, "changed": False, "live": uuid}
            if live is None and self._live_oauth():
                raise AccountError(
                    "The current sign-in couldn't be identified (offline?). Not "
                    "swapping, so it isn't lost.")

            # The target is stored, so it's ours to renew if it's nearly expired.
            exp = (rec["claudeAiOauth"].get("expiresAt") or 0) / 1000
            if exp - self.clock() < SWAP_MIN_VALID_S:
                self._renew_locked(uuid)
                rec = self._load_signin(uuid)

            creds = _read_json(self.paths.credentials) or {}
            creds["claudeAiOauth"] = rec["claudeAiOauth"]
            _write_json(self.paths.credentials, creds)

            acct_block = rec.get("oauthAccount")
            if not acct_block:
                meta = self._meta(uuid)
                acct_block = {"accountUuid": uuid,
                              "emailAddress": meta.get("email", ""),
                              "displayName": meta.get("display_name", "")}
            if self.paths.config.exists():
                cfg = _read_json(self.paths.config)  # raises if unreadable: don't clobber
            else:
                cfg = {}
            cfg["oauthAccount"] = acct_block
            _write_json(self.paths.config, cfg)

            now_oauth = self._live_oauth() or {}
            want = fingerprint(rec["claudeAiOauth"]["refreshToken"])
            if fingerprint(now_oauth.get("refreshToken")) != want:
                raise AccountError("The swap didn't stick; the sign-in file changed underneath it.")
            self._set_state(live_uuid=uuid, live_fp=want)
            self._update_meta(uuid, swapped_in_at=self.clock())
            self.log(f"accounts: swapped to {self._meta(uuid).get('nickname', uuid)}")
            return {"ok": True, "changed": True, "live": uuid, "previous": live}

    def prepare_new_login(self) -> str | None:
        """Store the current account, then remove only the Claude sign-in so
        `/login` in Claude Code can add another account. Never sends a logout."""
        with self._lock, _FileLock(self.paths.store / ".lock"):
            live = self.sync_live()
            if live is None and self._live_oauth():
                raise AccountError(
                    "The current sign-in couldn't be identified (offline?). Not "
                    "clearing it, so it isn't lost.")
            creds = _read_json(self.paths.credentials)
            if creds is not None and "claudeAiOauth" in creds:
                del creds["claudeAiOauth"]
                _write_json(self.paths.credentials, creds)
            if self.paths.config.exists():
                cfg = _read_json(self.paths.config)
                if "oauthAccount" in cfg:
                    del cfg["oauthAccount"]
                    _write_json(self.paths.config, cfg)
            self._set_state(live_uuid=None, live_fp=None)
            return live

    # -- renewal ----------------------------------------------------------------

    def renew(self, uuid: str) -> None:
        with self._lock:
            self._renew_locked(uuid)

    def _renew_locked(self, uuid: str) -> None:
        if uuid == self._state().get("live_uuid"):
            raise AccountError("The live account is renewed by Claude Code, not by us.")
        rec = self._load_signin(uuid)
        if not rec or not rec.get("claudeAiOauth"):
            raise AccountError("That account isn't in the store.")
        oauth = rec["claudeAiOauth"]
        live = self._live_oauth()
        if live and live.get("refreshToken") == oauth.get("refreshToken"):
            raise AccountError("That sign-in is currently live; Claude Code renews it.")

        code, data = self.http.post_json(TOKEN_URL, {
            "grant_type": "refresh_token",
            "refresh_token": oauth["refreshToken"],
            "client_id": CLIENT_ID,
            "scope": " ".join(oauth.get("scopes") or ["user:inference"])})
        if code != 200 or not data.get("access_token"):
            err = data.get("error") or f"HTTP {code}"
            if err == "invalid_grant":
                self._update_meta(uuid, needs_relogin=True)
            self._update_meta(uuid, renew_error=err, renew_error_at=self.clock())
            raise AccountError(f"Renewing the sign-in failed: {err}")

        acct = data.get("account") if isinstance(data.get("account"), dict) else {}
        if acct.get("uuid") and acct["uuid"] != uuid:
            raise AccountError("Renewal returned a different account; nothing was saved.")

        now = self.clock()
        oauth["accessToken"] = data["access_token"]
        oauth["refreshToken"] = data.get("refresh_token") or oauth["refreshToken"]
        oauth["expiresAt"] = int((now + int(data.get("expires_in") or 28800)) * 1000)
        if data.get("refresh_token_expires_in"):
            oauth["refreshTokenExpiresAt"] = int(
                (now + int(data["refresh_token_expires_in"])) * 1000)
        if data.get("scope"):
            oauth["scopes"] = data["scope"].split()
        rec["claudeAiOauth"] = oauth
        self._save_signin(uuid, rec)
        self._update_meta(uuid, refresh_fp=fingerprint(oauth["refreshToken"]),
                          renewed_at=now, renew_error=None,
                          relogin_by=(oauth.get("refreshTokenExpiresAt") or 0) / 1000)

    def renew_due(self) -> None:
        live = self._state().get("live_uuid")
        for uuid in self._uuids():
            if uuid == live or self._meta(uuid).get("needs_relogin"):
                continue
            rec = self._load_signin(uuid) or {}
            exp = ((rec.get("claudeAiOauth") or {}).get("expiresAt") or 0) / 1000
            if exp - self.clock() < RENEW_BEFORE_S:
                try:
                    self.renew(uuid)
                except AccountError as e:
                    self.log(f"accounts: {self._meta(uuid).get('nickname', uuid)}: {e}")

    # -- usage ------------------------------------------------------------------

    @staticmethod
    def parse_usage(data: dict, now: float) -> dict:
        """`s` is None when the account has no 5-hour limit. Some plans (seen on
        Max 20x, 2026-11) report only weekly limits: five_hour is null even
        while in use, and `limits` lists only weekly kinds."""
        five = data.get("five_hour")
        seven = data.get("seven_day") or {}
        s = float(five.get("utilization") or 0) if isinstance(five, dict) else None
        w = float(seven.get("utilization") or 0)
        limited = (s or 0) >= 100 or w >= 100
        for lim in data.get("limits") or []:
            if isinstance(lim, dict) and lim.get("is_active") and (lim.get("percent") or 0) >= 100:
                limited = True
        return {"s": None if s is None else round(s),
                "s_reset_at": _iso_to_epoch((five or {}).get("resets_at")),
                "w": round(w), "w_reset_at": _iso_to_epoch(seven.get("resets_at")),
                "limited": limited, "at": now}

    def poll(self, uuid: str) -> dict | None:
        """Read one account's usage (free endpoint, no quota used)."""
        with self._lock:
            live = self._state().get("live_uuid")
            if uuid == live:
                oauth = self._live_oauth() or {}
            else:
                rec = self._load_signin(uuid) or {}
                oauth = rec.get("claudeAiOauth") or {}
                if ((oauth.get("expiresAt") or 0) / 1000) - self.clock() < 60:
                    try:
                        self._renew_locked(uuid)
                    except AccountError as e:
                        self._update_meta(uuid, usage_error=str(e))
                        return None
                    oauth = (self._load_signin(uuid) or {}).get("claudeAiOauth") or {}
            token = oauth.get("accessToken")
        if not token:
            return None
        code, data = self.http.get_json(f"{API_BASE}/api/oauth/usage", token)
        with self._lock:
            if code != 200 or not isinstance(data, dict):
                self._update_meta(uuid, usage_error=data.get("error") if isinstance(data, dict) else f"HTTP {code}",
                                  usage_error_at=self.clock())
                return None
            usage = self.parse_usage(data, self.clock())
            self._update_meta(uuid, usage=usage, usage_error=None)
            return usage

    def poll_all(self) -> None:
        """Re-read the live account and fetch usage for every account now."""
        self.sync_live()
        for uuid in self._uuids():
            if not self._meta(uuid).get("needs_relogin"):
                self.poll(uuid)

    def poll_due(self) -> None:
        live = self._state().get("live_uuid")
        now = self.clock()
        for uuid in self._uuids():
            meta = self._meta(uuid)
            if meta.get("needs_relogin"):
                continue
            last = ((meta.get("usage") or {}).get("at") or 0)
            last = max(last, meta.get("usage_error_at") or 0)
            every = LIVE_POLL_S if uuid == live else PARKED_POLL_S
            if now - last >= every:
                self.poll(uuid)

    # -- listing / editing ---------------------------------------------------

    def accounts(self) -> list[dict]:
        """Everything the UI and the gadget need, live account first."""
        live = self._state().get("live_uuid")
        now = self.clock()
        out = []
        for uuid in self._uuids():
            m = self._meta(uuid)
            usage = m.get("usage")
            relogin_by = m.get("relogin_by") or 0
            out.append({
                "uuid": uuid,
                "nickname": m.get("nickname") or uuid[:8],
                "email": m.get("email", ""),
                "plan": m.get("plan", ""),
                "live": uuid == live,
                "usage": usage,
                "usage_age_s": (now - usage["at"]) if usage else None,
                "has_room": (not usage["limited"]) if usage else None,
                "relogin_by": relogin_by or None,
                "relogin_soon": bool(relogin_by) and relogin_by - now < RELOGIN_WARN_DAYS * 86400,
                "needs_relogin": bool(m.get("needs_relogin")),
            })
        out.sort(key=lambda a: (not a["live"], a["nickname"].lower()))
        return out

    def resolve(self, name: str) -> str:
        """Account id from an id, id prefix, nickname or email (case-insensitive)."""
        name = (name or "").strip().lower()
        hits = [a["uuid"] for a in self.accounts()
                if name in (a["uuid"].lower(), a["nickname"].lower(), a["email"].lower())]
        if not hits:
            hits = [a["uuid"] for a in self.accounts() if a["uuid"].lower().startswith(name)]
        if len(hits) != 1:
            raise AccountError(f"No single account matches {name!r}.")
        return hits[0]

    def rename(self, uuid: str, nickname: str) -> None:
        nickname = " ".join((nickname or "").split())[:NICKNAME_MAX]
        if not nickname:
            raise AccountError("The nickname can't be empty.")
        self._dir(uuid)
        self._update_meta(uuid, nickname=nickname)

    def remove(self, uuid: str) -> None:
        with self._lock:
            if uuid == self._state().get("live_uuid"):
                raise AccountError("Can't remove the account that's signed in right now.")
            shutil.rmtree(self._dir(uuid), ignore_errors=True)

    # -- background ---------------------------------------------------------------

    def tick(self) -> None:
        """One round of upkeep: find the live account, renew, read usage."""
        for step in (self.sync_live, self.renew_due, self.poll_due):
            try:
                step()
            except Exception as e:      # keep the background loop alive
                self.log(f"accounts: {step.__name__} failed: {e}")


class AccountsService:
    """Runs AccountManager.tick() in the background."""

    INTERVAL = 60.0

    def __init__(self, manager: AccountManager, on_change=None):
        self.manager = manager
        self._on_change = on_change
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="accounts")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.manager.tick()
            if self._on_change:
                try:
                    self._on_change(self.manager.accounts())
                except Exception:
                    pass
            self._stop.wait(self.INTERVAL)


# --------------------------------------------------------------------------
# Command line:  python accounts.py <command>
# --------------------------------------------------------------------------

def _fmt_when(epoch) -> str:
    if not epoch:
        return "-"
    return time.strftime("%b %d %H:%M", time.localtime(epoch))


def _cli(argv) -> int:
    usage = ("usage: python accounts.py list | sync | poll | swap NAME | "
             "rename NAME NEW | add | remove NAME")
    if not argv:
        print(usage)
        return 2
    mgr = AccountManager(log=print)
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "sync":
            print("live account:", mgr.sync_live() or "(none identified)")
        elif cmd == "poll":
            mgr.sync_live()
            for uuid in mgr._uuids():
                mgr.poll(uuid)
            cmd = "list"
        elif cmd == "swap" and len(args) == 1:
            res = mgr.swap_to(mgr.resolve(args[0]))
            print("already live" if not res["changed"] else "swapped")
            cmd = "list"
        elif cmd == "rename" and len(args) == 2:
            mgr.rename(mgr.resolve(args[0]), args[1])
            cmd = "list"
        elif cmd == "add":
            prev = mgr.prepare_new_login()
            print(f"Stored the current account ({prev or 'none'}) and removed it from "
                  "Claude Code without logging out.\nNow run `claude`, use /login with "
                  "the new account, then run `python accounts.py sync`.")
            return 0
        elif cmd == "remove" and len(args) == 1:
            mgr.remove(mgr.resolve(args[0]))
            cmd = "list"
        elif cmd != "list":
            print(usage)
            return 2
        if cmd == "list":
            for a in mgr.accounts():
                u = a["usage"] or {}
                room = {True: "room", False: "LIMITED", None: "?"}[a["has_room"]]
                flags = (" *re-login soon*" if a["relogin_soon"] else "") + \
                        (" *SIGN IN AGAIN*" if a["needs_relogin"] else "")
                s = "  -" if u.get("s") is None else f"{u['s']:>3}"
                w = "  -" if u.get("w") is None else f"{u['w']:>3}"
                print(f"{'>' if a['live'] else ' '} {a['nickname']:<16} {a['email']:<28} "
                      f"5h {s}%  wk {w}%  {room:<7} "
                      f"re-login by {_fmt_when(a['relogin_by'])}{flags}")
        return 0
    except AccountError as e:
        print("error:", e)
        return 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
