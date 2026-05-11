"""OAuth2 Callback Server for 3LO flow (same pattern as tutorial 12-m2m-3lo-runtime).

Handles:
- POST /userIdentifier/token — Frontend stores the user's Cognito JWT here
- GET /oauth2/callback?session_id=... — Google redirects here after consent
- GET /ping — Health check

Usage:
    python oauth2_callback_server.py --region us-west-2
"""

import argparse
import logging
import uvicorn
import boto3
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

OAUTH2_CALLBACK_PORT = 9090


class UserTokenRequest(BaseModel):
    user_token: str


class OAuth2CallbackServer:
    def __init__(self, region: str):
        self._client = boto3.client("bedrock-agentcore", region_name=region)
        self._user_token: str | None = None
        self.app = FastAPI(title="OAuth2 Callback Server")

        # Allow CORS from the React frontend
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://localhost:3000", "http://127.0.0.1:5173"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

        self._setup_routes()

    def _setup_routes(self):
        @self.app.get("/ping")
        async def ping():
            return {"status": "healthy"}

        @self.app.post("/userIdentifier/token")
        async def store_user_token(request: UserTokenRequest):
            """Store the user's Cognito JWT for session binding.

            The frontend calls this before invoking the agent.
            When Google redirects back, we use this token to identify the user.
            """
            self._user_token = request.user_token
            logger.info(f"[3LO] Stored user token (first 20 chars): {request.user_token[:20]}...")
            return {"status": "stored"}

        @self.app.get("/oauth2/callback")
        async def handle_oauth2_callback(session_id: str):
            """Handle the OAuth2 callback from Google after user consent.

            Flow:
            1. Google redirects here with session_id
            2. We call CompleteResourceTokenAuth with session_id + stored user token
            3. AgentCore Identity exchanges the auth code for Gmail tokens
            4. Tokens are stored in the vault, bound to this user
            """
            if not session_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Missing session_id query parameter",
                )

            if not self._user_token:
                logger.error("[3LO] No user token stored. Frontend must POST to /userIdentifier/token first.")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="No user token stored. Please retry the email request.",
                )

            try:
                # Complete the OAuth2 flow — binds the Google token to this user
                self._client.complete_resource_token_auth(
                    sessionUri=session_id,
                    userIdentifier={"userToken": self._user_token},
                )
                logger.info(f"[3LO] ✅ OAuth2 flow completed successfully!")

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
                            Return to the chat and ask the agent to send the email again.
                        </p>
                    </div>
                </body>
                </html>
                """
                return HTMLResponse(content=html, status_code=200)

            except Exception as e:
                logger.error(f"[3LO] ❌ Failed to complete OAuth2 flow: {e}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to complete authorization: {str(e)}",
                )

    def get_app(self) -> FastAPI:
        return self.app


def main():
    parser = argparse.ArgumentParser(description="OAuth2 Callback Server")
    parser.add_argument("-r", "--region", type=str, default="us-west-2")
    parser.add_argument("-p", "--port", type=int, default=OAUTH2_CALLBACK_PORT)
    args = parser.parse_args()

    server = OAuth2CallbackServer(region=args.region)

    logger.info(f"Starting OAuth2 callback server on 0.0.0.0:{args.port}")
    logger.info(f"Callback URL: http://localhost:{args.port}/oauth2/callback")

    uvicorn.run(server.get_app(), host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
