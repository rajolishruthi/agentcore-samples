"""Unit tests for the monitoring agent OBO claim decoder."""

import base64
import json

from obo_claims import (
    DEFAULT_ROLE,
    decode_user_identity,
    filter_log_groups_by_role,
)


def _make_jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.sig"


def test_decode_returns_role_for_known_email():
    obo = {"email": "alice@demo.com", "sub": "s1", "username": "alice"}
    token = _make_jwt({"onBehalfOf": json.dumps(obo)})

    result = decode_user_identity(token)
    assert result == {
        "email": "alice@demo.com",
        "sub": "s1",
        "username": "alice",
        "role": "admin",
    }


def test_unknown_email_falls_back_to_viewer():
    obo = {"email": "stranger@demo.com", "sub": "s2"}
    token = _make_jwt({"onBehalfOf": json.dumps(obo)})
    assert decode_user_identity(token)["role"] == DEFAULT_ROLE


def test_no_onbehalfof_claim_returns_none():
    token = _make_jwt({"sub": "no-obo"})
    assert decode_user_identity(token) is None


def test_filter_log_groups_admin_passthrough():
    groups = ["/aws/lambda/foo", "/aws/rds/bar", "/custom/baz"]
    assert filter_log_groups_by_role(groups, "admin") == groups


def test_filter_log_groups_analyst_only_lambda():
    groups = [
        {"logGroupName": "/aws/lambda/foo"},
        {"logGroupName": "/aws/rds/bar"},
    ]
    filtered = filter_log_groups_by_role(groups, "analyst")
    assert len(filtered) == 1
    assert filtered[0]["logGroupName"] == "/aws/lambda/foo"


def test_filter_log_groups_viewer_denies_all():
    assert filter_log_groups_by_role(["/aws/lambda/x"], "viewer") == []
