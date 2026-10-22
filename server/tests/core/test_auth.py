# SPDX-License-Identifier: AGPL-3.0-or-later
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jesinia.core import auth
from jesinia.core.app import create_app
from jesinia.core.auth import issue_login_nonce, register_session_token
from jesinia.core.config import Config
from tests.conftest import BASE_URL, bearer, lifespan_client


@pytest.fixture
async def client(cfg: Config):  # type: ignore[no-untyped-def]
    async with lifespan_client(create_app(cfg, [])) as c:
        yield c


async def test_api_requires_auth(client: httpx.AsyncClient, cfg: Config) -> None:
    assert (await client.get("/api/ping")).status_code == 401
    assert (await client.get("/api/ping", headers=bearer(cfg.token))).status_code == 200
    assert (await client.get("/api/ping", headers=bearer("wrong"))).status_code == 401
    assert (await client.get("/api/ping", headers={"Authorization": "Basic x"})).status_code == 401


async def test_nonce_login_sets_cookie_once(client: httpx.AsyncClient) -> None:
    n = issue_login_nonce()
    r = await client.get(f"/login?nonce={n}", follow_redirects=False)
    assert r.status_code in (302, 303) and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert "jesinia_session" in r.cookies and "httponly" in cookie and "samesite=strict" in cookie
    assert "path=/" in cookie
    assert (await client.get(f"/login?nonce={n}")).status_code == 401


async def test_cookie_authenticates_api(client: httpx.AsyncClient) -> None:
    await client.get(f"/login?nonce={issue_login_nonce()}", follow_redirects=False)
    assert "jesinia_session" in client.cookies  # the client keeps the cookie
    ok = await client.get("/api/ping")
    assert ok.status_code == 200 and ok.json() == {"ok": True, "principal": "user"}
    auth.invalidate_sessions()
    assert (await client.get("/api/ping")).status_code == 401


async def test_expired_nonce_rejected(client: httpx.AsyncClient) -> None:
    n = issue_login_nonce(ttl=timedelta(seconds=-1))
    assert (await client.get(f"/login?nonce={n}")).status_code == 401
    assert (await client.get("/login")).status_code in (401, 422)


async def test_session_token_principal(client: httpx.AsyncClient) -> None:
    register_session_token("sess-token", "s-1")
    r = await client.get("/api/ping", headers=bearer("sess-token"))
    assert r.json() == {"ok": True, "principal": "session", "session_id": "s-1"}
    auth.revoke_session_token("sess-token")
    assert (await client.get("/api/ping", headers=bearer("sess-token"))).status_code == 401


async def test_nonce_endpoint_issues_login_url(client: httpx.AsyncClient, cfg: Config) -> None:
    assert (await client.post("/api/auth/nonce")).status_code == 401
    body = (await client.post("/api/auth/nonce", headers=bearer(cfg.token))).json()
    assert body["url"] == f"{BASE_URL}/login?nonce={body['nonce']}"
    assert (
        await client.get(body["url"].removeprefix(BASE_URL), follow_redirects=False)
    ).status_code in (302, 303)


async def test_session_principal_cannot_mint_nonces(client: httpx.AsyncClient) -> None:
    register_session_token("agent", "s-2")
    assert (await client.post("/api/auth/nonce", headers=bearer("agent"))).status_code == 403


async def test_reload_token_adopts_rotated_token_and_signs_out(
    client: httpx.AsyncClient, cfg: Config
) -> None:
    from dataclasses import replace

    from jesinia.core.config import save_config

    nonce = issue_login_nonce()
    await client.get(f"/login?nonce={nonce}")
    assert (await client.get("/api/ping")).status_code == 200  # cookie session
    save_config(replace(cfg, token="n" * 43), cfg.data_dir / "config.toml")
    r = await client.post("/api/auth/reload-token", headers=bearer(cfg.token))
    assert r.status_code == 200
    assert (await client.get("/api/ping")).status_code == 401  # cookie signed out
    assert (await client.get("/api/ping", headers=bearer(cfg.token))).status_code == 401
    assert (await client.get("/api/ping", headers=bearer("n" * 43))).status_code == 200


async def test_rotate_issues_new_token_signs_out_and_calls_hook(cfg: Config) -> None:
    from jesinia.core.config import load_config, save_config

    save_config(cfg, cfg.data_dir / "config.toml")
    seen: list[str] = []
    app = create_app(cfg, [], static_root=None, on_token_rotated=lambda c: seen.append(c.token))
    async with lifespan_client(app) as c:
        await c.get(f"/login?nonce={issue_login_nonce()}")
        assert (await c.post("/api/auth/rotate")).status_code == 200  # cookie auth
        new = load_config(cfg.data_dir / "config.toml").token
        assert new != cfg.token and seen == [new]
        assert (await c.get("/api/ping")).status_code == 401
        assert (await c.get("/api/ping", headers=bearer(new))).status_code == 200
    app2 = create_app(cfg, [], static_root=None)
    async with lifespan_client(app2) as c:
        assert (await c.post("/api/auth/rotate", headers=bearer(cfg.token))).status_code == 200


async def test_rotation_survives_a_failing_agent_refresh(cfg: Config) -> None:
    from jesinia.core.config import load_config, save_config

    save_config(cfg, cfg.data_dir / "config.toml")

    def broken(c: Config) -> None:
        raise PermissionError("~/.claude.json is read-only")

    app = create_app(cfg, [], static_root=None, on_token_rotated=broken)
    async with lifespan_client(app) as c:
        r = await c.post("/api/auth/rotate", headers=bearer(cfg.token))
        assert r.status_code == 200 and r.json()["agents_refreshed"] is False
        assert "read-only" in r.json()["warning"]
        new = load_config(cfg.data_dir / "config.toml").token
        assert (await c.get("/api/ping", headers=bearer(new))).status_code == 200


async def _sign_in(c: httpx.AsyncClient) -> str:
    r = await c.get(f"/login?nonce={issue_login_nonce()}", follow_redirects=False)
    return r.cookies[auth.COOKIE]


async def test_should_set_a_thirty_day_cookie_on_login(client: httpx.AsyncClient) -> None:
    r = await client.get(f"/login?nonce={issue_login_nonce()}", follow_redirects=False)
    assert f"max-age={30 * 24 * 3600}" in r.headers["set-cookie"].lower()


async def test_should_keep_browser_signed_in_across_server_restart(cfg: Config) -> None:
    async with lifespan_client(create_app(cfg, [], static_root=None)) as c:
        sid = await _sign_in(c)
    auth.reset_state()  # a new server process starts with nothing in memory
    async with lifespan_client(create_app(cfg, [], static_root=None)) as c:
        c.cookies.set(auth.COOKIE, sid)
        assert (await c.get("/api/ping")).status_code == 200


async def test_should_store_only_session_hashes_with_owner_only_mode(cfg: Config) -> None:
    async with lifespan_client(create_app(cfg, [], static_root=None)) as c:
        sid = await _sign_in(c)
    path = cfg.data_dir / auth.SESSIONS_FILE
    assert sid not in path.read_text() and (path.stat().st_mode & 0o777) == 0o600


async def test_should_stay_signed_out_after_rotation_and_restart(cfg: Config) -> None:
    async with lifespan_client(create_app(cfg, [], static_root=None)) as c:
        sid = await _sign_in(c)
        auth.invalidate_sessions()
    auth.reset_state()
    async with lifespan_client(create_app(cfg, [], static_root=None)) as c:
        c.cookies.set(auth.COOKIE, sid)
        assert (await c.get("/api/ping")).status_code == 401


async def test_should_reject_expired_session(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    async with lifespan_client(create_app(cfg, [], static_root=None)) as c:
        await _sign_in(c)
        monkeypatch.setattr(auth, "_now", lambda: datetime.now(UTC) + timedelta(days=31))
        assert (await c.get("/api/ping")).status_code == 401
        assert (await c.get("/api/ping")).status_code == 401  # forgotten, not resurrected


async def test_should_renew_session_and_cookie_after_a_day_of_use(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with lifespan_client(create_app(cfg, [], static_root=None)) as c:
        await _sign_in(c)
        fresh = await c.get("/api/ping")
        assert "set-cookie" not in fresh.headers  # renewed at most once a day
        later = datetime.now(UTC) + timedelta(days=20)
        monkeypatch.setattr(auth, "_now", lambda: later)
        renewed = await c.get("/api/ping")
        assert renewed.status_code == 200
        assert f"max-age={30 * 24 * 3600}" in renewed.headers["set-cookie"].lower()
        monkeypatch.setattr(auth, "_now", lambda: later + timedelta(days=25))
        assert (await c.get("/api/ping")).status_code == 200  # 45 days after login


async def test_should_start_empty_when_sessions_file_is_corrupt(
    cfg: Config, caplog: pytest.LogCaptureFixture
) -> None:
    cfg.data_dir.mkdir(parents=True)
    (cfg.data_dir / auth.SESSIONS_FILE).write_text("{not json")
    async with lifespan_client(create_app(cfg, [], static_root=None)) as c:
        c.cookies.set(auth.COOKIE, "anything")
        assert (await c.get("/api/ping")).status_code == 401
        await _sign_in(c)
        assert (await c.get("/api/ping")).status_code == 200
    assert "JSONDecodeError" in caplog.text or "Expecting" in caplog.text


async def test_should_keep_login_working_when_sessions_file_cannot_be_written(
    cfg: Config, caplog: pytest.LogCaptureFixture
) -> None:
    cfg.data_dir.mkdir(parents=True)
    (cfg.data_dir / auth.SESSIONS_FILE).mkdir()  # a directory: every write fails
    async with lifespan_client(create_app(cfg, [], static_root=None)) as c:
        await _sign_in(c)
        assert (await c.get("/api/ping")).status_code == 200
    assert "could not save browser sessions" in caplog.text


async def test_should_drop_expired_sessions_when_loading(cfg: Config) -> None:
    import json

    cfg.data_dir.mkdir(parents=True)
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    saved = {"token": auth._fingerprint(cfg.token), "sessions": {"stale": past, "live": future}}
    (cfg.data_dir / auth.SESSIONS_FILE).write_text(json.dumps(saved))
    async with lifespan_client(create_app(cfg, [], static_root=None)) as c:
        await _sign_in(c)
    kept = (cfg.data_dir / auth.SESSIONS_FILE).read_text()
    assert "stale" not in kept and "live" in kept


async def test_cookie_renewal_should_pass_non_http_scopes_through() -> None:
    from starlette.types import Message, Receive, Scope, Send

    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(str(scope["type"]))

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        return None

    await auth.SessionCookieRenewal(inner)({"type": "lifespan"}, receive, send)
    assert seen == ["lifespan"]


async def test_should_drop_sessions_after_offline_token_rotation(cfg: Config) -> None:
    from dataclasses import replace

    async with lifespan_client(create_app(cfg, [], static_root=None)) as c:
        sid = await _sign_in(c)
    auth.reset_state()  # `jesinia token rotate` while the server is down
    rotated = replace(cfg, token="r" * 43)
    async with lifespan_client(create_app(rotated, [], static_root=None)) as c:
        c.cookies.set(auth.COOKIE, sid)
        assert (await c.get("/api/ping")).status_code == 401


async def test_should_drop_sessions_when_sign_out_save_failed_and_token_changed(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    async with lifespan_client(create_app(cfg, [], static_root=None)) as c:
        sid = await _sign_in(c)

        def refuse(*args: object) -> None:
            raise PermissionError("read-only")

        monkeypatch.setattr("os.replace", refuse)
        auth.invalidate_sessions("r" * 43)  # the stale file stays on disk
    monkeypatch.undo()
    auth.reset_state()
    async with lifespan_client(create_app(replace(cfg, token="r" * 43), [], static_root=None)) as c:
        c.cookies.set(auth.COOKIE, sid)
        assert (await c.get("/api/ping")).status_code == 401


async def test_should_keep_new_sessions_after_in_server_rotation_and_restart(cfg: Config) -> None:
    from dataclasses import replace

    from jesinia.core.config import load_config, save_config

    save_config(cfg, cfg.data_dir / "config.toml")
    async with lifespan_client(create_app(cfg, [], static_root=None)) as c:
        assert (await c.post("/api/auth/rotate", headers=bearer(cfg.token))).status_code == 200
        sid = await _sign_in(c)
    new = load_config(cfg.data_dir / "config.toml").token
    auth.reset_state()
    async with lifespan_client(create_app(replace(cfg, token=new), [], static_root=None)) as c:
        c.cookies.set(auth.COOKIE, sid)
        assert (await c.get("/api/ping")).status_code == 200


def test_stream_recheck_should_not_renew_a_session_it_cannot_resend(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    auth.load_sessions(cfg.data_dir / auth.SESSIONS_FILE, cfg.token)
    sid = auth.start_cookie_session()
    req = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(ctx=SimpleNamespace(cfg=cfg))),
        cookies={auth.COOKIE: sid},
        headers={},
        state=SimpleNamespace(),
    )
    later = datetime.now(UTC) + timedelta(days=20)
    monkeypatch.setattr(auth, "_now", lambda: later)
    assert auth.is_authorized(req)  # type: ignore[arg-type]  # a stand-in Request
    assert not hasattr(req.state, auth._RENEW_STATE_KEY)
    monkeypatch.setattr(auth, "_now", lambda: later + timedelta(days=11))
    assert not auth.is_authorized(req)  # type: ignore[arg-type]  # never silently extended


async def test_should_keep_login_working_when_failed_save_cannot_clean_up(
    cfg: Config, caplog: pytest.LogCaptureFixture
) -> None:
    cfg.data_dir.mkdir(parents=True)
    (cfg.data_dir / f".{auth.SESSIONS_FILE}.tmp").mkdir()  # neither writable nor unlinkable
    async with lifespan_client(create_app(cfg, [], static_root=None)) as c:
        await _sign_in(c)
        assert (await c.get("/api/ping")).status_code == 200
    assert "could not save browser sessions" in caplog.text


REMOTE = "mac.tail1234.ts.net"


async def test_should_mark_the_cookie_secure_when_signing_in_over_the_remote_host(
    cfg: Config,
) -> None:
    from dataclasses import replace

    async with lifespan_client(create_app(replace(cfg, remote_host=REMOTE), [])) as c:
        remote = await c.get(
            f"/login?nonce={issue_login_nonce()}", headers={"Host": REMOTE}, follow_redirects=False
        )
        assert "; secure" in remote.headers["set-cookie"].lower()
        local = await c.get(f"/login?nonce={issue_login_nonce()}", follow_redirects=False)
        assert "secure" not in local.headers["set-cookie"].lower()


async def test_should_keep_the_cookie_secure_when_renewing_over_the_remote_host(
    cfg: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    async with lifespan_client(create_app(replace(cfg, remote_host=REMOTE), [])) as c:
        await _sign_in(c)  # the client's jar sends the cookie on every request
        later = datetime.now(UTC) + timedelta(days=2)
        monkeypatch.setattr(auth, "_now", lambda: later)
        r = await c.get("/api/ping", headers={"Host": REMOTE})
        assert r.status_code == 200 and "; secure" in r.headers["set-cookie"].lower()


async def test_nonce_should_offer_a_remote_link_only_when_remote_access_is_on(
    client: httpx.AsyncClient, cfg: Config
) -> None:
    from dataclasses import replace

    off = (await client.post("/api/auth/nonce", headers=bearer(cfg.token))).json()
    assert off["remote_url"] is None
    async with lifespan_client(create_app(replace(cfg, remote_host=REMOTE), [])) as c:
        on = (await c.post("/api/auth/nonce", headers=bearer(cfg.token))).json()
        assert on["remote_url"] == f"https://{REMOTE}/login?nonce={on['nonce']}"
        assert on["url"] == f"{BASE_URL}/login?nonce={on['nonce']}"
