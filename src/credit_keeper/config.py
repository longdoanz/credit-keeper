"""YAML config loader and dataclasses for credit-keeper rules."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

logger = logging.getLogger(__name__)

Action = Literal["set", "add", "remove", "replace"]
ApplyTo = Literal["request", "response", "both"]

_VALID_ACTIONS: tuple[str, ...] = ("set", "add", "remove", "replace")
_VALID_APPLY_TO: tuple[str, ...] = ("request", "response", "both")


class ConfigError(ValueError):
    """Raised when a YAML config file fails validation."""


@dataclass
class HeaderOp:
    action: Action
    name: str
    value: str | None = None
    pattern: str | None = None


@dataclass
class Rule:
    name: str
    host: str | None = None
    host_regex: str | None = None
    url_regex: str | None = None
    methods: list[str] | None = None
    apply_to: ApplyTo = "request"
    request: list[HeaderOp] = field(default_factory=list)
    response: list[HeaderOp] = field(default_factory=list)


@dataclass
class CredentialPoolConfig:
    enabled: bool = False
    intercept_hosts: list[str] = field(default_factory=list)
    usage_path: str = "/getUsageLimits"
    extract_headers: list[str] = field(default_factory=lambda: ["Authorization"])
    auto_rotate: bool = True
    refresh_token_header: str = ""


@dataclass
class Config:
    rules: list[Rule] = field(default_factory=list)
    credential_pool: CredentialPoolConfig | None = None


def _fail(rule_name: str, message: str) -> "ConfigError":
    return ConfigError(f"rule {rule_name!r}: {message}")


def _parse_op(rule_name: str, raw: Any) -> HeaderOp:
    if not isinstance(raw, dict):
        raise _fail(rule_name, f"header op must be a mapping, got {type(raw).__name__}")

    action = raw.get("action")
    if action not in _VALID_ACTIONS:
        raise _fail(
            rule_name,
            f"unknown action {action!r}; expected one of {list(_VALID_ACTIONS)}",
        )

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise _fail(rule_name, f"action {action!r} requires a non-empty 'name'")

    value = raw.get("value")
    pattern = raw.get("pattern")

    if action in ("set", "add"):
        if value is None:
            raise _fail(rule_name, f"action {action!r} on header {name!r} requires 'value'")
    elif action == "remove":
        # only name is required; value/pattern are ignored
        pass
    elif action == "replace":
        if pattern is None:
            raise _fail(
                rule_name,
                f"action 'replace' on header {name!r} requires 'pattern'",
            )
        if value is None:
            raise _fail(
                rule_name,
                f"action 'replace' on header {name!r} requires 'value' (replacement string)",
            )
        try:
            re.compile(pattern)
        except re.error as exc:
            raise _fail(
                rule_name,
                f"invalid regex in 'replace' on header {name!r}: {pattern!r}: {exc}",
            ) from exc

    return HeaderOp(action=action, name=name, value=value, pattern=pattern)


def _parse_rule(raw: Any, index: int) -> Rule:
    if not isinstance(raw, dict):
        raise ConfigError(
            f"rule at index {index}: must be a mapping, got {type(raw).__name__}"
        )

    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError(f"rule at index {index}: missing or empty 'name'")

    host = raw.get("host")
    if host is not None and not isinstance(host, str):
        raise _fail(name, "'host' must be a string if provided")

    host_regex = raw.get("host_regex")
    if host_regex is not None and not isinstance(host_regex, str):
        raise _fail(name, "'host_regex' must be a string if provided")
    if isinstance(host_regex, str):
        try:
            re.compile(host_regex)
        except re.error as exc:
            raise _fail(
                name,
                f"invalid 'host_regex' {host_regex!r}: {exc}",
            ) from exc

    url_regex = raw.get("url_regex")
    if url_regex is not None and not isinstance(url_regex, str):
        raise _fail(name, "'url_regex' must be a string if provided")
    if isinstance(url_regex, str):
        try:
            re.compile(url_regex)
        except re.error as exc:
            raise _fail(
                name,
                f"invalid 'url_regex' {url_regex!r}: {exc}",
            ) from exc

    methods = raw.get("methods")
    if methods is not None:
        if not isinstance(methods, list) or not all(isinstance(m, str) for m in methods):
            raise _fail(name, "'methods' must be a list of strings if provided")
        methods = [m.upper() for m in methods]

    request_raw = raw.get("request") or []
    response_raw = raw.get("response") or []
    if not isinstance(request_raw, list):
        raise _fail(name, "'request' must be a list of header ops")
    if not isinstance(response_raw, list):
        raise _fail(name, "'response' must be a list of header ops")

    request_ops = [_parse_op(name, op) for op in request_raw]
    response_ops = [_parse_op(name, op) for op in response_raw]

    apply_to_raw = raw.get("apply_to")
    if apply_to_raw is None:
        # Default to 'request' but flip to 'response' if only response ops are set.
        if response_ops and not request_ops:
            apply_to: ApplyTo = "response"
        elif request_ops and response_ops:
            apply_to = "both"
        else:
            apply_to = "request"
        logger.info(
            "credit-keeper: rule %r: inferred apply_to=%r from populated op lists "
            "(request=%d, response=%d)",
            name,
            apply_to,
            len(request_ops),
            len(response_ops),
        )
    else:
        if apply_to_raw not in _VALID_APPLY_TO:
            raise _fail(
                name,
                f"'apply_to' must be one of {list(_VALID_APPLY_TO)}, got {apply_to_raw!r}",
            )
        apply_to = apply_to_raw

    return Rule(
        name=name,
        host=host,
        host_regex=host_regex,
        url_regex=url_regex,
        methods=methods,
        apply_to=apply_to,
        request=request_ops,
        response=response_ops,
    )


def load_config(path: str | Path) -> Config:
    """Load and validate a YAML config file."""
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if raw is None:
        return Config(rules=[])
    if not isinstance(raw, dict):
        raise ConfigError(
            f"config root must be a mapping with a 'rules' key, got {type(raw).__name__}"
        )

    rules_raw = raw.get("rules", [])
    if not isinstance(rules_raw, list):
        raise ConfigError("'rules' must be a list")

    rules = [_parse_rule(r, i) for i, r in enumerate(rules_raw)]

    credential_pool: CredentialPoolConfig | None = None
    pool_raw = raw.get("credential_pool")
    if pool_raw is not None:
        if not isinstance(pool_raw, dict):
            raise ConfigError(
                "'credential_pool' must be a mapping"
            )
        enabled = pool_raw.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ConfigError("'credential_pool.enabled' must be a boolean")

        # Support both 'intercept_host' (string, backward compat) and
        # 'intercept_hosts' (list).
        intercept_hosts: list[str] = []
        if "intercept_hosts" in pool_raw:
            val = pool_raw["intercept_hosts"]
            if isinstance(val, list):
                intercept_hosts = val
            elif isinstance(val, str):
                intercept_hosts = [val]
            else:
                raise ConfigError(
                    "'credential_pool.intercept_hosts' must be a list of strings or a string"
                )
        elif "intercept_host" in pool_raw:
            val = pool_raw["intercept_host"]
            if isinstance(val, str) and val:
                intercept_hosts = [val]
            elif isinstance(val, str):
                intercept_hosts = []
            else:
                raise ConfigError(
                    "'credential_pool.intercept_host' must be a string"
                )

        if enabled and not intercept_hosts:
            raise ConfigError(
                "'credential_pool.intercept_hosts' is required when credential_pool is enabled"
            )
        usage_path = pool_raw.get("usage_path", "/getUsageLimits")
        extract_headers = pool_raw.get("extract_headers", ["Authorization"])
        auto_rotate = pool_raw.get("auto_rotate", True)
        refresh_token_header = pool_raw.get("refresh_token_header", "")
        credential_pool = CredentialPoolConfig(
            enabled=enabled,
            intercept_hosts=intercept_hosts,
            usage_path=usage_path,
            extract_headers=extract_headers,
            auto_rotate=auto_rotate,
            refresh_token_header=refresh_token_header,
        )

    return Config(rules=rules, credential_pool=credential_pool)
