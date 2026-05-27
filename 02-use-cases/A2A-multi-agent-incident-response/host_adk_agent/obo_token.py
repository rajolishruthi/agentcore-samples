"""Cognito-native OBO token exchange for the host agent.

Trades the inbound user JWT for an agent M2M access token that carries
an `onBehalfOf` claim, without invoking AgentCore Identity's
`ON_BEHALF_OF_TOKEN_EXCHANGE` API (Cognito does not implement RFC 8693).

The host agent posts to Cognito `/oauth2/token` with `client_credentials`
plus `aws_client_metadata={"onBehalfOfToken": <user JWT>, "callerApp":
"host-agent"}`. A Pre-Token-Generation v3 Lambda copies user identity
from the metadata into a custom `onBehalfOf` claim on the agent token.
"""

import asyncio
import base64
import json
import logging
import os
import time
from dataclasses import dataclass

import boto3
import httpx

logger = logging.getLogger(__name__)

# Refresh slightly before the token actually expires to avoid races.
_EXPIRY_SKEW_SECONDS = 30
# Cap memory usage if many distinct user JWTs are seen in the same process.
_MAX_CACHE_ENTRIES = 256

_secrets_client = None


def _get_secrets_client():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client("secretsmanager")
    return _secrets_client


@dataclass
class _CachedToken:
    access_token: str
    expires_at: float


class OBOTokenExchanger:
    """Fetches OBO tokens with per-user-JWT caching and single-flight."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_endpoint: str,
        scopes: list[str] | None = None,
        caller_app: str = "host-agent",
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_endpoint = token_endpoint
        self._scopes = scopes or []
        self._caller_app = caller_app
        self._cache: dict[str, _CachedToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._cache_lock = asyncio.Lock()

    async def fetch(self, user_jwt: str) -> str:
        cached = self._cache.get(user_jwt)
        now = time.time()
        if cached and cached.expires_at - _EXPIRY_SKEW_SECONDS > now:
            return cached.access_token

        async with self._cache_lock:
            lock = self._locks.setdefault(user_jwt, asyncio.Lock())

        async with lock:
            cached = self._cache.get(user_jwt)
            now = time.time()
            if cached and cached.expires_at - _EXPIRY_SKEW_SECONDS > now:
                return cached.access_token

            access_token, expires_in = await self._exchange(user_jwt)
            self._cache[user_jwt] = _CachedToken(
                access_token=access_token,
                expires_at=now + expires_in,
            )
            self._evict_if_needed()
            return access_token

    async def _exchange(self, user_jwt: str) -> tuple[str, int]:
        basic = base64.b64encode(
            f"{self._client_id}:{self._client_secret}".encode()
        ).decode()
        headers = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        body = {
            "grant_type": "client_credentials",
            "aws_client_metadata": json.dumps(
                {"onBehalfOfToken": user_jwt, "callerApp": self._caller_app}
            ),
        }
        if self._scopes:
            body["scope"] = " ".join(self._scopes)

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                self._token_endpoint, headers=headers, data=body
            )
        if resp.status_code != 200:
            logger.error(
                "OBO token exchange failed: %s %s", resp.status_code, resp.text
            )
            resp.raise_for_status()

        data = resp.json()
        return data["access_token"], int(data.get("expires_in", 3600))

    def _evict_if_needed(self):
        if len(self._cache) <= _MAX_CACHE_ENTRIES:
            return
        oldest = sorted(self._cache.items(), key=lambda kv: kv[1].expires_at)
        for key, _ in oldest[: len(self._cache) - _MAX_CACHE_ENTRIES]:
            self._cache.pop(key, None)
            self._locks.pop(key, None)


_default_exchanger: OBOTokenExchanger | None = None


def _load_credentials_from_ssm() -> tuple[str, str, str]:
    """Load OBO client credentials from SSM/Secrets Manager.

    Returns (client_id, client_secret, token_endpoint).
    """
    ssm = boto3.client("ssm")
    client_id = ssm.get_parameter(Name="/hostagent/cognito/obo/client-id")[
        "Parameter"
    ]["Value"]
    secret_arn = ssm.get_parameter(
        Name="/hostagent/cognito/obo/client-secret-arn"
    )["Parameter"]["Value"]
    domain = ssm.get_parameter(Name="/hostagent/cognito/obo/domain")["Parameter"][
        "Value"
    ]

    secret_str = _get_secrets_client().get_secret_value(SecretId=secret_arn)[
        "SecretString"
    ]
    secret = json.loads(secret_str)
    client_secret = secret["client_secret"]
    token_endpoint = secret.get("token_endpoint") or f"https://{domain}/oauth2/token"
    return client_id, client_secret, token_endpoint


def get_default_exchanger() -> OBOTokenExchanger:
    """Process-wide singleton, lazily configured from SSM."""
    global _default_exchanger
    if _default_exchanger is None:
        client_id, client_secret, token_endpoint = _load_credentials_from_ssm()
        scopes_env = os.getenv("OBO_SCOPES", "")
        scopes = [s for s in scopes_env.split() if s]
        _default_exchanger = OBOTokenExchanger(
            client_id=client_id,
            client_secret=client_secret,
            token_endpoint=token_endpoint,
            scopes=scopes,
        )
    return _default_exchanger


async def fetch_obo_token(user_jwt: str) -> str:
    """Convenience wrapper around the default exchanger."""
    return await get_default_exchanger().fetch(user_jwt)
