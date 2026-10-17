# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner, Result

from jesinia.cli import install, main
from jesinia.cli.doctor import Check, doctor, run_command
from jesinia.core.config import load_config, save_config
from jesinia.core.db import open_db
from jesinia.services.agents.module import SESSIONS_DB
from jesinia.services.search.module import SEARCH_DB

Cli = Callable[[list[str]], Result]
BASE = "http://127.0.0.1:5374"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("JESINIA_HOME", str(h))
    return h


@pytest.fixture
def refreshed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Never reach the real claude/codex configs from tests."""
    seen: list[str] = []
    monkeypatch.setattr(install, "refresh_registrations", lambda cfg, sys: seen.append(cfg.token))
    return seen


@pytest.fixture
def cli(home: Path, refreshed: list[str]) -> Cli:
    runner = CliRunner()

    def invoke(args: list[str]) -> Result:
        return runner.invoke(main.app, args)

    return invoke


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    d = tmp_path / "kb"
    d.mkdir()
    return d


def test_config_set_and_get(cli: Cli, kb: Path) -> None:
    assert cli(["config", "set", "kb.path", str(kb)]).exit_code == 0
    assert cli(["config", "get", "kb.path"]).stdout.strip() == str(kb)
    assert cli(["config", "get", "server.port"]).stdout.strip() == "5374"
    assert cli(["config", "get", "kbsync.interval"]).stdout.strip() == "4h"
    assert cli(["config", "get", "kbsync.quiet"]).stdout.strip() == "30s"
    assert cli(["config", "get", "folders.note"]).stdout.strip() == "Notes"
    bad = cli(["config", "get", "server.token"])
    assert bad.exit_code == 1 and "token show" in bad.stderr
    assert cli(["config", "set", "server.port", "nope"]).exit_code == 2


def test_unreadable_config_exits_2(cli: Cli, home: Path) -> None:
    home.mkdir()
    (home / "config.toml").write_text("[nope]\n")
    r = cli(["token", "show"])
    assert r.exit_code == 2 and "unknown section" in r.stderr


@respx.mock
def test_token_rotate_changes_token(cli: Cli, home: Path, refreshed: list[str]) -> None:
    old = cli(["token", "show"]).stdout.strip()
    route = respx.post(f"{BASE}/api/auth/reload-token").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    r = cli(["token", "rotate"])
    new = cli(["token", "show"]).stdout.strip()
    assert r.exit_code == 0 and new != old and len(new) >= 40
    assert route.calls[0].request.headers["Authorization"] == f"Bearer {old}"
    assert "browser sessions signed out" in r.stdout and refreshed == [new]
    assert (home / "config.toml").stat().st_mode & 0o777 == 0o600


@respx.mock
def test_token_rotate_with_server_down(cli: Cli) -> None:
    respx.post(f"{BASE}/api/auth/reload-token").mock(side_effect=httpx.ConnectError("refused"))
    r = cli(["token", "rotate"])
    assert r.exit_code == 0 and "not running" in r.stdout


def test_backup_copies_history_dbs_only(cli: Cli, home: Path, tmp_path: Path) -> None:
    load_config(home / "config.toml")
    for spec in (SESSIONS_DB, SEARCH_DB):
        open_db(home, spec).close()
    r = cli(["backup", "--dest", str(tmp_path / "bk")])
    out = Path(r.stdout.strip())
    assert r.exit_code == 0 and out.parent == tmp_path / "bk"
    assert (out / "sessions.sqlite").exists() and not (out / "search.sqlite").exists()
    assert (out / "config.toml").stat().st_mode & 0o777 == 0o600
    conn = sqlite3.connect(out / "sessions.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] >= 1
    conn.close()
    default = Path(cli(["backup"]).stdout.strip())
    assert default.parent == home / "backups"


def test_doctor_reports_missing_kb_as_fail_with_remedy(
    cli: Cli, home: Path, tmp_path: Path
) -> None:
    cfg = load_config(home / "config.toml")
    save_config(
        cfg.__class__(**{**cfg.__dict__, "kb_path": tmp_path / "gone"}), home / "config.toml"
    )
    r = cli(["doctor"])
    assert r.exit_code == 1
    assert "fail kb.path" in r.stdout and "→ jesinia config set kb.path" in r.stdout


def test_openapi_includes_kb_routes(cli: Cli) -> None:
    doc = json.loads(cli(["openapi"]).stdout)
    assert "/api/kb/pages/{id}" in doc["paths"] and "/api/search/reindex" in doc["paths"]


@respx.mock
def test_status_prints_health_and_login(cli: Cli) -> None:
    respx.get(f"{BASE}/healthz").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": False,
                "modules": {
                    "kb": {"ok": True, "detail": "3 pages indexed"},
                    "github": {"ok": False, "detail": "x"},
                },
            },
        )
    )
    respx.post(f"{BASE}/api/auth/nonce").mock(
        return_value=httpx.Response(200, json={"nonce": "n", "url": f"{BASE}/login?nonce=n"})
    )
    r = cli(["status"])
    assert r.exit_code == 0 and "degraded" in r.stdout and "FAIL github" in r.stdout
    assert f"login: {BASE}/login?nonce=n" in r.stdout


@respx.mock
def test_status_when_server_down(cli: Cli) -> None:
    respx.get(f"{BASE}/healthz").mock(side_effect=httpx.ConnectError("refused"))
    r = cli(["status"])
    assert r.exit_code == 1 and "not running" in r.stderr


@respx.mock
def test_sync_now(cli: Cli) -> None:
    respx.post(f"{BASE}/api/kbsync/run").mock(
        return_value=httpx.Response(200, json={"result": {"outcome": "pushed", "message": "ok"}})
    )
    assert cli(["sync", "now"]).stdout.strip() == "pushed: ok"
    respx.post(f"{BASE}/api/kbsync/run").mock(side_effect=httpx.ConnectError("refused"))
    assert cli(["sync", "now"]).exit_code == 1


@respx.mock
def test_reindex_online_and_offline(cli: Cli, home: Path) -> None:
    respx.post(f"{BASE}/api/kb/scan").mock(return_value=httpx.Response(200, json={"indexed": 3}))
    respx.post(f"{BASE}/api/search/reindex").mock(
        return_value=httpx.Response(200, json={"indexed": 3})
    )
    assert cli(["reindex"]).stdout.strip() == "kb: 3 pages; search: 3 pages"
    load_config(home / "config.toml")
    open_db(home, SEARCH_DB).close()
    open_db(home, SESSIONS_DB).close()
    respx.post(f"{BASE}/api/kb/scan").mock(side_effect=httpx.ConnectError("refused"))
    r = cli(["reindex"])
    assert "rebuild on start" in r.stdout
    assert not (home / "search.sqlite").exists() and (home / "sessions.sqlite").exists()


def test_serve_runs_uvicorn_with_json_logs(
    cli: Cli, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    seen: dict[str, object] = {}

    def fake_run(app: object, **kw: object) -> None:
        seen.update(kw)
        logging.getLogger("jesinia.test").warning("hello")

    monkeypatch.setattr(uvicorn, "run", fake_run)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        assert cli(["serve"]).exit_code == 0
    finally:
        for h in root.handlers[:]:
            if h not in before:
                root.removeHandler(h)
                h.close()
    assert seen["host"] == "127.0.0.1" and seen["port"] == 5374
    line = (home / "logs/server.log").read_text().splitlines()[-1]
    assert json.loads(line)["msg"] == "hello"


def test_json_formatter_includes_exceptions() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    assert "ValueError: boom" in json.loads(main.JsonFormatter().format(rec))["exc"]


# -- doctor, unit level -----------------------------------------------------------------


def fake_run(responses: dict[str, tuple[int, str]]):  # type: ignore[no-untyped-def]
    def run(argv: list[str], timeout: float) -> tuple[int, str]:
        key = " ".join(argv)
        for prefix, result in responses.items():
            if key.startswith(prefix) or prefix in key:
                return result
        return 0, ""

    return run


def healthy_body(**overrides: object) -> dict[str, object]:
    modules: dict[str, object] = {
        name: {"ok": True, "detail": "ok"} for name in install.required_modules()
    }
    return {"ok": True, "modules": {**modules, **overrides}}


def lines(checks: list[Check]) -> str:
    return "\n".join(c.line() for c in checks)


def test_doctor_missing_and_bad_config(tmp_path: Path) -> None:
    out = lines(doctor(tmp_path / "config.toml", run=fake_run({}), which=lambda n: None))
    assert "fail config" in out and "→ run ./setup" in out and "fail uv: not found" in out
    (tmp_path / "config.toml").write_text("[x]\n")
    assert "fail config: unknown section" in lines(
        doctor(tmp_path / "config.toml", run=fake_run({}), which=lambda n: None)
    )


@respx.mock
def test_doctor_full_pass(tmp_path: Path, kb: Path) -> None:
    path = tmp_path / "cfg" / "config.toml"
    cfg = load_config(path)
    save_config(cfg.__class__(**{**cfg.__dict__, "kb_path": kb}), path)
    respx.get(f"{BASE}/healthz").mock(return_value=httpx.Response(200, json=healthy_body()))
    run = fake_run(
        {
            "rev-parse": (0, "true"),
            " remote": (0, "origin"),
            "ls-remote": (0, "abc refs/heads/main"),
            "--version": (0, "tool 1.0"),
            "auth status": (0, "ok"),
            "launchctl": (0, "loaded"),
        }
    )
    out = lines(doctor(path, run=run, which=lambda n: f"/bin/{n}"))
    assert "fail" not in out and "warn" not in out
    assert "ok kb.git: push to origin" in out and "ok server: healthy" in out


@respx.mock
def test_doctor_warnings(tmp_path: Path, kb: Path) -> None:
    path = tmp_path / "cfg" / "config.toml"
    cfg = load_config(path)
    save_config(cfg.__class__(**{**cfg.__dict__, "kb_path": kb}), path)
    path.chmod(0o644)
    respx.get(f"{BASE}/healthz").mock(
        return_value=httpx.Response(
            200,
            json=healthy_body(kb={"ok": False, "detail": "bad"}),
        )
    )
    run = fake_run(
        {
            "rev-parse": (0, "true"),
            " remote": (0, "origin"),
            "ls-remote": (128, "fatal: could not read"),
            "/bin/gh --version": (0, "gh 2.0"),
            "--version": (1, ""),
            "auth status": (1, "not logged in"),
            "launchctl": (113, "not found"),
        }
    )
    out = lines(doctor(path, run=run, which=lambda n: f"/bin/{n}"))
    assert "fail config: mode 644" in out and "warn kb.git: remote unreachable" in out
    assert "warn gh.auth" in out and "warn launchd" in out and "warn server: kb: bad" in out
    assert (
        "fail uv: /bin/uv --version exited 1" in out
        and "→ reinstall or fix it: brew install uv" in out
    )
    assert "warn pnpm: /bin/pnpm --version exited 1" in out


@respx.mock
def test_doctor_keeps_tool_level_when_version_probe_times_out(tmp_path: Path) -> None:
    run = fake_run({"--version": (1, "Command '/bin/node' timed out after 10 seconds")})
    out = lines(doctor(tmp_path / "config.toml", run=run, which=lambda n: f"/bin/{n}"))
    assert "fail node: /bin/node --version exited 1: Command '/bin/node' timed out" in out
    assert "warn codex: /bin/codex --version exited 1" in out and "ok node" not in out


def server_check(tmp_path: Path, response: httpx.Response) -> str:
    path = tmp_path / "cfg" / "config.toml"
    load_config(path)
    with respx.mock:
        respx.get(f"{BASE}/healthz").mock(return_value=response)
        checks = doctor(path, run=fake_run({}), which=lambda n: None)
    return next(c.line() for c in checks if c.name == "server")


def test_doctor_server_rejects_non_jesinia_responses(tmp_path: Path) -> None:
    for response in (
        httpx.Response(404, json={"detail": "Not Found"}),
        httpx.Response(200, json={"status": "ok"}),
        httpx.Response(200, json=["modules"]),
        httpx.Response(500, json=healthy_body()),
    ):
        line = server_check(tmp_path, response)
        assert line.startswith("fail server: port 5374 is not answering as Jesinia"), line


def test_doctor_server_fails_on_malformed_module_entries(tmp_path: Path) -> None:
    line = server_check(tmp_path, httpx.Response(200, json=healthy_body(kb="up", search={})))
    assert line.startswith("fail server: malformed health for kb, search"), line


def test_doctor_server_applies_required_module_rule(tmp_path: Path) -> None:
    body = healthy_body(agents={"ok": False, "detail": "no claude"})
    assert server_check(tmp_path, httpx.Response(200, json=body)).startswith("ok server")
    body = healthy_body()
    del body["modules"]["kb"]  # type: ignore[attr-defined]
    assert server_check(tmp_path, httpx.Response(200, json=body)) == "warn server: kb: missing"


@respx.mock
def test_doctor_kb_variants(tmp_path: Path, kb: Path) -> None:
    path = tmp_path / "cfg" / "config.toml"
    cfg = load_config(path)
    respx.get(f"{BASE}/healthz").mock(side_effect=httpx.ConnectError("refused"))
    out = lines(doctor(path, run=fake_run({}), which=lambda n: None))
    assert "fail kb.path: not set" in out and "fail server: not answering" in out
    save_config(cfg.__class__(**{**cfg.__dict__, "kb_path": kb}), path)
    out = lines(doctor(path, run=fake_run({"rev-parse": (128, "")}), which=lambda n: None))
    assert "warn kb.git: not a git repository" in out
    out = lines(
        doctor(
            path, run=fake_run({"rev-parse": (0, "true"), " remote": (0, "")}), which=lambda n: None
        )
    )
    assert "ok kb.git: local commits only" in out
    kb.chmod(0o500)
    try:
        assert "fail kb.path: " in lines(doctor(path, run=fake_run({}), which=lambda n: None))
    finally:
        kb.chmod(0o700)


def test_run_command() -> None:
    assert run_command(["python3", "-c", "print('hi')"], 10) == (0, "hi")
    assert run_command(["/nonexistent"], 10)[0] == 1


def test_registry_runs_every_module_in_order() -> None:
    from jesinia.registry import modules

    assert [m.name for m in modules()] == [
        "kb",
        "search",
        "kbsync",
        "agents",
        "github",
        "fetch",
        "notes",
        "review",
    ]


@respx.mock
def test_reindex_timeout_never_deletes_live_databases(cli: Cli, home: Path) -> None:
    load_config(home / "config.toml")
    open_db(home, SEARCH_DB).close()
    respx.post(f"{BASE}/api/kb/scan").mock(side_effect=httpx.ReadTimeout("slow"))
    r = cli(["reindex"])
    assert r.exit_code == 1 and "may still be working" in r.stderr
    assert (home / "search.sqlite").exists()
    respx.get(f"{BASE}/healthz").mock(side_effect=httpx.ReadTimeout("slow"))
    assert cli(["status"]).exit_code == 1
