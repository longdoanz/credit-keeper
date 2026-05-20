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


def test_addon_buggy_rule_does_not_crash(tmp_path: Path) -> None:
    """A rule with an invalid regex should be logged, not raised."""
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
          - name: good-rule
            response:
              - {action: set, name: X-Ok, value: "1"}
        """,
    )
    addon = HeaderCustomizer(config_path=cfg)
    flow = _flow_for()
    flow.response.headers["Authorization"] = "Bearer abc"

    # Should not raise even though the first rule has a broken regex.
    addon.response(flow)

    # Subsequent good rule still applied.
    assert flow.response.headers["X-Ok"] == "1"


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
