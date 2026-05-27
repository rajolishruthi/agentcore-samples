"""Decode the inbound agent JWT for OBO claims.

The signature is already validated by GetWorkloadAccessTokenForJWT
upstream (see agentcore_identity_auth.py); this just reads the custom
onBehalfOf claim from the JWT payload.
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


def _decode_jwt_payload(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def decode_user_identity(bearer_token: str) -> Optional[dict]:
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
    return {
        "email": email,
        "sub": obo.get("sub"),
        "username": obo.get("username"),
        "role": ROLE_MAP.get(email, DEFAULT_ROLE),
    }
