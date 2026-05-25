"""A2A Server for the Strands-based Web Search Agent — GCP Cloud Run version.

Uses OIDC (GCP Workload Identity Federation) for AWS credentials.
Credentials are set by entrypoint.sh before this module loads.
"""

import os
import logging
import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from agent_executor import WebSearchAgentExecutor
from agentcore_identity_auth import AgentCoreIdentityMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

# --- Server setup ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

request_handler = DefaultRequestHandler(
    agent_executor=WebSearchAgentExecutor(), task_store=InMemoryTaskStore()
)

server = A2AStarletteApplication(agent_card=agent_card, http_handler=request_handler)
app = server.build()
app.add_middleware(AgentCoreIdentityMiddleware)


async def ping(request):
    return JSONResponse({"status": "healthy"})


app.routes.append(Route("/ping", endpoint=ping, methods=["GET"]))

logger.info("✅ A2A Server configured (Strands on GCP Cloud Run)")

if __name__ == "__main__":
    uvicorn.run(app, host=host, port=port)
