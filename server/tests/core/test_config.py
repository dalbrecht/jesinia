# SPDX-License-Identifier: AGPL-3.0-or-later
import stat
from datetime import timedelta
from pathlib import Path

import pytest

from jesinia.core.config import (
    Config,
    ConfigError,
    format_duration,
    load_config,
    parse_duration,
    save_config,
    set_value,
)
from jesinia.core.paths import config_path, default_data_dir, default_log_dir


def test_defaults_and_token_created_0600(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.port == 5374 and cfg.kbsync_interval == timedelta(hours=4)
    assert cfg.kbsync_quiet == timedelta(seconds=30)
    assert cfg.kb_path is None
    assert dict(cfg.folders) == {"note": "Notes", "review": "Reviews", "source": "Sources"}
    assert cfg.rss_retention == timedelta(days=30)
    assert len(cfg.token) >= 43
    assert stat.S_IMODE((tmp_path / "config.toml").stat().st_mode) == 0o600


def test_token_is_stable_across_loads(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    assert load_config(p).token == load_config(p).token


def test_set_value_parses_durations(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    load_config(p)
    assert set_value(p, "kbsync.interval", "15m").kbsync_interval == timedelta(minutes=15)
    assert load_config(p).kbsync_interval == timedelta(minutes=15)
    with pytest.raises(ConfigError):
        set_value(p, "kbsync.interval", "soon")
    with pytest.raises(ConfigError):
        set_value(p, "nope", "1")


@pytest.mark.parametrize(
    ("key", "raw", "attr", "expected"),
    [
        ("server.port", "5000", "port", 5000),
        ("kbsync.quiet", "45s", "kbsync_quiet", timedelta(seconds=45)),
        ("kbsync.interval", "1h30m", "kbsync_interval", timedelta(hours=1, minutes=30)),
    ],
)
def test_set_value_round_trips(
    tmp_path: Path, key: str, raw: str, attr: str, expected: object
) -> None:
    p = tmp_path / "config.toml"
    load_config(p)
    set_value(p, key, raw)
    assert getattr(load_config(p), attr) == expected


def test_kb_path_and_folders(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    load_config(p)
    kb = tmp_path / "My KB"
    assert set_value(p, "kb.path", str(kb)).kb_path == kb
    assert set_value(p, "folders.source", "Sources").folders["source"] == "Sources"
    cfg = load_config(p)
    assert cfg.kb_path == kb
    assert cfg.folders == {"note": "Notes", "review": "Reviews", "source": "Sources"}
    assert set_value(p, "folders.review", "00 Inbox").folders["review"] == "00 Inbox"
    assert set_value(p, "kb.path", "").kb_path is None


def test_kb_path_expands_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    p = tmp_path / "config.toml"
    load_config(p)
    assert set_value(p, "kb.path", "~/KB").kb_path == tmp_path / "KB"


@pytest.mark.parametrize(
    ("key", "raw"),
    [
        ("server.port", "0"),
        ("server.port", "70000"),
        ("server.port", "abc"),
        ("kbsync.quiet", "0s"),
        ("folders.note", ""),
        ("folders.note", "../x"),
        ("folders.note", "/abs"),
        ("folders.", "X"),
        ("kb.path", "relative/kb"),
    ],
)
def test_bad_values_rejected(tmp_path: Path, key: str, raw: str) -> None:
    p = tmp_path / "config.toml"
    load_config(p)
    with pytest.raises(ConfigError):
        set_value(p, key, raw)


def test_strings_with_quotes_survive(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    load_config(p)
    set_value(p, "folders.quote", 'He said "hi" \\ there')
    assert load_config(p).folders["quote"] == 'He said "hi" \\ there'


def test_corrupt_file_raises_config_error(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text("this is = = not toml")
    with pytest.raises(ConfigError):
        load_config(p)


def test_save_config_is_0600(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    cfg = load_config(p)
    p.chmod(0o644)
    save_config(cfg, p)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_config_is_frozen(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "config.toml")
    assert isinstance(cfg, Config)
    with pytest.raises(AttributeError):
        cfg.port = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("text", "td"),
    [
        ("4h", timedelta(hours=4)),
        ("30s", timedelta(seconds=30)),
        ("2h15m10s", timedelta(seconds=8110)),
        ("30d", timedelta(days=30)),
        ("1d2h", timedelta(days=1, hours=2)),
    ],
)
def test_duration_round_trip(text: str, td: timedelta) -> None:
    assert parse_duration(text) == td and format_duration(td) == text


@pytest.mark.parametrize("bad", ["", "4", "h", "d", "4h1d", "-1h", "1.5h", "1.5d"])
def test_parse_duration_rejects(bad: str) -> None:
    with pytest.raises(ConfigError):
        parse_duration(bad)


def test_paths_default_to_macos_locations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("JESINIA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_data_dir() == tmp_path / "Library/Application Support/jesinia"
    assert default_log_dir() == tmp_path / "Library/Logs/jesinia"
    assert config_path() == default_data_dir() / "config.toml"


def test_jesinia_home_overrides_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("JESINIA_HOME", str(tmp_path / "jh"))
    assert default_data_dir() == tmp_path / "jh"
    assert default_log_dir() == tmp_path / "jh" / "logs"
    assert config_path() == tmp_path / "jh" / "config.toml"


def test_minimal_file_uses_defaults(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('[server]\ntoken = "abc"\n')
    cfg = load_config(p)
    assert cfg.token == "abc" and cfg.port == 5374 and cfg.kb_path is None
    assert cfg.kbsync_interval == timedelta(hours=4)
    assert dict(cfg.folders) == {"note": "Notes", "review": "Reviews", "source": "Sources"}


@pytest.mark.parametrize("text", ["server = 5\n", "[server]\nport = 1\n", '[server]\ntoken = ""\n'])
def test_invalid_structure_rejected(tmp_path: Path, text: str) -> None:
    p = tmp_path / "config.toml"
    p.write_text(text)
    with pytest.raises(ConfigError):
        load_config(p)


@pytest.mark.parametrize(
    "extra",
    ['[kbsync]\nintervel = "5m"\n', '[server]\ntoken = "abc"\nprot = 1\n', "[sever]\nport = 1\n"],
)
def test_unknown_keys_fail_fast(tmp_path: Path, extra: str) -> None:
    p = tmp_path / "config.toml"
    base = '[server]\ntoken = "abc"\n' if not extra.startswith("[server]") else ""
    p.write_text(base + extra)
    with pytest.raises(ConfigError, match="unknown"):
        load_config(p)


@pytest.mark.parametrize(
    "body",
    [
        "[folders]\nnote = 123\n",
        "[folders]\nnote = true\n",
        "[kb]\npath = 5\n",
        "[kbsync]\ninterval = 4\n",
        '[server]\nport = "5374"\n',
        "[server]\nport = true\n",
    ],
)
def test_wrong_value_types_rejected(tmp_path: Path, body: str) -> None:
    p = tmp_path / "config.toml"
    header = '[server]\ntoken = "abc"\n'
    text = header + body if not body.startswith("[server]") else body.replace("[server]\n", header)
    p.write_text(text)
    with pytest.raises(ConfigError, match="must be a"):
        load_config(p)


def test_rss_retention_round_trips_in_whole_days(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    load_config(p)
    assert set_value(p, "rss.retention", "90d").rss_retention == timedelta(days=90)
    assert load_config(p).rss_retention == timedelta(days=90)
    assert '[rss]\nretention = "90d"' in p.read_text()


@pytest.mark.parametrize("raw", ["0d", "366d", "36h", "soon"])
def test_rss_retention_must_be_1_to_365_days(tmp_path: Path, raw: str) -> None:
    p = tmp_path / "config.toml"
    load_config(p)
    with pytest.raises(ConfigError):
        set_value(p, "rss.retention", raw)


def test_rss_section_rejects_unknown_keys_and_types(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    token = load_config(p).token
    for bad in ("[rss]\nnope = 1\n", "[rss]\nretention = 30\n"):
        p.write_text(f'[server]\ntoken = "{token}"\n{bad}')
        with pytest.raises(ConfigError):
            load_config(p)


def test_should_keep_new_folder_defaults_when_an_older_file_sets_only_some(
    tmp_path: Path,
) -> None:
    p = tmp_path / "config.toml"
    p.write_text('[server]\ntoken = "abc"\n\n[folders]\nnote = "My Notes"\nsource = "Sources"\n')
    cfg = load_config(p)
    assert dict(cfg.folders) == {"note": "My Notes", "review": "Reviews", "source": "Sources"}


def test_should_round_trip_remote_host_and_default_to_off(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    assert load_config(p).remote_host is None
    cfg = set_value(p, "server.remote_host", "Mac.Tail1234.ts.net.")
    assert cfg.remote_host == "mac.tail1234.ts.net"
    assert load_config(p).remote_host == "mac.tail1234.ts.net"
    assert set_value(p, "server.remote_host", "").remote_host is None
    assert load_config(p).remote_host is None


@pytest.mark.parametrize(
    "raw",
    [
        "mac",
        "https://mac.ts.net",
        "mac.ts.net:443",
        "mac.ts.net/x",
        "-mac.ts.net",
        "mac..ts.net",
        "a b",
    ],
)
def test_should_reject_a_remote_host_that_is_not_a_bare_dns_name(tmp_path: Path, raw: str) -> None:
    p = tmp_path / "config.toml"
    load_config(p)
    with pytest.raises(ConfigError, match="server.remote_host"):
        set_value(p, "server.remote_host", raw)
