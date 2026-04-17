"""Web search and memory tools for the Strands-based web search agent."""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv
from strands import tool
from tavily import TavilyClient
from bedrock_agentcore.memory import MemoryClient
from memory_tools import create_memory_tools

# Load environment variables
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

logger = logging.getLogger(__name__)

MCP_REGION = os.getenv("MCP_REGION")
if not MCP_REGION:
    raise RuntimeError("Missing MCP_REGION environment variable")

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
if not TAVILY_API_KEY:
    raise RuntimeError("Missing TAVILY_API_KEY environment variable")

memory_client = MemoryClient(region_name=MCP_REGION)


@tool
def web_search(query: str, top_k: int = 5, recency_days: int = 0) -> dict:
    """Search the web using Tavily API for AWS documentation and solutions.

    Args:
        query: The search query string.
        top_k: Number of results to return (max 10).
        recency_days: Filter results by recency in days. 0 means no filter.

    Returns:
        Dictionary with search results, provider, and query.
    """
    tavily = TavilyClient(api_key=TAVILY_API_KEY)
    search_kwargs = {
        "query": query,
        "max_results": max(1, min(top_k, 10)),
    }
    if recency_days and recency_days > 0:
        if recency_days <= 1:
            search_kwargs["time_range"] = "day"
        elif recency_days <= 7:
            search_kwargs["time_range"] = "week"
        elif recency_days <= 30:
            search_kwargs["time_range"] = "month"
        else:
            search_kwargs["time_range"] = "year"

    res = tavily.search(**search_kwargs)
    results = []
    for item in res.get("results", []):
        results.append(
            {
                "title": item.get("title"),
                "url": item.get("url"),
                "snippet": item.get("content") or item.get("snippet"),
                "score": item.get("score"),
            }
        )
    return {"results": results, "provider": "tavily", "query": query}


def get_memory_tools(memory_id: str, actor_id: str, session_id: str) -> list:
    """Get memory tools using the initialized memory_id."""
    if memory_id:
        logger.info(f"Creating memory tools with memory_id: {memory_id}")
        return create_memory_tools(
            memory_id, memory_client, actor_id=actor_id, session_id=session_id
        )
    logger.info("No memory_id set, skipping memory tools")
    return []
