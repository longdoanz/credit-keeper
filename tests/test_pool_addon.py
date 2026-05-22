"""Tests for the CredentialPoolAddon mitmproxy addon."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mitmproxy.test import tflow

from credit_keeper.config import CredentialPoolConfig
from credit_keeper.db import CredentialDB
from credit_keeper.pool_addon import CredentialPoolAddon


def _make_config(
    intercept_host: str = "q.us-east-1.amazonaws.com",
    usage_path: str = "/getUsageLimits",
    auto_rotate: bool = True,
    extract_headers: list[str] | None = None,
) -> CredentialPoolConfig:
    return CredentialPoolConfig(
        enabled=True,
        intercept_host=intercept_host,
        usage_path=usage_path,
        auto_rotate=auto_rotate,
        extract_headers=extract_headers or ["Authorization"],
    )


def _make_usage_response(
    current_usage: int = 50,
    usage_limit: int = 100,
    user_id: str = "user1",
    subscription_title: str = "Pro Plan",
    display_name: str = "API Calls",
    resource_type: str = "api_calls",
    unit: str = "calls",
    days_until_reset: int = 15,
    next_date_reset: float = 1700000000.0,
) -> str:
    return json.dumps({
        "usageBreakdownList": [
            {
                "currentUsage": current_usage,
                "usageLimit": usage_limit,
                "displayName": display_name,
                "resourceType": resource_type,
                "unit": unit,
            }
        ],
        "userInfo": {"userId": user_id},
        "subscriptionInfo": {"subscriptionTitle": subscription_title},
        "daysUntilReset": days_until_reset,
        "nextDateReset": next_date_reset,
    })


def _flow_for(
    host: str = "q.us-east-1.amazonaws.com",
    path: str = "/getUsageLimits",
    auth: str = "Bearer token123",
) -> tflow.tflow:
    flow = tflow.tflow(resp=True)
    flow.request.host = host
    flow.request.scheme = "https"
    flow.request.path = path
    flow.request.port = 443
    flow.request.headers["Authorization"] = auth
    return flow


def test_response_hook_extracts_usage_and_stores(tmp_path: Path) -> None:
    db = CredentialDB(tmp_path / "test.db")
    config = _make_config()
    addon = CredentialPoolAddon(config, db)

    flow = _flow_for()
    flow.response.set_text(_make_usage_response(current_usage=50, usage_limit=100))

    addon.request(flow)
    addon.response(flow)

    # Credential should be stored
    with db._lock:
        cred = db._conn.execute(
            "SELECT client_id, subscription_title, is_exhausted FROM credentials"
        ).fetchone()
    assert cred is not None
    assert cred[0] == "user1"
    assert cred[1] == "Pro Plan"
    assert cred[2] == 0  # not exhausted

    # Usage snapshot should be stored
    with db._lock:
        snap = db._conn.execute(
            "SELECT current_usage, usage_limit, resource_type FROM usage_snapshots"
        ).fetchone()
    assert snap == (50, 100, "api_calls")


def test_response_hook_marks_exhausted(tmp_path: Path) -> None:
    db = CredentialDB(tmp_path / "test.db")
    config = _make_config()
    addon = CredentialPoolAddon(config, db)

    flow = _flow_for()
    flow.response.set_text(_make_usage_response(current_usage=100, usage_limit=100))

    addon.request(flow)
    addon.response(flow)

    auth_hash = hashlib.sha256(b"Bearer token123").hexdigest()
    with db._lock:
        row = db._conn.execute(
            "SELECT is_exhausted FROM credentials WHERE auth_hash = ?",
            (auth_hash,),
        ).fetchone()
    assert row is not None
    assert row[0] == 1


def test_request_hook_rotates_exhausted_credential(tmp_path: Path) -> None:
    db = CredentialDB(tmp_path / "test.db")
    config = _make_config(auto_rotate=True)
    addon = CredentialPoolAddon(config, db)

    # Pre-populate: one exhausted credential, one available
    exhausted_auth = "Bearer exhausted_token"
    available_auth = "Bearer available_token"
    h_exhausted = db.upsert_credential("user1", exhausted_auth, "Plan A")
    db.upsert_credential("user2", available_auth, "Plan B")
    db.mark_exhausted(h_exhausted)

    flow = _flow_for(auth=exhausted_auth)
    addon.request(flow)

    # The Authorization header should have been swapped
    assert flow.request.headers["Authorization"] == available_auth


def test_request_hook_no_available_credential_passes_through(
    tmp_path: Path, caplog
) -> None:
    db = CredentialDB(tmp_path / "test.db")
    config = _make_config(auto_rotate=True)
    addon = CredentialPoolAddon(config, db)

    # Pre-populate: only one credential, mark it exhausted
    exhausted_auth = "Bearer only_token"
    h = db.upsert_credential("user1", exhausted_auth, "Plan A")
    db.mark_exhausted(h)

    flow = _flow_for(auth=exhausted_auth)

    import logging
    with caplog.at_level(logging.WARNING):
        addon.request(flow)

    # Header stays unchanged (pass through)
    assert flow.request.headers["Authorization"] == exhausted_auth
    assert "no available credential" in caplog.text


def test_non_matching_host_ignored(tmp_path: Path) -> None:
    db = CredentialDB(tmp_path / "test.db")
    config = _make_config(intercept_host="q.us-east-1.amazonaws.com")
    addon = CredentialPoolAddon(config, db)

    flow = _flow_for(host="other.example.com")
    flow.response.set_text(_make_usage_response())

    addon.request(flow)
    addon.response(flow)

    # Nothing should be stored
    with db._lock:
        count = db._conn.execute("SELECT COUNT(*) FROM credentials").fetchone()[0]
    assert count == 0


def test_non_matching_path_ignored_for_response(tmp_path: Path) -> None:
    db = CredentialDB(tmp_path / "test.db")
    config = _make_config(usage_path="/getUsageLimits")
    addon = CredentialPoolAddon(config, db)

    flow = _flow_for(path="/otherEndpoint")
    flow.response.set_text(_make_usage_response())

    addon.request(flow)
    addon.response(flow)

    # Request log should exist (host matches) but no usage snapshot
    with db._lock:
        req_count = db._conn.execute("SELECT COUNT(*) FROM request_log").fetchone()[0]
        snap_count = db._conn.execute(
            "SELECT COUNT(*) FROM usage_snapshots"
        ).fetchone()[0]
    assert req_count == 1
    assert snap_count == 0


def test_auto_rotate_false_disables_rotation(tmp_path: Path) -> None:
    """When auto_rotate is False, exhausted credentials should not be swapped."""
    db = CredentialDB(tmp_path / "test.db")
    config = _make_config(auto_rotate=False)
    addon = CredentialPoolAddon(config, db)

    # Pre-populate: one exhausted credential, one available
    exhausted_auth = "Bearer exhausted_token"
    available_auth = "Bearer available_token"
    h_exhausted = db.upsert_credential("user1", exhausted_auth, "Plan A")
    db.upsert_credential("user2", available_auth, "Plan B")
    db.mark_exhausted(h_exhausted)

    flow = _flow_for(auth=exhausted_auth)
    addon.request(flow)

    # The Authorization header should NOT have been swapped
    assert flow.request.headers["Authorization"] == exhausted_auth


def test_extract_headers_config_honored(tmp_path: Path) -> None:
    """When extract_headers is configured with a custom header, use it instead of Authorization."""
    db = CredentialDB(tmp_path / "test.db")
    config = _make_config(extract_headers=["X-Api-Key"])
    addon = CredentialPoolAddon(config, db)

    flow = tflow.tflow(resp=True)
    flow.request.host = "q.us-east-1.amazonaws.com"
    flow.request.scheme = "https"
    flow.request.path = "/getUsageLimits"
    flow.request.port = 443
    # Set custom header instead of Authorization
    flow.request.headers["X-Api-Key"] = "my-secret-key"
    flow.response.set_text(_make_usage_response(current_usage=50, usage_limit=100))

    addon.request(flow)
    addon.response(flow)

    # Credential should be stored using the X-Api-Key value
    expected_hash = hashlib.sha256(b"my-secret-key").hexdigest()
    with db._lock:
        cred = db._conn.execute(
            "SELECT auth_hash FROM credentials WHERE auth_hash = ?",
            (expected_hash,),
        ).fetchone()
    assert cred is not None


def test_malformed_json_response_does_not_crash(tmp_path: Path, caplog) -> None:
    """A malformed JSON response should be logged and not crash the addon."""
    db = CredentialDB(tmp_path / "test.db")
    config = _make_config()
    addon = CredentialPoolAddon(config, db)

    flow = _flow_for()
    flow.response.set_text("this is not valid json {{{")

    import logging
    with caplog.at_level(logging.WARNING):
        addon.request(flow)
        addon.response(flow)

    assert "failed to parse usage response JSON" in caplog.text

    # No snapshots or credentials should have been stored
    with db._lock:
        snap_count = db._conn.execute(
            "SELECT COUNT(*) FROM usage_snapshots"
        ).fetchone()[0]
    assert snap_count == 0
