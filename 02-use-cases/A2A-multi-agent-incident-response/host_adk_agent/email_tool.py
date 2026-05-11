"""Gmail send-email tool using AgentCore Gateway MCP.

Uses the same auth pattern as the host agent's A2A calls to monitor/websearch agents:
- @requires_access_token with M2M flow to get a Cognito JWT
- Uses that JWT as Bearer token to call the Gateway MCP endpoint

The Gateway handles the Gmail 3LO (consent, token vault, injection) transparently.
"""

import base64
import json
import logging
import os
from email.mime.text import MIMEText

import httpx
from bedrock_agentcore.identity.auth import requires_access_token
from google.adk.tools import FunctionTool

logger = logging.getLogger(__name__)

# Configuration
GMAIL_PROVIDER_NAME = os.getenv("GMAIL_PROVIDER_NAME", "gmail-3lo-provider")
GMAIL_CALLBACK_URL = os.getenv("GMAIL_CALLBACK_URL", "http://localhost:9090/oauth2/callback")
GMAIL_GATEWAY_URL = os.getenv("GMAIL_GATEWAY_URL", "")

# M2M provider for authenticating TO the Gateway
# Reuse the same provider that monitor agent uses for its Gateway
GATEWAY_AUTH_PROVIDER = os.getenv("GATEWAY_PROVIDER_NAME", "")

# MCP protocol version that supports URL-mode elicitation
MCP_PROTOCOL_VERSION = "2025-11-25"

# Gateway MCP tool name (derived from target name + operationId)
GATEWAY_TOOL_NAME = "GmailSend___sendEmail"


def _get_gateway_url() -> str:
    """Get the Gateway MCP endpoint URL from env var or SSM."""
    if GMAIL_GATEWAY_URL:
        return GMAIL_GATEWAY_URL
    try:
        import boto3
        ssm = boto3.client("ssm")
        # Try Gmail gateway first, fall back to monitor agent gateway
        try:
            response = ssm.get_parameter(
                Name="/hostagent/agentcore/gmail-gateway-url", WithDecryption=True
            )
            return response["Parameter"]["Value"]
        except ssm.exceptions.ParameterNotFound:
            # Fall back to monitor agent gateway URL
            response = ssm.get_parameter(
                Name="/monitoragent/agentcore/gateway/gateway_url", WithDecryption=True
            )
            return response["Parameter"]["Value"]
    except Exception as e:
        logger.error(f"Failed to get Gateway URL: {e}")
        raise RuntimeError("Gmail Gateway URL not configured.")


def _compose_raw_email(recipient: str, subject: str, body: str) -> str:
    """Compose an RFC 2822 email and return base64url-encoded string."""
    content_type = "html" if "<" in body else "plain"
    message = MIMEText(body, content_type)
    message["to"] = recipient
    message["subject"] = subject
    return base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")


def _call_gateway_mcp(gateway_url: str, access_token: str, raw_email: str) -> dict:
    """Make a JSON-RPC tools/call request to the Gateway MCP endpoint."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
    }

    # Don't override returnUrl - let gateway use its configured defaultReturnUrl
    _meta = {
        "aws.bedrock-agentcore.gateway/credentialProviderConfiguration": {
            "oauthCredentialProvider": {
                "forceAuthentication": False,
            }
        }
    }

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": GATEWAY_TOOL_NAME,
            "arguments": {"raw": raw_email},
            "_meta": _meta,
        },
    }

    logger.info(f"DEBUG: Calling gateway at {gateway_url}")
    with httpx.Client(timeout=120.0) as client:
        response = client.post(gateway_url, headers=headers, json=payload)
        logger.info(f"DEBUG: Gateway response status: {response.status_code}")
        logger.info(f"DEBUG: Gateway response body: {response.text[:500]}")
        response.raise_for_status()
        return response.json()


def _parse_gateway_response(rpc_response: dict) -> str:
    """Parse the Gateway MCP JSON-RPC response."""
    logger.info(f"DEBUG: Parsing gateway response: {json.dumps(rpc_response)[:500]}")

    # Check for elicitation (authorization required) - this is an "error" with elicitations
    if "error" in rpc_response:
        error = rpc_response["error"]
        error_data = error.get("data", {})
        elicitations = error_data.get("elicitations", [])

        # If there are elicitations, extract the authorization URL
        if elicitations:
            for elicitation in elicitations:
                if elicitation.get("mode") == "url":
                    auth_url = elicitation.get("url", "")
                    logger.info(f"DEBUG: Found authorization URL: {auth_url}")
                    return json.dumps({
                        "auth_required": True,
                        "authorization_url": auth_url,
                        "message": f"Gmail authorization is required. Please visit this URL to grant access:\n\n{auth_url}\n\nAfter authorizing, try sending the email again.",
                    })

        # Otherwise it's a regular error
        logger.error(f"DEBUG: Gateway returned error: {error}")
        return json.dumps({
            "error": True,
            "message": f"Gateway error: {error.get('message', 'Unknown error')}",
        })

    result = rpc_response.get("result", {})
    content_items = result.get("content", [])
    logger.info(f"DEBUG: Found {len(content_items)} content items")

    for item in content_items:
        if item.get("type") == "resource":
            resource = item.get("resource", {})
            text = resource.get("text", "")
            uri = resource.get("uri", "")
            if "elicitation" in uri or "accounts.google.com" in text:
                return json.dumps({
                    "auth_required": True,
                    "authorization_url": text,
                    "message": (
                        "Gmail authorization is required. "
                        "Please visit this URL to grant access, then try again:\n\n"
                        f"{text}"
                    ),
                })

        if item.get("type") == "text":
            text = item.get("text", "")
            try:
                gmail_result = json.loads(text)
                message_id = gmail_result.get("id", "unknown")
                return json.dumps({
                    "success": True,
                    "message_id": message_id,
                    "message": f"Email sent successfully. Message ID: {message_id}",
                })
            except json.JSONDecodeError:
                return json.dumps({"success": True, "message": text})

    return json.dumps({
        "error": True,
        "message": f"Unexpected Gateway response: {json.dumps(result)}",
    })


def send_email_to_user(recipient: str, subject: str, body: str) -> str:
    """Send an email with incident findings or recommendations via Gmail.

    Uses AgentCore Gateway with 3-Legged OAuth. First call returns a consent URL.
    After authorization, subsequent calls send emails directly.

    Args:
        recipient: Email address to send to
        subject: Email subject line
        body: Email body with findings/recommendations

    Returns:
        JSON string with success, auth_required, or error information.
    """

    @requires_access_token(
        provider_name=GATEWAY_AUTH_PROVIDER,
        scopes=[],
        auth_flow="M2M",
        into="gateway_token",
        force_authentication=False,
    )
    def _send_via_gateway(gateway_token: str = "") -> str:
        try:
            if not GATEWAY_AUTH_PROVIDER:
                return json.dumps({
                    "error": True,
                    "message": "GATEWAY_PROVIDER_NAME environment variable not set. Cannot authenticate to Gateway.",
                })

            if not gateway_token:
                return json.dumps({
                    "error": True,
                    "message": "Unable to obtain Gateway access token.",
                })

            try:
                gateway_url = _get_gateway_url()
            except RuntimeError as e:
                return json.dumps({"error": True, "message": str(e)})

            raw_email = _compose_raw_email(recipient, subject, body)

            try:
                rpc_response = _call_gateway_mcp(gateway_url, gateway_token, raw_email)
            except httpx.HTTPStatusError as e:
                error_detail = e.response.text if e.response else str(e)
                return json.dumps({
                    "error": True,
                    "message": f"Email service error ({e.response.status_code}): {error_detail}",
                })
            except (httpx.ConnectError, httpx.TimeoutException):
                return json.dumps({
                    "error": True,
                    "message": "Email service temporarily unavailable.",
                })
            except Exception as e:
                return json.dumps({
                    "error": True,
                    "message": f"Failed to send email: {str(e)}",
                })

            return _parse_gateway_response(rpc_response)

        except Exception as e:
            logger.error(f"Unexpected error in email tool: {e}", exc_info=True)
            return json.dumps({
                "error": True,
                "message": f"Unexpected error: {str(e)}",
            })

    return _send_via_gateway()


# Create the ADK FunctionTool for use in the agent
send_email_tool = FunctionTool(func=send_email_to_user)
