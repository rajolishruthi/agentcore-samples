"""Gmail 3LO send-email tool using @requires_access_token on Runtime.

Pattern from tutorial 12-m2m-3lo-runtime:
- Uses @requires_access_token(auth_flow="USER_FEDERATION") to get Gmail token
- First call: returns consent URL (non-blocking via TokenPoller)
- After consent: token is cached in vault, subsequent calls send directly

Prerequisites:
1. Gmail credential provider registered in AgentCore Identity (GoogleOauth2)
2. GMAIL_PROVIDER_NAME env var set (enables the tool)
3. CALLBACK_URL env var set to the OAuth2 callback endpoint
4. OAuth2 callback server running locally to handle session binding
"""

import base64
import json
import logging
import os
from email.mime.text import MIMEText

import httpx
from bedrock_agentcore.identity.auth import requires_access_token
from bedrock_agentcore.services.identity import TokenPoller
from google.adk.tools import FunctionTool

logger = logging.getLogger(__name__)

# Configuration
GMAIL_PROVIDER_NAME = os.getenv("GMAIL_PROVIDER_NAME", "gmail-3lo-provider")
CALLBACK_URL = os.getenv("CALLBACK_URL", "http://localhost:9090/oauth2/callback")

# Gmail API scope
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

# Cache for auth URL (shown to user when consent needed)
_gmail_auth_url_cache: dict = {}


class _NonBlockingPoller(TokenPoller):
    """Returns immediately so the consent URL can be passed to the user.

    On first call (no token yet): on_auth_url is called with the URL, then
    this poller returns "" immediately instead of blocking.
    On second invocation (after user completes consent): token is returned directly.
    """
    async def poll_for_token(self) -> str:
        return ""


def _on_gmail_auth_url(url: str) -> None:
    """Callback when Gmail consent is required."""
    _gmail_auth_url_cache["url"] = url
    logger.info(f"[3LO] Gmail authorization URL generated")


def send_email_to_user(recipient: str, subject: str, body: str) -> str:
    """Send an email with incident findings or recommendations via Gmail.

    Uses 3-Legged OAuth (Authorization Code Grant) to send emails on behalf
    of the authenticated user. First call returns a consent URL. After the
    user authorizes, subsequent calls send emails directly.

    Args:
        recipient: Email address to send to (e.g., user's Gmail)
        subject: Email subject line
        body: Email body with findings/recommendations

    Returns:
        A message with success confirmation, or an authorization URL if consent is needed.
    """

    @requires_access_token(
        provider_name=GMAIL_PROVIDER_NAME,
        auth_flow="USER_FEDERATION",
        scopes=GMAIL_SCOPES,
        on_auth_url=_on_gmail_auth_url,
        callback_url=CALLBACK_URL,
        token_poller=_NonBlockingPoller(),
    )
    def _send_email(access_token: str = "") -> str:
        # No token yet — consent required
        if not access_token:
            auth_url = _gmail_auth_url_cache.get("url", "")
            if auth_url:
                return (
                    f"Gmail authorization required to send emails on your behalf. "
                    f"Please visit this URL and grant access:\n\n{auth_url}\n\n"
                    "After authorizing, ask me to send the email again."
                )
            return "Gmail authorization required. Please try again in a moment."

        # Token available — compose and send email
        content_type = "html" if "<" in body else "plain"
        message = MIMEText(body, content_type)
        message["to"] = recipient
        message["subject"] = subject
        raw_email = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json",
                    },
                    json={"raw": raw_email},
                )
                response.raise_for_status()
                result = response.json()
                msg_id = result.get("id", "unknown")
                logger.info(f"[3LO] Email sent successfully. Message ID: {msg_id}")
                return f"Email sent successfully to {recipient}. Message ID: {msg_id}"

        except httpx.HTTPStatusError as e:
            error_detail = e.response.text if e.response else str(e)
            logger.error(f"[3LO] Gmail API error: {e.response.status_code} - {error_detail}")
            return f"Failed to send email: {e.response.status_code} - {error_detail}"
        except Exception as e:
            logger.error(f"[3LO] Error sending email: {e}")
            return f"Failed to send email: {str(e)}"

    return _send_email()


# Create the ADK FunctionTool for use in the agent
send_email_tool = FunctionTool(func=send_email_to_user)
