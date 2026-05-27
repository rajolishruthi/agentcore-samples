"""Unit tests for the host agent OBO token exchanger."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from obo_token import OBOTokenExchanger


def _exchanger():
    return OBOTokenExchanger(
        client_id="cid",
        client_secret="csecret",
        token_endpoint="https://cognito.example.com/oauth2/token",
        scopes=["resource/read"],
    )


def _mock_response(token: str = "agent-token", expires_in: int = 3600):
    resp = AsyncMock()
    resp.status_code = 200
    resp.json = lambda: {"access_token": token, "expires_in": expires_in}
    resp.raise_for_status = lambda: None
    return resp


def test_post_includes_aws_client_metadata_with_user_jwt():
    exchanger = _exchanger()

    with patch("obo_token.httpx.AsyncClient") as MockClient:
        instance = MockClient.return_value.__aenter__.return_value
        instance.post = AsyncMock(return_value=_mock_response())

        token = asyncio.run(exchanger.fetch("USER-JWT-1"))

    assert token == "agent-token"
    instance.post.assert_called_once()
    call = instance.post.call_args
    assert call.args[0] == "https://cognito.example.com/oauth2/token"
    body = call.kwargs["data"]
    assert body["grant_type"] == "client_credentials"
    assert body["scope"] == "resource/read"
    metadata = json.loads(body["aws_client_metadata"])
    assert metadata == {"onBehalfOfToken": "USER-JWT-1", "callerApp": "host-agent"}
    assert call.kwargs["headers"]["Authorization"].startswith("Basic ")


def test_caches_tokens_per_user_jwt():
    exchanger = _exchanger()

    async def _run():
        with patch("obo_token.httpx.AsyncClient") as MockClient:
            instance = MockClient.return_value.__aenter__.return_value
            instance.post = AsyncMock(return_value=_mock_response())

            t1 = await exchanger.fetch("USER-A")
            t2 = await exchanger.fetch("USER-A")
            t3 = await exchanger.fetch("USER-B")
            return t1, t2, t3, instance.post.await_count

    t1, t2, t3, calls = asyncio.run(_run())
    assert t1 == t2 == t3 == "agent-token"
    assert calls == 2


def test_single_flight_for_concurrent_calls_with_same_jwt():
    exchanger = _exchanger()

    async def _run():
        with patch("obo_token.httpx.AsyncClient") as MockClient:
            instance = MockClient.return_value.__aenter__.return_value

            async def slow_post(*args, **kwargs):
                await asyncio.sleep(0.05)
                return _mock_response()

            instance.post = AsyncMock(side_effect=slow_post)

            results = await asyncio.gather(
                exchanger.fetch("USER-X"),
                exchanger.fetch("USER-X"),
                exchanger.fetch("USER-X"),
            )
            return results, instance.post.await_count

    results, calls = asyncio.run(_run())
    assert results == ["agent-token"] * 3
    assert calls == 1
