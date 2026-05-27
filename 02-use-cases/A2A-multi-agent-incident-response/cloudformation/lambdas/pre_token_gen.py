"""Cognito Pre-Token-Generation v3 trigger for the agent-obo-client.

Fires on M2M (client_credentials) grants. Reads
event.request.clientMetadata.onBehalfOfToken (passed by the host agent as
aws_client_metadata) and copies the decoded user identity into an
'onBehalfOf' claim on the access token. Downstream agents read that
claim to attribute the action to a real Cognito user.

Trust model: this Lambda runs only inside Cognito's trusted invocation
flow. The user JWT was already validated by AgentCore Runtime before
the host agent forwarded it here, so we decode the payload without
verifying the signature.
"""

import base64
import json
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _decode_jwt_payload(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def lambda_handler(event, context):
    logger.info(f"Trigger source: {event.get('triggerSource')}")

    metadata = (event.get("request", {}) or {}).get("clientMetadata") or {}
    on_behalf_of_token = metadata.get("onBehalfOfToken")

    claims = {}
    if on_behalf_of_token:
        try:
            user_claims = _decode_jwt_payload(on_behalf_of_token)
            on_behalf_of = {
                "email": (
                    user_claims.get("email")
                    or user_claims.get("username")
                    or user_claims.get("cognito:username")
                ),
                "sub": user_claims.get("sub"),
                "username": (
                    user_claims.get("cognito:username")
                    or user_claims.get("username")
                ),
            }
            claims["onBehalfOf"] = json.dumps(
                {k: v for k, v in on_behalf_of.items() if v}
            )
            claims["callerApp"] = metadata.get("callerApp", "unknown")
            logger.info(f"OBO claim added for sub={on_behalf_of.get('sub')}")
        except Exception as e:
            logger.error(f"Failed to decode onBehalfOfToken: {e}")
    else:
        logger.info("No onBehalfOfToken in clientMetadata; skipping OBO claim")

    event["response"] = {
        "claimsAndScopeOverrideDetails": {
            "accessTokenGeneration": {
                "claimsToAddOrOverride": claims,
            }
        }
    }
    return event
