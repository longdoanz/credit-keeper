"""mitmproxy addon for credential pool management and usage tracking."""

from __future__ import annotations

import hashlib
import json
import logging

from .config import CredentialPoolConfig
from .db import CredentialDB

logger = logging.getLogger(__name__)


class CredentialPoolAddon:
    """Intercepts requests/responses to track credentials and rotate exhausted ones."""

    def __init__(self, config: CredentialPoolConfig, db: CredentialDB) -> None:
        self.config = config
        self.db = db

    def done(self) -> None:  # type: ignore[no-untyped-def]
        """Called by mitmproxy on shutdown. Clean up DB resources."""
        self.db.close()

    def _compute_auth_hash(self, authorization_header: str) -> str:
        return hashlib.sha256(authorization_header.encode("utf-8")).hexdigest()

    def request(self, flow) -> None:  # type: ignore[no-untyped-def]
        host = flow.request.pretty_host
        if host not in self.config.intercept_hosts:
            return

        # Iterate over configured extract_headers to find the credential
        auth_header = ""
        for header_name in self.config.extract_headers:
            auth_header = flow.request.headers.get(header_name, "")
            if auth_header:
                break

        if not auth_header:
            return

        auth_hash = self._compute_auth_hash(auth_header)

        # Store auth hash in flow metadata for use in response hook
        if not hasattr(flow, "metadata") or flow.metadata is None:
            flow.metadata = {}
        flow.metadata["ck_auth_hash"] = auth_hash
        flow.metadata["ck_auth_header"] = auth_header
        flow.metadata["ck_header_name"] = header_name

        # Extract refresh token header if configured
        if self.config.refresh_token_header:
            refresh_token = flow.request.headers.get(self.config.refresh_token_header, "")
            if refresh_token:
                flow.metadata["ck_refresh_token"] = refresh_token

        # Log the request
        self.db.insert_request_log(
            method=flow.request.method,
            url=flow.request.pretty_url,
            host=host,
            client_id=None,
            auth_hash=auth_hash,
        )

        # Auto-rotate if credential is exhausted
        if self.config.auto_rotate:
            if self.db.is_exhausted(auth_hash):
                # Current credential is exhausted, try to rotate
                available = self.db.get_best_available_credential(exclude_auth_hash=auth_hash)
                if available:
                    new_header, new_hash, new_client_id = available
                    flow.request.headers[header_name] = new_header
                    flow.metadata["ck_auth_hash"] = new_hash
                    flow.metadata["ck_auth_header"] = new_header
                    logger.info(
                        "credit-keeper: rotated exhausted credential %s -> %s",
                        auth_hash[:8],
                        new_hash[:8],
                    )
                else:
                    logger.warning(
                        "credit-keeper: credential %s is exhausted but no available "
                        "credential in pool to rotate to",
                        auth_hash[:8],
                    )

    def response(self, flow) -> None:  # type: ignore[no-untyped-def]
        host = flow.request.pretty_host
        if host not in self.config.intercept_hosts:
            return

        if not flow.request.path.startswith(self.config.usage_path):
            return

        if flow.response is None:
            return

        # Get the authorization header from flow metadata or from the request
        auth_header = ""
        auth_hash = ""
        if hasattr(flow, "metadata") and flow.metadata:
            auth_header = flow.metadata.get("ck_auth_header", "")
            auth_hash = flow.metadata.get("ck_auth_hash", "")

        if not auth_header:
            for header_name in self.config.extract_headers:
                auth_header = flow.request.headers.get(header_name, "")
                if auth_header:
                    break
            if auth_header:
                auth_hash = self._compute_auth_hash(auth_header)

        if not auth_header:
            return

        # Parse JSON response
        try:
            body = json.loads(flow.response.get_text())
        except (json.JSONDecodeError, ValueError):
            logger.warning("credit-keeper: failed to parse usage response JSON")
            return

        # Extract usage data
        try:
            usage_list = body.get("usageBreakdownList", [])
            if not usage_list:
                return
            usage = usage_list[0]
            current_usage = usage.get("currentUsage", 0)
            usage_limit = usage.get("usageLimit", 0)
            display_name = usage.get("displayName", "")
            resource_type = usage.get("resourceType", "")
            unit = usage.get("unit", "")

            user_info = body.get("userInfo", {})
            client_id = user_info.get("userId", "")

            subscription_info = body.get("subscriptionInfo", {})
            subscription_title = subscription_info.get("subscriptionTitle", "")

            days_until_reset = body.get("daysUntilReset", 0)
            next_date_reset = body.get("nextDateReset")
        except (AttributeError, TypeError, IndexError):
            logger.warning("credit-keeper: unexpected usage response structure")
            return

        # Upsert credential
        refresh_token = ""
        if hasattr(flow, "metadata") and flow.metadata:
            refresh_token = flow.metadata.get("ck_refresh_token", "")

        self.db.upsert_credential(
            client_id=client_id,
            authorization_header=auth_header,
            subscription_title=subscription_title,
            refresh_token=refresh_token or None,
        )

        # Update refresh token if present
        if refresh_token:
            self.db.update_refresh_token(auth_hash, refresh_token)

        # Insert usage snapshot
        self.db.insert_usage_snapshot(
            client_id=client_id,
            current_usage=current_usage,
            usage_limit=usage_limit,
            resource_type=resource_type,
            display_name=display_name,
            unit=unit,
            days_until_reset=days_until_reset,
            next_date_reset=next_date_reset,
            raw_json=json.dumps(body),
        )

        # Check if exhausted
        if current_usage >= usage_limit:
            self.db.mark_exhausted(auth_hash)
            logger.warning(
                "credit-keeper: credential %s exhausted (%d/%d %s)",
                auth_hash[:8],
                current_usage,
                usage_limit,
                display_name,
            )
        else:
            self.db.mark_available(auth_hash)
            logger.info(
                "credit-keeper: credential %s usage %d/%d %s",
                auth_hash[:8],
                current_usage,
                usage_limit,
                display_name,
            )
