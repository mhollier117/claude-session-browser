"""Tests for accounts.py: swap rules, renewal rules, usage parsing.

Everything runs against fake sign-in files in a temp folder and a fake HTTP
client, so real sign-ins are never touched.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import accounts as A  # noqa: E402

UA, UB = "aaaaaaaa-0000-4000-8000-00000000000a", "bbbbbbbb-0000-4000-8000-00000000000b"
NOW = 1_800_000_000.0


class FakeHttp:
    """Profile/usage keyed by access token; token endpoint rotates tokens."""

    def __init__(self):
        self.owner = {}          # access token -> account uuid
        self.refresh_owner = {}  # refresh token -> account uuid (valid ones only)
        self.usage = {}          # uuid -> usage body
        self.counter = 0
        self.calls = []
        self.offline = False

    def issue(self, uuid):
        self.counter += 1
        at, rt = f"at-{uuid[:1]}-{self.counter}", f"rt-{uuid[:1]}-{self.counter}"
        self.owner[at] = uuid
        self.refresh_owner[rt] = uuid
        return at, rt

    def get_json(self, url, token, timeout=20):
        self.calls.append(("GET", url))
        if self.offline:
            return 0, {"error": "network"}
        uuid = self.owner.get(token)
        if not uuid:
            return 401, {"error": "unauthorized"}
        if url.endswith("/api/oauth/profile"):
            return 200, {"account": {"uuid": uuid, "email": f"{uuid[:1]}@x.com",
                                     "display_name": "Michael"},
                         "organization": {"plan_display_name": "Max"}}
        if url.endswith("/api/oauth/usage"):
            return 200, self.usage.get(uuid, {"five_hour": None, "seven_day": None})
        return 404, {}

    def post_json(self, url, body, timeout=30):
        self.calls.append(("POST", url))
        uuid = self.refresh_owner.pop(body["refresh_token"], None)  # rotation
        if not uuid:
            return 400, {"error": "invalid_grant"}
        at, rt = self.issue(uuid)
        return 200, {"access_token": at, "refresh_token": rt, "expires_in": 28800,
                     "refresh_token_expires_in": 2_000_000,
                     "scope": "user:inference user:profile", "account": {"uuid": uuid}}


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    http, clock = FakeHttp(), Clock()
    paths = A.Paths(claude_dir=tmp_path / "claude", config_file=tmp_path / "claude.json",
                    store_dir=tmp_path / "store")
    paths.claude_dir.mkdir()
    mgr = A.AccountManager(paths, http=http, codec=A.PlainCodec(), clock=clock)
    return mgr, http, clock, paths


def login(paths, http, uuid, clock, mcp=True):
    """What Claude Code's /login leaves behind."""
    at, rt = http.issue(uuid)
    creds = json.loads(paths.credentials.read_text()) if paths.credentials.exists() else {}
    creds["claudeAiOauth"] = {"accessToken": at, "refreshToken": rt,
                              "expiresAt": int((clock() + 28800) * 1000),
                              "refreshTokenExpiresAt": int((clock() + 2_300_000) * 1000),
                              "scopes": ["user:inference", "user:profile"],
                              "subscriptionType": "max"}
    if mcp:
        creds.setdefault("mcpOAuth", {"gmail": {"token": "keep-me"}})
    paths.credentials.write_text(json.dumps(creds))
    cfg = json.loads(paths.config.read_text()) if paths.config.exists() else {"theme": "dark"}
    cfg["oauthAccount"] = {"accountUuid": uuid, "emailAddress": f"{uuid[:1]}@x.com"}
    paths.config.write_text(json.dumps(cfg))
    return at, rt


def live(paths):
    return json.loads(paths.credentials.read_text())


def two_accounts(mgr, http, clock, paths):
    login(paths, http, UA, clock)
    assert mgr.sync_live() == UA
    mgr.prepare_new_login()
    login(paths, http, UB, clock)
    assert mgr.sync_live() == UB


# -- live sync ---------------------------------------------------------------

def test_sync_live_identifies_and_stores_without_connector_signins(env):
    mgr, http, clock, paths = env
    login(paths, http, UA, clock)
    assert mgr.sync_live() == UA
    rec = mgr._load_signin(UA)
    assert "mcpOAuth" not in rec and rec["claudeAiOauth"]["accessToken"].startswith("at-a")
    a = mgr.accounts()[0]
    assert a["live"] and a["nickname"] == "a" and a["plan"] == "Max"
    assert a["relogin_by"] == pytest.approx(NOW + 2_300_000)


def test_sync_live_stores_nothing_when_account_cant_be_identified(env):
    mgr, http, clock, paths = env
    login(paths, http, UA, clock)
    http.offline = True
    assert mgr.sync_live() is None
    assert mgr._uuids() == []


def test_add_account_clears_only_the_claude_signin(env):
    mgr, http, clock, paths = env
    login(paths, http, UA, clock)
    mgr.prepare_new_login()
    creds = live(paths)
    assert "claudeAiOauth" not in creds and creds["mcpOAuth"] == {"gmail": {"token": "keep-me"}}
    cfg = json.loads(paths.config.read_text())
    assert "oauthAccount" not in cfg and cfg["theme"] == "dark"
    assert mgr._uuids() == [UA]
    assert not any(c[0] == "POST" for c in http.calls), "must never log out or renew"


# -- swapping ------------------------------------------------------------------

def test_swap_switches_claude_signin_and_keeps_everything_else(env):
    mgr, http, clock, paths = env
    two_accounts(mgr, http, clock, paths)
    res = mgr.swap_to(UA)
    assert res == {"ok": True, "changed": True, "live": UA, "previous": UB}
    creds = live(paths)
    assert http.owner[creds["claudeAiOauth"]["accessToken"]] == UA
    assert creds["mcpOAuth"] == {"gmail": {"token": "keep-me"}}
    cfg = json.loads(paths.config.read_text())
    assert cfg["oauthAccount"]["accountUuid"] == UA and cfg["theme"] == "dark"
    assert [a["uuid"] for a in mgr.accounts() if a["live"]] == [UA]


def test_swap_stores_the_latest_live_signin_first(env):
    """Rule 2: Claude Code renewed B after our last sync; the swap must store
    B's NEW sign-in, not the stale one, or B would be lost."""
    mgr, http, clock, paths = env
    two_accounts(mgr, http, clock, paths)
    creds = live(paths)
    old_rt = creds["claudeAiOauth"]["refreshToken"]
    at, rt = http.issue(UB)                       # Claude Code renews B
    http.refresh_owner.pop(old_rt)
    creds["claudeAiOauth"].update(accessToken=at, refreshToken=rt)
    paths.credentials.write_text(json.dumps(creds))

    mgr.swap_to(UA)
    assert mgr._load_signin(UB)["claudeAiOauth"]["refreshToken"] == rt
    mgr.swap_to(UB)                               # and B still works afterwards
    assert live(paths)["claudeAiOauth"]["refreshToken"] == rt


def test_swap_refuses_when_live_signin_is_unidentifiable(env):
    mgr, http, clock, paths = env
    two_accounts(mgr, http, clock, paths)
    mgr.prepare_new_login()
    login(paths, http, "cccccccc-0000-4000-8000-00000000000c", clock)
    http.offline = True
    with pytest.raises(A.AccountError, match="couldn't be identified"):
        mgr.swap_to(UA)
    assert http.owner.get(live(paths)["claudeAiOauth"]["accessToken"]) .startswith("c")


def test_swap_to_live_account_is_a_no_op(env):
    mgr, http, clock, paths = env
    two_accounts(mgr, http, clock, paths)
    assert mgr.swap_to(UB)["changed"] is False


def test_swap_renews_a_nearly_expired_target_first(env):
    mgr, http, clock, paths = env
    two_accounts(mgr, http, clock, paths)
    clock.t += 28800 - 60                         # A's token almost expired
    mgr.swap_to(UA)
    exp = live(paths)["claudeAiOauth"]["expiresAt"] / 1000
    assert exp - clock() > 8 * 3600 - 5


# -- renewal ---------------------------------------------------------------------

def test_never_renews_the_live_account(env):
    mgr, http, clock, paths = env
    two_accounts(mgr, http, clock, paths)
    with pytest.raises(A.AccountError, match="Claude Code"):
        mgr.renew(UB)
    clock.t += 28800
    mgr.renew_due()
    assert live(paths)["claudeAiOauth"]["accessToken"] in http.owner
    posts = [c for c in http.calls if c[0] == "POST"]
    assert len(posts) == 1, "only the stored account A gets renewed"


def test_renewal_rotates_and_tracks_relogin_date(env):
    mgr, http, clock, paths = env
    two_accounts(mgr, http, clock, paths)
    before = mgr._load_signin(UA)["claudeAiOauth"]
    mgr.renew(UA)
    after = mgr._load_signin(UA)["claudeAiOauth"]
    assert after["refreshToken"] != before["refreshToken"]
    assert after["refreshTokenExpiresAt"] == int((NOW + 2_000_000) * 1000)
    assert mgr._meta(UA)["relogin_by"] == pytest.approx(NOW + 2_000_000)


def test_dead_renewal_token_marks_account_for_relogin(env):
    mgr, http, clock, paths = env
    two_accounts(mgr, http, clock, paths)
    http.refresh_owner.clear()
    with pytest.raises(A.AccountError, match="invalid_grant"):
        mgr.renew(UA)
    assert [a["needs_relogin"] for a in mgr.accounts() if a["uuid"] == UA] == [True]
    with pytest.raises(A.AccountError, match="sign in to it again"):
        mgr.swap_to(UA)


def test_renewal_for_a_different_account_is_rejected(env):
    mgr, http, clock, paths = env
    two_accounts(mgr, http, clock, paths)
    rt = mgr._load_signin(UA)["claudeAiOauth"]["refreshToken"]
    http.refresh_owner[rt] = UB                   # server says: that's B
    with pytest.raises(A.AccountError, match="different account"):
        mgr.renew(UA)


# -- usage --------------------------------------------------------------------

def test_poll_reads_usage_and_room(env):
    mgr, http, clock, paths = env
    two_accounts(mgr, http, clock, paths)
    http.usage[UA] = {"five_hour": {"utilization": 100.0, "resets_at": "2027-01-15T08:00:00+00:00"},
                      "seven_day": {"utilization": 41.6, "resets_at": "2027-01-20T08:00:00Z"}}
    http.usage[UB] = {"five_hour": None, "seven_day": {"utilization": 12.0, "resets_at": None}}
    mgr.poll_due()
    by = {a["uuid"]: a for a in mgr.accounts()}
    assert by[UA]["usage"]["s"] == 100 and by[UA]["usage"]["w"] == 42
    assert by[UA]["has_room"] is False and by[UB]["has_room"] is True
    assert by[UA]["usage"]["s_reset_at"] > 0


def test_account_without_a_five_hour_limit():
    """Max 20x (seen 2026-11): five_hour is null even while in use."""
    u = A.AccountManager.parse_usage(
        {"five_hour": None, "seven_day": {"utilization": 12.0, "resets_at": "2026-11-04T22:59:59Z"},
         "limits": [{"kind": "weekly_all", "is_active": True, "percent": 12}]}, NOW)
    assert u["s"] is None and u["w"] == 12 and u["limited"] is False


def test_limits_list_can_mark_an_account_limited():
    u = A.AccountManager.parse_usage(
        {"five_hour": {"utilization": 3}, "seven_day": {"utilization": 20},
         "limits": [{"is_active": True, "percent": 100}]}, NOW)
    assert u["limited"] is True


# -- misc ------------------------------------------------------------------------

def test_resolve_rename_remove(env):
    mgr, http, clock, paths = env
    two_accounts(mgr, http, clock, paths)
    mgr.rename(UA, "  Work   main ")
    assert mgr.resolve("work main") == UA and mgr.resolve("bbbb") == UB
    with pytest.raises(A.AccountError):
        mgr.remove(UB)                            # live
    mgr.remove(UA)
    assert mgr._uuids() == [UB]


def test_rejects_path_like_ids(env):
    mgr = env[0]
    with pytest.raises(A.AccountError):
        mgr.rename("../../evil", "x")


def test_stale_lock_is_cleared(env, tmp_path):
    lock = tmp_path / "l.lock"
    lock.write_text("123")
    import os
    os.utime(lock, (0, 0))
    with A._FileLock(lock, timeout=1):
        pass
    assert not lock.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_dpapi_round_trip():
    c = A.DpapiCodec()
    blob = c.encode(b"secret sign-in")
    assert blob != b"secret sign-in" and c.decode(blob) == b"secret sign-in"


def test_fable_weekly_limit_is_parsed_but_never_marks_the_account_limited():
    data = {"five_hour": {"utilization": 10}, "seven_day": {"utilization": 30},
            "limits": [{"kind": "weekly_all", "is_active": True, "percent": 30},
                       {"kind": "weekly_scoped", "is_active": True, "percent": 100,
                        "resets_at": "2027-01-20T08:00:00Z",
                        "scope": {"model": {"id": None, "display_name": "Fable"}}}]}
    u = A.AccountManager.parse_usage(data, NOW)
    assert u["f"] == 100 and u["f_reset_at"] > 0
    assert u["limited"] is False          # other models keep working


def test_no_fable_entry_means_none():
    u = A.AccountManager.parse_usage({"five_hour": None, "seven_day": {"utilization": 5}}, NOW)
    assert u["f"] is None
