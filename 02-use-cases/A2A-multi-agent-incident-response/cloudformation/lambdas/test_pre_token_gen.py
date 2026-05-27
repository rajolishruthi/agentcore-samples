"""Unit tests for the Cognito Pre-Token-Gen v3 OBO Lambda."""

import base64
import json

import pre_token_gen


def _make_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').decode().rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.signature"


def _v3_event(client_metadata: dict | None) -> dict:
    return {
        "version": "3",
        "triggerSource": "TokenGeneration_ClientCredentials",
        "request": {
            "clientMetadata": client_metadata,
            "scopes": ["api/read"],
        },
        "response": {},
    }


def test_copies_onbehalfof_claim_from_user_jwt():
    user_jwt = _make_jwt(
        {
            "sub": "abc-123",
            "email": "alice@demo.com",
            "cognito:username": "alice",
        }
    )
    event = _v3_event({"onBehalfOfToken": user_jwt, "callerApp": "host-agent"})

    result = pre_token_gen.lambda_handler(event, None)

    overrides = result["response"]["claimsAndScopeOverrideDetails"][
        "accessTokenGeneration"
    ]["claimsToAddOrOverride"]
    obo = json.loads(overrides["onBehalfOf"])
    assert obo == {"email": "alice@demo.com", "sub": "abc-123", "username": "alice"}
    assert overrides["callerApp"] == "host-agent"


def test_falls_back_to_username_when_email_missing():
    user_jwt = _make_jwt({"sub": "x", "username": "bob"})
    event = _v3_event({"onBehalfOfToken": user_jwt})

    result = pre_token_gen.lambda_handler(event, None)
    overrides = result["response"]["claimsAndScopeOverrideDetails"][
        "accessTokenGeneration"
    ]["claimsToAddOrOverride"]
    obo = json.loads(overrides["onBehalfOf"])
    assert obo == {"email": "bob", "sub": "x", "username": "bob"}
    assert overrides["callerApp"] == "unknown"


def test_no_metadata_returns_empty_claims():
    event = _v3_event(None)
    result = pre_token_gen.lambda_handler(event, None)
    assert (
        result["response"]["claimsAndScopeOverrideDetails"]["accessTokenGeneration"][
            "claimsToAddOrOverride"
        ]
        == {}
    )


def test_malformed_token_returns_empty_claims():
    event = _v3_event({"onBehalfOfToken": "not-a-jwt"})
    result = pre_token_gen.lambda_handler(event, None)
    assert (
        result["response"]["claimsAndScopeOverrideDetails"]["accessTokenGeneration"][
            "claimsToAddOrOverride"
        ]
        == {}
    )
