"""A2A Server for the Strands-based Web Search Agent — GCP Cloud Run version.

Runs as a standalone A2A server with Cognito JWT validation middleware,
since there's no AgentCore Runtime handling inbound auth on GCP.
"""

from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from agent_executor import WebSearchAgentExecutor
from auth_middleware import CognitoAuthMiddleware
from starlette.responses import JSONResponse
import logging
import os
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# On GCP Cloud Run, the URL is the Cloud Run service URL
runtime_url = os.getenv("SERVICE_URL", "http://127.0.0.1:9000/")
host, port = "0.0.0.0", int(os.getenv("PORT", "9000"))

agent_card = AgentCard(
    name="WebSearch Agent",
    description=(
        "Web search agent that provides AWS documentation and solutions "
        "by searching for relevant information (hosted on GCP Cloud Run)"
    ),
    url=runtime_url,
    version="0.3.0",
    defaultInputModes=["text/plain"],
    defaultOutputModes=["text/plain"],
    capabilities=AgentCapabilities(streaming=True, pushNotifications=False),
    skills=[
        AgentSkill(
            id="websearch",
            name="Web Search",
            description="Search AWS documentation and provide solutions",
            tags=["websearch", "aws", "documentation", "solutions"],
            examples=[
                "Find documentation for fixing high CPU usage in EC2",
                "Search for solutions to RDS connection timeout issues",
            ],
        ),
        AgentSkill(
            id="aws-documentation",
            name="AWS Documentation Search",
            description="Search and retrieve AWS documentation and best practices",
            tags=["aws", "documentation", "search"],
            examples=[
                "Search for AWS CloudWatch best practices",
                "Find AWS troubleshooting guides",
            ],
        ),
    ],
)

# Create request handler with Strands-based executor
request_handler = DefaultRequestHandler(
    agent_executor=WebSearchAgentExecutor(), task_store=InMemoryTaskStore()
)

# Create A2A server
server = A2AStarletteApplication(agent_card=agent_card, http_handler=request_handler)

# Build the app
app = server.build()

# Add Cognito JWT validation middleware (replaces AgentCore Runtime inbound auth)
app.add_middleware(CognitoAuthMiddleware)


async def ping(request):
    """Health check endpoint"""
    return JSONResponse({"status": "healthy"})


from starlette.routing import Route
app.routes.append(Route("/ping", endpoint=ping, methods=["GET"]))


logger.info("✅ A2A Server configured (Strands on GCP Cloud Run)")
logger.info(f"📍 Server URL: {runtime_url}")

if __name__ == "__main__":
    uvicorn.run(app, host=host, port=port)
