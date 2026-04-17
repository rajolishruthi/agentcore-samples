"""Web Search Agent — Strands SDK version.

Converted from OpenAI Agents SDK. Uses Strands Agent with BedrockModel
and Tavily web search tool with AgentCore Memory integration.
"""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv
from strands import Agent
from strands.models import BedrockModel
from prompt import SYSTEM_PROMPT
from tools import web_search, get_memory_tools

# Load environment variables
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

logger = logging.getLogger(__name__)

MODEL_ID = os.getenv("MODEL_ID", "global.anthropic.claude-sonnet-4-20250514-v1:0")
MEMORY_ID = os.getenv("MEMORY_ID")
MCP_REGION = os.getenv("MCP_REGION")

if not MEMORY_ID:
    raise RuntimeError("Missing MEMORY_ID environment variable")
if not MCP_REGION:
    raise RuntimeError("Missing MCP_REGION environment variable")


class WebSearchAgent:
    """Strands-based web search agent with memory support."""

    SUPPORTED_CONTENT_TYPES = ["text", "text/plain"]

    def __init__(
        self,
        memory_id: str,
        model_id: str,
        region_name: str,
        actor_id: str,
        session_id: str,
    ):
        bedrock_model = BedrockModel(model_id=model_id, region_name=region_name)

        # Build tool list: web_search + memory tools
        memory_tools = get_memory_tools(
            memory_id=memory_id, actor_id=actor_id, session_id=session_id
        )
        all_tools = [web_search] + memory_tools

        self.agent = Agent(
            name="WebSearch Agent",
            description="Web search agent for finding AWS solutions, documentation, and best practices",
            system_prompt=SYSTEM_PROMPT,
            model=bedrock_model,
            tools=all_tools,
        )

    async def stream(self, query: str, session_id: str):
        """Stream agent response, yielding chunks compatible with A2A executor."""
        response = ""
        try:
            async for event in self.agent.stream_async(query):
                if "data" in event:
                    response += event["data"]
                    yield {
                        "is_task_complete": "complete" in event,
                        "require_user_input": False,
                        "content": event["data"],
                    }
        except Exception as e:
            yield {
                "is_task_complete": False,
                "require_user_input": True,
                "content": f"Unable to process request. Error: {e}",
            }
        finally:
            yield {
                "is_task_complete": True,
                "require_user_input": False,
                "content": response,
            }

    def invoke(self, query: str, session_id: str) -> str:
        """Synchronous invocation."""
        try:
            return str(self.agent(query))
        except Exception as e:
            raise RuntimeError(f"Error invoking agent: {e}")
