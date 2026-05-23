"""Tests for credit_keeper.rules."""

from __future__ import annotations

import pytest

from credit_keeper.config import HeaderOp, Rule
from credit_keeper.rules import CaseInsensitiveHeaders, apply_ops, rule_matches


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _req_rule(**kwargs) -> Rule:
    kwargs.setdefault(
        "request",
        [HeaderOp(action="set", name="X-Test", value="1")],
    )
    return Rule(name=kwargs.pop("name", "r"), **kwargs)


def test_host_glob_matches_subdomain():
    rule = _req_rule(host="*.example.com")
    assert rule_matches(
        rule,
        host="api.example.com",
        url="https://api.example.com/x",
        method="GET",
        direction="request",
    )


def test_host_glob_does_not_match_other_domain():
    rule = _req_rule(host="*.example.com")
    assert not rule_matches(
        rule,
        host="example.org",
        url="https://example.org/x",
        method="GET",
        direction="request",
    )


def test_host_glob_is_case_insensitive():
    rule = _req_rule(host="*.Example.com")
    assert rule_matches(
        rule,
        host="API.example.COM",
        url="https://api.example.com/",
        method="GET",
        direction="request",
    )


def test_host_regex_matches():
    rule = _req_rule(host_regex=r"^api\.")
    assert rule_matches(
        rule,
        host="api.example.com",
        url="https://api.example.com/",
        method="GET",
        direction="request",
    )
    assert not rule_matches(
        rule,
        host="www.example.com",
        url="https://www.example.com/",
        method="GET",
        direction="request",
    )


def test_url_regex_matches():
    rule = _req_rule(url_regex=r"/v1/users/\d+")
    assert rule_matches(
        rule,
        host="api.example.com",
        url="https://api.example.com/v1/users/42",
        method="GET",
        direction="request",
    )
    assert not rule_matches(
        rule,
        host="api.example.com",
        url="https://api.example.com/v1/orders",
        method="GET",
        direction="request",
    )


def test_method_filter():
    rule = _req_rule(methods=["POST", "PUT"])
    assert rule_matches(
        rule,
        host="x",
        url="https://x/",
        method="post",
        direction="request",
    )
    assert not rule_matches(
        rule,
        host="x",
        url="https://x/",
        method="GET",
        direction="request",
    )


def test_direction_filter_response_only_rule_does_not_match_request():
    rule = Rule(
        name="resp-only",
        apply_to="response",
        response=[HeaderOp(action="remove", name="Server")],
    )
    assert rule_matches(
        rule,
        host="x",
        url="https://x/",
        method="GET",
        direction="response",
    )
    assert not rule_matches(
        rule,
        host="x",
        url="https://x/",
        method="GET",
        direction="request",
    )


def test_apply_to_both_matches_both_directions():
    rule = Rule(
        name="both",
        apply_to="both",
        request=[HeaderOp(action="set", name="X-A", value="1")],
        response=[HeaderOp(action="remove", name="Server")],
    )
    for d in ("request", "response"):
        assert rule_matches(
            rule, host="x", url="https://x/", method="GET", direction=d
        )


def test_rule_without_ops_for_direction_does_not_match():
    rule = Rule(
        name="req-only",
        apply_to="both",
        request=[HeaderOp(action="set", name="X-A", value="1")],
    )
    # No response ops -> response direction should not match.
    assert not rule_matches(
        rule, host="x", url="https://x/", method="GET", direction="response"
    )


# ---------------------------------------------------------------------------
# apply_ops
# ---------------------------------------------------------------------------


def test_set_replaces_all_existing_values():
    h = CaseInsensitiveHeaders([("X-Foo", "a"), ("X-Foo", "b"), ("Other", "z")])
    apply_ops([HeaderOp(action="set", name="x-foo", value="new")], h)
    assert h.get_all("X-Foo") == ["new"]
    assert h.get_all("Other") == ["z"]


def test_add_appends_without_removing_existing():
    h = CaseInsensitiveHeaders([("X-Foo", "a")])
    apply_ops([HeaderOp(action="add", name="X-Foo", value="b")], h)
    assert h.get_all("X-Foo") == ["a", "b"]


def test_remove_drops_all_values_and_is_idempotent():
    h = CaseInsensitiveHeaders([("X-Foo", "a"), ("x-foo", "b")])
    apply_ops([HeaderOp(action="remove", name="X-Foo")], h)
    assert h.get_all("X-Foo") == []
    # Idempotent: removing again does not raise.
    apply_ops([HeaderOp(action="remove", name="X-Foo")], h)
    assert h.get_all("X-Foo") == []


def test_replace_uses_regex_with_backreference():
    h = CaseInsensitiveHeaders([("Authorization", "Bearer secrettoken123")])
    apply_ops(
        [
            HeaderOp(
                action="replace",
                name="Authorization",
                pattern=r"Bearer (.*)",
                value=r"Bearer redacted-\1",
            )
        ],
        h,
    )
    assert h.get_all("Authorization") == ["Bearer redacted-secrettoken123"]


def test_replace_skips_when_header_absent():
    h = CaseInsensitiveHeaders([("Other", "z")])
    apply_ops(
        [
            HeaderOp(
                action="replace",
                name="Authorization",
                pattern=r"Bearer (.*)",
                value=r"Bearer redacted",
            )
        ],
        h,
    )
    assert "Authorization" not in h
    assert h.get_all("Other") == ["z"]


def test_replace_applies_to_each_value():
    h = CaseInsensitiveHeaders(
        [("X-Multi", "Bearer a"), ("X-Multi", "Bearer b")]
    )
    apply_ops(
        [
            HeaderOp(
                action="replace",
                name="X-Multi",
                pattern=r"Bearer (.*)",
                value=r"redacted-\1",
            )
        ],
        h,
    )
    assert h.get_all("X-Multi") == ["redacted-a", "redacted-b"]


def test_case_insensitive_headers_basic_protocol():
    h = CaseInsensitiveHeaders()
    h.add("Content-Type", "text/plain")
    h["content-type"] = "application/json"
    assert h.get_all("Content-Type") == ["application/json"]
    assert "CONTENT-TYPE" in h
    del h["content-type"]
    assert "Content-Type" not in h
    with pytest.raises(KeyError):
        del h["Content-Type"]
