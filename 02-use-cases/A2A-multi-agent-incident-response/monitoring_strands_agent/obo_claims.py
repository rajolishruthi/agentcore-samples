"""Decode the inbound agent JWT for OBO claims.

The token signature is already validated upstream (by AgentCore Runtime
authorizer for AWS, by GetWorkloadAccessTokenForJWT for GCP). This module
just decodes the payload to read the custom `onBehalfOf` claim that the
Cognito Pre-Token-Generation Lambda inserts.
"""

import base64
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

ROLE_MAP = {
    "alice@demo.com": "admin",
    "bob@demo.com": "manager",
    "charlie@demo.com": "analyst",
}
DEFAULT_ROLE = "viewer"

# Tools each role is allowed to invoke (monitoring agent).
ROLE_TOOL_FILTERS = {
    "admin": None,  # no filter
    "manager": ["/aws/lambda/", "/aws/apigateway/"],
    "analyst": ["/aws/lambda/"],
    "viewer": [],
}


def _decode_jwt_payload(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def decode_user_identity(bearer_token: str) -> Optional[dict]:
    """Return {email, sub, username, role} from the agent JWT, or None."""
    if not bearer_token:
        return None
    try:
        claims = _decode_jwt_payload(bearer_token)
    except Exception as e:
        logger.warning(f"Failed to decode bearer token payload: {e}")
        return None

    raw = claims.get("onBehalfOf")
    if not raw:
        return None

    try:
        obo = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as e:
        logger.warning(f"Failed to parse onBehalfOf claim: {e}")
        return None

    email = obo.get("email")
    role = ROLE_MAP.get(email, DEFAULT_ROLE)
    return {
        "email": email,
        "sub": obo.get("sub"),
        "username": obo.get("username"),
        "role": role,
    }


def filter_log_groups_by_role(log_groups: list, role: str) -> list:
    """Apply the role-based prefix filter to a list of log group names."""
    prefixes = ROLE_TOOL_FILTERS.get(role, [])
    if prefixes is None:
        return log_groups
    if not prefixes:
        return []
    return [
        group
        for group in log_groups
        if any(_log_group_name(group).startswith(p) for p in prefixes)
    ]


def _log_group_name(item) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return item.get("logGroupName") or item.get("name") or ""
    return ""
