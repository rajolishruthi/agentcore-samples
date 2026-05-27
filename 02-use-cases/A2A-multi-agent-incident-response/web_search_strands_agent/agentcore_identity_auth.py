"""AgentCore Identity inbound auth middleware.

Validates incoming Bearer tokens via GetWorkloadAccessTokenForJWT.
Uses OIDC-based AWS credentials (no static keys).
"""

import os
import logging
from obo_claims import decode_user_identity
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from aws_credentials import get_boto3_client

logger = logging.getLogger(__name__)

WORKLOAD_NAME = os.getenv("AGENTCORE_WORKLOAD_NAME", "web-search-agent")
SKIP_PATHS = {"/ping", "/health", "/.well-known/agent-card.json"}

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = get_boto3_client("bedrock-agentcore")
    return _client


class AgentCoreIdentityMiddleware(BaseHTTPMiddleware):
    """Validates incoming JWTs via AgentCore Identity GetWorkloadAccessTokenForJWT."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in SKIP_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse({"error": "Missing Authorization header"}, status_code=401)

        bearer_token = auth_header[7:]

        try:
            response = _get_client().get_workload_access_token_for_jwt(
                workloadName=WORKLOAD_NAME,
                userToken=bearer_token,
            )
            request.state.workload_access_token = response["workloadAccessToken"]

            # Decode the original incoming bearer to read the onBehalfOf claim
            # (the workload access token is a different token without it).
            on_behalf_of = decode_user_identity(bearer_token)
            if on_behalf_of:
                request.state.user_on_behalf_of = on_behalf_of
                logger.info(
                    "[OBO] acting on behalf of user=%s role=%s",
                    on_behalf_of.get("email"),
                    on_behalf_of.get("role"),
                )
            return await call_next(request)
        except Exception as e:
            logger.error(f"Token validation failed: {e}")
            return JSONResponse({"error": "Invalid or expired token"}, status_code=401)
