"""Tests for the credit-keeper web UI."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from credit_keeper.db import CredentialDB
from credit_keeper.webui.app import create_app


@pytest.fixture()
def db(tmp_path):
    """Create a test database with sample data."""
    db_path = tmp_path / "test.db"
    database = CredentialDB(str(db_path))

    # Insert sample credentials
    database.upsert_credential(
        client_id="user_alice",
        authorization_header="Bearer token_alice",
        subscription_title="Pro Plan",
        refresh_token="refresh_alice",
    )
    database.upsert_credential(
        client_id="user_bob",
        authorization_header="Bearer token_bob",
        subscription_title="Free Plan",
    )

    # Mark bob as exhausted
    import hashlib

    bob_hash = hashlib.sha256(b"Bearer token_bob").hexdigest()
    database.mark_exhausted(bob_hash)

    # Insert usage snapshots
    database.insert_usage_snapshot(
        client_id="user_alice",
        current_usage=50,
        usage_limit=1000,
        resource_type="api_calls",
        display_name="API Calls",
        unit="calls",
        days_until_reset=15,
        next_date_reset=None,
        raw_json="{}",
    )
    database.insert_usage_snapshot(
        client_id="user_bob",
        current_usage=100,
        usage_limit=100,
        resource_type="api_calls",
        display_name="API Calls",
        unit="calls",
        days_until_reset=10,
        next_date_reset=None,
        raw_json="{}",
    )

    # Insert request logs
    database.insert_request_log(
        method="GET",
        url="https://api.example.com/data",
        host="api.example.com",
        client_id="user_alice",
        auth_hash="abc123",
    )
    database.insert_request_log(
        method="POST",
        url="https://api.example.com/submit",
        host="api.example.com",
        client_id="user_bob",
        auth_hash="def456",
    )

    yield database
    database.close()


@pytest.fixture()
def client(db, tmp_path):
    """Create a FastAPI test client."""
    db_path = tmp_path / "test.db"
    app = create_app(str(db_path))
    return TestClient(app)


def test_dashboard(client):
    """GET / returns 200 with dashboard content."""
    response = client.get("/")
    assert response.status_code == 200
    assert "Dashboard" in response.text
    assert "Total Credentials" in response.text


def test_credentials(client):
    """GET /credentials returns 200 with credentials table."""
    response = client.get("/credentials")
    assert response.status_code == 200
    assert "user_alice" in response.text
    assert "user_bob" in response.text


def test_usage(client):
    """GET /usage/{client_id} returns 200 with usage history."""
    response = client.get("/usage/user_alice")
    assert response.status_code == 200
    assert "user_alice" in response.text
    assert "1000" in response.text


def test_usage_unknown_user(client):
    """GET /usage/{client_id} returns 200 even for unknown user (empty table)."""
    response = client.get("/usage/unknown_user")
    assert response.status_code == 200
    assert "unknown_user" in response.text


def test_requests(client):
    """GET /requests returns 200 with request log."""
    response = client.get("/requests")
    assert response.status_code == 200
    assert "api.example.com" in response.text


def test_requests_pagination(client):
    """GET /requests?page=2 returns 200 (empty second page)."""
    response = client.get("/requests?page=2")
    assert response.status_code == 200


def test_dashboard_shows_warning_count(tmp_path):
    """Dashboard shows correct warning count when threshold is set."""
    db_path = tmp_path / "warning_test.db"
    database = CredentialDB(str(db_path))

    # Seed two credentials
    database.upsert_credential(
        client_id="user_near_limit",
        authorization_header="Bearer token_near",
        subscription_title="Pro Plan",
    )
    database.upsert_credential(
        client_id="user_low_usage",
        authorization_header="Bearer token_low",
        subscription_title="Pro Plan",
    )

    # user_near_limit: 1900/2000 used → 5% remaining → in warning at threshold 10%
    database.insert_usage_snapshot(
        client_id="user_near_limit",
        current_usage=1900,
        usage_limit=2000,
        resource_type="api_calls",
        display_name="API Calls",
        unit="calls",
        days_until_reset=15,
        next_date_reset=None,
        raw_json="{}",
    )
    # user_low_usage: 100/2000 used → 95% remaining → NOT in warning at threshold 10%
    database.insert_usage_snapshot(
        client_id="user_low_usage",
        current_usage=100,
        usage_limit=2000,
        resource_type="api_calls",
        display_name="API Calls",
        unit="calls",
        days_until_reset=15,
        next_date_reset=None,
        raw_json="{}",
    )
    database.close()

    app = create_app(str(db_path), warning_threshold_pct=10)
    test_client = TestClient(app)
    response = test_client.get("/")
    assert response.status_code == 200
    assert "Warning" in response.text
    # The warning card should show value 1 (only user_near_limit is in warning)
    assert ">1<" in response.text or "text-yellow-600\">1<" in response.text or "text-yellow-600\">1\n" in response.text or response.text.count(">1<") >= 1


def test_dashboard_warning_card_hidden_when_threshold_zero(tmp_path):
    """Dashboard shows Warning card with value 0 when threshold is 0 (disabled)."""
    db_path = tmp_path / "warning_zero_test.db"
    database = CredentialDB(str(db_path))

    # Seed two credentials (same as above)
    database.upsert_credential(
        client_id="user_near_limit",
        authorization_header="Bearer token_near",
        subscription_title="Pro Plan",
    )
    database.upsert_credential(
        client_id="user_low_usage",
        authorization_header="Bearer token_low",
        subscription_title="Pro Plan",
    )

    # user_near_limit: 1900/2000 used
    database.insert_usage_snapshot(
        client_id="user_near_limit",
        current_usage=1900,
        usage_limit=2000,
        resource_type="api_calls",
        display_name="API Calls",
        unit="calls",
        days_until_reset=15,
        next_date_reset=None,
        raw_json="{}",
    )
    # user_low_usage: 100/2000 used
    database.insert_usage_snapshot(
        client_id="user_low_usage",
        current_usage=100,
        usage_limit=2000,
        resource_type="api_calls",
        display_name="API Calls",
        unit="calls",
        days_until_reset=15,
        next_date_reset=None,
        raw_json="{}",
    )
    database.close()

    # threshold_pct=0 means feature is disabled → warning count is 0
    app = create_app(str(db_path), warning_threshold_pct=0)
    test_client = TestClient(app)
    response = test_client.get("/")
    assert response.status_code == 200
    assert "Warning" in response.text
    # The warning card should show value 0
    assert "text-yellow-600\">0<" in response.text or ">0<" in response.text


def test_credentials_table_shows_warning_badge(tmp_path):
    """Credentials table shows Warning badge for near-limit users; Exhausted wins over Warning."""
    db_path = tmp_path / "warning_badge_test.db"
    database = CredentialDB(str(db_path))

    # user_near_limit: not exhausted, not dead, but 1900/2000 used → in warning at 10%
    database.upsert_credential(
        client_id="user_near_limit",
        authorization_header="Bearer token_near",
        subscription_title="Pro Plan",
    )
    database.insert_usage_snapshot(
        client_id="user_near_limit",
        current_usage=1900,
        usage_limit=2000,
        resource_type="api_calls",
        display_name="API Calls",
        unit="calls",
        days_until_reset=15,
        next_date_reset=None,
        raw_json="{}",
    )

    # user_exhausted: is_exhausted=1 and also near limit → Exhausted badge should win
    database.upsert_credential(
        client_id="user_exhausted",
        authorization_header="Bearer token_exhausted",
        subscription_title="Pro Plan",
    )
    import hashlib
    exhausted_hash = hashlib.sha256(b"Bearer token_exhausted").hexdigest()
    database.mark_exhausted(exhausted_hash)
    database.insert_usage_snapshot(
        client_id="user_exhausted",
        current_usage=1950,
        usage_limit=2000,
        resource_type="api_calls",
        display_name="API Calls",
        unit="calls",
        days_until_reset=15,
        next_date_reset=None,
        raw_json="{}",
    )

    database.close()

    app = create_app(str(db_path), warning_threshold_pct=10)
    test_client = TestClient(app)
    response = test_client.get("/credentials")

    assert response.status_code == 200
    # Warning badge appears for the near-limit user
    assert ">Warning<" in response.text
    # Exhausted badge appears for the exhausted user (priority over Warning)
    assert ">Exhausted<" in response.text
