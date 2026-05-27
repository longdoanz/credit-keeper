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
    intercept_hosts: list[str] | None = None,
    usage_path: str = "/getUsageLimits",
    auto_rotate: bool = True,
    extract_headers: list[str] | None = None,
    refresh_token_header: str = "",
) -> CredentialPoolConfig:
    return CredentialPoolConfig(
        enabled=True,
        intercept_hosts=intercept_hosts or ["q.us-east-1.amazonaws.com"],
        usage_path=usage_path,
        auto_rotate=auto_rotate,
        extract_headers=extract_headers or ["Authorization"],
        refresh_token_header=refresh_token_header,
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
    config = _make_config(intercept_hosts=["q.us-east-1.amazonaws.com"])
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


def test_multi_host_matching(tmp_path: Path) -> None:
    """Requests to any host in the intercept_hosts list should be intercepted."""
    db = CredentialDB(tmp_path / "test.db")
    config = _make_config(
        intercept_hosts=["host-a.example.com", "host-b.example.com"]
    )
    addon = CredentialPoolAddon(config, db)

    # Request to first host
    flow_a = _flow_for(host="host-a.example.com", auth="Bearer tokenA")
    flow_a.response.set_text(_make_usage_response(current_usage=10, usage_limit=100, user_id="userA"))
    addon.request(flow_a)
    addon.response(flow_a)

    # Request to second host
    flow_b = _flow_for(host="host-b.example.com", auth="Bearer tokenB")
    flow_b.response.set_text(_make_usage_response(current_usage=20, usage_limit=100, user_id="userB"))
    addon.request(flow_b)
    addon.response(flow_b)

    # Both credentials should be stored
    with db._lock:
        count = db._conn.execute("SELECT COUNT(*) FROM credentials").fetchone()[0]
    assert count == 2

    # Request to a non-matching host should be ignored
    flow_c = _flow_for(host="other.example.com", auth="Bearer tokenC")
    flow_c.response.set_text(_make_usage_response())
    addon.request(flow_c)
    addon.response(flow_c)

    with db._lock:
        count = db._conn.execute("SELECT COUNT(*) FROM credentials").fetchone()[0]
    assert count == 2  # still 2


def test_smart_rotation_picks_best_credential(tmp_path: Path) -> None:
    """Auto-rotation should pick the credential with the most remaining credits."""
    db = CredentialDB(tmp_path / "test.db")
    config = _make_config(auto_rotate=True)
    addon = CredentialPoolAddon(config, db)

    # Pre-populate credentials
    exhausted_auth = "Bearer exhausted_token"
    low_auth = "Bearer low_token"
    high_auth = "Bearer high_token"

    h_exhausted = db.upsert_credential("user_exhausted", exhausted_auth, "Plan E")
    db.upsert_credential("user_low", low_auth, "Plan L")
    db.upsert_credential("user_high", high_auth, "Plan H")
    db.mark_exhausted(h_exhausted)

    # Add usage snapshots: low has 90/100 used (10 remaining), high has 20/100 used (80 remaining)
    db.insert_usage_snapshot(
        client_id="user_low", current_usage=90, usage_limit=100,
        resource_type="api_calls", display_name="API Calls", unit="calls",
        days_until_reset=15, next_date_reset=1700000000.0, raw_json="{}",
    )
    db.insert_usage_snapshot(
        client_id="user_high", current_usage=20, usage_limit=100,
        resource_type="api_calls", display_name="API Calls", unit="calls",
        days_until_reset=15, next_date_reset=1700000000.0, raw_json="{}",
    )

    flow = _flow_for(auth=exhausted_auth)
    addon.request(flow)

    # Should have rotated to the high-remaining credential
    assert flow.request.headers["Authorization"] == high_auth


def test_usage_path_prefix_not_matched(tmp_path: Path) -> None:
    """A path that starts with the usage_path but is not exact should not trigger usage parsing."""
    db = CredentialDB(tmp_path / "test.db")
    config = _make_config(usage_path="/getUsageLimits")
    addon = CredentialPoolAddon(config, db)

    flow = _flow_for(path="/getUsageLimitsExtra")
    flow.response.set_text(_make_usage_response())

    addon.request(flow)
    addon.response(flow)

    # Request log should exist but no usage snapshot (path is not exact match)
    with db._lock:
        req_count = db._conn.execute("SELECT COUNT(*) FROM request_log").fetchone()[0]
        snap_count = db._conn.execute(
            "SELECT COUNT(*) FROM usage_snapshots"
        ).fetchone()[0]
    assert req_count == 1
    assert snap_count == 0


def test_usage_path_with_query_string_matched(tmp_path: Path) -> None:
    """A path that matches usage_path but has a query string should still trigger usage parsing."""
    db = CredentialDB(tmp_path / "test.db")
    config = _make_config(usage_path="/getUsageLimits")
    addon = CredentialPoolAddon(config, db)

    flow = _flow_for(path="/getUsageLimits?foo=bar")
    flow.response.set_text(_make_usage_response())

    addon.request(flow)
    addon.response(flow)

    # Usage snapshot should be stored since path portion matches exactly
    with db._lock:
        snap_count = db._conn.execute(
            "SELECT COUNT(*) FROM usage_snapshots"
        ).fetchone()[0]
    assert snap_count == 1


def test_refresh_token_extraction(tmp_path: Path) -> None:
    """When refresh_token_header is configured, the token should be extracted and stored."""
    db = CredentialDB(tmp_path / "test.db")
    config = _make_config(refresh_token_header="X-Refresh-Token")
    addon = CredentialPoolAddon(config, db)

    flow = _flow_for()
    flow.request.headers["X-Refresh-Token"] = "my-refresh-token-123"
    flow.response.set_text(_make_usage_response(current_usage=50, usage_limit=100))

    addon.request(flow)
    addon.response(flow)

    # The refresh token should be stored in flow metadata
    assert flow.metadata.get("ck_refresh_token") == "my-refresh-token-123"

    # The refresh token should be stored in the database
    with db._lock:
        row = db._conn.execute(
            "SELECT refresh_token FROM credentials"
        ).fetchone()
    assert row is not None
    assert row[0] == "my-refresh-token-123"


# ---------------------------------------------------------------------------
# Helpers for warning-blend tests
# ---------------------------------------------------------------------------

def _make_blend_config(
    warning_blend_ratio: float = 0.2,
    warning_threshold_pct: float = 10.0,
    auto_rotate: bool = True,
) -> CredentialPoolConfig:
    return CredentialPoolConfig(
        enabled=True,
        intercept_hosts=["q.us-east-1.amazonaws.com"],
        usage_path="/getUsageLimits",
        auto_rotate=auto_rotate,
        extract_headers=["Authorization"],
        warning_blend_ratio=warning_blend_ratio,
        warning_threshold_pct=warning_threshold_pct,
    )


def _seed_warning_credential(
    db: CredentialDB,
    client_id: str = "c1",
    auth: str = "Bearer owner_token",
    current_usage: int = 1900,
    usage_limit: int = 2000,
) -> str:
    """Insert a credential + usage snapshot indicating warning state. Returns auth_hash."""
    auth_hash = db.upsert_credential(client_id, auth, "Plan W")
    db.insert_usage_snapshot(
        client_id=client_id,
        current_usage=current_usage,
        usage_limit=usage_limit,
        resource_type="api_calls",
        display_name="API Calls",
        unit="calls",
        days_until_reset=15,
        next_date_reset=1700000000.0,
        raw_json="{}",
    )
    return auth_hash


# ---------------------------------------------------------------------------
# Warning-blend tests
# ---------------------------------------------------------------------------

def test_warning_blend_rotates_when_not_owner_turn(tmp_path: Path) -> None:
    """Counter=1, every_nth=5 (ratio=0.2); 1%5!=0 → header should rotate."""
    db = CredentialDB(tmp_path / "test.db")
    config = _make_blend_config(warning_blend_ratio=0.2, warning_threshold_pct=10.0)
    addon = CredentialPoolAddon(config, db)

    # Seed owner credential in warning (1900/2000 → 5% remaining < 10%)
    owner_auth = "Bearer owner_token"
    _seed_warning_credential(db, client_id="c1", auth=owner_auth, current_usage=1900, usage_limit=2000)

    # Seed a second available credential in the pool
    pool_auth = "Bearer pool_token"
    db.upsert_credential("c2", pool_auth, "Plan P")
    db.insert_usage_snapshot(
        client_id="c2", current_usage=100, usage_limit=2000,
        resource_type="api_calls", display_name="API Calls", unit="calls",
        days_until_reset=15, next_date_reset=1700000000.0, raw_json="{}",
    )

    flow = _flow_for(auth=owner_auth)
    addon.request(flow)

    # Counter becomes 1; 1 % 5 != 0 → should have rotated to pool credential
    assert flow.request.headers["Authorization"] == pool_auth


def test_warning_blend_skips_rotation_on_owner_turn(tmp_path: Path) -> None:
    """5th call (counter=5, 5%5==0) should NOT rotate."""
    db = CredentialDB(tmp_path / "test.db")
    config = _make_blend_config(warning_blend_ratio=0.2, warning_threshold_pct=10.0)
    addon = CredentialPoolAddon(config, db)

    owner_auth = "Bearer owner_token"
    _seed_warning_credential(db, client_id="c1", auth=owner_auth, current_usage=1900, usage_limit=2000)

    pool_auth = "Bearer pool_token"
    db.upsert_credential("c2", pool_auth, "Plan P")
    db.insert_usage_snapshot(
        client_id="c2", current_usage=100, usage_limit=2000,
        resource_type="api_calls", display_name="API Calls", unit="calls",
        days_until_reset=15, next_date_reset=1700000000.0, raw_json="{}",
    )

    # Make 4 requests (counter 1-4 → all rotate)
    for _ in range(4):
        flow = _flow_for(auth=owner_auth)
        addon.request(flow)

    # 5th request: counter=5, 5%5==0 → owner turn, no rotation
    flow5 = _flow_for(auth=owner_auth)
    addon.request(flow5)

    assert flow5.request.headers["Authorization"] == owner_auth


def test_warning_blend_inactive_when_above_threshold(tmp_path: Path) -> None:
    """Snapshot at 500/2000 → 75% remaining, above 10% threshold → no warning, no rotation."""
    db = CredentialDB(tmp_path / "test.db")
    config = _make_blend_config(warning_blend_ratio=0.2, warning_threshold_pct=10.0)
    addon = CredentialPoolAddon(config, db)

    owner_auth = "Bearer owner_token"
    _seed_warning_credential(db, client_id="c1", auth=owner_auth, current_usage=500, usage_limit=2000)

    pool_auth = "Bearer pool_token"
    db.upsert_credential("c2", pool_auth, "Plan P")

    flow = _flow_for(auth=owner_auth)
    addon.request(flow)

    # Not in warning → no rotation, no counter increment
    assert flow.request.headers["Authorization"] == owner_auth
    assert "c1" not in addon._owner_counter


def test_warning_blend_inactive_when_feature_disabled(tmp_path: Path) -> None:
    """warning_blend_ratio=0.0 disables the feature entirely."""
    db = CredentialDB(tmp_path / "test.db")
    config = _make_blend_config(warning_blend_ratio=0.0, warning_threshold_pct=10.0)
    addon = CredentialPoolAddon(config, db)

    owner_auth = "Bearer owner_token"
    _seed_warning_credential(db, client_id="c1", auth=owner_auth, current_usage=1900, usage_limit=2000)

    pool_auth = "Bearer pool_token"
    db.upsert_credential("c2", pool_auth, "Plan P")

    flow = _flow_for(auth=owner_auth)
    addon.request(flow)

    # Feature disabled → no rotation, counter empty
    assert flow.request.headers["Authorization"] == owner_auth
    assert addon._owner_counter == {}


def test_warning_blend_unknown_client_id(tmp_path: Path) -> None:
    """auth_hash not in credentials → get_client_id_by_auth_hash returns None → branch skipped."""
    db = CredentialDB(tmp_path / "test.db")
    config = _make_blend_config(warning_blend_ratio=0.2, warning_threshold_pct=10.0)
    addon = CredentialPoolAddon(config, db)

    # No credential inserted for this auth header
    unknown_auth = "Bearer unknown_token"

    flow = _flow_for(auth=unknown_auth)
    addon.request(flow)

    # Counter should be empty and header unchanged
    assert addon._owner_counter == {}
    assert flow.request.headers["Authorization"] == unknown_auth


def test_warning_blend_exhausted_takes_priority(tmp_path: Path) -> None:
    """Exhausted credential gets rotated by exhausted logic; warning branch must not tick counter."""
    db = CredentialDB(tmp_path / "test.db")
    config = _make_blend_config(warning_blend_ratio=0.2, warning_threshold_pct=10.0, auto_rotate=True)
    addon = CredentialPoolAddon(config, db)

    # Owner credential is exhausted AND in warning
    owner_auth = "Bearer owner_token"
    auth_hash = _seed_warning_credential(db, client_id="c1", auth=owner_auth, current_usage=1900, usage_limit=2000)
    db.mark_exhausted(auth_hash)

    pool_auth = "Bearer pool_token"
    db.upsert_credential("c2", pool_auth, "Plan P")
    db.insert_usage_snapshot(
        client_id="c2", current_usage=100, usage_limit=2000,
        resource_type="api_calls", display_name="API Calls", unit="calls",
        days_until_reset=15, next_date_reset=1700000000.0, raw_json="{}",
    )

    flow = _flow_for(auth=owner_auth)
    addon.request(flow)

    # Exhausted rotation fires, header changes
    assert flow.request.headers["Authorization"] == pool_auth
    # Warning branch should NOT have incremented the counter
    assert addon._owner_counter.get("c1", 0) == 0


def test_warning_blend_pool_empty_fallthrough(tmp_path: Path, caplog) -> None:
    """Pool empty when blend says rotate → header stays unchanged, WARNING log emitted."""
    import logging

    db = CredentialDB(tmp_path / "test.db")
    config = _make_blend_config(warning_blend_ratio=0.2, warning_threshold_pct=10.0)
    addon = CredentialPoolAddon(config, db)

    # Only one credential (in warning), no pool available
    owner_auth = "Bearer owner_token"
    _seed_warning_credential(db, client_id="c1", auth=owner_auth, current_usage=1900, usage_limit=2000)

    flow = _flow_for(auth=owner_auth)
    with caplog.at_level(logging.WARNING):
        addon.request(flow)

    # Counter ticked but header unchanged (fell through)
    assert flow.request.headers["Authorization"] == owner_auth
    assert addon._owner_counter.get("c1", 0) == 1
    assert "falling through with owner" in caplog.text


def test_warning_blend_counter_persists_across_requests(tmp_path: Path) -> None:
    """Counter increments on each request while client is in warning."""
    db = CredentialDB(tmp_path / "test.db")
    config = _make_blend_config(warning_blend_ratio=0.2, warning_threshold_pct=10.0)
    addon = CredentialPoolAddon(config, db)

    owner_auth = "Bearer owner_token"
    _seed_warning_credential(db, client_id="c1", auth=owner_auth, current_usage=1900, usage_limit=2000)

    # No second credential so blend falls through each time (counter still ticks)
    for _ in range(3):
        flow = _flow_for(auth=owner_auth)
        addon.request(flow)

    assert addon._owner_counter.get("c1") == 3


def test_warning_blend_counter_shared_across_refreshed_tokens(tmp_path: Path) -> None:
    """Two auth_hashes for same client_id share the same counter slot."""
    db = CredentialDB(tmp_path / "test.db")
    config = _make_blend_config(warning_blend_ratio=0.2, warning_threshold_pct=10.0)
    addon = CredentialPoolAddon(config, db)

    # Insert two credentials sharing client_id "c1" (simulating token refresh)
    auth1 = "Bearer token_v1"
    auth2 = "Bearer token_v2"
    db.upsert_credential("c1", auth1, "Plan W")
    db.upsert_credential("c1", auth2, "Plan W")
    # Insert a single usage snapshot under client_id "c1" indicating warning
    db.insert_usage_snapshot(
        client_id="c1", current_usage=1900, usage_limit=2000,
        resource_type="api_calls", display_name="API Calls", unit="calls",
        days_until_reset=15, next_date_reset=1700000000.0, raw_json="{}",
    )

    # Request with first token
    flow1 = _flow_for(auth=auth1)
    addon.request(flow1)

    # Request with second token
    flow2 = _flow_for(auth=auth2)
    addon.request(flow2)

    # Counter for "c1" should be 2 (both requests share the same slot)
    assert addon._owner_counter.get("c1") == 2
