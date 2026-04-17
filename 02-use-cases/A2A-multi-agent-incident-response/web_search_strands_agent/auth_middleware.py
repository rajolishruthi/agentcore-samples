"""Cognito JWT validation middleware for GCP Cloud Run.

Since this agent runs outside AgentCore Runtime, we validate
incoming Bearer tokens ourselves against the Cognito User Pool.
This replaces the automatic inbound auth that AgentCore Runtime provides.
"""

import os
import logging
import json
import time
from typing import Optional
from urllib.request import urlopen

import jwt
from jwt import PyJWKClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Cognito configuration from environment
COGNITO_REGION = os.getenv("COGNITO_REGION", "us-west-2")
COGNITO_USER_POOL_ID = os.getenv("COGNITO_USER_POOL_ID")
COGNITO_ISSUER = (
    f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}"
    if COGNITO_USER_POOL_ID
    else None
)
JWKS_URL = f"{COGNITO_ISSUER}/.well-known/jwks.json" if COGNITO_ISSUER else None

# Cache the JWKS client
_jwks_client: Optional[PyJWKClient] = None


def _get_jwks_client() -> PyJWKClient:
    """Get or create a cached JWKS client."""
    global _jwks_client
    if _jwks_client is None and JWKS_URL:
        _jwks_client = PyJWKClient(JWKS_URL, cache_keys=True)
    return _jwks_client


def validate_cognito_token(token: str) -> Optional[dict]:
    """Validate a Cognito JWT token and return claims if valid."""
    if not COGNITO_ISSUER or not JWKS_URL:
        logger.error("Cognito configuration missing")
        return None

    try:
        jwks_client = _get_jwks_client()
        signing_key = jwks_client.get_signing_key_from_jwt(token)

        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=COGNITO_ISSUER,
            options={
                "verify_exp": True,
                "verify_iss": True,
                "verify_aud": False,  # Cognito M2M tokens use client_id, not aud
            },
        )
        logger.info(f"Token validated for client: {claims.get('client_id', 'unknown')}")
        return claims

    except jwt.ExpiredSignatureError:
        logger.warning("Token has expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning(f"Invalid token: {e}")
        return None
    except Exception as e:
        logger.error(f"Token validation error: {e}")
        return None


class CognitoAuthMiddleware(BaseHTTPMiddleware):
    """Starlette middleware that validates Cognito Bearer tokens.

    Skips validation for health/ping endpoints and the agent card endpoint.
    """

    SKIP_PATHS = {"/ping", "/health", "/.well-known/agent-card.json"}

    async def dispatch(self, request: Request, call_next):
        # Skip auth for health checks and agent card discovery
        if request.url.path in self.SKIP_PATHS:
            return await call_next(request)

        # Extract Bearer token
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                {"error": "Missing or invalid Authorization header"},
                status_code=401,
            )

        token = auth_header[7:]  # Strip "Bearer "
        claims = validate_cognito_token(token)

        if claims is None:
            return JSONResponse(
                {"error": "Invalid or expired token"},
                status_code=401,
            )

        # Token is valid — continue to the A2A handler
        return await call_next(request)
