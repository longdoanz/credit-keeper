"""Tests for credit_keeper.config."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from credit_keeper.config import (
    Config,
    ConfigError,
    HeaderOp,
    Rule,
    load_config,
)


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(dedent(body), encoding="utf-8")
    return p


def test_load_valid_multi_rule_config(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        """
        rules:
          - name: forward-and-ua
            host: "*.api.example.com"
            methods: [GET, POST]
            request:
              - {action: add, name: X-Forwarded-For, value: 10.0.0.1}
              - {action: set, name: User-Agent, value: credit-keeper/0.1}
          - name: drop-server
            response:
              - {action: remove, name: Server}
          - name: redact-auth
            url_regex: "^https://auth\\\\.example\\\\.com/.*"
            response:
              - {action: replace, name: Authorization, pattern: "Bearer (.*)", value: "Bearer redacted"}
        """,
    )

    cfg = load_config(cfg_path)
    assert isinstance(cfg, Config)
    assert len(cfg.rules) == 3

    r1, r2, r3 = cfg.rules
    assert isinstance(r1, Rule)
    assert r1.name == "forward-and-ua"
    assert r1.host == "*.api.example.com"
    assert r1.methods == ["GET", "POST"]
    assert r1.apply_to == "request"
    assert len(r1.request) == 2 and not r1.response
    assert isinstance(r1.request[0], HeaderOp)
    assert r1.request[0].action == "add"
    assert r1.request[1].action == "set"

    assert r2.name == "drop-server"
    # Only response ops -> apply_to flips to 'response'
    assert r2.apply_to == "response"
    assert r2.response[0].action == "remove"

    assert r3.name == "redact-auth"
    assert r3.url_regex is not None
    assert r3.response[0].pattern == "Bearer (.*)"
    assert r3.response[0].value == "Bearer redacted"


def test_unknown_action_raises(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        """
        rules:
          - name: bad-action
            request:
              - {action: nuke, name: X}
        """,
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg_path)
    msg = str(excinfo.value)
    assert "bad-action" in msg
    assert "unknown action" in msg


def test_set_missing_value_raises(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        """
        rules:
          - name: missing-value
            request:
              - {action: set, name: X-Foo}
        """,
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg_path)
    msg = str(excinfo.value)
    assert "missing-value" in msg
    assert "value" in msg


def test_replace_missing_pattern_raises(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        """
        rules:
          - name: missing-pattern
            response:
              - {action: replace, name: Authorization, value: redacted}
        """,
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg_path)
    msg = str(excinfo.value)
    assert "missing-pattern" in msg
    assert "pattern" in msg


def test_apply_to_defaults_to_response_when_only_response_ops(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        """
        rules:
          - name: only-response
            response:
              - {action: remove, name: Server}
        """,
    )
    cfg = load_config(cfg_path)
    assert cfg.rules[0].apply_to == "response"


def test_apply_to_defaults_to_both_when_both_directions_have_ops(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        """
        rules:
          - name: both-dirs
            request:
              - {action: set, name: X-Req, value: a}
            response:
              - {action: remove, name: Server}
        """,
    )
    cfg = load_config(cfg_path)
    assert cfg.rules[0].apply_to == "both"


def test_example_config_loads() -> None:
    """examples/headers.example.yaml must parse cleanly."""
    repo_root = Path(__file__).resolve().parent.parent
    example = repo_root / "examples" / "headers.example.yaml"
    assert example.exists(), f"missing example config at {example}"
    cfg = load_config(example)
    assert isinstance(cfg, Config)
    assert len(cfg.rules) >= 1


def test_invalid_host_regex_raises_at_load(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        """
        rules:
          - name: bad-host-regex
            host_regex: "(unclosed"
            request:
              - {action: set, name: X, value: y}
        """,
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg_path)
    msg = str(excinfo.value)
    assert "bad-host-regex" in msg
    assert "host_regex" in msg


def test_invalid_url_regex_raises_at_load(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        """
        rules:
          - name: bad-url-regex
            url_regex: "[unclosed"
            request:
              - {action: set, name: X, value: y}
        """,
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg_path)
    msg = str(excinfo.value)
    assert "bad-url-regex" in msg
    assert "url_regex" in msg


def test_invalid_replace_pattern_raises_at_load(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        """
        rules:
          - name: bad-replace-regex
            response:
              - action: replace
                name: Authorization
                pattern: "(unclosed"
                value: "x"
        """,
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg_path)
    msg = str(excinfo.value)
    assert "bad-replace-regex" in msg
    assert "invalid regex" in msg
    assert "Authorization" in msg


# ---------------------------------------------------------------------------
# credential_pool config parsing
# ---------------------------------------------------------------------------


def test_credential_pool_valid_config(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        """
        rules: []
        credential_pool:
          enabled: true
          intercept_host: "q.us-east-1.amazonaws.com"
          usage_path: "/getUsageLimits"
          extract_headers: ["Authorization"]
          auto_rotate: true
        """,
    )
    cfg = load_config(cfg_path)
    assert cfg.credential_pool is not None
    assert cfg.credential_pool.enabled is True
    assert cfg.credential_pool.intercept_host == "q.us-east-1.amazonaws.com"
    assert cfg.credential_pool.usage_path == "/getUsageLimits"
    assert cfg.credential_pool.extract_headers == ["Authorization"]
    assert cfg.credential_pool.auto_rotate is True


def test_credential_pool_disabled_by_default(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        """
        rules: []
        credential_pool:
          intercept_host: "example.com"
        """,
    )
    cfg = load_config(cfg_path)
    assert cfg.credential_pool is not None
    assert cfg.credential_pool.enabled is False


def test_credential_pool_missing_intercept_host_when_enabled(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        """
        rules: []
        credential_pool:
          enabled: true
        """,
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(cfg_path)
    msg = str(excinfo.value)
    assert "intercept_host" in msg


def test_credential_pool_absent_means_none(tmp_path: Path) -> None:
    cfg_path = _write(
        tmp_path,
        """
        rules: []
        """,
    )
    cfg = load_config(cfg_path)
    assert cfg.credential_pool is None
