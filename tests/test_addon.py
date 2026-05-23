"""Integration-style tests for the HeaderCustomizer mitmproxy addon.

These tests build a real ``mitmproxy.http.HTTPFlow`` via ``mitmproxy.test.tflow``
and exercise the addon's ``request`` / ``response`` hooks without spinning up
a proxy server.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from mitmproxy.test import tflow

from credit_keeper.addon import HeaderCustomizer
from credit_keeper.cli import main as cli_main


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _flow_for(host: str = "api.example.com", path: str = "/v1/things",
              scheme: str = "http", method: str = "GET"):
    flow = tflow.tflow(resp=True)
    flow.request.host = host
    flow.request.scheme = scheme
    flow.request.path = path
    flow.request.method = method
    # Default port for the scheme so pretty_url stays clean.
    flow.request.port = 80 if scheme == "http" else 443
    return flow


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_addon_set_and_add_on_request(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        """
        rules:
          - name: stamp
            host: "*.example.com"
            request:
              - {action: set, name: User-Agent, value: "credit-keeper/test"}
              - {action: add, name: X-Forwarded-For, value: "10.0.0.1"}
        """,
    )
    addon = HeaderCustomizer(config_path=cfg)
    flow = _flow_for(host="api.example.com")
    flow.request.headers["User-Agent"] = "old-ua/1.0"
    flow.request.headers["X-Forwarded-For"] = "127.0.0.1"

    addon.request(flow)

    assert flow.request.headers["User-Agent"] == "credit-keeper/test"
    xff = flow.request.headers.get_all("X-Forwarded-For")
    assert xff == ["127.0.0.1", "10.0.0.1"]


def test_addon_remove_on_response(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        """
        rules:
          - name: drop-server
            response:
              - {action: remove, name: Server}
        """,
    )
    addon = HeaderCustomizer(config_path=cfg)
    flow = _flow_for()
    flow.response.headers["Server"] = "nginx/1.27"

    # Request hook should be a no-op (rule is response-only).
    addon.request(flow)
    assert flow.response.headers["Server"] == "nginx/1.27"

    addon.response(flow)
    assert "Server" not in flow.response.headers


def test_addon_replace_uses_regex_backref(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        """
        rules:
          - name: redact-bearer
            response:
              - action: replace
                name: Authorization
                pattern: "Bearer (.+)"
                value: "Bearer redacted"
        """,
    )
    addon = HeaderCustomizer(config_path=cfg)
    flow = _flow_for()
    flow.response.headers["Authorization"] = "Bearer s3cret-token"

    addon.response(flow)

    assert flow.response.headers["Authorization"] == "Bearer redacted"


def test_addon_host_filter_skips_non_matching(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        """
        rules:
          - name: only-api
            host: "*.api.example.com"
            request:
              - {action: set, name: X-Stamp, value: "yes"}
        """,
    )
    addon = HeaderCustomizer(config_path=cfg)
    flow = _flow_for(host="example.org")
    before = list(flow.request.headers.items())

    addon.request(flow)

    assert "X-Stamp" not in flow.request.headers
    assert list(flow.request.headers.items()) == before


def test_addon_response_only_rule_does_not_touch_request(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        """
        rules:
          - name: only-response
            apply_to: response
            response:
              - {action: set, name: X-Resp, value: "1"}
        """,
    )
    addon = HeaderCustomizer(config_path=cfg)
    flow = _flow_for()
    before = list(flow.request.headers.items())

    addon.request(flow)
    assert "X-Resp" not in flow.request.headers
    assert list(flow.request.headers.items()) == before

    addon.response(flow)
    assert flow.response.headers["X-Resp"] == "1"


def test_addon_url_regex_match(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        """
        rules:
          - name: auth-token
            url_regex: "^http://auth\\\\.example\\\\.com/.*"
            request:
              - {action: set, name: Authorization, value: "Bearer xyz"}
        """,
    )
    addon = HeaderCustomizer(config_path=cfg)

    matching = _flow_for(host="auth.example.com", path="/login")
    addon.request(matching)
    assert matching.request.headers["Authorization"] == "Bearer xyz"

    other = _flow_for(host="api.example.com", path="/login")
    addon.request(other)
    assert "Authorization" not in other.request.headers


def test_addon_method_filter(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        """
        rules:
          - name: post-only
            methods: [POST]
            request:
              - {action: set, name: X-Method, value: "post"}
        """,
    )
    addon = HeaderCustomizer(config_path=cfg)

    get_flow = _flow_for(method="GET")
    addon.request(get_flow)
    assert "X-Method" not in get_flow.request.headers

    post_flow = _flow_for(method="POST")
    addon.request(post_flow)
    assert post_flow.request.headers["X-Method"] == "post"


def test_addon_rejects_bad_regex_at_load(tmp_path: Path) -> None:
    """Bad regexes must be rejected at config-load time, not silently no-op."""
    from credit_keeper.config import ConfigError

    cfg = _write_config(
        tmp_path,
        """
        rules:
          - name: bad-regex
            response:
              - action: replace
                name: Authorization
                pattern: "(unclosed"
                value: "x"
        """,
    )
    with pytest.raises(ConfigError) as excinfo:
        HeaderCustomizer(config_path=cfg)
    assert "bad-regex" in str(excinfo.value)


def test_cli_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli_main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--config" in out
    assert "--listen-host" in out
    assert "--listen-port" in out


def test_cli_version_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli_main(["--version"])
    assert excinfo.value.code == 0


# ---------------------------------------------------------------------------
# CLI failure modes (config errors must surface as clean error: messages).
# ---------------------------------------------------------------------------


def test_cli_missing_config_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    rc = cli_main(["-c", str(missing)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "credit-keeper: error:" in err
    assert "not found" in err
    # No raw traceback.
    assert "Traceback" not in err


def test_cli_invalid_yaml_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("rules: [\n  - name: oops\n", encoding="utf-8")  # unterminated
    rc = cli_main(["-c", str(cfg)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "credit-keeper: error:" in err
    assert "Traceback" not in err


def test_cli_config_error_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _write_config(
        tmp_path,
        """
        rules:
          - name: bad-action
            request:
              - {action: nuke, name: X}
        """,
    )
    rc = cli_main(["-c", str(cfg)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "credit-keeper: error:" in err
    assert "bad-action" in err
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Defensive branches the previous suite missed.
# ---------------------------------------------------------------------------


def test_addon_response_hook_when_no_response(tmp_path: Path) -> None:
    """A response hook on a flow without a response must be a no-op."""
    cfg = _write_config(
        tmp_path,
        """
        rules:
          - name: drop-server
            response:
              - {action: remove, name: Server}
        """,
    )
    addon = HeaderCustomizer(config_path=cfg)
    flow = tflow.tflow(resp=False)
    flow.request.host = "example.com"
    flow.request.scheme = "http"
    flow.request.path = "/"
    assert flow.response is None

    # Should not raise even though flow.response is None.
    addon.response(flow)


# ---------------------------------------------------------------------------
# Script-addon path: load() registers the option, configure() reloads on it.
# ---------------------------------------------------------------------------


class _FakeLoader:
    def __init__(self) -> None:
        self.options: dict[str, dict[str, object]] = {}

    def add_option(self, *, name: str, typespec: type, default: object, help: str) -> None:
        self.options[name] = {"typespec": typespec, "default": default, "help": help}


class _FakeOptions:
    def __init__(self, **kwargs: object) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class _FakeMaster:
    def __init__(self) -> None:
        self.shutdown_called = False

    def shutdown(self) -> None:
        self.shutdown_called = True


def test_addon_load_registers_config_option() -> None:
    addon = HeaderCustomizer()
    loader = _FakeLoader()
    addon.load(loader)
    assert "config" in loader.options
    assert loader.options["config"]["typespec"] is str
    assert loader.options["config"]["default"] == ""


def test_addon_configure_reloads_from_ctx_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _write_config(
        tmp_path,
        """
        rules:
          - name: stamp
            request:
              - {action: set, name: X-Stamp, value: "1"}
        """,
    )
    addon = HeaderCustomizer()
    assert addon.config.rules == []

    from credit_keeper import addon as addon_module

    monkeypatch.setattr(
        addon_module.ctx, "options", _FakeOptions(config=str(cfg)), raising=False
    )
    addon.configure({"config"})

    assert addon.config_path == cfg
    assert len(addon.config.rules) == 1
    assert addon.config.rules[0].name == "stamp"


def test_addon_configure_failure_shuts_master_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad_cfg = tmp_path / "bad.yaml"
    bad_cfg.write_text(
        "rules:\n  - name: bad\n    request:\n      - {action: nuke, name: X}\n",
        encoding="utf-8",
    )
    addon = HeaderCustomizer()

    from credit_keeper import addon as addon_module

    fake_master = _FakeMaster()
    monkeypatch.setattr(
        addon_module.ctx, "options", _FakeOptions(config=str(bad_cfg)), raising=False
    )
    monkeypatch.setattr(addon_module.ctx, "master", fake_master, raising=False)

    addon.configure({"config"})

    # Failed reload must fail loudly: master.shutdown() called, config not
    # silently left at the previous (empty) value.
    assert fake_master.shutdown_called is True
    assert addon.config.rules == []


def test_addon_running_prints_banner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _write_config(
        tmp_path,
        """
        rules:
          - name: stamp
            request:
              - {action: set, name: X, value: "1"}
        """,
    )
    addon = HeaderCustomizer(
        config_path=cfg, listen_host="127.0.0.1", listen_port=18080
    )
    addon.running()
    out = capsys.readouterr().out
    assert "credit-keeper listening on 127.0.0.1:18080" in out
    assert "1 rules" in out
