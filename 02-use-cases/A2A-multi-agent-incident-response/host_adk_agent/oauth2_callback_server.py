"""OAuth2 Callback Server for Gmail 3LO flow.

Handles the OAuth2 redirect after the user grants Gmail access.
This server runs alongside the host agent and completes the session binding
with AgentCore Identity.

Usage:
    python oauth2_callback_server.py --region us-west-2

The server listens on port 9090 and handles:
- GET /oauth2/callback?session_id=... — Completes the OAuth2 flow
- GET /ping — Health check
"""

import argparse
import logging
import uvicorn
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse
from bedrock_agentcore.services.identity import IdentityClient

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

OAUTH2_CALLBACK_PORT = 9090
OAUTH2_CALLBACK_ENDPOINT = "/oauth2/callback"
PING_ENDPOINT = "/ping"


class GmailOAuth2CallbackServer:
    """Handles OAuth2 callbacks for Gmail 3LO authorization flow.

    When the user clicks the authorization URL and grants Gmail access,
    Google redirects to this server. The server then calls AgentCore Identity's
    complete_resource_token_auth() to bind the token to the user's session.
    """

    def __init__(self, region: str):
        self.identity_client = IdentityClient(region=region)
        self.app = FastAPI(title="Gmail 3LO Callback Server")
        self._setup_routes()

    def _setup_routes(self):
        @self.app.get(PING_ENDPOINT)
        async def ping():
            return {"status": "healthy"}

        @self.app.get(OAUTH2_CALLBACK_ENDPOINT)
        async def handle_oauth2_callback(session_id: str):
            """Handle the OAuth2 callback from Google after user consent.

            Google redirects here with a session_id parameter. We call
            AgentCore Identity to complete the token binding.
            """
            if not session_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Missing session_id query parameter",
                )

            try:
                # Complete the OAuth2 flow — this stores the token in the Token Vault
                self.identity_client.complete_resource_token_auth(
                    session_uri=session_id,
                )
                logger.info(f"[3LO] OAuth2 flow completed successfully for session: {session_id[:20]}...")

                html = """
                <!DOCTYPE html>
                <html>
                <head>
                    <title>Gmail Access Granted</title>
                    <style>
                        body { margin: 0; padding: 0; height: 100vh; display: flex;
                               justify-content: center; align-items: center;
                               font-family: -apple-system, BlinkMacSystemFont, sans-serif;
                               background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); }
                        .card { text-align: center; padding: 3rem; background: white;
                                border-radius: 16px; box-shadow: 0 20px 60px rgba(0,0,0,0.3);
                                max-width: 500px; }
                        h1 { color: #28a745; margin-bottom: 0.5rem; }
                        p { color: #666; font-size: 1.1rem; }
                        .icon { font-size: 4rem; margin-bottom: 1rem; }
                    </style>
                </head>
                <body>
                    <div class="card">
                        <div class="icon">✅</div>
                        <h1>Gmail Access Granted</h1>
                        <p>The agent can now send emails on your behalf.</p>
                        <p style="color: #999; font-size: 0.9rem;">
                            You can close this tab and return to the agent.
                        </p>
                    </div>
                </body>
                </html>
                """
                return HTMLResponse(content=html, status_code=200)

            except Exception as e:
                logger.error(f"[3LO] Failed to complete OAuth2 flow: {e}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to complete authorization: {str(e)}",
                )

    def get_app(self) -> FastAPI:
        return self.app


def main():
    parser = argparse.ArgumentParser(description="Gmail OAuth2 Callback Server")
    parser.add_argument(
        "-r", "--region", type=str, default="us-west-2",
        help="AWS Region (default: us-west-2)",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=OAUTH2_CALLBACK_PORT,
        help=f"Port to listen on (default: {OAUTH2_CALLBACK_PORT})",
    )
    args = parser.parse_args()

    server = GmailOAuth2CallbackServer(region=args.region)

    logger.info(f"Starting Gmail OAuth2 callback server on 0.0.0.0:{args.port}")
    logger.info(f"Callback URL: http://localhost:{args.port}{OAUTH2_CALLBACK_ENDPOINT}")

    uvicorn.run(server.get_app(), host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
