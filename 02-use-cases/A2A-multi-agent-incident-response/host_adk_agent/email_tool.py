"""Gmail send-email tool using AgentCore Gateway MCP.

Demonstrates the Gateway-mediated 3LO (Authorization Code Grant) pattern:
- Gateway exposes Gmail send as an MCP tool (derived from OpenAPI spec)
- Gateway handles token acquisition, refresh, and injection via AgentCore Identity
- The agent has zero auth code — it just calls the MCP tool

Flow:
- First invocation (no cached token): Gateway returns an elicitation response
  with an authorization URL. The agent surfaces this to the user.
- Subsequent invocations (cached token): Gateway injects the Bearer token
  into the Gmail API call and returns the result.

Prerequisites:
1. A Gateway target configured with the Gmail OpenAPI spec and outbound auth
2. The Gateway URL stored in SSM or GMAIL_GATEWAY_URL env var
3. GMAIL_PROVIDER_NAME env var set (enables the tool in agent.py)
4. GMAIL_CALLBACK_URL env var set to the OAuth2 callback endpoint
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

# M2M provider for authenticating TO the Gateway (Cognito client_credentials)
# Uses the same gateway credential provider as the monitor agent
GATEWAY_AUTH_PROVIDER = os.getenv("GATEWAY_AUTH_PROVIDER", "GatewayOAuth2Provider-monitor-agent-a2a")

# MCP protocol version that supports URL-mode elicitation
MCP_PROTOCOL_VERSION = "2025-11-25"

# Gateway MCP tool name (derived from target name + operationId)
GATEWAY_TOOL_NAME = "GmailSend___sendEmail"


def _get_gateway_url() -> str:
    """Get the Gateway MCP endpoint URL from SSM or env var fallback."""
    if GMAIL_GATEWAY_URL:
        return GMAIL_GATEWAY_URL

    # Try SSM
    try:
        from utils import get_ssm_parameter
        return get_ssm_parameter("/hostagent/agentcore/gmail-gateway-url")
    except Exception:
        pass

    # Docker vs local import
    try:
        from host_adk_agent.utils import get_ssm_parameter
        return get_ssm_parameter("/hostagent/agentcore/gmail-gateway-url")
    except Exception as e:
        logger.error(f"Failed to get Gateway URL from SSM: {e}")
        raise RuntimeError(
            "Gmail Gateway URL not configured. Set GMAIL_GATEWAY_URL env var "
            "or store in SSM at /hostagent/agentcore/gmail-gateway-url"
        )


def _compose_raw_email(recipient: str, subject: str, body: str) -> str:
    """Compose an RFC 2822 email and return base64url-encoded string."""
    content_type = "html" if "<" in body else "plain"
    message = MIMEText(body, content_type)
    message["to"] = recipient
    message["subject"] = subject
    return base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")


def _call_gateway_mcp(gateway_url: str, access_token: str, raw_email: str) -> dict:
    """Make a JSON-RPC tools/call request to the Gateway MCP endpoint.

    Returns the parsed JSON-RPC response body.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
    }

    _meta = {
        "aws.bedrock-agentcore.gateway/credentialProviderConfiguration": {
            "oauthCredentialProvider": {
                "returnUrl": GMAIL_CALLBACK_URL,
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

    with httpx.Client(timeout=120.0) as client:
        response = client.post(gateway_url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()


def _parse_gateway_response(rpc_response: dict) -> str:
    """Parse the Gateway MCP JSON-RPC response into a user-facing JSON string.

    Handles three cases:
    1. Elicitation response (auth URL) — consent required
    2. Success response (Gmail API result) — email sent
    3. Error response — something went wrong
    """
    # Check for JSON-RPC error
    if "error" in rpc_response:
        error = rpc_response["error"]
        return json.dumps({
            "error": True,
            "message": f"Gateway error: {error.get('message', 'Unknown error')}",
        })

    result = rpc_response.get("result", {})
    content_items = result.get("content", [])

    for item in content_items:
        # Elicitation response: resource with auth URL
        if item.get("type") == "resource":
            resource = item.get("resource", {})
            uri = resource.get("uri", "")
            text = resource.get("text", "")

            if "elicitation" in uri or "accounts.google.com" in text:
                auth_url = text
                return json.dumps({
                    "auth_required": True,
                    "authorization_url": auth_url,
                    "message": (
                        "Gmail authorization is required to send emails on your behalf. "
                        "Please visit the following URL to grant access, then try again:\n\n"
                        f"{auth_url}"
                    ),
                })

        # Success response: text content with Gmail API result
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
                # Non-JSON text response — treat as success message
                return json.dumps({
                    "success": True,
                    "message": text,
                })

    # Fallback: unexpected response shape
    return json.dumps({
        "error": True,
        "message": f"Unexpected Gateway response: {json.dumps(result)}",
    })


def send_email_to_user(recipient: str, subject: str, body: str) -> str:
    """Send an email with incident findings or recommendations via Gmail.

    This tool uses AgentCore Gateway with 3-Legged OAuth to send emails
    on behalf of the authenticated user. The first time it's called, it will
    return an authorization URL that the user must visit to grant Gmail access.
    After authorization, subsequent calls will send emails directly.

    Args:
        recipient: Email address to send the findings to (e.g., user's email)
        subject: Email subject line (e.g., "Incident Report: High CPU on EC2")
        body: Email body containing the findings, recommendations, or best practices

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
        """Inner function that sends the email via Gateway MCP."""

        if not gateway_token:
            return json.dumps({
                "error": True,
                "message": "Unable to obtain Gateway access token. Please try again.",
            })

        try:
            gateway_url = _get_gateway_url()
        except RuntimeError as e:
            return json.dumps({
                "error": True,
                "message": str(e),
            })

        # Compose and encode the email
        raw_email = _compose_raw_email(recipient, subject, body)

        # Call Gateway MCP
        try:
            rpc_response = _call_gateway_mcp(gateway_url, gateway_token, raw_email)
        except httpx.HTTPStatusError as e:
            error_detail = e.response.text if e.response else str(e)
            logger.error(f"Gateway HTTP error: {e.response.status_code} - {error_detail}")
            return json.dumps({
                "error": True,
                "message": f"Email service error ({e.response.status_code}): {error_detail}",
            })
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            logger.error(f"Gateway connection error: {e}")
            return json.dumps({
                "error": True,
                "message": "Email service temporarily unavailable. Please try again later.",
            })
        except Exception as e:
            logger.error(f"Unexpected error calling Gateway: {e}")
            return json.dumps({
                "error": True,
                "message": f"Failed to send email: {str(e)}",
            })

        # Parse the response
        return _parse_gateway_response(rpc_response)

    return _send_via_gateway()


# Create the ADK FunctionTool for use in the agent
send_email_tool = FunctionTool(func=send_email_to_user)
